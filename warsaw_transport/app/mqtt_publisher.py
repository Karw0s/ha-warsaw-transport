"""Publish stop departures to Home Assistant via MQTT Discovery.

Each saved stop becomes one sensor. All sensors are grouped under a single
"Warsaw Transport" device. The sensor state is the minutes-to-next-departure;
full details (next 5 departures) ride along in the JSON attributes topic.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import paho.mqtt.client as mqtt

log = logging.getLogger("warsaw_transport.mqtt")

DISCOVERY_PREFIX = "homeassistant"
NODE = "warsaw_transport"
AVAILABILITY_TOPIC = f"{NODE}/status"

DEVICE = {
    "identifiers": [NODE],
    "name": "Warsaw Transport",
    "manufacturer": "ZTM Warszawa (open data)",
    "model": "Public transport departures",
}


class MqttPublisher:
    def __init__(
        self, host: str, port: int, username: str = "", password: str = ""
    ) -> None:
        self._host = host
        self._port = port
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id="warsaw_transport"
        )
        if username:
            self._client.username_pw_set(username, password)
        self._client.will_set(AVAILABILITY_TOPIC, "offline", retain=True)
        self._connected = False

    def connect(self) -> None:
        self._client.connect(self._host, self._port, keepalive=60)
        self._client.loop_start()
        self._connected = True
        self._client.publish(AVAILABILITY_TOPIC, "online", retain=True)
        log.info("Connected to MQTT broker %s:%s", self._host, self._port)

    def disconnect(self) -> None:
        if self._connected:
            self._client.publish(AVAILABILITY_TOPIC, "offline", retain=True)
            self._client.loop_stop()
            self._client.disconnect()
            self._connected = False

    def _state_topic(self, stop_id: str) -> str:
        return f"{NODE}/{stop_id}/state"

    def _attr_topic(self, stop_id: str) -> str:
        return f"{NODE}/{stop_id}/attributes"

    def _config_topic(self, stop_id: str) -> str:
        return f"{DISCOVERY_PREFIX}/sensor/{NODE}_{stop_id}/config"

    def publish_discovery(self, stop: dict[str, Any]) -> None:
        stop_id = stop["id"]
        name = stop.get("name") or stop_id
        payload = {
            "name": name,
            "unique_id": f"{NODE}_{stop_id}",
            "object_id": f"warsaw_{stop_id}",
            "state_topic": self._state_topic(stop_id),
            "unit_of_measurement": "min",
            "icon": "mdi:bus-clock",
            "json_attributes_topic": self._attr_topic(stop_id),
            "availability_topic": AVAILABILITY_TOPIC,
            "device": DEVICE,
        }
        self._client.publish(
            self._config_topic(stop_id), json.dumps(payload), retain=True
        )
        log.debug("Published discovery config for %s", stop_id)

    def publish_state(self, stop: dict[str, Any], departures: list[dict[str, Any]]) -> None:
        stop_id = stop["id"]
        next_min = departures[0]["minutes"] if departures else None
        self._client.publish(
            self._state_topic(stop_id),
            "" if next_min is None else str(next_min),
            retain=True,
        )
        attributes = {
            "stop_name": stop.get("name"),
            "busstop_id": stop.get("busstop_id"),
            "pole": stop.get("pole"),
            "departures": departures,
        }
        self._client.publish(
            self._attr_topic(stop_id), json.dumps(attributes, ensure_ascii=False), retain=True
        )

    def remove_stop(self, stop_id: str) -> None:
        # Empty retained config payload removes the entity from Home Assistant.
        self._client.publish(self._config_topic(stop_id), "", retain=True)
        self._client.publish(self._state_topic(stop_id), "", retain=True)
        self._client.publish(self._attr_topic(stop_id), "", retain=True)
        log.info("Removed MQTT entity for stop %s", stop_id)
