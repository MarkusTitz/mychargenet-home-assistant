import asyncio
import logging
import random
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from app.backend_client import pair, send_events, send_heartbeat
from app.identity_store import IdentityStore
from app.home_assistant_worker import HEALTH_STATE as HOME_ASSISTANT_STATE
from app.modbus_worker import HEALTH_STATE as MODBUS_STATE
from app.queue_store import QueueStore


logger = logging.getLogger(__name__)
RUNTIME_STATE = {
    "status": "starting",
    "paired": False,
    "edge_uid": None,
    "site_id": None,
    "last_success_at": None,
    "last_error": None,
    "retry_in_seconds": 0,
    "queue_depth": 0,
}


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"cloud_http_{exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "cloud_timeout"
    if isinstance(exc, httpx.NetworkError):
        return "cloud_unreachable"
    return type(exc).__name__.lower()


async def ensure_identity(settings, store: IdentityStore) -> dict:
    identity = store.load()
    if identity:
        return identity
    if not settings.pairing_code:
        raise RuntimeError("EDGE_PAIRING_CODE is required for first-time pairing")
    result = await pair(settings, {
        "pairing_code": settings.pairing_code,
        "device_instance_id": store.device_instance_id(),
        "agent_version": settings.firmware_version,
        "capabilities": _capabilities(settings),
    })
    identity = {
        "device_instance_id": store.device_instance_id(),
        "edge_uid": result["edge_uid"],
        "site_id": result["site_id"],
        "device_credential": result["device_credential"],
        "credential_expires_at": result["credential_expires_at"],
        "config_version": result["config_version"],
    }
    store.save(identity)
    return identity


def _capabilities(settings) -> list[str]:
    capabilities = ["heartbeat", "offline_queue"]
    if settings.enable_modbus:
        capabilities.append("modbus_tcp_readonly")
    if settings.enable_home_assistant:
        capabilities.append("home_assistant_readonly")
    return capabilities


async def connector_loop(settings) -> None:
    identity_store = IdentityStore(settings.state_dir)
    queue = QueueStore(settings.state_dir, settings.queue_retention_days)
    queue.purge_expired()
    queue.enqueue("connector_started", {
        "agent_version": settings.firmware_version,
        "config_version": 1,
    })
    consecutive_failures = 0

    while True:
        try:
            identity = await ensure_identity(settings, identity_store)
            RUNTIME_STATE.update(
                paired=True,
                edge_uid=identity["edge_uid"],
                site_id=identity["site_id"],
            )
            queue_depth = queue.count()
            await send_heartbeat(settings, identity["device_credential"], {
                "heartbeat_id": str(uuid4()),
                "agent_version": settings.firmware_version,
                "capabilities": _capabilities(settings),
                "connected_devices": (
                    (list(MODBUS_STATE["connected_devices"]) if settings.enable_modbus else [])
                    + (["home_assistant"] if settings.enable_home_assistant and HOME_ASSISTANT_STATE["status"] in {"ok", "degraded"} else [])
                ),
                "pending_sync_count": queue_depth,
                "config_version": int(identity.get("config_version") or 1),
                "local_time": datetime.now(UTC).isoformat(),
            })

            if consecutive_failures:
                queue.enqueue("connectivity_restored", {
                    "agent_version": settings.firmware_version,
                    "reason_code": "cloud_reconnected",
                })
            events = queue.batch(50)
            if events:
                result = await send_events(
                    settings,
                    identity["device_credential"],
                    events,
                )
                queue.acknowledge(
                    result.get("accepted_event_ids", [])
                    + result.get("duplicate_event_ids", [])
                    + result.get("rejected_event_ids", [])
                )
            consecutive_failures = 0
            RUNTIME_STATE.update(
                status="ok",
                last_success_at=datetime.now(UTC).isoformat(),
                last_error=None,
                retry_in_seconds=settings.heartbeat_interval_seconds,
                queue_depth=queue.count(),
            )
            await asyncio.sleep(settings.heartbeat_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            consecutive_failures += 1
            base = min(
                settings.max_backoff_seconds,
                max(2, 2 ** min(consecutive_failures, 8)),
            )
            retry = min(
                settings.max_backoff_seconds,
                base + random.uniform(0, max(1, base * 0.2)),
            )
            safe_error = _safe_error(exc)
            RUNTIME_STATE.update(
                status="degraded",
                last_error=safe_error,
                retry_in_seconds=round(retry, 1),
                queue_depth=queue.count(),
            )
            logger.warning(
                "Customer edge cloud connection unavailable",
                extra={"error_code": safe_error, "retry_seconds": round(retry, 1)},
            )
            await asyncio.sleep(retry)
