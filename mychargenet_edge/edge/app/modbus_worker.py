import asyncio
import logging
import math
import struct
import time
from datetime import UTC, datetime

import httpx
from pymodbus.client import AsyncModbusTcpClient

from app.backend_client import fetch_config, send_measurement
from app.identity_store import IdentityStore

logger = logging.getLogger(__name__)
CONFIG_TTL_SECONDS = 60
HEALTH_STATE = {
    "status": "disabled",
    "last_success_at": None,
    "last_error": None,
    "last_error_at": None,
    "connected_devices": [],
}


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"cloud_http_{exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "cloud_timeout"
    if isinstance(exc, httpx.NetworkError):
        return "cloud_unreachable"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "device_timeout"
    if isinstance(exc, ConnectionError):
        return "device_unreachable"
    if isinstance(exc, (ValueError, TypeError)):
        return "invalid_device_config_or_value"
    return type(exc).__name__.lower()


def _register(config: dict) -> dict:
    mapping = (config.get("measurement_map") or {}).get("grid_power_w") or {}
    def value(mapped, legacy, default=None):
        return mapping.get(mapped, config.get(legacy, default))
    address = value("address", "modbus_power_register")
    if address is None:
        raise ValueError("Leistungsregister fehlt")
    address = int(address)
    if not 0 <= address <= 65535: raise ValueError("Registeradresse ausserhalb 0..65535")
    word_order = str(value("word_order", "modbus_word_order", "big"))
    if word_order not in {"big", "little_swap"}: raise ValueError("Word-Order muss big oder little_swap sein")
    function_code = int(value("function_code", "modbus_function_code", 4))
    if function_code not in {3, 4}: raise ValueError("Function Code muss 3 oder 4 sein")
    return {
        "address": address,
        "data_type": str(value("data_type", "modbus_data_type", "float32")),
        "word_order": word_order,
        "function_code": function_code,
        "scale": float(value("scale", "modbus_power_scale", 1)),
        "unit": str(value("unit", "modbus_power_unit", "W")),
    }


def decode(registers: list[int], data_type: str, word_order: str, scale: float) -> float:
    words = list(registers)
    if word_order not in {"big", "little_swap"}: raise ValueError("Nicht unterstuetzte Word-Order")
    if word_order == "little_swap": words.reverse()
    raw = b"".join(int(word).to_bytes(2, "big") for word in words)
    formats = {"float32": ">f", "int32": ">i", "uint32": ">I", "int16": ">h", "uint16": ">H"}
    if data_type not in formats: raise ValueError("Nicht unterstuetzter Datentyp")
    result = float(struct.unpack(formats[data_type], raw)[0]) * scale
    if not math.isfinite(result): raise ValueError("Messwert ist nicht endlich")
    return result


async def poll(poller: dict) -> float:
    cfg, reg = poller.get("config") or {}, _register(poller.get("config") or {})
    if cfg.get("read_only") is not True:
        raise ValueError("Nur explizit lesende Modbus-Konfigurationen sind erlaubt")
    count = 1 if reg["data_type"] in {"int16", "uint16"} else 2
    client = AsyncModbusTcpClient(
        poller["host"],
        port=int(poller.get("port") or 502),
        timeout=float(cfg.get("timeout_seconds") or 3),
    )
    try:
        if not await client.connect(): raise ConnectionError("Modbus-Verbindung fehlgeschlagen")
        kwargs = {"address": reg["address"], "count": count, "slave": int(cfg.get("modbus_unit_id") or cfg.get("unit_id") or 1)}
        response = await (client.read_holding_registers(**kwargs) if reg["function_code"] == 3 else client.read_input_registers(**kwargs))
        if response.isError(): raise IOError("Modbus-Leseantwort meldet einen Fehler")
        watts = decode(response.registers, reg["data_type"], reg["word_order"], reg["scale"])
        if reg["unit"].lower() == "kw": watts *= 1000
        if abs(watts) > float(cfg.get("max_abs_grid_power_w") or 500_000): raise ValueError("Messwert unplausibel")
        return watts
    finally:
        client.close()


async def worker_loop(settings) -> None:
    identity_store = IdentityStore(settings.state_dir)
    cached_config, config_loaded_at = None, 0.0
    while True:
        try:
            identity = identity_store.load()
            if identity is None:
                HEALTH_STATE.update(status="waiting_for_pairing", connected_devices=[])
                await asyncio.sleep(settings.poll_interval_seconds)
                continue
            if cached_config is None or time.monotonic() - config_loaded_at >= CONFIG_TTL_SECONDS:
                cached_config = await fetch_config(
                    settings,
                    identity["device_credential"],
                    int(identity["site_id"]),
                )
                config_loaded_at = time.monotonic()
            config = cached_config
            if not config.get("pollers"):
                HEALTH_STATE.update(
                    status="waiting_for_device",
                    last_error=None,
                    last_error_at=None,
                    connected_devices=[],
                )
            for poller in config.get("pollers", []):
                try:
                    watts = await poll(poller)
                    await send_measurement(
                        settings,
                        identity["device_credential"],
                        int(identity["site_id"]),
                        {
                            "integration_id": poller["id"],
                            "sample_kind": "live",
                            "config_revision": config.get("config_revision"),
                            "grid_power_w": watts,
                            "building_power_w": max(0, watts),
                        },
                    )
                    HEALTH_STATE.update(
                        status="ok",
                        last_success_at=datetime.now(UTC).isoformat(),
                        last_error=None,
                        connected_devices=[f"modbus:{poller['id']}"],
                    )
                except Exception as exc:
                    error_code = _safe_error(exc)
                    HEALTH_STATE.update(
                        status="degraded",
                        last_error=error_code,
                        last_error_at=datetime.now(UTC).isoformat(),
                        connected_devices=[],
                    )
                    logger.warning(
                        "Edge Modbus read failed",
                        extra={"poller_id": poller.get("id"), "error_code": error_code},
                    )
                    continue
        except Exception as exc:
            cached_config = None
            error_code = _safe_error(exc)
            HEALTH_STATE.update(
                status="error",
                last_error=error_code,
                last_error_at=datetime.now(UTC).isoformat(),
                connected_devices=[],
            )
            logger.warning("Edge Modbus configuration unavailable", extra={"error_code": error_code})
        await asyncio.sleep(settings.poll_interval_seconds)
