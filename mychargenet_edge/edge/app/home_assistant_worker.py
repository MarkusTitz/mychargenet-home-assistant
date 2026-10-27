import asyncio
import hashlib
import json
import logging
import math
import re
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from app.backend_client import fetch_protocol, send_measurement
from app.identity_store import IdentityStore


logger = logging.getLogger(__name__)
ENTITY_ID_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
MEASUREMENT_FIELDS = {
    "grid_power_w": "ha_grid_power_entity",
    "building_power_w": "ha_building_power_entity",
    "pv_power_w": "ha_pv_power_entity",
    "battery_power_w": "ha_battery_power_entity",
    "available_power_w": "ha_available_power_entity",
    "phase_l1_w": "ha_phase_l1_entity",
    "phase_l2_w": "ha_phase_l2_entity",
    "phase_l3_w": "ha_phase_l3_entity",
    "phase_l1_voltage_v": "ha_phase_l1_voltage_entity",
    "phase_l2_voltage_v": "ha_phase_l2_voltage_entity",
    "phase_l3_voltage_v": "ha_phase_l3_voltage_entity",
    "phase_l1_current_a": "ha_phase_l1_current_entity",
    "phase_l2_current_a": "ha_phase_l2_current_entity",
    "phase_l3_current_a": "ha_phase_l3_current_entity",
}
CONTROL_REVISION_FIELDS = (
    "grid_power_w",
    "building_power_w",
    "phase_l1_w",
    "phase_l2_w",
    "phase_l3_w",
)
UNIT_FACTORS = {
    "power": {"W": 1.0, "kW": 1000.0, "MW": 1_000_000.0},
    "voltage": {"V": 1.0, "kV": 1000.0},
    "current": {"A": 1.0, "mA": 0.001},
}
HEALTH_STATE = {
    "status": "disabled",
    "last_success_at": None,
    "last_error": None,
    "last_error_at": None,
    "last_error_origin": None,  # D1: "home_assistant" | "cloud" | "edge"
    "configured_entities": 0,
    "confirmed_unchanged_entities": 0,
    "diagnostic_aged_entities": 0,
}


def configured_entities(settings) -> dict[str, str]:
    result = {}
    for measurement_field, settings_field in MEASUREMENT_FIELDS.items():
        entity_id = str(getattr(settings, settings_field, "") or "").strip().lower()
        if not entity_id:
            continue
        if not ENTITY_ID_PATTERN.fullmatch(entity_id):
            raise ValueError("invalid_home_assistant_entity_id")
        if measurement_field in CONTROL_REVISION_FIELDS and not entity_id.startswith("sensor."):
            raise ValueError("home_assistant_control_entity_requires_sensor_domain")
        result[measurement_field] = entity_id
    return result


def configuration_revision(entity_map: dict[str, str]) -> str:
    control_map = {
        field: entity_map[field]
        for field in CONTROL_REVISION_FIELDS
        if entity_map.get(field)
    }
    canonical = json.dumps(control_map, sort_keys=True, separators=(",", ":"))
    return f"ha-control:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def electrical_value(field: str, state: dict) -> float:
    try:
        value = float(state["state"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid_home_assistant_state") from exc
    if not math.isfinite(value):
        raise ValueError("invalid_home_assistant_state")
    unit = str((state.get("attributes") or {}).get("unit_of_measurement") or "").strip()
    quantity = "voltage" if field.endswith("_voltage_v") else "current" if field.endswith("_current_a") else "power"
    factors = UNIT_FACTORS[quantity]
    if unit not in factors:
        raise ValueError(f"unsupported_home_assistant_{quantity}_unit")
    return value * factors[unit]


def power_in_watts(state: dict) -> float:
    return electrical_value("grid_power_w", state)


# EDGE-KERN-WAHRHEIT1 (D2/D3): Home Assistant liefert keine physische Messzeit, sondern
#   last_changed  = letzte Aenderung des Zustandswerts in HA (massgeblich fuer changed_at),
#   last_updated  = letzte Zustands- ODER Attributaenderung (eine reine Attributaenderung ist keine neue Messung,
#                   daher nur Rueckfall, wenn last_changed fehlt) und
#   last_reported = letzte Meldung der Integration, auch bei unveraendertem Wert (optional).
# Zeitbasis aller Felder ist daher TIME_BASIS_HA ("ha_state_changed"): eine zeitnahe HA-Wertaenderung, keine belegte
# physische Quellzeit. Ob diese Zeitbasis regelungsfaehig ist, entscheidet ausschliesslich die Cloud-Inbetriebnahme.
# Qualitaet je Feld:
#   fresh               - Aenderung innerhalb der Frist; einzige Qualitaet, die als aktueller Messwert gilt.
#   confirmed_unchanged - Aenderung zu alt, aber die Integration hat den unveraenderten Zustand frisch gemeldet.
#                         Lebenszeichen, KEIN frischer Messwert: Wert nur in field_meta, nie als Messfeld.
#   diagnostic_aged     - Phasenwert innerhalb der bestehenden Phasen-Haltefrist (1800 s), aber aelter als die
#                         Primaerfrist. Bleibt wie bisher im Messfeld, ist aber markiert und nie Regelgrundlage.
QUALITY_FRESH = "fresh"
QUALITY_CONFIRMED = "confirmed_unchanged"
QUALITY_DIAGNOSTIC = "diagnostic_aged"
FUTURE_TOLERANCE_SECONDS = 5
TIME_BASIS_HA = "ha_state_changed"
REQUIRED_MEASUREMENT_PROTOCOL = "field-meta-v1"
QUALITY_UNAVAILABLE = "unavailable"


def _parse_timestamp(raw) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid_home_assistant_timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("invalid_home_assistant_timestamp")
    return parsed.astimezone(UTC)


def state_timestamp(state: dict) -> datetime:
    """Letzte Wertaenderung in HA (last_changed; last_updated nur als Rueckfall, da es auch Attributaenderungen zaehlt)."""
    raw = state.get("last_changed") or state.get("last_updated")
    if not raw:
        raise ValueError("missing_home_assistant_timestamp")
    return _parse_timestamp(raw)


def state_reported_timestamp(state: dict) -> datetime | None:
    """Letzte Meldung der Integration (HA last_reported); fehlt bei aelteren HA-Versionen."""
    raw = state.get("last_reported")
    return _parse_timestamp(raw) if raw else None


def classify_field(field: str, changed_at: datetime, reported_at: datetime | None, observed_at: datetime, settings) -> str:
    if (observed_at - changed_at).total_seconds() < -FUTURE_TOLERANCE_SECONDS:
        raise ValueError("future_home_assistant_state")
    if reported_at is not None and (observed_at - reported_at).total_seconds() < -FUTURE_TOLERANCE_SECONDS:
        raise ValueError("future_home_assistant_state")
    primary_max = settings.ha_max_entity_age_seconds
    age = (observed_at - changed_at).total_seconds()
    if age <= primary_max:
        return QUALITY_FRESH
    if reported_at is not None and (observed_at - reported_at).total_seconds() <= primary_max:
        return QUALITY_CONFIRMED
    if field.startswith("phase_") and age <= settings.ha_phase_max_entity_age_seconds:
        return QUALITY_DIAGNOSTIC
    raise ValueError("stale_home_assistant_state")


async def collect_measurement(settings, now: datetime | None = None) -> tuple[dict, int]:
    entity_map = configured_entities(settings)
    if not entity_map:
        raise RuntimeError("home_assistant_no_entities")
    observed_at = now or datetime.now(UTC)
    values = {}
    field_meta = {}
    fresh_primary = []
    fresh_phase = []
    reported_only = []
    skipped = 0
    last_error = None
    headers = {"Authorization": f"Bearer {settings.home_assistant_token}"}
    async with httpx.AsyncClient(timeout=10, headers=headers, follow_redirects=False) as client:
        for field, entity_id in entity_map.items():
            try:
                response = await client.get(
                    f"{settings.home_assistant_url.rstrip('/')}/states/{quote(entity_id, safe='._')}"
                )
                response.raise_for_status()
                state = response.json()
                if not isinstance(state, dict):
                    raise ValueError("invalid_home_assistant_state")
                changed_at = state_timestamp(state)
                reported_at = state_reported_timestamp(state)
                quality = classify_field(field, changed_at, reported_at, observed_at, settings)
                value = electrical_value(field, state)
                meta = {
                    "quality": quality,
                    "time_basis": TIME_BASIS_HA,
                    "changed_at": changed_at.isoformat(),
                    "reported_at": reported_at.isoformat() if reported_at else None,
                }
                if quality == QUALITY_CONFIRMED:
                    meta["value"] = value  # Lebenszeichen, nicht als Messfeld
                    reported_only.append(reported_at)
                else:
                    values[field] = value
                    if quality == QUALITY_FRESH:
                        (fresh_phase if field.startswith("phase_") else fresh_primary).append(changed_at)
                field_meta[field] = meta
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                skipped += 1
                last_error = exc
                # M2: ausgefallenes oder zu altes Feld ausdruecklich melden, damit die Cloud keinen aelteren Wert
                # weiter als aktuell zeigt. changed_at = Beobachtungszeit, kein Wert.
                field_meta[field] = {"quality": QUALITY_UNAVAILABLE, "time_basis": TIME_BASIS_HA,
                                     "changed_at": observed_at.isoformat(), "reported_at": None}
    if not values and not reported_only:
        if isinstance(last_error, httpx.HTTPError):
            raise last_error
        raise RuntimeError("home_assistant_no_fresh_measurements")
    # Momentaufnahme-Zeit: aelteste Aenderung der frischen Primaerfelder (wie bisher); ohne frisches Primaerfeld die
    # aelteste frische Phase; ohne frisches Feld die aelteste Meldezeit - dann traegt kein Feld die Qualitaet fresh.
    anchor = fresh_primary or fresh_phase or reported_only
    if not anchor:
        anchor = [datetime.fromisoformat(field_meta[f]["changed_at"]) for f in values]
    return {
        "sample_kind": "live",
        "config_revision": configuration_revision(entity_map),
        "home_assistant_entities": {f: entity_map[f] for f in CONTROL_REVISION_FIELDS if f in entity_map},
        "sampled_at": min(anchor).isoformat(),
        "observed_at": observed_at.isoformat(),
        "field_meta": field_meta,
        **values,
    }, skipped


def _safe_error(exc: Exception) -> str:
    """Fehler beim LESEN aus Home Assistant."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"home_assistant_http_{exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "home_assistant_timeout"
    if isinstance(exc, httpx.NetworkError):
        return "home_assistant_unreachable"
    if isinstance(exc, RuntimeError) and str(exc).startswith("home_assistant_"):
        return str(exc)
    return "home_assistant_read_failed"


class CloudProtocolUnsupported(RuntimeError):
    pass


PROTOCOL_STATE = {"confirmed": False}


def measurement_protocol_status() -> str:
    if PROTOCOL_STATE["confirmed"]:
        return "confirmed"
    if HEALTH_STATE.get("last_error") == "cloud_protocol_unsupported":
        return "unsupported"
    return "unknown"


async def ensure_measurement_protocol(settings, credential: str) -> None:
    if PROTOCOL_STATE["confirmed"]:
        return
    try:
        answer = await fetch_protocol(settings, credential)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {404, 405}:
            raise CloudProtocolUnsupported("cloud_protocol_unsupported") from exc
        raise
    protocols = answer.get("measurement_protocols") if isinstance(answer, dict) else None
    if not isinstance(protocols, list) or REQUIRED_MEASUREMENT_PROTOCOL not in protocols:
        raise CloudProtocolUnsupported("cloud_protocol_unsupported")
    PROTOCOL_STATE["confirmed"] = True


def _cloud_error(exc: Exception) -> str:
    """D1: Fehler beim SENDEN an die mychargenet-Cloud - nie als Home-Assistant-Fehler ausweisen."""
    if isinstance(exc, CloudProtocolUnsupported):
        return "cloud_protocol_unsupported"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"cloud_http_{exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "cloud_timeout"
    if isinstance(exc, httpx.NetworkError):
        return "cloud_unreachable"
    return "cloud_send_failed"


async def worker_loop(settings) -> None:
    identity_store = IdentityStore(settings.state_dir)
    try:
        entity_map = configured_entities(settings)
        HEALTH_STATE["configured_entities"] = len(entity_map)
    except ValueError as exc:
        message = ("Regelfelder (Summe/Netz und L1–L3) müssen sensor.* verwenden. "
            "Bitte die Add-on-Konfiguration auf passende Leistungssensoren ändern und das Add-on neu starten. "
            "Hilfsfelder dürfen andere gültige Domänen verwenden. Bis zur Korrektur werden keine HA-Messwerte übertragen."
            if str(exc) == "home_assistant_control_entity_requires_sensor_domain" else
            "Ungültige Home-Assistant-Entitäts-ID. Bitte die Add-on-Konfiguration prüfen und das Add-on neu starten; "
            "bis zur Korrektur werden keine HA-Messwerte übertragen.")
        HEALTH_STATE.update(
            status="configuration_error",
            last_error=message,
            last_error_origin="home_assistant",
            last_error_at=datetime.now(UTC).isoformat(),
        )
        logger.error(message)
        return
    await asyncio.sleep(min(2, settings.poll_interval_seconds))
    while True:
        try:
            identity = identity_store.load()
        except asyncio.CancelledError:
            raise
        except Exception:
            _record_error("edge_identity_invalid", "edge")
            logger.error("Edge identity file is unusable")
            await asyncio.sleep(settings.poll_interval_seconds)
            continue
        if identity is None:
            HEALTH_STATE.update(status="waiting_for_pairing", last_error=None, last_error_origin=None)
            await asyncio.sleep(settings.poll_interval_seconds)
            continue
        try:
            payload, skipped = await collect_measurement(settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_code = _safe_error(exc)
            _record_error(error_code, "home_assistant")
            logger.warning("Home Assistant read unavailable", extra={"error_code": error_code, "error_origin": "home_assistant"})
            await asyncio.sleep(settings.poll_interval_seconds)
            continue
        try:
            # P2-R05: nur an einen Server senden, der die Feldqualitaet nachweislich versteht (fail-closed).
            await ensure_measurement_protocol(settings, identity["device_credential"])
            answer = await send_measurement(
                settings,
                identity["device_credential"],
                int(identity["site_id"]),
                payload,
            )
            # N-D2-1: jede Annahme muss das Protokoll erneut bestaetigen (z. B. Backend-Rueckrollung zur Laufzeit).
            if not isinstance(answer, dict) or answer.get("measurement_protocol") != REQUIRED_MEASUREMENT_PROTOCOL:
                PROTOCOL_STATE["confirmed"] = False
                raise CloudProtocolUnsupported("cloud_protocol_unsupported")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_code = _cloud_error(exc)
            _record_error(error_code, "cloud")
            logger.warning("Cloud measurement send failed", extra={"error_code": error_code, "error_origin": "cloud"})
            await asyncio.sleep(settings.poll_interval_seconds)
            continue
        now = datetime.now(UTC).isoformat()
        qualities = [meta.get("quality") for meta in (payload.get("field_meta") or {}).values()]
        HEALTH_STATE.update(
            status="degraded" if skipped else "ok",
            last_success_at=now,
            last_error="home_assistant_partial_data" if skipped else None,
            last_error_at=now if skipped else None,
            last_error_origin="home_assistant" if skipped else None,
            confirmed_unchanged_entities=qualities.count(QUALITY_CONFIRMED),
            diagnostic_aged_entities=qualities.count(QUALITY_DIAGNOSTIC),
        )
        await asyncio.sleep(settings.poll_interval_seconds)


def _record_error(error_code: str, origin: str) -> None:
    HEALTH_STATE.update(
        status="degraded",
        last_error=error_code,
        last_error_at=datetime.now(UTC).isoformat(),
        last_error_origin=origin,
    )
