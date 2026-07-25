"""Async client for the Warsaw Open Data public-transport API.

All endpoints live under https://api.um.warszawa.pl/api/action/ and require an
`apikey` query parameter. Row-based responses wrap each record as a list of
{"key": ..., "value": ...} pairs under a "values" key; `_flatten` normalises
those into plain dicts.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger("warsaw_transport.api")

BASE_URL = "https://api.um.warszawa.pl/api/action"

# Dataset / resource identifiers (see DOCS.md for the source catalogue entries).
ID_STOPS_BY_NAME = "b27f4c17-5c50-4a5b-89dd-236b282bc499"
ID_LINES_AT_STOP = "88cd555f-6f31-43ca-9de4-66c479ad5942"
ID_TIMETABLE = "e923fa0e-d96c-43f9-ae6e-60518c9f3238"
RESOURCE_VEHICLES = "f2e5503e-927d-4ad3-9500-4ab9e55deb59"


class WarsawApiError(RuntimeError):
    """Raised when the API returns an error result (bad key, rate limit, ...)."""


def _flatten(row: Any) -> dict[str, Any]:
    """Turn a {"values": [{"key": k, "value": v}, ...]} row into {k: v}."""
    if isinstance(row, dict) and "values" in row:
        return {item.get("key"): item.get("value") for item in row["values"]}
    return row if isinstance(row, dict) else {}


class WarsawApiClient:
    def __init__(self, api_key: str, timeout: float = 15.0) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "WarsawApiClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _call(
        self, endpoint: str, params: dict[str, Any], *, retries: int = 2
    ) -> list[dict[str, Any]]:
        if not self._api_key:
            raise WarsawApiError("No API key configured.")

        query = {**params, "apikey": self._api_key}
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await self._client.get(f"{BASE_URL}/{endpoint}", params=query)
                resp.raise_for_status()
                data = resp.json()
                break
            except (httpx.HTTPError, ValueError) as exc:  # network / JSON errors
                last_exc = exc
                if attempt < retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise WarsawApiError(f"Request to {endpoint} failed: {exc}") from exc

        result = data.get("result")
        # The API signals errors either with result == "false" or a bare string
        # (e.g. "Błędna metoda lub parametry wywołania"/rate-limit messages).
        if result is None or isinstance(result, bool) or isinstance(result, str):
            raise WarsawApiError(
                f"API error for {endpoint}: {data.get('error') or result!r}"
            )
        return [_flatten(row) for row in result]

    async def search_stops(self, name: str) -> list[dict[str, Any]]:
        """Find stop poles whose group name matches `name`.

        Returns dicts with zespol (busstopId), slupek (pole), nazwa_zespolu,
        kierunek, szer_geo, dlug_geo.
        """
        rows = await self._call(
            "dbtimetable_get", {"id": ID_STOPS_BY_NAME, "name": name}
        )
        return rows

    async def lines_for_stop(self, busstop_id: str, pole: str) -> list[str]:
        rows = await self._call(
            "dbtimetable_get",
            {"id": ID_LINES_AT_STOP, "busstopId": busstop_id, "busstopNr": pole},
        )
        return [str(r.get("linia")) for r in rows if r.get("linia") is not None]

    async def timetable(
        self, busstop_id: str, pole: str, line: str
    ) -> list[dict[str, Any]]:
        """Scheduled departures for one line at one stop pole (current day)."""
        return await self._call(
            "dbtimetable_get",
            {
                "id": ID_TIMETABLE,
                "busstopId": busstop_id,
                "busstopNr": pole,
                "line": line,
            },
        )

    async def vehicle_positions(
        self, vehicle_type: int, line: str | None = None
    ) -> list[dict[str, Any]]:
        """Live GPS positions. type: 1 = bus, 2 = tram."""
        params: dict[str, Any] = {
            "resource_id": RESOURCE_VEHICLES,
            "type": vehicle_type,
        }
        if line:
            params["line"] = line
        return await self._call("busestrams_get", params)
