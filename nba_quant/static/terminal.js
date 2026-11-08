/* Case NBA Quant Intelligence Terminal. Every view renders backend state from /api/intel/*. */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v) ? "—" : typeof v === "number" ? v.toFixed(d) : esc(v));
const pct = (v, d = 1) => (v === null || v === undefined ? "—" : (100 * v).toFixed(d) + "%");
const signed = (v, d = 1) => (v === null || v === undefined ? "—" : (v > 0 ? "+" : "") + v.toFixed(d));
const S = { season: null, teams: [], teamById: {}, status: null, player: null, team: null, lineups: null, predict: null, scen: {} };

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)) : `HTTP ${r.status}`);
  return body;
}
const q = (o) => Object.entries(o).filter(([, v]) => v !== "" && v !== null && v !== undefined).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
const loading = (el, text = "Computing from the warehouse…") => { el.innerHTML = `<div class="panel"><div class="muted">${esc(text)}</div><div class="progress"><i></i></div></div>`; };
const fail = (el, e) => { el.innerHTML = `<div class="err">${esc(e.message || e)}</div>`; };
const teamName = (id) => S.teamById[id]?.abbreviation || id;
const st = (s) => `<span class="st ${esc(s)}">${esc((s || "").slice(0, 3))}</span>`;

function pbar(p) {
  if (p === null || p === undefined) return `<span class="muted">—</span>`;
  const c = p >= 80 ? "var(--pos)" : p >= 55 ? "var(--blue)" : p >= 30 ? "var(--warn)" : "var(--neg)";
  return `<span class="pv">${Math.round(p)}</span><span class="pbar"><i style="width:${Math.max(2, p)}%;background:${c}"></i></span>`;
}

/* ---------------------------------------------------------------- shell */
function switchWs(ws) {
  $$("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.ws === ws));
  $$(".ws").forEach((s) => s.classList.toggle("active", s.id === `ws-${ws}`));
  $("#crumb").textContent = $(`#nav button[data-ws="${ws}"]`).textContent.slice(1).toUpperCase();
  try { localStorage.setItem("nbaq-ws", ws); } catch (_) { /* storage optional */ }
  if (ws === "audit") loadAudit();
  if (ws === "predict" && !S.scheduleLoaded) loadSchedule();
}

async function boot() {
  $$("#nav button").forEach((b) => b.addEventListener("click", () => switchWs(b.dataset.ws)));
  try {
    S.status = await api("/api/intel/status");
  } catch (e) { $("#banner").innerHTML = `<div class="err">Status unavailable: ${esc(e.message)}</div>`; return; }
  renderStatus();
  if (S.status.status !== "READY") {
    $("#banner").innerHTML = `<div class="err">${esc(S.status.message)} Example: <span class="mono">python -m nba_quant.intel acquire 2025-10-21 2026-06-25 &amp;&amp; python -m nba_quant.intel build</span></div>`;
    return;
  }
  const seasons = S.status.seasons;
  $("#season").innerHTML = seasons.map((s) => `<option value="${s.season}">${s.season - 1}-${String(s.season).slice(2)} · ${s.games} games</option>`).join("");
  S.season = seasons.length ? seasons[seasons.length - 1].season : null;
  $("#season").value = S.season;
  $("#season").addEventListener("change", () => { S.season = Number($("#season").value); refreshActive(); });
  const t = await api("/api/intel/teams");
  S.teams = t.teams; S.teamById = Object.fromEntries(t.teams.map((x) => [x.id, x]));
  const opts = t.teams.map((x) => `<option value="${x.id}">${esc(x.abbreviation)} · ${esc(x.name)}</option>`).join("");
  ["#t-team", "#l-team", "#m-home", "#m-away", "#e-home", "#e-away"].forEach((s) => ($(s).innerHTML = opts));
  $("#t-opp").innerHTML = `<option value="">All</option>` + opts;
  $("#m-away").selectedIndex = 1; $("#e-away").selectedIndex = 1;
  wirePlayer(); wireTeam(); wireLineup(); wireMatchup(); wirePredict();
  let ws = "player";
  try { ws = localStorage.getItem("nbaq-ws") || "player"; } catch (_) { /* optional */ }
  switchWs(ws);
  setInterval(async () => { try { S.status = await api("/api/intel/status"); renderStatus(); } catch (_) { /* keep last */ } }, 20000);
}

function renderStatus() {
  const s = S.status;
  if (s.status !== "READY") { $("#status-row").innerHTML = `<span class="chip"><span class="dot bad"></span>WAREHOUSE EMPTY</span>`; return; }
  const rows = s.coverage.rows;
  const valid = s.coverage.seasons.reduce((a, x) => a + x.pbp_valid, 0);
  const warm = s.warm.status === "ready" ? "ok" : s.warm.status === "error" ? "bad" : "warn";
  $("#status-row").innerHTML = [
    `<span class="chip"><span class="dot ok"></span>${rows.games} games · ${valid} PBP-validated</span>`,
    `<span class="chip">${(rows.possessions / 1000).toFixed(0)}k poss · ${(rows.shots / 1000).toFixed(0)}k shots · ${(rows.lineup_stints / 1000).toFixed(0)}k stints</span>`,
    `<span class="chip">cutoff ${esc(s.latest_cutoff)}</span>`,
    `<span class="chip"><span class="dot ${warm}"></span>models ${esc(s.warm.status)}</span>`,
    `<span class="chip"><span class="dot ${s.jobs_running ? "warn" : "ok"}"></span>${s.jobs_running} sim jobs</span>`,
    `<span class="chip"><span class="dot ok"></span>LLM: none</span>`,
  ].join("");
}

function refreshActive() {
  const ws = $(".ws.active").id.replace("ws-", "");
  if (ws === "player" && S.player) loadPlayer(S.player.player.id);
  if (ws === "team" && S.team) loadTeam();
  if (ws === "lineup" && S.lineups) loadLineups();
  if (ws === "matchup" && S.matchupLoaded) loadMatchup();
}

/* ---------------------------------------------------------------- shot chart */
function court(shots, opts = {}) {
  const W = 50, mode = opts.mode || "dots";
  const lines = `<g fill="none" stroke="#2a3a5c" stroke-width=".25">
    <rect x="0" y="-5.25" width="50" height="47"/><rect x="17" y="-5.25" width="16" height="19"/>
    <circle cx="25" cy="13.75" r="6"/><circle cx="25" cy="0" r=".75" stroke="#e8b24a"/>
    <line x1="22" y1="-1.25" x2="28" y2="-1.25" stroke="#e8b24a"/><path d="M21 0 A4 4 0 0 0 29 0"/>
    <line x1="3" y1="-5.25" x2="3" y2="8.95"/><line x1="47" y1="-5.25" x2="47" y2="8.95"/>
    <path d="M3 8.95 A23.75 23.75 0 0 0 47 8.95"/><path d="M19 41.75 A6 6 0 0 1 31 41.75"/></g>`;
  let marks = "";
  if (mode === "heat") {
    const bins = {};
    shots.forEach((s) => { const k = `${Math.floor(s.x / 3)}:${Math.floor(s.y / 3)}`; (bins[k] ||= { n: 0, m: 0, x: 0 }); bins[k].n++; bins[k].m += s.made; bins[k].x += s.xfg; });
    marks = Object.entries(bins).filter(([, b]) => b.n >= 3).map(([k, b]) => {
      const [i, j] = k.split(":").map(Number); const d = (b.m - b.x) / b.n;
      const c = d >= 0 ? `rgba(53,196,139,${Math.min(.85, .2 + Math.abs(d) * 3)})` : `rgba(240,89,107,${Math.min(.85, .2 + Math.abs(d) * 3)})`;
      return `<rect x="${i * 3}" y="${j * 3}" width="2.8" height="2.8" rx=".4" fill="${c}" opacity="${Math.min(1, .35 + b.n / 40)}"><title>${b.n} FGA · FG ${(100 * b.m / b.n).toFixed(0)}% · xFG ${(100 * b.x / b.n).toFixed(0)}%</title></rect>`;
    }).join("");
  } else if (mode === "xfg") {
    marks = shots.map((s) => `<circle cx="${s.x}" cy="${s.y}" r=".45" fill="hsl(${Math.round(s.xfg * 240)},75%,55%)" opacity=".7"/>`).join("");
  } else {
    marks = shots.map((s) => s.made ? `<circle cx="${s.x}" cy="${s.y}" r=".45" fill="#35c48b" opacity=".75"/>` : `<g stroke="#f0596b" stroke-width=".22" opacity=".6"><line x1="${s.x - .4}" y1="${s.y - .4}" x2="${s.x + .4}" y2="${s.y + .4}"/><line x1="${s.x - .4}" y1="${s.y + .4}" x2="${s.x + .4}" y2="${s.y - .4}"/></g>`).join("");
  }
  return `<svg viewBox="-1 -6.5 ${W + 2} 49.5" style="width:100%;max-width:520px;display:block;margin:auto;background:#0a1120;border-radius:6px" role="img" aria-label="Shot chart">${lines}${marks}</svg>`;
}

function shotPanel(id, data, title) {
  const zones = data.zones.map((z) => `<tr><td>${esc(z.zone.replaceAll("_", " "))}</td><td class="n">${z.fga}</td><td class="n">${fmt(z.share)}</td><td class="n">${fmt(z.fg_pct)}</td><td class="n">${fmt(z.xfg_pct)}</td><td class="n ${z.smoe >= 0 ? "pos" : "neg"}">${signed(z.smoe)}</td><td class="n">${fmt(z.league_fg_pct)}</td><td class="n">${fmt(z.pps, 2)}</td></tr>`).join("");
  return `<div class="panel"><div class="panel-head"><h2>${esc(title)} · ${data.total} FGA</h2>
    <div class="seg" data-chart="${id}"><button data-mode="dots" class="on">Makes/misses</button><button data-mode="xfg">Expected FG%</button><button data-mode="heat">Above expected</button></div></div>
    <div id="${id}">${court(data.shots, { mode: "dots" })}</div>
    <div class="legend"><span style="--c:#35c48b">made</span><span style="--c:#f0596b">missed</span><span style="--c:hsl(200,75%,55%)">xFG hue: red low → blue high</span><span style="--c:rgba(53,196,139,.7)">heat: FG% minus xFG</span></div>
    <div class="tbl-wrap" style="margin-top:8px"><table><thead><tr><th>Zone</th><th class="n">FGA</th><th class="n">Share%</th><th class="n">FG%</th><th class="n">xFG%</th><th class="n">vs exp</th><th class="n">Lg FG%</th><th class="n">PPS</th></tr></thead><tbody>${zones}</tbody></table></div>
    <p class="note">xFG: logistic model on location, zone, shot type and possession context, trained through ${esc(data.model?.trained_through || "—")}; holdout log loss ${fmt(data.model?.evaluation?.log_loss, 4)} vs base-rate ${fmt(data.model?.evaluation?.baseline_log_loss, 4)}. No defender-distance data.</p></div>`;
}
function wireShotToggles(root, data) {
  $$(".seg[data-chart]", root).forEach((seg) => seg.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    $$("button", seg).forEach((x) => x.classList.toggle("on", x === b));
    $(`#${seg.dataset.chart}`).innerHTML = court(data[seg.dataset.chart].shots, { mode: b.dataset.mode });
  }));
}

/* ---------------------------------------------------------------- charts */
function histogram(edges, counts, opts = {}) {
  const W = 520, H = 150, pad = 26, n = counts.length, max = Math.max(...counts, 1);
  const bw = (W - pad * 2) / n;
  const bars = counts.map((c, i) => {
    const x = pad + i * bw, h = (H - pad - 10) * c / max, mid = (edges[i] + edges[i + 1]) / 2;
    const col = opts.split !== undefined ? (mid > opts.split ? "var(--blue)" : "var(--gold)") : "var(--blue)";
    return `<rect x="${x}" y="${H - pad - h}" width="${Math.max(1, bw - 1)}" height="${h}" fill="${col}" opacity=".8"><title>${mid}: ${c}</title></rect>`;
  }).join("");
  const ticks = [0, .25, .5, .75, 1].map((f) => { const i = Math.min(n - 1, Math.round(f * (n - 1))); return `<text x="${pad + i * bw}" y="${H - 8}">${Math.round((edges[i] + edges[i + 1]) / 2)}</text>`; }).join("");
  const mark = opts.mark !== undefined ? (() => { const i = edges.findIndex((e) => e > opts.mark) - 1; return i >= 0 ? `<line x1="${pad + i * bw}" x2="${pad + i * bw}" y1="6" y2="${H - pad}" stroke="var(--text)" stroke-dasharray="3 3"/>` : ""; })() : "";
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%">${bars}${mark}${ticks}<line x1="${pad}" x2="${W - pad}" y1="${H - pad}" y2="${H - pad}" stroke="#2a3a5c"/></svg>`;
}
function lineChart(series, opts = {}) {
  const W = 620, H = 170, pad = 30, all = series.flatMap((s) => s.values).filter((v) => v !== null);
  if (!all.length) return `<div class="muted">No data.</div>`;
  const lo = Math.min(...all, opts.zero ? 0 : Infinity), hi = Math.max(...all, opts.zero ? 0 : -Infinity), n = Math.max(...series.map((s) => s.values.length));
  const X = (i) => pad + (W - 2 * pad) * i / Math.max(1, n - 1), Y = (v) => H - pad - (H - 2 * pad) * (v - lo) / Math.max(1e-9, hi - lo);
  const paths = series.map((s) => `<path d="${s.values.map((v, i) => (v === null ? "" : `${i ? "L" : "M"}${X(i)},${Y(v)}`)).join("")}" fill="none" stroke="${s.color}" stroke-width="1.6"/>`).join("");
  const zero = opts.zero ? `<line x1="${pad}" x2="${W - pad}" y1="${Y(0)}" y2="${Y(0)}" stroke="#2a3a5c" stroke-dasharray="3 3"/>` : "";
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%">${zero}${paths}<text x="2" y="${Y(hi) + 4}">${hi.toFixed(1)}</text><text x="2" y="${Y(lo)}">${lo.toFixed(1)}</text></svg>
    <div class="legend">${series.map((s) => `<span style="--c:${s.color}">${esc(s.label)}</span>`).join("")}</div>`;
}
function calibrationChart(bins, label) {
  const W = 300, H = 300, pad = 30, X = (v) => pad + (W - 2 * pad) * v, Y = (v) => H - pad - (H - 2 * pad) * v;
  const max = Math.max(...bins.map((b) => b.count), 1);
  const pts = bins.map((b) => `<circle cx="${X(b.predicted)}" cy="${Y(b.observed)}" r="${3 + 7 * Math.sqrt(b.count / max)}" fill="var(--gold)" opacity=".75"><title>${b.count} games · predicted ${(100 * b.predicted).toFixed(1)}% · observed ${(100 * b.observed).toFixed(1)}%</title></circle>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:320px"><rect x="${pad}" y="${pad}" width="${W - 2 * pad}" height="${H - 2 * pad}" fill="none" stroke="#2a3a5c"/>
    <line x1="${X(0)}" y1="${Y(0)}" x2="${X(1)}" y2="${Y(1)}" stroke="#56627f" stroke-dasharray="4 4"/>${pts}
    <text x="${pad}" y="${H - 8}">predicted →</text><text x="4" y="${pad - 8}">observed</text><text x="${W - 70}" y="${pad - 8}">${esc(label)}</text></svg>`;
}

/* ---------------------------------------------------------------- A: player */
function wirePlayer() {
  let timer;
  $("#p-search").addEventListener("input", (e) => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const v = e.target.value.trim();
      if (v.length < 2) { $("#p-suggest").hidden = true; return; }
      const r = await api(`/api/intel/players?${q({ q: v, limit: 15 })}`);
      $("#p-suggest").innerHTML = r.players.map((p) => `<div data-id="${p.id}"><span>${esc(p.name)}</span><span class="muted">${esc(p.team || "")} · ${esc(p.position)}</span></div>`).join("") || `<div class="muted">No match</div>`;
      $("#p-suggest").hidden = false;
    }, 180);
  });
  $("#p-suggest").addEventListener("click", (e) => { const d = e.target.closest("[data-id]"); if (!d) return; $("#p-suggest").hidden = true; $("#p-search").value = d.firstElementChild.textContent; loadPlayer(d.dataset.id); });
  $("#p-show").addEventListener("change", () => S.player && renderPlayer());
  document.addEventListener("click", (e) => { if (!e.target.closest(".search")) $("#p-suggest").hidden = true; });
}

async function loadPlayer(id) {
  const el = $("#p-body"); loading(el, "Building the league board and player profile (first load fits models; later loads are cached)…");
  try {
    const [prof, shots] = await Promise.all([api(`/api/intel/player/${id}?${q({ season: S.season })}`), api(`/api/intel/shots?${q({ player_id: id, season: S.season })}`)]);
    S.player = prof; S.playerShots = shots; S.pcat = S.pcat || "Scoring";
    renderPlayer();
  } catch (e) { fail(el, e); }
}

const PCATS = ["Scoring", "Shooting efficiency", "Shot profile", "Playmaking", "Ball handling and usage", "Rebounding", "Defense", "Movement and athleticism", "Player impact", "Consistency and availability"];
function categoryOf(m, idx) { return m.specialization ? `${m.specialization} specialization` : PCATS[Math.floor(idx / 10)]; }

function renderPlayer() {
  const p = S.player, el = $("#p-body");
  if (!p.metrics.length) { el.innerHTML = `<div class="empty">${esc(p.player.name)} has no games in this season window.</div>`; return; }
  const by = {}; p.metrics.forEach((m, i) => { const c = categoryOf(m, i); (by[c] ||= []).push(m); });
  const showAll = $("#p-show").value === "all";
  const get = (id) => p.metrics.find((m) => m.id === id)?.value;
  const cards = [["PTS/G", get("p_pts_g"), 1], ["TS%", get("p_ts"), 1], ["USG%", get("p_usg"), 1], ["AST/100", get("p_ast_100"), 1], ["RAPM", get("p_rapm"), 2], ["On/off", get("p_onoff"), 1]]
    .map(([l, v, d]) => `<div class="card"><div class="l">${l}</div><div class="v ${l === "RAPM" ? "gold" : ""}">${fmt(v, d)}</div></div>`).join("");
  const measured = p.metrics.filter((m) => m.value !== null).length;
  const tabs = Object.keys(by).map((c) => `<button data-c="${esc(c)}" class="${c === S.pcat ? "on" : ""}">${esc(c)} <span class="muted">${by[c].filter((m) => m.value !== null).length}/${by[c].length}</span></button>`).join("");
  const rows = (by[S.pcat] || by[Object.keys(by)[0]]).filter((m) => showAll || m.value !== null).map((m) => `<tr class="${m.value === null ? "unavail" : ""}" title="${esc(m.formula || m.reason || "")}">
      <td>${esc(m.label)}</td><td class="n">${fmt(m.value, Math.abs(m.value) >= 100 ? 0 : 2)}</td><td>${st(m.status)}</td><td class="muted">${esc(m.unit)}</td>
      <td>${pbar(m.league_pct)}</td><td>${pbar(m.position_pct)}</td><td>${pbar(m.role_pct)}</td>
      <td class="n">${fmt(m.recent, 2)}</td><td class="n ${m.trend > 0 ? "pos" : m.trend < 0 ? "neg" : ""}">${signed(m.trend, 2)}</td>
      <td class="n muted">${m.se !== null && m.se !== undefined ? "±" + fmt(m.se, 2) : "—"}</td><td class="n">${fmt(m.opponent_adjusted, 2)}</td><td class="n muted">${fmt(m.sample, 0)}</td>
      <td class="n">${fmt(m.baseline, 2)}</td></tr>`).join("") || `<tr><td colspan="13" class="muted">No measured metrics in this category — toggle "All slots" to see unavailable definitions and reasons.</td></tr>`;
  const log = p.game_log.map((g) => `<tr><td class="mono">${esc(g.tipoff.slice(0, 10))}</td><td>${g.home ? "vs" : "@"} ${esc(teamName(g.opponent_id))}</td><td class="n">${g.dnp ? "DNP" : fmt(g.minutes, 0)}</td><td class="n">${g.pts}</td><td class="n">${g.reb}</td><td class="n">${g.ast}</td><td class="n">${g.fg3m}</td><td class="n">${g.fgm}-${g.fga}</td><td class="n">${g.stl}</td><td class="n">${g.blk}</td><td class="n">${g.tov}</td><td class="n ${g.plus_minus > 0 ? "pos" : g.plus_minus < 0 ? "neg" : ""}">${signed(g.plus_minus, 0)}</td></tr>`).join("");
  el.innerHTML = `
    <div class="panel" style="margin-bottom:12px"><div class="panel-head"><div><h3 style="font-size:17px;margin:0">${esc(p.player.name)} <span class="muted" style="font-size:12px">${esc(p.team?.name || "")} · ${esc(p.position)} · #${esc(p.player.jersey || "")}</span></h3>
      <div style="margin-top:4px">${p.archetypes.map((a, i) => `<span class="tag ${i === 0 ? "gold" : ""}">${esc(a)}</span>`).join("") || `<span class="muted">No archetype threshold met</span>`}</div></div>
      <div class="mono muted">${p.games_played} GP · ${fmt(p.minutes, 0)} MIN · ${measured}/${p.metrics.length} measured · ${p.qualified ? "qualified" : "below percentile qualifier"}</div></div>
      <div class="grid g6">${cards}</div><p class="note">${esc(p.percentile_note)} Trend = last 10 games minus window. Baseline = previous season (${esc(p.baseline_season || "none ingested")}). Opp-adj multiplies by league ORtg / minutes-weighted opponent adjusted DRtg (factor ${fmt(p.opponent_factor, 3)}).</p></div>
    <div class="panel" style="margin-bottom:12px"><div class="tabs" id="p-tabs">${tabs}</div>
      <div class="tbl-wrap scroll"><table><thead><tr><th>Metric</th><th class="n">Value</th><th>Class</th><th>Unit</th><th>League</th><th>Position</th><th>Role</th><th class="n">Last 10</th><th class="n">Δ</th><th class="n">SE</th><th class="n">Opp-adj</th><th class="n">n</th><th class="n">Prev season</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="note">Hover a row for its formula or unavailability reason. Classes: OBS observed, DER derived, EST estimated proxy, MOD model output, UNA no authorized source.</p></div>
    <div class="grid g2">${shotPanel("pshots", S.playerShots, "Shot chart")}
      <div class="panel"><div class="panel-head"><h2>Next-game projection</h2></div>
        <div class="controls"><label class="f">Opponent<select id="p-opp">${S.teams.filter((t) => t.id !== p.team_id).map((t) => `<option value="${t.id}">${esc(t.abbreviation)}</option>`).join("")}</select></label>
        <label class="f">Venue<select id="p-venue"><option value="home">Home</option><option value="away">Away</option></select></label><button class="btn primary" id="p-proj">Simulate 10,000 →</button></div>
        <div id="p-proj-out" class="muted">Runs the full game simulation and extracts this player's joint box-score distribution.</div>
        <h2 style="margin-top:14px">Game log</h2><div class="tbl-wrap scroll" style="max-height:300px"><table><thead><tr><th>Date</th><th>Opp</th><th class="n">MIN</th><th class="n">PTS</th><th class="n">REB</th><th class="n">AST</th><th class="n">3PM</th><th class="n">FG</th><th class="n">STL</th><th class="n">BLK</th><th class="n">TOV</th><th class="n">+/-</th></tr></thead><tbody>${log}</tbody></table></div></div></div>`;
  $("#p-tabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { S.pcat = b.dataset.c; renderPlayer(); } });
  wireShotToggles(el, { pshots: S.playerShots });
  $("#p-proj").addEventListener("click", async () => {
    const home = $("#p-venue").value === "home" ? p.team_id : $("#p-opp").value, away = $("#p-venue").value === "home" ? $("#p-opp").value : p.team_id;
    const out = $("#p-proj-out"); loading(out, "Simulating…");
    try {
      const r = await runJob({ home_id: home, away_id: away, simulations: 10000, seed: 42 });
      const pl = r.players.find((x) => x.id === p.player.id);
      if (!pl) { out.innerHTML = `<div class="muted">${esc(p.player.name)} is not on the projected roster (no recent appearance for this team before the cutoff).</div>`; return; }
      out.innerHTML = playerDistTable([pl]) + `<p class="note">Win prob ${pct(home === p.team_id ? r.home_win_probability : r.away_win_probability)} for ${esc(teamName(p.team_id))}. Played probability ${pct(pl.played_prob)}; double-double ${pct(pl.double_double)}.</p>`;
    } catch (e) { fail(out, e); }
  });
}

/* ---------------------------------------------------------------- B: team */
function wireTeam() { $("#t-load").addEventListener("click", loadTeam); $("#t-show").addEventListener("change", () => S.team && renderTeam()); }
async function loadTeam() {
  const el = $("#t-body"); loading(el, "Aggregating possessions, shots, stints and schedule for all 30 teams…");
  const id = $("#t-team").value;
  try {
    const [t, off, def] = await Promise.all([
      api(`/api/intel/team/${id}?${q({ season: S.season, venue: $("#t-venue").value, last_n: $("#t-last").value, opponent_id: $("#t-opp").value })}`),
      api(`/api/intel/shots?${q({ team_id: id, season: S.season })}`), api(`/api/intel/shots?${q({ opponent_id: id, season: S.season })}`)]);
    S.team = t; S.teamShots = { tshots_o: off, tshots_d: def }; S.tcat = S.tcat || "Overall team strength";
    renderTeam();
  } catch (e) { fail(el, e); }
}
function renderTeam() {
  const t = S.team, el = $("#t-body"), showAll = $("#t-show").value === "all";
  const cats = [...new Set(t.metrics.map((m) => m.category))];
  const get = (id) => t.metrics.find((m) => m.id === id);
  const card = (l, id, d = 1) => { const m = get(id); return `<div class="card"><div class="l">${l}</div><div class="v">${fmt(m?.value, d)}</div><div class="s">rank ${m?.rank ?? "—"}/${m?.of ?? "—"}</div></div>`; };
  const tabs = cats.map((c) => { const ms = t.metrics.filter((m) => m.category === c); return `<button data-c="${esc(c)}" class="${c === S.tcat ? "on" : ""}">${esc(c)} <span class="muted">${ms.filter((m) => m.value !== null).length}/${ms.length}</span></button>`; }).join("");
  const rows = t.metrics.filter((m) => m.category === S.tcat && (showAll || m.value !== null)).map((m) => `<tr class="${m.value === null ? "unavail" : ""}" title="${esc(m.formula || m.reason || "")}">
    <td>${esc(m.label)}</td><td class="n">${fmt(m.value, Math.abs(m.value) >= 100 ? 1 : 2)}</td><td>${st(m.status)}</td><td class="muted">${esc(m.unit)}</td>
    <td class="n">${m.rank ? `${m.rank}/${m.of}` : "—"}</td><td>${pbar(m.league_pct)}</td><td class="n">${fmt(m.league_avg, 2)}</td><td class="n muted">${m.se !== null && m.se !== undefined ? "±" + fmt(m.se, 2) : "—"}</td><td class="n muted">${fmt(m.sample, 0)}</td>
    <td class="muted" style="white-space:normal;max-width:360px">${esc(m.value === null ? m.reason || "" : "")}</td></tr>`).join("");
  const measured = t.metrics.filter((m) => m.value !== null).length;
  const rolling = lineChart([{ label: "10-game rolling net", color: "var(--gold)", values: t.game_log.map((g) => g.roll10_net) },
    { label: "Game ORtg - DRtg", color: "rgba(61,139,253,.5)", values: t.game_log.map((g) => g.ortg - g.drtg) }], { zero: true });
  const quarters = t.quarters.map((x) => `<tr><td>${x.period <= 4 ? "Q" + x.period : "OT" + (x.period - 4)}</td><td class="n">${fmt(x.pts / x.games, 1)}</td><td class="n">${fmt(x.opp / x.games, 1)}</td><td class="n ${x.pts > x.opp ? "pos" : "neg"}">${signed((x.pts - x.opp) / x.games, 1)}</td><td class="n muted">${x.games}</td></tr>`).join("");
  const lus = t.lineups.slice(0, 10).map((l) => `<tr><td style="white-space:normal">${l.names.map(esc).join(", ")}</td><td class="n">${fmt(l.minutes, 0)}</td><td class="n">${fmt(l.net, 1)}</td><td class="n gold">${fmt(l.adjusted_net, 1)}</td><td class="n ${l.interaction >= 0 ? "pos" : "neg"}">${signed(l.interaction, 1)}</td></tr>`).join("");
  el.innerHTML = `<div class="panel" style="margin-bottom:12px"><div class="panel-head"><h3 style="font-size:17px;margin:0">${esc(t.team.name)}</h3><span class="mono muted">${t.games} games in filter · ${measured}/${t.metrics.length} measured · cutoff ${esc(t.cutoff)}</span></div>
      <div class="grid g6">${card("Net rating", "t_net")}${card("Adj. net", "t_net_adj", 2)}${card("ORtg", "t_ortg")}${card("DRtg", "t_drtg")}${card("Pace", "t_pace")}${card("Expected wins", "t_xwins")}</div></div>
    <div class="panel" style="margin-bottom:12px"><div class="tabs" id="t-tabs">${tabs}</div>
      <div class="tbl-wrap scroll"><table><thead><tr><th>Metric</th><th class="n">Value</th><th>Class</th><th>Unit</th><th class="n">Rank</th><th>Percentile</th><th class="n">League avg</th><th class="n">SE</th><th class="n">n</th><th>Unavailable because</th></tr></thead><tbody>${rows || `<tr><td colspan="10" class="muted">No measured metrics in this category — toggle "All slots".</td></tr>`}</tbody></table></div>
      <p class="note">Filters apply to box-score, possession, shot and schedule aggregates. Ranks are among teams with games in the same filter. Lineup metrics use the full season window.</p></div>
    <div class="grid g2" style="margin-bottom:12px"><div class="panel"><h2>Rolling efficiency</h2>${rolling}</div>
      <div class="panel"><h2>Quarter splits (per game)</h2><table><thead><tr><th>Period</th><th class="n">For</th><th class="n">Against</th><th class="n">Diff</th><th class="n">Games</th></tr></thead><tbody>${quarters}</tbody></table>
      <h2 style="margin-top:12px">Top lineups</h2><div class="tbl-wrap"><table><thead><tr><th>Lineup</th><th class="n">Min</th><th class="n">Net</th><th class="n">Adj</th><th class="n">Chem</th></tr></thead><tbody>${lus}</tbody></table></div></div></div>
    <div class="grid g2">${shotPanel("tshots_o", S.teamShots.tshots_o, "Offense shot chart")}${shotPanel("tshots_d", S.teamShots.tshots_d, "Shots allowed")}</div>`;
  $("#t-tabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { S.tcat = b.dataset.c; renderTeam(); } });
  wireShotToggles(el, S.teamShots);
}

/* ---------------------------------------------------------------- C: lineups */
function wireLineup() {
  $("#l-load").addEventListener("click", loadLineups);
  $("#l-team").addEventListener("change", () => { $("#l-inc").innerHTML = `<option value="">Anyone</option>`; $("#l-exc").innerHTML = `<option value="">Nobody</option>`; loadLineups(); });
  $("#l-sort").addEventListener("change", () => S.lineups && renderLineups());
}
async function loadLineups() {
  const el = $("#l-body"); loading(el);
  try {
    S.lineups = await api(`/api/intel/lineups/${$("#l-team").value}?${q({ season: S.season, min_poss: $("#l-min").value, include: $("#l-inc").value, exclude: $("#l-exc").value })}`);
    const keepI = $("#l-inc").value, keepE = $("#l-exc").value;
    if ($("#l-inc").options.length <= 1) {
      const opts = S.lineups.roster.map((p) => `<option value="${p.id}">${esc(p.name)} (${esc(p.position)})</option>`).join("");
      $("#l-inc").innerHTML = `<option value="">Anyone</option>${opts}`; $("#l-exc").innerHTML = `<option value="">Nobody</option>${opts}`;
      $("#l-inc").value = keepI; $("#l-exc").value = keepE;
    }
    S.lsel = [];
    renderLineups();
  } catch (e) { fail(el, e); }
}
function renderLineups() {
  const L = S.lineups, key = $("#l-sort").value, el = $("#l-body");
  const rows = [...L.lineups].sort((a, b) => b[key] - a[key]);
  const body = rows.slice(0, 120).map((l, i) => `<tr data-i="${L.lineups.indexOf(l)}" class="${S.lsel.includes(L.lineups.indexOf(l)) ? "sel" : ""}" style="cursor:pointer">
      <td style="white-space:normal;min-width:260px">${l.players.map((p) => `${esc(p.name)} <span class="muted">${esc(p.position)}</span>`).join(" · ")}</td>
      <td class="n">${fmt(l.minutes, 0)}</td><td class="n">${l.possessions}</td><td class="n">${fmt(l.ortg)}</td><td class="n">${fmt(l.drtg)}</td>
      <td class="n ${l.net >= 0 ? "pos" : "neg"}">${signed(l.net)}</td><td class="n gold">${signed(l.adjusted_net)}</td><td class="n">${signed(l.talent_prior)}</td>
      <td class="n ${l.interaction >= 0 ? "pos" : "neg"}">${signed(l.interaction)}</td><td class="n muted">±${fmt(l.uncertainty_sd)}</td>
      <td class="n">${fmt(l.three_rate)}</td><td class="n">${fmt(l.oreb_pct)}</td><td class="n">${fmt(l.dreb_pct)}</td><td class="n">${fmt(l.tov_pct)}</td>
      <td style="white-space:normal">${l.role_balance.flags.map((f) => `<span class="tag">${esc(f)}</span>`).join("") || `<span class="muted">balanced</span>`}</td></tr>`).join("");
  const cmp = S.lsel.length === 2 ? compareLineups(L.lineups[S.lsel[0]], L.lineups[S.lsel[1]]) : `<p class="muted">Click two rows to compare lineups side by side.</p>`;
  el.innerHTML = `<div class="panel" style="margin-bottom:12px"><div class="panel-head"><h2>${esc(teamName(L.team_id))} five-man units · ${L.lineups.length} with ≥ ${esc($("#l-min").value)} possessions</h2><span class="muted mono">season ${L.season} · cutoff ${esc(L.cutoff)}</span></div>
    <div class="tbl-wrap scroll"><table><thead><tr><th>Lineup</th><th class="n">Min</th><th class="n">Poss</th><th class="n">ORtg</th><th class="n">DRtg</th><th class="n">Net</th><th class="n">Adj net</th><th class="n">Talent</th><th class="n">Chemistry</th><th class="n">SD</th><th class="n">3PA%</th><th class="n">OREB%</th><th class="n">DREB%</th><th class="n">TOV%</th><th>Role flags</th></tr></thead><tbody>${body || `<tr><td colspan="15" class="muted">No lineups meet the filter.</td></tr>`}</tbody></table></div>
    <p class="note">${esc(L.method)} "Without" filters lineups that exclude a player — the injury substitution view of which units remain and how they performed. Role flags are heuristic.</p></div><div class="panel">${cmp}</div>`;
  $$("tbody tr[data-i]", el).forEach((tr) => tr.addEventListener("click", () => {
    const i = Number(tr.dataset.i); S.lsel = S.lsel.includes(i) ? S.lsel.filter((x) => x !== i) : [...S.lsel, i].slice(-2); renderLineups();
  }));
}
function compareLineups(a, b) {
  const keys = [["Minutes", "minutes", 0], ["Possessions", "possessions", 0], ["ORtg", "ortg", 1], ["DRtg", "drtg", 1], ["Net", "net", 1], ["Adjusted net", "adjusted_net", 1], ["Talent prior", "talent_prior", 2], ["Offensive talent", "talent_prior_off", 2], ["Defensive talent", "talent_prior_def", 2], ["Chemistry", "interaction", 2], ["Uncertainty SD", "uncertainty_sd", 1], ["3PA rate", "three_rate", 1], ["FT rate", "ft_rate", 1], ["OREB%", "oreb_pct", 1], ["DREB%", "dreb_pct", 1], ["TOV%", "tov_pct", 1], ["Opp TOV%", "opp_tov_pct", 1]];
  const names = (l) => l.players.map((p) => esc(p.name.split(" ").slice(-1)[0])).join(" · ");
  return `<h2>Lineup comparison</h2><table><thead><tr><th>Measure</th><th class="n">${names(a)}</th><th class="n">${names(b)}</th><th class="n">Diff</th></tr></thead><tbody>${keys.map(([l, k, d]) => `<tr><td>${l}</td><td class="n">${fmt(a[k], d)}</td><td class="n">${fmt(b[k], d)}</td><td class="n">${signed(a[k] - b[k], d)}</td></tr>`).join("")}</tbody></table>
    <p class="note">Differences under ~2 SD of the uncertainty column are not distinguishable from noise.</p>`;
}

/* ---------------------------------------------------------------- D: matchup */
function wireMatchup() {
  $("#m-load").addEventListener("click", loadMatchup);
  $("#m-to-predict").addEventListener("click", () => { $("#e-home").value = $("#m-home").value; $("#e-away").value = $("#m-away").value; switchWs("predict"); });
}
async function loadMatchup() {
  const el = $("#m-body"); loading(el);
  try { const m = await api(`/api/intel/matchup?${q({ home_id: $("#m-home").value, away_id: $("#m-away").value, season: S.season })}`); S.matchupLoaded = true; renderMatchup(m); } catch (e) { fail(el, e); }
}
function matchupSide(title, s) {
  const z = s.zones.map((r) => `<tr><td>${esc(r.zone)}</td><td class="n">${fmt(r.offense_share)}</td><td class="n">${fmt(r.defense_share_allowed)}</td><td class="n">${fmt(r.league_share)}</td><td class="n">${fmt(r.offense_fg)}</td><td class="n">${fmt(r.defense_fg_allowed)}</td><td class="n">${fmt(r.league_fg)}</td><td class="n ${r.edge_pp >= 0 ? "pos heat-pos" : "neg heat-neg"}">${signed(r.edge_pp)}</td></tr>`).join("");
  const f = s.four_factors;
  return `<div class="panel"><h2>${esc(title)}</h2><div class="tbl-wrap"><table><thead><tr><th>Zone</th><th class="n">Off share</th><th class="n">Def allows</th><th class="n">Lg share</th><th class="n">Off FG%</th><th class="n">Def FG% allowed</th><th class="n">Lg FG%</th><th class="n">Edge pp</th></tr></thead><tbody>${z}</tbody></table></div>
    <div class="grid g4" style="margin-top:10px">
      <div class="card"><div class="l">eFG% off / allowed</div><div class="v" style="font-size:15px">${fmt(f.offense_efg)} / ${fmt(f.defense_efg_allowed)}</div></div>
      <div class="card"><div class="l">TOV% off / forced</div><div class="v" style="font-size:15px">${fmt(f.offense_tov)} / ${fmt(f.defense_forced_tov)}</div></div>
      <div class="card"><div class="l">OREB% / opp DREB%</div><div class="v" style="font-size:15px">${fmt(f.offense_orb)} / ${fmt(f.defense_drb)}</div></div>
      <div class="card"><div class="l">FTr off / allowed</div><div class="v" style="font-size:15px">${fmt(f.offense_ftr)} / ${fmt(f.defense_ftr_allowed)}</div></div></div>
    <div class="kv" style="margin-top:10px"><span>Shot quality (xPPS) off / allowed</span><span class="mono">${fmt(s.shot_quality.offense_xpps, 3)} / ${fmt(s.shot_quality.defense_xpps_allowed, 3)}</span>
      <span>Shot making vs expected off / allowed</span><span class="mono">${signed(s.shot_quality.offense_smoe)} / ${signed(s.shot_quality.defense_smoe_allowed)} pp</span>
      <span>Transition freq · PPP · allowed PPP</span><span class="mono">${fmt(s.transition.offense_freq)}% · ${fmt(s.transition.offense_ppp, 3)} · ${fmt(s.transition.defense_ppp_allowed, 3)}</span></div></div>`;
}
function keyPlayers(title, rows) {
  return `<div class="panel"><h2>${esc(title)}</h2><div class="tbl-wrap"><table><thead><tr><th>Player</th><th class="n">MIN</th><th class="n">USG</th><th class="n">Rim rate</th><th class="n">3PA rate</th><th>Rim FG / opp allows</th><th>Corner 3 / allows</th><th>AB 3 / allows</th></tr></thead><tbody>
    ${rows.map((r) => `<tr><td>${esc(r.name)} <span class="muted">${esc(r.position)}</span><div>${r.archetypes.slice(0, 2).map((a) => `<span class="tag">${esc(a)}</span>`).join("")}</div></td><td class="n">${fmt(r.min_g)}</td><td class="n">${fmt(r.usage)}</td><td class="n">${fmt(r.rim_rate)}</td><td class="n">${fmt(r.three_rate)}</td>
      ${r.zone_vs_defense.map((z) => `<td class="mono">${fmt(z.player_fg)} <span class="muted">(${z.attempts})</span> / ${fmt(z.opp_allowed_fg)}</td>`).join("")}</tr>`).join("")}</tbody></table></div></div>`;
}
function renderMatchup(m) {
  $("#m-body").innerHTML = `<div class="panel" style="margin-bottom:12px"><h2>Projected tactical adjustments (data-derived)</h2><ul style="margin:0;padding-left:18px">${m.tactical_notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>
      <p class="note">${esc(m.method)} Unavailable: ${m.unavailable.map(esc).join("; ")}.</p></div>
    <div class="grid g2" style="margin-bottom:12px">${matchupSide(`${m.home.abbreviation} offense vs ${m.away.abbreviation} defense`, m.home_offense_vs_away_defense)}${matchupSide(`${m.away.abbreviation} offense vs ${m.home.abbreviation} defense`, m.away_offense_vs_home_defense)}</div>
    <div class="grid g2">${keyPlayers(`${m.home.abbreviation} key players vs ${m.away.abbreviation} zone defense`, m.key_players.home)}${keyPlayers(`${m.away.abbreviation} key players vs ${m.home.abbreviation} zone defense`, m.key_players.away)}</div>`;
}

/* ---------------------------------------------------------------- E: prediction */
function wirePredict() {
  $("#e-day").value = new Date().toISOString().slice(0, 10);
  $("#e-day-load").addEventListener("click", loadSchedule);
  $("#e-run").addEventListener("click", runPredict);
  $("#e-roster").addEventListener("click", toggleScenarios);
  ["#e-home", "#e-away"].forEach((s) => $(s).addEventListener("change", () => { S.scen = {}; if (!$("#e-scen").hidden) loadScenarioRosters(); }));
}
async function loadSchedule() {
  S.scheduleLoaded = true;
  const el = $("#e-schedule");
  try {
    const r = await api(`/api/intel/schedule?${q({ day: $("#e-day").value })}`);
    el.innerHTML = r.games.length ? `<div style="display:flex;gap:6px;flex-wrap:wrap">${r.games.map((g) => `<button class="btn small" data-h="${g.home_id}" data-a="${g.away_id}" data-t="${g.tipoff}">${esc(g.away)} @ ${esc(g.home)} <span class="muted">${g.completed ? `${g.away_score}-${g.home_score} F` : new Date(g.tipoff).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</span></button>`).join("")}</div>` : `<span class="muted">No NBA games on ${esc(r.date)}.</span>`;
    $$("button[data-h]", el).forEach((b) => b.addEventListener("click", () => {
      $("#e-home").value = b.dataset.h; $("#e-away").value = b.dataset.a; S.tipoff = b.dataset.t; S.scen = {};
      const tip = new Date(b.dataset.t);
      // Completed games: default the cutoff to one hour before tipoff so the forecast is pregame-only.
      if (tip < new Date()) $("#e-cutoff").value = new Date(tip.getTime() - 3600e3).toISOString().slice(0, 16); else $("#e-cutoff").value = "";
      if (!$("#e-scen").hidden) loadScenarioRosters();
    }));
  } catch (e) { el.innerHTML = `<span class="neg">${esc(e.message)}</span>`; }
}
async function toggleScenarios() { const p = $("#e-scen"); p.hidden = !p.hidden; if (!p.hidden) loadScenarioRosters(); }
async function loadScenarioRosters() {
  const p = $("#e-scen"); loading(p, "Loading projected rosters…");
  const cutoff = $("#e-cutoff").value ? $("#e-cutoff").value + ":00Z" : "";
  try {
    const [h, a] = await Promise.all([api(`/api/intel/roster/${$("#e-home").value}?${q({ cutoff })}`), api(`/api/intel/roster/${$("#e-away").value}?${q({ cutoff })}`)]);
    const block = (r) => `<div><h3>${esc(teamName(r.team_id))}</h3><table><thead><tr><th>Player</th><th class="n">L5 min</th><th class="n">Missed</th><th>Status</th><th>Min cap</th></tr></thead><tbody>${r.players.map((pl) => {
      const s = S.scen[pl.id] || {};
      return `<tr><td>${esc(pl.name)} <span class="muted">${esc(pl.position)}</span></td><td class="n">${fmt(pl.recent_minutes)}</td><td class="n">${pl.missed_last}</td>
      <td><select data-p="${pl.id}" data-k="status"><option value="">Model</option><option value="1" ${s.active_probability === 1 ? "selected" : ""}>Available</option><option value="0.5" ${s.active_probability === 0.5 ? "selected" : ""}>Questionable 50%</option><option value="0.25" ${s.active_probability === 0.25 ? "selected" : ""}>Doubtful 25%</option><option value="0" ${s.active_probability === 0 ? "selected" : ""}>Out</option></select></td>
      <td><input data-p="${pl.id}" data-k="cap" type="number" min="0" max="48" step="1" style="width:64px" value="${s.minutes_cap ?? ""}" placeholder="—"></td></tr>`; }).join("")}</tbody></table></div>`;
    p.innerHTML = `<div class="panel-head"><h2>Availability & minutes scenarios</h2><button class="btn small" id="e-scen-clear">Clear</button></div><div class="grid g2">${block(h)}${block(a)}</div>
      <p class="note">"Model" uses the fitted availability table (missed-game streak × recent role). Scenarios override it; RAPM of the remaining rotation re-anchors team efficiency.</p>`;
    $$("[data-p]", p).forEach((x) => x.addEventListener("change", () => {
      const id = x.dataset.p, s = (S.scen[id] ||= { player_id: id });
      if (x.dataset.k === "status") { if (x.value === "") delete s.active_probability; else s.active_probability = Number(x.value); }
      else { if (x.value === "") delete s.minutes_cap; else s.minutes_cap = Number(x.value); }
      if (s.active_probability === undefined && s.minutes_cap === undefined) delete S.scen[id];
    }));
    $("#e-scen-clear").addEventListener("click", () => { S.scen = {}; loadScenarioRosters(); });
  } catch (e) { fail(p, e); }
}
async function runJob(body) {
  const { job_id } = await api("/api/intel/project", { method: "POST", body: JSON.stringify(body) });
  for (;;) {
    await new Promise((r) => setTimeout(r, 900));
    const j = await api(`/api/intel/jobs/${job_id}`);
    if (j.status === "done") return j.result;
    if (j.status === "error") throw new Error(j.error);
  }
}
async function runPredict() {
  const el = $("#e-body"), btn = $("#e-run");
  const sims = Number($("#e-sims").value);
  btn.disabled = true; loading(el, `Running ${sims.toLocaleString()} possession-level simulations (plus calibration pilots)…`);
  const t0 = performance.now();
  try {
    const r = await runJob({ home_id: $("#e-home").value, away_id: $("#e-away").value, simulations: sims, seed: Number($("#e-seed").value) || 42,
      cutoff: $("#e-cutoff").value ? $("#e-cutoff").value + ":00Z" : null, tipoff: S.tipoff || null, scenarios: Object.values(S.scen) });
    S.predict = r; S.predict.wall = (performance.now() - t0) / 1000; renderPredict();
  } catch (e) { fail(el, e); } finally { btn.disabled = false; }
}
function playerDistTable(players) {
  return `<div class="tbl-wrap"><table><thead><tr><th>Player</th><th class="n">Play%</th><th class="n">MIN</th><th class="n">PTS</th><th class="n">p10–p90</th><th class="n">REB</th><th class="n">AST</th><th class="n">3PM</th><th class="n">STL</th><th class="n">BLK</th><th class="n">TOV</th><th class="n">PF</th><th class="n">PRA</th><th class="n">PRA p10–p90</th><th class="n">DD%</th><th class="n">TD%</th></tr></thead><tbody>
    ${players.map((p) => `<tr data-pid="${p.id}" style="cursor:pointer"><td>${esc(p.name)} <span class="muted">${esc(p.position)}</span></td><td class="n">${pct(p.played_prob, 0)}</td><td class="n">${fmt(p.stats.minutes.mean)}</td>
      <td class="n gold">${fmt(p.stats.pts.mean)}</td><td class="n muted">${fmt(p.stats.pts.p10, 0)}–${fmt(p.stats.pts.p90, 0)}</td><td class="n">${fmt(p.stats.reb.mean)}</td><td class="n">${fmt(p.stats.ast.mean)}</td>
      <td class="n">${fmt(p.stats.fg3m.mean)}</td><td class="n">${fmt(p.stats.stl.mean)}</td><td class="n">${fmt(p.stats.blk.mean)}</td><td class="n">${fmt(p.stats.tov.mean)}</td><td class="n">${fmt(p.stats.pf.mean)}</td>
      <td class="n">${fmt(p.stats.pra.mean)}</td><td class="n muted">${fmt(p.stats.pra.p10, 0)}–${fmt(p.stats.pra.p90, 0)}</td><td class="n">${pct(p.double_double, 1)}</td><td class="n">${pct(p.triple_double, 1)}</td></tr>`).join("")}</tbody></table></div>`;
}
function renderPredict() {
  const r = S.predict, el = $("#e-body"), H = r.home.abbreviation, A = r.away.abbreviation, [th, ta] = r.teams;
  const teamRow = (l, k, d = 1) => `<tr><td>${l}</td><td class="n">${fmt(th[k].mean, d)}</td><td class="n">${fmt(ta[k].mean, d)}</td></tr>`;
  const checks = Object.values(r.checks).every((c) => Object.values(c).every(Boolean));
  const homeP = r.players.filter((p) => p.team_id === r.home_id && p.played_prob > 0.02), awayP = r.players.filter((p) => p.team_id === r.away_id && p.played_prob > 0.02);
  el.innerHTML = `<div class="grid g6" style="margin-bottom:12px">
      <div class="card"><div class="l">Projected score</div><div class="v gold">${A} ${fmt(ta.points.mean, 1)} – ${fmt(th.points.mean, 1)} ${H}</div><div class="s">median ${fmt(ta.points.median, 0)}–${fmt(th.points.median, 0)}</div></div>
      <div class="card"><div class="l">${H} win probability</div><div class="v blue">${pct(r.home_win_probability)}</div><div class="s">MC ±${(196 * r.win_probability_mc_se).toFixed(2)} pp (95%)</div></div>
      <div class="card"><div class="l">Expected margin (${H})</div><div class="v">${signed(r.margin.mean)}</div><div class="s">80%: ${fmt(r.margin.p10, 0)} to ${fmt(r.margin.p90, 0)}</div></div>
      <div class="card"><div class="l">Total points</div><div class="v">${fmt(r.total.mean)}</div><div class="s">80%: ${fmt(r.total.p10, 0)}–${fmt(r.total.p90, 0)}</div></div>
      <div class="card"><div class="l">Pace / OT</div><div class="v">${fmt(r.pace)}</div><div class="s">OT ${pct(r.overtime_probability)}</div></div>
      <div class="card"><div class="l">Ratings-only model</div><div class="v">${pct(r.ratings_model_win_probability)}</div><div class="s">margin ${signed(r.ratings_model_margin)}</div></div></div>
    <div class="grid g2" style="margin-bottom:12px">
      <div class="panel"><h2>Margin distribution (${H} − ${A})</h2>${histogram(r.margin_histogram.edges, r.margin_histogram.counts, { split: 0, mark: 0 })}<div class="legend"><span style="--c:var(--blue)">${H} wins</span><span style="--c:var(--gold)">${A} wins</span></div></div>
      <div class="panel"><h2>Total points distribution</h2>${histogram(r.total_histogram.edges, r.total_histogram.counts, { mark: r.total.mean })}
        <div class="kv mono" style="margin-top:6px">${Object.entries(r.total_over).map(([k, v]) => `<span>Over ${k}</span><span>${pct(v)}</span>`).join("")}</div></div></div>
    <div class="grid g3" style="margin-bottom:12px">
      <div class="panel"><h2>Quarter scoring</h2><table><thead><tr><th></th><th class="n">Q1</th><th class="n">Q2</th><th class="n">Q3</th><th class="n">Q4</th><th class="n">OT</th></tr></thead><tbody>
        <tr><td>${A}</td>${ta.quarters.map((x) => `<td class="n">${fmt(x)}</td>`).join("")}<td class="n">${fmt(ta.overtime_points, 2)}</td></tr><tr><td>${H}</td>${th.quarters.map((x) => `<td class="n">${fmt(x)}</td>`).join("")}<td class="n">${fmt(th.overtime_points, 2)}</td></tr></tbody></table>
        <h2 style="margin-top:12px">Spread cover (${H} margin &gt; x)</h2><div class="kv mono">${Object.entries(r.spread_cover).map(([k, v]) => `<span>${k}</span><span>${pct(v)}</span>`).join("")}</div></div>
      <div class="panel"><h2>Team box (means)</h2><table><thead><tr><th></th><th class="n">${H}</th><th class="n">${A}</th></tr></thead><tbody>
        <tr><td>ORtg (sim / target)</td><td class="n">${fmt(th.ortg)} / ${fmt(th.target_ortg)}</td><td class="n">${fmt(ta.ortg)} / ${fmt(ta.target_ortg)}</td></tr>
        ${teamRow("Possessions", "possessions")}${teamRow("Rebounds", "reb")}${teamRow("Assists", "ast")}${teamRow("Turnovers", "tov")}${teamRow("3PA", "fg3a")}${teamRow("3PM", "fg3m")}${teamRow("FTA", "fta")}${teamRow("Steals", "stl")}${teamRow("Blocks", "blk")}${teamRow("Fouls", "pf")}
        <tr><td>RAPM availability adj (O/D)</td><td class="n">${signed(th.rapm_adjustment.offense, 2)} / ${signed(th.rapm_adjustment.defense, 2)}</td><td class="n">${signed(ta.rapm_adjustment.offense, 2)} / ${signed(ta.rapm_adjustment.defense, 2)}</td></tr></tbody></table></div>
      <div class="panel"><h2>Run provenance</h2><div class="kv mono">
        <span>Simulations</span><span>${r.simulations.toLocaleString()} (seed ${r.seed})</span><span>Sim time</span><span>${fmt(r.seconds, 2)} s · wall ${fmt(r.wall, 1)} s</span>
        <span>Info cutoff</span><span>${esc(r.cutoff)}</span><span>Team model through</span><span>${esc(r.team_model.trained_through)}</span>
        <span>Calibration shift (logit)</span><span>${r.calibration.shifts.map((x) => signed(x, 3)).join(" / ")}</span>
        <span>Accounting identities</span><span class="${checks ? "pos" : "neg"}">${checks ? "PASS (points, minutes, assists, steals, blocks)" : "FAIL"}</span>
        <span>Scenarios</span><span>${r.scenarios.length}</span><span>Version</span><span>${esc(r.version)} · key ${esc(r.key)}</span></div>
        <p class="note">Win probability is the share of simulated games won; the MC interval measures simulation noise only. Model calibration is in the audit workspace.</p></div></div>
    <div class="panel" style="margin-bottom:12px"><div class="panel-head"><h2>${A} player projections</h2><span class="muted">click a row for thresholds & drivers</span></div>${playerDistTable(awayP)}</div>
    <div class="panel" style="margin-bottom:12px"><div class="panel-head"><h2>${H} player projections</h2></div>${playerDistTable(homeP)}</div>
    <div class="panel" id="e-detail"><div class="muted">Select a player row to explore threshold probabilities, joint correlations and drivers.</div></div>`;
  $$("tr[data-pid]", el).forEach((tr) => tr.addEventListener("click", () => renderPlayerDetail(r.players.find((p) => p.id === tr.dataset.pid))));
}
function renderPlayerDetail(p) {
  const statSel = Object.keys(p.thresholds);
  const box = $("#e-detail");
  const thr = (k) => Object.entries(p.thresholds[k]).map(([t, v]) => `<span>Over ${t}</span><span>${pct(v)}</span>`).join("");
  box.innerHTML = `<div class="panel-head"><h2>${esc(p.name)} · distributions from shared game events</h2><span class="muted mono">play ${pct(p.played_prob)} · start ${pct(p.start_prob)} · foul trouble (≥4 PF) ${pct(p.foul_trouble_prob)}</span></div>
    <div class="grid g3"><div><div class="controls"><label class="f">Statistic<select id="d-stat">${statSel.map((k) => `<option>${k}</option>`).join("")}</select></label><label class="f">Custom line<input id="d-line" type="number" step="0.5" style="width:90px"></label></div>
      <div class="kv mono" id="d-thr">${thr(statSel[0])}</div><div id="d-custom" class="mono gold" style="margin-top:6px"></div></div>
      <div><h3>Quantiles</h3><table><thead><tr><th></th><th class="n">mean</th><th class="n">sd</th><th class="n">p10</th><th class="n">p25</th><th class="n">p50</th><th class="n">p75</th><th class="n">p90</th></tr></thead><tbody>
        ${["pts", "reb", "ast", "fg3m", "pra", "pr", "pa", "ra", "stocks", "minutes"].map((k) => { const d = p.stats[k]; return `<tr><td>${k}</td><td class="n">${fmt(d.mean)}</td><td class="n">${fmt(d.sd)}</td><td class="n">${fmt(d.p10, 0)}</td><td class="n">${fmt(d.p25, 0)}</td><td class="n">${fmt(d.median, 0)}</td><td class="n">${fmt(d.p75, 0)}</td><td class="n">${fmt(d.p90, 0)}</td></tr>`; }).join("")}</tbody></table></div>
      <div><h3>Joint correlations</h3><div class="kv mono">${Object.entries(p.correlations).map(([k, v]) => `<span>${esc(k)}</span><span>${fmt(v, 3)}</span>`).join("")}</div>
        <h3 style="margin-top:10px">Drivers</h3><div class="kv mono">${p.drivers.map((d) => `<span>${esc(d.driver)}</span><span>${typeof d.value === "number" ? fmt(d.value, 3) : esc(d.value)} ${esc(d.direction || "")}</span>`).join("")}</div>
        <h3 style="margin-top:10px">Inputs</h3><div class="kv mono"><span>Usage share</span><span>${fmt(p.inputs.usage, 3)}</span><span>TOV / FT trip rate</span><span>${fmt(p.inputs.tov, 3)} / ${fmt(p.inputs.foul_draw, 3)}</span><span>Zone mix rim·sm·lm·c3·ab3</span><span>${p.inputs.zone_mix.map((x) => fmt(100 * x, 0)).join("·")}</span><span>Zone FG%</span><span>${p.inputs.zone_fg.map((x) => fmt(100 * x, 0)).join("·")}</span><span>Role / missed streak</span><span>${esc(p.inputs.role)} / ${p.inputs.streak_missed}</span></div></div></div>
    <p class="note">Combined stats (PRA etc.) are summed within each simulated game, so their spread reflects the simulated joint dependence rather than independent marginals.</p>`;
  const upd = () => { const k = $("#d-stat").value; $("#d-thr").innerHTML = thr(k); };
  $("#d-stat").addEventListener("change", upd);
  $("#d-line").addEventListener("input", () => {
    const k = $("#d-stat").value, line = Number($("#d-line").value), d = p.stats[k];
    if (!$("#d-line").value) { $("#d-custom").textContent = ""; return; }
    // Interpolate between simulated quantiles for an arbitrary line.
    const qs = [[0.05, d.p05], [0.10, d.p10], [0.25, d.p25], [0.5, d.median], [0.75, d.p75], [0.9, d.p90], [0.95, d.p95]];
    let below = line <= qs[0][1] ? 0.05 : line >= qs[6][1] ? 0.95 : null;
    for (let i = 0; below === null && i < qs.length - 1; i++) if (line >= qs[i][1] && line <= qs[i + 1][1]) below = qs[i][0] + (qs[i + 1][0] - qs[i][0]) * ((line - qs[i][1]) / Math.max(1e-9, qs[i + 1][1] - qs[i][1]));
    $("#d-custom").textContent = `≈ ${pct(1 - below)} over ${line} (interpolated from simulated quantiles; exact ladder values listed above)`;
  });
}

/* ---------------------------------------------------------------- F: audit */
async function loadAudit() {
  const el = $("#f-body"); loading(el, "Loading model registry and walk-forward evidence…");
  try { renderAudit(await api("/api/intel/audit")); } catch (e) { fail(el, e); }
}
function renderAudit(a) {
  const el = $("#f-body");
  const reg = a.models.map((m) => `<tr><td class="gold">${esc(m.family)}</td><td class="mono">${esc(m.trained_through || m.version || "—")}</td><td class="n">${fmt(m.n ?? m.games, 0)}</td><td class="muted" style="white-space:normal">${esc(m.note || (m.lambda ? `lambda ${m.lambda}` : "") + (m.margin_sigma ? ` · margin sigma ${m.margin_sigma} · HCA ${m.home_court_per100}/100` : ""))}</td></tr>`).join("");
  const shot = a.models[0];
  const imp = (shot.feature_importance || []).map((f) => `<tr><td>${esc(f.feature)}</td><td class="n ${f.coefficient >= 0 ? "pos" : "neg"}">${signed(f.coefficient, 3)}</td></tr>`).join("");
  let bt = `<div class="empty">No walk-forward backtest stored yet. Run <span class="mono">python -m nba_quant.intel backtest 2025-11-17T12:00Z 2026-06-25T00:00Z</span>.</div>`;
  if (a.backtests.length) {
    const tabs = a.backtests.map((b, i) => `<button data-i="${i}" class="${i === (S.btI || 0) ? "on" : ""}">${esc(b.payload.start.slice(0, 10))} → ${esc(b.payload.end.slice(0, 10))} · ${b.payload.report.game?.games ?? 0} games</button>`).join("");
    const b = a.backtests[S.btI || 0].payload, R = b.report, G = R.game, P = R.players, W = G.win_prob;
    const wp = Object.entries(W).map(([k, v]) => `<tr><td>${esc(k.replaceAll("_", " "))}</td><td class="n">${fmt(v.log_loss, 4)}</td><td class="n">${fmt(v.brier, 4)}</td><td class="n">${v.accuracy !== undefined ? pct(v.accuracy) : "—"}</td><td class="n muted">${v.games ?? G.games}</td></tr>`).join("");
    const mk = G.market_benchmark;
    const stats = Object.entries(P.stats).map(([k, v]) => `<tr><td>${k}</td><td class="n">${v.n}</td><td class="n gold">${fmt(v.model_mae_conditional, 3)}</td><td class="n">${fmt(v.baseline_last10_mae, 3)}</td><td class="n ${v.model_mae_same_rows_as_baseline < v.baseline_last10_mae ? "pos" : "neg"}">${signed(v.model_mae_same_rows_as_baseline - v.baseline_last10_mae, 3)}</td><td class="n">${fmt(v.unconditional_mae_all_rostered, 3)}</td><td class="n">${pct(v.interval_80_coverage_all_rostered)}</td></tr>`).join("");
    const segs = Object.entries(P.points_mae_segments).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="n">${v.n}</td><td class="n">${fmt(v.mae, 3)}</td></tr>`).join("");
    const gsegs = Object.entries(G.segments).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="n">${v.games}</td><td class="n">${fmt(v.log_loss, 4)}</td><td class="n">${fmt(v.brier, 4)}</td><td class="n">${fmt(v.margin_mae, 2)}</td></tr>`).join("");
    bt = `<div class="tabs" id="f-tabs">${tabs}</div>
      <div class="grid g6" style="margin-bottom:12px">
        <div class="card"><div class="l">Games scored</div><div class="v">${G.games}</div><div class="s">${b.sims_per_game} sims/game</div></div>
        <div class="card"><div class="l">Win log loss</div><div class="v gold">${fmt(W.simulation.log_loss, 4)}</div><div class="s">coin flip ${fmt(W.coin_flip.log_loss, 4)}</div></div>
        <div class="card"><div class="l">Brier</div><div class="v">${fmt(W.simulation.brier, 4)}</div><div class="s">coin flip 0.2500</div></div>
        <div class="card"><div class="l">Margin MAE</div><div class="v">${fmt(G.score.margin_mae, 2)}</div><div class="s">market ${fmt(mk?.market_margin_mae, 2)}</div></div>
        <div class="card"><div class="l">Total MAE</div><div class="v">${fmt(G.score.total_mae, 2)}</div><div class="s">market ${fmt(mk?.market_total_mae, 2)}</div></div>
        <div class="card"><div class="l">Player PTS MAE</div><div class="v">${fmt(P.stats.pts.model_mae_conditional, 2)}</div><div class="s">last-10 avg ${fmt(P.stats.pts.baseline_last10_mae, 2)}</div></div></div>
      <div class="grid g3" style="margin-bottom:12px">
        <div class="panel"><h2>Win probability calibration</h2>${calibrationChart(G.calibration_bins, "raw sim")}${G.calibration_bins_platt ? calibrationChart(G.calibration_bins_platt, "walk-fwd Platt") : ""}</div>
        <div class="panel"><h2>Win probability scoring</h2><table><thead><tr><th>Model</th><th class="n">Log loss</th><th class="n">Brier</th><th class="n">Acc</th><th class="n">n</th></tr></thead><tbody>${wp}</tbody></table>
          ${mk ? `<h2 style="margin-top:12px">Market benchmark (${mk.games} games)</h2><div class="kv mono"><span>Market margin MAE</span><span>${fmt(mk.market_margin_mae, 3)}</span><span>Model margin MAE</span><span>${fmt(mk.model_margin_mae_same_games, 3)}</span><span>Market total MAE</span><span>${fmt(mk.market_total_mae, 3)}</span><span>Model total MAE</span><span>${fmt(mk.model_total_mae_same_games, 3)}</span><span>Market ML log loss</span><span>${fmt(mk.market_moneyline_log_loss, 4)}</span><span>Model log loss</span><span>${fmt(mk.model_log_loss_same_games, 4)}</span><span>Model vs spread corr</span><span>${fmt(mk.model_vs_spread_corr, 3)}</span></div><p class="note">${esc(mk.note)}</p>` : ""}
          <h2 style="margin-top:12px">Score accuracy</h2><div class="kv mono">${Object.entries(G.score).map(([k, v]) => `<span>${esc(k.replaceAll("_", " "))}</span><span>${k.includes("coverage") ? pct(v) : fmt(v, 3)}</span>`).join("")}</div></div>
        <div class="panel"><h2>Segments</h2><table><thead><tr><th>Game segment</th><th class="n">n</th><th class="n">LL</th><th class="n">Brier</th><th class="n">Margin MAE</th></tr></thead><tbody>${gsegs}</tbody></table>
          <h2 style="margin-top:12px">Player points MAE by segment</h2><table><thead><tr><th>Segment</th><th class="n">n</th><th class="n">MAE</th></tr></thead><tbody>${segs}</tbody></table></div></div>
      <div class="panel" style="margin-bottom:12px"><h2>Player projection accuracy (conditional on playing)</h2><div class="tbl-wrap"><table><thead><tr><th>Stat</th><th class="n">n</th><th class="n">Model MAE</th><th class="n">Last-10 avg MAE</th><th class="n">Model − baseline</th><th class="n">Uncond. MAE (DNP=0)</th><th class="n">80% interval cover</th></tr></thead><tbody>${stats}</tbody></table></div>
        <div class="kv mono" style="margin-top:8px"><span>Availability Brier / log loss</span><span>${fmt(P.availability.brier, 4)} / ${fmt(P.availability.log_loss, 4)} (base rate ${pct(P.availability.base_rate)})</span><span>Rostered forecasts / played</span><span>${P.rostered_player_forecasts} / ${P.played}</span></div>
        <ul class="note">${R.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div>`;
  }
  el.innerHTML = `${bt}<div class="grid g2"><div class="panel"><h2>Model registry</h2><table><thead><tr><th>Family</th><th>Trained through</th><th class="n">n</th><th>Notes</th></tr></thead><tbody>${reg}</tbody></table><p class="note">${esc(a.promotion_policy)}</p></div>
    <div class="panel"><h2>Shot-quality model</h2><div class="grid g2"><div>${shot.evaluation ? calibrationChart(shot.evaluation.reliability, "xFG holdout") : ""}<div class="kv mono"><span>Holdout shots</span><span>${shot.evaluation?.holdout_shots ?? "—"}</span><span>Log loss / base rate</span><span>${fmt(shot.evaluation?.log_loss, 4)} / ${fmt(shot.evaluation?.baseline_log_loss, 4)}</span><span>Brier</span><span>${fmt(shot.evaluation?.brier, 4)}</span></div></div>
      <div><h3>Largest coefficients (logit)</h3><table><tbody>${imp}</tbody></table></div></div></div></div>`;
  $("#f-tabs")?.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { S.btI = Number(b.dataset.i); renderAudit(a); } });
}

boot();
