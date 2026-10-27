import httpx
from app.config import Settings


def _headers(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


async def pair(settings: Settings, payload: dict) -> dict:
    url = f"{settings.backend_url.rstrip('/')}/api/edge/pair"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        return response.json()


async def send_heartbeat(settings: Settings, credential: str, payload: dict) -> dict:
    url = f"{settings.backend_url.rstrip('/')}/api/edge/heartbeat"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(url, json=payload, headers=_headers(credential))
        response.raise_for_status()
        return response.json()


async def send_events(settings: Settings, credential: str, events: list[dict]) -> dict:
    url = f"{settings.backend_url.rstrip('/')}/api/edge/events"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(url, json={"events": events}, headers=_headers(credential))
        response.raise_for_status()
        return response.json()


async def send_measurement(settings: Settings, credential: str, site_id: int, payload: dict) -> dict:
    url = f"{settings.backend_url.rstrip('/')}/api/edge/sites/{site_id}/measurements"
    headers = {
        **_headers(credential),
        "X-Gateway-Firmware": settings.firmware_version,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()


async def fetch_protocol(settings: Settings, credential: str) -> dict:
    url = f"{settings.backend_url.rstrip('/')}/api/edge/protocol"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url, headers=_headers(credential))
        response.raise_for_status()
        return response.json()


async def fetch_config(settings: Settings, credential: str, site_id: int) -> dict:
    url = f"{settings.backend_url.rstrip('/')}/api/edge/sites/{site_id}/config"
    headers = _headers(credential)
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
