# Warsaw Public Transport — Home Assistant Add-on

A Home Assistant **add-on** that shows live Warsaw public-transport departures on your
dashboard. Pick bus/tram stops in the add-on's web panel and each one is published to
Home Assistant as a sensor (via MQTT Discovery) showing the **next 5 departures** relative
to the current time, using scheduled timetables augmented with a **live GPS overlay**.

When a departure is matched to a vehicle that is on its way, the add-on also estimates
**when it will actually arrive**, since the API publishes positions only. Given an optional
second API key it measures **along the route the vehicle is driving** — the stops it has
still to serve and how long it took over the ones behind it; otherwise it falls back to how
fast the straight-line gap is closing. The estimate sits next to the scheduled time with its
delay (`14:05 → 14:07 +2`), and is withheld rather than guessed when the positions cannot
support one; see [`warsaw_transport/DOCS.md`](warsaw_transport/DOCS.md) §3.1 and §5.1.

It also ships a **custom Lovelace card** that puts a stop on your dashboard in the style of
the built-in weather card — the next departure as a big countdown, the four after it in a
forecast-style row. The add-on installs the card for you; see
[`warsaw_transport/DOCS.md`](warsaw_transport/DOCS.md) §6.

Data source: [Warsaw Open Data API](https://dane.um.warszawa.pl/) (Zarząd Transportu
Miejskiego). An API key is required — see the add-on docs.

## Installation

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**.
2. Open the **⋮** menu (top-right) → **Repositories**.
3. Add this repository URL:

   ```
   https://github.com/Karw0s/ha-warsaw-transport
   ```

4. Install the **Warsaw Public Transport** add-on that appears.
5. Follow the setup steps in [`warsaw_transport/DOCS.md`](warsaw_transport/DOCS.md).

## Requirements

- A Home Assistant install that supports add-ons (Home Assistant OS or Supervised).
- The **Mosquitto broker** add-on (or any MQTT broker configured in HA) — departures are
  published via MQTT Discovery.
- A free API key from <https://dane.um.warszawa.pl/pl/key-api>.

## API usage and caching

The API is polled once every `poll_interval` seconds (default 30), but only the data that
actually changes is refetched:

| Endpoint | How often |
|---|---|
| `get_ztm_lokalizacja_pojazdow` (live GPS) | Once or twice per sweep — **only the feeds your stops need** (trams are lines 1–99, buses 100+), **shared by every tracked stop**. |
| `get_ztm_lista_linii_na_przystanku` (lines at a pole) | Once per stop per service day. |
| `get_ztm_odjazdy_linii_z_przystanku` (timetable) | Once per stop+line per service day (the API publishes it daily). |
| `get_ztm_przystanki_komunikacji_miejskiej` (stop list) | Once a day, on demand when searching. |
| `public_transport_routes` (route plans, legacy host) | Once a day, only when `legacy_api_key` is set. |

Timetables are cached in `/data/timetable_cache.json` and reused across restarts. The
service day rolls over at 04:00 rather than midnight, so after-midnight departures
(expressed as hours ≥ 24, e.g. `25:14`) are not dropped when the clock passes midnight.

Every request that reaches the network is logged at INFO with its parameters, so the log
shows exactly what was called and what was served from cache:

```
INFO warsaw_transport.api: api vehicles[bus] type=1 -> 200 in 41ms, 1832 row(s)
INFO warsaw_transport.api: api vehicles[tram] type=2 -> 200 in 46ms, 412 row(s)
INFO warsaw_transport.api: api timetable busstopId=7009 busstopNr=01 line=190 -> 200 in 34ms, 42 row(s)
INFO warsaw_transport: Poll sweep: 2 stop(s), 2 API call(s) in 0.1s.
```

Set `log_level: debug` to also see cache hits. Two extra knobs are available as environment
variables (no add-on option, they rarely need changing):

| Variable | Default | Meaning |
|---|---|---|
| `WT_TIMETABLE_TTL` | `43200` (12 h) | Seconds before a cached timetable/line list is refreshed. Entries always expire at the service-day rollover regardless. |
| `WT_VEHICLES_TTL` | `poll_interval`, capped at 20 | Seconds a GPS snapshot is shared between the poller and the web panel. |
| `WT_LEGACY_API_BASE` | `https://api.um.warszawa.pl/api/action` | Host serving the route plans; override to point at a replacement. |

## What's in this repo

| Path | Purpose |
|------|---------|
| `repository.yaml` | Registers this repo as a Home Assistant add-on store. |
| `warsaw_transport/` | The add-on itself (Docker container + FastAPI app). |
| `warsaw_transport/lovelace/` | The custom Lovelace card (plain JS, no build step). |
| `warsaw_transport/DOCS.md` | End-user setup and dashboard-card documentation. |
| `scripts/smoke.py` | Offline unit checks for the parsing/merge logic. |
| `.claude/docs/` | Reference notes on the dane.um.warszawa.pl API endpoints. |
| `dane-um-warszawa-requests.http` | Ready-to-run requests for all four endpoints. |

## License

MIT. Transport data © City of Warsaw, provided under Creative Commons Attribution.
