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
// Optional `hint` becomes the row's tooltip - for the few numbers whose label
// cannot carry their own meaning.
const MIN_FIELDS = [
  { group: "wind", key: "sustained_max_kt",    id: "set-wind-sustained",    label: "Sustained wind",     unit: "kt", grp: "grp-wind",        min: 1,   max: 60,    step: 1   },
  { group: "wind", key: "gust_spread_max_kt",  id: "set-wind-gust",         label: "Gust spread",        unit: "kt", grp: "grp-wind",        min: 1,   max: 40,    step: 1   },
  { group: "wind", key: "gust_spread_floor_kt",id: "set-wind-gust-floor",   label: "Gust spread floor",  unit: "kt", grp: "grp-wind",        min: 0,   max: 40,    step: 1,
    hint: "Peak gust below which the gust-spread limit is reported but does not fail the flight. A forecast wind and a forecast gust are different statistics, so their difference runs larger than a METAR's G - and 10 kt of spread under a 13 kt peak is not the weather the spread limit is written for. 0 = gate on the spread alone." },
  { group: "wind", key: "crosswind_max_kt",    id: "set-wind-xwind",        label: "Crosswind",          unit: "kt", grp: "grp-wind",        min: 1,   max: 40,    step: 1   },
  { group: "ceiling_agl_ft", key: "day_circuit",          id: "set-ceil-day-circuit",  label: "Day circuit",        unit: "ft", grp: "grp-ceiling",     min: 100, max: 15000, step: 100 },
  { group: "ceiling_agl_ft", key: "day_xc",               id: "set-ceil-day-xc",       label: "Day cross-country",  unit: "ft", grp: "grp-ceiling",     min: 100, max: 15000, step: 100 },
  { group: "ceiling_agl_ft", key: "night_circuit",        id: "set-ceil-night-circuit",label: "Night circuit",      unit: "ft", grp: "grp-ceiling",     min: 100, max: 15000, step: 100 },
  { group: "ceiling_agl_ft", key: "night_xc_cloud_base",  id: "set-ceil-night-xc",     label: "Night XC cloud base",unit: "ft", grp: "grp-ceiling",     min: 100, max: 15000, step: 100 },
  { group: "visibility_sm",  key: "day_circuit",          id: "set-vis-day-circuit",   label: "Day circuit",        unit: "SM", grp: "grp-vis",         min: 0,   max: 20,    step: 1   },
  { group: "visibility_sm",  key: "day_xc",               id: "set-vis-day-xc",        label: "Day cross-country",  unit: "SM", grp: "grp-vis",         min: 0,   max: 20,    step: 1   },
  { group: "visibility_sm",  key: "night_circuit",        id: "set-vis-night-circuit", label: "Night circuit",      unit: "SM", grp: "grp-vis",         min: 0,   max: 20,    step: 1   },
  { group: "visibility_sm",  key: "night_xc",             id: "set-vis-night-xc",      label: "Night cross-country",unit: "SM", grp: "grp-vis",         min: 0,   max: 20,    step: 1   },
  { group: "density_altitude", key: "advisory_above_field_ft", id: "set-da-advisory", label: "Advise above field by", unit: "ft", grp: "grp-da", min: 0, max: 5000, step: 100 },
  // One flat floor each, day and night - IFR has no circuit/cross-country
  // split, so the labels carry no "XC". The KEYS stay day_xc/night_xc: they are
  // the wire format (config._NUMERIC_LIMITS) and the saved-profile shape, and
  // renaming them would silently drop every pilot's stored IFR minimums.
  { group: "ifr_ceiling_agl_ft", key: "day_xc",   id: "set-ifr-ceil-day",  label: "IFR day",   unit: "ft", grp: "grp-ifr-ceiling", min: 100, max: 15000, step: 100 },
  { group: "ifr_ceiling_agl_ft", key: "night_xc", id: "set-ifr-ceil-night",label: "IFR night", unit: "ft", grp: "grp-ifr-ceiling", min: 100, max: 15000, step: 100 },
  { group: "ifr_visibility_sm",  key: "day_xc",   id: "set-ifr-vis-day",   label: "IFR day",   unit: "SM", grp: "grp-ifr-vis",     min: 0,   max: 20,    step: 1   },
  { group: "ifr_visibility_sm",  key: "night_xc", id: "set-ifr-vis-night", label: "IFR night", unit: "SM", grp: "grp-ifr-vis",     min: 0,   max: 20,    step: 1   },
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
// ON by default - matches the original card exactly
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
    items: ["Expect airspeed changes - stay alert", "Avoid terrain rotor areas", "Slow toward manoeuvring speed"],
  },
  icing_potential: {
    label: "Icing risk",
    items: ["Know the freezing level", "Identify warm and cold layers", "Exit immediately - usually descend"],
  },
  // The two per-flight ticks had no entry here, so a MITIGATE driven by one of
  // them rendered an empty "Threat mitigation reference" - a heading over nothing.
  unfamiliar_or_complex_airspace: {
    label: "Unfamiliar / complex airspace",
    items: ["Study the VTA and CFS entry before start-up", "Frequencies and transition routes written down",
            "Call well back - ask for progressive taxi", "Pick a hold-off point to orbit if it gets busy"],
  },
  terrain_critical: {
    label: "Terrain",
    items: ["Know the highest obstacle within 10 nm of track", "Set a hard minimum en-route altitude",
            "Plan the escape turn before you need it", "Cross ridges at an angle, never square on"],
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

// ---------- Aircraft profile (per-browser) ----------
// Typical cruise true airspeed (kt) by manufacturer/model. Selecting a model
// prefills its TAS, but the pilot can override it; only the TAS reaches the
// backend, where it drives every time & groundspeed calculation.
const AIRCRAFT_CATALOG = {
  Cessna: { "152": 107, "172 Skyhawk": 110, "172RG Cutlass": 140, "182 Skylane": 145, "T182 Turbo Skylane": 156, "206 Stationair": 145, "210 Centurion": 190, "TTx (Corvalis)": 235 },
  Cirrus: { "SR20": 155, "SR22": 170, "SR22T": 185 },
  Piper: { "PA-28 Cherokee": 115, "PA-28 Archer": 125, "PA-28R Arrow": 137, "PA-32 Saratoga": 165, "PA-46 Malibu": 213 },
  Diamond: { "DA20 Katana": 120, "DA40 Star": 140, "DA42 Twin Star": 170 },
  Beechcraft: { "Sundowner": 115, "A36 Bonanza": 170, "58 Baron": 200 },
  Mooney: { "M20J (201)": 160, "M20R Ovation": 190, "M20TN Acclaim": 237 },
  Grumman: { "AA-1 Yankee": 120, "AA-5 Tiger": 140 },
  Custom: {},
};
const AC_LS_KEY = "minima.aircraft.v1";
let AIRCRAFT = { make: null, model: null, tas: null };

function loadAircraft() {
  let a = null;
  try { a = JSON.parse(localStorage.getItem(AC_LS_KEY) || "null"); } catch { a = null; }
  AIRCRAFT = {
    make: (a && a.make) || null,
    model: (a && a.model) || null,
    tas: a && Number.isFinite(+a.tas) && +a.tas > 0 ? +a.tas : null,
  };
}
function saveAircraft() {
  const out = {};
  if (AIRCRAFT.make) out.make = AIRCRAFT.make;
  if (AIRCRAFT.model) out.model = AIRCRAFT.model;
  if (AIRCRAFT.tas) out.tas = AIRCRAFT.tas;
  if (Object.keys(out).length) localStorage.setItem(AC_LS_KEY, JSON.stringify(out));
  else localStorage.removeItem(AC_LS_KEY);
}
// The TAS to use for requests; falls back to the server's default profile.
function currentTas() {
  if (AIRCRAFT.tas && AIRCRAFT.tas > 0) return AIRCRAFT.tas;
  return CONFIG ? CONFIG.cruise_kt : null;
}

// ---------- Appearance (per-browser) ----------
// Auto follows the OS; Light and Dark pin it. The inline boot script in
// index.html has already applied the resolved theme before first paint - this
// pair is what keeps the stored choice, the toggle and the theme-color meta
// agreeing with each other afterwards.
//
// Deliberately NOT part of PROFILE: appearance changes how the app looks, never
// how a flight is assessed, so it must not travel with the minimums and must
// not be wiped by "Reset to defaults".
//
// Note this is a different axis from the Day flight / Night flight control on
// the Route tab. That one is civil twilight and decides which set of personal
// minimums a flight is gated against; this one is only the colour scheme.
const THEME_KEY = "minima.theme.v1";   // duplicated in the boot script in index.html
function loadTheme() {
  try {
    const v = localStorage.getItem(THEME_KEY);
    if (v === "light" || v === "dark" || v === "auto") return v;
  } catch (_) {}
  return "auto";
}
function saveTheme(v) {
  // Storing nothing for "auto" collapses "never chose" and "chose Auto" into one
  // state - same convention as saveProfile() dropping its defaults.
  try {
    if (v === "auto") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, v);
  } catch (_) {}
}
const prefersLight = () =>
  !!(window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches);

function applyTheme(pref) {
  const resolved = pref === "auto" ? (prefersLight() ? "light" : "dark") : pref;
  document.documentElement.dataset.theme = resolved;
  // The address bar / task-switcher colour. Read back off --bg rather than kept
  // as a second copy of the hex, so the meta can never disagree with the
  // stylesheet about what the page background actually is.
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) {
    const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
    if (bg) meta.setAttribute("content", bg);
  }
}

// The cycle order the header button steps through. Auto first because it is the
// default, and because Auto -> Light -> Dark reads as "let the device decide,
// then override it one way, then the other".
const THEME_CYCLE = ["auto", "light", "dark"];
const THEME_WORDS = {
  auto: "Auto (following your device)",
  light: "Light",
  dark: "Dark",
};

function wireTheme() {
  let pref = loadTheme();
  applyTheme(pref);
  const btn = document.getElementById("theme-toggle");

  // The button is a cycle, not an on/off, so aria-pressed would be a lie - it
  // is defined for two states. Instead the accessible name carries the current
  // mode and what the next tap does, and is rewritten on every change.
  const paint = () => {
    if (!btn) return;
    btn.dataset.themePref = pref;                       // picks the icon in CSS
    const next = THEME_CYCLE[(THEME_CYCLE.indexOf(pref) + 1) % THEME_CYCLE.length];
    const label = `Theme: ${THEME_WORDS[pref]}. Switch to ${THEME_WORDS[next]}.`;
    btn.setAttribute("aria-label", label);
    btn.setAttribute("title", label);
  };
  paint();

  if (btn) {
    btn.addEventListener("click", () => {
      pref = THEME_CYCLE[(THEME_CYCLE.indexOf(pref) + 1) % THEME_CYCLE.length];
      saveTheme(pref);
      applyTheme(pref);
      paint();
    });
  }

  // Auto has to keep following the OS while the tab sits open. The label moves
  // too: on Auto the icon means "whatever the device says", and the device just
  // said something different.
  if (window.matchMedia) {
    matchMedia("(prefers-color-scheme: light)")
      .addEventListener("change", () => { if (pref === "auto") { applyTheme(pref); paint(); } });
  }
}

function fillModelOptions(make) {
  const sel = $("#ac-model");
  const names = Object.keys(AIRCRAFT_CATALOG[make] || {});
  if (!names.length) {  // "Custom" - no preset models, TAS typed by hand
    sel.innerHTML = `<option value="">Custom</option>`;
    sel.disabled = true;
  } else {
    sel.disabled = false;
    sel.innerHTML = names.map((n) => `<option value="${n}">${n}</option>`).join("");
  }
}

function buildAircraftPicker() {
  const makeSel = $("#ac-make"), modelSel = $("#ac-model"), tasInput = $("#ac-tas");
  if (!makeSel || !modelSel || !tasInput) return;
  makeSel.innerHTML = Object.keys(AIRCRAFT_CATALOG).map((m) => `<option value="${m}">${m}</option>`).join("");

  // Restore the stored selection, defaulting to the Cessna 172 profile.
  const make = (AIRCRAFT.make && AIRCRAFT_CATALOG[AIRCRAFT.make]) ? AIRCRAFT.make : "Cessna";
  makeSel.value = make;
  fillModelOptions(make);
  const models = Object.keys(AIRCRAFT_CATALOG[make] || {});
  const model = (AIRCRAFT.model && models.includes(AIRCRAFT.model)) ? AIRCRAFT.model : (models[0] || "");
  modelSel.value = model;
  const presetTas = (AIRCRAFT_CATALOG[make] || {})[model];
  tasInput.value = Math.round(AIRCRAFT.tas || presetTas || (CONFIG ? CONFIG.cruise_kt : 110));
  AIRCRAFT = { make, model: model || null, tas: +tasInput.value || null };
  saveAircraft();

  const syncTasFromModel = () => {
    const m = makeSel.value, md = modelSel.value;
    const t = (AIRCRAFT_CATALOG[m] || {})[md];
    if (t) tasInput.value = Math.round(t);  // prefill, but stay editable
    AIRCRAFT = { make: m, model: md || null, tas: +tasInput.value || null };
    saveAircraft();
  };
  makeSel.addEventListener("change", () => {
    fillModelOptions(makeSel.value);
    modelSel.value = Object.keys(AIRCRAFT_CATALOG[makeSel.value] || {})[0] || "";
    syncTasFromModel();
  });
  modelSel.addEventListener("change", syncTasFromModel);
  tasInput.addEventListener("change", () => {
    let v = Math.round(+tasInput.value);
    if (!Number.isFinite(v) || v <= 0) v = Math.round(CONFIG ? CONFIG.cruise_kt : 110);
    v = Math.max(40, Math.min(400, v));
    tasInput.value = v;
    AIRCRAFT = { make: makeSel.value, model: modelSel.value || null, tas: v };
    saveAircraft();
  });
}

// ---------- Init ----------
async function init() {
  // Before the config fetch, and outside the try: the theme must still apply and
  // the toggle must still work when the backend is down, which is exactly when
  // someone is sitting here staring at the error banner.
  wireTheme();
  // Everything below is built from CONFIG - threats, sliders, default limits - so
  // a failed config fetch used to leave a shell with no controls and no
  // explanation. Say what happened and offer the retry.
  try {
    const res = await fetch("/api/config");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    CONFIG = await res.json();
  } catch (e) {
    setHealth("discovery-data-health", fetchFailedBanner(String(e), "init"));
    setHealth("route-data-health", fetchFailedBanner(String(e), "init"));
    return;
  }
  // Start warming the backend while the pilot is still typing a destination.
  //
  // The machine scales to zero when idle, so the first assessment of the day
  // wakes it with an empty cache and no open connections to any upstream. The
  // config fetch above is what wakes it; this puts that head start to use by
  // pulling the products that don't depend on which route gets picked - the
  // national advisory feeds and the home base. Deliberately not awaited: it
  // must never hold up the page, and nothing on the page depends on it. Its
  // failures are equally deliberately ignored - the assessment will re-fetch
  // and report honestly, and a warmup has no business raising a banner.
  fetch("/api/prewarm").catch(() => {});

  $("#radius").value = CONFIG.default_radius_nm;
  $("#radius").max = CONFIG.max_radius_nm;

  loadProfile();
  loadAircraft();
  buildAircraftPicker();
  renderExtraThreats();
  buildConservatism();
  renderMinSliders();
  renderRecencySlider();
  buildWxFlags();
  fillProfileForm();
  renderMinimums();
  renderMyMinimumsSettings();
  // Preflight self-assessment is a standing pre-check shown above the route/discovery
  // inputs - render it up front so it's done before any weather is pulled.
  renderSelfAssessment("route-self-check");
  renderSelfAssessment("discovery-self-check");
  $("#dep").value = baseIdent();
  buildEtdOptions();
  // Quarter-hour options go stale four times as fast as the old hourly ones, so
  // the list is refreshed on a timer rather than only on focus/tab changes.
  setInterval(() => { if (!document.hidden) buildEtdOptions(); }, 60000);
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
// Freeze the "Data time" to now - call when a fresh assessment's data arrives.
function stampDataTime() {
  const el = $("#data-time");
  if (el) el.textContent = fmtZulu(new Date());
}

// ---------- "Pulling data…" elapsed timer ----------
// An assessment pulls ~19 products from three upstreams and can take tens of
// seconds on a cold backend. All the pilot used to see was a disabled button
// reading "Pulling data…", which is indistinguishable from a frozen app - and
// the honest answer to "is it stuck?" is a number that keeps moving. When it
// lands, the elapsed time stays on screen, so the wait is attributable to
// gathering the data rather than left a mystery.
//
// Handles live on the function object (the `flashStatus` idiom) so a re-entrant
// run - the error banner's retry button calls runRoute() again by name - can
// never leave an orphaned interval ticking into a stale element.
const RUN_TIMER_TICK_MS = 100;

function runTimerSecs(id) {
  const started = (startRunTimer._at || {})[id];
  return started == null ? 0 : (performance.now() - started) / 1000;
}

// One decimal is the right resolution for the case this exists for - a cold
// backend taking ten or twenty seconds. It is the wrong resolution for a fully
// cached re-run, which finishes in milliseconds and would report "0.0 s", a
// number that reads as a broken counter rather than as the good news it is.
const runTimerText = (s) => `${s < 1 ? s.toFixed(2) : s.toFixed(1)} s`;

function startRunTimer(id) {
  stopRunTimer(id);                       // idempotent across re-entry
  const el = $("#" + id);
  if (!el) return;
  startRunTimer._at = startRunTimer._at || {};
  startRunTimer._t = startRunTimer._t || {};
  startRunTimer._at[id] = performance.now();
  // aria-hidden while it ticks: announcing a counter ten times a second would
  // bury the result it is counting towards. The final duration below is
  // announced once, when it means something.
  el.setAttribute("aria-hidden", "true");
  const tick = () => (el.textContent = runTimerText(runTimerSecs(id)));
  tick();
  startRunTimer._t[id] = setInterval(tick, RUN_TIMER_TICK_MS);
}

// `ok` false means the run failed: the error banner is the message, and
// "data fetched in" would be a lie about a fetch that didn't.
function stopRunTimer(id, ok) {
  const handles = startRunTimer._t || {};
  clearInterval(handles[id]);
  delete handles[id];
  const el = $("#" + id);
  if (!el) return;
  if (ok) {
    el.textContent = `data fetched in ${runTimerText(runTimerSecs(id))}`;
    el.removeAttribute("aria-hidden");
  } else {
    el.textContent = "";
  }
  if (startRunTimer._at) delete startRunTimer._at[id];
}

const baseIdent = () => PROFILE.base || CONFIG.departure;
const labelOf = (s) => s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

// ---------- ETD (Zulu) ----------
// Deliberately NOT persisted to localStorage: a restored "yesterday 14:00Z"
// would silently assess the wrong flight. It resets to "Now" every load.
const ETD_IDS = ["#etd", "#d-etd"];
const zPad = (x) => String(x).padStart(2, "0");
const zHM = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? "" : `${zPad(d.getUTCHours())}${zPad(d.getUTCMinutes())}Z`;
};
// Parse a timeline timestamp as UTC. The backend's hourly times are Zulu but
// carry no "Z" suffix ("2026-08-13T04:00"), and a bare ISO string without one is
// parsed as *browser-local* - which silently shifted every hour label by the
// viewer's own offset. Returns null on an unparseable value.
const utcDate = (t) => {
  if (!t) return null;
  const d = new Date(/[Zz]|[+-]\d{2}:?\d{2}$/.test(t) ? t : t + "Z");
  return isNaN(d) ? null : d;
};

// Whole UTC calendar days from `a` to `b` - "which day does this land on", not
// "how many hours apart". The day-ahead prefix and the +N rollover marker both
// ask that one question, so they share one answer.
const utcDayDiff = (a, b) => Math.round(
  (Date.UTC(b.getUTCFullYear(), b.getUTCMonth(), b.getUTCDate())
    - Date.UTC(a.getUTCFullYear(), a.getUTCMonth(), a.getUTCDate())) / 86400000);

// Days the end of a span runs past its start; 0 when it stays on one UTC date.
const dayDiff = (fromIso, toIso) => {
  const a = utcDate(fromIso), b = utcDate(toIso);
  return a && b ? utcDayDiff(a, b) : 0;
};

// A Zulu span, mirroring the backend's `weather.zulu_range`: "2000Z-0300Z+1".
// A span routinely runs past midnight Z, and a bare "2000Z-0300Z" reads as
// running backwards. Plain text - `supDays` raises the +N once, at render time.
const zRange = (fromIso, toIso) => {
  const n = dayDiff(fromIso, toIso);
  return `${zHM(fromIso)}-${zHM(toIso)}${n > 0 ? `+${n}` : ""}`;
};

// Raise a rollover marker into a superscript, so "+1" reads as an annotation on
// the time rather than part of it. Anchored on a four-digit Zulu time, so it
// cannot touch other "+N" text - etdLabel's "· +21 min" is left alone. Only
// ever run on already-escaped text, and only ever inserts this one known tag,
// so it cannot introduce markup the escape just removed.
const supDays = (s) => s.replace(/(\d{4}Z)\+(\d+)/g, "$1<sup>+$2</sup>");
// Escaped for HTML with any +N raised: for server text that can carry a span.
const zText = (s) => supDays(escapeHtml(s));

// The arriving end of a span, marker already raised. The arrow forms
// ("ETD 2330Z → ETA 0115Z+1") are not "A-B" ranges, but they ask the same
// question of the same two times. Returns HTML - these sites build their own.
const zEnd = (fromIso, toIso) => {
  const n = dayDiff(fromIso, toIso);
  return supDays(`${zHM(toIso)}${n > 0 ? `+${n}` : ""}`);
};

// Quarter-hour granularity for the first few hours, because that is the
// resolution people actually plan a departure at - the old list jumped straight
// from "Now" to the next whole hour, so at 1424Z a flight leaving in twenty
// minutes had no option that described it. Past FINE_HRS the weather no longer
// moves fast enough to justify four options an hour.
const ETD_STEP_MIN = 15;
const ETD_FINE_HRS = 4;

const etdValue = (d) => `${d.toISOString().slice(0, 16)}Z`;

// "Today" / "Tomorrow" / "Thu", by UTC date difference.
function dayPrefix(d, now) {
  const diff = utcDayDiff(now, d);
  if (diff === 0) return "Today";
  if (diff === 1) return "Tomorrow";
  return d.toLocaleDateString(undefined, { weekday: "short", timeZone: "UTC" });
}

// "Today 1445Z · +21 min" - the Zulu time to fly by, and how far off it is. The
// offset is the true delta from now, not the nominal step: snapping to the
// quarter hour means the first option is rarely exactly 15 minutes away.
function etdLabel(d, now) {
  const mins = Math.round((d - now) / 60000);
  return `${dayPrefix(d, now)} ${zPad(d.getUTCHours())}${zPad(d.getUTCMinutes())}Z · +${fmtHrMin(mins / 60)}`;
}

function etdOptionList(now = new Date()) {
  const hours = CONFIG.timeline_hours || 48;
  const out = [{
    value: "now",
    label: `Now (${zPad(now.getUTCHours())}${zPad(now.getUTCMinutes())}Z)`,
  }];
  const horizon = now.getTime() + hours * 3600000;

  // Quarter hours: step first, then snap up to the next :00/:15/:30/:45. Snapping
  // first would offer a departure two minutes out at 1428Z, which is not a plan.
  const step = ETD_STEP_MIN * 60000;
  let t = Math.ceil((now.getTime() + step) / step) * step;
  const fineEnd = now.getTime() + ETD_FINE_HRS * 3600000;
  for (; t <= fineEnd && t <= horizon; t += step) {
    const d = new Date(t);
    out.push({ value: etdValue(d), label: etdLabel(d, now) });
  }
  // Whole hours from there to the forecast horizon.
  const hourly = new Date(t);
  hourly.setUTCMinutes(0, 0, 0);
  if (hourly.getTime() < t) hourly.setUTCHours(hourly.getUTCHours() + 1);
  for (let ms = hourly.getTime(); ms <= horizon; ms += 3600000) {
    const d = new Date(ms);
    out.push({ value: etdValue(d), label: etdLabel(d, now) });
  }
  return out;
}

// Rebuilt on load, on tab switch, whenever the tab regains focus and on a timer
// - a window left open would otherwise still be offering times that have been
// and gone. The server clamps out-of-range values as a backstop.
function buildEtdOptions() {
  const now = new Date();
  const opts = etdOptionList(now);
  let changed = false;
  for (const id of ETD_IDS) {
    const sel = $(id);
    if (!sel) continue;
    const prev = sel.value;
    const list = opts.slice();
    // A selection that is still in the future but no longer on a boundary (you
    // picked 1445Z, it is now 1446Z) must survive the rebuild. Dropping it would
    // silently snap the control back to "Now" and assess a different flight than
    // the one on screen - the whole reason ETD is not persisted across loads.
    const keep = prev && prev !== "now" && !opts.some((o) => o.value === prev);
    const stale = keep && new Date(prev) <= now;
    if (keep && !stale) {
      list.splice(1, 0, { value: prev, label: etdLabel(new Date(prev), now) });
    }
    sel.innerHTML = list.map((o) =>
      `<option value="${o.value}">${escapeHtml(o.label)}</option>`).join("");
    if (prev && list.some((o) => o.value === prev)) sel.value = prev;
    // Past its own ETD: reset, but say so rather than changing it behind you.
    const row = sel.closest(".ac, .control") || sel.parentElement;
    if (row) row.classList.toggle("etd-lapsed", !!stale);
    // Setting .value in script fires no "change", so a rebuild that dropped a
    // lapsed ETD back to "Now" used to leave the day/night toggle answering for
    // a departure time that no longer exists.
    if (sel.value !== prev) changed = true;
  }
  syncDiscoveryBtn();
  if (changed) refreshAutoDayNight();
}

const etdParam = (id = "#etd") => {
  const v = ($(id) || {}).value;
  return !v || v === "now" ? {} : { etd: v };
};

// Whether an ETD control is still on "Now". Drives the copy that only makes
// sense once you have planned a departure ("Planned ETD 1800Z").
const isNowEtd = (id = "#etd") => {
  const v = ($(id) || {}).value;
  return !v || v === "now";
};

// "Find flights now" is a claim about the present tense. Once you have picked a
// departure time it is answering for then, not now.
const discoveryBtnLabel = () => {
  const v = ($("#d-etd") || {}).value;
  return !v || v === "now" ? "Find flights now" : "Find flights";
};

function syncDiscoveryBtn() {
  const btn = $("#run-discovery");
  if (btn && !btn.disabled) btn.textContent = discoveryBtnLabel();
}

// ---------- Wire ----------
function wire() {
  // makeDragOnly first so its guard runs before the readout listener (see note there).
  makeDragOnly($("#radius")); makeDragOnly($("#f-time"));
  $("#radius").addEventListener("input", (e) => ($("#radius-out").textContent = `${e.target.value} nm`));
  $("#f-time").addEventListener("input", (e) => ($("#f-time-out").textContent = +e.target.value ? `${e.target.value} min` : "Any"));
  // Scope each seg-btn toggle to its own .seg group; re-render extra threats on IFR/VFR change.
  $$(".seg-btn").forEach((b) => b.addEventListener("click", () => {
    b.closest(".seg").querySelectorAll(".seg-btn").forEach((x) => x.classList.toggle("active", x === b));
    if (b.dataset.rules !== undefined) renderExtraThreats();
    // Picking day/night yourself overrides the civil-twilight auto-selection
    // until the ETD or aerodrome changes.
    if (b.dataset.mode !== undefined) MODE_MANUAL = true;
  }));

  // A new ETD, or either end of the route, is a different flight - so the manual
  // override lapses and day/night is derived again. The destination is in here
  // because a day departure can still be a night landing, and that decides which
  // personal minimums the whole assessment runs against.
  for (const id of ETD_IDS) {
    const sel = $(id);
    if (sel) sel.addEventListener("change", rederiveDayNight);
  }
  const dEtd = $("#d-etd");
  if (dEtd) dEtd.addEventListener("change", syncDiscoveryBtn);
  for (const id of ["#dep", "#dest", "#circ-aerodrome"]) {
    const el = $(id);
    if (!el) continue;
    el.addEventListener("change", rederiveDayNight);
    // `change` on a text input only fires on blur, so typing a full identifier
    // and going straight to the ETD dropdown left the toggle describing the
    // previous aerodrome. Debounced so it fires once, on the finished code.
    let typing = null;
    el.addEventListener("input", () => {
      clearTimeout(typing);
      typing = setTimeout(rederiveDayNight, 350);
    });
  }
  // The aircraft's TAS moves the ETA, which can move the arrival across
  // twilight. Cheap to re-derive; wrong to leave stale.
  for (const id of ["#ac-model", "#ac-tas"]) {
    const el = $(id);
    if (el) el.addEventListener("change", rederiveDayNight);
  }
  $$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
  // The logo is the way home. My Minimums is where the app opens and where the
  // profile every verdict is gated against lives, so that is "home".
  const brand = $("#brand-home");
  if (brand) {
    brand.addEventListener("click", (e) => { e.preventDefault(); switchTab("settings"); });
    brand.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); switchTab("settings"); }
    });
  }
  wireWxPopovers();
  $("#run-route").addEventListener("click", runRoute);
  $("#run-discovery").addEventListener("click", runDiscovery);
  $("#save-minimums").addEventListener("click", saveMinimums);
  $("#reset-minimums").addEventListener("click", resetMinimums);
  // VFR/IFR tab on the minimums card swaps which weather-minimums set is shown.
  // It deliberately no longer rebuilds the hazard checkboxes: that rebuild was
  // what dropped the widespread-IMC tick on the way back to VFR, and the list
  // is the same under both rule sets anyway.
  $$(".rule-tab").forEach((b) => b.addEventListener("click", () => {
    const rule = b.dataset.rule;
    $$(".rule-tab").forEach((x) => x.classList.toggle("active", x === b));
    $$(".rule-pane").forEach((p) => p.classList.toggle("hidden", p.dataset.rule !== rule));
  }));
  autocomplete("dep", "dep-list");
  autocomplete("dest", "dest-list");
  autocomplete("circ-aerodrome", "circ-list");
  autocomplete("set-base", "base-list");

  // A backgrounded tab's ETD hours drift into the past; rebuild on return.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) buildEtdOptions();
  });

  // Flight-type toggle: XC ↔ Circuits
  $$("[data-ftype]").forEach((b) => b.addEventListener("click", () => {
    $$("[data-ftype]").forEach((x) => x.classList.toggle("active", x === b));
    applyFlightType(b.dataset.ftype);
    // Circuits assess a different aerodrome, so re-derive day/night for it.
    rederiveDayNight();
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

// ---------- Auto day/night ----------
// The toggle used to default to Day on every load, so a 0200Z departure was
// quietly assessed against daytime ceiling and visibility minimums unless you
// remembered to flip it. It now selects itself from the ETD, using civil
// twilight at the departure aerodrome (CARs 101.01: night runs from the end of
// evening civil twilight to the beginning of morning civil twilight).
//
// A manual click wins and sticks - but only until the ETD or the aerodrome
// changes, because that is a different flight and the old override says nothing
// about it.
let MODE_MANUAL = false;

// Something about the flight changed, so any manual day/night choice lapses -
// it was made about a different flight - and the toggle is derived again.
function rederiveDayNight() {
  MODE_MANUAL = false;
  refreshAutoDayNight();
}

// Which aerodrome(s) and time the current tab is actually planning from.
// A cross-country carries its destination too: the toggle drives which personal
// minimums are applied, and a leg that lands after evening civil twilight is a
// night flight however bright it was on departure.
function autoDayNightContext() {
  const tab = ($$(".tab.active")[0] || {}).dataset?.tab;
  if (tab === "discovery") return { ident: baseIdent(), etd: ($("#d-etd") || {}).value };
  if (tab === "route") {
    if (currentFlightType() === "circuits") {
      return { ident: $("#circ-aerodrome").value.trim() || baseIdent(),
               etd: ($("#etd") || {}).value };
    }
    return {
      ident: $("#dep").value.trim() || baseIdent(),
      dest: $("#dest").value.trim(),
      etd: ($("#etd") || {}).value,
    };
  }
  return null;  // My Minimums - the flight controls are hidden there
}

// Move the selection, and nothing else. The control used to switch to a dashed
// border when derived, and to carry a caption whose length set its width, so
// picking an ETD redrew the whole thing. Only the active half moves now.
function setMode(mode) {
  const btn = $$(".seg-btn[data-mode]").find((b) => b.dataset.mode === mode);
  if (!btn) return;
  btn.closest(".seg").querySelectorAll(".seg-btn")
     .forEach((x) => x.classList.toggle("active", x === btn));
}

// Requests are numbered so a slow earlier answer can never overwrite a newer
// one. Changing the destination and then the ETD fires two lookups; without
// this the first to *return* wins, and the toggle settles on the flight you had
// already moved on from.
let DAYNIGHT_SEQ = 0;

async function refreshAutoDayNight() {
  if (MODE_MANUAL) return;
  const ctx = autoDayNightContext();
  if (!ctx || !ctx.ident) return;
  const p = new URLSearchParams({ ident: ctx.ident });
  if (ctx.etd && ctx.etd !== "now") p.set("at", ctx.etd);
  // A destination only helps once it is long enough to be an identifier; the
  // server ignores one it doesn't know, so a half-typed code is harmless.
  if (ctx.dest && ctx.dest.length >= 3) p.set("dest", ctx.dest);
  const tas = currentTas();
  if (tas && tas > 0) p.set("tas", Math.round(tas));
  const seq = ++DAYNIGHT_SEQ;
  try {
    const r = await fetch(`/api/daynight?${p}`);
    if (!r.ok) return;                       // unknown aerodrome mid-typing
    const d = await r.json();
    if (MODE_MANUAL || seq !== DAYNIGHT_SEQ) return;  // superseded while in flight
    setMode(d.mode);
  } catch { /* leave the toggle as it stands */ }
}

function switchTab(name) {
  closeWxPop();   // it lives on <body>, so it would otherwise outlive its card
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $("#tab-route").classList.toggle("hidden", name !== "route");
  $("#tab-discovery").classList.toggle("hidden", name !== "discovery");
  $("#tab-settings").classList.toggle("hidden", name !== "settings");
  // The per-flight controls (time of day, flight rules, extra threats) are
  // meaningless on the My Minimums tab - hide them there.
  $("#flight-controls").classList.toggle("hidden", name === "settings");
  // Refresh the ETD hours - the list goes stale in a tab left open.
  if (name !== "settings") { buildEtdOptions(); refreshAutoDayNight(); }
}

// This flight's threats = per-flight toggles + night.
function threatsParam() {
  const set = new Set();
  $$(".threat").filter((c) => c.checked).forEach((c) => set.add(c.value));
  // Night rides in as a manual threat, unless you've said night isn't one for
  // you. The backend enforces the same rule - this keeps the on-screen threat
  // mitigations in step with the card.
  if (currentMode() === "night" && effectiveLimits().night_as_threat) set.add("night_operations");
  return [...set].join(",");
}
// Backend prefs payload: custom minimums and/or a non-default conservatism preset.
function prefsParam() {
  const p = { ...(PROFILE.minimums || {}) };
  if (PROFILE.conservatism && PROFILE.conservatism !== CONFIG.default_conservatism) p.conservatism = PROFILE.conservatism;
  return Object.keys(p).length ? { prefs: JSON.stringify(p) } : {};
}

// Aircraft true airspeed sent with route/discovery requests; omitted when it
// matches the server default so the backend keeps its own profile.
function tasParam() {
  const t = currentTas();
  return t && t > 0 ? { tas: Math.round(t) } : {};
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
    density_altitude: { ...(d.density_altitude || {}), ...(m.density_altitude || {}) },
    ifr_ceiling_agl_ft: { ...(difr.ceiling_agl_ft || {}), ...(m.ifr_ceiling_agl_ft || {}) },
    ifr_visibility_sm:  { ...(difr.visibility_sm   || {}), ...(m.ifr_visibility_sm  || {}) },
    weather_flags:   m.weather_flags || d.weather_flags,
    imc_as_threat:   (m.imc_as_threat !== undefined) ? m.imc_as_threat : !!difr.imc_as_threat,
    night_as_threat: (m.night_as_threat !== undefined) ? m.night_as_threat
                                                       : CONFIG.default_night_as_threat !== false,
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
      // Setting .value in script does not fire "change", and picking an
      // aerodrome from the list has to reach the listeners that react to it
      // (day/night auto-selection) exactly as typing one does.
      $$(`#${listId} .ac-item`).forEach((el) => el.addEventListener("click", () => {
        input.value = el.dataset.id;
        hide();
        input.dispatchEvent(new Event("change", { bubbles: true }));
      }));
    }, 180);
  });
  input.addEventListener("blur", () => setTimeout(hide, 200));
  function hide() { list.classList.add("hidden"); list.innerHTML = ""; }
}

// ---------- GFA (graphical area forecast) ----------
let GFA = { region: null, products: {}, sub: null, frame: 0, etd: null };
const GFA_LABELS = { CLDWX: "Clouds & weather", TURBC: "Icing & turbulence", GFA: "GFA" };

// The panel that actually covers your ETD. GFA panels are issued 6-hourly and
// each covers a slice, so opening on frame 0 showed the chart for *now* even
// when the flight is at 0100Z - the pilot then had to work out which tab to
// press. Falls back to the last panel starting at or before the ETD, since a
// single issuance only reaches ~12 h ahead and a distant ETD runs off the end.
function gfaFrameFor(frames, etdIso) {
  const t = Date.parse(etdIso || "");
  if (!frames.length || isNaN(t)) return 0;
  for (let i = 0; i < frames.length; i++) {
    const s = Date.parse(frames[i].validity), e = Date.parse(frames[i].valid_end);
    if (!isNaN(s) && !isNaN(e) && t >= s && t < e) return i;
  }
  let best = -1, bestT = -Infinity;
  for (let i = 0; i < frames.length; i++) {
    const s = Date.parse(frames[i].validity);
    if (!isNaN(s) && s <= t && s > bestT) { bestT = s; best = i; }
  }
  return best >= 0 ? best : 0;
}

// Whether the shown panel really covers the ETD, so the caption can say when it
// doesn't rather than implying the chart describes the flight.
function gfaCovers(frame, etdIso) {
  const t = Date.parse(etdIso || "");
  const s = Date.parse((frame || {}).validity || ""), e = Date.parse((frame || {}).valid_end || "");
  if (isNaN(t) || isNaN(s) || isNaN(e)) return true;
  return t >= s && t < e;
}

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
  return `<div class="panel gfa-panel"><h3>GFA - graphical area forecast</h3>
    <p class="hint">Charts couldn't be loaded right now.
    <a href="https://plan.navcanada.ca/" target="_blank" rel="noopener">Open the GFA on NAV CANADA ↗</a></p></div>`;
}

async function loadGfa(dep, dest, etdIso) {
  const host = $("#route-gfa");
  if (!host) return;
  host.innerHTML = `<div class="panel gfa-panel"><h3>GFA - graphical area forecast <span class="hint">loading…</span></h3></div>`;
  try {
    const params = new URLSearchParams({ dep, ...(dest ? { dest } : {}) });
    const data = await fetch(`/api/gfa?${params}`).then((r) => r.json());
    GFA = { region: data.region || null, products: data.products || {},
            sub: null, frame: 0, etd: etdIso || null };
    const subs = gfaSubs();
    if (!subs.length) { host.innerHTML = gfaFallback(); return; }
    GFA.sub = subs[0];
    GFA.frame = gfaFrameFor(GFA.products[GFA.sub] || [], GFA.etd);
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
  // Which panel your ETD actually falls in, so it can be marked even when you
  // have clicked away to another one.
  const etdFrame = GFA.etd ? gfaFrameFor(frames, GFA.etd) : -1;
  const etdNote = !GFA.etd ? ""
    : gfaCovers(fr, GFA.etd) ? `Covers your ETD ${zHM(GFA.etd)} · `
    : `Does not cover your ETD ${zHM(GFA.etd)} - latest chart stops short · `;
  const tabs = subs.map((s) => `<button class="gfa-tab ${s === GFA.sub ? "active" : ""}" data-sub="${s}">${GFA_LABELS[s] || s}</button>`).join("");
  const frameBtns = frames.length > 1
    ? `<div class="gfa-frames">${frames.map((f, i) => `<button class="gfa-frame ${i === GFA.frame ? "active" : ""}${i === etdFrame ? " etd" : ""}" data-frame="${i}"${i === etdFrame ? ' title="covers your ETD"' : ""}>${gfaFrameLabel(f, i)}</button>`).join("")}</div>`
    : "";
  host.innerHTML = `<div class="panel gfa-panel">
    <div class="gfa-head">
      <h3>GFA - graphical area forecast${GFA.region ? ` <span class="hint">${escapeHtml(GFA.region)}</span>` : ""}</h3>
      <div class="gfa-tabs">${tabs}</div>
    </div>
    ${frameBtns}
    <a class="gfa-img-link" href="${fr.url || "https://plan.navcanada.ca/"}" target="_blank" rel="noopener">
      <img class="gfa-img" src="${fr.url || ""}" alt="GFA ${GFA.sub}" loading="lazy"
           onerror="this.closest('.gfa-panel').querySelector('.gfa-err').hidden=false" />
    </a>
    <p class="hint gfa-err" hidden>Chart image didn't load - <a href="https://plan.navcanada.ca/" target="_blank" rel="noopener">view on NAV CANADA ↗</a></p>
    <p class="hint gfa-cap">${fr.validity ? "Valid " + escapeHtml(String(fr.validity)) + " · " : ""}${etdNote}Source: NAV CANADA CFPS · tap chart to enlarge</p>
  </div>`;
  host.querySelectorAll(".gfa-tab").forEach((b) => b.addEventListener("click", () => {
    GFA.sub = b.dataset.sub;
    GFA.frame = gfaFrameFor(GFA.products[GFA.sub] || [], GFA.etd);
    drawGfa();
  }));
  host.querySelectorAll(".gfa-frame").forEach((b) => b.addEventListener("click", () => { GFA.frame = +b.dataset.frame; drawGfa(); }));
}

// ---------- Radar (Environment Canada GeoMet WMS, animated) ----------
const GEOMET_WMS = "https://geo.weather.gc.ca/geomet";
const RADAR_LABELS = { RADAR_1KM_RRAI: "Rain", RADAR_1KM_RSNO: "Snow" };
// ``routeView`` / ``hazardView`` are the two framings the fit button swaps
// between: the route as it has always been drawn, and that widened to take in
// the off-route areas as well.
let RADAR = { map: null, wms: null, frames: [], idx: 0, layer: "RADAR_1KM_RRAI",
              timer: null, routeView: null, hazardView: null };

const radarFallback = () => `<div class="panel radar-panel"><h3>Radar</h3>
  <p class="hint">Radar map couldn't load.
  <a href="https://weather.gc.ca/radar/index_e.html" target="_blank" rel="noopener">Open Environment Canada radar ↗</a></p></div>`;

// ---------- Area hazards drawn on the same map ----------
// The polygons a SIGMET or AIRMET is actually drawn as. A bulletin's "WI N4200
// W08100 - N4400 W08100 - ..." is a shape, and reading it as a shape is the
// difference between "there is weather somewhere in Ontario" and "it is forty
// miles north of your track".
// THEME-INDEPENDENT ON PURPOSE. Every colour in this map section is painted over
// OSM raster tiles and a 70%-opacity radar overlay, and that substrate is light
// whichever theme the app is in. These are not app chrome, so they must NOT be
// re-pointed at CSS tokens when the theme flips - doing so would break the
// contrast reasoning in the comments on pirepStyle() and the radar ring below.
// The only genuinely themed map surface is .leaflet-container's background (the
// void behind the tiles), which style.css owns.
const HAZARD_COLORS = {
  conv: "#e0483a", ts: "#e0483a", ice: "#4aa3df", turb: "#e8a33d",
  ifr: "#8f6fd6", mtn_obs: "#7d8c99", llws: "#d46fa8", sfc_wind: "#d46fa8",
  fzlvl: "#5bc0be", pcpn: "#6b8fb5", ash: "#9b7653", unknown: "#8a8a8a",
};
const hazardColor = (p) => HAZARD_COLORS[p.hazard] || HAZARD_COLORS.unknown;

// Relevant areas are drawn solid; the ones that were set aside stay on the map,
// dashed. Seeing a line of convective SIGMETs sitting just north of track is
// worth more than being told there is nothing on it.
//
// The set-aside branch used to be 1 px at 45% over a 6% fill, which is the same
// mistake `pirepStyle` below already had to undo: these are drawn over OSM tiles
// *and* a 70%-opacity radar layer, and at those values a convective SIGMET 90 nm
// off track was on the map but could not be seen. The dash is what carries the
// distinction - it is what the "not on your route" legend key points at - so the
// weight and opacity can come up without the two relevances blurring together.
function hazardStyle(f) {
  const p = f.properties || {};
  const c = hazardColor(p);
  return p.relevant
    ? { color: c, weight: 2, opacity: 0.9, fillColor: c, fillOpacity: 0.18 }
    : { color: c, weight: 2, opacity: 0.8, fillColor: c, fillOpacity: 0.12, dashArray: "4 4" };
}

function hazardPopup(p) {
  const bits = [];
  if (p.band_label && p.band_label !== "no altitude given") bits.push(p.band_label);
  if (p.distance_nm === 0) bits.push("on your route");
  else if (p.distance_nm > 0) bits.push(`${Math.round(p.distance_nm)} NM off track`);
  const exp = expiryState(p.valid_from, p.valid_to);
  if (exp) bits.push(exp.text);
  if (!p.relevant && p.drop_label) bits.push(p.drop_label);
  const head = [p.kind, p.hazard_label, p.severity].filter(Boolean).join(" · ");
  // A PIREP has no validity to run "until", only a moment it was filed, so the
  // popup above showed it no time at all. Age is the whole question with a
  // PIREP: the air one aircraft flew through an hour ago has moved on.
  const age = p.kind === "PIREP" ? pirepAgeChip(p.valid_from) : "";
  return `<div class="hz-pop"><strong>${escapeHtml(head)}</strong>${age}
    <div class="hint">${escapeHtml(bits.join(" · "))}</div>
    <pre>${escapeHtml((p.text || "").trim())}</pre>
    ${p.source_url ? `<a href="${p.source_url}" target="_blank" rel="noopener">${escapeHtml(p.source || "source")} ↗</a>` : ""}</div>`;
}

// A PIREP is a point, and it cannot be drawn the way an area is. Leaflet
// re-applies the layer's `style` function *after* `pointToLayer` (addData calls
// resetStyle), so the polygon fill values always won: a set-aside PIREP came out
// a 6 px circle at 6% fill behind a dashed 1 px outline, over an OSM tile and a
// 70%-opacity radar overlay. That is invisible, which is exactly how it looked.
// Points get their own style - solid, filled, dark-rimmed for contrast, and
// legible at both relevances, because one aircraft's report of moderate icing 40
// nm off track is still the most interesting thing on the map.
function pirepStyle(f) {
  const c = hazardColor(f.properties || {});
  return (f.properties || {}).relevant
    ? { color: "#0b0f14", weight: 2, opacity: 0.95, fillColor: c, fillOpacity: 0.95 }
    : { color: "#0b0f14", weight: 1, opacity: 0.7, fillColor: c, fillOpacity: 0.6 };
}

function hazardLayers(gj) {
  const make = (filter, style) => L.geoJSON(gj, {
    filter,
    style,
    // Radius only. Anything else set here is overwritten by `style` above.
    pointToLayer: (f, latlng) => L.circleMarker(latlng, { radius: 7 }),
    onEachFeature: (f, layer) => layer.bindPopup(hazardPopup(f.properties || {}),
                                                 { maxWidth: 360 }),
  });
  return {
    areas: make((f) => f.geometry && f.geometry.type !== "Point", hazardStyle),
    pireps: make((f) => f.geometry && f.geometry.type === "Point", pirepStyle),
  };
}

// The button that swaps between the two framings. Added after the layers exist
// rather than written into the panel template, because whether it is worth
// offering at all is only known once the areas have bounds to compare.
function addHazardFitControl() {
  const types = $("#route-radar .radar-types");
  if (!types || $("#radar-fit")) return;
  const b = document.createElement("button");
  b.type = "button";
  b.id = "radar-fit";
  b.className = "radar-fit";
  b.textContent = "Fit hazards";
  b.title = "Zoom out to the SIGMET / AIRMET areas that were fetched but sit off your route";
  b.addEventListener("click", () => {
    if (!RADAR.map) return;
    const showing = b.dataset.showing === "1";
    const target = showing ? RADAR.routeView : RADAR.hazardView;
    if (!target) return;
    // Only the route framing is capped - the whole point of the other one is to
    // pull back far enough to see the areas, however far back that is.
    RADAR.map.fitBounds(target, showing ? { maxZoom: 8 } : undefined);
    b.dataset.showing = showing ? "" : "1";
    b.textContent = showing ? "Fit hazards" : "Fit route";
    b.classList.toggle("active", !showing);
  });
  types.appendChild(b);
}

function hazardLegend(gj) {
  const seen = [];
  (gj.features || []).forEach((f) => {
    const p = f.properties || {};
    const label = p.hazard_label || p.hazard;
    if (label && !seen.some((s) => s.label === label)) seen.push({ label, color: hazardColor(p) });
  });
  if (!seen.length) return "";
  // A lone dot needs saying: it is one aircraft's report, not a forecast area.
  const dots = (gj.features || []).some((f) => f.geometry && f.geometry.type === "Point")
    ? `<span class="hz-key hz-key-dot"><i></i>PIREP (one aircraft's report)</span>` : "";
  return `<div class="hz-legend">${seen.map((s) =>
    `<span class="hz-key"><i style="background:${s.color}"></i>${escapeHtml(s.label)}</span>`).join("")}
    <span class="hz-key hz-key-dash"><i></i>not on your route</span>${dots}</div>`;
}

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
  RADAR = { map: null, wms: null, frames: [], idx: 0, layer: RADAR.layer || "RADAR_1KM_RRAI",
            timer: null, routeView: null, hazardView: null };
}

// The map takes a list of aerodromes rather than a route result, because a
// circuit has one of them and a route has two. Everything below - the radar
// animation, the hazard layers, the legend, the layer control - is the same
// picture either way; only the course line and the second marker are a route's.
function routeStops(r) {
  return [r.departure.airport, r.destination.airport];
}
function circuitStop(r) {
  return [r.airport];
}

async function loadRadar(r, stops) {
  const host = $("#route-radar");
  if (!host) return;
  if (typeof L === "undefined") { host.innerHTML = radarFallback(); return; }
  const pts = (stops || routeStops(r)).filter(Boolean);
  if (!pts.length) return;
  const midLat = pts.reduce((s, p) => s + p.lat, 0) / pts.length;
  const midLon = pts.reduce((s, p) => s + p.lon, 0) / pts.length;
  const hazardsGeo = r.hazards_geojson && (r.hazards_geojson.features || []).length
    ? r.hazards_geojson : null;
  host.innerHTML = `<div class="panel radar-panel">
    <div class="radar-head">
      <h3>Radar${hazardsGeo ? " &amp; hazards" : ""} <span class="hint">Environment Canada · last 3 h</span></h3>
      <div class="radar-types">
        ${Object.entries(RADAR_LABELS).map(([k, v]) =>
          `<button class="radar-type ${k === RADAR.layer ? "active" : ""}" data-layer="${k}">${v}</button>`).join("")}
      </div>
    </div>
    <div id="radar-map" class="radar-map"></div>
    ${hazardsGeo ? hazardLegend(hazardsGeo) : ""}
    <div class="radar-controls">
      <button id="radar-play" class="radar-play" title="Play / pause">▶</button>
      <input type="range" id="radar-slider" min="0" max="0" value="0" />
      <span id="radar-time" class="radar-time hint">-</span>
    </div>
  </div>`;
  destroyRadar();
  RADAR.map = L.map("radar-map", { scrollWheelZoom: false }).setView([midLat, midLon], 7);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    { maxZoom: 11, attribution: "© OpenStreetMap" }).addTo(RADAR.map);
  const seen = new Set();
  pts.forEach((p) => {
    if (seen.has(p.ident)) return;
    seen.add(p.ident);
    L.marker([p.lat, p.lon]).addTo(RADAR.map).bindTooltip(p.ident, { permanent: false });
  });
  RADAR.wms = L.tileLayer.wms(GEOMET_WMS, {
    layers: RADAR.layer, format: "image/png", transparent: true, version: "1.3.0", opacity: 0.7,
  }).addTo(RADAR.map);
  // The course line. A hazard polygon means nothing without the track it does
  // or does not cross, and the map used to show only the two end markers. A
  // circuit has no track to draw - the single marker is the whole flight.
  if (seen.size > 1) {
    L.polyline(pts.map((p) => [p.lat, p.lon]),
      { color: "#e8eef7", weight: 2, opacity: 0.8, dashArray: "6 4" }).addTo(RADAR.map);
  }
  if (hazardsGeo) {
    const layers = hazardLayers(hazardsGeo);
    layers.areas.addTo(RADAR.map);
    layers.pireps.addTo(RADAR.map);
    L.control.layers(null, {
      "SIGMET / AIRMET areas": layers.areas,
      "PIREPs": layers.pireps,
    }, { collapsed: false, position: "topright" }).addTo(RADAR.map);
    // Show the route first, then widen far enough to keep the PIREPs in frame.
    // The route bounds alone left a report 40 nm off track drawn but off-screen,
    // which is the same as not drawing it. PIREPs are capped at
    // ``pirep_corridor_nm`` from the route, so this can never run away; areas
    // are national and deliberately still do not get a vote.
    //
    // A single aerodrome has no extent of its own, so the pad has nothing to
    // work on - give it a corridor's worth of margin to open out from.
    const view = seen.size > 1
      ? L.latLngBounds(pts.map((p) => [p.lat, p.lon])).pad(0.4)
      : L.latLngBounds([pts[0].lat, pts[0].lon], [pts[0].lat, pts[0].lon]).pad(0.4)
        .extend([pts[0].lat + 0.85, pts[0].lon + 1.15])
        .extend([pts[0].lat - 0.85, pts[0].lon - 1.15]);
    const pireps = layers.pireps.getBounds();
    if (pireps.isValid()) view.extend(pireps);
    RADAR.routeView = view;
    RADAR.map.fitBounds(view, { maxZoom: 8 });

    // Areas still do not get a vote above, for the reason given there. But an
    // area drawn off-screen is an area that was not drawn, and that is how a
    // convective SIGMET 90 nm off track came to be on this map and invisible.
    // The compromise is a button: the route keeps the framing it had, and the
    // wider view is one press away when there is actually something out there
    // to widen for. Everything reaching the map is inside ``NEARBY_NM`` of the
    // route already, so this cannot open out to the whole continent.
    const areas = layers.areas.getBounds();
    if (areas.isValid() && !view.contains(areas)) {
      // extend() mutates, and `view` is the route framing we must be able to
      // come back to - so widen a copy.
      RADAR.hazardView = L.latLngBounds(view.getSouthWest(), view.getNorthEast()).extend(areas);
      addHazardFitControl();
    }
  }
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
  startRunTimer("run-timer");
  let ok = false;
  clearRoute();
  try {
    const params = new URLSearchParams({ dep, dest, mode: currentMode(), threats: threatsParam(), flight_rules: currentFlightRules(), ...prefsParam(), ...tasParam(), ...etdParam() });
    const res = await fetch(`/api/route?${params}`);
    // A 404 means those idents aren't in the dataset - a real answer. Anything
    // else (500, gateway timeout, dropped connection) is a failed fetch, and
    // reporting it as "unknown departure" sent the pilot to fix a typo that was
    // never there.
    if (res.status === 404) { $("#route-verdict").innerHTML = `<div class="empty">Unknown departure or destination.</div>`; return; }
    if (!res.ok) { setHealth("route-data-health", fetchFailedBanner(`HTTP ${res.status}`, "runRoute")); return; }
    const data = await res.json();
    renderRoute(data);
    stampDataTime();
    ok = true;
    // Deliberately outside the timer: the GFA imagery is not awaited, so the
    // number means "the assessment is on screen", not "every panel has drawn".
    loadGfa(dep, dest, (data.window || {}).etd_utc);
  } catch (e) {
    setHealth("route-data-health", fetchFailedBanner(String(e), "runRoute"));
  } finally {
    btn.disabled = false; btn.textContent = "Assess route";
    stopRunTimer("run-timer", ok);
  }
}

async function runCircuits() {
  const aerodrome = ($("#circ-aerodrome").value.trim() || baseIdent()).toUpperCase();
  const btn = $("#run-route"); btn.disabled = true; btn.textContent = "Pulling data…";
  startRunTimer("run-timer");
  let ok = false;
  clearRoute();
  try {
    const params = new URLSearchParams({ aerodrome, mode: currentMode(), threats: threatsParam(), flight_rules: currentFlightRules(), ...prefsParam(), ...etdParam() });
    const res = await fetch(`/api/circuits?${params}`);
    if (res.status === 404) { $("#route-verdict").innerHTML = `<div class="empty">Unknown aerodrome.</div>`; return; }
    if (!res.ok) { setHealth("route-data-health", fetchFailedBanner(`HTTP ${res.status}`, "runRoute")); return; }
    renderCircuits(await res.json());
    stampDataTime();
    ok = true;
  } catch (e) {
    setHealth("route-data-health", fetchFailedBanner(String(e), "runRoute"));
  } finally {
    btn.disabled = false; btn.textContent = "Assess circuits";
    stopRunTimer("run-timer", ok);
  }
}

function renderCircuits(r) {
  const v = r.verdict;
  const frLabel = currentFlightRules() === "ifr" ? " · IFR" : " · VFR";
  setHealth("route-data-health", dataHealthBanner(r.data_health, "runRoute"));
  $("#route-verdict").innerHTML = `<div class="verdict-banner ${cls(v)}">${r.airport.ident} circuits: <span class="vb-state">${v}</span> now${frLabel}</div>`;
  $("#route-checklist").innerHTML = checklistGroups(r, "(circuit minimums)");
  $("#route-advisories").innerHTML = advisoriesBlock(r);
  $("#route-mitigation").innerHTML = v === "MITIGATE" ? mitigationBlock(r.threat_checks) : "";
  const etdVal = ($("#etd") || {}).value;
  $("#route-endpoints").innerHTML = endpointCard(
    r, "Aerodrome", etdVal && etdVal !== "now" ? `your ETD ${zHM(etdVal)}` : "");
  // Circuits carries the same hazard GeoJSON the route does, so it gets the same
  // map - one marker instead of two, and no course line.
  loadRadar(r, circuitStop(r));
}

// Both endpoint cards mark the same span, so they get the same label: the TAF
// highlight answers "what will I fly through", not "what is it doing there at
// one instant".
function winLabel(win) {
  // Plain text: this is handed to `tafBlock`, which escapes it - `zText` there
  // raises the +N.
  return win ? `your flight (${zRange(win.etd_utc, win.eta_utc)})` : "";
}

function clearRoute() {
  // route-self-check is a standing pre-check rendered on load - never cleared here,
  // so the pilot's ticked items survive a route assessment.
  if (typeof destroyRadar === "function") destroyRadar();  // tear down any live Leaflet map
  // Circuits fills only some of these, so every mount a route run can write has
  // to be listed - otherwise route-then-circuits leaves the previous flight's
  // panel on screen under the new verdict.
  ["route-data-health", "route-verdict", "route-summary", "route-etd-suggestion", "route-checklist", "route-advisories", "route-mitigation", "route-endpoints", "route-enroute", "route-gfa", "route-radar", "route-windows", "route-timeline"]
    .forEach((id) => ($("#" + id).innerHTML = ""));
}

function renderRoute(r) {
  const v = r.verdict_now;
  const frLabel = currentFlightRules() === "ifr" ? " · IFR" : " · VFR";
  const win = r.window;
  const when = win && !win.is_now ? `at ${zHM(win.etd_utc)}` : "now";
  const notes = (win && win.notes.length)
    ? `<small>${win.notes.map(escapeHtml).join(" · ")}</small>` : "";
  setHealth("route-data-health", dataHealthBanner(r.data_health, "runRoute"));
  $("#route-verdict").innerHTML = `<div class="verdict-banner ${cls(v)}">${r.departure.airport.ident} → ${r.destination.airport.ident}: <span class="vb-state">${v}</span> ${when}${frLabel}${notes}</div>`;
  // Directly under the verdict: the nudge that reaches a GO first, then the
  // "you could do better" options, which are subordinate to it by definition -
  // one is about becoming legal, the other about a flight that already is.
  $("#route-etd-suggestion").innerHTML =
    etdNudgeCard(r.etd_suggestion) + etdOptionsCard(r.etd_options, v);
  $("#route-checklist").innerHTML = checklist(r);
  $("#route-mitigation").innerHTML = v === "MITIGATE" ? mitigationBlock(r.threat_checks) : "";

  const alt = r.altitude;
  $("#route-summary").innerHTML = `<div class="panel meta">
      ${win ? `<span title="Conditions are assessed for this window">ETD ${zHM(win.etd_utc)} → ETA ${zEnd(win.etd_utc, win.eta_utc)}${win.eta_provisional ? " (est.)" : ""}</span>` : ""}
      <span><span class="mk">Dist</span> ${r.distance_nm} nm · course ${dirM(r.bearing_mag, r.bearing_true)}</span>
      <span><span class="mk">Time</span> ${fmtHrMin(r.flight_time_hr)}</span>
      ${alt ? `<span title="best cruising altitude for the winds aloft - VFR is kept ≥500 ft below every ceiling on this page (both ends, enroute, and what the TAF forecasts for your window); IFR is not gated on cloud"><span class="mk">Best alt</span> ${fmtFt(alt.altitude_ft)} · GS ${Math.round(alt.groundspeed_kt)} kt (${alt.headwind_kt >= 0 ? "head" : "tail"}wind ${Math.abs(alt.headwind_kt)} kt)</span>` : ""}
      ${daylightSpan(r.daylight_margin)}
      ${r.enroute_ceiling_ft != null ? `<span><span class="mk">Enroute ceiling</span> ${fmtCeil(r.enroute_ceiling_ft)}</span>` : ""}
      ${topsSpan(r)}
      ${r.cloud_at_cruise ? `<span class="warn">Cloud below planned cruise altitude</span>` : ""}
      ${alt && alt.levels.length ? `<span>Winds aloft: ${alt.levels.map((l) => `${fmtFt(l.altitude_ft)} ${windDir(l.direction_mag, l.direction_true)}/${Math.round(l.speed_kt)}`).join(" · ")}</span>` : ""}
    </div>`;

  $("#route-advisories").innerHTML = advisoriesBlock(r);
  $("#route-endpoints").innerHTML =
    endpointCard(r.departure, "Departure", winLabel(win)) +
    endpointCard(r.destination, "Destination", winLabel(win));
  $("#route-enroute").innerHTML = enrouteBlock(r);
  loadRadar(r, routeStops(r));

  // The ETD suggestions now render under the verdict chip (see above), so this
  // block is purely the three window states.
  let windowsHtml;
  if (r.best_windows.length) {
    windowsHtml = `<h3>Best windows (next ${CONFIG.timeline_hours} h) - wind, ceiling &amp; visibility</h3>` +
      r.best_windows.map((w) => `<div class="window-card"><strong>${fmtRange(w.start, w.end)}</strong> - ${w.summary}</div>`).join("");
  } else if ((r.timeline || []).length) {
    // Windows are filtered to those long enough to hold this flight, so say so:
    // otherwise a pilot who saw a window on a shorter leg wonders where it went.
    windowsHtml = `<div class="empty">No clearly favourable window long enough for this flight in the next ${CONFIG.timeline_hours} h.</div>`;
  } else {
    // No timeline means no hourly forecast came back - "no favourable window" is
    // a statement about the weather, and there is no weather here to make it
    // about. This is the sentence that used to be wrong.
    windowsHtml = `<div class="empty fetch-empty">Best windows unavailable - the hourly forecast did not download, so none could be searched for. This is <strong>not</strong> "no good window": pull the data again.</div>`;
  }
  $("#route-windows").innerHTML = `<div class="timeline-wrap">${windowsHtml}</div>`;
  renderTimeline(r.timeline, r.best_windows);
}

// One checklist group. Rows that need attention (failed, or advisory) show by
// default; passing and not-applicable rows collapse behind an expander, so the
// page leads with what's actually wrong instead of ~24 uniform rows.
function clGroup(title, hint, rows, opts = {}) {
  const row = opts.row || rowCheck;
  const needsEye = (c) => c.applicable !== false && (!c.passed || c.advisory);
  const shown = rows.filter(needsEye);
  const hidden = rows.filter((c) => !needsEye(c));
  const head = `<h3>${title}${hint ? ` <span class="hint">${hint}</span>` : ""}${opts.badge || ""}</h3>`;
  const body = shown.length
    ? shown.map(row).join("")
    : `<div class="cl-clean">✓ All ${hidden.length} checks pass</div>`;
  const more = hidden.length
    ? `<details class="cl-more"><summary>${shown.length ? `${hidden.length} checks passed` : `show the ${hidden.length} passing checks`}</summary>${hidden.map(row).join("")}</details>`
    : "";
  return `<div class="cl-group">${head}${body}${more}</div>`;
}

// Threat rows use "present" rather than "passed", so they get their own filter.
function threatGroup(threats, label) {
  const n = threats.filter((t) => t.present).length;
  const shown = threats.filter((t) => t.present);
  const hidden = threats.filter((t) => !t.present);
  const badge = ` <span class="badge ${cls(labelVerdict(label))}">${n} present → ${label}</span>`;
  const body = shown.length
    ? shown.map(rowThreat).join("")
    : `<div class="cl-clean">✓ None of the ${hidden.length} threats present</div>`;
  const more = hidden.length
    ? `<details class="cl-more"><summary>${shown.length ? `${hidden.length} not present` : `show the ${hidden.length} threats checked`}</summary>${hidden.map(rowThreat).join("")}</details>`
    : "";
  return `<div class="cl-group"><h3>Two-trigger threat stack${badge}</h3>${body}${more}</div>`;
}

function checklistGroups(r, condHint) {
  const cond = r.limit_checks.filter((c) => c.group === "conditions");
  const wx = r.limit_checks.filter((c) => c.group === "weather");
  const label = r.threat_result_label || stackWord(r.threat_checks.filter((t) => t.present).length);
  return `<div class="panel checklist">
    ${clGroup("Hard limits - conditions", condHint, cond)}
    ${clGroup("Weather", "(TAF + SIGMET/AIRMET/PIREP + model, scoped to your flight window)", wx)}
    ${threatGroup(r.threat_checks, label)}
  </div>`;
}

function checklist(r) {
  return checklistGroups(r, "(worst point on the route)");
}
const stackWord = (n) => ["Normal flight", "Mitigate carefully", "No-go solo", "No-go"][Math.min(n, 3)];
// Map the backend's result label to a badge colour (verdict driven by the
// pilot's conservatism preset, so we trust the label, not a local count).
const labelVerdict = (label) => /no-go/i.test(label) ? "NOGO" : /mitigate/i.test(label) ? "MITIGATE" : "GO";

function rowCheck(c) {
  const state = !c.applicable ? "na" : c.advisory ? "advisory" : c.passed ? "pass" : "fail";
  const mark = { pass: "✓", fail: "✗", advisory: "!", na: "–" }[state];
  // Where the value came from goes on a second, muted line under it rather than
  // inside the value cell. Three things of different weights in one cell made
  // that column's content swing by 30 characters row to row, which is what you
  // saw as a ragged edge. The TAF group still rides along with the source chip,
  // and the raw TAF line is still the chip's tooltip.
  const bits = [];
  // Some rows already name their location in the value - the crosswind row reads
  // "0 kt on RWY 05 (CYFD)" and carries location "05 (CYFD)". Saying it twice was
  // easy to miss when it was inline; on its own line it is just noise.
  if (c.location && !(c.actual_text || "").includes(c.location)) {
    bits.push(`<span class="loc">@ ${escapeHtml(c.location)}</span>`);
  }
  if (c.source && c.source !== "-") {
    const detail = c.source_detail ? ` ${zText(c.source_detail)}` : "";
    const label = `${escapeHtml(c.source)}${detail}`;
    // "CYCK METAR, 18 nm" names the report a row was decided by; the pilot's
    // next question is what that report actually said. Where we have the text,
    // the chip becomes a button opening the same popover the discovery cards
    // use - hover to preview, tap to pin, Esc to close - instead of a native
    // tooltip that a phone can never show and that truncates a long TAF.
    // The popover's own heading names the source *and* where it applies. The
    // enroute chips already carry the station ("CYCK METAR, 18 nm"), but an
    // endpoint's reads only "Observed", and a heading that doesn't say whose
    // report you are looking at is no use on a card showing three of them.
    // Built from the raw fields, not from `label`/`detail` - those are already
    // escaped for the chip, and escaping them again for the attribute would put
    // a literal "&amp;" in the heading.
    const popLabel = [c.source, c.source_detail].filter(Boolean).join(" ")
      + (c.location ? ` · ${c.location}` : "");
    bits.push(c.source_text
      ? `<button type="button" class="src-mini src-pop" data-pop-kind="REPORT"
          data-pop-text="${escapeHtml(c.source_text)}" data-pop-label="${escapeHtml(popLabel)}"
          aria-haspopup="dialog" aria-expanded="false"
          title="See the full report">${label}<span class="src-pop-caret" aria-hidden="true">▾</span></button>`
      : `<span class="src-mini">${label}</span>`);
  }
  const sub = bits.length ? `<span class="sub">${bits.join(" ")}</span>` : "";
  return `<div class="chk ${state}">
    <span class="mark">${mark}</span>
    <span class="lbl">${zText(c.label)}</span>
    <span class="val"><span class="act">${zText(c.actual_text)}</span>${sub}</span>
    <span class="lim">${zText(c.limit_text)}</span></div>`;
}
function rowThreat(t) {
  // Same four cells as rowCheck, so threat rows share the checklist's columns.
  // The empty limit cell is deliberate: it holds the column open.
  return `<div class="chk ${t.present ? "fail" : "pass"}"><span class="mark">${t.present ? "✗" : "✓"}</span><span class="lbl">${escapeHtml(t.label)}</span><span class="val"><span class="act">${t.present ? "present" : "-"}</span></span><span class="lim"></span></div>`;
}

// ---------- Wind-vs-runway diagram ----------
// A small NORTH-UP graphic: the runway is drawn at its real compass orientation
// (RWY 09 lies along the 9-/3-o'clock axis, "N" marks the top) with two component
// arrows - green headwind down the runway axis and a crosswind across it - so a
// pilot can read runway orientation, where the wind is coming from, and whether the
// crosswind is from the left or right at a glance. Frontend-only: every value comes
// from the API. Component magnitudes reuse the backend headwind_kt/crosswind_kt so
// the arrows match the printed text; the signed wind angle (computed here from true
// bearings, mirroring app/services/runway.py) decides the crosswind side and whether
// it's a severe (mostly-crosswind) situation.
const XW_SEVERE_DEG = 60; // wind more than this far off the runway => severe crosswind (red)
const svgNum = (n) => Math.round(n * 10) / 10; // trim coordinate precision

// What the diagram actually draws for this runway in this wind: which component
// arrows appear, and in which state (a headwind or a tailwind, a routine
// crosswind or a severe one). Both the picture and its key read this one answer,
// so the key can only ever name arrows that are on the picture. Returns null for
// the two windless cases the diagram draws as a word instead of vectors.
function windVectors(rwy, w) {
  if (!rwy || !w) return null;
  const wind = w.wind_kt;
  if (wind == null || wind < 1 || w.wind_dir_true == null) return null;  // CALM / VRB
  // Side and severity from true bearings (variation cancels, matches the backend).
  const delta = ((w.wind_dir_true - rwy.heading_true + 180) % 360 + 360) % 360 - 180;
  const head = Math.round(rwy.headwind_kt);   // negative = tailwind
  const xw = Math.round(rwy.crosswind_kt);
  return {
    delta, wind, head, xw,
    tail: head < 0,
    severe: Math.abs(delta) > XW_SEVERE_DEG,
    hasHead: Math.abs(head) >= 1,
    hasCross: xw >= 1,
  };
}

function windRunwaySvg(rwy, w, opts = {}) {
  try {
    if (!rwy || !w) return "";
    const ident = rwy.runway_ident || rwy.ident || "";
    // Orient by magnetic heading so the picture matches the runway number (09 = E-W).
    const H = rwy.heading_mag != null ? rwy.heading_mag : rwy.heading_true;
    if (H == null) return "";
    const compact = !!opts.compact;
    const S = compact
      ? { vb: 96,  Lr: 23, rw: 7,  maxArrow: 30, pad: 13, font: 9,  comp: 2.3, labels: true }
      : { vb: 144, Lr: 34, rw: 12, maxArrow: 42, pad: 17, font: 12, comp: 3.4, labels: true };
    const cx = S.vb / 2, cy = S.vb / 2;

    // North-up unit vectors (screen y points down). Bearing th -> (sin, -cos).
    const h = H * Math.PI / 180;
    const ux = Math.sin(h), uy = -Math.cos(h);   // landing direction (toward the active end)
    const prx = Math.cos(h), pry = Math.sin(h);   // right of the landing direction

    // Runway strip drawn through the centre at its true bearing, with the active
    // (selected ident) end at +u and its reciprocal at -u.
    const ax = cx + ux * S.Lr, ay = cy + uy * S.Lr;
    const bx = cx - ux * S.Lr, by = cy - uy * S.Lr;
    const parts = [
      `<line class="wr-strip-line" x1="${svgNum(bx)}" y1="${svgNum(by)}" x2="${svgNum(ax)}" y2="${svgNum(ay)}" stroke-width="${S.rw}"/>`,
      `<line class="wr-center" x1="${svgNum(bx)}" y1="${svgNum(by)}" x2="${svgNum(ax)}" y2="${svgNum(ay)}"/>`,
      `<text class="wr-n" x="${cx}" y="${S.font + 1}" font-size="${S.font - 1}">N</text>`,
    ];
    // The runway designator is painted at the APPROACH end (opposite the direction
    // you travel), so the active ident sits at -u and its reciprocal at +u.
    if (ident) parts.push(`<text class="wr-ident" x="${svgNum(cx - ux * (S.Lr + S.pad))}" y="${svgNum(cy - uy * (S.Lr + S.pad) + S.font / 3)}" font-size="${S.font}">${escapeHtml(ident)}</text>`);
    const recipNum = parseInt(ident, 10);
    if (!Number.isNaN(recipNum)) {
      const recip = String((recipNum + 18) % 36 || 36).padStart(2, "0");
      parts.push(`<text class="wr-ident wr-recip" x="${svgNum(cx + ux * (S.Lr + S.pad))}" y="${svgNum(cy + uy * (S.Lr + S.pad) + S.font / 3)}" font-size="${S.font}">${recip}</text>`);
    }

    const wrap = (label) => `<svg class="wr${compact ? " wr-sm" : ""}" viewBox="0 0 ${S.vb} ${S.vb}" role="img" aria-label="${escapeHtml(label)}">${parts.join("")}</svg>`;
    const wind = w.wind_kt;

    if (wind == null || wind < 1) {
      parts.push(`<text class="wr-calm" x="${cx}" y="${cy + S.font / 3}" font-size="${S.font}">CALM</text>`);
      return wrap(`Runway ${ident}: winds calm`);
    }
    if (w.wind_dir_true == null) {
      parts.push(`<text class="wr-calm" x="${cx}" y="${cy + S.font / 3}" font-size="${S.font}">VRB</text>`);
      return wrap(`Runway ${ident}: wind variable`);
    }

    const { delta, head, xw, severe, tail, hasHead, hasCross } = windVectors(rwy, w);
    const fromRight = delta > 0;
    // Component arrows grow with strength but keep a generous floor so a light
    // wind still draws a full, legible arrow (not a tiny stub) - the corner
    // number carries the exact value.
    const pxPerKt = S.maxArrow / 18;
    const arm = (kt) => Math.min(Math.max(Math.abs(kt) * pxPerKt, S.maxArrow * 0.82), S.maxArrow);
    // Aviation-style "12G18" when a gust component is present (gust > steady).
    const gustTxt = (base, gust) => gust != null ? `${base}G${Math.round(Math.abs(gust))}` : `${base}`;

    // Total wind: a dashed arrow that flies WITH the wind (tail upwind where it
    // comes from, head downwind where it's blowing to) so the direction reads at
    // a glance, then decomposed into the head/cross components below.
    const wb = (H + delta) * Math.PI / 180;        // wind FROM bearing, drawn frame
    const fvx = Math.sin(wb), fvy = -Math.cos(wb); // unit vector toward the source
    // The total-wind arrow is the bold one; the component arrows are full length
    // but drawn with a lighter stroke so the picture stays uncluttered.
    const Rw = S.Lr * 0.95;
    const windW = S.comp * 0.95, compW = S.comp * 0.6;
    parts.push(`<line class="wr-wind" x1="${svgNum(cx + fvx * Rw)}" y1="${svgNum(cy + fvy * Rw)}" x2="${svgNum(cx - fvx * Rw)}" y2="${svgNum(cy - fvy * Rw)}" stroke-width="${svgNum(windW)}" marker-end="url(#wr-arrow-wind)"/>`);
    // A hub at the centre makes it clear the component arrows share one origin.
    parts.push(`<circle class="wr-hub" cx="${cx}" cy="${cy}" r="${svgNum(S.comp * 0.6)}"/>`);

    const hsign = head >= 0 ? -1 : 1;  // headwind points to the approach end (-u)
    const xsign = fromRight ? -1 : 1;  // wind from the right pushes the aircraft left
    const hlen = arm(head), xlen = arm(xw);

    // The three numbers (head / cross / total wind) are parked in the diagram's
    // diagonal corners, which sit clear of the on-axis runway idents and of each
    // other - so labels never overlap, whatever the wind angle. Head and cross
    // take opposite corners; the wind speed takes whichever of the two remaining
    // corners is nearer the arrow's tail.
    const Rlbl = S.Lr + S.font * 1.1;
    const corner = (su, sp) => {
      const dx = (su * ux + sp * prx) / Math.SQRT2, dy = (su * uy + sp * pry) / Math.SQRT2;
      return [svgNum(cx + dx * Rlbl), svgNum(cy + dy * Rlbl + S.font / 3)];
    };

    // Headwind arrow (green; red for a tailwind) + its corner label.
    if (hasHead) {
      const hx = cx + hsign * ux * hlen, hy = cy + hsign * uy * hlen;
      parts.push(`<line class="wr-head${tail ? " wr-head-tail" : ""}" x1="${cx}" y1="${cy}" x2="${svgNum(hx)}" y2="${svgNum(hy)}" stroke-width="${svgNum(compW)}" marker-end="url(#${tail ? "wr-arrow-tail" : "wr-arrow-head"})"/>`);
    }
    if (S.labels && hasHead) {
      const [lx, ly] = corner(hsign, -xsign);
      parts.push(`<text class="wr-kt wr-kt-head${tail ? " wr-kt-tail" : ""}" x="${lx}" y="${ly}" text-anchor="middle">${gustTxt(Math.abs(head), rwy.headwind_kt_gust)}</text>`);
    }

    // Crosswind arrow (amber; red when severe) + its opposite-corner label.
    if (hasCross) {
      const xx = cx + xsign * prx * xlen, xy = cy + xsign * pry * xlen;
      const sev = severe ? "nogo" : "mit";
      parts.push(`<line class="wr-cross wr-sev-${sev}" x1="${cx}" y1="${cy}" x2="${svgNum(xx)}" y2="${svgNum(xy)}" stroke-width="${svgNum(compW)}" marker-end="url(#wr-arrow-cross-${sev})"/>`);
    }
    if (S.labels && hasCross) {
      const [lx, ly] = corner(-hsign, xsign);
      parts.push(`<text class="wr-kt wr-kt-cross${severe ? " wr-kt-severe" : ""}" x="${lx}" y="${ly}" text-anchor="middle">${gustTxt(xw, rwy.crosswind_kt_gust)}</text>`);
    }

    // Total wind speed (with gust) in the free corner nearest the tail (upwind).
    if (S.labels) {
      const uf = ux * fvx + uy * fvy, pf = prx * fvx + pry * fvy;
      const neg = ((-hsign) * uf + (-xsign) * pf) >= (hsign * uf + xsign * pf);
      const [lx, ly] = corner(neg ? -hsign : hsign, neg ? -xsign : xsign);
      const gust = (w.gust_kt != null && w.gust_kt > wind) ? w.gust_kt : null;
      parts.push(`<text class="wr-kt wr-kt-wind" x="${lx}" y="${ly}" text-anchor="middle">${gustTxt(Math.round(wind), gust)}</text>`);
    }

    const headTxt = head >= 0 ? `headwind ${head}` : `tailwind ${Math.abs(head)}`;
    return wrap(`Runway ${ident}: wind ${Math.round(w.wind_dir_true)} at ${Math.round(wind)} knots, ${headTxt}, crosswind ${xw} from the ${fromRight ? "right" : "left"}`);
  } catch (e) { return ""; }
}

// ---------- Wind-diagram key ----------
// The diagram says everything in colour, so the colours need a caption or the
// picture is a guess. The key sits in the text column beside the diagram (the
// space the runway line leaves empty) and names ONLY the arrows this particular
// wind put on this particular runway: no headwind row when the wind is all
// crosswind, a red "Tailwind component" row instead of the green one when the
// runway is downwind. The swatches are drawn from the diagram's own classes and
// arrowheads, off the same windVectors() answer, so a row cannot describe a
// colour that is not in the picture next to it.
function windKeyRow(cls, marker, label, note) {
  return `<svg class="wr-key-arrow" viewBox="0 0 26 10" aria-hidden="true" focusable="false">`
    + `<line class="${cls}" x1="1" y1="5" x2="17" y2="5" stroke-width="2.4" marker-end="url(#${marker})"/></svg>`
    + `<span class="wr-key-lbl">${label}${note ? ` <span class="wr-key-note">${note}</span>` : ""}</span>`;
}

function windLegend(rwy, w) {
  const v = windVectors(rwy, w);
  if (!v) return "";  // calm or variable: the diagram drew a word, not vectors
  const rows = [windKeyRow("wr-wind", "wr-arrow-wind", "Total wind component")];
  if (v.hasHead) {
    rows.push(v.tail
      ? windKeyRow("wr-head wr-head-tail", "wr-arrow-tail", "Tailwind component")
      : windKeyRow("wr-head", "wr-arrow-head", "Headwind component"));
  }
  if (v.hasCross) {
    // The severe row is the only one that says why it is the colour it is: red
    // here means the wind is mostly across the runway, which the number alone
    // does not tell you.
    rows.push(v.severe
      ? windKeyRow("wr-cross wr-sev-nogo", "wr-arrow-cross-nogo", "Crosswind component", `over ${XW_SEVERE_DEG}&deg; off the runway`)
      : windKeyRow("wr-cross wr-sev-mit", "wr-arrow-cross-mit", "Crosswind component"));
  }
  return `<div class="wr-key">${rows.join("")}</div>`;
}

function endpointCard(a, role, timeLabel) {
  const w = a.weather || {};
  const issues = a.reasons || [];
  const wind = windStr(w);
  const to = a.best_takeoff, ld = a.best_landing;
  // Departure cards show only takeoff, destination cards only landing; an
  // aerodrome (circuits) shows both.
  const showTakeoff = role !== "Destination";
  const showLanding = role !== "Departure";
  const gust = (g) => g ? ` (gust ${Math.round(Math.abs(g))})` : "";
  // The green line and the badge answer different questions, so the card says
  // both. `reasons` is failing *hard limits* only; a verdict off the threat
  // stack - 6G14 kt is a passing 8 kt gust spread and a "strong or gusty winds"
  // threat at the same time - left the card reading "✓ Within personal limits"
  // under a MITIGATE badge with nothing to reconcile the two. whyBlock names
  // what actually moved the verdict, and re-renders the failing limits itself,
  // so the reasons list is not drawn twice.
  return `<div class="card ${cls(a.verdict)}">
    <div class="card-head"><h3>${role}: ${a.airport.ident} · ${a.airport.name}</h3><span class="badge ${cls(a.verdict)}">${a.verdict}</span></div>
    ${issues.length ? "" : `<div class="ok-line">✓ Within personal limits</div>`}
    ${whyBlock(a)}
    <div class="meta obs">
      <span>${srcChip(w.source)}${w.as_of ? " " + w.as_of : ""}</span>
      <span><span class="mk">Wind</span> ${wind}</span>
      ${ceilChip(w)}
      ${w.visibility_sm != null ? `<span><span class="mk">Vis</span> ${w.visibility_sm} SM</span>` : ""}
      ${notamToggle(a)}
    </div>
    ${showTakeoff && to ? `<div class="rwy-wrap"><span class="rwy-diag">${windRunwaySvg(to, w)}</span><div class="rwy-lines"><div><strong>Takeoff</strong>: RWY ${to.runway_ident} (${dirM(to.heading_mag, to.heading_true)})${dims(to)} · headwind ${Math.round(to.headwind_kt)} kt${gust(to.headwind_kt_gust)} · xwind ${Math.round(to.crosswind_kt)} kt${gust(to.crosswind_kt_gust)}</div>${windLegend(to, w)}</div></div>` : ""}
    ${showLanding && ld ? `<div class="rwy-wrap"><span class="rwy-diag">${windRunwaySvg(ld, w)}</span><div class="rwy-lines"><div><strong>Landing</strong>: RWY ${ld.runway_ident} (${dirM(ld.heading_mag, ld.heading_true)})${dims(ld)} · headwind ${Math.round(ld.headwind_kt)} kt${gust(ld.headwind_kt_gust)} · xwind ${Math.round(ld.crosswind_kt)} kt${gust(ld.crosswind_kt_gust)}</div>${windLegend(ld, w)}</div></div>` : ""}
    ${a.nearby_station ? nearbyBlock(a.nearby_station, timeLabel) : ""}
    ${trendsBlock(a)}
    ${runwaysBlock(a)}
    <div class="links">${linksHtml(a)}</div>
    <div class="notam-list hidden" id="notams-${a.airport.ident}">${notamItems(a)}</div>
    ${obsLine(w.raw_metar)}
    ${tafBlock(w, timeLabel)}
    ${metarHistory(a)}
  </div>`;
}

// The TAF, split into its FM/BECMG/TEMPO periods, with every period the flight
// passes through highlighted green - the same ETD->ETA window on both cards, so
// "green" means one thing wherever you read it. `in_window` and `gates` are
// computed server-side so the browser never reasons about TAF validity
// arithmetic.
function tafBlock(w, timeLabel) {
  if (!w.raw_taf) return "";
  const ps = w.taf_periods || [];
  // Unparseable TAF: fall back to the raw line rather than showing nothing.
  if (!ps.length) return `<div class="raw">TAF ${escapeHtml(w.raw_taf)}</div>`;
  const label = timeLabel || "your flight";
  const covered = ps.some((p) => p.in_window);
  const advisory = ps.some((p) => p.in_window && !p.gates);
  let note;
  if (!covered) {
    note = `${zText(label)} is outside this TAF (valid ${supDays(zRange(w.taf_valid_from, w.taf_valid_to))})`;
  } else {
    note = `green = happens during ${zText(label)}`;
    if (advisory) note += " · amber edge = only a chance, not counted against your limits";
  }
  return `<details class="taf" open><summary>TAF <span class="hint">${note}</span></summary>
    ${ps.map(tafRow).join("")}
    <div class="raw taf-raw">${escapeHtml(w.raw_taf)}</div></details>`;
}

function tafRow(p) {
  // A PROB30/40 you fly through is still green - it happens during the flight -
  // but carries an amber edge, because it is a possibility to weigh rather than
  // a limit that fails the card on its own.
  const cls = [
    "taf-p",
    p.in_window ? "in" : "out",
    p.kind === "overlay" ? "overlay" : "",
    p.in_window && p.gates === false ? "prob" : "",
  ].filter(Boolean).join(" ");
  return `<div class="${cls}">
    <span class="taf-k">${escapeHtml(p.label || "")}</span>
    <span class="taf-t">${supDays(zRange(p.start, p.end))}</span>
    <span class="taf-x">${escapeHtml(p.text || "")}</span></div>`;
}

// Aerodromes within the route corridor - precautionary-landing options, shown
// collapsed by default. Purely situational: none of this gates the verdict.
function enrouteBlock(r) {
  const list = r.enroute_airports || [];
  const total = r.enroute_airports_total || list.length;
  const w = r.enroute_corridor_nm ?? 5;
  if (!list.length) {
    return `<div class="panel adv-none">No aerodromes within ${w} nm of the route.</div>`;
  }
  const more = total > list.length
    ? ` <span class="hint">(${list.length} of ${total} shown, nearest the centreline)</span>` : "";
  return `<details class="panel enroute"><summary>En-route aerodromes within ${w} nm: ${total}${more}
    <span class="hint">- precautionary options, grass and private included. Wind is HRDPS at your overfly time; these never affect your verdict.</span></summary>
    ${list.map(enrouteRow).join("")}</details>`;
}

function enrouteRow(e) {
  const rw = e.best_runway;
  const wind = e.wind_kt != null
    ? `${windDir(e.wind_dir_mag, e.wind_dir_true)}/${Math.round(e.wind_kt)}${e.gust_kt && e.gust_kt > e.wind_kt ? "G" + Math.round(e.gust_kt) : ""} kt`
    : "no wind data";
  const off = Math.abs(e.cross_track_nm).toFixed(1);
  const where = e.side === "on course" ? "on course" : `${off} nm ${e.side}`;
  return `<div class="er-row">
    <div class="er-head"><strong>${escapeHtml(e.airport.ident)}</strong> ${escapeHtml(e.airport.name)}${e.access_note ? ` <span class="ppr">${escapeHtml(e.access_note)}</span>` : ""}</div>
    <div class="er-meta">
      <span>${Math.round(e.along_track_nm)} nm along · ${where}</span>
      <span>${e.overfly_utc ? '<span class="mk">Over</span> ' + zHM(e.overfly_utc) : ""}</span>
      <span><span class="mk">Wind</span> ${wind}</span>
      ${rw ? `<span><span class="mk">Rwy</span> RWY ${rw.runway_ident}${dimsText(rw) ? " · " + dimsText(rw) : ""} · xwind ${Math.round(rw.crosswind_kt)} kt</span>`
           : `<span class="rwy-na">runway data unavailable</span>`}
    </div></div>`;
}

// ---------- "Some of this didn't download" ----------
//
// Every upstream call degrades to an empty default rather than failing the whole
// assessment, which is right - but an empty default renders exactly like good
// news. A dropped HRDPS request produced an empty timeline, which this page
// printed as "No clearly favourable window in the next 48 h": a confident claim
// about weather nobody had fetched. Pulling the data again fixed it, which is
// the tell. So the backend now reports what failed (`data_health`) and this
// draws it, loudly, above the verdict - with the button that fixes it.
//
// `retry` is the name of the global function to re-run, not the function itself:
// the banner is built as an HTML string like everything else on the page, and
// the click is wired through `window.<name>`.
function dataHealthBanner(health, retry) {
  if (!health || health.ok !== false) return "";
  const failed = health.failed || [];
  const list = failed.length
    ? `<ul class="reasons">${failed.map((f) => `<li>${escapeHtml(f)}</li>`).join("")}</ul>`
    : "";
  // Which of the seven advisory feeds actually dropped. Kept behind a fold: the
  // pilot needs to know what is missing, not which coroutine raised - but when
  // only some sources are down, "which ones" is the difference between a gap
  // and a blackout.
  const detail = (health.details || []).length
    ? `<details class="fetch-detail"><summary>Which sources</summary><ul class="reasons">${
        health.details.map((d) => `<li>${escapeHtml(d)}</li>`).join("")}</ul></details>`
    : "";
  return `<div class="fetch-fail" role="alert">
    <div class="fetch-fail-title">Failed to fetch some data</div>
    <p>These did not download, so anything below was assessed without them - and
       a missing forecast looks the same on the page as a clear sky. Pull the
       data again before you use this.</p>
    ${list}${detail}
    <button type="button" class="fetch-retry" onclick="${retry}()">Pull the data again</button>
  </div>`;
}

// The same banner for a request that never landed at all - a dropped connection,
// a 500, a body that wasn't JSON. `detail` is the browser's own reason, kept
// because "Failed to fetch" and "500" send you to different places.
function fetchFailedBanner(detail, retry) {
  return `<div class="fetch-fail" role="alert">
    <div class="fetch-fail-title">Failed to fetch</div>
    <p>The request did not complete, so nothing below is current.
       ${detail ? `<span class="fetch-fail-detail">${escapeHtml(detail)}</span>` : ""}</p>
    <button type="button" class="fetch-retry" onclick="${retry}()">Pull the data again</button>
  </div>`;
}

function setHealth(hostId, html) {
  const el = $("#" + hostId);
  if (el) el.innerHTML = html;
}

function trendsBlock(a) {
  const t = a.trends || [];
  // An empty panel used to mean both "nothing is trending" and "the observation
  // service did not answer", so trends appeared to come and go between two runs
  // of the same route. Say which, so an absent panel always means the former.
  if (!t.length) {
    return a.history_unavailable
      ? `<div class="trends-na">Trend data unavailable - the observation history service did not respond. Your verdict is unaffected.</div>`
      : "";
  }
  return `<details class="trends" open><summary>Trends from recent METARs (${t.length})</summary>${t.map((x) => `<div class="trend">${x}</div>`).join("")}</details>`;
}
function nearbyBlock(n, timeLabel) {
  // This station's TAF is the only forecast the field has, so it gets the same
  // period split and flight-window highlight as one that reports its own - but
  // it is NOT your field, and nothing in it gates the verdict. A TAF's ceiling,
  // visibility and wind describe a ~5 SM radius around its own aerodrome, so at
  // this distance they are background, not your conditions. Regional hazards
  // reach you through the GFA/SIGMET/AIRMET instead, which are area products.
  const taf = tafBlock({
    raw_taf: n.taf, taf_periods: n.taf_periods,
    taf_valid_from: n.taf_valid_from, taf_valid_to: n.taf_valid_to,
  }, timeLabel);
  const caveat = n.taf
    ? `<div class="hint nearby-caveat">Reference only - ${escapeHtml(n.ident)} is ${n.distance_nm} NM away, so this forecast does not count against your limits.</div>`
    : "";
  return `<div class="nearby"><span class="nlabel">Nearest reporting station</span> <strong>${n.ident}</strong>${n.name ? " · " + n.name : ""} - ${n.distance_nm} NM ${n.direction} of here
    ${obsLine(n.metar)}${taf}${caveat}
    ${trendsBlock(n)}${metarHistoryList(n.metar_history)}</div>`;
}
// One advisory: a collapsed one-line teaser that expands to the full product
// text plus a deep link back to the source feed it was fetched from.
function advisoryItem(a) {
  const full = (a.text || "").trim();
  // Teaser: the hazard/header before the first colon (from display_text), else
  // the first line - truncated so the summary stays one line.
  const head = full.includes(":") ? full.slice(0, full.indexOf(":")) : full.split("\n")[0];
  let teaser = (head || full).trim();
  if (teaser.length > 90) teaser = teaser.slice(0, 90) + "…";
  const link = a.source_url
    ? `<a class="adv-src" href="${a.source_url}" target="_blank" rel="noopener">source: ${escapeHtml(a.source || "feed")} ↗</a>`
    : "";
  return `<details class="adv"><summary><span class="adv-k">${escapeHtml(a.kind)}</span> ${escapeHtml(teaser)}${advisoryChips(a)}</summary><pre class="adv-text">${escapeHtml(full)}</pre>${link}</details>`;
}
// The three facts that decide whether a bulletin is yours, shown without making
// the pilot decode the bulletin: how high, how far off track, and until when.
function advisoryChips(a) {
  const chip = (c) => `<span class="adv-chip">${escapeHtml(c)}</span>`;
  const out = [];
  if (a.band_label && a.band_label !== "no altitude given") out.push(chip(a.band_label));
  if (a.distance_nm === 0) out.push(chip("on route"));
  else if (a.distance_nm > 0) out.push(chip(`${Math.round(a.distance_nm)} NM off track`));
  // Green with the time remaining while it is actually running. Built as HTML
  // rather than escaped with the rest because it carries its own class, and
  // empty for a product with no validity - a PIREP, mostly.
  out.push(expiryChip(a.valid_from, a.valid_to));
  if (a.drop_label) out.push(chip(a.drop_label));
  // A PIREP has an age rather than a validity, and it is the fact that decides
  // whether the report is still about the air you are about to fly through.
  // Appended as raw HTML because it carries its own green/red class.
  const age = a.kind === "PIREP" ? pirepAgeChip(a.valid_from) : "";
  return out.join("") + age;
}
// "9 outside your altitudes, 3 not on your route" - the line that keeps the
// empty state honest. Something was found; it just does not reach this flight,
// and that is a different sentence from "there is nothing out there".
const DROP_ORDER = ["altitude", "geometry", "time", "fir"];
const DROP_WORDS = {
  altitude: "outside your altitudes", geometry: "not on your route",
  time: "not valid during your flight", fir: "another region",
};
function filteredLine(counts) {
  const keys = DROP_ORDER.filter((k) => counts[k]);
  if (!keys.length) return "";
  const total = Object.values(counts).reduce((n, v) => n + v, 0);
  return `${total} more fetched: ${keys.map((k) => `${counts[k]} ${DROP_WORDS[k]}`).join(", ")}`;
}
function advisoriesBlock(r) {
  const items = [...(r.sigmets || []), ...(r.airmets || []), ...(r.pireps || [])];
  const nearby = r.nearby_advisories || [];
  const line = filteredLine(r.hazards_filtered || {});
  // A circuits result is a bare airport card; a route result has two ends.
  const where = r.departure ? "on the route" : "over the field";
  const aside = nearby.length
    ? `<details class="adv-aside"><summary>${escapeHtml(line || `${nearby.length} more fetched`)}</summary>${nearby.map(advisoryItem).join("")}</details>`
    : "";
  if (!items.length) {
    // Only ever reached when the fetch actually succeeded - a failed one raises
    // the data-health banner above this panel instead.
    return `<div class="panel adv-none">No active SIGMET/AIRMET/PIREP ${where}.${aside}</div>`;
  }
  return `<details class="panel advisories" open><summary>Area advisories: ${items.length} <span class="hint">(tap an item for the full text - check the altitudes, many apply only to higher levels)</span></summary>${items.map(advisoryItem).join("")}${aside}</details>`;
}
function metarHistory(a) {
  return metarHistoryList(a.metar_history);
}
function metarHistoryList(h) {
  if (!h || h.length < 2) return "";
  return `<details class="mhist"><summary>Observation history (${h.length})</summary>${h.map((m) => obsLine(m)).join("")}</details>`;
}

function runwaysBlock(a) {
  const comps = a.runway_components || [];
  if (!comps.length) return `<div class="rwy-na">Runway data unavailable</div>`;
  const w = a.weather || {};
  const usable = comps.filter((c) => c.tailwind_kt <= 0).sort((x, y) => y.headwind_kt - x.headwind_kt);
  if (!usable.length) return "";
  const gust = (g) => g ? ` (gust ${Math.round(Math.abs(g))})` : "";
  const rows = usable.map((c) =>
    `<div class="rwy-comp-row"><span class="rwy-diag-sm">${windRunwaySvg(c, w, { compact: true })}</span><div class="rwy-comp">RWY ${c.ident} ${dirM(c.heading_mag, c.heading_true)} · ${dimsText(c)} · head ${Math.round(c.headwind_kt)} kt${gust(c.headwind_kt_gust)} / xwind ${Math.round(c.crosswind_kt)} kt${gust(c.crosswind_kt_gust)}</div></div>`).join("");
  return `<details class="runways"><summary>Usable runways into wind: ${usable.length} <span class="hint">(no tailwind component)</span></summary>${rows}</details>`;
}

function linksHtml(a) {
  const out = [];
  if (a.cfs_url) out.push(`<a href="${a.cfs_url}" target="_blank" rel="noopener">CFS PDF ↗</a>`);
  if (a.info_url) out.push(`<a href="${a.info_url}" target="_blank" rel="noopener">Airport info (${a.info_label || "link"}) ↗</a>`);
  return out.join(" · ");
}

function notamToggle(a) {
  if (!a.notam_count) return `<span>0 NOTAM</span>`;
  return `<span class="notam-btn" onclick="toggleNotams('${a.airport.ident}')">${a.notam_count} NOTAM ▾</span>`;
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

// ---------- "When instead?" ----------
//
// The best-windows list answers "when is the weather good in the next 48 h".
// A pilot looking at a MITIGATE has a narrower question: how far am I from a
// GO, and is the good spell long enough for my leg. The backend answers it
// against the ETD actually picked (services/timeline.etd_nudge), and only when
// the hour-by-hour strip agrees with the card about that ETD - so this card
// speaks for the strip, and says so, rather than promising a verdict the card
// might not give.
// A time offset, read aloud: "45 min", "3 h", "1 h 15 min". fmtHrMin is the
// wrong shape here - it prints a flight time, where "1 h 0 min" is a duration
// nobody says out loud but every leg has, so it always carries the minutes.
function fmtDelta(min) {
  const h = Math.floor(min / 60), m = min % 60;
  if (!h) return `${m} min`;
  return m ? `${h} h ${m} min` : `${h} h`;
}

function etdNudgeCard(s) {
  if (!s) return "";
  const dir = s.delta_min >= 0 ? "later" : "earlier";
  const mag = fmtDelta(Math.abs(s.delta_min));
  const why = s.reason
    ? `<div class="nudge-why">Stops applying: ${escapeHtml(s.reason)}</div>` : "";
  return `<div class="window-card etd-nudge ${cls(s.verdict)}">
    <strong>Depart ${mag} ${dir} (${zDayTime(s.etd_utc)})</strong> and the
    hour-by-hour forecast turns ${s.verdict} - ${s.hours_available} h available.
    ${why}</div>`;
}

/* Why waiting helps, named rather than pictured. These sit at the head of a
   gain line, so they are set in the same condensed caps as every other label. */
const NUDGE_ICON = { tailwind: "Wind", ceiling: "Ceiling", hazard: "Wx", crosswind: "Xwind" };

// "Wait and it gets better" - offered even when the flight is already legal,
// which is the case etdNudgeCard is structurally unable to speak to. Rendered
// deliberately quieter than the nudge: this is never a reason not to go now, so
// a GO flight gets a plain advisory card with no verdict colour at all.
function etdOptionsCard(options, verdictNow) {
  if (!options || !options.length) return "";
  const cards = options.map((o) => {
    const dir = o.delta_min >= 0 ? "later" : "earlier";
    const mag = fmtDelta(Math.abs(o.delta_min));
    const gains = o.improvements
      .map((i) => `<li>${NUDGE_ICON[i.kind] || "•"} ${escapeHtml(i.text)}</li>`)
      .join("");
    return `<div class="window-card etd-option">
      <strong>Depart ${mag} ${dir} (${zDayTime(o.etd_utc)})</strong>
      - ${o.hours_available} h available
      <ul class="nudge-gains">${gains}</ul></div>`;
  }).join("");
  // On a GO the heading has to make clear this is an option, not a caveat.
  const head = verdictNow === "GO"
    ? "Already good to go - waiting would buy you:"
    : "Waiting would also improve:";
  return `<div class="etd-options"><small>${head}</small>${cards}</div>`;
}

// Daylight left at the destination on arrival. The app already computes civil
// twilight to choose day or night minimums; this is that instant, stated, plus
// the departure time that would still land inside it. The latest ETD only
// appears when the margin is tight enough for it to be a decision.
function daylightSpan(m) {
  if (!m) return "";
  // Only for a pilot who counts night operations as a threat. Someone who has
  // deliberately turned that off is night-current and equipped, and a countdown
  // to last light is one more line for them to read past on the day it matters.
  // The "arrival is a night landing" note the window still carries is the
  // backstop, and it is a different sentence from a margin.
  if (!effectiveLimits().night_as_threat) return "";
  const late = m.margin_min < 0;
  const state = late
    ? `lands ${fmtDelta(Math.abs(m.margin_min))} after dark`
    : `${fmtDelta(m.margin_min)} of daylight left after landing`;
  // Bare Zulu, like the ETD and ETA it sits beside in this strip. The latest
  // departure is always within hours of the dusk it is derived from, so the
  // weekday the nudge card carries would be noise here.
  const latest = m.margin_min < 60
    ? ` · latest ETD ${zHM(m.latest_etd_utc)}` : "";
  const title = "End of evening civil twilight at the destination (CARs 101.01). "
    + "The latest ETD keeps the same 30 min arrival allowance the flight window uses.";
  return `<span class="${m.margin_min < 30 ? "warn" : ""}" title="${title}">Last light ${zHM(m.dusk_utc)} · ${state}${latest}</span>`;
}

function renderTimeline(timeline, windows) {
  // An empty grid used to be drawn as no grid at all, silently - the same blank
  // space you'd get from a page that simply hadn't got there yet. Say why.
  if (!timeline.length) {
    $("#route-timeline").innerHTML = `<div class="timeline-wrap"><div class="empty fetch-empty">Hour-by-hour forecast unavailable - the HRDPS data did not download. Pull the data again.</div></div>`;
    return;
  }
  const inWindow = (t) => windows.some((w) => t >= w.start && t <= w.end);
  const byDay = {};
  timeline.forEach((h) => { (byDay[h.time.slice(0, 10)] ||= []).push(h); });
  let html = `<div class="timeline-wrap"><h3>Hour-by-hour, Zulu (full decision card; worse of departure &amp; destination)</h3>
    <div class="legend"><span class="go">GO</span><span class="mit">MITIGATE</span><span class="nogo">NO-GO</span><span>· all times Zulu · dimmed = night · outlined = best window · amber edge = only a chance, not counted against your limits · TS storm · FZRA freezing · SN snow · RA rain</span></div>`;
  for (const day of Object.keys(byDay).sort()) {
    const label = (utcDate(day + "T12:00") || new Date()).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
    html += `<div class="tl-day">${label} (Z)</div><div class="tl-row">`;
    for (const h of byDay[day]) {
      const hour = h.time.slice(11, 13);
      const title = [
        `${h.time.replace("T", " ")}Z  ${h.verdict}`,
        h.wind_kt != null ? `wind ${windDir(h.wind_dir_mag, h.wind_dir_true)}/${Math.round(h.wind_kt)}${(h.gust_kt && h.gust_kt > h.wind_kt) ? "G" + Math.round(h.gust_kt) : ""} kt${h.wind_source ? " from " + h.wind_source : ""}` : "",
        h.crosswind_kt != null ? `xwind ${h.crosswind_kt} kt${h.crosswind_runway ? " on RWY " + h.crosswind_runway : ""}` : "",
        h.ceiling_agl_ft != null ? `ceiling ${(Math.round(h.ceiling_agl_ft / 100) * 100).toLocaleString()} ft` : "",
        h.visibility_sm != null ? `vis ${h.visibility_sm} SM` : "",
        precipText(h),
        h.hazards.length ? "hazards: " + h.hazards.join(",") : "",
        `[${h.source}]`, ...h.reasons,
        // A 30-40% chance is shown but never counted, exactly as the route card
        // treats it - so say plainly that it did not move this hour's verdict.
        h.prob ? `${h.prob} - only a chance, not counted against your limits` : "",
      ].filter(Boolean).join("\n");
      const klass = `${cls(h.verdict)}${h.daylight ? "" : " night"}${inWindow(h.time) ? " best" : ""}${h.prob ? " prob" : ""}`;
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
  startRunTimer("discovery-timer");
  let ok = false;
  $("#discovery-results").innerHTML = "";
  setHealth("discovery-data-health", "");
  closeWxPop();
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
    Object.assign(p, prefsParam(), tasParam(), etdParam("#d-etd"));
    const params = new URLSearchParams(p);
    const res = await fetch(`/api/suggest?${params}`);
    // This used to go straight to .json(): a 500 surfaced as "Error: SyntaxError"
    // in the results list, which reads like a bug in the app rather than a
    // failed download you can retry.
    if (!res.ok) { setHealth("discovery-data-health", fetchFailedBanner(`HTTP ${res.status}`, "runDiscovery")); return; }
    const payload = await res.json();
    const data = payload.results || [];
    setHealth("discovery-data-health", dataHealthBanner(payload.data_health, "runDiscovery"));
    // Keyed by ident so the weather popovers can read the assessment itself,
    // rather than the card having to carry a copy of it in an attribute.
    DISCOVERY_BY_IDENT = Object.fromEntries(data.map((a) => [a.airport.ident, a]));
    // "Nothing matched your filters" is a real answer; "nothing came back
    // because the forecast failed" is not, and the banner above says which.
    $("#discovery-results").innerHTML = data.length
      ? data.map(discoveryCard).join("")
      : (payload.data_health && payload.data_health.ok === false
          ? "" : `<p class="empty">No airports match within radius + filters.</p>`);
    stampDataTime();
    ok = true;
  } catch (e) { setHealth("discovery-data-health", fetchFailedBanner(String(e), "runDiscovery")); }
  finally {
    btn.disabled = false; btn.textContent = discoveryBtnLabel();
    stopRunTimer("discovery-timer", ok);
  }
}

// One failing row as a sentence - the same string, from the same fields, that
// orchestrator.reason_line builds server-side for `reasons`.
function reasonLine(c) {
  if (c.reason_text) return c.reason_text;
  const where = c.location ? ` at ${c.location}` : "";
  return `${c.label} ${c.actual_text} exceeds your limit (${c.limit_text})${where}`;
}

// Where a failing value came from. A bare "Visibility (XC) 2 SM exceeds your
// limit" left the pilot no way to tell whether that was a METAR, the model, or a
// TEMPO two hours out, so every failing line names its own source and group -
// they are not always the same one, which is the point.
function whySource(c) {
  if (!c.source || c.source === "-") return "";
  const detail = c.source_detail ? ` · ${zText(c.source_detail)}` : "";
  return `<div class="why-src">from ${escapeHtml(c.source)}${detail}</div>`;
}

// The raw TAF groups behind the busts, once each. One TEMPO typically breaks
// four limits at once; repeating its line under every one of them buried the
// card in six copies of the same string.
function whyRaw(checks) {
  const seen = new Map();
  for (const c of checks) {
    if (c.source_text && !seen.has(c.source_text)) seen.set(c.source_text, c.source_detail);
  }
  if (!seen.size) return "";
  return [...seen.entries()].map(([text, label]) => {
    // FM/BECMG/TEMPO/PROB open with their own keyword, so the line says which
    // group it is. The TAF's opening group has no such token - stacked under a
    // TEMPO its bare "27010KT P6SM OVC012" is anonymous - so that one is named.
    const token = (label || "").split(" ")[0].toUpperCase();
    const named = token && text.toUpperCase().startsWith(token);
    const tag = named || !label ? "" : `<span class="why-raw-tag">${zText(label)}</span>`;
    return `<div class="why-raw">${tag}${escapeHtml(text)}</div>`;
  }).join("");
}

// Why a candidate is not a plain GO, stated on the card itself.
//
// A discovery card used to show a MITIGATE or NO-GO badge and, often, nothing at
// all to explain it: the only thing rendered was `reasons`, which the backend
// builds from *failing limit checks alone* (orchestrator._explicit_reasons). A
// verdict that came from the threat stack - which is most MITIGATEs - arrived
// with an empty list. The threats and the advisories were on the wire the whole
// time; nobody was drawing them.
function whyBlock(a) {
  const threats = (a.threat_checks || []).filter((t) => t.present);
  const advisories = (a.limit_checks || []).filter((c) => c.advisory);
  // Rendered from the checks rather than the flattened `reasons` strings: the
  // provenance is only on the check.
  const limits = (a.limit_checks || []).filter((c) => !c.passed && c.applicable && !c.advisory);
  // A GO with nothing to add stays silent. A verdict that is NOT a GO always
  // renders, even with all three lists empty: an unexplained badge is the one
  // outcome this block exists to prevent, so the empty case falls through to a
  // stated fallback below rather than returning nothing and hiding it.
  const nothingToSay = !limits.length && !threats.length && !advisories.length;
  if (nothingToSay && a.verdict === "GO") return "";
  const heading = a.verdict === "GO" ? "Worth knowing" : `Why ${a.verdict}`;
  const parts = [];
  if (limits.length) {
    parts.push(`<div class="why-group"><span class="why-h">Over your limits</span>
      <ul class="reasons">${limits.map((c) =>
        `<li>${zText(reasonLine(c))}${whySource(c)}</li>`).join("")}</ul>
      ${whyRaw(limits)}</div>`);
  }
  if (threats.length) {
    const stack = `${threats.length} threat${threats.length > 1 ? "s" : ""}` +
      (a.threat_result_label ? ` → ${escapeHtml(a.threat_result_label)}` : "");
    parts.push(`<div class="why-group"><span class="why-h">Stacked threats (${stack})</span>
      <ul class="reasons">${threats.map((t) => `<li>${escapeHtml(t.label || threatLabel(t.key))}</li>`).join("")}</ul></div>`);
  }
  if (advisories.length) {
    parts.push(`<div class="why-group"><span class="why-h">Advisory - not counted against your limits</span>
      <ul class="reasons">${advisories.map((c) => `<li>${zText(c.label)}: ${zText(c.actual_text)}</li>`).join("")}</ul></div>`);
  }
  if (!parts.length) {
    // Reached only if the engine downgraded a verdict without attaching a row
    // to say why. Say *that*, plainly, instead of rendering a bare badge - a
    // pilot who can see the app has no reason is better off than one staring
    // at a MITIGATE that looks like it means nothing.
    parts.push(`<div class="why-group"><span class="why-h">No reason was attached to this verdict</span>
      <ul class="reasons"><li>${escapeHtml(a.verdict)} with no limit bust or threat reported - treat as unverified and check the raw METAR/TAF below.</li></ul></div>`);
  }
  return `<div class="why ${cls(a.verdict)}"><div class="why-title">${heading}</div>${parts.join("")}</div>`;
}

// The ETD you picked, beside the airport it applies to. Every candidate leaves
// at the same ETD and arrives at its own ETA - so the card carries both, and you
// can read a list of twenty and see when each one is for without scrolling back
// up to the dropdown. Omitted on a "Now" scan, where the answer is "now".
function plannedEtd(a) {
  if (!a.etd_utc || isNowEtd("#d-etd")) return "";
  const eta = a.eta_utc ? ` <span class="pe-arrow">→</span> ETA ${zEnd(a.etd_utc, a.eta_utc)}` : "";
  return `<span class="planned-etd" title="Assessed for this window - your ETD, and this aerodrome's own ETA">Planned ETD ${zHM(a.etd_utc)}${eta}</span>`;
}

function discoveryCard(a) {
  const w = a.weather || {}, rw = a.best_runway;
  return `<div class="card ${cls(a.verdict)}">
    <div class="card-head"><h3>${a.airport.ident} · ${a.airport.name}${a.access_note ? ` <span class="ppr">${a.access_note}</span>` : ""}${plannedEtd(a)}</h3><span class="badge ${cls(a.verdict)}">${a.verdict}</span></div>
    ${whyBlock(a)}
    <div class="meta">
      <span>${a.distance_nm} nm · ${dirM(null, a.bearing_true)}</span>
      <span><span class="mk">Time</span> ${fmtHrMin(a.flight_time_hr)}</span>
      <span>${srcChip(w.source, a)}</span>
      <span><span class="mk">Wind</span> ${windStr(w)}</span>
      ${ceilChip(w)}
      ${w.visibility_sm != null ? `<span><span class="mk">Vis</span> ${w.visibility_sm} SM</span>` : ""}
      ${a.altitude ? `<span title="best VFR cruising altitude - kept ≥500 ft below every ceiling on this card (reported now and forecast for your window) and scaled to leg distance"><span class="mk">Best alt</span> ${fmtFt(a.altitude.altitude_ft)}</span><span title="wind component along the leg at best altitude → groundspeed">${a.altitude.headwind_kt < 0 ? "tailwind" : "headwind"} ${Math.abs(Math.round(a.altitude.headwind_kt))} kt → GS ${Math.round(a.altitude.groundspeed_kt)} kt</span>` : ""}
    </div>
    ${rw ? `<div class="rwy-wrap"><span class="rwy-diag">${windRunwaySvg(rw, w)}</span><div class="rwy-lines"><div><strong>Best runway into wind</strong>: RWY ${rw.runway_ident} (${dirM(rw.heading_mag, rw.heading_true)})${dims(rw)} · xwind ${Math.round(rw.crosswind_kt)} kt · headwind ${Math.round(rw.headwind_kt)} kt</div>${windLegend(rw, w)}</div></div>` : `<div class="rwy-na">Runway data unavailable</div>`}
    ${runwaysBlock(a)}
    <div class="meta">${notamToggle(a)}<span class="links">${linksHtml(a)}</span></div>
    ${obsLine(w.raw_metar)}
    <div class="notam-list hidden" id="notams-${a.airport.ident}">${notamItems(a)}</div>
  </div>`;
}

// ---------- Discovery: the forecast behind the chip ----------
//
// A discovery card compresses a whole flight window into one line - one wind,
// one ceiling, one visibility, worst case across the leg. That is the right
// input to a go/no-go decision and the wrong amount of information for planning
// a departure four hours out, where what you want to know is which way it is
// moving. So the provenance chip opens onto its own source: the blue TAF chip
// shows the TAF split into its periods, the yellow HRDPS chip the model hour by
// hour. Green means the same thing in both, and the same thing it means on the
// route page: you are airborne during this.
//
// Hover on a pointer device, tap on a touch one - one element, reused, appended
// to <body> so it can escape the card's overflow and stacking context.
let DISCOVERY_BY_IDENT = {};
// `pinned` is what a click buys you on a mouse: hover already opened the thing,
// so the click has to mean "keep it open while I read it" rather than toggling
// it shut the instant the pointer that opened it arrives.
let WX_POP = { el: null, anchor: null, pinned: false };
const CAN_HOVER = () => window.matchMedia && window.matchMedia("(hover: hover)").matches;

function wxPopEl() {
  if (!WX_POP.el) {
    const el = document.createElement("div");
    el.className = "wx-pop";
    el.setAttribute("role", "dialog");
    el.hidden = true;
    // Clicks inside are for reading, not for dismissing.
    el.addEventListener("click", (e) => e.stopPropagation());
    document.body.appendChild(el);
    WX_POP.el = el;
  }
  return WX_POP.el;
}

function closeWxPop() {
  if (!WX_POP.el || WX_POP.el.hidden) return;
  WX_POP.el.hidden = true;
  if (WX_POP.anchor) WX_POP.anchor.setAttribute("aria-expanded", "false");
  WX_POP.anchor = null;
  WX_POP.pinned = false;
}

function openWxPop(anchor) {
  const el = wxPopEl();
  // A checklist row carries its report inline rather than by ident: the row's
  // source may be a station under the route that has no card of its own, so
  // there is nothing in DISCOVERY_BY_IDENT to look it up in.
  if (anchor.dataset.popKind === "REPORT") {
    el.innerHTML = reportPopBody(anchor.dataset.popLabel, anchor.dataset.popText);
  } else {
    const a = DISCOVERY_BY_IDENT[anchor.dataset.pop];
    if (!a) return;
    el.innerHTML = anchor.dataset.popKind === "TAF" ? tafPopBody(a) : modelPopBody(a);
  }
  el.hidden = false;
  if (WX_POP.anchor && WX_POP.anchor !== anchor) WX_POP.anchor.setAttribute("aria-expanded", "false");
  WX_POP.anchor = anchor;
  anchor.setAttribute("aria-expanded", "true");
  positionWxPop(anchor, el);
}

// Below the chip by default, flipped above when it would run off the bottom, and
// clamped to the viewport horizontally - on a phone that makes it a near
// full-width sheet rather than something hanging off the right edge.
function positionWxPop(anchor, el) {
  const margin = 8;
  el.style.left = "0px"; el.style.top = "0px";   // measure unconstrained
  const r = anchor.getBoundingClientRect();
  const box = el.getBoundingClientRect();
  const maxLeft = window.innerWidth - box.width - margin;
  const left = Math.max(margin, Math.min(r.left, maxLeft));
  const below = r.bottom + margin;
  const flip = below + box.height > window.innerHeight && r.top - box.height - margin > 0;
  const top = flip ? r.top - box.height - margin : below;
  el.style.left = `${left + window.scrollX}px`;
  el.style.top = `${top + window.scrollY}px`;
}

// The TAF, split into the periods this candidate's flight passes through. Reuses
// `tafBlock`, so a discovery popover and a route endpoint card render the same
// TAF the same way - including the green in-window rows and the amber PROB edge.
function tafPopBody(a) {
  const w = a.weather || {};
  return `<div class="wx-pop-head">
      <strong>TAF ${escapeHtml(a.airport.ident)}</strong>
      <span class="hint">${w.taf_valid_from ? `valid ${supDays(zRange(w.taf_valid_from, w.taf_valid_to))}` : ""}</span>
      ${wxPopClose()}
    </div>
    ${tafBlock(w, wxPopWindowLabel(a))}`;
}

// The model, hour by hour, either side of the leg. Deliberately raw HRDPS with
// no TAF laid over it and no verdict attached: the chip says HRDPS, so this is
// what HRDPS says. The hours you are airborne for are green.
function modelPopBody(a) {
  const hours = a.model_hours || [];
  const rows = hours.map((h) => {
    const gust = (h.gust_kt != null && h.wind_kt != null && h.gust_kt > h.wind_kt)
      ? "G" + Math.round(h.gust_kt) : "";
    const wind = h.wind_kt == null ? "-"
      : `${windDir(h.wind_dir_mag, h.wind_dir_true)}/${Math.round(h.wind_kt)}${gust}`;
    const extra = [precipText(h), (h.hazards || []).join(", ")].filter(Boolean).join(" · ");
    return `<div class="wx-h ${h.in_window ? "in" : ""}">
      <span class="wx-h-t">${escapeHtml(h.time.slice(11, 16))}Z</span>
      <span class="wx-h-w">${wind}</span>
      <span class="wx-h-c">${h.ceiling_agl_ft != null ? fmtCeil(h.ceiling_agl_ft) : cloudWord(h)}</span>
      <span class="wx-h-v">${h.visibility_sm != null ? h.visibility_sm + " SM" : "-"}</span>
      <span class="wx-h-x">${escapeHtml(extra)}</span>
    </div>`;
  }).join("");
  return `<div class="wx-pop-head">
      <strong>HRDPS ${escapeHtml(a.airport.ident)}</strong>
      <span class="hint">${escapeHtml(wxPopWindowLabel(a))}</span>
      ${wxPopClose()}
    </div>
    <div class="wx-hours">
      <div class="wx-h wx-h-hd"><span>Zulu</span><span>Wind</span><span>Ceiling</span><span>Vis</span><span></span></div>
      ${rows || `<div class="empty">No model hours available.</div>`}
    </div>
    <div class="hint wx-pop-foot">Green = airborne during this hour. Model only -
      no TAF overlay, no verdict. Hours are Zulu.</div>`;
}

// No ceiling reported is not the same as no cloud, so say which the model means.
function cloudWord(h) {
  if (h.cloud_cover_pct == null) return "-";
  return h.cloud_cover_pct < 55 ? "no ceiling" : "-";
}

// The span the green marks, named the same way the route cards name it.
function wxPopWindowLabel(a) {
  if (!a.etd_utc) return "your flight";
  return `your flight (${zRange(a.etd_utc, a.eta_utc)})`;
}

// The raw report behind a checklist row's source chip, verbatim. No decoding
// and no verdict: the row above it has already said what the value was and
// whether it passed, and what this answers is the narrower "says who?". The
// label is the chip's own text ("CYCK METAR, 18 nm"), so the popover names the
// same station and distance the row does.
function reportPopBody(label, text) {
  // The empty .hint is the spacer that pushes the close button to the right -
  // .wx-pop-head is a flex row and .hint is its only flexible child.
  return `<div class="wx-pop-head">
      <strong>${escapeHtml(label || "Report")}</strong>
      <span class="hint"></span>
      ${wxPopClose()}
    </div>
    <pre class="wx-raw">${escapeHtml(text || "")}</pre>`;
}

const wxPopClose = () =>
  `<button type="button" class="wx-pop-x" onclick="closeWxPop()" aria-label="Close">×</button>`;

// Every container that can hold a source chip. Both are re-rendered wholesale
// on each run - discovery cards on every scan, the checklist on every
// assessment - so delegation on the container is what keeps one set of
// listeners working across renders instead of leaking a set per card.
const WX_POP_HOSTS = ["#discovery-results", "#route-checklist"];

function wireWxPopovers() {
  WX_POP_HOSTS.forEach(wireWxPopoverHost);
  // Document- and window-level listeners are shared by every host, so they are
  // bound once here rather than once per host.
  document.addEventListener("click", closeWxPop);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeWxPop(); });
  // Anchored to a chip that moves with the page - reposition rather than let it
  // drift away from what it is describing.
  window.addEventListener("scroll", () => {
    if (WX_POP.anchor && WX_POP.el && !WX_POP.el.hidden) positionWxPop(WX_POP.anchor, WX_POP.el);
  }, { passive: true });
  window.addEventListener("resize", closeWxPop);
}

function wireWxPopoverHost(selector) {
  const host = $(selector);
  if (!host) return;
  const chipOf = (e) => e.target.closest && e.target.closest(".src-pop");
  host.addEventListener("click", (e) => {
    const chip = chipOf(e);
    if (!chip) return;
    e.preventDefault(); e.stopPropagation();
    // Already pinned on this chip → the click means "I'm done". Otherwise open
    // and pin, which on a mouse turns the hover preview into something that
    // stays put while you read it, and on a touch screen is the whole gesture.
    if (WX_POP.pinned && WX_POP.anchor === chip) { closeWxPop(); return; }
    openWxPop(chip);
    WX_POP.pinned = true;
  });
  // Pointer devices get a preview on hover; touch devices report no hover and
  // use the tap above, so this never fights the click on a phone.
  host.addEventListener("mouseover", (e) => {
    const chip = chipOf(e);
    if (chip && CAN_HOVER() && !WX_POP.pinned) openWxPop(chip);
  });
  host.addEventListener("mouseout", (e) => {
    const chip = chipOf(e);
    if (!chip || !CAN_HOVER() || WX_POP.pinned) return;
    // Moving from the chip into the popover itself must not close it.
    const to = e.relatedTarget;
    if (to && WX_POP.el && WX_POP.el.contains(to)) return;
    closeWxPop();
  });
  host.addEventListener("focusin", (e) => { const c = chipOf(e); if (c) openWxPop(c); });
}
window.closeWxPop = closeWxPop;

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

// Which hazards are ticked, held here rather than read back off the checkboxes.
//
// The tick state used to live only in the DOM, and the widespread-IMC checkbox
// was removed whenever the IFR pane was showing. Going VFR → IFR → VFR therefore
// destroyed it: the rebuild seeded itself from a DOM that no longer contained
// the control, so it came back unticked while the readout - which reads the
// saved profile - still said "8 of 8". Saving from that DOM then deleted the
// flag from the profile for good. A control that isn't on screen must never be
// able to change a setting, so the set is the source of truth and the
// checkboxes are just its view.
let WX_FLAGS_SELECTED = null;

function wxFlagsSelected() {
  if (WX_FLAGS_SELECTED === null) WX_FLAGS_SELECTED = new Set(effectiveLimits().weather_flags);
  return WX_FLAGS_SELECTED;
}

function buildWxFlags() {
  const selected = wxFlagsSelected();
  // widespread_ifr is a shared setting and the backend already ignores it on
  // IFR flights, so it stays on screen under both - with a note saying so.
  const note = { widespread_ifr: "not applied on IFR flights" };
  $("#wxflags").innerHTML = (CONFIG.weather_flag_options || [])
    .map((f) => `<label class="control checkbox"><input type="checkbox" class="wxflag" value="${f}"${selected.has(f) ? " checked" : ""}> ${wxLabel(f)}${note[f] ? ` <span class="hint">(${note[f]})</span>` : ""}</label>`)
    .join("");
  // Read the set through the accessor at event time, never capture it: saving
  // and resetting both replace it wholesale, and a captured reference would
  // leave these handlers quietly updating a set nobody reads any more.
  $$(".wxflag").forEach((c) => c.addEventListener("change", () => {
    const set = wxFlagsSelected();
    if (c.checked) set.add(c.value);
    else set.delete(c.value);
  }));
}

// Per-flight extra threats (all kind:"per_flight" from the config), e.g.
// terrain-critical and unfamiliar/complex airspace. single_pilot_ifr_no_autopilot
// only applies when IFR is selected - and because that one *is* hidden under VFR,
// its tick is held outside the DOM for the same reason as the weather flags
// above, so an IFR → VFR → IFR round trip no longer silently clears it.
const THREATS_SELECTED = new Set();

function renderExtraThreats() {
  const ifr = currentFlightRules() === "ifr";
  const items = threatsOfKind("per_flight")
    .map((t) => t.key)
    .filter((k) => ifr || k !== "single_pilot_ifr_no_autopilot");
  $("#threats-list").innerHTML = items
    .map((t) => `<label><input type="checkbox" class="threat" value="${t}"${THREATS_SELECTED.has(t) ? " checked" : ""}> ${threatLabel(t)}</label>`)
    .join("");
  $$(".threat").forEach((c) => c.addEventListener("change", () => {
    if (c.checked) THREATS_SELECTED.add(c.value);
    else THREATS_SELECTED.delete(c.value);
  }));
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

// On touch devices the value only moves when the pilot grabs the thumb and drags
// it - a tap/touch anywhere on the bare bar does nothing. preventDefault() on
// pointerdown does NOT stop a native range from jumping to the tap, so instead we
// decide on pointerdown whether the touch landed on the thumb, and if it didn't,
// revert the value on the resulting `input` event (same task → no visible jump)
// and stopImmediatePropagation() so the readout listener never sees it. We never
// preventDefault a touch, so an inadvertent touch while scrolling still scrolls.
// NOTE: makeDragOnly() must be wired BEFORE the readout `input` listener so this
// guard runs first (AT_TARGET listeners fire in registration order).
function makeDragOnly(el) {
  const THUMB = 18, GRAB = THUMB / 2 + 12; // grab radius around the small ball
  let allow = true, startVal = el.value;
  el.addEventListener('pointerdown', e => {
    startVal = el.value;
    if (e.pointerType !== 'touch') { allow = true; return; } // desktop unchanged
    const r = el.getBoundingClientRect();
    const min = +el.min, max = +el.max;
    const frac = (el.value - min) / (max - min || 1);
    const center = r.left + THUMB / 2 + frac * (r.width - THUMB);
    allow = Math.abs(e.clientX - center) <= GRAB; // only when grabbing the ball
  });
  el.addEventListener('input', e => {
    if (allow) return;
    el.value = startVal;          // undo the tap-jump…
    e.stopImmediatePropagation(); // …and hide it from the readout listener
  });
  const release = () => { allow = true; };
  el.addEventListener('pointerup', release);
  el.addEventListener('pointercancel', release);
}

// Build a labelled slider per minimum, with a live value readout.
function renderMinSliders() {
  const byGrp = {};
  for (const f of MIN_FIELDS) (byGrp[f.grp] ||= []).push(f);
  for (const [grp, fields] of Object.entries(byGrp)) {
    const el = $("#" + grp);
    if (!el) continue;
    el.innerHTML = fields.map((f) => `
      <div class="sld"${f.hint ? ` title="${escapeHtml(f.hint)}"` : ""}>
        <span class="sld-label">${f.label}</span>
        <output class="sld-val" id="${f.id}-out"></output>
        <input type="range" id="${f.id}" min="${f.min}" max="${f.max}" step="${f.step}" />
      </div>`).join("");
  }
  for (const f of MIN_FIELDS) {
    const el = $("#" + f.id);
    if (el) {
      makeDragOnly(el); // before the readout listener - see note on makeDragOnly
      el.addEventListener("input", (e) => ($("#" + f.id + "-out").textContent = `${e.target.value} ${f.unit}`));
    }
  }
}

// Recent experience slider in the settings form (local only - not sent to backend).
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
  makeDragOnly(recencyEl); // before the readout listener - see note on makeDragOnly
  recencyEl.addEventListener("input", (e) => {
    const val = +e.target.value;
    document.getElementById("set-recency-out").textContent = `${val} hr`;
    saveRecencyMin(val);
    renderSelfAssessment("route-self-check");
    renderSelfAssessment("discovery-self-check");
  });
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
  // Re-seed the tick set from the saved profile, then paint the checkboxes from
  // it - so Save and Reset both land in the controls and in the state behind
  // them, and the two can never drift apart.
  WX_FLAGS_SELECTED = new Set(eff.weather_flags);
  $$(".wxflag").forEach((c) => (c.checked = WX_FLAGS_SELECTED.has(c.value)));
  const imc = $("#set-imc-threat");
  if (imc) imc.checked = !!eff.imc_as_threat;
  const night = $("#set-night-threat");
  if (night) night.checked = !!eff.night_as_threat;
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
  // Saved from the tick set, in the config's own order. Comparing *membership*
  // against the defaults, not just the count: swapping one hazard for another
  // leaves the length unchanged, and that used to be saved as "no change".
  const selected = wxFlagsSelected();
  const checked = (CONFIG.weather_flag_options || []).filter((f) => selected.has(f));
  if (checked.length !== d.weather_flags.length
      || d.weather_flags.some((f) => !selected.has(f))) {
    mins.weather_flags = checked;
  }

  // IMC-as-threat: only persist when it differs from the default (off).
  const imcEl = $("#set-imc-threat");
  if (imcEl && imcEl.checked !== !!difr.imc_as_threat) mins.imc_as_threat = imcEl.checked;

  // Night-as-threat: same, against a default of on.
  const nightDefault = CONFIG.default_night_as_threat !== false;
  const nightEl = $("#set-night-threat");
  if (nightEl && nightEl.checked !== nightDefault) mins.night_as_threat = nightEl.checked;

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
  flashStatus("Saved - every flight is now gated by your profile.");
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
      <span class="val"><span class="act">${cur}${unit ? " " + unit : ""}</span></span>
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
  const nightDefault = CONFIG.default_night_as_threat !== false;
  const nightRow = row("Night as threat", eff.night_as_threat ? "on" : "off",
    nightDefault ? "on" : "off", "", !!eff.night_as_threat !== nightDefault);
  $("#minimums-readout").innerHTML =
    `<div class="min-banner ${custom ? "custom" : ""}">${custom
      ? "Using your saved profile (★ = changed from default)."
      : "Using the built-in default profile."}</div>${baseRow}${minRows}${flagsRow}${imcRow}${nightRow}${consRow}`;
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
    makeField(PILOT_FITNESS_ITEMS, "Pilot fitness - included in self-assessment if checked") +
    makeField(EXTERNAL_PRESSURE_ITEMS, "External pressures - included in self-assessment if checked");
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
    <h3>Preflight self-assessment <span class="hint">(personal hard limits - check before pulling weather)</span></h3>
    <div class="checks-grid">
      ${activePF.length ? `<fieldset><legend>Pilot fitness - do not fly if any apply</legend>${gates(activePF)}${recencyGate}</fieldset>` : ""}
      ${activeEP.length ? `<fieldset><legend>External pressure - pause &amp; reassess</legend>${gates(activeEP)}</fieldset>` : ""}
    </div>
    <div id="${bannerId}" class="banner hidden">
      One or more personal factors checked - <strong>PAUSE and reassess.</strong>
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

// Mitigation reference block - shown when verdict is MITIGATE.
function mitigationBlock(threatChecks) {
  const active = (threatChecks || []).filter((t) => t.present && THREAT_MITIGATIONS[t.key]);
  if (!active.length) return "";
  return `<div class="panel mit-block">
    <h3>Threat mitigation reference</h3>
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
  return "-";
}
function gustStr(w) {
  return (w.gust_kt != null && w.wind_kt != null && w.gust_kt > w.wind_kt) ? "G" + Math.round(w.gust_kt) : "";
}
function windStr(w) {
  if (w.wind_kt == null) return "-";
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
  return "-";
}
const fmtFt = (ft) => (ft == null ? "-" : `${Math.round(ft).toLocaleString()} ft`);
const fmtCeil = (ft) => (ft == null ? "-" : `${(Math.round(ft / 100) * 100).toLocaleString()} ft`);
// Tops round to 500 ft where a ceiling rounds to 100. A ceiling is compared
// against a minimum, so its hundreds matter. A top is compared against cruising
// levels a thousand feet apart, and above 10,000 ft it is derived from model
// levels two thousand feet apart - printing "6,428 ft" would claim a precision the
// number has never had.
const fmtTops = (ft) => (ft == null ? "-" : `${(Math.round(ft / 500) * 500).toLocaleString()} ft`);

const TOPS_TITLE =
  "Estimated, not observed. The app walks the model's pressure-level cloud cover and "
  + "interpolates where it falls back through broken - good to a few hundred feet low "
  + "down, and to about a thousand above 10,000 ft where the levels are 2,000 ft apart. "
  + "Treat it as a planning figure and confirm it against a PIREP or the GFA.";

const TOPS_RH_TITLE =
  " This one came from the humidity profile rather than from cloud cover, because the "
  + "model served no per-level cover here - weaker again, since air ceasing to be "
  + "saturated is close to, but not the same as, the cloud stopping.";

// Cloud tops, next to the ceiling they belong with. The ceiling is AGL and the
// tops are MSL, so both carry their datum: "1,400" and "5,500" side by side with
// no units invites subtracting one from the other.
function topsSpan(r) {
  if (r.enroute_tops_state === "above_scan") {
    const lim = fmtTops(r.enroute_tops_scan_msl_ft);
    return `<span title="The pressure levels this app samples stop near ${lim} MSL, and the deck was still broken at the top of the scan. The tops are higher than that - which is not the same as unknown, and not the same as known-but-out-of-reach."><span class="mk">Tops</span> above ${lim} MSL</span>`;
  }
  if (r.enroute_tops_msl_ft == null) return "";
  const where = r.enroute_tops_at ? ` <span class="hint">${escapeHtml(r.enroute_tops_at)}</span>` : "";
  const title = TOPS_TITLE + (r.enroute_tops_from_rh ? TOPS_RH_TITLE : "");
  return `<span title="${escapeHtml(title)}"><span class="mk">Tops</span> ~${fmtTops(r.enroute_tops_msl_ft)} MSL <span class="src-model">model estimate</span>${where}</span>`;
}
function ceilChip(w) {
  if (w.ceiling_agl_ft != null) return `<span><span class="mk">Ceiling</span> ${fmtCeil(w.ceiling_agl_ft)}</span>`;
  if (w.source === "Observed") return `<span><span class="mk">Ceiling</span> none</span>`;
  return "";
}
/* What the hour holds, in the notation it is reported in. These were weather
   emoji; a METAR abbreviation is the same information in the vocabulary the
   pilot reading this page already uses - and it fits a 32px cell, which a
   colour emoji at .7rem never legibly did. */
function wxGlyph(h) {
  if ((h.hazards || []).includes("thunderstorm")) return "TS";
  if ((h.hazards || []).includes("freezing_rain")) return "FZRA";
  if (!h.precip) return "";
  if (h.precip.includes("snow")) return "SN";
  if (h.precip.includes("freezing")) return "FZ";
  return "RA";
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
// How old a report is, in green/red. ``staleAfter`` is where red starts: a METAR
// keeps a grey band between the hour it is issued on and the point it is
// genuinely overdue, because a 70-minute-old hourly observation is normal. A
// PIREP has no schedule - it describes air one aircraft flew through, and past
// an hour that air has moved - so it goes straight from green to red.
function ageChipFromMin(mins, { staleAfter = 90 } = {}) {
  if (mins == null) return "";
  const txt = mins < 60 ? `${mins} min ago` : `${Math.floor(mins / 60)} h ${mins % 60} min ago`;
  const staleClass = mins > staleAfter ? " stale" : mins < 60 ? " fresh" : "";
  return ` <span class="age${staleClass}">${txt}</span>`;
}
function ageChip(raw) {
  return ageChipFromMin(metarAgeMin(raw));
}
// One observation line, labelled with what the report actually is.
//
// Every call site used to hardcode "METAR " in front of the raw text, so a
// SPECI - a special observation issued off the hour precisely because something
// changed - was displayed as an ordinary hourly report, and read as one. The
// label comes from the report's own type token, which is also stripped from the
// text so a feed that includes the prefix does not render it twice.
function obsLine(raw, cls = "raw") {
  if (!raw) return "";
  const m = /^\s*(METAR|SPECI)\s+/i.exec(raw);
  const label = m ? m[1].toUpperCase() : "METAR";
  const body = m ? raw.slice(m[0].length) : raw;
  return `<div class="${cls}">${label} ${escapeHtml(body)}${ageChip(raw)}</div>`;
}
// Minutes since an ISO8601 stamp - what the advisory feeds carry, where a METAR
// carries its own DDHHMM group.
function isoAgeMin(iso) {
  const t = Date.parse(iso || "");
  if (isNaN(t)) return null;
  return Math.max(0, Math.round((Date.now() - t) / 60000));
}
// A PIREP's age. Green under the hour, red over it.
function pirepAgeChip(validFrom) {
  return ageChipFromMin(isoAgeMin(validFrom), { staleAfter: 60 });
}
// Where a bulletin stands against the clock right now: running, lapsed, or not
// yet in force. The card and the map popup both used to format `valid_to` into a
// bare "until 0255Z" of their own, which said the same thing about a SIGMET with
// ninety minutes left and one that expired yesterday. Relevance here is decided
// against the *flight window* server-side (`_drop_reason`), deliberately - this
// is the other question, the one a pilot asks while looking at the card.
//
// Returns null when there is no readable `valid_to`, which is the honest answer
// for a product that never carried one rather than a reason to guess.
function expiryState(validFrom, validTo) {
  const to = Date.parse(validTo || "");
  if (isNaN(to)) return null;
  const z = zHM(validTo);
  const now = Date.now();
  if (now >= to) return { state: "expired", text: `expired ${z}` };
  const from = Date.parse(validFrom || "");
  // Issued for later. "1 h left" would be a lie about something not yet running.
  if (!isNaN(from) && from > now) return { state: "pending", text: `until ${z}` };
  return { state: "live", text: `until ${z} · ${fmtHrMin((to - now) / 3600000)} left` };
}
// The same fact as a card chip, carrying its own colour: green while it is
// running, muted once it has lapsed.
function expiryChip(validFrom, validTo) {
  const e = expiryState(validFrom, validTo);
  if (!e) return "";
  const cls = e.state === "live" ? " live" : e.state === "expired" ? " expired" : "";
  return `<span class="adv-chip${cls}">${escapeHtml(e.text)}</span>`;
}
function dimsText(c) {
  const l = c.length_ft ? Math.round(c.length_ft).toLocaleString() : "?";
  const wid = c.width_ft ? ` × ${Math.round(c.width_ft)} ft` : " ft";
  return `${l}${wid}${c.surface_label ? " " + c.surface_label : ""}`;
}
const dims = (rw) => (rw && rw.length_ft ? ` · ${dimsText(rw)}` : "");
function fmtHrMin(hr) {
  if (hr == null) return "-";
  const total = Math.round(hr * 60), h = Math.floor(total / 60), m = total % 60;
  return h ? `${h} h ${m} min` : `${m} min`;
}
// The provenance chip. With a discovery assessment passed in, the TAF (blue) and
// HRDPS (yellow) chips become buttons that open the forecast behind the value -
// a card reports one merged worst-case line, and "what is the weather actually
// doing around my ETD" is the next question it raises. Observed (a METAR, which
// the card already prints in full at the bottom) stays a plain chip.
function srcChip(source, a) {
  if (!source || source === "-") return `<span class="src">-</span>`;
  const k = { Observed: "OBSERVED", TAF: "TAF", HRDPS: "HRDPS" }[source] || "";
  if (a && (k === "TAF" || k === "HRDPS") && wxPopHas(a, k)) {
    return `<button type="button" class="src ${k} src-pop" data-pop="${escapeHtml(a.airport.ident)}" data-pop-kind="${k}"
      aria-haspopup="dialog" aria-expanded="false"
      title="${k === "TAF" ? "See the full TAF" : "See the hourly HRDPS forecast"}">${source}<span class="src-pop-caret" aria-hidden="true">▾</span></button>`;
  }
  return `<span class="src ${k}">${source}</span>`;
}

// Whether there is anything behind the chip worth opening. A chip that opens an
// empty box is worse than one that doesn't open.
function wxPopHas(a, kind) {
  const w = a.weather || {};
  return kind === "TAF" ? !!w.raw_taf : !!(a.model_hours || []).length;
}
// One timeline instant as "Mon 0430Z". The timeline's own times are UTC without
// a "Z" suffix (Open-Meteo is queried with timezone=UTC), so they go through
// utcDate explicitly - `new Date("2026-08-13T04:00")` would read them as
// browser-local and print a time hours away from the flight.
function zDayTime(t) {
  const d = utcDate(t);
  if (!d) return t;
  return `${d.toLocaleDateString(undefined, { weekday: "short", timeZone: "UTC" })} ${zPad(d.getUTCHours())}${zPad(d.getUTCMinutes())}Z`;
}
function fmtRange(a, b) {
  return `${zDayTime(a)} → ${zDayTime(b)}`;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------- PWA: register the service worker (installable + offline shell) ----------
// The SW caches only the static shell; /api/* always hits the network so weather
// data stays live. Safe to call unconditionally — unsupported browsers ignore it.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch((err) => {
      console.warn("Service worker registration failed:", err);
    });
  });
}

init();
