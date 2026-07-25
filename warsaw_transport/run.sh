#!/usr/bin/with-contenv bashio
# ==============================================================================
# Warsaw Public Transport add-on entrypoint.
# Reads add-on options + MQTT service credentials via bashio, exports them as
# environment variables, then launches the FastAPI/uvicorn application.
# ==============================================================================
set -e

export WT_API_KEY="$(bashio::config 'api_key')"
export WT_POLL_INTERVAL="$(bashio::config 'poll_interval')"
export WT_GPS_OVERLAY="$(bashio::config 'gps_overlay')"
export WT_LOG_LEVEL="$(bashio::config 'log_level')"

if bashio::config.is_empty 'api_key'; then
    bashio::log.warning "No API key set. Open the add-on configuration and paste your dane.um.warszawa.pl key."
fi

# MQTT credentials — prefer the broker offered by the Supervisor (mqtt:want).
if bashio::services.available "mqtt"; then
    export WT_MQTT_HOST="$(bashio::services 'mqtt' 'host')"
    export WT_MQTT_PORT="$(bashio::services 'mqtt' 'port')"
    export WT_MQTT_USER="$(bashio::services 'mqtt' 'username')"
    export WT_MQTT_PASSWORD="$(bashio::services 'mqtt' 'password')"
    bashio::log.info "Using Supervisor MQTT broker at ${WT_MQTT_HOST}:${WT_MQTT_PORT}"
else
    bashio::log.warning "No MQTT service available. Install/configure the Mosquitto broker add-on so departures can be published."
fi

export WT_DATA_DIR="/data"

bashio::log.info "Starting Warsaw Public Transport add-on..."
cd /opt/warsaw_transport
exec python3 -m app.main
