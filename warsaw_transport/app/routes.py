"""Route plans: the ordered stop list behind every trip, and geometry built on it.

A timetable row names its route variant in `trasa` ("TO-FSOpOko"); this module
turns that code into the sequence of poles the trip serves and how far apart they
are, which is what lets the ETA measure progress *along the route* instead of as
the crow flies. See eta.py for how the measurement is used.

The dataset only exists on the deprecated `api.um.warszawa.pl` host, under a
different key and a different calling convention, so it is reached through a
provider adapter: `LegacyRoutesProvider` is the only place that knows the legacy
wire format. When the data reappears on dane.um.warszawa.pl, a second provider
class is the whole migration — `RouteCatalog` and everything downstream keep
working against the normalised `Route` type.

## `odleglosc` is per-segment, not cumulative

The published spec calls `odleglosc` "odległość od początku trasy" (distance from
the start of the route) and its example is a rising sequence. The live data is
not that: every route's first stop is 0 (2,049 of 2,049 variants), only 36% of
variants are non-decreasing, and reading it cumulatively would make 44% of
segments negative. Checked against stop coordinates, each value is the distance
from the *previous* stop — median 1.04x the straight line between the two poles,
which is what a road segment should look like. This module sums it into the
cumulative `distance_m` the estimator wants, and keeps the raw `segment_m`.

Rows are keyed by stop order as strings, starting at "0" and contiguous, but they
arrive in arbitrary dict order — always sort numerically.
"""
from __future__ import annotations

import logging
import math
import os
import time
from typing import Any, Iterable, NamedTuple, Protocol, Sequence

import httpx

from .cache import DailyCache
from .eta import to_float

log = logging.getLogger("warsaw_transport.routes")

LEGACY_BASE_URL = "https://api.um.warszawa.pl/api/action"
LEGACY_ENDPOINT = "public_transport_routes"
ROUTES_CACHE_FILE = "routes_cache.json"
# The whole catalogue is ~3.7 MB of JSON over 2,000 route variants; it is
# published once a day, so it is fetched at most once per service day.
ROUTES_TTL = 12 * 3600
ROUTES_TIMEOUT = 180.0

# A vehicle further than this from every segment of the route is not running it —
# most often it is still finishing its previous trip on a different variant.
MAX_ROUTE_RESIDUAL_M = 400.0
# Loops and out-and-back variants revisit the same streets, so the projection
# searches around the vehicle's last known position before considering the whole
# route; without that a vehicle can snap onto the return leg.
SEARCH_WINDOW = 6
# Residuals within this of each other count as a tie: GPS noise is far larger,
# so anything finer is not a real distinction between two candidate segments.
TIE_EPSILON_M = 5.0

EARTH_RADIUS_M = 6_371_000.0


class RoutesError(RuntimeError):
    """Raised when the route catalogue cannot be fetched or parsed."""


class RouteStop(NamedTuple):
    """One pole on one route variant.

    A NamedTuple rather than a dataclass on purpose: the catalogue holds ~40,000
    of these, and the tuple layout keeps that affordable on add-on hardware.
    """

    order: int
    zespol: str
    slupek: str
    segment_m: float  # raw `odleglosc`: metres from the previous stop
    distance_m: float  # cumulative metres from the start of the route
    typ: str


class Route(NamedTuple):
    line: str
    code: str
    stops: tuple[RouteStop, ...]

    @property
    def length_m(self) -> float:
        return self.stops[-1].distance_m if self.stops else 0.0

    def index_of(self, zespol: str, slupek: str) -> int | None:
        """Position of a pole on this route, or None if it is not served.

        Pole numbers are zero-padded in both datasets, but a stop saved by hand
        may not be, so the comparison is padding-insensitive.
        """
        group = str(zespol).strip()
        pole = str(slupek).strip().lstrip("0")
        for i, stop in enumerate(self.stops):
            if stop.zespol == group and stop.slupek.lstrip("0") == pole:
                return i
        return None


def _sorted_orders(stops: dict[str, Any]) -> list[str]:
    """Stop keys in route order; they are numeric strings in arbitrary dict order."""
    numeric = [k for k in stops if str(k).strip().lstrip("-").isdigit()]
    return sorted(numeric, key=lambda k: int(k))


def normalize_routes(payload: Any) -> dict[tuple[str, str], Route]:
    """Turn a legacy `public_transport_routes` payload into `Route` objects.

    Accepts the `{"result": {...}}` envelope the live host returns as well as the
    bare mapping the published example shows. Pure function, so the parsing is
    testable without touching the network.
    """
    if isinstance(payload, dict) and "result" in payload:
        payload = payload["result"]
    if not isinstance(payload, dict):
        raise RoutesError(f"Unexpected routes payload: {type(payload).__name__}")

    catalogue: dict[tuple[str, str], Route] = {}
    for line, variants in payload.items():
        if not isinstance(variants, dict):
            continue
        for code, stops in variants.items():
            if not isinstance(stops, dict):
                continue
            built: list[RouteStop] = []
            running = 0.0
            for key in _sorted_orders(stops):
                row = stops[key]
                if not isinstance(row, dict):
                    continue
                segment = to_float(row.get("odleglosc")) or 0.0
                running += segment
                built.append(
                    RouteStop(
                        order=int(key),
                        zespol=str(row.get("nr_zespolu", "")).strip(),
                        slupek=str(row.get("nr_przystanku", "")).strip(),
                        segment_m=segment,
                        distance_m=running,
                        typ=str(row.get("typ", "")).strip(),
                    )
                )
            if built:
                key_pair = (str(line).strip(), str(code).strip())
                catalogue[key_pair] = Route(key_pair[0], key_pair[1], tuple(built))
    return catalogue


# --- persistence -----------------------------------------------------------
# Stored as plain lists rather than dicts: at 40,000 stops the key repetition of
# an object-per-stop encoding costs several megabytes on the add-on volume.


def dump_routes(catalogue: dict[tuple[str, str], Route]) -> dict[str, Any]:
    return {
        f"{line}|{code}": [
            [s.order, s.zespol, s.slupek, s.segment_m, s.typ] for s in route.stops
        ]
        for (line, code), route in catalogue.items()
    }


def load_routes(raw: Any) -> dict[tuple[str, str], Route]:
    catalogue: dict[tuple[str, str], Route] = {}
    if not isinstance(raw, dict):
        return catalogue
    for key, rows in raw.items():
        line, _, code = str(key).partition("|")
        stops: list[RouteStop] = []
        running = 0.0
        for row in rows or []:
            try:
                order, zespol, slupek, segment, typ = row
            except (TypeError, ValueError):
                continue
            segment = to_float(segment) or 0.0
            running += segment
            stops.append(RouteStop(int(order), str(zespol), str(slupek), segment, running, str(typ)))
        if stops:
            catalogue[(line, code)] = Route(line, code, tuple(stops))
    return catalogue


# --- providers -------------------------------------------------------------


class RoutesProvider(Protocol):
    """Source of route plans. Swap the implementation, keep everything above."""

    name: str

    async def fetch(self) -> dict[tuple[str, str], Route]:
        ...

    async def aclose(self) -> None:
        ...


class NullRoutesProvider:
    """Used when no legacy key is configured: no routes, no requests.

    The ETA then falls back to the straight-line estimate, which is exactly how
    the add-on behaved before route support existed.
    """

    name = "disabled"

    async def fetch(self) -> dict[tuple[str, str], Route]:
        return {}

    async def aclose(self) -> None:
        return None


class LegacyRoutesProvider:
    """`public_transport_routes` on api.um.warszawa.pl.

    The legacy host differs from dane.um.warszawa.pl in every respect that
    matters: GET rather than POST, the key as an `apikey` query parameter rather
    than an `Authorization` header, and a `{"result": ...}` envelope rather than
    a bare payload. Containing that difference here is the point of the adapter.
    """

    name = "legacy"

    def __init__(self, api_key: str, base_url: str = LEGACY_BASE_URL) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=ROUTES_TIMEOUT)

    async def fetch(self) -> dict[tuple[str, str], Route]:
        if not self._api_key:
            raise RoutesError("No legacy API key configured.")
        url = f"{self._base_url}/{LEGACY_ENDPOINT}/"
        started = time.monotonic()
        try:
            resp = await self._client.get(url, params={"apikey": self._api_key})
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RoutesError(f"Route plan request failed: {exc}") from exc

        # The legacy host answers 200 with a bare string for a bad key or a rate
        # limit, so the payload shape is the real success signal.
        if isinstance(payload, str):
            raise RoutesError(f"Route plan request rejected: {payload[:200]!r}")

        catalogue = normalize_routes(payload)
        log.info(
            "api routes[legacy] -> %s in %dms, %.1f MB, %d route(s) over %d line(s)",
            resp.status_code,
            (time.monotonic() - started) * 1000,
            len(resp.content) / 1e6,
            len(catalogue),
            len({line for line, _ in catalogue}),
        )
        return catalogue

    async def aclose(self) -> None:
        await self._client.aclose()


def build_provider(api_key: str, base_url: str = LEGACY_BASE_URL) -> RoutesProvider:
    """Pick a provider from configuration. The one place that decides."""
    if api_key:
        return LegacyRoutesProvider(api_key, base_url)
    return NullRoutesProvider()


# --- catalogue -------------------------------------------------------------


class RouteCatalog:
    """The loaded routes, refreshed once per service day and kept on /data.

    Route plans change on the same daily cadence as timetables, so this reuses
    `DailyCache` — including its 04:00 service-day rollover and its single-flight
    fetching — rather than inventing a second expiry scheme.
    """

    def __init__(
        self,
        provider: RoutesProvider,
        cache_dir: str | None = None,
        ttl: float = ROUTES_TTL,
    ) -> None:
        self._provider = provider
        self._cache = DailyCache(
            os.path.join(cache_dir, ROUTES_CACHE_FILE) if cache_dir else None,
            ttl,
            name="routes",
        )
        self._routes: dict[tuple[str, str], Route] = {}
        self._raw: Any = None
        self._failed_at = 0.0
        # Prepared geometry per (stop, line, route). Building one resolves and
        # projects every pole on the route, so it is worth keeping between polls;
        # a None entry remembers "this route does not serve this stop" too.
        self._tracks: dict[tuple[str, str, str, str], RouteTrack | None] = {}

    @property
    def enabled(self) -> bool:
        return not isinstance(self._provider, NullRoutesProvider)

    @property
    def loaded(self) -> int:
        return len(self._routes)

    def load(self) -> None:
        """Read the on-disk copy, so startup reports what it will reuse."""
        self._cache.load()

    async def flush(self) -> None:
        await self._cache.flush()

    async def aclose(self) -> None:
        await self._provider.aclose()

    async def ensure(self) -> dict[tuple[str, str], Route]:
        """Return the catalogue, fetching it at most once per service day.

        A failure is logged and remembered briefly rather than raised: routes are
        an enhancement, and the caller's fallback is the straight-line estimate.
        """
        if not self.enabled:
            return self._routes

        async def factory() -> dict[str, Any]:
            return dump_routes(await self._provider.fetch())

        try:
            raw = await self._cache.get("routes", factory)
        except RoutesError as exc:
            if time.time() - self._failed_at > 300:
                log.warning("Route plans unavailable (%s); ETAs fall back to straight-line.", exc)
                self._failed_at = time.time()
            return self._routes

        # Rebuilding on every call would be wasteful; the cache hands back the
        # same object each time, so identity is enough to detect a refresh.
        if raw is not self._raw:
            self._raw = raw
            self._routes = load_routes(raw)
            self._tracks.clear()
            log.info(
                "Route plans ready: %d route(s) over %d line(s).",
                len(self._routes),
                len({line for line, _ in self._routes}),
            )
        return self._routes

    def get(self, line: str, code: str) -> Route | None:
        return self._routes.get((str(line).strip(), str(code).strip()))

    def track_for(
        self,
        busstop_id: str,
        pole: str,
        line: str,
        code: str,
        locate: dict[tuple[str, str], tuple[float, float]],
    ) -> RouteTrack | None:
        """Geometry for one trip's route measured against one stop, memoised."""
        if not code:
            return None
        key = (str(busstop_id), str(pole), str(line).strip(), str(code).strip())
        if key in self._tracks:
            return self._tracks[key]

        route = self.get(line, code)
        track = build_track(route, busstop_id, pole, locate) if route is not None else None
        self._tracks[key] = track
        return track

    def report(self) -> dict[str, Any]:
        """Health/panel summary — how much of the catalogue is actually usable."""
        lines = {line for line, _ in self._routes}
        stops = sum(len(r.stops) for r in self._routes.values())
        return {
            "enabled": self.enabled,
            "provider": self._provider.name,
            "routes": len(self._routes),
            "lines": len(lines),
            "stops": stops,
        }


# --- geometry --------------------------------------------------------------


class Projection(NamedTuple):
    """Where a vehicle sits on a route: metres travelled, error, stop index."""

    distance_m: float
    residual_m: float
    index: int


class RouteTrack:
    """One route, prepared for measuring vehicles against one target stop.

    Built once per (route, stop) and reused across polls: resolving the poles'
    coordinates and projecting to a local plane is the expensive part, and none
    of it changes between vehicles.

    Positions are projected onto the polyline through the route's stops. That
    polyline is not the real road, but the *distances* along it come from the
    dataset's own segment lengths, so interpolating within a segment gives a
    faithful "metres travelled" without any route shape being published.
    """

    def __init__(self, route: Route, target_index: int, points: Sequence[tuple[float, float] | None]):
        self.route = route
        self.target_index = target_index
        self.target_m = route.stops[target_index].distance_m
        self._points = list(points)
        # Equirectangular projection about the route's own latitude: over a few
        # kilometres the error is far below GPS noise, and it keeps the maths to
        # plain arithmetic.
        lats = [p[0] for p in self._points if p is not None]
        self._lat0 = sum(lats) / len(lats) if lats else 0.0
        self._cos_lat0 = math.cos(math.radians(self._lat0))
        self._plane = [self._to_plane(p) for p in self._points]

    def _to_plane(self, point: tuple[float, float] | None) -> tuple[float, float] | None:
        if point is None:
            return None
        lat, lon = point
        return (
            math.radians(lon) * EARTH_RADIUS_M * self._cos_lat0,
            math.radians(lat) * EARTH_RADIUS_M,
        )

    def stops_between(self, index: int) -> int:
        """How many stops the vehicle still has to serve before ours."""
        return max(0, self.target_index - index)

    def project(self, lat: float, lon: float, hint: int | None = None) -> Projection | None:
        """Place a GPS fix on the route.

        `hint` is the vehicle's previous index; segments near it are considered
        first and win ties, which is what stops a loop route from snapping the
        vehicle onto the leg it already covered.
        """
        here = self._to_plane((lat, lon))
        if here is None:
            return None

        best: Projection | None = None
        for i in self._windowed_segments(hint):
            a, b = self._plane[i], self._plane[i + 1]
            if a is None or b is None:
                continue
            t, foot = _project_on_segment(here, a, b)
            residual = math.hypot(here[0] - foot[0], here[1] - foot[1])
            start = self.route.stops[i].distance_m
            end = self.route.stops[i + 1].distance_m
            candidate = Projection(start + t * (end - start), residual, i)
            if _is_better(candidate, best, hint):
                best = candidate

        if best is None or best.residual_m > MAX_ROUTE_RESIDUAL_M:
            return None
        return best

    def _windowed_segments(self, hint: int | None) -> Iterable[int]:
        """Segment indices to test: around the hint first, then the rest."""
        last = len(self._plane) - 2
        if last < 0:
            return ()
        if hint is None:
            return range(last + 1)
        low = max(0, hint - 1)
        high = min(last, hint + SEARCH_WINDOW)
        near = list(range(low, high + 1))
        rest = [i for i in range(last + 1) if i < low or i > high]
        return near + rest


def _is_better(candidate: Projection, best: Projection | None, hint: int | None) -> bool:
    """Whether `candidate` beats `best`, resolving ties toward the hint.

    A stop where two legs meet — the turnaround of an out-and-back route, or any
    point the line passes twice — sits on several segments with an identical
    residual. Picking by iteration order there would make the reported position
    jitter between legs, so near-equal residuals are settled by staying closest
    to where the vehicle already was, and then by taking the later segment.
    """
    if best is None:
        return True
    if candidate.residual_m < best.residual_m - TIE_EPSILON_M:
        return True
    if candidate.residual_m > best.residual_m + TIE_EPSILON_M:
        return False
    if hint is not None:
        near, best_near = abs(candidate.index - hint), abs(best.index - hint)
        # Adjacent segments tie because the vehicle is standing exactly at the
        # stop they share; only a genuinely distant candidate (the other leg of
        # a loop) is ruled out by the hint.
        if near > 1 or best_near > 1:
            return near < best_near
    # At a shared stop, take the later segment: the vehicle has *reached* that
    # stop rather than still being on its way to it.
    return candidate.index > best.index


def _project_on_segment(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> tuple[float, tuple[float, float]]:
    """Foot of the perpendicular from `p` onto segment a-b, clamped to it."""
    abx, aby = b[0] - a[0], b[1] - a[1]
    length_sq = abx * abx + aby * aby
    if length_sq == 0:
        return 0.0, a
    t = ((p[0] - a[0]) * abx + (p[1] - a[1]) * aby) / length_sq
    t = max(0.0, min(1.0, t))
    return t, (a[0] + t * abx, a[1] + t * aby)


def build_track(
    route: Route,
    busstop_id: str,
    pole: str,
    locate: dict[tuple[str, str], tuple[float, float]],
) -> RouteTrack | None:
    """Prepare a route for measuring against one stop.

    `locate` maps (zespol, slupek) to coordinates — in practice the city stop
    list the client already caches, so this costs no requests. Returns None when
    the stop is not on the route or too little of the route can be located.
    """
    target = route.index_of(busstop_id, pole)
    if target is None:
        return None

    points: list[tuple[float, float] | None] = []
    for stop in route.stops:
        point = locate.get((stop.zespol, stop.slupek))
        if point is None:
            point = locate.get((stop.zespol, stop.slupek.zfill(2)))
        points.append(point)

    known = sum(1 for p in points if p is not None)
    if known < 2 or known < len(points) // 2:
        log.debug(
            "Route %s/%s: only %d of %d stops have coordinates; skipping.",
            route.line, route.code, known, len(points),
        )
        return None
    return RouteTrack(route, target, points)
