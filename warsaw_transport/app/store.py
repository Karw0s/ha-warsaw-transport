"""Persistence for the user-selected stops.

Stops are stored in <data_dir>/stops.json which lives on the add-on's /data
volume and survives restarts/updates.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any

log = logging.getLogger("warsaw_transport.store")


def _slug(*parts: str) -> str:
    raw = "_".join(str(p) for p in parts)
    return re.sub(r"[^a-z0-9_]+", "_", raw.lower()).strip("_")


class StopStore:
    def __init__(self, data_dir: str) -> None:
        self._path = os.path.join(data_dir, "stops.json")
        self._lock = threading.Lock()
        self._stops: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._stops = {s["id"]: s for s in data.get("stops", [])}
            log.info("Loaded %d saved stop(s).", len(self._stops))
        except FileNotFoundError:
            self._stops = {}
        except (json.JSONDecodeError, KeyError) as exc:
            log.error("Could not read %s (%s); starting empty.", self._path, exc)
            self._stops = {}

    def _save(self) -> None:
        tmp = f"{self._path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"stops": list(self._stops.values())}, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    def list_stops(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._stops.values())

    def get(self, stop_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._stops.get(stop_id)

    def add(self, busstop_id: str, pole: str, name: str) -> dict[str, Any]:
        stop_id = _slug(busstop_id, pole)
        entry = {
            "id": stop_id,
            "busstop_id": str(busstop_id),
            "pole": str(pole),
            "name": name,
        }
        with self._lock:
            self._stops[stop_id] = entry
            self._save()
        return entry

    def remove(self, stop_id: str) -> bool:
        with self._lock:
            existed = self._stops.pop(stop_id, None) is not None
            if existed:
                self._save()
        return existed
