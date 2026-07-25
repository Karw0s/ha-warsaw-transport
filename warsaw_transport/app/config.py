"""Runtime configuration, read from environment variables set by run.sh.

When developing outside the add-on, the same variables can be exported manually
or placed in a `.env`-style shell before launching.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _get_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    api_key: str
    poll_interval: int
    gps_overlay: bool
    vehicle_type: str  # "bus" or "tram"
    log_level: str
    data_dir: str

    # MQTT
    mqtt_host: str
    mqtt_port: int
    mqtt_user: str
    mqtt_password: str

    # Web server
    host: str
    port: int

    @property
    def mqtt_enabled(self) -> bool:
        return bool(self.mqtt_host)

    @property
    def vehicle_type_code(self) -> int:
        """Warsaw GPS API type code: 1 = bus, 2 = tram."""
        return 2 if self.vehicle_type == "tram" else 1


def load_settings() -> Settings:
    return Settings(
        api_key=os.environ.get("WT_API_KEY", "").strip(),
        poll_interval=max(10, _get_int("WT_POLL_INTERVAL", 30)),
        gps_overlay=_get_bool("WT_GPS_OVERLAY", True),
        vehicle_type=os.environ.get("WT_VEHICLE_TYPE", "bus").strip().lower(),
        log_level=os.environ.get("WT_LOG_LEVEL", "info").strip().lower(),
        data_dir=os.environ.get("WT_DATA_DIR", "/data"),
        mqtt_host=os.environ.get("WT_MQTT_HOST", "").strip(),
        mqtt_port=_get_int("WT_MQTT_PORT", 1883),
        mqtt_user=os.environ.get("WT_MQTT_USER", "").strip(),
        mqtt_password=os.environ.get("WT_MQTT_PASSWORD", ""),
        host=os.environ.get("WT_BIND_HOST", "0.0.0.0"),
        port=_get_int("WT_INGRESS_PORT", 8099),
    )
