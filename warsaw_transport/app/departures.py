"""Compute the next N departures for a stop.

Merges the timetables of every line serving a stop pole, parses the `czas`
departure time (which may exceed 24h for after-midnight service), keeps only
future departures, sorts them, and optionally overlays live GPS vehicle data.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from .warsaw_api import WarsawApiClient, WarsawApiError

log = logging.getLogger("warsaw_transport.departures")

# The GPS endpoint takes one vehicle type per call (1 = bus, 2 = tram) and has
# no line filter, so both feeds are fetched wholesale; the client caches them.
VEHICLE_TYPES = (1, 2)


def parse_czas(czas: str, now: datetime) -> datetime | None:
    """Parse a 'HH:MM:SS' departure time into a concrete datetime near `now`.

    Warsaw uses hours >= 24 (e.g. '25:14:00') for trips after midnight; those
    roll into the following day. Plain times (< 24h) are anchored to today's
    date; callers filter out times that have already passed.
    """
    parts = str(czas).strip().split(":")
    if len(parts) < 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        return None

    day_offset, hour = divmod(hour, 24)
    base = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    return base + timedelta(days=day_offset)


def build_departures(
    timetables: dict[str, list[dict[str, Any]]],
    now: datetime,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Merge per-line timetable rows into a sorted list of upcoming departures.

    `timetables` maps line -> list of raw timetable rows (with czas/kierunek/
    brygada). Pure function so it can be unit-tested without network access.
    """
    merged: list[dict[str, Any]] = []
    for line, rows in timetables.items():
        for row in rows:
            dep = parse_czas(row.get("czas", ""), now)
            if dep is None or dep < now - timedelta(minutes=1):
                continue
            minutes = int((dep - now).total_seconds() // 60)
            merged.append(
                {
                    "line": str(line),
                    "direction": row.get("kierunek", ""),
                    "brigade": str(row.get("brygada", "")).strip(),
                    "time": dep.strftime("%H:%M"),
                    "timestamp": dep.isoformat(),
                    "minutes": max(0, minutes),
                    "live": False,
                    "lat": None,
                    "lon": None,
                }
            )

    merged.sort(key=lambda d: d["timestamp"])
    return merged[:limit]


def overlay_gps(
    departures: list[dict[str, Any]],
    vehicles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach live position to departures that match a vehicle by (line, brigade)."""
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for v in vehicles:
        key = (str(v.get("Lines", "")).strip(), str(v.get("Brigade", "")).strip())
        index[key] = v

    for dep in departures:
        v = index.get((dep["line"], dep["brigade"]))
        if v is not None:
            dep["live"] = True
            dep["lat"] = v.get("Lat")
            dep["lon"] = v.get("Lon")
            dep["vehicle"] = v.get("VehicleNumber")
    return departures


async def next_departures(
    client: WarsawApiClient,
    busstop_id: str,
    pole: str,
    *,
    limit: int = 5,
    gps_overlay: bool = True,
    vehicle_types: tuple[int, ...] = VEHICLE_TYPES,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Full pipeline: fetch lines, fetch all timetables, merge, overlay GPS."""
    now = now or datetime.now()

    lines = await client.lines_for_stop(busstop_id, pole)
    if not lines:
        return []

    results = await asyncio.gather(
        *(client.timetable(busstop_id, pole, line) for line in lines),
        return_exceptions=True,
    )
    timetables: dict[str, list[dict[str, Any]]] = {}
    for line, res in zip(lines, results):
        if isinstance(res, Exception):
            log.warning("Timetable fetch failed for line %s: %s", line, res)
            continue
        timetables[line] = res

    departures = build_departures(timetables, now, limit=limit)

    if gps_overlay and departures:
        try:
            # One (cached) snapshot per vehicle type, then flatten. A pole can
            # be served by both buses and trams, so both feeds are needed.
            vehicle_lists = await asyncio.gather(
                *(client.vehicle_positions(t) for t in vehicle_types),
                return_exceptions=True,
            )
            vehicles: list[dict[str, Any]] = []
            for vehicle_type, vl in zip(vehicle_types, vehicle_lists):
                if isinstance(vl, Exception):
                    log.warning(
                        "GPS feed for type %s unavailable: %s", vehicle_type, vl
                    )
                    continue
                vehicles.extend(vl)
            overlay_gps(departures, vehicles)
        except WarsawApiError as exc:
            log.warning("GPS overlay skipped: %s", exc)

    return departures
