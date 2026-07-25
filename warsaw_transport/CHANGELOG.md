# Changelog

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
