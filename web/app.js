"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
let CONFIG = null;

// ---------- Pilot profile (per-browser, persists across sessions) ----------
const LS_KEY = "minima.profile.v1";
const LEGACY_MIN_KEY = "minima.minimums.v1";
// PROFILE: { base, minimums, conservatism }. minimums===null means
// "using built-in defaults". Sent to the backend via prefs / base / threats.
let PROFILE = { base: null, minimums: null, conservatism: null };

// Editable numeric leaves: {group, key, id, label, unit, grp(container), min, max, step}.
// group+key must match data/limits.yaml exactly so the backend merge accepts them.
const MIN_FIELDS = [
  { group: "wind", key: "sustained_max_kt",    id: "set-wind-sustained",    label: "Sustained wind",     unit: "kt", grp: "grp-wind",        min: 1,   max: 60,    step: 1   },
  { group: "wind", key: "gust_spread_max_kt",  id: "set-wind-gust",         label: "Gust spread",        unit: "kt", grp: "grp-wind",        min: 1,   max: 40,    step: 1   },
  { group: "wind", key: "crosswind_max_kt",    id: "set-wind-xwind",        label: "Crosswind",          unit: "kt", grp: "grp-wind",        min: 1,   max: 40,    step: 1   },
  { group: "ceiling_agl_ft", key: "day_circuit",          id: "set-ceil-day-circuit",  label: "Day circuit",        unit: "ft", grp: "grp-ceiling",     min: 100, max: 15000, step: 100 },
  { group: "ceiling_agl_ft", key: "day_xc",               id: "set-ceil-day-xc",       label: "Day cross-country",  unit: "ft", grp: "grp-ceiling",     min: 100, max: 15000, step: 100 },
  { group: "ceiling_agl_ft", key: "night_circuit",        id: "set-ceil-night-circuit",label: "Night circuit",      unit: "ft", grp: "grp-ceiling",     min: 100, max: 15000, step: 100 },
  { group: "ceiling_agl_ft", key: "night_xc_cloud_base",  id: "set-ceil-night-xc",     label: "Night XC cloud base",unit: "ft", grp: "grp-ceiling",     min: 100, max: 15000, step: 100 },
  { group: "visibility_sm",  key: "day_circuit",          id: "set-vis-day-circuit",   label: "Day circuit",        unit: "SM", grp: "grp-vis",         min: 0,   max: 20,    step: 1   },
  { group: "visibility_sm",  key: "day_xc",               id: "set-vis-day-xc",        label: "Day cross-country",  unit: "SM", grp: "grp-vis",         min: 0,   max: 20,    step: 1   },
  { group: "visibility_sm",  key: "night_circuit",        id: "set-vis-night-circuit", label: "Night circuit",      unit: "SM", grp: "grp-vis",         min: 0,   max: 20,    step: 1   },
  { group: "visibility_sm",  key: "night_xc",             id: "set-vis-night-xc",      label: "Night cross-country",unit: "SM", grp: "grp-vis",         min: 0,   max: 20,    step: 1   },
  { group: "ifr_ceiling_agl_ft", key: "day_xc",   id: "set-ifr-ceil-day",  label: "IFR day XC",   unit: "ft", grp: "grp-ifr-ceiling", min: 100, max: 15000, step: 100 },
  { group: "ifr_ceiling_agl_ft", key: "night_xc", id: "set-ifr-ceil-night",label: "IFR night XC", unit: "ft", grp: "grp-ifr-ceiling", min: 100, max: 15000, step: 100 },
  { group: "ifr_visibility_sm",  key: "day_xc",   id: "set-ifr-vis-day",   label: "IFR day XC",   unit: "SM", grp: "grp-ifr-vis",     min: 0,   max: 20,    step: 1   },
  { group: "ifr_visibility_sm",  key: "night_xc", id: "set-ifr-vis-night", label: "IFR night XC", unit: "SM", grp: "grp-ifr-vis",     min: 0,   max: 20,    step: 1   },
];

// ---- My Minimums: pilot fitness & external pressure item catalogue ----
const PILOT_FITNESS_ITEMS = [
  { id: "pf_illness",   label: "Illness or feeling unwell" },
  { id: "pf_meds",      label: "Medication affecting alertness" },
  { id: "pf_alcohol",   label: "Alcohol within 12 hours" },
  { id: "pf_fatigue",   label: "Significant fatigue / poor sleep" },
  { id: "pf_stress",    label: "High stress or emotional distraction" },
  { id: "pf_hydration", label: "Poor hydration or no food in several hours" },
  { id: "pf_blood",     label: "Blood donation within 24 hours" },
  { id: "pf_scuba",     label: "Scuba diving within 12 hours" },
  { id: "pf_co",        label: "Carbon monoxide exposure" },
  { id: "pf_injury",    label: "Physical injury / pain affecting controls" },
  { id: "pf_emotional", label: "Emotional distress (grief, anger, shock)" },
];
const EXTERNAL_PRESSURE_ITEMS = [
  { id: "ep_schedule",  label: "Schedule pressure" },
  { id: "ep_peers",     label: "Other pilots flying (peer pressure)" },
  { id: "ep_training",  label: "Training pressure or feeling behind" },
  { id: "ep_pax",       label: "Passengers waiting" },
  { id: "ep_gethome",   label: "Get-home-itis (must return today)" },
  { id: "ep_sunk",      label: "Sunk-cost pressure (already paid / committed)" },
  { id: "ep_wishful",   label: '"It will improve" wishful thinking' },
  { id: "ep_pride",     label: "Pride / reluctance to cancel" },
];
// ON by default — matches the original card exactly
const MM_DEFAULTS = new Set([
  "pf_illness","pf_meds","pf_alcohol","pf_fatigue","pf_stress","pf_hydration",
  "ep_schedule","ep_peers","ep_training","ep_pax",
]);

// ---- Threat mitigation reference (straight from the decision card) ----
const THREAT_MITIGATIONS = {
  night_operations: {
    label: "Night operations",
    items: ["Familiar airport and runway", "Stable VMC forecast", "Light winds expected", "Simple direct route", "Extra fuel margin"],
  },
  actual_imc: {
    label: "IMC / IFR",
    items: ["Stable weather system (not frontal)", "Precision approaches preferred", "Higher personal minimums", "Autopilot if available"],
  },
  strong_or_gusty_winds: {
    label: "Gusty winds",
    items: ["Favour runway aligned into wind", "Longer runway preferred", "Add half gust factor on final"],
  },
  moderate_turbulence_or_shear: {
    label: "Turbulence / wind shear",
    items: ["Expect airspeed changes — stay alert", "Avoid terrain rotor areas", "Slow toward manoeuvring speed"],
  },
  icing_potential: {
    label: "Icing risk",
    items: ["Know the freezing level", "Identify warm and cold layers", "Exit immediately — usually descend"],
  },
};

// ---------- Storage ----------
function loadProfile() {
  let p = null;
  try { p = JSON.parse(localStorage.getItem(LS_KEY) || "null"); } catch { p = null; }
  if (!p) {
    let legacy = null;
    try { legacy = JSON.parse(localStorage.getItem(LEGACY_MIN_KEY) || "null"); } catch { legacy = null; }
    if (legacy) p = { minimums: legacy };
  }
  PROFILE = {
    base: (p && p.base) || null,
    minimums: (p && p.minimums) || null,
    conservatism: (p && p.conservatism) || null,
  };
}

function saveProfile() {
  const out = {};
  if (PROFILE.base) out.base = PROFILE.base;
  if (PROFILE.minimums) out.minimums = PROFILE.minimums;
  if (PROFILE.conservatism && PROFILE.conservatism !== CONFIG.default_conservatism) out.conservatism = PROFILE.conservatism;
  if (Object.keys(out).length) localStorage.setItem(LS_KEY, JSON.stringify(out));
  else localStorage.removeItem(LS_KEY);
  localStorage.removeItem(LEGACY_MIN_KEY);
}

function loadEnabledMM() {
  try { const s = localStorage.getItem("fm_minimums_v1"); if (s) return new Set(JSON.parse(s)); } catch (_) {}
  return new Set(MM_DEFAULTS);
}
function saveEnabledMM(set) {
  try { localStorage.setItem("fm_minimums_v1", JSON.stringify([...set])); } catch (_) {}
}
let enabledMM = loadEnabledMM();

function loadRecencyMin() {
  try { const v = localStorage.getItem("fm_recency_min"); if (v !== null) return +v; } catch (_) {}
  return 5;
}
function saveRecencyMin(v) {
  try { localStorage.setItem("fm_recency_min", String(v)); } catch (_) {}
}

// ---------- Init ----------
async function init() {
  CONFIG = await fetch("/api/config").then((r) => r.json());
  $("#radius").value = CONFIG.default_radius_nm;
  $("#radius").max = CONFIG.max_radius_nm;

  loadProfile();
  renderExtraThreats();
  buildConservatism();
  renderMinSliders();
  renderRecencySlider();
  buildWxFlags();
  fillProfileForm();
  renderMinimums();
  renderMyMinimumsSettings();
  // Preflight self-assessment is a standing pre-check shown above the route/discovery
  // inputs — render it up front so it's done before any weather is pulled.
  renderSelfAssessment("route-self-check");
  renderSelfAssessment("discovery-self-check");
  $("#dep").value = baseIdent();
  wire();
  // Apply the initially-active tab so the per-flight controls start hidden on
  // the default My Minimums tab.
  switchTab(($$(".tab.active")[0] || {}).dataset?.tab || "settings");
  startClock();
}

// ---------- Zulu clock (header) ----------
// Format a Date as "YYYY-MM-DD HH:MM:SSZ" in UTC.
function fmtZulu(d) {
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ` +
         `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}Z`;
}
// Live current Zulu time, ticking every second.
function startClock() {
  const el = $("#current-time");
  const tick = () => { if (el) el.textContent = fmtZulu(new Date()); };
  tick();
  setInterval(tick, 1000);
}
// Freeze the "Data time" to now — call when a fresh assessment's data arrives.
function stampDataTime() {
  const el = $("#data-time");
  if (el) el.textContent = fmtZulu(new Date());
}

const baseIdent = () => PROFILE.base || CONFIG.departure;
const labelOf = (s) => s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

// ---------- Wire ----------
function wire() {
  $("#radius").addEventListener("input", (e) => ($("#radius-out").textContent = `${e.target.value} nm`));
  $("#f-time").addEventListener("input", (e) => ($("#f-time-out").textContent = +e.target.value ? `${e.target.value} min` : "Any"));
  makeDragOnly($("#radius")); makeDragOnly($("#f-time"));
  // Scope each seg-btn toggle to its own .seg group; re-render extra threats on IFR/VFR change.
  $$(".seg-btn").forEach((b) => b.addEventListener("click", () => {
    b.closest(".seg").querySelectorAll(".seg-btn").forEach((x) => x.classList.toggle("active", x === b));
    if (b.dataset.rules !== undefined) renderExtraThreats();
  }));
  $$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
  $("#run-route").addEventListener("click", runRoute);
  $("#run-discovery").addEventListener("click", runDiscovery);
  $("#save-minimums").addEventListener("click", saveMinimums);
  $("#reset-minimums").addEventListener("click", resetMinimums);
  // VFR/IFR tab on the minimums card swaps which weather-minimums set is shown.
  $$(".rule-tab").forEach((b) => b.addEventListener("click", () => {
    const rule = b.dataset.rule;
    $$(".rule-tab").forEach((x) => x.classList.toggle("active", x === b));
    $$(".rule-pane").forEach((p) => p.classList.toggle("hidden", p.dataset.rule !== rule));
    buildWxFlags();
  }));
  autocomplete("dep", "dep-list");
  autocomplete("dest", "dest-list");
  autocomplete("circ-aerodrome", "circ-list");
  autocomplete("set-base", "base-list");

  // Flight-type toggle: XC ↔ Circuits
  $$("[data-ftype]").forEach((b) => b.addEventListener("click", () => {
    $$("[data-ftype]").forEach((x) => x.classList.toggle("active", x === b));
    applyFlightType(b.dataset.ftype);
  }));
}

function currentFlightType() {
  return ($$("[data-ftype].active")[0] || {}).dataset?.ftype || "xc";
}

function applyFlightType(ftype) {
  const isCircuits = ftype === "circuits";
  $("#dep-row").classList.toggle("hidden", isCircuits);
  $("#dest-row").classList.toggle("hidden", isCircuits);
  $("#circ-row").classList.toggle("hidden", !isCircuits);
  if (isCircuits && !$("#circ-aerodrome").value) {
    $("#circ-aerodrome").value = $("#dep").value || baseIdent();
  }
  $("#run-route").textContent = isCircuits ? "Assess circuits" : "Assess route";
}

const currentMode = () => ($$(".seg-btn[data-mode]").find((b) => b.classList.contains("active")) || {}).dataset?.mode || "day";
const currentFlightRules = () => ($$(".seg-btn[data-rules]").find((b) => b.classList.contains("active")) || {}).dataset?.rules || "vfr";

function switchTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $("#tab-route").classList.toggle("hidden", name !== "route");
  $("#tab-discovery").classList.toggle("hidden", name !== "discovery");
  $("#tab-settings").classList.toggle("hidden", name !== "settings");
  // The per-flight controls (time of day, flight rules, extra threats) are
  // meaningless on the My Minimums tab — hide them there.
  $("#flight-controls").classList.toggle("hidden", name === "settings");
}

// This flight's threats = per-flight toggles + night.
function threatsParam() {
  const set = new Set();
  $$(".threat").filter((c) => c.checked).forEach((c) => set.add(c.value));
  if (currentMode() === "night") set.add("night_operations");
  return [...set].join(",");
}
// Backend prefs payload: custom minimums and/or a non-default conservatism preset.
function prefsParam() {
  const p = { ...(PROFILE.minimums || {}) };
  if (PROFILE.conservatism && PROFILE.conservatism !== CONFIG.default_conservatism) p.conservatism = PROFILE.conservatism;
  return Object.keys(p).length ? { prefs: JSON.stringify(p) } : {};
}

// Effective limits = defaults with the custom minimums merged over them.
function effectiveLimits() {
  const d = CONFIG.default_limits;
  const difr = CONFIG.default_ifr_minimums || {};
  const m = PROFILE.minimums || {};
  return {
    wind:             { ...d.wind,             ...(m.wind             || {}) },
    ceiling_agl_ft:  { ...d.ceiling_agl_ft,   ...(m.ceiling_agl_ft  || {}) },
    visibility_sm:   { ...d.visibility_sm,     ...(m.visibility_sm   || {}) },
    ifr_ceiling_agl_ft: { ...(difr.ceiling_agl_ft || {}), ...(m.ifr_ceiling_agl_ft || {}) },
    ifr_visibility_sm:  { ...(difr.visibility_sm   || {}), ...(m.ifr_visibility_sm  || {}) },
    weather_flags:   m.weather_flags || d.weather_flags,
    imc_as_threat:   (m.imc_as_threat !== undefined) ? m.imc_as_threat : !!difr.imc_as_threat,
  };
}

// ---------- Autocomplete ----------
function autocomplete(inputId, listId) {
  const input = document.getElementById(inputId), list = document.getElementById(listId);
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) return hide();
    timer = setTimeout(async () => {
      const items = await fetch(`/api/airports/search?q=${encodeURIComponent(q)}`).then((r) => r.json());
      if (!items.length) return hide();
      list.innerHTML = items.map((a) =>
        `<div class="ac-item" data-id="${a.ident}"><span class="id">${a.ident}</span> <span class="nm">${a.name}${a.municipality ? " · " + a.municipality : ""}</span></div>`).join("");
      list.classList.remove("hidden");
      $$(`#${listId} .ac-item`).forEach((el) => el.addEventListener("click", () => { input.value = el.dataset.id; hide(); }));
    }, 180);
  });
  input.addEventListener("blur", () => setTimeout(hide, 200));
  function hide() { list.classList.add("hidden"); list.innerHTML = ""; }
}

// ---------- GFA (graphical area forecast) ----------
let GFA = { region: null, products: {}, sub: null, frame: 0 };
const GFA_LABELS = { CLDWX: "Clouds & weather", TURBC: "Icing & turbulence", GFA: "GFA" };

function gfaSubs() {
  return Object.keys(GFA.products)
    .filter((s) => (GFA.products[s] || []).length)
    .sort((a, b) => (a === "CLDWX" ? -1 : b === "CLDWX" ? 1 : a.localeCompare(b)));
}
function gfaFrameLabel(f, i) {
  if (f && f.validity) {
    const d = new Date(f.validity);
    if (!isNaN(d)) return `${String(d.getUTCHours()).padStart(2, "0")}Z`;
  }
  return `#${i + 1}`;
}
function gfaFallback() {
  return `<div class="panel gfa-panel"><h3>GFA — graphical area forecast</h3>
    <p class="hint">Charts couldn't be loaded right now.
    <a href="https://plan.navcanada.ca/" target="_blank" rel="noopener">Open the GFA on NAV CANADA ↗</a></p></div>`;
}

async function loadGfa(dep, dest) {
  const host = $("#route-gfa");
  if (!host) return;
  host.innerHTML = `<div class="panel gfa-panel"><h3>GFA — graphical area forecast <span class="hint">loading…</span></h3></div>`;
  try {
    const params = new URLSearchParams({ dep, ...(dest ? { dest } : {}) });
    const data = await fetch(`/api/gfa?${params}`).then((r) => r.json());
    GFA = { region: data.region || null, products: data.products || {}, sub: null, frame: 0 };
    const subs = gfaSubs();
    if (!subs.length) { host.innerHTML = gfaFallback(); return; }
    GFA.sub = subs[0];
    drawGfa();
  } catch (e) {
    host.innerHTML = gfaFallback();
  }
}

function drawGfa() {
  const host = $("#route-gfa");
  const subs = gfaSubs();
  if (!subs.length) { host.innerHTML = gfaFallback(); return; }
  if (!subs.includes(GFA.sub)) GFA.sub = subs[0];
  const frames = GFA.products[GFA.sub] || [];
  if (GFA.frame >= frames.length) GFA.frame = 0;
  const fr = frames[GFA.frame] || {};
  const tabs = subs.map((s) => `<button class="gfa-tab ${s === GFA.sub ? "active" : ""}" data-sub="${s}">${GFA_LABELS[s] || s}</button>`).join("");
  const frameBtns = frames.length > 1
    ? `<div class="gfa-frames">${frames.map((f, i) => `<button class="gfa-frame ${i === GFA.frame ? "active" : ""}" data-frame="${i}">${gfaFrameLabel(f, i)}</button>`).join("")}</div>`
    : "";
  host.innerHTML = `<div class="panel gfa-panel">
    <div class="gfa-head">
      <h3>GFA — graphical area forecast${GFA.region ? ` <span class="hint">${escapeHtml(GFA.region)}</span>` : ""}</h3>
      <div class="gfa-tabs">${tabs}</div>
    </div>
    ${frameBtns}
    <a class="gfa-img-link" href="${fr.url || "https://plan.navcanada.ca/"}" target="_blank" rel="noopener">
      <img class="gfa-img" src="${fr.url || ""}" alt="GFA ${GFA.sub}" loading="lazy"
           onerror="this.closest('.gfa-panel').querySelector('.gfa-err').hidden=false" />
    </a>
    <p class="hint gfa-err" hidden>Chart image didn't load — <a href="https://plan.navcanada.ca/" target="_blank" rel="noopener">view on NAV CANADA ↗</a></p>
    <p class="hint gfa-cap">${fr.validity ? "Valid " + escapeHtml(String(fr.validity)) + " · " : ""}Source: NAV CANADA CFPS · tap chart to enlarge</p>
  </div>`;
  host.querySelectorAll(".gfa-tab").forEach((b) => b.addEventListener("click", () => { GFA.sub = b.dataset.sub; GFA.frame = 0; drawGfa(); }));
  host.querySelectorAll(".gfa-frame").forEach((b) => b.addEventListener("click", () => { GFA.frame = +b.dataset.frame; drawGfa(); }));
}

// ---------- Radar (Environment Canada GeoMet WMS, animated) ----------
const GEOMET_WMS = "https://geo.weather.gc.ca/geomet";
const RADAR_LABELS = { RADAR_1KM_RRAI: "Rain", RADAR_1KM_RSNO: "Snow" };
let RADAR = { map: null, wms: null, frames: [], idx: 0, layer: "RADAR_1KM_RRAI", timer: null };

const radarFallback = () => `<div class="panel radar-panel"><h3>Radar</h3>
  <p class="hint">Radar map couldn't load.
  <a href="https://weather.gc.ca/radar/index_e.html" target="_blank" rel="noopener">Open Environment Canada radar ↗</a></p></div>`;

function parseISODurationMin(s) {
  const m = /^P(?:T)?(?:(\d+)H)?(?:(\d+)M)?/.exec(s || "");
  return m ? (+(m[1] || 0)) * 60 + (+(m[2] || 0)) : 0;
}
function radarFrameTimes(caps) {
  if (caps.times && caps.times.length) return caps.times;
  const start = Date.parse(caps.start), end = Date.parse(caps.end);
  const stepMin = parseISODurationMin(caps.interval) || 6;
  if (isNaN(start) || isNaN(end)) return caps.default ? [caps.default] : [];
  const out = [];
  for (let t = start; t <= end && out.length < 40; t += stepMin * 60000) {
    out.push(new Date(t).toISOString().replace(/\.\d+Z$/, "Z"));
  }
  return out.length ? out : (caps.default ? [caps.default] : []);
}
const radarTimeLabel = (iso) => {
  const d = new Date(iso);
  return isNaN(d) ? iso : `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}Z`;
};

function stopRadar() {
  if (RADAR.timer) { clearInterval(RADAR.timer); RADAR.timer = null; }
  const b = $("#radar-play"); if (b) b.textContent = "▶";
}
function destroyRadar() {
  stopRadar();
  if (RADAR.map) { try { RADAR.map.remove(); } catch (_) {} }
  RADAR = { map: null, wms: null, frames: [], idx: 0, layer: RADAR.layer || "RADAR_1KM_RRAI", timer: null };
}

async function loadRadar(r) {
  const host = $("#route-radar");
  if (!host) return;
  if (typeof L === "undefined") { host.innerHTML = radarFallback(); return; }
  const dep = r.departure.airport, dest = r.destination.airport;
  const midLat = (dep.lat + dest.lat) / 2, midLon = (dep.lon + dest.lon) / 2;
  host.innerHTML = `<div class="panel radar-panel">
    <div class="radar-head">
      <h3>Radar <span class="hint">Environment Canada · last 3 h</span></h3>
      <div class="radar-types">
        ${Object.entries(RADAR_LABELS).map(([k, v]) =>
          `<button class="radar-type ${k === RADAR.layer ? "active" : ""}" data-layer="${k}">${v}</button>`).join("")}
      </div>
    </div>
    <div id="radar-map" class="radar-map"></div>
    <div class="radar-controls">
      <button id="radar-play" class="radar-play" title="Play / pause">▶</button>
      <input type="range" id="radar-slider" min="0" max="0" value="0" />
      <span id="radar-time" class="radar-time hint">—</span>
    </div>
  </div>`;
  destroyRadar();
  RADAR.map = L.map("radar-map", { scrollWheelZoom: false }).setView([midLat, midLon], 7);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    { maxZoom: 11, attribution: "© OpenStreetMap" }).addTo(RADAR.map);
  L.marker([dep.lat, dep.lon]).addTo(RADAR.map).bindTooltip(dep.ident, { permanent: false });
  if (dest.ident !== dep.ident) L.marker([dest.lat, dest.lon]).addTo(RADAR.map).bindTooltip(dest.ident);
  RADAR.wms = L.tileLayer.wms(GEOMET_WMS, {
    layers: RADAR.layer, format: "image/png", transparent: true, version: "1.3.0", opacity: 0.7,
  }).addTo(RADAR.map);
  setTimeout(() => RADAR.map && RADAR.map.invalidateSize(), 150);

  $$("#route-radar .radar-type").forEach((b) => b.addEventListener("click", () => {
    RADAR.layer = b.dataset.layer;
    $$("#route-radar .radar-type").forEach((x) => x.classList.toggle("active", x === b));
    if (RADAR.wms) RADAR.wms.setParams({ layers: RADAR.layer });
    loadRadarFrames();
  }));
  $("#radar-play").addEventListener("click", toggleRadarPlay);
  $("#radar-slider").addEventListener("input", (e) => { stopRadar(); setRadarFrame(+e.target.value); });
  await loadRadarFrames();
}

async function loadRadarFrames() {
  try {
    const caps = await fetch(`/api/radar_times?layer=${RADAR.layer}`).then((r) => r.json());
    if (caps.error) throw new Error(caps.error);
    RADAR.frames = radarFrameTimes(caps);
  } catch (e) { RADAR.frames = []; }
  const slider = $("#radar-slider");
  if (!RADAR.frames.length) { if ($("#radar-time")) $("#radar-time").textContent = "no radar frames"; return; }
  slider.max = String(RADAR.frames.length - 1);
  setRadarFrame(RADAR.frames.length - 1); // newest first
}

function setRadarFrame(i) {
  if (!RADAR.frames.length) return;
  RADAR.idx = Math.max(0, Math.min(i, RADAR.frames.length - 1));
  const t = RADAR.frames[RADAR.idx];
  if (RADAR.wms) RADAR.wms.setParams({ time: t });
  const slider = $("#radar-slider"); if (slider) slider.value = String(RADAR.idx);
  const lbl = $("#radar-time"); if (lbl) lbl.textContent = radarTimeLabel(t);
}

function toggleRadarPlay() {
  if (RADAR.timer) { stopRadar(); return; }
  if (RADAR.frames.length < 2) return;
  $("#radar-play").textContent = "⏸";
  RADAR.timer = setInterval(() => setRadarFrame((RADAR.idx + 1) % RADAR.frames.length), 700);
}

// ---------- Route ----------
async function runRoute() {
  if (currentFlightType() === "circuits") { runCircuits(); return; }
  const dep = $("#dep").value.trim().toUpperCase(), dest = $("#dest").value.trim().toUpperCase();
  if (!dest) { $("#route-verdict").innerHTML = `<div class="empty">Enter a destination.</div>`; return; }
  const btn = $("#run-route"); btn.disabled = true; btn.textContent = "Pulling data…";
  clearRoute();
  try {
    const params = new URLSearchParams({ dep, dest, mode: currentMode(), threats: threatsParam(), flight_rules: currentFlightRules(), ...prefsParam() });
    const res = await fetch(`/api/route?${params}`);
    if (!res.ok) { $("#route-verdict").innerHTML = `<div class="empty">Unknown departure or destination.</div>`; return; }
    renderRoute(await res.json());
    stampDataTime();
    loadGfa(dep, dest);
  } catch (e) {
    $("#route-verdict").innerHTML = `<div class="empty">Error: ${e}</div>`;
  } finally { btn.disabled = false; btn.textContent = "Assess route"; }
}

async function runCircuits() {
  const aerodrome = ($("#circ-aerodrome").value.trim() || baseIdent()).toUpperCase();
  const btn = $("#run-route"); btn.disabled = true; btn.textContent = "Pulling data…";
  clearRoute();
  try {
    const params = new URLSearchParams({ aerodrome, mode: currentMode(), threats: threatsParam(), flight_rules: currentFlightRules(), ...prefsParam() });
    const res = await fetch(`/api/circuits?${params}`);
    if (!res.ok) { $("#route-verdict").innerHTML = `<div class="empty">Unknown aerodrome.</div>`; return; }
    renderCircuits(await res.json());
    stampDataTime();
  } catch (e) {
    $("#route-verdict").innerHTML = `<div class="empty">Error: ${e}</div>`;
  } finally { btn.disabled = false; btn.textContent = "Assess circuits"; }
}

function renderCircuits(r) {
  const v = r.verdict;
  const frLabel = currentFlightRules() === "ifr" ? " · IFR" : " · VFR";
  $("#route-verdict").innerHTML = `<div class="verdict-banner ${cls(v)}">${r.airport.ident} circuits: ${v} now${frLabel}</div>`;
  const cond = r.limit_checks.filter((c) => c.group === "conditions");
  const wx = r.limit_checks.filter((c) => c.group === "weather");
  const n = r.threat_checks.filter((t) => t.present).length;
  const label = r.threat_result_label || stackWord(n);
  $("#route-checklist").innerHTML = `<div class="panel checklist">
    <div class="cl-group"><h3>Hard limits — conditions <span class="hint">(circuit minimums)</span></h3>${cond.map(rowCheck).join("")}</div>
    <div class="cl-group"><h3>Weather</h3>${wx.map(rowCheck).join("")}</div>
    <div class="cl-group"><h3>Two-trigger threat stack <span class="badge ${cls(labelVerdict(label))}">${n} present → ${label}</span></h3>${r.threat_checks.map(rowThreat).join("")}</div>
  </div>`;
  $("#route-mitigation").innerHTML = v === "MITIGATE" ? mitigationBlock(r.threat_checks) : "";
  $("#route-endpoints").innerHTML = endpointCard(r, "Aerodrome");
}

function clearRoute() {
  // route-self-check is a standing pre-check rendered on load — never cleared here,
  // so the pilot's ticked items survive a route assessment.
  if (typeof destroyRadar === "function") destroyRadar();  // tear down any live Leaflet map
  ["route-verdict", "route-checklist", "route-mitigation", "route-summary", "route-gfa", "route-radar", "route-endpoints", "route-windows", "route-timeline"]
    .forEach((id) => ($("#" + id).innerHTML = ""));
}

function renderRoute(r) {
  const v = r.verdict_now;
  const frLabel = currentFlightRules() === "ifr" ? " · IFR" : " · VFR";
  $("#route-verdict").innerHTML = `<div class="verdict-banner ${cls(v)}">${r.departure.airport.ident} → ${r.destination.airport.ident}: ${v} now${frLabel}</div>`;
  $("#route-checklist").innerHTML = checklist(r);
  $("#route-mitigation").innerHTML = v === "MITIGATE" ? mitigationBlock(r.threat_checks) : "";

  const alt = r.altitude;
  $("#route-summary").innerHTML = `<div class="panel meta">
      <span>📏 ${r.distance_nm} nm · course ${dirM(r.bearing_mag, r.bearing_true)}</span>
      <span>⏱ ${fmtHrMin(r.flight_time_hr)}</span>
      ${alt ? `<span>⬆ Best alt ${fmtFt(alt.altitude_ft)} · GS ${Math.round(alt.groundspeed_kt)} kt (${alt.headwind_kt >= 0 ? "head" : "tail"}wind ${Math.abs(alt.headwind_kt)} kt)</span>` : ""}
      ${r.enroute_ceiling_ft != null ? `<span>☁ Enroute ceiling ${fmtCeil(r.enroute_ceiling_ft)}</span>` : ""}
      ${r.cloud_at_cruise ? `<span class="warn">⚠️ Cloud below planned cruise altitude</span>` : ""}
      ${alt && alt.levels.length ? `<span>Winds aloft: ${alt.levels.map((l) => `${fmtFt(l.altitude_ft)} ${windDir(l.direction_mag, l.direction_true)}/${Math.round(l.speed_kt)}`).join(" · ")}</span>` : ""}
    </div>`;

  $("#route-summary").innerHTML += advisoriesBlock(r);
  $("#route-endpoints").innerHTML = endpointCard(r.departure, "Departure") + endpointCard(r.destination, "Destination");
  loadRadar(r);

  if (r.best_windows.length) {
    $("#route-windows").innerHTML = `<div class="timeline-wrap"><h3>Best windows (next ${CONFIG.timeline_hours} h) — wind, ceiling &amp; visibility</h3>` +
      r.best_windows.map((w) => `<div class="window-card">🟢 <strong>${fmtRange(w.start, w.end)}</strong> — ${w.summary}</div>`).join("") + `</div>`;
  } else {
    $("#route-windows").innerHTML = `<div class="timeline-wrap"><div class="empty">No clearly favourable window in the next ${CONFIG.timeline_hours} h.</div></div>`;
  }
  renderTimeline(r.timeline, r.best_windows);
}

function checklist(r) {
  const cond = r.limit_checks.filter((c) => c.group === "conditions");
  const wx = r.limit_checks.filter((c) => c.group === "weather");
  const n = r.threat_checks.filter((t) => t.present).length;
  const label = r.threat_result_label || stackWord(n);
  return `<div class="panel checklist">
    <div class="cl-group"><h3>Hard limits — conditions <span class="hint">(worst point on the route)</span></h3>${cond.map(rowCheck).join("")}</div>
    <div class="cl-group"><h3>Weather <span class="hint">(SIGMET/AIRMET/PIREP + model; ⚠ = review GFA)</span></h3>${wx.map(rowCheck).join("")}</div>
    <div class="cl-group"><h3>Two-trigger threat stack <span class="badge ${cls(labelVerdict(label))}">${n} present → ${label}</span></h3>${r.threat_checks.map(rowThreat).join("")}</div>
  </div>`;
}
const stackWord = (n) => ["Normal flight", "Mitigate carefully", "No-go solo", "No-go"][Math.min(n, 3)];
// Map the backend's result label to a badge colour (verdict driven by the
// pilot's conservatism preset, so we trust the label, not a local count).
const labelVerdict = (label) => /no-go/i.test(label) ? "NOGO" : /mitigate/i.test(label) ? "MITIGATE" : "GO";

function rowCheck(c) {
  const state = !c.applicable ? "na" : c.advisory ? "advisory" : c.passed ? "pass" : "fail";
  const mark = { pass: "✓", fail: "✗", advisory: "⚠", na: "–" }[state];
  const loc = c.location ? ` <span class="loc">@ ${c.location}</span>` : "";
  const src = c.source && c.source !== "—" ? ` <span class="src-mini">${c.source}</span>` : "";
  return `<div class="chk ${state}">
    <span class="mark">${mark}</span>
    <span class="lbl">${c.label}</span>
    <span class="act">${c.actual_text}${loc}${src}${c.advisory ? ` <a href="https://plan.navcanada.ca/" target="_blank" rel="noopener">GFA ↗</a>` : ""}</span>
    <span class="lim">${c.limit_text}</span></div>`;
}
function rowThreat(t) {
  return `<div class="chk ${t.present ? "fail" : "pass"}"><span class="mark">${t.present ? "✗" : "✓"}</span><span class="lbl">${t.label}</span><span class="act">${t.present ? "present" : "—"}</span><span class="lim"></span></div>`;
}

function endpointCard(a, role) {
  const w = a.weather || {};
  const issues = a.reasons || [];
  const wind = windStr(w);
  const to = a.best_takeoff, ld = a.best_landing;
  return `<div class="card ${cls(a.verdict)}">
    <div class="card-head"><h3>${role}: ${a.airport.ident} · ${a.airport.name}</h3><span class="badge ${cls(a.verdict)}">${a.verdict}</span></div>
    ${issues.length ? `<ul class="reasons nogo-reasons">${issues.map((x) => `<li>${x}</li>`).join("")}</ul>` : `<div class="ok-line">✓ Within personal limits</div>`}
    <div class="meta obs">
      <span>${srcChip(w.source)}${w.as_of ? " " + w.as_of : ""}</span>
      <span>💨 ${wind}</span>
      ${ceilChip(w)}
      ${w.visibility_sm != null ? `<span>👁 ${w.visibility_sm} SM</span>` : ""}
      ${notamToggle(a)}
    </div>
    <div class="rwy-lines">
      ${to ? `<div>🛫 <strong>Takeoff</strong>: RWY ${to.runway_ident} (${dirM(to.heading_mag, to.heading_true)})${dims(to)} · headwind ${Math.round(to.headwind_kt)} kt · xwind ${to.crosswind_kt} kt</div>` : ""}
      ${ld ? `<div>🛬 <strong>Landing</strong>: RWY ${ld.runway_ident} (${dirM(ld.heading_mag, ld.heading_true)})${dims(ld)} · xwind ${ld.crosswind_kt} kt${ld.crosswind_kt_gust ? ` (gust ${ld.crosswind_kt_gust})` : ""}</div>` : ""}
    </div>
    ${a.nearby_station ? nearbyBlock(a.nearby_station) : ""}
    ${trendsBlock(a)}
    ${runwaysBlock(a)}
    <div class="links">${linksHtml(a)}</div>
    <div class="notam-list hidden" id="notams-${a.airport.ident}">${notamItems(a)}</div>
    ${w.raw_metar ? `<div class="raw">METAR ${escapeHtml(w.raw_metar)}${ageChip(w.raw_metar)}</div>` : ""}
    ${w.raw_taf ? `<div class="raw">TAF ${w.raw_taf}</div>` : ""}
    ${metarHistory(a)}
  </div>`;
}

function trendsBlock(a) {
  const t = a.trends || [];
  if (!t.length) return "";
  return `<details class="trends" open><summary>Trends from recent METARs (${t.length})</summary>${t.map((x) => `<div class="trend">${x}</div>`).join("")}</details>`;
}
function nearbyBlock(n) {
  return `<div class="nearby"><span class="nlabel">Nearest reporting station</span> <strong>${n.ident}</strong>${n.name ? " · " + n.name : ""} — ${n.distance_nm} NM ${n.direction} of here
    ${n.metar ? `<div class="raw">METAR ${escapeHtml(n.metar)}${ageChip(n.metar)}</div>` : ""}${n.taf ? `<div class="raw">TAF ${escapeHtml(n.taf)}</div>` : ""}
    ${trendsBlock(n)}${metarHistoryList(n.metar_history)}</div>`;
}
function advisoriesBlock(r) {
  const items = [];
  (r.sigmets || []).forEach((t) => items.push(["SIGMET", t]));
  (r.airmets || []).forEach((t) => items.push(["AIRMET", t]));
  (r.pireps || []).forEach((t) => items.push(["PIREP", t]));
  if (!items.length) return `<div class="panel adv-none">No active SIGMET/AIRMET/PIREP on the route.</div>`;
  return `<details class="panel advisories" open><summary>Area advisories: ${items.length} <span class="hint">(check the altitudes — many apply only to higher levels)</span></summary>${items.map(([k, t]) => `<div class="adv"><span class="adv-k">${k}</span> ${escapeHtml(t)}</div>`).join("")}</details>`;
}
function metarHistory(a) {
  return metarHistoryList(a.metar_history);
}
function metarHistoryList(h) {
  if (!h || h.length < 2) return "";
  return `<details class="mhist"><summary>METAR history (${h.length})</summary>${h.map((m) => `<div class="raw">${escapeHtml(m)}${ageChip(m)}</div>`).join("")}</details>`;
}

function runwaysBlock(a) {
  const comps = a.runway_components || [];
  if (!comps.length) return `<div class="rwy-na">🛬 Runway data unavailable</div>`;
  const usable = comps.filter((c) => c.tailwind_kt <= 0).sort((x, y) => y.headwind_kt - x.headwind_kt);
  if (!usable.length) return "";
  const rows = usable.map((c) =>
    `<div class="rwy-comp">RWY ${c.ident} ${dirM(c.heading_mag, c.heading_true)} · ${dimsText(c)} · head ${Math.round(c.headwind_kt)} kt / xwind ${c.crosswind_kt} kt</div>`).join("");
  return `<details class="runways"><summary>Usable runways into wind: ${usable.length} <span class="hint">(no tailwind component)</span></summary>${rows}</details>`;
}

function linksHtml(a) {
  const out = [];
  if (a.cfs_url) out.push(`<a href="${a.cfs_url}" target="_blank" rel="noopener">CFS PDF ↗</a>`);
  if (a.info_url) out.push(`<a href="${a.info_url}" target="_blank" rel="noopener">Airport info (${a.info_label || "link"}) ↗</a>`);
  return out.join(" · ");
}

function notamToggle(a) {
  if (!a.notam_count) return `<span>📋 0 NOTAM</span>`;
  return `<span class="notam-btn" onclick="toggleNotams('${a.airport.ident}')">📋 ${a.notam_count} NOTAM ▾</span>`;
}
// Plain-language NOTAM timing: a colour-coded status + a one-line "when".
// Green = active now, amber = upcoming, grey = expired. Null when we can't
// parse any validity (so we don't mislabel it).
function notamMeta(n) {
  const start = n.start ? Date.parse(n.start) : null;
  const end = n.end ? Date.parse(n.end) : null;
  if (start === null && end === null && !n.permanent) return null;
  const now = Date.now();
  const mon = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const p = (x) => String(x).padStart(2, "0");
  const fmt = (ms) => { const d = new Date(ms);
    return `${d.getUTCDate()} ${mon[d.getUTCMonth()]} ${d.getUTCFullYear()}, ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}Z`; };
  if (start !== null && now < start)
    return { cls: "upcoming", label: "Upcoming", when: `Starts ${fmt(start)}` };
  if (end !== null && now > end)
    return { cls: "expired", label: "Expired", when: `Ended ${fmt(end)}` };
  if (n.permanent || end === null)
    return { cls: "active", label: "Active", when: "Permanent" };
  return { cls: "active", label: "Active", when: `Ends ${fmt(end)}${n.estimated ? " (est.)" : ""}` };
}
function notamItems(a) {
  return (a.notams || []).map((n) => {
    const m = notamMeta(n);
    const head = m
      ? `<span class="notam-status ${m.cls}">${m.label}</span><span class="notam-when">${m.when}</span>`
      : "";
    return `<div class="notam">
      <div class="notam-head"><a href="${n.url || "https://plan.navcanada.ca/"}" target="_blank" rel="noopener">${n.number || "NOTAM"} ↗</a>${head}</div>
      <div class="notam-text">${escapeHtml(n.text)}</div>
    </div>`;
  }).join("");
}
window.toggleNotams = (id) => $("#notams-" + id).classList.toggle("hidden");

function renderTimeline(timeline, windows) {
  if (!timeline.length) { $("#route-timeline").innerHTML = ""; return; }
  const inWindow = (t) => windows.some((w) => t >= w.start && t <= w.end);
  const byDay = {};
  timeline.forEach((h) => { (byDay[h.time.slice(0, 10)] ||= []).push(h); });
  let html = `<div class="timeline-wrap"><h3>Hour-by-hour (full decision card; worse of departure &amp; destination)</h3>
    <div class="legend"><span class="go">GO</span><span class="mit">MITIGATE</span><span class="nogo">NO-GO</span><span>· dimmed = night · outlined = best window · ⛈ storm 🧊 freezing ❄ snow 🌧 rain</span></div>`;
  for (const day of Object.keys(byDay).sort()) {
    const label = new Date(day + "T12:00").toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
    html += `<div class="tl-day">${label}</div><div class="tl-row">`;
    for (const h of byDay[day]) {
      const hour = h.time.slice(11, 13);
      const title = [
        `${h.time.replace("T", " ")}  ${h.verdict}`,
        h.wind_kt != null ? `wind ${windDir(h.wind_dir_mag, h.wind_dir_true)}/${Math.round(h.wind_kt)}${(h.gust_kt && h.gust_kt > h.wind_kt) ? "G" + Math.round(h.gust_kt) : ""} kt${h.wind_source ? " from " + h.wind_source : ""}` : "",
        h.crosswind_kt != null ? `xwind ${h.crosswind_kt} kt${h.crosswind_runway ? " on RWY " + h.crosswind_runway : ""}` : "",
        h.ceiling_agl_ft != null ? `ceiling ${(Math.round(h.ceiling_agl_ft / 100) * 100).toLocaleString()} ft` : "",
        h.visibility_sm != null ? `vis ${h.visibility_sm} SM` : "",
        precipText(h),
        h.hazards.length ? "hazards: " + h.hazards.join(",") : "",
        `[${h.source}]`, ...h.reasons,
      ].filter(Boolean).join("\n");
      const klass = `${cls(h.verdict)}${h.daylight ? "" : " night"}${inWindow(h.time) ? " best" : ""}`;
      const safe = title.replace(/"/g, "'");
      const wx = wxGlyph(h);
      html += `<div class="tl-cell ${klass}" title="${safe}" data-detail="${title.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;")}"><span class="tl-hour">${hour}</span>${wx ? `<span class="tl-wx">${wx}</span>` : ""}</div>`;
    }
    html += `</div>`;
  }
  html += `</div><div id="tl-detail" class="tl-detail" hidden></div>`;
  const root = $("#route-timeline");
  root.innerHTML = html;
  const panel = root.querySelector("#tl-detail");
  root.querySelectorAll(".tl-cell").forEach((cell) => {
    cell.addEventListener("click", () => {
      const prev = root.querySelector(".tl-cell.active");
      if (prev && prev !== cell) prev.classList.remove("active");
      const on = cell.classList.toggle("active");
      if (on) { panel.textContent = cell.dataset.detail; panel.hidden = false; }
      else { panel.hidden = true; }
    });
  });
}

// ---------- Discovery ----------
async function runDiscovery() {
  const btn = $("#run-discovery"); btn.disabled = true; btn.textContent = "Checking…";
  $("#discovery-results").innerHTML = "";
  try {
    const p = {
      radius: $("#radius").value, mode: currentMode(), threats: threatsParam(), base: baseIdent(),
      flight_rules: currentFlightRules(),
      surface: $("#f-surface").value, min_length_ft: $("#f-length").value, into_wind: $("#f-into-wind").checked,
      min_width_ft: $("#f-width").value, sort: $("#f-sort").value,
      max_crosswind: $("#f-xwind").checked, go_only: $("#f-go").checked,
    };
    const t = +$("#f-time").value;
    if (t > 0) p.max_time_min = t;
    Object.assign(p, prefsParam());
    const params = new URLSearchParams(p);
    const data = await fetch(`/api/suggest?${params}`).then((r) => r.json());
    $("#discovery-results").innerHTML = data.length ? data.map(discoveryCard).join("") : `<p class="empty">No airports match within radius + filters.</p>`;
    stampDataTime();
  } catch (e) { $("#discovery-results").innerHTML = `<p class="empty">Error: ${e}</p>`; }
  finally { btn.disabled = false; btn.textContent = "Find flights now"; }
}

function discoveryCard(a) {
  const w = a.weather || {}, rw = a.best_runway;
  return `<div class="card ${cls(a.verdict)}">
    <div class="card-head"><h3>${a.airport.ident} · ${a.airport.name}${a.access_note ? ` <span class="ppr">${a.access_note}</span>` : ""}</h3><span class="badge ${cls(a.verdict)}">${a.verdict}</span></div>
    <div class="meta">
      <span>${a.distance_nm} nm · ${dirM(null, a.bearing_true)}</span>
      <span>⏱ ${fmtHrMin(a.flight_time_hr)}</span>
      <span>${srcChip(w.source)}</span>
      <span>💨 ${windStr(w)}</span>
      ${ceilChip(w)}
      ${w.visibility_sm != null ? `<span>👁 ${w.visibility_sm} SM</span>` : ""}
      ${a.altitude ? `<span title="wind component along the leg at best altitude → groundspeed">${a.altitude.headwind_kt < 0 ? "🟢 tailwind" : "🔴 headwind"} ${Math.abs(Math.round(a.altitude.headwind_kt))} kt → GS ${Math.round(a.altitude.groundspeed_kt)} kt</span>` : ""}
    </div>
    ${rw ? `<div class="rwy-lines"><div>🛬 <strong>Best runway into wind</strong>: RWY ${rw.runway_ident} (${dirM(rw.heading_mag, rw.heading_true)})${dims(rw)} · xwind ${rw.crosswind_kt} kt · headwind ${Math.round(rw.headwind_kt)} kt</div></div>` : `<div class="rwy-na">🛬 Runway data unavailable</div>`}
    ${runwaysBlock(a)}
    <div class="meta">${notamToggle(a)}<span class="links">${linksHtml(a)}</span></div>
    ${a.reasons.length ? `<ul class="reasons">${a.reasons.map((x) => `<li>${x}</li>`).join("")}</ul>` : ""}
    ${w.raw_metar ? `<div class="raw">METAR ${escapeHtml(w.raw_metar)}${ageChip(w.raw_metar)}</div>` : ""}
    <div class="notam-list hidden" id="notams-${a.airport.ident}">${notamItems(a)}</div>
  </div>`;
}

// ---------- My Minimums & profile (settings) ----------
const WX_LABELS = {
  convective_sigmet: "Convective SIGMET", thunderstorm: "Thunderstorm (TS)",
  embedded_thunderstorm: "Embedded TS", freezing_rain: "Freezing rain (FZRA)",
  forecast_icing: "Forecast icing", moderate_turbulence_low: "Mod. turbulence < 3000 ft",
  low_level_wind_shear: "Low-level wind shear", widespread_ifr: "Widespread IMC",
};
const wxLabel = (f) => WX_LABELS[f] || labelOf(f);
const threatMeta = () => CONFIG.threats || [];
const threatsOfKind = (kind) => threatMeta().filter((t) => t.kind === kind);
const threatLabel = (key) => (threatMeta().find((t) => t.key === key) || {}).label || labelOf(key);

function buildWxFlags() {
  const ruleTabIfr = ($$(".rule-tab").find(b => b.classList.contains("active")) || {}).dataset?.rule === "ifr";
  const ifr = currentFlightRules() === "ifr" || ruleTabIfr;
  const flags = (CONFIG.weather_flag_options || []).filter((f) => !ifr || f !== "widespread_ifr");
  const prev = new Set($$(".wxflag").filter((c) => c.checked).map((c) => c.value));
  $("#wxflags").innerHTML = flags
    .map((f) => `<label class="control checkbox"><input type="checkbox" class="wxflag" value="${f}"${prev.has(f) ? " checked" : ""}> ${wxLabel(f)}</label>`)
    .join("");
}

// Per-flight extra threats (all kind:"per_flight" from the config), e.g.
// terrain-critical and unfamiliar/complex airspace. single_pilot_ifr_no_autopilot
// only applies when IFR is selected.
function renderExtraThreats() {
  const ifr = currentFlightRules() === "ifr";
  const items = threatsOfKind("per_flight")
    .map((t) => t.key)
    .filter((k) => ifr || k !== "single_pilot_ifr_no_autopilot");
  const wasChecked = new Set($$(".threat").filter((c) => c.checked).map((c) => c.value));
  $("#threats-list").innerHTML = items
    .map((t) => `<label><input type="checkbox" class="threat" value="${t}"${wasChecked.has(t) ? " checked" : ""}> ${threatLabel(t)}</label>`)
    .join("");
  buildWxFlags();
}

function buildConservatism() {
  const cur = PROFILE.conservatism || CONFIG.default_conservatism;
  const presets = CONFIG.conservatism_presets || [];
  $("#conservatism").innerHTML =
    `<div class="preset-row">` +
    presets.map((p) => `<label class="preset"><input type="radio" name="conservatism" value="${p.key}" ${p.key === cur ? "checked" : ""}> ${p.label}</label>`).join("") +
    `</div><p class="preset-desc hint" id="conservatism-desc"></p>`;
  const updateDesc = () => {
    const sel = ($$('input[name="conservatism"]').find((r) => r.checked) || {}).value || cur;
    const desc = (presets.find((p) => p.key === sel) || {}).description || "";
    $("#conservatism-desc").textContent = desc;
  };
  $$('input[name="conservatism"]').forEach((r) => r.addEventListener("change", updateDesc));
  updateDesc();
}

// On touch devices, a tap anywhere off the thumb does nothing at all — the
// value only moves when the pilot grabs the thumb and drags it. We compute the
// thumb centre from the current value and preventDefault() on any touch that
// lands on the bare track, so there's no jump and no bounce-back. Desktop mouse
// behaviour (click-to-jump) is untouched.
function makeDragOnly(el) {
  const THUMB = 18;            // approx native thumb width
  const GRAB = THUMB / 2 + 10; // forgiving grab radius around the small ball
  el.addEventListener('pointerdown', e => {
    if (e.pointerType !== 'touch') return;
    const r = el.getBoundingClientRect();
    const min = +el.min, max = +el.max;
    const frac = (el.value - min) / (max - min || 1);
    const center = r.left + THUMB / 2 + frac * (r.width - THUMB);
    if (Math.abs(e.clientX - center) > GRAB) e.preventDefault();
  }, { passive: false });
}

// Build a labelled slider per minimum, with a live value readout.
function renderMinSliders() {
  const byGrp = {};
  for (const f of MIN_FIELDS) (byGrp[f.grp] ||= []).push(f);
  for (const [grp, fields] of Object.entries(byGrp)) {
    const el = $("#" + grp);
    if (!el) continue;
    el.innerHTML = fields.map((f) => `
      <div class="sld">
        <span class="sld-label">${f.label}</span>
        <output class="sld-val" id="${f.id}-out"></output>
        <input type="range" id="${f.id}" min="${f.min}" max="${f.max}" step="${f.step}" />
      </div>`).join("");
  }
  for (const f of MIN_FIELDS) {
    const el = $("#" + f.id);
    if (el) {
      el.addEventListener("input", (e) => ($("#" + f.id + "-out").textContent = `${e.target.value} ${f.unit}`));
      makeDragOnly(el);
    }
  }
}

// Recent experience slider in the settings form (local only — not sent to backend).
function renderRecencySlider() {
  const container = $("#grp-recency");
  if (!container) return;
  const v = loadRecencyMin();
  container.innerHTML = `<div class="sld">
    <span class="sld-label">Min hours / 30 days</span>
    <output class="sld-val" id="set-recency-out">${v} hr</output>
    <input type="range" id="set-recency" min="1" max="20" step="1" value="${v}" />
  </div>`;
  const recencyEl = document.getElementById("set-recency");
  recencyEl.addEventListener("input", (e) => {
    const val = +e.target.value;
    document.getElementById("set-recency-out").textContent = `${val} hr`;
    saveRecencyMin(val);
    renderSelfAssessment("route-self-check");
    renderSelfAssessment("discovery-self-check");
  });
  makeDragOnly(recencyEl);
}

// Populate every control from the effective profile (defaults + custom).
function fillProfileForm() {
  $("#set-base").value = baseIdent();
  const eff = effectiveLimits();
  for (const f of MIN_FIELDS) {
    const grp = eff[f.group];
    if (!grp || grp[f.key] === undefined) continue;
    const el = $("#" + f.id);
    if (el) { el.value = grp[f.key]; ($("#" + f.id + "-out") || {}).textContent = `${grp[f.key]} ${f.unit}`; }
  }
  const active = new Set(eff.weather_flags);
  $$(".wxflag").forEach((c) => (c.checked = active.has(c.value)));
  const imc = $("#set-imc-threat");
  if (imc) imc.checked = !!eff.imc_as_threat;
}

function readProfileForm() {
  const d = CONFIG.default_limits;
  const difr = CONFIG.default_ifr_minimums || {};
  const mins = {};
  for (const f of MIN_FIELDS) {
    const el = $("#" + f.id);
    if (!el) continue;
    const v = parseFloat(el.value);
    if (!Number.isFinite(v)) continue;
    const grpDefault = f.group.startsWith("ifr_")
      ? (f.group === "ifr_ceiling_agl_ft" ? (difr.ceiling_agl_ft || {})[f.key] : (difr.visibility_sm || {})[f.key])
      : (d[f.group] || {})[f.key];
    if (grpDefault === undefined || v === grpDefault) continue;
    (mins[f.group] ||= {})[f.key] = v;
  }
  const checked = $$(".wxflag").filter((c) => c.checked).map((c) => c.value);
  if (checked.length !== d.weather_flags.length) mins.weather_flags = checked;

  // IMC-as-threat: only persist when it differs from the default (off).
  const imcEl = $("#set-imc-threat");
  if (imcEl && imcEl.checked !== !!difr.imc_as_threat) mins.imc_as_threat = imcEl.checked;

  const base = $("#set-base").value.trim().toUpperCase();
  const preset = ($$('input[name="conservatism"]').find((r) => r.checked) || {}).value || CONFIG.default_conservatism;
  PROFILE = {
    base: base && base !== CONFIG.departure ? base : null,
    minimums: Object.keys(mins).length ? mins : null,
    conservatism: preset,
  };
}

function saveMinimums() {
  readProfileForm();
  saveProfile();
  fillProfileForm();
  renderMinimums();
  $("#dep").value = baseIdent();
  flashStatus("Saved — every flight is now gated by your profile.");
}

function resetMinimums() {
  PROFILE = { base: null, minimums: null, conservatism: null };
  localStorage.removeItem(LS_KEY);
  localStorage.removeItem(LEGACY_MIN_KEY);
  buildConservatism();
  fillProfileForm();
  renderMinimums();
  renderRecencySlider();
  $("#dep").value = baseIdent();
  flashStatus("Reset to default profile.");
}

function flashStatus(msg) {
  const el = $("#minimums-status");
  el.textContent = msg;
  clearTimeout(flashStatus._t);
  flashStatus._t = setTimeout(() => (el.textContent = ""), 4000);
}

// Read-only "at a glance" summary; flags anything changed from the default.
function renderMinimums() {
  const eff = effectiveLimits(), d = CONFIG.default_limits;
  const custom = !!(PROFILE.minimums ||
    (PROFILE.conservatism && PROFILE.conservatism !== CONFIG.default_conservatism) || PROFILE.base);
  const row = (label, cur, def, unit, diff) => `<div class="chk ${diff ? "custom" : "pass"}">
      <span class="mark">${diff ? "★" : "–"}</span>
      <span class="lbl">${label}</span>
      <span class="act">${cur}${unit ? " " + unit : ""}</span>
      <span class="lim">${diff ? `default ${def}${unit ? " " + unit : ""}` : "default"}</span>
    </div>`;
  const baseRow = row("Home base", baseIdent(), CONFIG.departure, "", baseIdent() !== CONFIG.departure);
  const vfrFields = MIN_FIELDS.filter((f) => !f.group.startsWith("ifr_"));
  const minRows = vfrFields.map((f) => {
    const cur = (eff[f.group] || {})[f.key];
    const def = (d[f.group] || {})[f.key];
    return cur !== undefined ? row(`${f.label} (${f.unit})`, cur, def, f.unit, cur !== def) : "";
  }).join("");
  const off = d.weather_flags.filter((f) => !eff.weather_flags.includes(f));
  const flagsRow = row("Weather auto NO-GO", `${eff.weather_flags.length} of ${d.weather_flags.length} active`,
    off.length ? "removed: " + off.map(wxLabel).join(", ") : "all", "", off.length > 0);
  const curPreset = PROFILE.conservatism || CONFIG.default_conservatism;
  const presetLabel = (CONFIG.conservatism_presets.find((p) => p.key === curPreset) || {}).label || curPreset;
  const consRow = row("Conservatism", presetLabel, "Standard", "", curPreset !== CONFIG.default_conservatism);
  const imcRow = row("IMC as threat (IFR)", eff.imc_as_threat ? "on" : "off", "off", "", !!eff.imc_as_threat);
  $("#minimums-readout").innerHTML =
    `<div class="min-banner ${custom ? "custom" : ""}">${custom
      ? "Using your saved profile (★ = changed from default)."
      : "Using the built-in default profile."}</div>${baseRow}${minRows}${flagsRow}${imcRow}${consRow}`;
}

// Self-assessment configurator (fitness/pressure items and recency, stored locally).
function renderMyMinimumsSettings() {
  const body = $("#my-minimums-body");
  if (!body) return;
  const makeField = (items, legend) =>
    `<fieldset><legend>${legend}</legend>${items.map(({ id, label }) =>
      `<label><input type="checkbox" class="mm-toggle" value="${id}"${enabledMM.has(id) ? " checked" : ""} /> ${label}</label>`
    ).join("")}</fieldset>`;
  body.innerHTML =
    makeField(PILOT_FITNESS_ITEMS, "Pilot fitness — included in self-assessment if checked") +
    makeField(EXTERNAL_PRESSURE_ITEMS, "External pressures — included in self-assessment if checked");
  body.querySelectorAll(".mm-toggle").forEach((cb) => {
    cb.addEventListener("change", () => {
      if (cb.checked) enabledMM.add(cb.value); else enabledMM.delete(cb.value);
      saveEnabledMM(enabledMM);
      renderSelfAssessment("route-self-check");
      renderSelfAssessment("discovery-self-check");
    });
  });
}

// Render a self-assessment panel (fitness + pressure + recency) below results.
function renderSelfAssessment(containerId) {
  const container = document.getElementById(containerId);
  if (!container) return;
  const activePF = PILOT_FITNESS_ITEMS.filter((i) => enabledMM.has(i.id));
  const activeEP = EXTERNAL_PRESSURE_ITEMS.filter((i) => enabledMM.has(i.id));
  const recMin = loadRecencyMin();
  if (!activePF.length && !activeEP.length) { container.innerHTML = ""; return; }
  const bannerId = `gate-banner-${containerId}`;
  const gates = (items) => items.map(({ label }) =>
    `<label><input type="checkbox" class="gate" data-banner="${bannerId}" /> ${label}</label>`
  ).join("");
  const recencyGate = `<label><input type="checkbox" class="gate" data-banner="${bannerId}" /> Fewer than ${recMin} hours flown in last 30 days</label>`;
  container.innerHTML = `<div class="panel self-check-inline">
    <h3>Preflight self-assessment <span class="hint">(personal hard limits — check before pulling weather)</span></h3>
    <div class="checks-grid">
      ${activePF.length ? `<fieldset><legend>Pilot fitness — do not fly if any apply</legend>${gates(activePF)}${recencyGate}</fieldset>` : ""}
      ${activeEP.length ? `<fieldset><legend>External pressure — pause &amp; reassess</legend>${gates(activeEP)}</fieldset>` : ""}
    </div>
    <div id="${bannerId}" class="banner hidden">
      ⚠️ One or more personal factors checked — <strong>PAUSE and reassess.</strong>
      Would you be comfortable explaining this decision to your instructor?
    </div>
  </div>`;
  container.querySelectorAll(".gate").forEach((cb) => {
    cb.addEventListener("change", () => {
      const banner = document.getElementById(cb.dataset.banner);
      if (banner) banner.classList.toggle("hidden", !container.querySelectorAll(".gate:checked").length);
    });
  });
}

// Mitigation reference block — shown when verdict is MITIGATE.
function mitigationBlock(threatChecks) {
  const active = (threatChecks || []).filter((t) => t.present && THREAT_MITIGATIONS[t.key]);
  if (!active.length) return "";
  return `<div class="panel mit-block">
    <h3>Threat mitigation reference <span class="hint">(from your decision card — 1 threat present)</span></h3>
    <div class="mit-grid">${active.map(({ key }) => {
      const m = THREAT_MITIGATIONS[key];
      return `<div class="mit-section">
        <div class="mit-label">${m.label}</div>
        <ul class="mit-list">${m.items.map((i) => `<li>${i}</li>`).join("")}</ul>
      </div>`;
    }).join("")}</div>
  </div>`;
}

// ---------- helpers ----------
const cls = (v) => String(v).replace("-", "");
function dirM(magVal, trueVal) {
  if (magVal != null) return `${String(Math.round(magVal)).padStart(3, "0")}°M`;
  if (trueVal != null) return `${String(Math.round(trueVal)).padStart(3, "0")}°T`;
  return "—";
}
function gustStr(w) {
  return (w.gust_kt != null && w.wind_kt != null && w.gust_kt > w.wind_kt) ? "G" + Math.round(w.gust_kt) : "";
}
function windStr(w) {
  if (w.wind_kt == null) return "—";
  return `${windDir(w.wind_dir_mag, w.wind_dir_true)}/${Math.round(w.wind_kt)}${gustStr(w)} kt${blendChip(w)}`;
}
function blendChip(w) {
  if (!w.wind_ensemble_n) return "";
  const models = (w.wind_models || []).join(", ");
  return ` <span class="blend" title="${escapeHtml(models)}">${w.wind_ensemble_n}-model blend</span>`;
}
function round10(d) { if (d == null) return null; let r = Math.round(d / 10) * 10; if (r >= 360) r -= 360; return r; }
function windDir(magVal, trueVal) {
  if (magVal != null) return `${String(round10(magVal)).padStart(3, "0")}°M`;
  if (trueVal != null) return `${String(round10(trueVal)).padStart(3, "0")}°T`;
  return "—";
}
const fmtFt = (ft) => (ft == null ? "—" : `${Math.round(ft).toLocaleString()} ft`);
const fmtCeil = (ft) => (ft == null ? "—" : `${(Math.round(ft / 100) * 100).toLocaleString()} ft`);
function ceilChip(w) {
  if (w.ceiling_agl_ft != null) return `<span>☁ ${fmtCeil(w.ceiling_agl_ft)}</span>`;
  if (w.source === "Observed") return `<span>☁ no ceiling</span>`;
  return "";
}
function wxGlyph(h) {
  if ((h.hazards || []).includes("thunderstorm")) return "⛈";
  if ((h.hazards || []).includes("freezing_rain")) return "🧊";
  if (!h.precip) return "";
  if (h.precip.includes("snow")) return "❄";
  if (h.precip.includes("freezing")) return "🧊";
  return "🌧";
}
function precipText(h) {
  if (!h.precip) return "";
  return `precip: ${h.precip}${h.precip_mm != null ? ` (${h.precip_mm} mm)` : ""}`;
}
function metarAgeMin(raw) {
  const m = /\b(\d{2})(\d{2})(\d{2})Z\b/.exec(raw || "");
  if (!m) return null;
  const now = new Date();
  let d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), +m[1], +m[2], +m[3]));
  if (d - now > 3600 * 1000) d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, +m[1], +m[2], +m[3]));
  return Math.max(0, Math.round((now - d) / 60000));
}
function ageChip(raw) {
  const mins = metarAgeMin(raw);
  if (mins == null) return "";
  const txt = mins < 60 ? `${mins} min ago` : `${Math.floor(mins / 60)} h ${mins % 60} min ago`;
  const staleClass = mins > 90 ? " stale" : mins < 60 ? " fresh" : "";
  return ` <span class="age${staleClass}">${txt}</span>`;
}
function dimsText(c) {
  const l = c.length_ft ? Math.round(c.length_ft).toLocaleString() : "?";
  const wid = c.width_ft ? ` × ${Math.round(c.width_ft)} ft` : " ft";
  return `${l}${wid}${c.surface_label ? " " + c.surface_label : ""}`;
}
const dims = (rw) => (rw && rw.length_ft ? ` · ${dimsText(rw)}` : "");
function fmtHrMin(hr) {
  if (hr == null) return "—";
  const total = Math.round(hr * 60), h = Math.floor(total / 60), m = total % 60;
  return h ? `${h} h ${m} min` : `${m} min`;
}
function srcChip(source) {
  if (!source || source === "—") return `<span class="src">—</span>`;
  const k = { Observed: "OBSERVED", TAF: "TAF", HRDPS: "HRDPS" }[source] || "";
  return `<span class="src ${k}">${source}</span>`;
}
function fmtRange(a, b) {
  const f = (t) => new Date(t).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
  return `${f(a)} → ${f(b)}`;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

init();
