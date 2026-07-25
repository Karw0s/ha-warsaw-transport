// All fetch URLs are RELATIVE (no leading slash) so Home Assistant ingress
// path-rewriting works correctly.
"use strict";

const $ = (sel) => document.querySelector(sel);

async function api(path, options) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return resp.status === 204 ? null : resp.json();
}

function setStatus(text, isError = false) {
  const el = $("#status");
  el.textContent = text;
  el.classList.toggle("error", isError);
}

async function refreshHealth() {
  try {
    const h = await api("api/health");
    const bits = [];
    if (!h.api_key_set) bits.push("⚠ no API key set");
    bits.push(h.mqtt ? "MQTT connected" : "⚠ MQTT off");
    bits.push(`${h.saved_stops} stop(s)`);
    // Route plans are optional, so say which way arrivals are being estimated.
    const routes = h.routes || {};
    if (!routes.enabled) {
        bits.push("ETA: straight-line");
    } else if (routes.routes) {
        bits.push(`ETA: routes (${routes.routes})`);
    } else {
        bits.push("⚠ route plans not loaded");
    }
    setStatus(bits.join(" · "), !h.api_key_set);
  } catch (e) {
    setStatus("Cannot reach add-on backend", true);
  }
}

/**
 * Report whether the dashboard can actually load the card.
 *
 * `card_installed` alone is not enough: Home Assistant only starts serving
 * /local when <config>/www exists at *its* startup, so a freshly installed file
 * 404s until Core is restarted. This panel is served from the Home Assistant
 * origin through ingress, so fetching CARD_URL here sees exactly what the
 * dashboard would see, and the two signals together give an unambiguous verdict.
 */
async function refreshCardStatus() {
  const box = $("#card-status");
  const details = $("#card-details");
  let h;
  try {
    h = await api("api/health");
  } catch (e) {
    box.textContent = "Cannot reach add-on backend.";
    return;
  }

  if (!h.card_installed) {
    details.hidden = true;
    box.classList.add("error");
    box.innerHTML =
      `⚠ The card could not be installed into your Home Assistant config folder` +
      (h.ha_config_dir ? ` (${h.ha_config_dir}).` : ".") +
      ` <a href="api/card" download>Download the card</a> and copy it to` +
      ` <code>&lt;config&gt;/www/warsaw_transport/</code> yourself, then register the` +
      ` resource URL <code>${h.card_url}</code>.` +
      ` If you just updated the add-on, rebuild it (⋮ → Rebuild) so the config` +
      ` folder mapping takes effect.`;
    return;
  }

  $("#card-url").value = h.card_url;
  details.hidden = false;

  // Absolute path on purpose: this must hit Home Assistant's /local, not the
  // ingress-rewritten add-on path that every other request here uses.
  let reachable = false;
  try {
    const resp = await fetch(h.card_url, { method: "HEAD", cache: "no-store" });
    reachable = resp.ok;
  } catch (_) {
    reachable = false;
  }

  if (reachable) {
    box.classList.remove("error");
    box.innerHTML =
      `✅ Card installed and reachable. Add it to a dashboard with` +
      ` <strong>+ Add card → Warsaw Transport</strong>, after registering the` +
      ` resource URL below under <strong>Settings → Dashboards → ⋮ → Resources</strong>` +
      ` as a <strong>JavaScript module</strong>.`;
  } else {
    box.classList.add("error");
    box.innerHTML =
      `⚠ The card file is installed (<code>${h.card_path}</code>) but Home Assistant` +
      ` is not serving it yet.<br><strong>Restart Home Assistant Core once</strong>` +
      ` (Settings → System → ⋮ → Restart Home Assistant), then reload this page.` +
      ` Home Assistant only starts serving <code>/local/</code> when the` +
      ` <code>www</code> folder already exists at startup, and this add-on created it.`;
  }
}

function depRow(d) {
  const live = d.live ? '<span class="live-dot" title="Live GPS position">● live</span>' : "";
  const when = d.minutes <= 0 ? "now" : `${d.minutes} min`;
  // Arrival estimate from the vehicle's GPS, shown next to the scheduled time.
  // "~" marks an estimate made before a speed could be measured.
  const delay =
    d.delay_minutes === null || d.delay_minutes === undefined
      ? ""
      : ` (${d.delay_minutes > 0 ? "+" : ""}${d.delay_minutes})`;
  const away =
    d.stops_away === null || d.stops_away === undefined
      ? ""
      : ` <span class="away">${d.stops_away === 1 ? "next stop" : `${d.stops_away} stops`}</span>`;
  const eta = d.eta_time
    ? `<span class="eta" title="Estimated arrival${d.eta_source === "route" ? " (measured along the route)" : ""}">→ ${d.eta_source === "approx" ? "~" : ""}${d.eta_time}${delay}</span>${away}`
    : "";
  return `<div class="dep">
    <span class="line">${d.line}</span>
    <span class="dir">${d.direction || ""}</span>
    <span class="when">${d.time} · ${when}</span>
    ${eta}
    ${live}
  </div>`;
}

async function renderSaved() {
  const container = $("#saved-stops");
  let stops;
  try {
    stops = await api("api/stops");
  } catch (e) {
    container.innerHTML = `<div class="empty">Error loading stops: ${e.message}</div>`;
    return;
  }
  if (!stops.length) {
    container.innerHTML = `<div class="empty">No stops yet. Search above to add one.</div>`;
    return;
  }
  container.innerHTML = "";
  for (const s of stops) {
    const card = document.createElement("div");
    card.className = "row stop-card";
    card.innerHTML = `
      <div class="header">
        <div class="info">
          <div class="name">${s.name || s.id}</div>
          <div class="sub">stop ${s.busstop_id}/${s.pole}</div>
        </div>
        <button class="danger" data-remove="${s.id}">Remove</button>
      </div>
      <div class="departures" data-deps="${s.id}"><div class="empty">Loading…</div></div>`;
    container.appendChild(card);
    loadDepartures(s.id);
  }
}

async function loadDepartures(stopId) {
  const target = document.querySelector(`[data-deps="${stopId}"]`);
  if (!target) return;
  try {
    const data = await api(`api/departures/${stopId}`);
    if (!data.departures.length) {
      target.innerHTML = `<div class="empty">No upcoming departures.</div>`;
      return;
    }
    target.innerHTML = data.departures.map(depRow).join("");
  } catch (e) {
    target.innerHTML = `<div class="empty">Error: ${e.message}</div>`;
  }
}

async function doSearch(ev) {
  ev.preventDefault();
  const name = $("#search-input").value.trim();
  const box = $("#search-results");
  if (name.length < 2) return;
  box.innerHTML = `<div class="empty">Searching…</div>`;
  let results;
  try {
    results = await api(`api/stops/search?name=${encodeURIComponent(name)}`);
  } catch (e) {
    box.innerHTML = `<div class="empty">Search failed: ${e.message}</div>`;
    return;
  }
  if (!results.length) {
    box.innerHTML = `<div class="empty">No matches.</div>`;
    return;
  }
  box.innerHTML = "";
  for (const r of results) {
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = `
      <div class="info">
        <div class="name">${r.name || ""} <span class="sub">(${r.busstop_id}/${r.pole})</span></div>
      </div>
      <button class="secondary">Add</button>`;
    row.querySelector("button").addEventListener("click", async () => {
      try {
        await api("api/stops", {
          method: "POST",
          body: JSON.stringify({
            busstop_id: r.busstop_id,
            pole: r.pole,
            name: r.name,
            // Kept with the stop so the ETA can measure distance to it without
            // re-reading the (3 MB) city stop list.
            lat: r.lat,
            lon: r.lon,
          }),
        });
        box.innerHTML = "";
        $("#search-input").value = "";
        await renderSaved();
        await refreshHealth();
      } catch (e) {
        setStatus("Add failed: " + e.message, true);
      }
    });
    box.appendChild(row);
  }
}

document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-remove]");
  if (!btn) return;
  try {
    await api(`api/stops/${btn.dataset.remove}`, { method: "DELETE" });
    await renderSaved();
    await refreshHealth();
  } catch (e) {
    setStatus("Remove failed: " + e.message, true);
  }
});

$("#search-form").addEventListener("submit", doSearch);

$("#card-copy").addEventListener("click", async (ev) => {
  const input = $("#card-url");
  try {
    await navigator.clipboard.writeText(input.value);
  } catch (_) {
    // Clipboard API needs a secure context; selecting the text is a usable
    // fallback over plain http.
    input.select();
  }
  ev.target.textContent = "Copied";
  setTimeout(() => { ev.target.textContent = "Copy"; }, 1500);
});

refreshHealth();
refreshCardStatus();
renderSaved();
setInterval(renderSaved, 30000);
