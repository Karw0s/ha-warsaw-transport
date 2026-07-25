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

# Publish the Lovelace card into the HA config www/ folder so it can be
# registered as a dashboard resource (and is refreshed on every add-on update).
# Never fatal: a missing or read-only config mount must not stop the add-on.
#
# A candidate directory only counts as the Home Assistant config folder if it
# contains configuration.yaml. Merely existing is not enough: when the
# homeassistant_config mount is not in effect, mkdir/cp still succeed into a
# throwaway directory inside the container and we would report a successful
# install while nothing ever reaches Home Assistant.
CARD_SRC="/opt/warsaw_transport/lovelace/warsaw-transport-card.js"
CARD_DEST=""
HA_CONFIG=""
export WT_WWW_CREATED="false"

for candidate in /homeassistant /config; do
    if [ -f "${candidate}/configuration.yaml" ]; then
        HA_CONFIG="${candidate}"
        break
    fi
    bashio::log.debug "Not the Home Assistant config folder (no configuration.yaml): ${candidate}"
done

if [ -z "${HA_CONFIG}" ]; then
    bashio::log.warning "Could not find the Home Assistant config folder (probed /homeassistant and /config)."
    bashio::log.warning "The Lovelace card was NOT installed. Open the add-on's web panel for manual instructions."
    bashio::log.warning "If you just updated, rebuild the add-on (⋮ → Rebuild) so the config folder mapping takes effect."
else
    # Whether www/ already existed decides if Home Assistant needs a restart:
    # Core only starts serving /local when www/ exists at its startup.
    if [ -d "${HA_CONFIG}/www" ]; then
        WT_WWW_CREATED="false"
    else
        WT_WWW_CREATED="true"
    fi

    CARD_DEST="${HA_CONFIG}/www/warsaw_transport/warsaw-transport-card.js"
    if mkdir -p "${HA_CONFIG}/www/warsaw_transport" && cp "${CARD_SRC}" "${CARD_DEST}"; then
        # Trust the file on disk, not cp's exit status.
        CARD_BYTES="$(wc -c < "${CARD_DEST}" 2>/dev/null | tr -d ' ')"
        if [ -s "${CARD_DEST}" ]; then
            bashio::log.info "Lovelace card installed: ${CARD_DEST} (${CARD_BYTES} bytes)"
            if bashio::var.true "${WT_WWW_CREATED}"; then
                bashio::log.warning "Created ${HA_CONFIG}/www for the first time."
                bashio::log.warning "RESTART HOME ASSISTANT CORE once (Settings → System → Restart), otherwise"
                bashio::log.warning "/local/ is not served and the card will fail with a 404."
            fi
        else
            bashio::log.warning "Copied the Lovelace card but ${CARD_DEST} is empty."
            CARD_DEST=""
        fi
    else
        bashio::log.warning "Could not write the Lovelace card to ${CARD_DEST} (read-only config folder?)."
        CARD_DEST=""
    fi
fi

# Surfaced by the web panel so the user can see what actually happened.
export WT_HA_CONFIG_DIR="${HA_CONFIG}"
export WT_CARD_PATH="${CARD_DEST}"
export WT_CARD_SRC="${CARD_SRC}"

bashio::log.info "Starting Warsaw Public Transport add-on..."
cd /opt/warsaw_transport
exec python3 -m app.main
