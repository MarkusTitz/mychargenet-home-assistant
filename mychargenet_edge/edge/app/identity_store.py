import hashlib
import json
import logging
import os
import re
import secrets
from pathlib import Path
from uuid import uuid4

# Muss exakt zum Server-Vertrag passen (EdgePairRequest.device_credential); sonst neuer Schluessel statt 422-Schleife.
DEVICE_CREDENTIAL_PATTERN = re.compile(r"^mc_edge_[A-Za-z0-9_-]{40,112}$")
LOGGER = logging.getLogger(__name__)


class IdentityStore:
    def __init__(self, state_dir: str) -> None:
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "identity.json"

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        data = json.loads(self.path.read_text(encoding="utf-8"))
        required = {"device_instance_id", "edge_uid", "site_id", "device_credential"}
        if not required.issubset(data):
            raise RuntimeError("Edge identity file is incomplete")
        return data

    def device_instance_id(self) -> str:
        identity = self.load()
        if identity:
            return str(identity["device_instance_id"])
        marker = self.state_dir / "device-instance-id"
        if marker.exists():
            return marker.read_text(encoding="ascii").strip()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        value = str(uuid4())
        self._atomic_write(marker, value)
        return value

    def pending_pairing(self, pairing_code: str) -> dict:
        """EDGE-KOPPLUNG1 Wiederaufnahme v2: Geraeteschluessel fuer diesen Kopplungsversuch, VOR dem Versuch dauerhaft
        gespeichert (0600). Jede Wiederholung desselben Versuchs (gleicher Code) sendet denselben Schluessel - der Server
        antwortet idempotent, das Geraet kann sich nie selbst aussperren. Neuer Code -> neuer Schluessel.
        Schlaegt das Speichern fehl, wird keine Kopplung versucht."""
        path = self.state_dir / "pairing-pending.json"
        code_digest = hashlib.sha256(pairing_code.encode("utf-8")).hexdigest()
        if path.exists():
            try:
                pending = json.loads(path.read_text(encoding="utf-8"))
                if (isinstance(pending, dict) and pending.get("code_digest") == code_digest
                        and DEVICE_CREDENTIAL_PATTERN.fullmatch(str(pending.get("device_credential", "")))):
                    return pending
            except (ValueError, OSError):
                pass
        self.state_dir.mkdir(parents=True, exist_ok=True)
        pending = {"code_digest": code_digest, "device_credential": "mc_edge_" + secrets.token_urlsafe(32)}
        self._atomic_write(path, json.dumps(pending, ensure_ascii=True, separators=(",", ":")))
        if json.loads(path.read_text(encoding="utf-8")) != pending:
            raise RuntimeError("Edge pairing state could not be stored")
        return pending

    def clear_pending_pairing(self) -> None:
        """Nur Aufraeumen: ein Loeschfehler darf eine fertige Identitaet nie sperren (naechster Lauf versucht es erneut)."""
        for name in ("pairing-pending.json", "pairing-proof"):
            try:
                (self.state_dir / name).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                LOGGER.warning("edge_pairing_cleanup_failed")

    def discard_pending_pairing(self) -> None:
        """Server hat den vorbereiteten Schluessel formal abgelehnt (422): verwerfen, naechster Versuch erzeugt neu."""
        try:
            (self.state_dir / "pairing-pending.json").unlink()
        except FileNotFoundError:
            pass
        except OSError:
            # Loeschen nicht moeglich: Datei unbrauchbar machen, damit der naechste Versuch neu erzeugt (keine Schleife).
            LOGGER.warning("edge_pairing_discard_failed")
            try:
                self._atomic_write(self.state_dir / "pairing-pending.json", "{}")
            except OSError:
                pass

    def save(self, identity: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(
            self.path,
            json.dumps(identity, ensure_ascii=True, separators=(",", ":")),
        )

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        # Von Anfang an 0600 (kein kurz lesbares Zwischenfile) und vor dem Umbenennen auf den Datentraeger geschrieben.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                LOGGER.warning("edge_state_fsync_failed")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
        # Umbenennen dauerhaft machen (Verzeichnis-fsync; auf Systemen ohne Verzeichnis-Handle wirkungslos).
        try:
            directory = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory = None
        if directory is not None:
            try:
                os.fsync(directory)
            except OSError:
                pass
            finally:
                os.close(directory)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
