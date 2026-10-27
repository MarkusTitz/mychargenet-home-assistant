import asyncio
import httpx
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from app.backend_client import send_measurement
from app.config import get_settings
from app.connector import RUNTIME_STATE, connector_loop
from app.identity_store import IdentityStore
from app.home_assistant_worker import HEALTH_STATE as HOME_ASSISTANT_STATE, measurement_protocol_status, worker_loop as home_assistant_worker_loop
from app.modbus_worker import HEALTH_STATE as MODBUS_STATE, worker_loop
from app.mqtt_client import mqtt_configured
from app.queue_store import QueueStore


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    tasks = [asyncio.create_task(connector_loop(settings))]
    if settings.enable_modbus:
        MODBUS_STATE["status"] = "starting"
        tasks.append(asyncio.create_task(worker_loop(settings)))
    if settings.enable_home_assistant:
        HOME_ASSISTANT_STATE["status"] = "starting"
        tasks.append(asyncio.create_task(home_assistant_worker_loop(settings)))
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="mychargenet.com Edge EMS Gateway", lifespan=lifespan)


class MeasurementRequest(BaseModel):
    grid_power_w: float | None = None
    building_power_w: float | None = 3000
    pv_power_w: float | None = None
    battery_power_w: float | None = None
    available_power_w: float | None = 11000
    phase_l1_w: float | None = None
    phase_l2_w: float | None = None
    phase_l3_w: float | None = None
    phase_l1_voltage_v: float | None = None
    phase_l2_voltage_v: float | None = None
    phase_l3_voltage_v: float | None = None
    phase_l1_current_a: float | None = None
    phase_l2_current_a: float | None = None
    phase_l3_current_a: float | None = None


@app.get("/health")
async def health():
    settings = get_settings()
    queue_depth = QueueStore(settings.state_dir, settings.queue_retention_days).count()
    return {
        "status": RUNTIME_STATE["status"],
        "paired": RUNTIME_STATE["paired"],
        "edge_uid": RUNTIME_STATE["edge_uid"],
        "site_id": RUNTIME_STATE["site_id"],
        "hardware_profile": settings.hardware_profile,
        "queue_depth": queue_depth,
        "last_success_at": RUNTIME_STATE["last_success_at"],
        "last_error": RUNTIME_STATE["last_error"],
        "retry_in_seconds": RUNTIME_STATE["retry_in_seconds"],
        "phase_2_adapters": {
            "mqtt_configured": mqtt_configured(settings.mqtt_url),
            "modbus_enabled": settings.enable_modbus,
            "modbus_status": MODBUS_STATE["status"],
            "modbus_last_success_at": MODBUS_STATE["last_success_at"],
            "modbus_last_error": MODBUS_STATE["last_error"],
            "connected_devices": list(MODBUS_STATE["connected_devices"]),
            "home_assistant_enabled": settings.enable_home_assistant,
            "home_assistant_status": HOME_ASSISTANT_STATE["status"],
            "home_assistant_last_success_at": HOME_ASSISTANT_STATE["last_success_at"],
            "home_assistant_last_error": HOME_ASSISTANT_STATE["last_error"],
            "home_assistant_last_error_origin": HOME_ASSISTANT_STATE["last_error_origin"],
            # B1: Messprotokoll der Cloud (confirmed/unsupported/unknown) - Installer prueft es bei aktivem HA-Pfad.
            "measurement_protocol": measurement_protocol_status(),
            "home_assistant_confirmed_unchanged_entities": HOME_ASSISTANT_STATE["confirmed_unchanged_entities"],
            "home_assistant_diagnostic_aged_entities": HOME_ASSISTANT_STATE["diagnostic_aged_entities"],
            "home_assistant_entities": HOME_ASSISTANT_STATE["configured_entities"],
        },
    }


@app.post("/simulate-measurement")
async def simulate_measurement(body: MeasurementRequest):
    settings = get_settings()
    if not settings.enable_simulator:
        raise HTTPException(status_code=404, detail="Simulator ist deaktiviert")
    identity = IdentityStore(settings.state_dir).load()
    if identity is None:
        raise HTTPException(status_code=409, detail="Edge ist noch nicht gekoppelt")
    payload = {
        "sample_kind": "simulation",
        "config_revision": "edge-simulator-v1",
        "grid_power_w": body.grid_power_w,
        "building_power_w": body.building_power_w,
        "pv_power_w": body.pv_power_w,
        "battery_power_w": body.battery_power_w,
        "available_power_w": body.available_power_w,
        "phase_l1_w": body.phase_l1_w,
        "phase_l2_w": body.phase_l2_w,
        "phase_l3_w": body.phase_l3_w,
        "phase_l1_voltage_v": body.phase_l1_voltage_v,
        "phase_l2_voltage_v": body.phase_l2_voltage_v,
        "phase_l3_voltage_v": body.phase_l3_voltage_v,
        "phase_l1_current_a": body.phase_l1_current_a,
        "phase_l2_current_a": body.phase_l2_current_a,
        "phase_l3_current_a": body.phase_l3_current_a,
    }
    try:
        return await send_measurement(
            settings,
            identity["device_credential"],
            int(identity["site_id"]),
            payload,
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
