"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
let CONFIG = null;

async function init() {
  CONFIG = await fetch("/api/config").then((r) => r.json());
  $("#dep-line").textContent =
    `Departure base: ${CONFIG.departure} — ${CONFIG.departure_name} · ${CONFIG.cruise_kt} kt · timeline ${CONFIG.timeline_hours} h`;
  $("#dep").value = CONFIG.departure;
  $("#radius").value = CONFIG.default_radius_nm;
  $("#radius").max = CONFIG.max_radius_nm;

  const manual = ["night_operations", "single_pilot_ifr_no_autopilot", "terrain_critical"];
  $("#threats-list").innerHTML = CONFIG.major_threats
    .filter((t) => manual.includes(t))
    .map((t) => `<label><input type="checkbox" class="threat" value="${t}"> ${labelOf(t)}</label>`)
    .join("");

  wire();
}

const labelOf = (s) => s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

function wire() {
  $("#radius").addEventListener("input", (e) => ($("#radius-out").textContent = `${e.target.value} nm`));
  $$(".gate").forEach((c) => c.addEventListener("change", updateGate));
  $("#mode").addEventListener("change", (e) => {
    const n = $$(".threat").find((c) => c.value === "night_operations");
    if (n) n.checked = e.target.value === "night";
  });
  $$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
  $("#run-route").addEventListener("click", runRoute);
  $("#run-discovery").addEventListener("click", runDiscovery);
  autocomplete("dep", "dep-list");
  autocomplete("dest", "dest-list");
}

function updateGate() {
  $("#gate-banner").classList.toggle("hidden", !$$(".gate").some((c) => c.checked));
}
function switchTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $("#tab-route").classList.toggle("hidden", name !== "route");
  $("#tab-discovery").classList.toggle("hidden", name !== "discovery");
}
const threatsParam = () => $$(".threat").filter((c) => c.checked).map((c) => c.value).join(",");

// ---------- Autocomplete ----------
function autocomplete(inputId, listId) {
  const input = document.getElementById(inputId);
  const list = document.getElementById(listId);
  let timer = null;

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) return hide();
    timer = setTimeout(async () => {
      const items = await fetch(`/api/airports/search?q=${encodeURIComponent(q)}`).then((r) => r.json());
      if (!items.length) return hide();
      list.innerHTML = items
        .map((a) => `<div class="ac-item" data-id="${a.ident}"><span class="id">${a.ident}</span> <span class="nm">${a.name}${a.municipality ? " · " + a.municipality : ""}</span></div>`)
        .join("");
      list.classList.remove("hidden");
      $$(`#${listId} .ac-item`).forEach((el) =>
        el.addEventListener("click", () => { input.value = el.dataset.id; hide(); })
      );
    }, 180);
  });
  input.addEventListener("blur", () => setTimeout(hide, 200));
  function hide() { list.classList.add("hidden"); list.innerHTML = ""; }
}

// ---------- Route ----------
async function runRoute() {
  const dep = $("#dep").value.trim().toUpperCase();
  const dest = $("#dest").value.trim().toUpperCase();
  if (!dest) { $("#route-verdict").innerHTML = `<div class="empty">Enter a destination.</div>`; return; }
  const btn = $("#run-route");
  btn.disabled = true; btn.textContent = "Pulling data…";
  clearRoute();
  try {
    const params = new URLSearchParams({ dep, dest, mode: $("#mode").value, threats: threatsParam() });
    const res = await fetch(`/api/route?${params}`);
    if (!res.ok) { $("#route-verdict").innerHTML = `<div class="empty">Unknown departure or destination.</div>`; return; }
    renderRoute(await res.json());
  } catch (e) {
    $("#route-verdict").innerHTML = `<div class="empty">Error: ${e}</div>`;
  } finally {
    btn.disabled = false; btn.textContent = "Assess route";
  }
}

function clearRoute() {
  ["route-verdict", "route-summary", "route-endpoints", "route-windows", "route-timeline"].forEach((id) => ($("#" + id).innerHTML = ""));
}

function renderRoute(r) {
  const v = r.verdict_now;
  $("#route-verdict").innerHTML =
    `<div class="verdict-banner ${cls(v)}">${r.departure.airport.ident} → ${r.destination.airport.ident}: ${v} now
      <small>${r.reasons_now.length ? r.reasons_now.join(" · ") : "Within personal limits at both ends."}</small></div>`;

  const alt = r.altitude;
  $("#route-summary").innerHTML = `<div class="panel meta">
      <span>📏 ${r.distance_nm} nm · ${fmtDir(r.bearing_true)}°T</span>
      <span>⏱ ${r.flight_time_hr} h</span>
      ${alt ? `<span>⬆ Best alt ${alt.altitude_ft} ft · GS ${alt.groundspeed_kt} kt (${alt.headwind_kt >= 0 ? "head" : "tail"}wind ${Math.abs(alt.headwind_kt)} kt)</span>` : ""}
      ${alt && alt.levels.length ? `<span>Winds aloft: ${alt.levels.map((l) => `${l.altitude_ft}ft ${fmtDir(l.direction_true)}/${Math.round(l.speed_kt)}`).join(" · ")}</span>` : ""}
      ${r.sigmets.length ? `<span>⚠️ ${r.sigmets.length} SIGMET</span>` : ""}
    </div>`;

  $("#route-endpoints").innerHTML = endpointCard(r.departure, "Departure") + endpointCard(r.destination, "Destination");

  if (r.best_windows.length) {
    $("#route-windows").innerHTML = `<div class="timeline-wrap"><h3>Best windows (next ${CONFIG.timeline_hours} h)</h3>` +
      r.best_windows.map((w) => `<div class="window-card">🟢 <strong>${fmtRange(w.start, w.end)}</strong> — ${w.summary}</div>`).join("") +
      `</div>`;
  } else {
    $("#route-windows").innerHTML = `<div class="timeline-wrap"><div class="empty">No clearly favourable window in the next ${CONFIG.timeline_hours} h.</div></div>`;
  }

  renderTimeline(r.timeline, r.best_windows);
}

function endpointCard(a, role) {
  const w = a.weather || {};
  const rw = a.best_runway;
  const wind = w.wind_kt != null
    ? `${fmtDir(w.wind_dir_true)}/${Math.round(w.wind_kt)}${w.gust_kt ? "G" + Math.round(w.gust_kt) : ""} kt`
    : "—";
  return `<div class="card ${cls(a.verdict)}">
    <div class="card-head"><h3>${role}: ${a.airport.ident}</h3><span class="badge ${cls(a.verdict)}">${a.verdict}</span></div>
    <div class="meta">
      <span>${srcChip(w.source)}${w.as_of ? " " + w.as_of : ""}</span>
      <span>💨 ${wind}</span>
      ${rw ? `<span>🛬 RWY ${rw.runway_ident} · xwind ${rw.crosswind_kt} kt${rw.crosswind_kt_gust ? " (gust " + rw.crosswind_kt_gust + ")" : ""}</span>` : ""}
      ${w.ceiling_agl_ft != null ? `<span>☁ ${Math.round(w.ceiling_agl_ft)} ft</span>` : ""}
      ${w.visibility_sm != null ? `<span>👁 ${w.visibility_sm} SM</span>` : ""}
      <span>📋 ${a.notam_count} NOTAM</span>
      ${w.model_vs_obs_wind_kt != null ? `<span title="model minus observed wind">Δmodel ${w.model_vs_obs_wind_kt > 0 ? "+" : ""}${w.model_vs_obs_wind_kt} kt</span>` : ""}
    </div>
    ${a.reasons.length ? `<ul class="reasons">${a.reasons.map((x) => `<li>${x}</li>`).join("")}</ul>` : ""}
    ${w.raw_metar ? `<div class="raw">METAR ${w.raw_metar}</div>` : ""}
    ${w.raw_taf ? `<div class="raw">TAF ${w.raw_taf}</div>` : ""}
  </div>`;
}

function renderTimeline(timeline, windows) {
  if (!timeline.length) { $("#route-timeline").innerHTML = ""; return; }
  const inWindow = (t) => windows.some((w) => t >= w.start && t <= w.end);
  const byDay = {};
  timeline.forEach((h) => { (byDay[h.time.slice(0, 10)] ||= []).push(h); });

  let html = `<div class="timeline-wrap"><h3>Hour-by-hour (worse of both ends)</h3>
    <div class="legend"><span class="go">GO</span><span class="mit">MITIGATE</span><span class="nogo">NO-GO</span><span>· dimmed = night · outlined = best window</span></div>`;
  for (const day of Object.keys(byDay).sort()) {
    const label = new Date(day + "T12:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
    html += `<div class="tl-day">${label}</div><div class="tl-row">`;
    for (const h of byDay[day]) {
      const hour = h.time.slice(11, 13);
      const title = [
        `${h.time.replace("T", " ")}  ${h.verdict}`,
        h.wind_kt != null ? `wind ${fmtDir(h.wind_dir_true)}/${Math.round(h.wind_kt)}${h.gust_kt ? "G" + Math.round(h.gust_kt) : ""} kt` : "",
        h.crosswind_kt != null ? `xwind ${h.crosswind_kt} kt` : "",
        h.ceiling_agl_ft != null ? `ceil ${Math.round(h.ceiling_agl_ft)} ft` : "",
        h.visibility_sm != null ? `vis ${h.visibility_sm} SM` : "",
        h.hazards.length ? "hazards: " + h.hazards.join(",") : "",
        `[${h.source}]`,
        ...h.reasons,
      ].filter(Boolean).join("\n");
      const klass = `${cls(h.verdict)}${h.daylight ? "" : " night"}${inWindow(h.time) ? " best" : ""}`;
      html += `<div class="tl-cell ${klass}" title="${title.replace(/"/g, "'")}"><span class="tl-hour">${hour}</span></div>`;
    }
    html += `</div>`;
  }
  html += `</div>`;
  $("#route-timeline").innerHTML = html;
}

// ---------- Discovery ----------
async function runDiscovery() {
  const btn = $("#run-discovery");
  btn.disabled = true; btn.textContent = "Checking…";
  $("#discovery-results").innerHTML = "";
  try {
    const params = new URLSearchParams({ radius: $("#radius").value, mode: $("#mode").value, threats: threatsParam() });
    const data = await fetch(`/api/suggest?${params}`).then((r) => r.json());
    $("#discovery-results").innerHTML = data.length
      ? data.map(discoveryCard).join("")
      : `<p class="empty">No airports within radius.</p>`;
  } catch (e) {
    $("#discovery-results").innerHTML = `<p class="empty">Error: ${e}</p>`;
  } finally {
    btn.disabled = false; btn.textContent = "Find flights now";
  }
}

function discoveryCard(a) {
  const w = a.weather || {}, rw = a.best_runway;
  const wind = w.wind_kt != null ? `${fmtDir(w.wind_dir_true)}/${Math.round(w.wind_kt)}${w.gust_kt ? "G" + Math.round(w.gust_kt) : ""} kt` : "—";
  return `<div class="card ${cls(a.verdict)}">
    <div class="card-head"><h3>${a.airport.ident} · ${a.airport.name}</h3><span class="badge ${cls(a.verdict)}">${a.verdict}</span></div>
    <div class="meta">
      <span>${a.distance_nm} nm · ${fmtDir(a.bearing_true)}°T</span>
      <span>⏱ ${a.flight_time_hr} h</span>
      <span>${srcChip(w.source)}</span>
      <span>💨 ${wind}</span>
      ${rw ? `<span>🛬 RWY ${rw.runway_ident} · xwind ${rw.crosswind_kt} kt</span>` : ""}
      <span>📋 ${a.notam_count} NOTAM</span>
    </div>
    ${a.reasons.length ? `<ul class="reasons">${a.reasons.map((x) => `<li>${x}</li>`).join("")}</ul>` : ""}
    ${w.raw_metar ? `<div class="raw">METAR ${w.raw_metar}</div>` : ""}
  </div>`;
}

// ---------- helpers ----------
const cls = (v) => String(v).replace("-", "");        // "NO-GO" -> "NOGO"
const fmtDir = (d) => (d == null ? "—" : String(Math.round(d)).padStart(3, "0"));
function srcChip(source) {
  if (!source || source === "—") return `<span class="src">—</span>`;
  const k = { Observed: "OBSERVED", TAF: "TAF", HRDPS: "HRDPS" }[source] || "";
  return `<span class="src ${k}">${source}</span>`;
}
function fmtRange(a, b) {
  const f = (t) => new Date(t).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
  return `${f(a)} → ${f(b)}`;
}

init();
