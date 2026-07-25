"""FastAPI application: ingress web UI + JSON API + background MQTT poller."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, load_settings
from .departures import fetch_vehicles, next_departures
from .eta import VehicleTracker, to_float
from .mqtt_publisher import MqttPublisher
from .store import StopStore
from .warsaw_api import WarsawApiClient, WarsawApiError

log = logging.getLogger("warsaw_transport")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class AppState:
    settings: Settings
    client: WarsawApiClient
    store: StopStore
    mqtt: MqttPublisher | None = None
    poller: asyncio.Task | None = None
    # One tracker for the whole process: the arrival estimate is built from how a
    # vehicle's distance to the stop changes between polls, so the history has to
    # outlive a single request and be shared by the poller and the panel.
    tracker: VehicleTracker = VehicleTracker()


state = AppState()


async def stop_location(stop: dict[str, Any]) -> tuple[float, float] | None:
    """Coordinates for a stop, needed to estimate arrivals.

    Stops added through the panel bring their own; older ones are resolved once
    from the cached city stop list and written back. A failure here is not fatal
    — the stop simply gets no ETA.
    """
    lat, lon = stop.get("lat"), stop.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return float(lat), float(lon)

    try:
        found = await state.client.stop_location(stop["busstop_id"], stop["pole"])
    except WarsawApiError as exc:
        log.warning("No coordinates for %s (%s); skipping its ETA.", stop["id"], exc)
        return None
    if found is None:
        log.warning("Stop %s is not in the city stop list; skipping its ETA.", stop["id"])
        return None

    state.store.set_location(stop["id"], *found)
    return found


async def compute_and_publish(
    stop: dict[str, Any],
    vehicles: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Compute departures for a stop and push to MQTT (if enabled)."""
    location = await stop_location(stop) if state.settings.gps_overlay else None
    departures = await next_departures(
        state.client,
        stop["busstop_id"],
        stop["pole"],
        limit=5,
        gps_overlay=state.settings.gps_overlay,
        vehicles=vehicles,
        stop_location=location,
        tracker=state.tracker,
    )
    if state.mqtt is not None:
        state.mqtt.publish_state(stop, departures)
    return departures


async def poll_loop() -> None:
    interval = state.settings.poll_interval
    log.info("Poller started (every %ss).", interval)
    while True:
        started = time.monotonic()
        calls_before = state.client.calls_made
        stops = state.store.list_stops()

        # The GPS feeds are city-wide and stop-independent, so fetch them once
        # per sweep and share the snapshot with every stop. Passing [] (not
        # None) on failure means the stops skip the overlay instead of each
        # retrying the download.
        vehicles: list[dict[str, Any]] | None = None
        if stops and state.settings.gps_overlay:
            try:
                vehicles = await fetch_vehicles(state.client)
            except WarsawApiError as exc:
                log.warning("GPS feeds unavailable this cycle: %s", exc)
                vehicles = []

        for stop in stops:
            try:
                await compute_and_publish(stop, vehicles=vehicles)
            except WarsawApiError as exc:
                log.warning("Departure update failed for %s: %s", stop["id"], exc)
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("Unexpected error updating stop %s", stop["id"])

        state.tracker.prune()
        await state.client.flush_caches()
        log.info(
            "Poll sweep: %d stop(s), %d API call(s) in %.1fs.",
            len(stops),
            state.client.calls_made - calls_before,
            time.monotonic() - started,
        )
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    state.settings = settings
    state.client = WarsawApiClient(
        settings.api_key,
        cache_dir=settings.data_dir,
        timetable_ttl=settings.timetable_ttl,
        vehicles_ttl=settings.vehicles_ttl,
    )
    # Warm eagerly so the log reports the cache state at startup rather than
    # part-way through the first sweep (or never, when no stops are saved yet).
    state.client.load_caches()
    state.store = StopStore(settings.data_dir)

    if settings.mqtt_enabled:
        try:
            state.mqtt = MqttPublisher(
                settings.mqtt_host,
                settings.mqtt_port,
                settings.mqtt_user,
                settings.mqtt_password,
            )
            state.mqtt.connect()
            for stop in state.store.list_stops():
                state.mqtt.publish_discovery(stop)
        except Exception:  # noqa: BLE001
            log.exception("MQTT connection failed; running without dashboard publishing.")
            state.mqtt = None
    else:
        log.warning("MQTT not configured; departures will only show in the web panel.")

    state.poller = asyncio.create_task(poll_loop())
    try:
        yield
    finally:
        if state.poller:
            state.poller.cancel()
        if state.mqtt:
            state.mqtt.disconnect()
        # Keep the day's timetables across a restart.
        await state.client.flush_caches()
        await state.client.aclose()


app = FastAPI(title="Warsaw Public Transport", lifespan=lifespan)


CARD_URL = "/local/warsaw_transport/warsaw-transport-card.js"


@app.get("/api/health")
async def health() -> dict[str, Any]:
    settings = state.settings
    return {
        "api_key_set": bool(settings.api_key),
        "mqtt": state.mqtt is not None,
        "gps_overlay": settings.gps_overlay,
        "saved_stops": len(state.store.list_stops()),
        # Lovelace card install state — the panel pairs this with a fetch of
        # CARD_URL to tell "not installed" apart from "installed but Home
        # Assistant is not serving /local yet".
        "card_installed": settings.card_installed,
        "card_path": settings.card_path,
        "card_url": CARD_URL,
        "card_www_created": settings.www_created,
        "ha_config_dir": settings.ha_config_dir,
    }


@app.get("/api/card")
async def card_file() -> Any:
    """Serve the card straight from the add-on, for manual installation."""
    path = state.settings.card_src
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="card file not bundled")
    return FileResponse(
        path,
        media_type="text/javascript",
        filename="warsaw-transport-card.js",
    )


@app.get("/api/stops/search")
async def search_stops(name: str = Query(..., min_length=2)) -> Any:
    try:
        rows = await state.client.search_stops(name)
    except WarsawApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    results = [
        {
            "busstop_id": r.get("zespol"),
            "pole": r.get("slupek"),
            "name": r.get("nazwa_zespolu"),
            "lat": r.get("szer_geo"),
            "lon": r.get("dlug_geo"),
        }
        for r in rows
    ]
    return results


@app.get("/api/stops")
async def list_saved() -> Any:
    return state.store.list_stops()


@app.post("/api/stops")
async def add_stop(payload: dict[str, Any]) -> Any:
    busstop_id = str(payload.get("busstop_id", "")).strip()
    pole = str(payload.get("pole", "")).strip()
    if not busstop_id or not pole:
        raise HTTPException(status_code=400, detail="busstop_id and pole are required")
    # Coordinates come from the search result; they are only used for the ETA, so
    # an unparseable pair is dropped rather than rejected.
    lat, lon = to_float(payload.get("lat")), to_float(payload.get("lon"))
    stop = state.store.add(
        busstop_id,
        pole,
        payload.get("name", f"{busstop_id}/{pole}"),
        lat=lat,
        lon=lon,
    )
    if state.mqtt is not None:
        state.mqtt.publish_discovery(stop)
    try:
        await compute_and_publish(stop)
    except WarsawApiError as exc:
        log.warning("Initial fetch for new stop failed: %s", exc)
    return stop


@app.delete("/api/stops/{stop_id}")
async def delete_stop(stop_id: str) -> Any:
    if not state.store.remove(stop_id):
        raise HTTPException(status_code=404, detail="stop not found")
    if state.mqtt is not None:
        state.mqtt.remove_stop(stop_id)
    return {"removed": stop_id}


@app.get("/api/departures/{stop_id}")
async def departures_for(stop_id: str) -> Any:
    stop = state.store.get(stop_id)
    if stop is None:
        raise HTTPException(status_code=404, detail="stop not found")
    try:
        location = await stop_location(stop) if state.settings.gps_overlay else None
        deps = await next_departures(
            state.client,
            stop["busstop_id"],
            stop["pole"],
            limit=5,
            gps_overlay=state.settings.gps_overlay,
            stop_location=location,
            tracker=state.tracker,
        )
    except WarsawApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return JSONResponse({"stop": stop, "departures": deps})


# Serve the ingress web UI at the root. Mounted last so /api/* takes precedence.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    main()
