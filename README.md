# Warsaw Public Transport — Home Assistant Add-on

A Home Assistant **add-on** that shows live Warsaw public-transport departures on your
dashboard. Pick bus/tram stops in the add-on's web panel and each one is published to
Home Assistant as a sensor (via MQTT Discovery) showing the **next 5 departures** relative
to the current time, using scheduled timetables augmented with a **live GPS overlay**.

Data source: [Warsaw Open Data API](https://api.um.warszawa.pl/) (Zarząd Transportu
Miejskiego). An API key is required — see the add-on docs.

## Installation

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**.
2. Open the **⋮** menu (top-right) → **Repositories**.
3. Add this repository URL:

   ```
   https://github.com/mkarwowski/ha-public-transport
   ```

4. Install the **Warsaw Public Transport** add-on that appears.
5. Follow the setup steps in [`warsaw_transport/DOCS.md`](warsaw_transport/DOCS.md).

## Requirements

- A Home Assistant install that supports add-ons (Home Assistant OS or Supervised).
- The **Mosquitto broker** add-on (or any MQTT broker configured in HA) — departures are
  published via MQTT Discovery.
- A free API key from <https://api.um.warszawa.pl/>.

## What's in this repo

| Path | Purpose |
|------|---------|
| `repository.yaml` | Registers this repo as a Home Assistant add-on store. |
| `warsaw_transport/` | The add-on itself (Docker container + FastAPI app). |
| `warsaw_transport/DOCS.md` | End-user setup and dashboard-card documentation. |
| `scripts/smoke.py` | Offline unit checks for the parsing/merge logic. |

## License

MIT. Transport data © City of Warsaw, provided under Creative Commons Attribution.
