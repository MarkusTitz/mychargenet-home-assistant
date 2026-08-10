import logging
import re
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict


logger = logging.getLogger(__name__)
RECOMMENDED_HARDWARE_PROFILES = {"siemens_iot2050", "raspberry_pi_5", "generic_linux", "home_assistant"}
HOME_ASSISTANT_ENTITY_PATTERN = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
HOME_ASSISTANT_ENTITY_FIELDS = (
    "ha_grid_power_entity",
    "ha_building_power_entity",
    "ha_pv_power_entity",
    "ha_battery_power_entity",
    "ha_available_power_entity",
    "ha_phase_l1_entity",
    "ha_phase_l2_entity",
    "ha_phase_l3_entity",
    "ha_phase_l1_voltage_entity",
    "ha_phase_l2_voltage_entity",
    "ha_phase_l3_voltage_entity",
    "ha_phase_l1_current_entity",
    "ha_phase_l2_current_entity",
    "ha_phase_l3_current_entity",
)


class Settings(BaseSettings):
    backend_url: str = "http://backend:8000"
    pairing_code: str = ""
    firmware_version: str = "0.4.0"
    hardware_profile: str = "generic_linux"
    state_dir: str = "/var/lib/mychargenet-edge"
    heartbeat_interval_seconds: int = 30
    max_backoff_seconds: int = 300
    queue_retention_days: int = 30
    mqtt_url: str | None = None
    poll_interval_seconds: int = 10
    enable_simulator: bool = False
    enable_modbus: bool = False
    enable_home_assistant: bool = False
    home_assistant_url: str = "http://supervisor/core/api"
    home_assistant_token: str = ""
    ha_grid_power_entity: str = ""
    ha_building_power_entity: str = ""
    ha_pv_power_entity: str = ""
    ha_battery_power_entity: str = ""
    ha_available_power_entity: str = ""
    ha_phase_l1_entity: str = ""
    ha_phase_l2_entity: str = ""
    ha_phase_l3_entity: str = ""
    ha_phase_l1_voltage_entity: str = ""
    ha_phase_l2_voltage_entity: str = ""
    ha_phase_l3_voltage_entity: str = ""
    ha_phase_l1_current_entity: str = ""
    ha_phase_l2_current_entity: str = ""
    ha_phase_l3_current_entity: str = ""
    ha_max_entity_age_seconds: int = 120

    model_config = SettingsConfigDict(
        env_prefix="EDGE_",
        env_file=".env",
        extra="ignore",
    )


def get_settings() -> Settings:
    settings = Settings()
    parsed = urlparse(settings.backend_url)
    if parsed.scheme not in {"https", "http"}:
        raise RuntimeError("EDGE_BACKEND_URL must use HTTPS")
    if parsed.scheme == "http" and (parsed.hostname or "") not in {"backend", "localhost", "127.0.0.1"}:
        raise RuntimeError("EDGE_BACKEND_URL must use HTTPS outside the local container network")
    if settings.hardware_profile not in RECOMMENDED_HARDWARE_PROFILES:
        logger.warning(
            "Unknown EDGE_HARDWARE_PROFILE %s; continuing as a generic Linux gateway",
            settings.hardware_profile,
        )
    settings.poll_interval_seconds = max(2, min(settings.poll_interval_seconds, 3600))
    settings.heartbeat_interval_seconds = max(10, min(settings.heartbeat_interval_seconds, 120))
    settings.max_backoff_seconds = max(30, min(settings.max_backoff_seconds, 3600))
    settings.queue_retention_days = max(1, min(settings.queue_retention_days, 30))
    settings.ha_max_entity_age_seconds = max(10, min(settings.ha_max_entity_age_seconds, 300))
    if settings.enable_home_assistant:
        ha_url = urlparse(settings.home_assistant_url)
        if ha_url.scheme not in {"http", "https"}:
            raise RuntimeError("EDGE_HOME_ASSISTANT_URL must use HTTP or HTTPS")
        if ha_url.scheme == "http" and (ha_url.hostname or "") not in {"supervisor", "localhost", "127.0.0.1"}:
            raise RuntimeError("EDGE_HOME_ASSISTANT_URL may use HTTP only for the local Supervisor proxy")
        if not settings.home_assistant_token:
            raise RuntimeError("EDGE_HOME_ASSISTANT_TOKEN is required when Home Assistant is enabled")
        entity_ids = [str(getattr(settings, field) or "").strip().lower() for field in HOME_ASSISTANT_ENTITY_FIELDS]
        if not any(entity_ids):
            raise RuntimeError("At least one EDGE_HA_*_ENTITY is required when Home Assistant is enabled")
        if any(entity_id and not HOME_ASSISTANT_ENTITY_PATTERN.fullmatch(entity_id) for entity_id in entity_ids):
            raise RuntimeError("EDGE_HA_*_ENTITY values must be valid Home Assistant entity IDs")
    return settings
