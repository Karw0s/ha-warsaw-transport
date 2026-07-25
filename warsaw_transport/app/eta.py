"""Estimate when a live vehicle will reach a stop.

Two measurements are possible, and the estimator prefers the first:

**Along the route** (needs a route plan — see routes.py). The vehicle's position
is projected onto the sequence of stops its trip serves, giving real metres still
to travel and the times it passed the previous stops. Speed measured stop to stop
already includes the time spent standing at them, so nothing has to be added back:

    ETA = metres remaining on the route / speed measured over the last stops

**As the crow flies** (always available). Without a route the only signal is how
fast the straight-line gap to the stop is *shrinking* across successive polls:

    ETA = remaining straight-line distance / closing speed

Using the closing rate rather than the vehicle's ground speed is what makes that
usable without a route: detours, one-way systems, turns and traffic all show up
in how quickly the gap actually closes, so no fudge factor for "roads are longer
than straight lines" is needed. The trade-off is that it takes two fixes to say
anything; the first sighting falls back to a default urban speed with a detour
factor and is flagged `approx` so the UI can mark it as rough.

Both modes feed the same history: what a sample records is *distance remaining to
the stop*, so the speed, smoothing and withholding rules below are shared. A
vehicle that drops off its route (usually because it is still finishing its
previous trip) falls back to the straight-line mode rather than losing its ETA.

The estimate is withheld rather than guessed whenever the positions do not
support one — the join key (line, brigade) identifies a *trip*, and a brigade
spends much of its day somewhere other than approaching this stop:

- distance growing → the vehicle is on another leg of its run (`moving_away`);
- distance not changing, and never seen to change → parked mid-run (`stalled`);
- estimate far ahead of the timetable → laying over at a terminus, where the
  schedule predicts the departure better than the position does (`waiting`);
- already past our stop on the route → this trip has served us (`passed`).

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

# Metres past our stop before the trip counts as having served it. A projection
# lands within a few tens of metres, so this only has to clear the noise.
PASSED_TOLERANCE_M = 80.0
# Passage marks left as the vehicle serves the stops before ours; two are enough
# for a speed, more smooth out a single long dwell.
MAX_PASSAGES = 5
# Sanity bound on a passage-derived speed (90 km/h): a mis-projection that jumps
# the vehicle several stops forward would otherwise read as a very fast trip.
MAX_PASSAGE_SPEED_MPS = 25.0

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
STATUS_PASSED = "passed"
STATUS_STALE_FIX = "stale_fix"
STATUS_NO_FIX_TIME = "no_fix_time"
STATUS_NO_POSITION = "no_position"
STATUS_TOO_FAR = "too_far"

# How the distance was measured, best first. `approx` means the *speed* was a
# guess (a first sighting), whichever way the distance was measured.
SOURCE_ROUTE = "route"
SOURCE_TRACKED = "tracked"
SOURCE_APPROX = "approx"

MODE_ROUTE = "route"
MODE_LINE = "line"

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


def blank_eta(
    status: str = STATUS_OK,
    distance_m: int | None = None,
    route_distance_m: int | None = None,
    stops_away: int | None = None,
) -> dict[str, Any]:
    """An ETA-shaped dict with nothing estimated, so rows keep a stable shape."""
    return {
        "eta_minutes": None,
        "eta_time": None,
        "eta_timestamp": None,
        "delay_minutes": None,
        "eta_source": None,
        "eta_status": status,
        "distance_m": distance_m,
        # Route-mode extras; None whenever no route plan applied.
        "route_distance_m": route_distance_m,
        "stops_away": stops_away,
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
        # Route mode only: where the vehicle was last projected, and when it
        # passed the stops before ours.
        self._mode: dict[TrackKey, str] = {}
        self._index: dict[TrackKey, int] = {}
        self._passages: dict[TrackKey, Deque[tuple[int, float, float]]] = {}

    def estimate(
        self,
        stop_key: str,
        stop_lat: float,
        stop_lon: float,
        vehicle: dict[str, Any],
        scheduled: datetime | None = None,
        now: datetime | None = None,
        route_track: Any | None = None,
    ) -> dict[str, Any]:
        """ETA fields for one vehicle heading for one stop.

        `route_track` is a `routes.RouteTrack` for this trip's route variant and
        this stop; with it the distance is measured along the route, without it
        as the crow flies. Always returns the full field set; `eta_status` says
        why an estimate is missing when `eta_minutes` is None.
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

        fix_epoch = fix.timestamp()
        projection = None
        if route_track is not None:
            projection = route_track.project(lat, lon, self._index.get(key))

        if projection is None:
            # No route, or the vehicle is nowhere near it — commonly because it
            # is still finishing its previous trip. Fall back to the crow-flies
            # measurement rather than dropping the estimate.
            mode = MODE_LINE
            remaining = distance
            route_remaining: float | None = None
            stops_away: int | None = None
        else:
            mode = MODE_ROUTE
            route_remaining = route_track.target_m - projection.distance_m
            stops_away = route_track.stops_between(projection.index)
            if route_remaining < -PASSED_TOLERANCE_M:
                # This trip has already served us; its next visit is a later
                # departure with its own row.
                return blank_eta(STATUS_PASSED, rounded, int(round(route_remaining)), 0)
            remaining = max(0.0, route_remaining)
            self._record_passage(key, fix_epoch, projection.index, route_track)

        # A sample means "distance still to cover", so the two modes measure
        # different things and their history cannot be mixed.
        if self._mode.get(key) != mode:
            self._mode[key] = mode
            self._reset_history(key)
        self._add_sample(key, fix_epoch, remaining)

        route_rounded = None if route_remaining is None else int(round(route_remaining))

        if remaining <= ARRIVED_M:
            # The vehicle is at the stop. That is an observation rather than an
            # extrapolation, so it counts as measured however little history it has.
            seconds = 0.0
            source = SOURCE_ROUTE if mode == MODE_ROUTE else SOURCE_TRACKED
        else:
            speed, source = self._speed_for(key, mode)
            if speed is None:
                return blank_eta(source, rounded, route_rounded, stops_away)
            # A measured rate already accounts for the real path, as does a
            # route distance; only a guessed speed over a crow-flies gap needs
            # the correction for roads being longer than straight lines.
            if mode == MODE_LINE and source == SOURCE_APPROX:
                remaining *= DETOUR_FACTOR
            seconds = min(remaining / speed, MAX_ETA_S)

        arrival = now + timedelta(seconds=seconds)

        delay = None
        if scheduled is not None:
            early = (scheduled - arrival).total_seconds()
            if early > MAX_EARLY_S:
                return blank_eta(STATUS_WAITING, rounded, route_rounded, stops_away)
            delay = int(round((arrival - scheduled).total_seconds() / 60))

        return {
            "eta_minutes": int(seconds // 60),
            "eta_time": arrival.strftime("%H:%M"),
            "eta_timestamp": arrival.isoformat(timespec="seconds"),
            "delay_minutes": delay,
            "eta_source": source,
            "eta_status": STATUS_OK,
            "distance_m": rounded,
            "route_distance_m": route_rounded,
            "stops_away": stops_away,
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
        self._mode.pop(key, None)
        self._index.pop(key, None)
        self._passages.pop(key, None)

    def _reset_history(self, key: TrackKey) -> None:
        """Drop measurements without forgetting the trip's route position."""
        self._samples.pop(key, None)
        self._speed.pop(key, None)
        self._passages.pop(key, None)

    def _record_passage(
        self, key: TrackKey, fix_epoch: float, index: int, route_track: Any
    ) -> None:
        """Note that the vehicle has reached stop `index` on its route.

        Only advances are recorded, and only the stop actually reached: when the
        index jumps by several between polls, the intermediate stops were passed
        at some unknown time in between, and inventing one for each would bias
        the speed. Timing stop to stop means the resulting rate already includes
        the seconds spent standing at them.
        """
        previous = self._index.get(key)
        self._index[key] = index
        if previous is not None and index <= previous:
            return

        passages = self._passages.setdefault(key, deque(maxlen=MAX_PASSAGES))
        if passages and fix_epoch <= passages[-1][1]:
            return
        distance = route_track.route.stops[index].distance_m
        passages.append((index, fix_epoch, distance))

    def _passage_speed(self, key: TrackKey) -> float | None:
        """Speed in m/s from the times the vehicle reached earlier stops."""
        passages = self._passages.get(key)
        if not passages or len(passages) < 2:
            return None
        (_, t_old, d_old), (_, t_new, d_new) = passages[0], passages[-1]
        span = t_new - t_old
        if span < MIN_SPAN_S:
            return None
        speed = (d_new - d_old) / span
        if speed <= 0 or speed > MAX_PASSAGE_SPEED_MPS:
            return None
        return max(speed, MIN_SPEED_KMH / 3.6)

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

    def _speed_for(self, key: TrackKey, mode: str = MODE_LINE) -> tuple[float | None, str]:
        """Closing speed in m/s and its provenance.

        A None speed carries the status explaining it instead: the vehicle is
        receding, or it has never been seen to move. Neither clears the sample
        history — a verdict that resets itself every poll would flip between
        "moving away" and a fresh default-speed guess on alternate cycles.
        """
        # Stop-to-stop timings are the best rate available: they are measured
        # over hundreds of metres of real route and include the dwell at each
        # stop, where the sample window only sees the last few minutes.
        passage_speed = self._passage_speed(key)
        if passage_speed is not None:
            self._speed[key] = passage_speed
            return passage_speed, SOURCE_ROUTE

        measured = SOURCE_ROUTE if mode == MODE_ROUTE else SOURCE_TRACKED
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
                    return previous, measured
                smoothed = (
                    closing
                    if previous is None
                    else SMOOTHING_ALPHA * closing + (1 - SMOOTHING_ALPHA) * previous
                )
                smoothed = max(smoothed, MIN_SPEED_KMH / 3.6)
                self._speed[key] = smoothed
                return smoothed, measured

        # Not enough span yet: reuse the last measured rate if there is one,
        # otherwise guess from a typical urban speed.
        if previous is not None:
            return previous, measured
        return DEFAULT_SPEED_KMH / 3.6, SOURCE_APPROX
