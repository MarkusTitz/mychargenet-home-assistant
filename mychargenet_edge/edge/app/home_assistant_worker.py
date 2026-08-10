import asyncio
import hashlib
import json
import logging
import math
import re
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from app.backend_client import send_measurement
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
    "configured_entities": 0,
}


def configured_entities(settings) -> dict[str, str]:
    result = {}
    for measurement_field, settings_field in MEASUREMENT_FIELDS.items():
        entity_id = str(getattr(settings, settings_field, "") or "").strip().lower()
        if not entity_id:
            continue
        if not ENTITY_ID_PATTERN.fullmatch(entity_id):
            raise ValueError("invalid_home_assistant_entity_id")
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


def state_timestamp(state: dict) -> datetime:
    raw = state.get("last_updated") or state.get("last_changed")
    if not raw:
        raise ValueError("missing_home_assistant_timestamp")
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid_home_assistant_timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("invalid_home_assistant_timestamp")
    return parsed.astimezone(UTC)


async def collect_measurement(settings, now: datetime | None = None) -> tuple[dict, int]:
    entity_map = configured_entities(settings)
    if not entity_map:
        raise RuntimeError("home_assistant_no_entities")
    observed_at = now or datetime.now(UTC)
    values = {}
    timestamps = []
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
                sampled_at = state_timestamp(state)
                age_seconds = (observed_at - sampled_at).total_seconds()
                max_age_seconds = (
                    settings.ha_phase_max_entity_age_seconds
                    if field.startswith("phase_")
                    else settings.ha_max_entity_age_seconds
                )
                if age_seconds < -5 or age_seconds > max_age_seconds:
                    raise ValueError("stale_home_assistant_state")
                values[field] = electrical_value(field, state)
                timestamps.append(sampled_at)
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                skipped += 1
                last_error = exc
    if not values:
        if isinstance(last_error, httpx.HTTPError):
            raise last_error
        raise RuntimeError("home_assistant_no_fresh_measurements")
    return {
        "sample_kind": "live",
        "config_revision": configuration_revision(entity_map),
        "sampled_at": min(timestamps).isoformat(),
        **values,
    }, skipped


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"home_assistant_http_{exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "home_assistant_timeout"
    if isinstance(exc, httpx.NetworkError):
        return "home_assistant_unreachable"
    if isinstance(exc, RuntimeError) and str(exc).startswith("home_assistant_"):
        return str(exc)
    return "home_assistant_read_failed"


async def worker_loop(settings) -> None:
    identity_store = IdentityStore(settings.state_dir)
    try:
        entity_map = configured_entities(settings)
        HEALTH_STATE["configured_entities"] = len(entity_map)
    except ValueError:
        HEALTH_STATE.update(
            status="configuration_error",
            last_error="invalid_home_assistant_entity_id",
            last_error_at=datetime.now(UTC).isoformat(),
        )
        logger.error("Home Assistant configuration is invalid")
        return
    await asyncio.sleep(min(2, settings.poll_interval_seconds))
    while True:
        try:
            identity = identity_store.load()
            if identity is None:
                HEALTH_STATE.update(status="waiting_for_pairing", last_error=None)
                await asyncio.sleep(settings.poll_interval_seconds)
                continue
            payload, skipped = await collect_measurement(settings)
            await send_measurement(
                settings,
                identity["device_credential"],
                int(identity["site_id"]),
                payload,
            )
            now = datetime.now(UTC).isoformat()
            HEALTH_STATE.update(
                status="degraded" if skipped else "ok",
                last_success_at=now,
                last_error="home_assistant_partial_data" if skipped else None,
                last_error_at=now if skipped else None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_code = _safe_error(exc)
            HEALTH_STATE.update(
                status="degraded",
                last_error=error_code,
                last_error_at=datetime.now(UTC).isoformat(),
            )
            logger.warning("Home Assistant read unavailable", extra={"error_code": error_code})
        await asyncio.sleep(settings.poll_interval_seconds)
