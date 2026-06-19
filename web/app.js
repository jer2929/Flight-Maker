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
  $("#f-time").addEventListener("input", (e) => ($("#f-time-out").textContent = +e.target.value ? `${e.target.value} min` : "Any"));
  $$(".gate").forEach((c) => c.addEventListener("change", updateGate));
  $$(".seg-btn").forEach((b) => b.addEventListener("click", () => {
    $$(".seg-btn").forEach((x) => x.classList.toggle("active", x === b));
    const n = $$(".threat").find((c) => c.value === "night_operations");
    if (n) n.checked = b.dataset.mode === "night";
  }));
  $$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));
  $("#run-route").addEventListener("click", runRoute);
  $("#run-discovery").addEventListener("click", runDiscovery);
  autocomplete("dep", "dep-list");
  autocomplete("dest", "dest-list");
}

const currentMode = () => ($$(".seg-btn").find((b) => b.classList.contains("active")) || {}).dataset?.mode || "day";
function updateGate() { $("#gate-banner").classList.toggle("hidden", !$$(".gate").some((c) => c.checked)); }
function switchTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $("#tab-route").classList.toggle("hidden", name !== "route");
  $("#tab-discovery").classList.toggle("hidden", name !== "discovery");
}
const threatsParam = () => $$(".threat").filter((c) => c.checked).map((c) => c.value).join(",");

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

// ---------- Route ----------
async function runRoute() {
  const dep = $("#dep").value.trim().toUpperCase(), dest = $("#dest").value.trim().toUpperCase();
  if (!dest) { $("#route-verdict").innerHTML = `<div class="empty">Enter a destination.</div>`; return; }
  const btn = $("#run-route"); btn.disabled = true; btn.textContent = "Pulling data…";
  clearRoute();
  try {
    const params = new URLSearchParams({ dep, dest, mode: currentMode(), threats: threatsParam() });
    const res = await fetch(`/api/route?${params}`);
    if (!res.ok) { $("#route-verdict").innerHTML = `<div class="empty">Unknown departure or destination.</div>`; return; }
    renderRoute(await res.json());
  } catch (e) {
    $("#route-verdict").innerHTML = `<div class="empty">Error: ${e}</div>`;
  } finally { btn.disabled = false; btn.textContent = "Assess route"; }
}

function clearRoute() {
  ["route-verdict", "route-checklist", "route-summary", "route-endpoints", "route-windows", "route-timeline"]
    .forEach((id) => ($("#" + id).innerHTML = ""));
}

function renderRoute(r) {
  const v = r.verdict_now;
  $("#route-verdict").innerHTML = `<div class="verdict-banner ${cls(v)}">${r.departure.airport.ident} → ${r.destination.airport.ident}: ${v} now</div>`;
  $("#route-checklist").innerHTML = checklist(r);

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
  return `<div class="panel checklist">
    <div class="cl-group"><h3>Hard limits — conditions <span class="hint">(worst point on the route)</span></h3>${cond.map(rowCheck).join("")}</div>
    <div class="cl-group"><h3>Weather <span class="hint">(SIGMET/AIRMET/PIREP + model; ⚠ = review GFA)</span></h3>${wx.map(rowCheck).join("")}</div>
    <div class="cl-group"><h3>Two-trigger threat stack <span class="badge ${cls(stackVerdict(n))}">${n} → ${r.threat_result_label || stackWord(n)}</span></h3>${r.threat_checks.map(rowThreat).join("")}</div>
  </div>`;
}
const stackWord = (n) => ["Normal flight", "Mitigate carefully", "No-go solo", "No-go"][Math.min(n, 3)];
const stackVerdict = (n) => (n === 0 ? "GO" : n === 1 ? "MITIGATE" : "NOGO");

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
      ${w.ceiling_agl_ft != null ? `<span>☁ ${fmtCeil(w.ceiling_agl_ft)}</span>` : ""}
      ${w.visibility_sm != null ? `<span>👁 ${w.visibility_sm} SM</span>` : ""}
      ${notamToggle(a)}
    </div>
    <div class="rwy-lines">
      ${to ? `<div>🛫 <strong>Takeoff</strong>: RWY ${to.runway_ident} (${dirM(to.heading_mag, to.heading_true)}) · headwind ${Math.round(to.headwind_kt)} kt · xwind ${to.crosswind_kt} kt${dims(to)}</div>` : ""}
      ${ld ? `<div>🛬 <strong>Landing</strong>: RWY ${ld.runway_ident} (${dirM(ld.heading_mag, ld.heading_true)}) · xwind ${ld.crosswind_kt} kt${ld.crosswind_kt_gust ? ` (gust ${ld.crosswind_kt_gust})` : ""}${dims(ld)}</div>` : ""}
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
  // Only the ends usable into wind — you never land with a tailwind component.
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
function notamItems(a) {
  return (a.notams || []).map((n) =>
    `<div class="notam"><a href="${n.url || "https://plan.navcanada.ca/"}" target="_blank" rel="noopener">${n.number || "NOTAM"} ↗</a> ${escapeHtml(n.text)}</div>`).join("");
}
window.toggleNotams = (id) => $("#notams-" + id).classList.toggle("hidden");

function renderTimeline(timeline, windows) {
  if (!timeline.length) { $("#route-timeline").innerHTML = ""; return; }
  const inWindow = (t) => windows.some((w) => t >= w.start && t <= w.end);
  const byDay = {};
  timeline.forEach((h) => { (byDay[h.time.slice(0, 10)] ||= []).push(h); });
  let html = `<div class="timeline-wrap"><h3>Hour-by-hour (full decision card; worse of departure &amp; destination)</h3>
    <div class="legend"><span class="go">GO</span><span class="mit">MITIGATE</span><span class="nogo">NO-GO</span><span>· dimmed = night · outlined = best window</span></div>`;
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
        h.hazards.length ? "hazards: " + h.hazards.join(",") : "",
        `[${h.source}]`, ...h.reasons,
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
  const btn = $("#run-discovery"); btn.disabled = true; btn.textContent = "Checking…";
  $("#discovery-results").innerHTML = "";
  try {
    const p = {
      radius: $("#radius").value, mode: currentMode(), threats: threatsParam(),
      surface: $("#f-surface").value, length: $("#f-length").value, into_wind: $("#f-into-wind").checked,
      min_width_ft: $("#f-width").value, sort: $("#f-sort").value,
      max_crosswind: $("#f-xwind").checked, go_only: $("#f-go").checked,
    };
    const t = +$("#f-time").value;
    if (t > 0) p.max_time_min = t;
    const params = new URLSearchParams(p);
    const data = await fetch(`/api/suggest?${params}`).then((r) => r.json());
    $("#discovery-results").innerHTML = data.length ? data.map(discoveryCard).join("") : `<p class="empty">No airports match within radius + filters.</p>`;
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
      ${w.ceiling_agl_ft != null ? `<span>☁ ${fmtCeil(w.ceiling_agl_ft)}</span>` : ""}
      ${w.visibility_sm != null ? `<span>👁 ${w.visibility_sm} SM</span>` : ""}
      ${a.altitude ? `<span title="wind component along the leg at best altitude → groundspeed">${a.altitude.headwind_kt < 0 ? "🟢 tailwind" : "🔴 headwind"} ${Math.abs(Math.round(a.altitude.headwind_kt))} kt → GS ${Math.round(a.altitude.groundspeed_kt)} kt</span>` : ""}
    </div>
    ${rw ? `<div class="rwy-lines"><div>🛬 <strong>Best runway into wind</strong>: RWY ${rw.runway_ident} (${dirM(rw.heading_mag, rw.heading_true)})${dims(rw)} · xwind ${rw.crosswind_kt} kt · headwind ${Math.round(rw.headwind_kt)} kt</div></div>` : `<div class="rwy-na">🛬 Runway data unavailable</div>`}
    ${runwaysBlock(a)}
    <div class="meta"><span>📋 ${a.notam_count} NOTAM</span><span class="links">${linksHtml(a)}</span></div>
    ${a.reasons.length ? `<ul class="reasons">${a.reasons.map((x) => `<li>${x}</li>`).join("")}</ul>` : ""}
    ${w.raw_metar ? `<div class="raw">METAR ${escapeHtml(w.raw_metar)}${ageChip(w.raw_metar)}</div>` : ""}
    <div class="notam-list hidden" id="notams-${a.airport.ident}">${notamItems(a)}</div>
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
  // Only show a gust when it actually exceeds the steady wind (no "14G14").
  return (w.gust_kt != null && w.wind_kt != null && w.gust_kt > w.wind_kt) ? "G" + Math.round(w.gust_kt) : "";
}
function windStr(w) {
  if (w.wind_kt == null) return "—";
  return `${windDir(w.wind_dir_mag, w.wind_dir_true)}/${Math.round(w.wind_kt)}${gustStr(w)} kt`;
}
// Wind vectors are rounded to the nearest 10° (e.g. 286 → 290).
function round10(d) { if (d == null) return null; let r = Math.round(d / 10) * 10; if (r >= 360) r -= 360; return r; }
function windDir(magVal, trueVal) {
  if (magVal != null) return `${String(round10(magVal)).padStart(3, "0")}°M`;
  if (trueVal != null) return `${String(round10(trueVal)).padStart(3, "0")}°T`;
  return "—";
}
const fmtFt = (ft) => (ft == null ? "—" : `${Math.round(ft).toLocaleString()} ft`);
// All ceiling / cloud-base heights rounded to the nearest 100 ft for display.
const fmtCeil = (ft) => (ft == null ? "—" : `${(Math.round(ft / 100) * 100).toLocaleString()} ft`);

// Age of a METAR from its DDHHMMZ stamp, vs now (UTC). Returns "" if unparseable.
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
  const stale = mins > 90 ? " stale" : "";
  return ` <span class="age${stale}">${txt}</span>`;
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
