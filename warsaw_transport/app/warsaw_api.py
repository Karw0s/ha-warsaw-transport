"""Async client for the Warsaw Open Data public-transport API.

All endpoints live under https://dane.um.warszawa.pl/api/action/, are called with
POST, take their parameters as a JSON body, and authenticate with the API key in
an `Authorization` header (no "Bearer" prefix). A successful response is a bare
JSON array; anything else is an error. See .claude/docs/warsaw-open-data-api.md.

Rows arrive in three encodings ({"values": [{key, value}, ...]}, a bare list of
those pairs, or an already-flat object); `_flatten` normalises all three.

Two endpoints cannot be filtered server-side, so this client caches them and
filters in Python: the stop list (whole city, ~3 MB) and the vehicle GPS feed
(all vehicles of one type, ~180 KB).
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

log = logging.getLogger("warsaw_transport.api")

BASE_URL = "https://dane.um.warszawa.pl/api/action"

EP_STOPS = "get_ztm_przystanki_komunikacji_miejskiej"
EP_LINES = "get_ztm_lista_linii_na_przystanku"
EP_TIMETABLE = "get_ztm_odjazdy_linii_z_przystanku"
EP_VEHICLES = "get_ztm_lokalizacja_pojazdow"

# The stop list changes rarely; the GPS feed refreshes roughly every 10 seconds.
STOPS_TTL = 24 * 3600
VEHICLES_TTL = 10
STOPS_CACHE_FILE = "stops_cache.json"
# Downloading every stop pole is ~3 MB, well beyond the default request timeout.
STOPS_TIMEOUT = 60.0

MAX_SEARCH_RESULTS = 50

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


def normalize(text: Any) -> str:
    """Casefold and strip Polish diacritics so "zeran" matches "Żerań"."""
    decomposed = unicodedata.normalize("NFKD", str(text).translate(_POLISH_L))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


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
    ) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout)
        self._cache_dir = cache_dir

        self._stops: list[dict[str, Any]] = []
        self._stops_at = 0.0
        self._stops_lock = asyncio.Lock()

        self._vehicles: dict[int, tuple[float, list[dict[str, Any]]]] = {}
        self._vehicles_lock = asyncio.Lock()

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

        for attempt in range(retries + 1):
            try:
                # These calls only read data, so retrying a POST is safe.
                resp = await self._client.post(f"{BASE_URL}/{endpoint}", **kwargs)
                if resp.status_code == 401:
                    raise WarsawApiError(
                        f"{endpoint}: unauthorized — the API key is missing or invalid."
                    )
                resp.raise_for_status()
                data = resp.json()
                break
            except (httpx.HTTPError, ValueError) as exc:  # network / JSON errors
                if attempt < retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
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

    # --- lines & timetables ------------------------------------------------

    async def lines_for_stop(self, busstop_id: str, pole: str) -> list[str]:
        rows = await self._call(
            EP_LINES, {"busstopId": str(busstop_id), "busstopNr": str(pole)}
        )
        flat = (_flatten(row) for row in rows)
        return [str(r["linia"]) for r in flat if r.get("linia") is not None]

    async def timetable(
        self, busstop_id: str, pole: str, line: str
    ) -> list[dict[str, Any]]:
        """Scheduled departures for one line at one stop pole (current day)."""
        rows = await self._call(
            EP_TIMETABLE,
            {
                "busstopId": str(busstop_id),
                "busstopNr": str(pole),
                "line": str(line),
            },
        )
        return [_flatten(row) for row in rows]

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
        if cached and time.time() - cached[0] < VEHICLES_TTL:
            return cached[1]

        async with self._vehicles_lock:
            cached = self._vehicles.get(vehicle_type)
            if cached and time.time() - cached[0] < VEHICLES_TTL:
                return cached[1]

            rows = await self._call(EP_VEHICLES, {"type": int(vehicle_type)})
            vehicles = [_flatten(row) for row in rows]
            self._vehicles[vehicle_type] = (time.time(), vehicles)
            log.debug("Fetched %d vehicle(s) of type %s.", len(vehicles), vehicle_type)
            return vehicles
