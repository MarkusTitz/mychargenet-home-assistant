"""Placeholder for the local EMS MQTT integration.

The first Edge version does not open an MQTT connection automatically. This
keeps local tests predictable and prevents accidental control loops.
"""


def mqtt_configured(mqtt_url: str | None) -> bool:
    return bool(mqtt_url)
