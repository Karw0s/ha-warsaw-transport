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
    log_level: str
    data_dir: str

    # Cache lifetimes, in seconds. Timetables are published once a day, so they
    # are refetched at most twice a day (and always after the service-day
    # rollover); the GPS feed is shared by every stop within one poll sweep.
    timetable_ttl: int
    vehicles_ttl: int

    # MQTT
    mqtt_host: str
    mqtt_port: int
    mqtt_user: str
    mqtt_password: str

    # Web server
    host: str
    port: int

    # Lovelace card install, as resolved by run.sh (all empty when developing
    # outside the add-on, where there is no Home Assistant config folder).
    ha_config_dir: str
    card_path: str
    card_src: str
    www_created: bool

    @property
    def mqtt_enabled(self) -> bool:
        return bool(self.mqtt_host)

    @property
    def card_installed(self) -> bool:
        return bool(self.card_path) and os.path.isfile(self.card_path)


def load_settings() -> Settings:
    poll_interval = max(10, _get_int("WT_POLL_INTERVAL", 30))
    return Settings(
        api_key=os.environ.get("WT_API_KEY", "").strip(),
        poll_interval=poll_interval,
        gps_overlay=_get_bool("WT_GPS_OVERLAY", True),
        log_level=os.environ.get("WT_LOG_LEVEL", "info").strip().lower(),
        data_dir=os.environ.get("WT_DATA_DIR", "/data"),
        timetable_ttl=max(300, _get_int("WT_TIMETABLE_TTL", 12 * 3600)),
        # Capped at the poll interval so the shared snapshot is never staler
        # than the sweep that uses it; it exists to let the web panel reuse the
        # poller's download rather than to throttle the poller.
        vehicles_ttl=max(5, _get_int("WT_VEHICLES_TTL", min(20, poll_interval))),
        mqtt_host=os.environ.get("WT_MQTT_HOST", "").strip(),
        mqtt_port=_get_int("WT_MQTT_PORT", 1883),
        mqtt_user=os.environ.get("WT_MQTT_USER", "").strip(),
        mqtt_password=os.environ.get("WT_MQTT_PASSWORD", ""),
        host=os.environ.get("WT_BIND_HOST", "0.0.0.0"),
        port=_get_int("WT_INGRESS_PORT", 8099),
        ha_config_dir=os.environ.get("WT_HA_CONFIG_DIR", "").strip(),
        card_path=os.environ.get("WT_CARD_PATH", "").strip(),
        card_src=os.environ.get(
            "WT_CARD_SRC",
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "lovelace",
                         "warsaw-transport-card.js"),
        ).strip(),
        www_created=_get_bool("WT_WWW_CREATED", False),
    )
