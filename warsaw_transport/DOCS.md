# Warsaw Public Transport — Add-on Documentation

This add-on shows the next departures for the Warsaw bus/tram stops you choose,
and publishes each one to Home Assistant as a sensor you can put on a dashboard.

## 1. Prerequisites

- **MQTT broker** — install the official **Mosquitto broker** add-on and make sure
  Home Assistant's MQTT integration is set up. Departures are delivered to your
  dashboard through MQTT Discovery.
- **A Warsaw Open Data API key** — register (free) at
  <https://dane.um.warszawa.pl/pl/key-api> and copy your API key.

## 2. Installation

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories** and add:
   `https://github.com/Karw0s/ha-warsaw-transport`
2. Install **Warsaw Public Transport**.
3. Open the **Configuration** tab and paste your API key into **`api_key`**
   (the field is hidden as it is a password/secret). Save.
4. Start the add-on and open **Open Web UI** (the ingress panel).

## 3. Options

| Option | Default | Description |
|--------|---------|-------------|
| `api_key` | *(empty)* | Your dane.um.warszawa.pl key. Stored as a secret (password field). |
| `legacy_api_key` | *(empty)* | **Optional.** An api.um.warszawa.pl key, which unlocks sharper arrival estimates — see §3.1. Also a secret. |
| `poll_interval` | `30` | Seconds between departure refreshes (minimum 10). |
| `gps_overlay` | `true` | Match scheduled departures to live vehicle GPS (line + brigade). |
| `log_level` | `info` | `debug`, `info`, `warning`, or `error`. |

### 3.1 Why there are two API keys

Everything the add-on needs comes from `dane.um.warszawa.pl` **except one dataset**:
the route plans that say which stops each trip serves and how far apart they are.
That one is still only published on the older `api.um.warszawa.pl` host, which
issues its own keys.

- **Without `legacy_api_key`** — everything works; arrival estimates are measured
  as the crow flies between the vehicle and your stop.
- **With `legacy_api_key`** — estimates are measured *along the route the vehicle
  is actually driving*, using the times it reached the previous stops. They are
  noticeably steadier, and the card can say how many stops away the vehicle is.

Register at <https://api.um.warszawa.pl/> (free, separate from your
dane.um.warszawa.pl account) and paste the key into `legacy_api_key`. The route
catalogue is downloaded once a day (~3.7 MB) and cached on `/data`; it adds no
per-poll requests. The add-on's web panel header shows which mode is in use.

## 4. Choosing stops

1. In the web panel, type a stop name (e.g. `Metro Politechnika`) and **Search**.
   Accents are optional — `zeran` finds `Żerań`. The very first search downloads the
   full list of city stops and can take a few seconds; later searches are instant.
2. Each result shows the stop group and its ID (group/pole). Click **Add**.
3. The stop appears under **Your stops** with a live preview of the next 5 departures.
4. Within a few seconds a sensor named `sensor.warsaw_<stop>` appears in Home
   Assistant (grouped under the **Warsaw Transport** device).

Removing a stop in the panel also removes its Home Assistant entity.

## 5. Sensor data

Each stop sensor:

- **State** — minutes until the next **scheduled** departure (`min`). It stays on
  the timetable even when a live estimate exists, so automations keep one meaning.
- **Attributes** — `stop_name`, `busstop_id`, `pole`, `next_eta_minutes` (the live
  estimate for the first departure, or `null`), and a `departures` list of the
  next 5:
  `{ line, direction, time, minutes, brigade, route, live, lat, lon, eta_time,
  eta_minutes, eta_timestamp, delay_minutes, eta_source, eta_status, distance_m,
  route_distance_m, stops_away }`.
  `live: true` means the departure is currently matched to a tracked vehicle.

### 5.1 The arrival estimate

The city API publishes vehicle positions and nothing else — no predicted arrivals.
The add-on works the arrival out for itself, in one of two ways.

**Along the route** (when `legacy_api_key` is set — see §3.1). The vehicle is placed
on the sequence of stops its trip actually serves, which gives the real distance it
still has to cover, and its speed is measured from the times it reached the previous
stops. Because that speed is measured stop to stop it already includes the seconds
spent standing at them. This is the accurate mode: the predicted arrival stays put
from poll to poll instead of drifting.

**As the crow flies** (always available). Without route plans the only signal is how
fast the straight-line gap to your stop is shrinking between polls. Traffic and
detours still show up in that rate, but the estimate is coarser and is withheld more
often.

| Field | Meaning |
|---|---|
| `eta_time` | Estimated arrival, `HH:MM` (`eta_timestamp` is the full ISO value). |
| `eta_minutes` | Minutes until that arrival, at the moment the add-on published it. |
| `delay_minutes` | Estimate minus timetable: `+3` is three minutes late, `-1` early. |
| `eta_source` | `route` — measured along the vehicle's route. `tracked` — measured from the straight-line gap closing. `approx` — a rough figure from a typical urban speed, used for the first ~30 s after a vehicle appears. |
| `eta_status` | `ok`, or why there is no estimate (below). |
| `distance_m` | Straight-line distance from the vehicle to the stop. |
| `route_distance_m` | Metres still to travel **on the route**; `null` without route plans. |
| `stops_away` | Stops the vehicle must still serve before yours (`1` = next stop); `null` without route plans. |
| `route` | The trip's route variant code, e.g. `TO-FSOpOko`. |

An estimate is withheld — `eta_time: null`, with the departure still marked
`live` — when the vehicle's positions cannot support one:

| `eta_status` | Meaning |
|---|---|
| `moving_away` | The vehicle is getting further from the stop; a brigade runs back and forth all day, so it is on another leg of its run. |
| `stalled` | It has not moved at all since it was first seen, e.g. parked mid-run. |
| `waiting` | The estimate lands far ahead of the timetable — a layover at a terminus, where the schedule is the better predictor. |
| `passed` | It is already beyond your stop on this route, so this trip has served you. (Route plans only.) |
| `stale_fix` | Its last GPS fix is too old to trust. |
| `too_far` | It is more than 20 km away. |

Estimates need `gps_overlay: true` (the default) and the stop's coordinates, which
the add-on stores when you add it. Route-measured estimates additionally need
`legacy_api_key`; without it the fields above still appear, with `eta_source` never
being `route` and `route_distance_m`/`stops_away` staying `null`.

## 6. The dashboard card

The add-on ships a custom Lovelace card that shows one stop in the style of the
built-in weather card: the next departure gets the big-number treatment, the
four after it sit below in a forecast-style row.

### 6.1 Register the card (once)

Every time the add-on starts it copies the card into your Home Assistant config
folder at `www/warsaw_transport/warsaw-transport-card.js`, so it stays in sync
with the add-on. You only need to tell Home Assistant about it once:

1. **Settings → Dashboards → ⋮ (top right) → Resources → + Add resource**
2. URL: `/local/warsaw_transport/warsaw-transport-card.js`
3. Resource type: **JavaScript module**
4. Create.
5. **Restart Home Assistant Core once** — Settings → System → ⋮ → *Restart Home
   Assistant*. This step is required the first time, and restarting the *add-on*
   is not a substitute. Home Assistant only starts serving `/local/` if the
   `www` folder already exists when Core starts up, and this add-on is what
   creates that folder. Skip it and the card fails with
   `GET /local/warsaw_transport/warsaw-transport-card.js 404 (Not Found)`
   followed by `Custom element not found: warsaw-transport-card`.

If the Resources menu is missing, turn on **Advanced Mode** in your user profile.

The add-on's web panel has a **Dashboard card** section that tells you exactly
where things stand — whether the file was installed, whether Home Assistant is
serving it, and what to do next. Check there first if the card does not appear.

### 6.2 Add it to a dashboard

**Edit dashboard → + Add card**, search for **Warsaw Transport**, and pick your
stop's sensor from the dropdown. Or in YAML:

```yaml
type: custom:warsaw-transport-card
entity: sensor.warsaw_7009_01
```

### 6.3 Card options

| Option | Default | Description |
|--------|---------|-------------|
| `entity` | *(required)* | The stop's sensor, e.g. `sensor.warsaw_7009_01`. |
| `name` | the stop name | Override the title. |
| `icon` | `mdi:bus` | Icon shown next to the title (e.g. `mdi:tram`). |
| `count` | `5` | How many departures to show, `1`–`5` (the add-on publishes 5). |
| `show_stop_id` | `true` | Show the `stop 7009 / 01` line under the title. |

The countdown is recalculated in the browser every 30 seconds, so the minutes
keep ticking down between add-on refreshes. A green **● live** marker means the
departure is matched to a vehicle currently reporting its GPS position — exactly
the same data the add-on's own web panel shows.

When that vehicle's position supports an estimate, it appears beside the
scheduled time as `14:05 → 14:07`, with the delay as a chip: amber when the bus
is running late, green when it is on time or early. A `~` before the time (as in
`→ ~14:07`) marks a rough first guess, replaced by a measured one within about a
poll. With route plans configured (§3.1) the card also shows how far off the
vehicle is — `3 stops away`, or `next stop`. The big countdown always stays on the
timetable, so it means the same thing whether or not a vehicle is being tracked —
see §5.1 for how the estimate is made and when it is deliberately left out.

### 6.4 Alternatives without the card

A plain **Entities** card showing just the "minutes to next" value:

```yaml
type: entities
title: My stops
entities:
  - entity: sensor.warsaw_7009_01
  - entity: sensor.warsaw_7013_02
```

Or a **Markdown** card built from the attributes:

```yaml
type: markdown
content: >
  ## {{ state_attr('sensor.warsaw_7009_01', 'stop_name') }}
  {% for d in state_attr('sensor.warsaw_7009_01', 'departures') %}
  **{{ d.line }}** → {{ d.direction }} — {{ d.time }} ({{ d.minutes }} min){% if d.live %} 🟢{% endif %}
  {% endfor %}
```

## 7. Notes & limitations

- The card needs the MQTT sensor: no broker configured means no entity, and
  therefore nothing for the card to show.
- After updating the add-on, hard-refresh the dashboard (Ctrl/Cmd+Shift+R) —
  browsers cache files under `/local/` aggressively, so you may otherwise keep
  running the previous version of the card.

### Troubleshooting the card

The add-on panel's **Dashboard card** section gives the verdict directly. What it
can say, and what to do:

| Panel says | Meaning | Fix |
|---|---|---|
| ✅ Card installed and reachable | Everything is in place. | Add it via **+ Add card → Warsaw Transport**. |
| ⚠ Installed but Home Assistant is not serving it | The file is on disk but `/local/` is not routed. | **Restart Home Assistant Core** (see §6.1 step 5). |
| ⚠ Could not be installed | The config folder was not mounted or not writable. | **Rebuild** the add-on (⋮ → Rebuild), or use the panel's **Download the card** link and copy it to `<config>/www/warsaw_transport/` by hand. |

If you see `404 (Not Found)` for the resource, or `Custom element not found:
warsaw-transport-card` (the same fault one step later — the file never loads, so
the element is never defined), confirm which of the two causes it is:

1. Put any text file at `<config>/www/test.txt` and open
   `http://<your-ha>:8123/local/test.txt` in a **plain browser tab**, not the
   dashboard, to avoid cached responses.
2. **It also 404s** → `/local/` is not being served at all. Restart Home
   Assistant Core.
3. **It loads but the card does not** → the card file is not in the folder Core
   actually serves. Compare `<config>` against the path printed in the add-on
   log (`Lovelace card installed: … (N bytes)`). Note that recent versions of
   the *Terminal & SSH* and *File editor* add-ons expose the Home Assistant
   config at `/homeassistant`, with `/config` being that add-on's own folder —
   so an `ls` can look convincing while pointing somewhere Core never reads.

Changing the add-on's config-folder mapping needs a **Rebuild** (⋮ → Rebuild),
not just a restart, for the new mount to take effect.
- The ZTM API exposes timetables for the **current day only**; lines that do not
  run today will have no departures.
- The GPS overlay is best-effort — a scheduled departure is flagged `live` only
  when a vehicle reporting the same line **and brigade** is currently online, with
  a recent GPS fix. The bus and tram feeds are separate downloads, and only the
  ones your stops need are fetched — a tram-only stop list never pulls the bus
  feed — so a stop served by both modes is still fully covered, at no cost to
  one served by a single mode.
- The arrival estimate is exactly that: a straight-line calculation from vehicle
  positions, since the API publishes no route geometry and no predicted arrivals.
  It is at its best for a vehicle already on its way to you and a few minutes out;
  it is withheld rather than guessed when the positions say nothing useful (§5.1).
  Treat it as a better-informed guess than the timetable, not as gospel.
- Data © City of Warsaw, provided under Creative Commons Attribution.
