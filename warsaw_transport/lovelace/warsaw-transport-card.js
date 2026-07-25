// Warsaw Transport — custom Lovelace card.
//
// Renders one bus/tram stop in the style of Home Assistant's weather card: the
// next departure gets the big-number "current conditions" treatment, the ones
// after it sit below in a forecast-style row.
//
// All data comes from the MQTT sensor the add-on publishes
// (sensor.warsaw_<busstop>_<pole>) — its `departures` attribute holds exactly
// the same 5 rows the add-on's own web panel shows.
//
// Plain custom elements, no framework and no build step, matching app/static/.
"use strict";

const CARD_VERSION = "0.3.0";

const DEFAULTS = {
  icon: "mdi:bus",
  count: 5,
  show_stop_id: true,
};

// The published `minutes` ages between add-on polls, so the card recomputes the
// countdown from `timestamp`. Anything further out than this is treated as a
// parse failure (e.g. a timezone mismatch) and the published value is used.
const MAX_PLAUSIBLE_MINUTES = 180;

// `direction` and `stop_name` come straight from the city API, so escape
// everything before it goes anywhere near innerHTML.
function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/**
 * Minutes until a departure, recomputed from its ISO timestamp so the number
 * keeps ticking down between add-on polls.
 *
 * `timestamp` is naive local time (the add-on calls datetime.now()), which
 * `new Date()` parses in the browser's timezone — right as long as the browser
 * and the Home Assistant host agree. When it does not parse, or lands outside
 * the plausible range, fall back to the value the add-on published.
 */
function minutesUntil(departure, now) {
  const published = Number(departure.minutes);
  const parsed = Date.parse(departure.timestamp);
  if (Number.isNaN(parsed)) {
    return Number.isNaN(published) ? null : published;
  }
  const live = Math.floor((parsed - now) / 60000);
  if (live < 0 || live > MAX_PLAUSIBLE_MINUTES) {
    return Number.isNaN(published) ? null : published;
  }
  return live;
}

function formatMinutes(minutes) {
  if (minutes === null) return "—";
  return minutes <= 0 ? "now" : String(minutes);
}

const STYLES = `
  :host { display: block; }

  ha-card {
    padding: 16px;
  }

  .header {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .header ha-icon {
    color: var(--state-icon-color, var(--primary-color));
    --mdc-icon-size: 28px;
  }
  .title {
    min-width: 0;
    font-size: 1.15rem;
    font-weight: 500;
    color: var(--primary-text-color);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .subtitle {
    font-size: 0.8rem;
    font-weight: 400;
    color: var(--secondary-text-color);
  }

  .hero {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 20px 4px 16px;
  }
  .hero .big {
    font-size: 2.6rem;
    font-weight: 300;
    line-height: 1;
    color: var(--primary-text-color);
    white-space: nowrap;
  }
  .hero .unit {
    font-size: 1.1rem;
    color: var(--secondary-text-color);
    margin-left: 4px;
  }
  .hero .detail {
    flex: 1;
    min-width: 0;
  }
  .hero .dir {
    color: var(--primary-text-color);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .hero .at {
    font-size: 0.85rem;
    color: var(--secondary-text-color);
  }

  .line {
    display: inline-block;
    background: var(--primary-color);
    color: var(--text-primary-color, #fff);
    border-radius: 6px;
    padding: 2px 8px;
    font-weight: 700;
    min-width: 44px;
    text-align: center;
  }
  .line.big-badge {
    font-size: 1.1rem;
    padding: 6px 10px;
    min-width: 56px;
  }

  .live {
    color: var(--success-color, #2e7d32);
    font-size: 0.75rem;
    white-space: nowrap;
  }

  .upcoming {
    display: grid;
    gap: 8px;
    border-top: 1px solid var(--divider-color);
    padding-top: 12px;
    text-align: center;
  }
  .upcoming .slot {
    min-width: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
  }
  .upcoming .at {
    font-size: 0.8rem;
    color: var(--secondary-text-color);
  }
  .upcoming .in {
    font-size: 0.95rem;
    color: var(--primary-text-color);
  }

  .message {
    padding: 16px 4px 4px;
    color: var(--secondary-text-color);
  }
  .message.error {
    color: var(--error-color, #db4437);
  }
`;

class WarsawTransportCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = null;
    this._hass = null;
    this._timer = null;
  }

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("You need to define an entity (the stop's sensor).");
    }
    this._config = { ...DEFAULTS, ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 4;
  }

  // Re-render on a timer so the countdown stays accurate between add-on polls.
  connectedCallback() {
    if (this._timer === null) {
      this._timer = window.setInterval(() => this._render(), 30000);
    }
  }

  disconnectedCallback() {
    if (this._timer !== null) {
      window.clearInterval(this._timer);
      this._timer = null;
    }
  }

  static getConfigElement() {
    return document.createElement("warsaw-transport-card-editor");
  }

  static getStubConfig(hass) {
    const entity = Object.keys((hass && hass.states) || {}).find((id) => {
      if (!id.startsWith("sensor.warsaw_")) return false;
      return Array.isArray(hass.states[id].attributes.departures);
    });
    return { entity: entity || "" };
  }

  _shell(headerHtml, bodyHtml) {
    this.shadowRoot.innerHTML = `<style>${STYLES}</style>
      <ha-card>${headerHtml}${bodyHtml}</ha-card>`;
  }

  _message(text, isError = false) {
    return `<div class="message${isError ? " error" : ""}">${esc(text)}</div>`;
  }

  _renderHeader(name, subtitle) {
    return `<div class="header">
        <ha-icon icon="${esc(this._config.icon)}"></ha-icon>
        <div class="title">${esc(name)}${
          subtitle ? `<div class="subtitle">${esc(subtitle)}</div>` : ""
        }</div>
      </div>`;
  }

  _renderHero(departure, now) {
    const minutes = minutesUntil(departure, now);
    const live = departure.live
      ? `<span class="live" title="${esc(
          departure.vehicle
            ? `Live GPS position — vehicle ${departure.vehicle}`
            : "Live GPS position"
        )}">● live</span>`
      : "";
    const unit = minutes !== null && minutes > 0 ? `<span class="unit">min</span>` : "";
    return `<div class="hero">
        <span class="line big-badge">${esc(departure.line)}</span>
        <div class="detail">
          <div class="dir">${esc(departure.direction)}</div>
          <div class="at">${esc(departure.time)}${live ? ` · ${live}` : ""}</div>
        </div>
        <div class="big">${formatMinutes(minutes)}${unit}</div>
      </div>`;
  }

  _renderUpcoming(departures, now) {
    const slots = departures
      .map((d) => {
        const minutes = minutesUntil(d, now);
        const suffix = minutes !== null && minutes > 0 ? " min" : "";
        return `<div class="slot">
            <span class="line">${esc(d.line)}</span>
            <span class="at">${esc(d.time)}</span>
            <span class="in">${formatMinutes(minutes)}${suffix}${
              d.live ? ` <span class="live" title="Live GPS position">●</span>` : ""
            }</span>
          </div>`;
      })
      .join("");
    return `<div class="upcoming" style="grid-template-columns: repeat(${departures.length}, 1fr);">${slots}</div>`;
  }

  _render() {
    if (!this._config || !this._hass || !this.shadowRoot) return;

    const entityId = this._config.entity;
    const stateObj = this._hass.states[entityId];

    if (!stateObj) {
      this._shell(
        this._renderHeader(this._config.name || "Warsaw Transport", ""),
        this._message(`Entity ${entityId} not found.`, true)
      );
      return;
    }

    const attrs = stateObj.attributes || {};
    const name = this._config.name || attrs.stop_name || attrs.friendly_name || entityId;
    const subtitle =
      this._config.show_stop_id && attrs.busstop_id
        ? `stop ${attrs.busstop_id} / ${attrs.pole}`
        : "";
    const header = this._renderHeader(name, subtitle);

    if (stateObj.state === "unavailable" || stateObj.state === "unknown") {
      this._shell(header, this._message("Waiting for the add-on…"));
      return;
    }

    const count = Math.max(1, Number(this._config.count) || DEFAULTS.count);
    const departures = (Array.isArray(attrs.departures) ? attrs.departures : []).slice(0, count);

    if (!departures.length) {
      this._shell(header, this._message("No upcoming departures."));
      return;
    }

    const now = Date.now();
    const upcoming = departures.slice(1);
    this._shell(
      header,
      this._renderHero(departures[0], now) +
        (upcoming.length ? this._renderUpcoming(upcoming, now) : "")
    );
  }
}

const EDITOR_SCHEMA = [
  { name: "entity", required: true, selector: { entity: { domain: "sensor" } } },
  { name: "name", selector: { text: {} } },
  { name: "icon", selector: { icon: {} } },
  { name: "count", selector: { number: { min: 1, max: 5, mode: "box" } } },
  { name: "show_stop_id", selector: { boolean: {} } },
];

const EDITOR_LABELS = {
  entity: "Stop sensor (required)",
  name: "Name (defaults to the stop name)",
  icon: "Icon",
  count: "Departures to show",
  show_stop_id: "Show the stop/pole number",
};

class WarsawTransportCardEditor extends HTMLElement {
  constructor() {
    super();
    this._config = {};
    this._hass = null;
    this._form = null;
  }

  setConfig(config) {
    this._config = { ...DEFAULTS, ...config };
    this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  connectedCallback() {
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.schema = EDITOR_SCHEMA;
      this._form.computeLabel = (item) => EDITOR_LABELS[item.name] || item.name;
      this._form.addEventListener("value-changed", (ev) => {
        ev.stopPropagation();
        this.dispatchEvent(
          new CustomEvent("config-changed", {
            detail: { config: ev.detail.value },
            bubbles: true,
            composed: true,
          })
        );
      });
      this.appendChild(this._form);
    }
    this._update();
  }

  _update() {
    if (!this._form) return;
    if (this._hass) this._form.hass = this._hass;
    this._form.data = this._config;
  }
}

customElements.define("warsaw-transport-card", WarsawTransportCard);
customElements.define("warsaw-transport-card-editor", WarsawTransportCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "warsaw-transport-card",
  name: "Warsaw Transport",
  description: "Next departures from a Warsaw bus/tram stop.",
  preview: true,
  documentationURL: "https://github.com/Karw0s/ha-warsaw-transport",
});

console.info(`%c WARSAW-TRANSPORT-CARD %c ${CARD_VERSION} `,
  "color: white; background: #03a9f4; font-weight: 700;",
  "color: #03a9f4; background: white; font-weight: 700;");
