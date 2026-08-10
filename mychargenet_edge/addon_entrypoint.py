import json
import os
import pwd
from pathlib import Path


OPTIONS_PATH = Path("/data/options.json")
STATE_DIR = Path("/data/mychargenet-edge")
ENTITY_OPTIONS = {
    "grid_power_entity": "EDGE_HA_GRID_POWER_ENTITY",
    "building_power_entity": "EDGE_HA_BUILDING_POWER_ENTITY",
    "pv_power_entity": "EDGE_HA_PV_POWER_ENTITY",
    "battery_power_entity": "EDGE_HA_BATTERY_POWER_ENTITY",
    "available_power_entity": "EDGE_HA_AVAILABLE_POWER_ENTITY",
    "phase_l1_entity": "EDGE_HA_PHASE_L1_ENTITY",
    "phase_l2_entity": "EDGE_HA_PHASE_L2_ENTITY",
    "phase_l3_entity": "EDGE_HA_PHASE_L3_ENTITY",
    "phase_l1_voltage_entity": "EDGE_HA_PHASE_L1_VOLTAGE_ENTITY",
    "phase_l2_voltage_entity": "EDGE_HA_PHASE_L2_VOLTAGE_ENTITY",
    "phase_l3_voltage_entity": "EDGE_HA_PHASE_L3_VOLTAGE_ENTITY",
    "phase_l1_current_entity": "EDGE_HA_PHASE_L1_CURRENT_ENTITY",
    "phase_l2_current_entity": "EDGE_HA_PHASE_L2_CURRENT_ENTITY",
    "phase_l3_current_entity": "EDGE_HA_PHASE_L3_CURRENT_ENTITY",
}


def load_options() -> dict:
    if not OPTIONS_PATH.exists():
        raise RuntimeError("Home Assistant options are unavailable")
    return json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))


def configure_environment(options: dict) -> None:
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not supervisor_token:
        raise RuntimeError("Home Assistant API access token is unavailable")
    os.environ.update({
        "EDGE_BACKEND_URL": os.environ.get(
            "MYCHARGENET_ADDON_BACKEND_URL_OVERRIDE",
            "https://api.mychargenet.com",
        ),
        "EDGE_PAIRING_CODE": str(options.get("pairing_code") or ""),
        "EDGE_FIRMWARE_VERSION": "0.6.1-ha-pilot",
        "EDGE_HARDWARE_PROFILE": "home_assistant",
        "EDGE_STATE_DIR": str(STATE_DIR),
        "EDGE_HEARTBEAT_INTERVAL_SECONDS": "30",
        "EDGE_MAX_BACKOFF_SECONDS": "300",
        "EDGE_QUEUE_RETENTION_DAYS": "30",
        "EDGE_POLL_INTERVAL_SECONDS": str(options.get("poll_interval_seconds") or 10),
        "EDGE_ENABLE_SIMULATOR": "false",
        "EDGE_ENABLE_MODBUS": "false",
        "EDGE_ENABLE_HOME_ASSISTANT": "true",
        "EDGE_HOME_ASSISTANT_URL": "http://supervisor/core/api",
        "EDGE_HOME_ASSISTANT_TOKEN": supervisor_token,
        "EDGE_HA_MAX_ENTITY_AGE_SECONDS": str(options.get("max_entity_age_seconds") or 120),
    })
    for option_name, environment_name in ENTITY_OPTIONS.items():
        os.environ[environment_name] = str(options.get(option_name) or "")


def drop_privileges() -> None:
    account = pwd.getpwnam("edge")
    if STATE_DIR.is_symlink():
        raise RuntimeError("State directory must not be a symbolic link")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_DIR.is_symlink() or not STATE_DIR.is_dir():
        raise RuntimeError("State directory is invalid")
    os.chown(STATE_DIR, account.pw_uid, account.pw_gid, follow_symlinks=False)
    os.setgroups([])
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)


def main() -> None:
    configure_environment(load_options())
    drop_privileges()
    os.execv(
        "/usr/local/bin/uvicorn",
        ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"],
    )


if __name__ == "__main__":
    main()
