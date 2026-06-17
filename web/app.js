"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

let CONFIG = null;

async function init() {
  CONFIG = await fetch("/api/config").then((r) => r.json());
  $("#origin-line").textContent =
    `Home base: ${CONFIG.origin} — ${CONFIG.origin_name} · ${CONFIG.cruise_kt} kt cruise`;
  $("#radius").value = CONFIG.default_radius_nm;
  $("#radius").max = CONFIG.max_radius_nm;
  $("#radius-out").textContent = `${CONFIG.default_radius_nm} nm`;

  // Build manual-threat checklist (excludes auto-derived ones the engine handles)
  const manual = ["night_operations", "single_pilot_ifr_no_autopilot", "terrain_critical"];
  $("#threats-list").innerHTML = CONFIG.major_threats
    .filter((t) => manual.includes(t))
    .map((t) => `<label><input type="checkbox" class="threat" value="${t}"> ${label(t)}</label>`)
    .join("");

  wireUp();
}

function label(s) {
  return s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function wireUp() {
  $("#radius").addEventListener("input", (e) => {
    $("#radius-out").textContent = `${e.target.value} nm`;
  });
  $$(".gate").forEach((c) => c.addEventListener("change", updateGate));
  $("#mode").addEventListener("change", (e) => {
    const nightThreat = $$(".threat").find((c) => c.value === "night_operations");
    if (nightThreat) nightThreat.checked = e.target.value === "night";
  });
  $$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
  $("#run-now").addEventListener("click", runNow);
  $("#run-outlook").addEventListener("click", runOutlook);
}

function updateGate() {
  const any = $$(".gate").some((c) => c.checked);
  $("#gate-banner").classList.toggle("hidden", !any);
}

function switchTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $("#tab-now").classList.toggle("hidden", name !== "now");
  $("#tab-outlook").classList.toggle("hidden", name !== "outlook");
}

function threatsParam() {
  return $$(".threat").filter((c) => c.checked).map((c) => c.value).join(",");
}

// ---------- Fly now ----------
async function runNow() {
  const btn = $("#run-now");
  btn.disabled = true;
  btn.textContent = "Checking weather…";
  $("#now-results").innerHTML = "";
  try {
    const params = new URLSearchParams({
      radius: $("#radius").value, mode: $("#mode").value, threats: threatsParam(),
    });
    const data = await fetch(`/api/suggest?${params}`).then((r) => r.json());
    renderNow(data);
  } catch (e) {
    $("#now-results").innerHTML = `<p class="empty">Error loading suggestions: ${e}</p>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Find flights now";
  }
}

function renderNow(items) {
  if (!items.length) {
    $("#now-results").innerHTML = `<p class="empty">No airports within radius.</p>`;
    return;
  }
  $("#now-results").innerHTML = items.map(nowCard).join("");
}

function nowCard(a) {
  const w = a.weather || {};
  const rw = a.best_runway;
  const alt = a.altitude;
  const wind = w.wind_kt != null
    ? `${fmtDir(w.wind_dir_true)}/${Math.round(w.wind_kt)}${w.gust_kt ? "G" + Math.round(w.gust_kt) : ""} kt`
    : "—";
  return `
  <div class="card ${cls(a.verdict)}">
    <div class="card-head">
      <h3>${a.airport.ident} · ${a.airport.name}</h3>
      <span class="badge ${cls(a.verdict)}">${a.verdict}</span>
    </div>
    <div class="meta">
      <span>${a.distance_nm} nm · ${fmtDir(a.bearing_true)}°T</span>
      <span>⏱ ${a.flight_time_hr} h</span>
      <span>💨 ${wind}</span>
      ${rw ? `<span>🛬 RWY ${rw.runway_ident} · xwind ${rw.crosswind_kt} kt${rw.crosswind_kt_gust ? " (gust " + rw.crosswind_kt_gust + ")" : ""}</span>` : ""}
      ${alt ? `<span>⬆ ${alt.altitude_ft} ft · GS ${alt.groundspeed_kt} kt</span>` : ""}
      <span>📋 ${a.notam_count} NOTAM</span>
    </div>
    <ul class="reasons">${a.reasons.map((r) => `<li>${r}</li>`).join("")}</ul>
    ${w.raw_metar ? `<div class="raw">METAR ${w.raw_metar}</div>` : ""}
    ${w.raw_taf ? `<div class="raw">TAF ${w.raw_taf}</div>` : ""}
  </div>`;
}

// ---------- Outlook ----------
async function runOutlook() {
  const btn = $("#run-outlook");
  btn.disabled = true;
  btn.textContent = "Loading model…";
  $("#outlook-strip").innerHTML = "";
  $("#day-results").innerHTML = "";
  $("#day-heading").classList.add("hidden");
  try {
    const days = await fetch(`/api/outlook?days=${CONFIG.outlook_days}`).then((r) => r.json());
    renderStrip(days);
  } catch (e) {
    $("#outlook-strip").innerHTML = `<p class="empty">Error loading outlook: ${e}</p>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Load 10-day outlook";
  }
}

function renderStrip(days) {
  if (!days.length) {
    $("#outlook-strip").innerHTML = `<p class="empty">No forecast available.</p>`;
    return;
  }
  $("#outlook-strip").innerHTML = days.map((d) => {
    const dt = new Date(d.date + "T12:00");
    const dow = dt.toLocaleDateString(undefined, { weekday: "short" });
    const md = dt.toLocaleDateString(undefined, { month: "numeric", day: "numeric" });
    const press = d.pressure ? d.pressure.label : "";
    return `<div class="day ${d.rating}" data-date="${d.date}">
      <div class="dow">${dow}</div>
      <div class="small">${md}</div>
      <div class="small">${Math.round(d.surface_wind_kt || 0)} kt</div>
      <div class="small">${pressIcon(press)}</div>
    </div>`;
  }).join("");
  $$(".day").forEach((el) => el.addEventListener("click", () => selectDay(el.dataset.date, el)));
}

function pressIcon(label) {
  if (label === "High building") return "🔵 High";
  if (label === "Low approaching") return "🔴 Low";
  return "⚪ Steady";
}

async function selectDay(date, el) {
  $$(".day").forEach((d) => d.classList.remove("selected"));
  el.classList.add("selected");
  const heading = $("#day-heading");
  heading.classList.remove("hidden");
  heading.textContent = `Destinations for ${new Date(date + "T12:00").toDateString()}`;
  $("#day-results").innerHTML = `<p class="empty">Loading destinations…</p>`;
  const params = new URLSearchParams({ date, radius: $("#radius").value });
  const data = await fetch(`/api/day?${params}`).then((r) => r.json());
  if (!data.length) {
    $("#day-results").innerHTML = `<p class="empty">No destination forecasts for this day.</p>`;
    return;
  }
  $("#day-results").innerHTML = data.map(dayCard).join("");
}

function dayCard(a) {
  const rw = a.best_runway;
  const alt = a.altitude;
  const wind = a.surface_wind_kt != null
    ? `${fmtDir(a.surface_wind_dir_true)}/${Math.round(a.surface_wind_kt)}${a.surface_gust_kt ? "G" + Math.round(a.surface_gust_kt) : ""} kt`
    : "—";
  return `
  <div class="card ${a.rating}">
    <div class="card-head">
      <h3>${a.airport.ident} · ${a.airport.name}</h3>
      <span class="badge ${a.rating}">${a.rating}</span>
    </div>
    <div class="meta">
      <span>${a.distance_nm} nm · ${fmtDir(a.bearing_true)}°T</span>
      <span>⏱ ${a.flight_time_hr} h</span>
      <span>💨 ${wind}</span>
      ${rw ? `<span>🛬 RWY ${rw.runway_ident} · xwind ${rw.crosswind_kt} kt</span>` : ""}
      ${alt ? `<span>⬆ ${alt.altitude_ft} ft · GS ${alt.groundspeed_kt} kt</span>` : ""}
    </div>
    <ul class="reasons">${a.reasons.map((r) => `<li>${r}</li>`).join("")}</ul>
    ${alt && alt.levels.length ? `<div class="raw">Winds aloft: ${alt.levels.map((l) => `${l.altitude_ft}ft ${fmtDir(l.direction_true)}/${Math.round(l.speed_kt)}`).join(" · ")}</div>` : ""}
  </div>`;
}

// ---------- helpers ----------
function cls(v) { return v.replace("-", ""); } // "NO-GO" -> "NOGO"
function fmtDir(d) { return d == null ? "—" : String(Math.round(d)).padStart(3, "0"); }

init();
