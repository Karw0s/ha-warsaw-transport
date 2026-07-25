"""A TTL cache scoped to the transport "service day".

Timetables and stop line lists describe the *current day only* (see
.claude/docs/endpoint-odjazdy.md) and the API refreshes them roughly once a day,
so re-fetching them every poll cycle is pure waste. Entries here expire on a
long TTL *and* whenever the service day rolls over, so a cached timetable can
never outlive the day it describes.

The day boundary is 04:00, not midnight: Warsaw expresses after-midnight service
as hours >= 24 ("25:14" = 01:14), so those departures belong to the previous
service day and must not be dropped the moment the clock passes midnight.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

log = logging.getLogger("warsaw_transport.cache")

SERVICE_DAY_START_HOUR = 4


def service_day(now: datetime | None = None) -> str:
    """Return the 'YYYY-MM-DD' service day; times before 04:00 map to yesterday."""
    now = now or datetime.now()
    if now.hour < SERVICE_DAY_START_HOUR:
        now -= timedelta(days=1)
    return now.strftime("%Y-%m-%d")


class DailyCache:
    """Service-day scoped cache with single-flight fetches and JSON persistence.

    `get` never lets two concurrent callers fetch the same key twice: the first
    stores its task, the rest await it. That matters because a cold stop fans
    out one timetable request per line at once.

    Persistence is optional (`path=None` keeps it in memory) and lazy: the file
    is read on first use and only written by `flush`, so a cold sweep writes
    once rather than once per entry.
    """

    def __init__(self, path: str | None, ttl: float, name: str = "daily") -> None:
        self._path = path
        self._ttl = ttl
        self._name = name
        # key -> (service_day, fetched_at, value)
        self._entries: dict[str, tuple[str, float, Any]] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._loaded = False
        self._dirty = False

    # --- persistence -------------------------------------------------------

    def load(self) -> None:
        """Read the cache file once, keeping only entries for today.

        Idempotent, and safe to call eagerly at startup so the log says whether
        the day's data was reused before any request is made. Every outcome is
        logged: a silent load would make "reused the cache" and "started cold"
        indistinguishable after a restart.
        """
        if self._loaded:
            return
        self._loaded = True

        if not self._path:
            log.debug("%s cache: in-memory only, nothing to load.", self._name)
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            raw = payload["entries"]
        except FileNotFoundError:
            log.info("%s cache: nothing on disk, starting cold.", self._name)
            return
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("Ignoring unreadable %s cache %s (%s).", self._name, self._path, exc)
            return

        today = service_day()
        stale_days: set[str] = set()
        for key, entry in raw.items():
            try:
                day, fetched_at, value = entry["day"], float(entry["at"]), entry["value"]
            except (KeyError, TypeError, ValueError):
                continue
            if day == today:
                self._entries[key] = (day, fetched_at, value)
            else:
                stale_days.add(day)

        if self._entries:
            log.info(
                "%s cache: reusing %d entrie(s) for service day %s.",
                self._name,
                len(self._entries),
                today,
            )
        elif stale_days:
            log.info(
                "%s cache: on-disk copy is for %s, current service day is %s — starting cold.",
                self._name,
                ", ".join(sorted(stale_days)),
                today,
            )
        else:
            log.info("%s cache: on-disk copy is empty, starting cold.", self._name)

    async def flush(self) -> None:
        """Persist the cache if anything changed, dropping stale service days."""
        if not self._path or not self._dirty:
            return
        today = service_day()
        self._entries = {k: v for k, v in self._entries.items() if v[0] == today}
        payload = {
            "entries": {
                key: {"day": day, "at": at, "value": value}
                for key, (day, at, value) in self._entries.items()
            }
        }
        try:
            tmp = f"{self._path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, self._path)
            self._dirty = False
        except (OSError, TypeError, ValueError) as exc:
            log.warning("Could not persist %s cache to %s (%s).", self._name, self._path, exc)

    # --- lookup ------------------------------------------------------------

    def _fresh(self, key: str) -> tuple[bool, Any, float]:
        """Return (hit, value, age) for `key` under today's service day and TTL."""
        entry = self._entries.get(key)
        if entry is None:
            return False, None, 0.0
        day, fetched_at, value = entry
        age = time.time() - fetched_at
        if day != service_day() or age >= self._ttl:
            return False, None, age
        return True, value, age

    async def get(self, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Return the cached value for `key`, calling `factory` only on a miss."""
        self.load()  # no-op once warmed; a safety net for callers that skip it

        hit, value, age = self._fresh(key)
        if hit:
            log.debug("cache hit %s (age %ds)", key, int(age))
            return value

        inflight = self._inflight.get(key)
        if inflight is not None:
            log.debug("cache wait %s (fetch already in flight)", key)
            return await asyncio.shield(inflight)

        task = asyncio.ensure_future(factory())
        self._inflight[key] = task
        try:
            value = await task
        finally:
            self._inflight.pop(key, None)

        self._entries[key] = (service_day(), time.time(), value)
        self._dirty = True
        return value
