"""Estimate when a live vehicle will reach a stop, from GPS positions alone.

The API publishes vehicle positions and nothing else — no route geometry, no
distance-along-route, no predicted arrivals (see
.claude/docs/endpoint-lokalizacja-pojazdow.md). So the estimate is built from how
fast the straight-line gap between the vehicle and the stop is *shrinking* across
successive polls:

    ETA = remaining straight-line distance / closing speed

Using the closing rate rather than the vehicle's ground speed is what makes this
usable without a route: detours, one-way systems, turns and traffic all show up
in how quickly the gap actually closes, so no fudge factor for "roads are longer
than straight lines" is needed. The trade-off is that it takes two fixes to say
anything; the first sighting falls back to a default urban speed with a detour
factor and is flagged `approx` so the UI can mark it as rough.

The estimate is withheld rather than guessed whenever the positions do not
support one — the join key (line, brigade) identifies a *trip*, and a brigade
spends much of its day somewhere other than approaching this stop:

- distance growing → the vehicle is on another leg of its run (`moving_away`);
- distance not changing, and never seen to change → parked mid-run (`stalled`);
- estimate far ahead of the timetable → laying over at a terminus, where the
  schedule predicts the departure better than the position does (`waiting`).

`VehicleTracker` holds the per-trip position history in memory. It is keyed by
stop *and* trip because the distance being tracked is to one specific stop.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Deque

log = logging.getLogger("warsaw_transport.eta")

EARTH_RADIUS_M = 6_371_000.0

# Some vehicles report fixes days or months old, so "matched to a vehicle" is
# only meaningful with a freshness check. An ETA is a stronger claim than "this
# departure is live", so it demands a fresher fix than the live flag does.
LIVE_FIX_MAX_AGE = 300.0
ETA_FIX_MAX_AGE = 120.0

# Beyond this the match is almost certainly a different leg of the brigade's day
# rather than the run about to serve this stop.
MAX_DISTANCE_M = 20_000.0
# Inside this the vehicle is at the stop; report "now" instead of dividing a few
# metres by a noisy speed.
ARRIVED_M = 120.0

# Fallback speed for a first sighting, with the usual "roads are longer than the
# crow flies" correction. ~18 km/h is a typical Warsaw bus/tram average.
DEFAULT_SPEED_KMH = 18.0
DETOUR_FACTOR = 1.3
# Floor on the closing speed: a vehicle waiting at a light would otherwise give
# an unbounded ETA.
MIN_SPEED_KMH = 5.0
MAX_ETA_S = 3600.0
# An estimate this far ahead of the timetable is not an early bus, it is a
# vehicle laying over at a terminus before its run (observed: a tram parked at
# the stop 44 minutes before its departure). The schedule predicts those better.
MAX_EARLY_S = 600.0

MAX_SAMPLES = 6
SAMPLE_WINDOW_S = 300.0
# Two fixes closer together than this say more about GPS noise than about speed.
MIN_SPAN_S = 40.0
# Closing rates smaller than this are standing still (dwell, red light, jitter);
# the same rate in the negative direction means genuinely moving away.
STILL_MPS = 0.3
SMOOTHING_ALPHA = 0.5
# Forget a trip's history once it has not been seen for this long.
KEY_TTL_S = 1800.0

TrackKey = tuple[str, str, str]

# Statuses reported on a departure, so the UI (and the log) can tell "no ETA yet"
# apart from "we know the vehicle is heading the other way".
STATUS_OK = "ok"
STATUS_MOVING_AWAY = "moving_away"
STATUS_STALLED = "stalled"
STATUS_WAITING = "waiting"
STATUS_STALE_FIX = "stale_fix"
STATUS_NO_FIX_TIME = "no_fix_time"
STATUS_NO_POSITION = "no_position"
STATUS_TOO_FAR = "too_far"

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def to_float(value: Any) -> float | None:
    """Coerce an API value to float; coordinates arrive as numbers *or* strings."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_fix_time(value: Any) -> datetime | None:
    """Parse a vehicle's `Time` ('YYYY-MM-DD HH:MM:SS', local Warsaw time)."""
    if not value:
        return None
    text = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def fix_age(vehicle: dict[str, Any], now: datetime) -> float | None:
    """Seconds since the vehicle's GPS fix, or None when `Time` is unusable."""
    fix = parse_fix_time(vehicle.get("Time"))
    if fix is None:
        return None
    return (now - fix).total_seconds()


def is_live(vehicle: dict[str, Any], now: datetime) -> bool:
    """Whether a matched vehicle is reporting *now* rather than months ago.

    An unparseable `Time` is treated as live: the match itself is evidence, and
    refusing it would silently drop departures if the field ever changes shape.
    """
    age = fix_age(vehicle, now)
    if age is None:
        return True
    return -LIVE_FIX_MAX_AGE <= age <= LIVE_FIX_MAX_AGE


def blank_eta(status: str = STATUS_OK, distance_m: int | None = None) -> dict[str, Any]:
    """An ETA-shaped dict with nothing estimated, so rows keep a stable shape."""
    return {
        "eta_minutes": None,
        "eta_time": None,
        "eta_timestamp": None,
        "delay_minutes": None,
        "eta_source": None,
        "eta_status": status,
        "distance_m": distance_m,
    }


class VehicleTracker:
    """Per-(stop, trip) distance history, and the ETA derived from it.

    Lives for the process. `estimate` is called once per departure per poll and
    is cheap: a haversine, a deque append and a subtraction.
    """

    def __init__(self) -> None:
        self._samples: dict[TrackKey, Deque[tuple[float, float]]] = {}
        self._speed: dict[TrackKey, float] = {}
        self._seen: dict[TrackKey, float] = {}

    def estimate(
        self,
        stop_key: str,
        stop_lat: float,
        stop_lon: float,
        vehicle: dict[str, Any],
        scheduled: datetime | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """ETA fields for one vehicle heading for one stop.

        Always returns the full field set; `eta_status` says why an estimate is
        missing when `eta_minutes` is None.
        """
        now = now or datetime.now()
        key = (
            stop_key,
            str(vehicle.get("Lines", "")).strip(),
            str(vehicle.get("Brigade", "")).strip(),
        )
        self._seen[key] = now.timestamp()

        lat = to_float(vehicle.get("Lat"))
        lon = to_float(vehicle.get("Lon"))
        if lat is None or lon is None:
            return blank_eta(STATUS_NO_POSITION)

        distance = haversine_m(lat, lon, stop_lat, stop_lon)
        rounded = int(round(distance))

        fix = parse_fix_time(vehicle.get("Time"))
        if fix is None:
            return blank_eta(STATUS_NO_FIX_TIME, rounded)
        age = (now - fix).total_seconds()
        if abs(age) > ETA_FIX_MAX_AGE:
            return blank_eta(STATUS_STALE_FIX, rounded)
        if distance > MAX_DISTANCE_M:
            self._forget(key)
            return blank_eta(STATUS_TOO_FAR, rounded)

        self._add_sample(key, fix.timestamp(), distance)

        if distance <= ARRIVED_M:
            # The vehicle is at the stop. That is an observation rather than an
            # extrapolation, so it counts as tracked however little history it has.
            seconds = 0.0
            source = "tracked"
        else:
            speed, source = self._speed_for(key)
            if speed is None:
                return blank_eta(source, rounded)
            # A tracked closing rate already accounts for the real path, so it is
            # paired with the raw gap; the default-speed fallback is not, so it
            # gets the detour correction.
            remaining = distance if source == "tracked" else distance * DETOUR_FACTOR
            seconds = min(remaining / speed, MAX_ETA_S)

        arrival = now + timedelta(seconds=seconds)

        delay = None
        if scheduled is not None:
            early = (scheduled - arrival).total_seconds()
            if early > MAX_EARLY_S:
                return blank_eta(STATUS_WAITING, rounded)
            delay = int(round((arrival - scheduled).total_seconds() / 60))

        return {
            "eta_minutes": int(seconds // 60),
            "eta_time": arrival.strftime("%H:%M"),
            "eta_timestamp": arrival.isoformat(timespec="seconds"),
            "delay_minutes": delay,
            "eta_source": source,
            "eta_status": STATUS_OK,
            "distance_m": rounded,
        }

    def prune(self, now: datetime | None = None) -> int:
        """Drop trips not seen for KEY_TTL_S. Returns how many were dropped."""
        cutoff = (now or datetime.now()).timestamp() - KEY_TTL_S
        stale = [key for key, seen in self._seen.items() if seen < cutoff]
        for key in stale:
            self._forget(key)
            self._seen.pop(key, None)
        if stale:
            log.debug("Forgot %d idle vehicle track(s).", len(stale))
        return len(stale)

    # --- internals ---------------------------------------------------------

    def _forget(self, key: TrackKey) -> None:
        self._samples.pop(key, None)
        self._speed.pop(key, None)

    def _add_sample(self, key: TrackKey, fix_epoch: float, distance: float) -> None:
        """Record a fix, ignoring one already seen.

        The GPS snapshot is cached for `vehicles_ttl` and the panel and poller
        share it, so the same fix is offered repeatedly; re-adding it would make
        the window collapse to zero elapsed time.
        """
        samples = self._samples.setdefault(key, deque(maxlen=MAX_SAMPLES))
        if samples and fix_epoch <= samples[-1][0]:
            return
        samples.append((fix_epoch, distance))
        while len(samples) > 1 and fix_epoch - samples[0][0] > SAMPLE_WINDOW_S:
            samples.popleft()

    def _speed_for(self, key: TrackKey) -> tuple[float | None, str]:
        """Closing speed in m/s and its provenance.

        A None speed carries the status explaining it instead: the vehicle is
        receding, or it has never been seen to move. Neither clears the sample
        history — a verdict that resets itself every poll would flip between
        "moving away" and a fresh default-speed guess on alternate cycles.
        """
        samples = self._samples.get(key)
        previous = self._speed.get(key)

        if samples and len(samples) >= 2:
            (t_old, d_old), (t_new, d_new) = samples[0], samples[-1]
            span = t_new - t_old
            if span >= MIN_SPAN_S:
                closing = (d_old - d_new) / span
                if closing <= -STILL_MPS:
                    self._speed.pop(key, None)
                    return None, STATUS_MOVING_AWAY
                if closing < STILL_MPS:
                    # Standing still. If it was moving earlier this is a light or
                    # a stop dwell, so the last measured rate still applies; if it
                    # has never moved it is parked somewhere in its run and any
                    # number would be invented (observed: trams idle at a terminus
                    # 2.4 km away, which a speed floor turned into a confident
                    # "37 min").
                    if previous is None:
                        return None, STATUS_STALLED
                    return previous, "tracked"
                smoothed = (
                    closing
                    if previous is None
                    else SMOOTHING_ALPHA * closing + (1 - SMOOTHING_ALPHA) * previous
                )
                smoothed = max(smoothed, MIN_SPEED_KMH / 3.6)
                self._speed[key] = smoothed
                return smoothed, "tracked"

        # Not enough span yet: reuse the last measured rate if there is one,
        # otherwise guess from a typical urban speed.
        if previous is not None:
            return previous, "tracked"
        return DEFAULT_SPEED_KMH / 3.6, "approx"
