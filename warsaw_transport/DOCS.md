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
| `poll_interval` | `30` | Seconds between departure refreshes (minimum 10). |
| `gps_overlay` | `true` | Match scheduled departures to live vehicle GPS (line + brigade). |
| `log_level` | `info` | `debug`, `info`, `warning`, or `error`. |

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

- **State** — minutes until the next departure (`min`).
- **Attributes** — `stop_name`, `busstop_id`, `pole`, and a
  `departures` list of the next 5:
  `{ line, direction, time, minutes, brigade, live, lat, lon }`.
  `live: true` means the departure is currently matched to a tracked vehicle.

## 6. Dashboard card example

A Markdown card that renders the next 5 departures (replace the entity id with
your stop's entity):

```yaml
type: markdown
content: >
  ## {{ state_attr('sensor.warsaw_7009_01', 'stop_name') }}
  {% for d in state_attr('sensor.warsaw_7009_01', 'departures') %}
  **{{ d.line }}** → {{ d.direction }} — {{ d.time }} ({{ d.minutes }} min){% if d.live %} 🟢{% endif %}
  {% endfor %}
```

Or a plain **Entities** card showing the "minutes to next" value:

```yaml
type: entities
title: My stops
entities:
  - entity: sensor.warsaw_7009_01
  - entity: sensor.warsaw_7013_02
```

## 7. Notes & limitations

- The ZTM API exposes timetables for the **current day only**; lines that do not
  run today will have no departures.
- The GPS overlay is best-effort — a scheduled departure is flagged `live` only
  when a vehicle reporting the same line **and brigade** is currently online. Both
  the bus and the tram feed are checked, so a stop served by both is fully covered.
- Data © City of Warsaw, provided under Creative Commons Attribution.
