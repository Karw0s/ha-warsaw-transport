# Changelog

## 0.6.0

- **Arrival estimates can now be measured along the vehicle's route.** Given the
  route plan for the trip, the add-on knows the stops it still has to serve, the
  real distance to cover, and how long it took over the previous stops — so the
  estimate is built from actual progress rather than from the straight-line gap.
  Predictions stay put from one poll to the next instead of drifting, and a
  vehicle looping or approaching down a parallel street no longer reads as
  "moving away".
- New optional **`legacy_api_key`** option. Route plans are the one dataset with
  no equivalent on `dane.um.warszawa.pl`, so they come from the older
  `api.um.warszawa.pl` host, which issues its own keys — register free at
  <https://api.um.warszawa.pl/>. **Without it nothing changes**: estimates keep
  using the straight-line measurement introduced in 0.5.0.
- The catalogue (~3.7 MB, 2,049 route variants) is fetched **once a day** and
  cached in `/data/routes_cache.json`. Polling still costs two API calls per
  cycle regardless of how many stops you track.
- Departures gained `route_distance_m` (metres still to travel on the route),
  `stops_away` (`1` = next stop) and `route` (the trip's route code);
  `eta_source` gained `route`, and `eta_status` gained `passed` for a trip that
  has already served your stop. The card shows `3 stops away` under the next
  departure, and the panel header says which estimation mode is in use.
- Route plan access is behind a provider adapter (`app/routes.py`), so if the
  dataset reappears on `dane.um.warszawa.pl` only that one class changes.
- **Note on the upstream data:** `public_transport_routes` documents `odleglosc`
  as the distance from the start of the route, but it is really the distance from
  the *previous* stop — reading it as documented makes 44% of segments negative.
  The add-on sums it itself; see `.claude/docs/endpoint-trasy-pojazdow.md`.

## 0.5.0

- **Live arrival estimates.** A departure matched to a vehicle now also carries an
  estimated arrival time and its delay against the timetable, shown on the card
  next to the scheduled time (`14:05 → 14:07`, with a `+2` chip) and in the
  add-on's web panel. The estimate comes from how fast the vehicle's distance to
  your stop is shrinking between polls, so traffic and detours are reflected
  without needing route geometry the API does not publish.
- The estimate is deliberately withheld rather than guessed when the positions do
  not support one — a vehicle heading away from the stop (the same brigade runs
  back and forth all day), one parked mid-run, or one laying over at a terminus
  long before its departure. Those keep the plain **● live** marker, as before.
  A vehicle only just spotted shows a rough figure marked with `~` until a second
  GPS fix gives a real speed, about one poll later.
- The sensor **state is unchanged** — still minutes to the next *scheduled*
  departure, so existing automations keep working. The estimate is available in
  the attributes: `next_eta_minutes`, and per departure `eta_time`,
  `eta_minutes`, `delay_minutes`, `eta_source`, `eta_status`, `distance_m`.
- A departure is now flagged `live` only while its vehicle's GPS fix is recent.
  The feed contains fixes days or months old, which previously lit up departures
  no vehicle was actually running.
- A vehicle is matched to the **soonest** departure of its line and brigade only.
  Brigades repeat through the day, so a bus visible now also lit up its run two
  hours later.
- Stops added from the panel now store their coordinates (needed to measure the
  distance to the vehicle); stops added earlier are filled in automatically from
  the cached city stop list, at no extra API cost.
- No new API calls: the estimate is computed from the GPS snapshot the add-on
  already fetches once per cycle.

## 0.4.0

- **Fixed:** the add-on re-downloaded every timetable on every poll. Timetables
  and stop line lists are published once a day, but were refetched every 30
  seconds — a stop served by five lines cost six API calls per cycle. They are
  now cached for the service day, so a warm cycle costs **two calls in total, no
  matter how many stops you track**. For a single five-line stop that is ~2,880
  timetable requests a day down to ~12.
- The live GPS feeds (bus and tram) are fetched **once per cycle and shared by
  every tracked stop**, instead of once per stop.
- Timetables are cached in `/data/timetable_cache.json` and reused across
  restarts and add-on updates. The service day rolls over at 04:00 rather than
  midnight, so after-midnight departures (expressed as hours ≥ 24, e.g. `25:14`)
  are not dropped as the clock passes midnight.
- Every API request that reaches the network is now logged with its parameters,
  so calls can be told apart in the log — including bus vs tram GPS feeds:

  ```
  api vehicles[bus] type=1 -> 200 in 41ms, 1832 row(s)
  api timetable busstopId=7009 busstopNr=01 line=190 -> 200 in 34ms, 42 row(s)
  Poll sweep: 2 stop(s), 2 API call(s) in 0.1s.
  ```

  Each cycle ends with a summary of how many calls it cost, and startup reports
  whether the day's timetables were reused or fetched fresh. Set
  `log_level: debug` to also see cache hits.
- Two new environment-variable knobs (no add-on option; they rarely need
  changing): `WT_TIMETABLE_TTL` (default 12 h) and `WT_VEHICLES_TTL` (defaults to
  the poll interval, capped at 20 s).

## 0.3.1

- **Fixed:** the Lovelace card could fail to load with
  `404 (Not Found)` / `Custom element not found: warsaw-transport-card` even
  though the file was installed. Home Assistant only serves `/local/` when the
  `www` folder exists at Core startup, and the add-on creates that folder — so a
  Home Assistant **Core restart** is required the first time. This is now a
  documented setup step and the add-on warns about it in its log.
- The web panel has a new **Dashboard card** section that reports whether the
  card is installed *and* whether Home Assistant is actually serving it, with the
  resource URL to copy and the specific fix for whichever is wrong.
- The card install no longer reports success when it has not written anywhere
  useful: the config folder is now identified by its `configuration.yaml` rather
  than by merely existing, and the copy is verified on disk and logged with its
  resolved path and size.
- New `GET /api/card` endpoint serves the card for manual installation when the
  config folder is unavailable.

## 0.3.0

- New **Warsaw Transport** Lovelace card: one stop per card, laid out like the
  built-in weather card — the next departure as a big countdown, the following
  four in a forecast-style row, with live-GPS markers.
- The card is configurable from the dashboard UI (no YAML needed) and supports
  `name`, `icon`, `count` and `show_stop_id`.
- The add-on now installs the card into `<config>/www/warsaw_transport/` on every
  start, so it updates along with the add-on. Register it once as a dashboard
  resource — see DOCS.md §6. This adds the `homeassistant_config:rw` mapping.
- The card's countdown refreshes in the browser every 30 seconds, so minutes stay
  accurate between add-on polls.

## 0.2.0

- Migrated to the `dane.um.warszawa.pl` API; `api.um.warszawa.pl` is deprecated.
  Existing API keys keep working — no action needed beyond updating the add-on.
- **Breaking:** removed the `vehicle_type` option. The GPS overlay now checks the
  bus *and* tram feeds, so stops served by both get live data.
- Stop search runs locally against a cached copy of the city stop list (the new
  API has no server-side search). The cache is kept on `/data` and refreshed
  daily, so it survives restarts. Accent-insensitive: `zeran` finds `Żerań`.
- Live vehicle positions are fetched once per refresh cycle and shared by all
  saved stops instead of once per line.
- Removed the stop-level `direction` field (search results, the stop sensor
  attribute, and the sensor's friendly name) — the new stops endpoint does not
  provide one. Each departure still carries its own destination.

## 0.1.0

- Initial release.
- Ingress web panel to search Warsaw stops by name and add them.
- Next 5 departures per stop from the ZTM timetable, with a live GPS overlay
  (matched by line + brigade).
- Publishes each stop as a Home Assistant sensor via MQTT Discovery.
- API key stored as a hidden (password) add-on option.
