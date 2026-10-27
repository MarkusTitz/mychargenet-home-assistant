import asyncio
import logging
import math
import struct
import time
from datetime import UTC, datetime

import httpx
from pymodbus import FramerType
from pymodbus.client import AsyncModbusTcpClient

from app.backend_client import fetch_config, send_measurement
from app.home_assistant_worker import PROTOCOL_STATE, CloudProtocolUnsupported, ensure_measurement_protocol
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
    if type(exc).__name__ == "CloudProtocolUnsupported":
        return "cloud_protocol_unsupported"
    if str(exc) in {"all_registers_unavailable", "no_transmittable_registers"}:
        return str(exc)
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


# ---------------------------------------------------------------------------------------------------------------------
# EDGE-LOKAL1 (P4): profilbasierter Mehrregister-Leser. Gilt nur fuer Schnittstellen mit uebernommenem Profil
# (config.modbus_transport gesetzt). Der bisherige Einzelregister-Pfad bleibt unveraendert.
#   modbus_tcp          echtes Modbus TCP (MBAP), z. B. 410s als RTU/TCP-Gateway
#   modbus_rtu_over_tcp transparenter RTU-Tunnel ueber TCP (RTU-Rahmen inkl. CRC)
#   modbus_rtu          direkt seriell; benoetigt pyserial und eine ausdruecklich zugeordnete Geraetedatei
# Nur FC03/FC04. Zeitbasis edge_polled = Zeitpunkt der Abfrage durch die Edge (kein belegter Sensor-Messzeitpunkt).
TIME_BASIS_POLLED = "edge_polled"
PROFILE_FIELD_MAP = {
    "grid_power_w": "grid_power_w", "pv_power_w": "pv_power_w", "battery_power_w": "battery_power_w",
    "building_power_w": "building_power_w",
    "power_l1_w": "phase_l1_w", "power_l2_w": "phase_l2_w", "power_l3_w": "phase_l3_w",
    "voltage_l1_v": "phase_l1_voltage_v", "voltage_l2_v": "phase_l2_voltage_v", "voltage_l3_v": "phase_l3_voltage_v",
    "current_l1_a": "phase_l1_current_a", "current_l2_a": "phase_l2_current_a", "current_l3_a": "phase_l3_current_a",
}
PROFILE_COUNTS = {"float32": 2, "int32": 2, "uint32": 2, "int16": 1, "uint16": 1}
SERIAL_PARITY = {"none": "N", "even": "E", "odd": "O"}


def profile_client(poller: dict):
    cfg = poller.get("config") or {}
    transport = cfg.get("modbus_transport")
    timeout = float(cfg.get("timeout_seconds") or 3)
    if transport in {"modbus_tcp", "modbus_rtu_over_tcp"}:
        framer = FramerType.RTU if transport == "modbus_rtu_over_tcp" else FramerType.SOCKET
        return AsyncModbusTcpClient(poller["host"], port=int(poller.get("port") or 502), framer=framer, timeout=timeout,
                                    retries=0)
    if transport == "modbus_rtu":
        serial_cfg = cfg.get("modbus_serial") or {}
        device = str(cfg.get("serial_device") or "")
        if not device.startswith("/dev/"):
            raise ValueError("serial_device_not_assigned")  # Geraetezuordnung erfolgt ausdruecklich vor Ort
        try:
            from pymodbus.client import AsyncModbusSerialClient
            import serial  # noqa: F401  (pyserial; ohne Laufzeitbibliothek kein serieller Betrieb)
        except ImportError as exc:
            raise RuntimeError("serial_runtime_missing") from exc
        return AsyncModbusSerialClient(device, baudrate=int(serial_cfg["baudrate"]), parity=SERIAL_PARITY[serial_cfg["parity"]],
                                       bytesize=int(serial_cfg.get("data_bits", 8)), stopbits=int(serial_cfg.get("stop_bits", 1)),
                                       timeout=timeout, retries=0)
    raise ValueError("unsupported_modbus_transport")


def _profile_entry(entry: dict) -> dict:
    data_type = str(entry.get("data_type"))
    if data_type not in PROFILE_COUNTS:
        raise ValueError("Nicht unterstuetzter Datentyp")
    function_code = int(entry.get("function_code"))
    if function_code not in {3, 4}:
        raise ValueError("Nur FC03/FC04")
    address = int(entry.get("address"))
    count = PROFILE_COUNTS[data_type]
    if not 0 <= address or address + count - 1 > 65535:
        raise ValueError("Registeradresse ausserhalb 0..65535")
    return {"address": address, "count": count, "function_code": function_code, "data_type": data_type,
            "word_order": str(entry.get("word_order") or "big"), "scale": float(entry.get("scale") or 1),
            "unit_id": int(entry.get("unit_id") or 1), "sign": entry.get("sign")}


async def poll_profile(poller: dict, now=None, serial_device: str = "") -> tuple[dict, dict]:
    """Liest alle Profilregister; je Messgroesse Wert oder Fehlercode. Rueckgabe: (Werte, Fehler)."""
    cfg = poller.get("config") or {}
    if cfg.get("read_only") is not True:
        raise ValueError("Nur explizit lesende Modbus-Konfigurationen sind erlaubt")
    # Nur Messgroessen lesen, die ins Portal uebertragen werden (Kimi-Beobachtung 3); Energie/Frequenz bleiben aus.
    entries = {k: v for k, v in (cfg.get("measurement_map") or {}).items() if k in PROFILE_FIELD_MAP}
    values, errors = {}, {}
    # Geraetedatei ausschliesslich aus der lokalen Zuordnung; ein Wert aus der Cloud-Konfiguration wird verworfen.
    poller = {**poller, "config": {**{k: v for k, v in cfg.items() if k != "serial_device"},
                                   **({"serial_device": serial_device} if serial_device else {})}}
    client = profile_client(poller)
    try:
        if not await client.connect():
            raise ConnectionError("Modbus-Verbindung fehlgeschlagen")
        for measurement, raw_entry in entries.items():
            try:
                reg = _profile_entry(raw_entry)
                kwargs = {"address": reg["address"], "count": reg["count"], "slave": reg["unit_id"]}
                response = await (client.read_holding_registers(**kwargs) if reg["function_code"] == 3
                                  else client.read_input_registers(**kwargs))
                if response.isError():
                    raise IOError("modbus_exception")
                value = decode(response.registers, reg["data_type"], reg["word_order"], reg["scale"])
                if reg["sign"] == "export_positive":
                    value = -value  # normalisiert: Bezug positiv
                values[measurement] = value
            except Exception as exc:  # je Register: Ausfall wird als unavailable gemeldet, nie als alter Wert
                errors[measurement] = _safe_error(exc) if not isinstance(exc, IOError) else "modbus_exception"
    finally:
        client.close()
    return values, errors


def profile_payload(poller: dict, config_revision, values: dict, errors: dict, polled_at: datetime) -> dict:
    """Messgroessen -> Edge-Payload mit Feldqualitaet (Zeitbasis edge_polled). Energie/Frequenz bleiben lokal."""
    payload = {"integration_id": poller["id"], "sample_kind": "live", "config_revision": config_revision,
               "sampled_at": polled_at.isoformat(), "field_meta": {}}
    stamp = polled_at.isoformat()
    for measurement, field in PROFILE_FIELD_MAP.items():
        if measurement in values:
            payload[field] = values[measurement]
            payload["field_meta"][field] = {"quality": "fresh", "time_basis": TIME_BASIS_POLLED, "changed_at": stamp}
        elif measurement in errors:
            payload["field_meta"][field] = {"quality": "unavailable", "time_basis": TIME_BASIS_POLLED, "changed_at": stamp}
    if not payload["field_meta"]:
        payload.pop("field_meta")
    return payload


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
                if (poller.get("config") or {}).get("modbus_transport"):
                    try:
                        polled_at = datetime.now(UTC)
                        try:
                            values, errors = await poll_profile(poller, serial_device=getattr(settings, "modbus_serial_device", ""))
                        except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
                            # P4-R05: Verbindungsausfall -> alle Register ausdruecklich als Datenluecke melden
                            values = {}
                            errors = {name: _safe_error(exc) for name in ((poller.get("config") or {}).get("measurement_map") or {})}
                        payload = profile_payload(poller, poller.get("config_revision") or config.get("config_revision"), values, errors, polled_at)
                        if "field_meta" not in payload:
                            raise IOError("no_transmittable_registers")
                        # Feldqualitaet nur an einen Server, der sie nachweislich versteht (P2-R05); jede Annahme pruefen (P4-R06)
                        await ensure_measurement_protocol(settings, identity["device_credential"])
                        answer = await send_measurement(settings, identity["device_credential"], int(identity["site_id"]), payload)
                        if not isinstance(answer, dict) or answer.get("measurement_protocol") != "field-meta-v1":
                            PROTOCOL_STATE["confirmed"] = False
                            raise CloudProtocolUnsupported("cloud_protocol_unsupported")
                        if not values:
                            raise IOError("all_registers_unavailable")  # Luecke wurde gemeldet; Health bleibt degraded
                        HEALTH_STATE.update(status="degraded" if errors else "ok", last_success_at=datetime.now(UTC).isoformat(),
                                            last_error=("modbus_partial_data" if errors else None),
                                            connected_devices=[f"modbus:{poller['id']}"])
                    except Exception as exc:
                        error_code = _safe_error(exc)
                        HEALTH_STATE.update(status="degraded", last_error=error_code,
                                            last_error_at=datetime.now(UTC).isoformat(), connected_devices=[])
                        logger.warning("Edge Modbus profile read failed", extra={"poller_id": poller.get("id"), "error_code": error_code})
                    continue
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
