"""Async client for the Warsaw Open Data public-transport API.

All endpoints live under https://dane.um.warszawa.pl/api/action/, are called with
POST, take their parameters as a JSON body, and authenticate with the API key in
an `Authorization` header (no "Bearer" prefix). A successful response is a bare
JSON array; anything else is an error. See .claude/docs/warsaw-open-data-api.md.

Rows arrive in three encodings ({"values": [{key, value}, ...]}, a bare list of
those pairs, or an already-flat object); `_flatten` normalises all three.

Two endpoints cannot be filtered server-side, so this client caches them and
filters in Python: the stop list (whole city, ~3 MB) and the vehicle GPS feed
(all vehicles of one type, ~180 KB). The line list and timetables *can* be
filtered server-side but only change once a day, so they are cached too — see
`DailyCache` in cache.py.

Every request that actually reaches the network is logged at INFO with its
parameters, so the log shows which calls were made and which were served from
a cache (cache hits log at DEBUG).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import unicodedata
from typing import Any

import httpx

from .cache import DailyCache

log = logging.getLogger("warsaw_transport.api")

BASE_URL = "https://dane.um.warszawa.pl/api/action"

EP_STOPS = "get_ztm_przystanki_komunikacji_miejskiej"
EP_LINES = "get_ztm_lista_linii_na_przystanku"
EP_TIMETABLE = "get_ztm_odjazdy_linii_z_przystanku"
EP_VEHICLES = "get_ztm_lokalizacja_pojazdow"

# The stop list changes rarely; the GPS feed refreshes roughly every 10 seconds.
# Timetables are published once a day, so half a day between refreshes still
# picks up same-day corrections while keeping traffic to ~2 calls per line.
STOPS_TTL = 24 * 3600
VEHICLES_TTL = 20
TIMETABLE_TTL = 12 * 3600
STOPS_CACHE_FILE = "stops_cache.json"
TIMETABLE_CACHE_FILE = "timetable_cache.json"
# Downloading every stop pole is ~3 MB, well beyond the default request timeout.
STOPS_TIMEOUT = 60.0

MAX_SEARCH_RESULTS = 50

# Short names used in the request log, so a call is identifiable without
# matching the long endpoint slug by eye.
EP_LABELS = {
    EP_STOPS: "stops",
    EP_LINES: "lines",
    EP_TIMETABLE: "timetable",
    EP_VEHICLES: "vehicles",
}
VEHICLE_TYPE_NAMES = {1: "bus", 2: "tram"}

# "ł" is a standalone codepoint that does not decompose under NFKD, so stripping
# combining marks alone would leave it in place.
_POLISH_L = str.maketrans({"ł": "l", "Ł": "L"})


class WarsawApiError(RuntimeError):
    """Raised when the API returns an error result (bad key, rate limit, ...)."""


def _flatten(row: Any) -> dict[str, Any]:
    """Normalise any of the API's row encodings into a plain dict.

    Handles {"values": [{"key": k, "value": v}, ...]} (stops, lines), a bare
    list of those pairs (timetables), and already-flat objects (vehicles).
    """
    if isinstance(row, dict) and "values" in row:
        row = row["values"]
    if isinstance(row, list):
        return {
            item.get("key"): item.get("value")
            for item in row
            if isinstance(item, dict)
        }
    return row if isinstance(row, dict) else {}


def describe_call(endpoint: str, payload: dict[str, Any] | None = None) -> str:
    """Render an endpoint + its parameters as one readable log token.

    The GPS feed's `type` is spelled out ("vehicles[bus]") because the bus and
    tram calls are otherwise indistinguishable in the log. Pure function so it
    can be unit-tested.
    """
    label = EP_LABELS.get(endpoint, endpoint)
    if endpoint == EP_VEHICLES and payload:
        vehicle_type = payload.get("type")
        name = VEHICLE_TYPE_NAMES.get(vehicle_type)
        label = f"{label}[{name}]" if name else label
    if not payload:
        return label
    params = " ".join(f"{key}={value}" for key, value in payload.items())
    return f"{label} {params}"


def normalize(text: Any) -> str:
    """Casefold and strip Polish diacritics so "zeran" matches "Żerań"."""
    decomposed = unicodedata.normalize("NFKD", str(text).translate(_POLISH_L))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def stop_coords(row: dict[str, Any]) -> tuple[float, float] | None:
    """Read (lat, lon) off a stop row; the API sends both as strings."""
    try:
        return float(row["szer_geo"]), float(row["dlug_geo"])
    except (KeyError, TypeError, ValueError):
        return None


def pole_variants(pole: str) -> tuple[str, ...]:
    """Spellings of a pole number to try — the API zero-pads it ("01")."""
    raw = str(pole).strip()
    padded = raw.zfill(2)
    stripped = raw.lstrip("0") or raw
    return tuple(dict.fromkeys((raw, padded, stripped)))


def match_stops(rows: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Filter stop rows whose group name contains `name`, ignoring diacritics.

    Pure function so it can be unit-tested without network access.
    """
    needle = normalize(name).strip()
    if not needle:
        return []
    return [r for r in rows if needle in normalize(r.get("nazwa_zespolu", ""))]


class WarsawApiClient:
    def __init__(
        self,
        api_key: str,
        timeout: float = 15.0,
        cache_dir: str | None = None,
        timetable_ttl: float = TIMETABLE_TTL,
        vehicles_ttl: float = VEHICLES_TTL,
    ) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout)
        self._cache_dir = cache_dir
        self._vehicles_ttl = vehicles_ttl

        # Counts requests that reached the network, so the poller can report
        # how much traffic a sweep actually cost.
        self.calls_made = 0

        self._stops: list[dict[str, Any]] = []
        self._stops_at = 0.0
        self._stops_lock = asyncio.Lock()
        # (zespol, slupek) -> row, built on demand and rebuilt when the stop list
        # is refreshed; -1.0 can never equal a real _stops_at timestamp.
        self._stop_index: dict[tuple[str, str], dict[str, Any]] = {}
        self._stop_index_at = -1.0

        self._vehicles: dict[int, tuple[float, list[dict[str, Any]]]] = {}
        self._vehicles_lock = asyncio.Lock()

        self._daily = DailyCache(
            os.path.join(cache_dir, TIMETABLE_CACHE_FILE) if cache_dir else None,
            timetable_ttl,
            name="timetable",
        )

    def load_caches(self) -> None:
        """Read the daily cache off disk, so startup reports what it will reuse."""
        self._daily.load()

    async def flush_caches(self) -> None:
        """Persist the daily cache so a restart does not refetch the whole day."""
        await self._daily.flush()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "WarsawApiClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _call(
        self,
        endpoint: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        retries: int = 2,
    ) -> list[Any]:
        if not self._api_key:
            raise WarsawApiError("No API key configured.")

        headers = {"Authorization": self._api_key}
        kwargs: dict[str, Any] = {"headers": headers}
        if payload is not None:
            kwargs["json"] = payload
        if timeout is not None:
            kwargs["timeout"] = timeout

        what = describe_call(endpoint, payload)
        for attempt in range(retries + 1):
            started = time.monotonic()
            try:
                # These calls only read data, so retrying a POST is safe.
                self.calls_made += 1
                resp = await self._client.post(f"{BASE_URL}/{endpoint}", **kwargs)
                if resp.status_code == 401:
                    raise WarsawApiError(
                        f"{endpoint}: unauthorized — the API key is missing or invalid."
                    )
                resp.raise_for_status()
                data = resp.json()
                log.info(
                    "api %s -> %s in %dms, %d row(s)",
                    what,
                    resp.status_code,
                    (time.monotonic() - started) * 1000,
                    len(data) if isinstance(data, list) else 0,
                )
                break
            except (httpx.HTTPError, ValueError) as exc:  # network / JSON errors
                if attempt < retries:
                    log.warning(
                        "api %s failed (attempt %d/%d): %s — retrying",
                        what,
                        attempt + 1,
                        retries + 1,
                        exc,
                    )
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                log.warning("api %s failed after %d attempt(s): %s", what, retries + 1, exc)
                raise WarsawApiError(f"Request to {endpoint} failed: {exc}") from exc

        # Success is a bare JSON array. Errors come back as an object with an
        # "error" key or as a bare string (bad parameters, rate limit, ...).
        if not isinstance(data, list):
            detail = data.get("error", data) if isinstance(data, dict) else data
            raise WarsawApiError(f"API error for {endpoint}: {detail!r}")
        return data

    # --- stops -------------------------------------------------------------

    @property
    def _stops_cache_path(self) -> str | None:
        if not self._cache_dir:
            return None
        return os.path.join(self._cache_dir, STOPS_CACHE_FILE)

    def _read_stops_cache(self) -> tuple[list[dict[str, Any]], float] | None:
        path = self._stops_cache_path
        if not path:
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            return payload["stops"], float(payload["fetched_at"])
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("Ignoring unreadable stop cache %s (%s).", path, exc)
            return None

    def _write_stops_cache(self, stops: list[dict[str, Any]], fetched_at: float) -> None:
        path = self._stops_cache_path
        if not path:
            return
        try:
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"fetched_at": fetched_at, "stops": stops}, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("Could not persist stop cache to %s (%s).", path, exc)

    async def _ensure_stops(self) -> list[dict[str, Any]]:
        """Return the full stop list, downloading it at most once per TTL.

        The lock stops concurrent searches from each kicking off their own 3 MB
        download. If the API is unreachable but a stale copy exists (in memory
        or on disk) it is used rather than failing the search.
        """
        now = time.time()
        if self._stops and now - self._stops_at < STOPS_TTL:
            return self._stops

        async with self._stops_lock:
            now = time.time()
            if self._stops and now - self._stops_at < STOPS_TTL:
                return self._stops

            cached = self._read_stops_cache()
            if cached is not None:
                stops, fetched_at = cached
                if now - fetched_at < STOPS_TTL:
                    self._stops, self._stops_at = stops, fetched_at
                    log.info("Loaded %d stop(s) from the on-disk cache.", len(stops))
                    return self._stops

            try:
                rows = await self._call(EP_STOPS, timeout=STOPS_TIMEOUT)
            except WarsawApiError:
                stale = self._stops or (cached[0] if cached else [])
                if stale:
                    log.warning("Stop list refresh failed; using the stale cache.")
                    self._stops = stale
                    self._stops_at = now  # back off before retrying
                    return self._stops
                raise

            self._stops = [_flatten(row) for row in rows]
            self._stops_at = now
            log.info("Downloaded %d stop pole(s).", len(self._stops))
            self._write_stops_cache(self._stops, now)
            return self._stops

    async def search_stops(self, name: str) -> list[dict[str, Any]]:
        """Find stop poles whose group name matches `name`.

        The API has no server-side search, so the whole (cached) stop list is
        filtered locally. Returns dicts with zespol (busstopId), slupek (pole),
        nazwa_zespolu, id_ulicy, szer_geo, dlug_geo.
        """
        rows = await self._ensure_stops()
        return match_stops(rows, name)[:MAX_SEARCH_RESULTS]

    async def stop_location(
        self, busstop_id: str, pole: str
    ) -> tuple[float, float] | None:
        """Coordinates of one stop pole, from the cached city stop list.

        Needed by the ETA (distance from vehicle to stop). Stops saved through
        the panel carry their own coordinates; this is the fallback for ones
        saved before that, and it costs no request unless the stop cache is cold.
        """
        rows = await self._ensure_stops()
        if self._stop_index_at != self._stops_at:
            self._stop_index = {
                (
                    str(r.get("zespol", "")).strip(),
                    str(r.get("slupek", "")).strip(),
                ): r
                for r in rows
            }
            self._stop_index_at = self._stops_at

        group = str(busstop_id).strip()
        for variant in pole_variants(pole):
            row = self._stop_index.get((group, variant))
            if row is not None:
                return stop_coords(row)
        return None

    # --- lines & timetables ------------------------------------------------

    async def lines_for_stop(self, busstop_id: str, pole: str) -> list[str]:
        """Lines calling at one pole. Cached for the service day — see cache.py."""

        async def fetch() -> list[str]:
            rows = await self._call(
                EP_LINES, {"busstopId": str(busstop_id), "busstopNr": str(pole)}
            )
            flat = (_flatten(row) for row in rows)
            return [str(r["linia"]) for r in flat if r.get("linia") is not None]

        return await self._daily.get(f"lines|{busstop_id}|{pole}", fetch)

    async def timetable(
        self, busstop_id: str, pole: str, line: str
    ) -> list[dict[str, Any]]:
        """Scheduled departures for one line at one stop pole (current day).

        The API publishes this once a day, so it is cached for the service day
        rather than refetched on every poll.
        """

        async def fetch() -> list[dict[str, Any]]:
            rows = await self._call(
                EP_TIMETABLE,
                {
                    "busstopId": str(busstop_id),
                    "busstopNr": str(pole),
                    "line": str(line),
                },
            )
            return [_flatten(row) for row in rows]

        return await self._daily.get(f"timetable|{busstop_id}|{pole}|{line}", fetch)

    # --- live positions ----------------------------------------------------

    async def vehicle_positions(
        self, vehicle_type: int, line: str | None = None
    ) -> list[dict[str, Any]]:
        """Live GPS positions. type: 1 = bus, 2 = tram.

        The endpoint returns every vehicle of the given type, so the snapshot is
        cached briefly and shared by all callers; `line` filters it locally.
        """
        vehicles = await self._vehicle_snapshot(vehicle_type)
        if line is None:
            return vehicles
        wanted = str(line).strip()
        return [v for v in vehicles if str(v.get("Lines", "")).strip() == wanted]

    async def _vehicle_snapshot(self, vehicle_type: int) -> list[dict[str, Any]]:
        cached = self._vehicles.get(vehicle_type)
        if cached and time.time() - cached[0] < self._vehicles_ttl:
            return cached[1]

        async with self._vehicles_lock:
            cached = self._vehicles.get(vehicle_type)
            if cached and time.time() - cached[0] < self._vehicles_ttl:
                return cached[1]

            rows = await self._call(EP_VEHICLES, {"type": int(vehicle_type)})
            vehicles = [_flatten(row) for row in rows]
            self._vehicles[vehicle_type] = (time.time(), vehicles)
            return vehicles
