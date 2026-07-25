"""FastAPI application: ingress web UI + JSON API + background MQTT poller."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, load_settings
from .departures import next_departures
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


state = AppState()


async def compute_and_publish(stop: dict[str, Any]) -> list[dict[str, Any]]:
    """Compute departures for a stop and push to MQTT (if enabled)."""
    departures = await next_departures(
        state.client,
        stop["busstop_id"],
        stop["pole"],
        limit=5,
        gps_overlay=state.settings.gps_overlay,
    )
    if state.mqtt is not None:
        state.mqtt.publish_state(stop, departures)
    return departures


async def poll_loop() -> None:
    interval = state.settings.poll_interval
    log.info("Poller started (every %ss).", interval)
    while True:
        stops = state.store.list_stops()
        for stop in stops:
            try:
                await compute_and_publish(stop)
            except WarsawApiError as exc:
                log.warning("Departure update failed for %s: %s", stop["id"], exc)
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("Unexpected error updating stop %s", stop["id"])
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    state.settings = settings
    state.client = WarsawApiClient(settings.api_key, cache_dir=settings.data_dir)
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
    stop = state.store.add(
        busstop_id,
        pole,
        payload.get("name", f"{busstop_id}/{pole}"),
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
        deps = await next_departures(
            state.client,
            stop["busstop_id"],
            stop["pole"],
            limit=5,
            gps_overlay=state.settings.gps_overlay,
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
