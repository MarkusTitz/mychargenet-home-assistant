import json
import os
from pathlib import Path
from uuid import uuid4


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

    def save(self, identity: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(
            self.path,
            json.dumps(identity, ensure_ascii=True, separators=(",", ":")),
        )

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(content, encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
