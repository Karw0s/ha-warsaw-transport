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
    setStatus(bits.join(" · "), !h.api_key_set);
  } catch (e) {
    setStatus("Cannot reach add-on backend", true);
  }
}

function depRow(d) {
  const live = d.live ? '<span class="live-dot" title="Live GPS position">● live</span>' : "";
  const when = d.minutes <= 0 ? "now" : `${d.minutes} min`;
  return `<div class="dep">
    <span class="line">${d.line}</span>
    <span class="dir">${d.direction || ""}</span>
    <span class="when">${d.time} · ${when}</span>
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
          <div class="sub">${s.direction ? "→ " + s.direction + " · " : ""}stop ${s.busstop_id}/${s.pole}</div>
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
        <div class="sub">${r.direction ? "→ " + r.direction : ""}</div>
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
            direction: r.direction,
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

refreshHealth();
renderSaved();
setInterval(renderSaved, 30000);
