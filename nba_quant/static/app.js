"use strict";

const $ = (id) => document.getElementById(id);
const state = { status: null, dataset: null, forecast: null, tab: "tonight", scenarios: [], metrics: [], profiles: null, metricRequest: 0, profileRequest: 0, sources: null, sgpForecast: null, sgpLegs: [], sgpRequest: 0, tonight: null, tonightRequest: 0, selectedDraft: null, draftBuildBusy: false, marketLegs: [], marketGameId: null, polymarket: null, polymarketRequest: 0, mlStatus: null, mlModels: null, mlRequest: 0 };
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (value, digits = 1) => typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
const pct = (value, digits = 1) => typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(digits)}%` : "—";
const bounded = (value, min, max) => Math.min(max, Math.max(min, Number(value) || 0));
const shortDate = (value) => value ? new Date(value).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC" }) + " UTC" : "Unavailable";
const interval = (stat) => stat ? `${num(stat.p10, 0)}–${num(stat.p90, 0)}` : "—";
const abbr = (name) => String(name || "?").split(/\s+/).filter(Boolean).map((s) => s[0]).join("").slice(0, 3).toUpperCase();
const option = (value, label) => `<option value="${escapeHTML(value)}">${escapeHTML(label)}</option>`;
const americanOdds = (value) => { const n = typeof value === "number" ? value : typeof value === "string" && value.trim() ? Number(value) : NaN; return Number.isFinite(n) && Math.abs(n) >= 100 ? `${n > 0 ? "+" : ""}${Math.round(n)}` : "Unavailable"; };
const selectionMode = () => $("builder-selection-mode").value === "standard" ? "standard" : "aggressive";
const metricArray = (metrics) => Array.isArray(metrics) ? metrics : Object.entries(metrics || {}).map(([label, value]) => ({ label, ...(typeof value === "object" && value !== null ? value : { value }) }));

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  let payload;
  try { payload = await response.json(); } catch { throw new Error(`Server returned ${response.status}. Check that the local NBA Quant API is running.`); }
  if (!response.ok) {
    const detail = payload.detail || payload.error || `Request failed (${response.status})`;
    throw new Error(Array.isArray(detail) ? detail.map((x) => `${(x.loc || []).join(".")}: ${x.msg || "Invalid value"}`).join("; ") : typeof detail === "object" ? JSON.stringify(detail) : detail);
  }
  return payload;
}

function message(text, error = false) {
  $("message").textContent = text;
  $("message").classList.toggle("error", error);
  $("message").hidden = !text;
}

function setBusy(busy, text = "Simulating…") {
  $("run-button").disabled = busy || Boolean(state.dataset && state.dataset.rotation_count === 0);
  $("demo-button").disabled = busy;
  $("dataset").disabled = busy;
  $("run-button").innerHTML = busy ? `<span class="loading">${escapeHTML(text)}</span>` : 'Run simulation <span>→</span>';
}

const sections = {
  tonight: ["Tonight", "The matchups, current team context, and source-backed SGP drafts."],
  overview: ["Game intelligence", "One coherent simulation. Players, possessions, and the final score."],
  players: ["Players & rotations", "Playing time, opportunities, and box scores from one shared game history."],
  sgp: ["Builder", "Research real player legs, then evaluate a matching frozen forecast when available."],
  polymarket: ["Polymarket", "Actual public exchange quotes, player-prop coverage, and source timestamps."],
  models: ["Predictions & models", "NBA training versions, target coverage, and inspectable evaluation evidence."],
  teams: ["Team intelligence", "Source-backed profiles, lineup context, and the limits of the available evidence."],
  metrics: ["Metric library", "440 blueprint definitions. Coverage and provenance at the point of use."],
  audit: ["Data & audit", "Source coverage, saved forecasts, and independent outcome evaluation."],
};

function tab(name, load = true) {
  if (!sections[name]) return;
  state.tab = name;
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === name);
    button.setAttribute("aria-current", button.dataset.tab === name ? "page" : "false");
  });
  Object.keys(sections).forEach((key) => { $(`tab-${key}`).hidden = key !== name; });
  $("page-title").innerHTML = `${escapeHTML(sections[name][0])}<span>.</span>`;
  $("section-name").textContent = sections[name][0];
  $("page-subtitle").textContent = sections[name][1];
  history.replaceState(null, "", `#${name}`);
  if (name === "audit") loadAudit();
  if (name === "metrics") loadMetrics();
  if (name === "teams") loadProfiles();
  if (name === "players" && !state.forecast) loadProfiles();
  if (name === "sgp" && load) loadSGPForecasts();
  if (name === "tonight") loadTonight();
  if (name === "polymarket") loadPolymarket();
  if (name === "models") loadModels();
}

async function loadStatus(selectedId) {
  state.status = await api("/api/status");
  $("model-version").textContent = state.status.model_version || "Event baseline";
  const datasets = [...(state.status.datasets || [])].sort((a, b) => Number(a.synthetic) - Number(b.synthetic));
  const previous = selectedId || $("dataset").value;
  $("dataset").innerHTML = option("", "Choose a data source") + datasets.map((d) => option(d.id, `${d.synthetic ? "DEMO · " : ""}${d.source}`)).join("");
  if (datasets.some((d) => d.id === previous)) $("dataset").value = previous;
  else if (datasets.some((d) => !d.synthetic)) $("dataset").value = datasets.filter((d) => !d.synthetic).at(-1).id;
  else if (datasets.length === 1) $("dataset").value = datasets[0].id;
  chooseDataset();
  renderReadiness();
}

function chooseDataset() {
  state.dataset = (state.status?.datasets || []).find((d) => d.id === $("dataset").value) || null;
  state.scenarios = [];
  state.profiles = null;
  renderScenarios();
  const teams = state.dataset?.teams || [];
  $("home-team").innerHTML = teams.map((t) => option(t.id, t.name)).join("");
  $("away-team").innerHTML = teams.map((t) => option(t.id, t.name)).join("");
  if (teams.length > 1) $("away-team").selectedIndex = 1;
  $("scenario-player").innerHTML = option("", "Choose a player") + (state.dataset?.players || []).map((p) => option(p.id, `${p.name} · ${p.position}`)).join("");
  if (state.forecast && state.forecast.request.dataset_id !== state.dataset?.id) {
    state.forecast = null;
    $("forecast-empty").hidden = false;
    $("forecast-content").hidden = true;
    $("players-content").className = "empty-panel";
    $("players-content").textContent = "Run a forecast to inspect player distributions and rotations.";
  }
  renderSource();
  $("run-button").disabled = Boolean(state.dataset && state.dataset.rotation_count === 0);
  updateMetricEntities();
  if (state.tab === "metrics") loadMetrics();
  if (state.tab === "teams") loadProfiles();
  if (state.tab === "players" && !state.forecast) loadProfiles();
}

function renderSource() {
  const d = state.dataset;
  const banner = $("source-banner");
  banner.classList.toggle("synthetic", Boolean(d?.synthetic));
  if (!d) {
    banner.innerHTML = '<span class="status-mark"></span><div><strong>No source selected</strong><span>Import a dataset with the CLI, or load the explicitly fictional demo.</span></div><span class="badge muted">NO FORECAST</span>';
    return;
  }
  banner.innerHTML = `<span class="status-mark"></span><div><strong>${d.synthetic ? "SYNTHETIC DEMO" : "IMPORTED SOURCE"}</strong><span>${escapeHTML(d.source)} · ${escapeHTML(d.games_count ?? 0)} historical games · Retrieved ${escapeHTML(shortDate(d.retrieved_at))}${d.rotation_count === 0 ? " · History only: current rotation snapshots required to forecast" : ""}</span></div><span class="badge ${d.synthetic ? "warning" : "muted"}">${d.synthetic ? "FICTIONAL DATA" : d.rotation_count === 0 ? "HISTORY ONLY" : "PROVIDER DECLARED"}</span>`;
}

async function loadSources() {
  try {
    state.sources = await api("/api/sources");
    renderReadiness();
  } catch (error) {
    state.sources = { readiness: { actionable: false, reasons: [`Source readiness unavailable: ${error.message}`] } };
    renderReadiness();
  }
}

function easternTime(value) {
  if (!value || !Number.isFinite(Date.parse(value))) return "Time unavailable";
  return new Date(value).toLocaleString("en-US", { timeZone: "America/New_York", weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" });
}

async function loadModels() {
  const requestId = ++state.mlRequest;
  $("ml-refresh").disabled = true;
  try {
    const results = await Promise.allSettled([api("/api/ml/v1/status"), api("/api/ml/v1/models"), api("/api/ml/v1/backtests")]);
    if (requestId !== state.mlRequest) return;
    state.mlStatus = results[0].status === "fulfilled" ? results[0].value : null;
    state.mlModels = results[1].status === "fulfilled" ? results[1].value : null;
    renderModels();
    renderMLBacktests(results[2].status === "fulfilled" ? results[2].value : null, results[2].status === "rejected" ? results[2].reason?.message : null);
    const failures = results.slice(0, 2).filter((result) => result.status === "rejected");
    if (failures.length) $("ml-summary").insertAdjacentHTML("beforeend", `<p class="ml-source-error">${failures.map((result) => escapeHTML(result.reason?.message || "Model endpoint unavailable")).join("<br>")}</p>`);
  } finally { if (requestId === state.mlRequest) $("ml-refresh").disabled = false; }
}

function mlTargets(status) {
  if (Array.isArray(status?.targets)) return status.targets;
  if (status?.targets && typeof status.targets === "object") return Object.entries(status.targets).map(([target, row]) => ({ target, ...(row && typeof row === "object" ? row : { status: row }) }));
  return [];
}

function mlRuns(models, status) {
  if (Array.isArray(models)) return models;
  for (const rows of [models?.runs, models?.models, status?.runs]) if (Array.isArray(rows)) return rows;
  return [];
}

function mlCohort(record) {
  if (record?.synthetic === true) return "SYNTHETIC";
  const declared = record?.cohort || record?.evaluation_cohort || record?.validation?.cohort || record?.data_cohort;
  if (typeof declared !== "string" || !declared.trim()) return "UNREPORTED";
  if (declared.toLowerCase().startsWith("synthetic")) return "SYNTHETIC";
  if (declared.toLowerCase().startsWith("retrospective")) return "RETROSPECTIVE";
  if (declared.toLowerCase() === "strict_point_in_time") return "STRICT POINT-IN-TIME RESEARCH";
  return declared.toUpperCase();
}

function mlEvidenceLabel(value) {
  if (value === null || value === undefined || value === "") return "UNAVAILABLE";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (typeof value === "boolean") return value ? "YES (recorded)" : "NO (recorded)";
  return JSON.stringify(value);
}

function mlMetricRows(value, path = [], depth = 0) {
  if (depth > 7 || value === null || value === undefined) return [];
  if (typeof value === "number") return Number.isFinite(value) ? [{ metric: path.join(" / "), value }] : [];
  if (typeof value !== "object") return [];
  return Object.entries(value).flatMap(([key, child]) => mlMetricRows(child, [...path, key], depth + 1));
}

function renderMLMetrics(value, caption) {
  const rows = mlMetricRows(value);
  if (!rows.length) return `<div class="ml-metric-empty"><strong>${escapeHTML(caption)}: UNAVAILABLE</strong><span>No measured values are present in this saved record.</span></div>`;
  return `<div class="ml-evidence-table"><h3>${escapeHTML(caption)}</h3><div class="data-table-wrap"><table class="data-table"><thead><tr><th>RECORDED METRIC / SPLIT</th><th>VALUE</th></tr></thead><tbody>${rows.map((row) => `<tr><td>${escapeHTML(row.metric)}</td><td>${num(row.value, 5)}</td></tr>`).join("")}</tbody></table></div></div>`;
}

function renderModels() {
  const status = state.mlStatus;
  const models = state.mlModels;
  const targets = mlTargets(status);
  const runs = mlRuns(models, status);
  const eligible = targets.filter((target) => target.production_eligible === true).length;
  $("ml-summary").innerHTML = `<strong>${targets.length}</strong> targets listed <span>·</span> <strong>${runs.length}</strong> saved runs <span>·</span> <strong>${eligible}</strong> reported production-eligible <small class="slate-price-note">${escapeHTML(status?.status || (status ? "REGISTRY RESPONSE" : "STATUS UNAVAILABLE"))}</small>`;
  $("ml-targets").innerHTML = `<section class="panel"><div class="panel-heading"><div><span class="eyebrow">TARGET READINESS</span><h2>Coverage before predictions</h2></div><span class="badge muted">READ-ONLY MODEL EVIDENCE</span></div>${targets.length ? `<div class="data-table-wrap"><table class="data-table ml-target-table"><thead><tr><th>TARGET</th><th>STATUS</th><th>DATA ROWS</th><th>PRODUCTION ELIGIBILITY</th><th>SOURCE STATUS</th><th>UNCERTAINTY</th></tr></thead><tbody>${targets.map((target) => `<tr><td>${escapeHTML(target.target || target.name || "Target unavailable")}</td><td>${escapeHTML(target.status || "UNAVAILABLE")}</td><td>${typeof target.data_rows === "number" ? num(target.data_rows, 0) : "UNAVAILABLE"}</td><td>${target.production_eligible === true ? "REPORTED ELIGIBLE" : target.production_eligible === false ? "NOT ELIGIBLE" : "UNREPORTED"}</td><td>${escapeHTML(mlEvidenceLabel(target.source_status || target.source))}</td><td>${escapeHTML(mlEvidenceLabel(target.uncertainty_status || target.uncertainty))}</td></tr>`).join("")}</tbody></table></div>` : '<div class="empty-panel">Target readiness is UNAVAILABLE. No data rows or model status are inferred.</div>'}<p class="help">Eligibility is displayed as recorded by the backend. Predictive accuracy, current source validity, and executable market coverage are separate checks. No training runs in the browser.</p></section>`;
  $("ml-runs").innerHTML = runs.length ? `<div class="section-intro"><div><span class="eyebrow">STORED TRAINING VERSIONS</span><p>Metrics are reproduced from manifests. Missing metrics stay unavailable; one cohort is not evidence for another.</p></div></div>${runs.map((run, index) => renderModelRun(run, index)).join("")}` : `<section class="panel"><div class="panel-heading"><div><span class="eyebrow">STORED MODELS</span><h2>No saved training evidence available</h2></div><span class="badge muted">UNAVAILABLE</span></div><p class="help">These registry endpoints have not supplied a trained model manifest or prediction distribution. Existing research drafts retain their own recorded method and are not relabeled as machine-learning forecasts.</p></section>`;
}

function renderModelRun(record, index) {
  const run = record.manifest && typeof record.manifest === "object" ? { ...record, ...record.manifest } : record;
  const cohort = mlCohort(run);
  const source = run.source_status || run.source || run.data_source || run.provenance?.source || run.feature_audit?.mode;
  const version = run.version || run.model_version || run.run_id || run.id;
  return `<section class="panel ml-model-run"><div class="panel-heading"><div><span class="eyebrow">TRAINING RUN ${index + 1} / ${escapeHTML(run.engine || run.engine_version || run.framework || run.model_type || "ENGINE UNREPORTED")}</span><h2>${escapeHTML(run.target || "Target unavailable")} <span class="ml-version">${escapeHTML(version || "VERSION UNAVAILABLE")}</span></h2></div><span class="badge ${cohort === "SYNTHETIC" ? "warning" : "muted"}">${escapeHTML(cohort)}</span></div><div class="ml-run-facts"><div><span>Saved status</span><strong>${escapeHTML(run.status || "UNAVAILABLE")}</strong></div><div><span>Training / creation time</span><strong>${escapeHTML(shortDate(run.trained_at || run.created_at))}</strong></div><div><span>Source status</span><strong>${escapeHTML(mlEvidenceLabel(source))}</strong></div><div><span>Uncertainty evidence</span><strong>${escapeHTML(mlEvidenceLabel(run.uncertainty_status || run.uncertainty || run.calibration))}</strong></div></div>${cohort === "SYNTHETIC" ? '<p class="ml-cohort-note">Synthetic results check software behavior; they do not establish predictive skill on NBA games.</p>' : cohort === "RETROSPECTIVE" ? '<p class="ml-cohort-note">Retrospective evaluation is historical replay. It is separate from forecasts frozen before future games.</p>' : cohort === "STRICT POINT-IN-TIME RESEARCH" ? '<p class="ml-cohort-note">Feature timing meets a strict historical cutoff policy. This alone does not establish prospective forecast validation.</p>' : cohort === "PROSPECTIVE" ? '<p class="ml-cohort-note">Prospective cohort is recorded in this manifest. Sample size, source evidence and calibration remain necessary to assess the result.</p>' : '<p class="ml-cohort-note">Evaluation cohort is unreported or custom-labeled. This view does not assume prospective validation.</p>'}${renderCalibrationEvidence(run)}${renderMLComparisonChart(run)}${renderMLReliability(run)}<div class="ml-comparison-grid">${renderMLMetrics(run.metrics || run.final_test_metrics, "Evaluation metrics")}${renderMLMetrics(run.comparison || run.comparison_metrics || run.baseline_comparison, "Baseline / candidate comparison")}</div><details class="draft-method"><summary>Inspect saved manifest</summary><pre class="ml-manifest">${escapeHTML(JSON.stringify(record, null, 2))}</pre></details></section>`;
}

function renderCalibrationEvidence(run) {
  const calibration = run.calibration;
  const audit = run.feature_audit;
  if (!calibration && !audit) return "";
  return `<div class="ml-calibration-evidence"><div><span class="eyebrow">UNCERTAINTY / CALIBRATION RECORD</span><strong>${escapeHTML(calibration?.status || "UNAVAILABLE")}</strong><span>Method: ${escapeHTML(mlEvidenceLabel(calibration?.method))}</span><span>Saved radius: ${typeof calibration?.radius === "number" ? num(calibration.radius, 5) : "UNAVAILABLE"}</span><small>A stored method or radius does not establish prospective coverage.</small></div><div><span class="eyebrow">SOURCE / FEATURE TIMING AUDIT</span><strong>${escapeHTML(audit?.mode || "UNAVAILABLE")}</strong>${audit ? `<details class="draft-method"><summary>Inspect recorded data audit</summary><pre class="ml-manifest">${escapeHTML(JSON.stringify(audit, null, 2))}</pre></details>` : '<span>No feature timing audit supplied.</span>'}</div></div>`;
}

function renderMLBacktests(payload, error = null) {
  const reports = Array.isArray(payload?.reports) ? payload.reports : Array.isArray(payload) ? payload : [];
  if (!reports.length) {
    $("ml-backtests").innerHTML = `<section class="panel"><div class="panel-heading"><div><span class="eyebrow">EXPANDING / ROLLING EVALUATION</span><h2>Saved walk-forward reports</h2></div><span class="badge muted">UNAVAILABLE</span></div><p class="help">${escapeHTML(error || "No saved rolling-evaluation report supplied by the backend.")} Model manifests and target readiness remain available independently.</p></section>`;
    return;
  }
  $("ml-backtests").innerHTML = `<div class="section-intro"><div><span class="eyebrow">REUSED HISTORICAL ARCHIVE / EXPLORATORY REPLAY</span><h2>Walk-forward evaluation</h2><p>Game counts apply within each report. Reports can reuse the same games, so their counts must not be added together.</p></div></div>${reports.map((report) => renderMLBacktestReport(report)).join("")}`;
}

function renderMLBacktestReport(report) {
  const folds = Array.isArray(report.folds) ? report.folds : [];
  const metric = report.kind === "binary" || folds.some((fold) => typeof fold.metrics?.log_loss === "number") ? "log_loss" : "mae";
  const count = report.independent_test_games;
  return `<section class="panel ml-backtest-report"><div class="panel-heading"><div><span class="eyebrow">${escapeHTML(report.mode || "MODE UNREPORTED")}</span><h2>${escapeHTML(report.target || "Target unavailable")} replay</h2></div><span class="badge warning">${escapeHTML(report.status || "RESEARCH_ONLY")}</span></div><div class="ml-backtest-facts"><span><strong>${typeof count === "number" ? num(count, 0) : "UNAVAILABLE"}</strong> unique test games in this report</span><span><strong>${folds.length}</strong> recorded folds</span><span>Overlapping holdouts <strong>${report.overlapping_holdouts === false ? "NO (recorded)" : report.overlapping_holdouts === true ? "YES (recorded)" : "UNREPORTED"}</strong></span></div><p class="ml-cohort-note">This reuses the historical archive for exploratory evaluation. It is not new untouched prospective evidence, and does not establish production eligibility or betting accuracy.</p>${renderMLFoldChart(folds, metric)}<div class="data-table-wrap"><table class="data-table ml-fold-table"><thead><tr><th>FOLD / TEST DAYS</th><th>MODEL / CHAMPION</th><th>COHORT</th><th>TEST GAMES</th><th>${metric === "mae" ? "MAE" : "LOG LOSS"}</th><th>HISTORICAL BASELINE</th></tr></thead><tbody>${folds.map((fold, index) => `<tr><td>Fold ${escapeHTML(fold.plan?.index ?? index + 1)}<div class="player-meta">${escapeHTML((fold.plan?.test_days || []).join(", ") || "Dates unavailable")}</div></td><td>${escapeHTML(fold.champion || "Champion unavailable")}<div class="player-meta">${escapeHTML(fold.model_id || "Model ID unavailable")}</div></td><td>${escapeHTML(mlCohort(fold))}</td><td>${typeof fold.metrics?.independent_games === "number" ? num(fold.metrics.independent_games, 0) : "UNAVAILABLE"}</td><td>${typeof fold.metrics?.[metric] === "number" ? num(fold.metrics[metric], 5) : "UNAVAILABLE"}</td><td>${typeof fold.comparison?.historical_baseline?.[metric] === "number" ? num(fold.comparison.historical_baseline[metric], 5) : "UNAVAILABLE"}</td></tr>`).join("")}</tbody></table></div><div class="hash">Input hash: ${escapeHTML(report.input_hash || "UNAVAILABLE")}</div><details class="draft-method"><summary>Inspect full report and backend comparisons</summary><pre class="ml-manifest">${escapeHTML(JSON.stringify(report, null, 2))}</pre></details></section>`;
}

function renderMLFoldChart(folds, metric) {
  const valid = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0;
  const values = folds.flatMap((fold) => [fold.metrics?.[metric], fold.comparison?.historical_baseline?.[metric]]).filter(valid);
  if (!values.length) return "";
  const w = 740, h = 250, left = 65, right = 25, top = 20, bottom = 47;
  const pw = w - left - right, ph = h - top - bottom, max = Math.max(...values, .00001) * 1.1;
  const point = (index, value) => [left + (folds.length === 1 ? .5 : index / (folds.length - 1)) * pw, top + (1 - value / max) * ph];
  let svg = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Recorded fold ${metric} for champion and historical baseline"><title>Per-fold recorded champion and historical baseline ${metric}; missing measurements are not filled.</title>`;
  for (const fraction of [0,.25,.5,.75,1]) {
    const y = top + (1 - fraction) * ph;
    svg += `<line x1="${left}" x2="${w - right}" y1="${y}" y2="${y}" stroke="#24303a"/><text x="${left - 10}" y="${y + 4}" text-anchor="end" fill="#81929f" font-size="10">${num(fraction * max, 3)}</text>`;
  }
  for (const series of [{ label: "Recorded champion", color: "#78e3cf", get: (fold) => fold.metrics?.[metric] }, { label: "Historical baseline", color: "#efb176", get: (fold) => fold.comparison?.historical_baseline?.[metric] }]) {
    let previous = null;
    folds.forEach((fold, index) => {
      const value = series.get(fold);
      if (!valid(value)) { previous = null; return; }
      const [x,y] = point(index, value);
      if (previous) svg += `<line x1="${previous[0]}" y1="${previous[1]}" x2="${x}" y2="${y}" stroke="${series.color}" stroke-width="2"/>`;
      svg += `<circle cx="${x}" cy="${y}" r="4" fill="${series.color}"><title>${series.label}; fold ${escapeHTML(fold.plan?.index ?? index + 1)}; ${metric} ${num(value, 5)}</title></circle>`;
      previous = [x,y];
    });
  }
  folds.forEach((fold,index) => { const [x] = point(index, 0); svg += `<text x="${x}" y="${h - 22}" text-anchor="middle" fill="#a0aeb9" font-size="10">Fold ${escapeHTML(fold.plan?.index ?? index + 1)}</text>`; });
  svg += "</svg>";
  return `<div class="ml-chart-panel"><div class="panel-heading"><h3>${metric === "mae" ? "MAE" : "Log loss"} across saved folds</h3><span>Lower is better</span></div><div class="ml-chart">${svg}</div><div class="fold-chart-legend"><span>Recorded champion</span><span>Historical baseline</span></div><p class="help">Only recorded per-fold measurements are plotted. Missing values break the line; no folds or baseline results are imputed.</p></div>`;
}

function mlComparisonValues(run) {
  const metric = run.kind === "binary" ? "log_loss" : "mae";
  const rows = new Map();
  const add = (name, metrics) => { const value = metrics?.[metric]; if (typeof value === "number" && Number.isFinite(value) && value >= 0) rows.set(name, { name, value }); };
  const comparison = run.comparison || run.comparison_metrics || run.baseline_comparison;
  if (comparison && !Array.isArray(comparison) && typeof comparison === "object") Object.entries(comparison).forEach(([name, metrics]) => add(name, metrics?.final_test_metrics || metrics));
  if (!rows.size && run.candidates && typeof run.candidates === "object") Object.entries(run.candidates).forEach(([name, candidate]) => add(candidate.backend || name, candidate.final_test_metrics));
  if (run.baseline_final_test_metrics && typeof run.baseline_final_test_metrics === "object") Object.entries(run.baseline_final_test_metrics).forEach(([name, metrics]) => { if (!rows.has(name)) add(name, metrics); });
  return { metric, rows: [...rows.values()] };
}

function renderMLComparisonChart(run) {
  const { metric, rows } = mlComparisonValues(run);
  if (!rows.length) return "";
  const width = 740, left = 170, right = 80, top = 22, stride = 32;
  const height = Math.max(130, rows.length * stride + 40);
  const max = Math.max(...rows.map((row) => row.value), 0.00001);
  const plotWidth = width - left - right;
  let chart = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Recorded final test ${metric} by model backend"><title>Recorded final-test ${metric}; lower is better. ${escapeHTML(rows.map((row) => `${row.name}: ${row.value}`).join("; "))}</title><line x1="${left}" x2="${left}" y1="12" y2="${height - 20}" stroke="#354653"/>`;
  rows.forEach((row, index) => {
    const y = top + index * stride;
    const label = row.name.length > 23 ? row.name.slice(0, 20) + "..." : row.name;
    chart += `<text x="${left - 12}" y="${y + 13}" text-anchor="end" fill="#a0aeb9" font-size="11" font-family="Segoe UI,Arial,sans-serif">${escapeHTML(label)}</text><rect x="${left}" y="${y}" width="${row.value / max * plotWidth}" height="18" rx="3" fill="${row.name === run.champion ? "#78e3cf" : "#3c6f78"}"><title>${escapeHTML(row.name)}: ${num(row.value, 5)}</title></rect><text x="${left + row.value / max * plotWidth + 9}" y="${y + 13}" fill="#d7e4e9" font-size="11" font-family="Consolas,monospace">${num(row.value, 5)}</text>`;
  });
  chart += "</svg>";
  return `<section class="ml-chart-panel"><div class="panel-heading"><div><span class="eyebrow">SOURCE: SAVED FINAL-TEST METRICS</span><h3>${metric === "log_loss" ? "Log loss" : "Mean absolute error"} by backend</h3></div><span>Lower is better</span></div><div class="ml-chart">${chart}</div><p class="help">Bars reproduce recorded backend final-test metrics. Champion selection used tuning data; this chart does not select or promote a model. Calibration can make the champion's separate final metrics differ from raw challenger scores.</p></section>`;
}

function renderMLReliability(run) {
  if (!Array.isArray(run.reliability_bins) || !run.reliability_bins.length) return "";
  const bins = run.reliability_bins.map((bin) => ({ predicted: bin.predicted ?? bin.mean_predicted_probability, observed: bin.observed ?? bin.observed_frequency, count: bin.count ?? bin.n })).filter((bin) => typeof bin.predicted === "number" && Number.isFinite(bin.predicted) && bin.predicted >= 0 && bin.predicted <= 1 && typeof bin.observed === "number" && Number.isFinite(bin.observed) && bin.observed >= 0 && bin.observed <= 1);
  if (!bins.length) return "";
  const w = 500, h = 270, left = 54, top = 16, pw = 410, ph = 214;
  let chart = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Recorded model reliability bins"><title>Reliability from saved bins; predicted probability against observed outcome frequency.</title>`;
  for (const q of [0,.25,.5,.75,1]) {
    const x = left + q * pw, y = top + (1 - q) * ph;
    chart += `<line x1="${x}" x2="${x}" y1="${top}" y2="${top + ph}" stroke="#24303a"/><line x1="${left}" x2="${left + pw}" y1="${y}" y2="${y}" stroke="#24303a"/><text x="${x}" y="${top + ph + 20}" text-anchor="middle" fill="#81929f" font-size="10">${Math.round(q * 100)}%</text><text x="${left - 10}" y="${y + 4}" text-anchor="end" fill="#81929f" font-size="10">${Math.round(q * 100)}%</text>`;
  }
  chart += `<line x1="${left}" y1="${top + ph}" x2="${left + pw}" y2="${top}" stroke="#9a7960" stroke-dasharray="4 4"/>`;
  bins.forEach((bin) => { chart += `<circle cx="${left + bin.predicted * pw}" cy="${top + (1 - bin.observed) * ph}" r="5" fill="#78e3cf"><title>Predicted ${pct(bin.predicted)}, observed ${pct(bin.observed)}${typeof bin.count === "number" ? `; n=${bin.count}` : ""}</title></circle>`; });
  chart += `<text x="${left + pw / 2}" y="${h - 5}" text-anchor="middle" fill="#a0aeb9" font-size="10">Predicted probability (x); observed frequency (y)</text></svg>`;
  return `<section class="ml-chart-panel ml-reliability"><div class="panel-heading"><div><span class="eyebrow">SOURCE: SAVED RELIABILITY BINS</span><h3>Recorded reliability</h3></div><span>${bins.length} valid bins</span></div><div class="ml-chart">${chart}</div><p class="help">Only recorded bins are plotted. Dashed diagonal is perfect agreement, not a claim that this model is calibrated.</p></section>`;
}

async function loadPolymarket(refresh = false) {
  const requestId = ++state.polymarketRequest;
  $("polymarket-refresh").disabled = true;
  $("polymarket-refresh").textContent = refresh ? "Fetching public markets…" : "Loading quotes…";
  try {
    if (refresh) await api("/api/polymarket/refresh", { method: "POST", body: "{}" });
    const report = await api("/api/polymarket");
    if (requestId !== state.polymarketRequest) return;
    state.polymarket = report;
    renderPolymarket();
  } catch (error) {
    if (requestId !== state.polymarketRequest) return;
    state.polymarket = null;
    $("polymarket-summary").textContent = "Polymarket coverage unavailable";
    $("polymarket-markets").innerHTML = `<div class="empty-panel"><h3>Public quotes unavailable</h3><p>${escapeHTML(error.message)}</p><p class="help">No FanDuel prices or synthetic lines are substituted.</p></div>`;
  } finally {
    if (requestId === state.polymarketRequest) { $("polymarket-refresh").disabled = false; $("polymarket-refresh").textContent = "Refresh Polymarket ↻"; }
  }
}

function polymarketType(market) {
  const type = String(market.market_type || "").toLowerCase();
  if (market.player_name || type.includes("player") || type.includes("prop")) return "player";
  if (type.includes("total")) return "total";
  if (type.includes("spread") || type.includes("handicap")) return "spread";
  if (type.includes("winner") || type.includes("moneyline")) return "winner";
  return "other";
}

function renderPolymarket() {
  const report = state.polymarket;
  if (!report) return;
  const markets = Array.isArray(report.markets) ? report.markets : [];
  const props = markets.filter((market) => polymarketType(market) === "player");
  const quoted = markets.filter((market) => typeof market.best_ask === "number" && market.best_ask > 0 && market.best_ask < 1);
  $("polymarket-summary").innerHTML = `<strong>${markets.length}</strong> market outcomes <span>·</span> <strong>${props.length}</strong> player-prop outcomes <span>·</span> <strong>${quoted.length}</strong> quoted asks <small class="slate-price-note">${escapeHTML(report.status || "UNVERIFIED")} · Retrieved ${escapeHTML(shortDate(report.retrieved_at))}</small>`;
  const filter = $("polymarket-type").value || "all";
  const search = $("polymarket-search").value.trim().toLowerCase();
  const selected = markets.filter((market) => (filter === "all" || polymarketType(market) === filter) && `${market.event_title || market.event_name || ""} ${market.question || ""} ${market.player_name || ""} ${market.outcome || ""}`.toLowerCase().includes(search));
  const errors = report.errors || [];
  const orderBookUnavailable = markets.length > 0 && quoted.length === 0;
  const diagnostics = errors.length ? `<details class="polymarket-diagnostics"><summary>Source coverage notes (${errors.length})</summary><ul class="warning-list">${errors.map((error) => `<li>${escapeHTML(typeof error === "string" ? error : error.reason || error.error || JSON.stringify(error))}</li>`).join("")}</ul></details>` : "";
  if (!markets.length) {
    $("polymarket-markets").innerHTML = `<div class="empty-panel"><h3>No NBA market lines returned</h3><p>${escapeHTML(report.reason || report.note || "The current public report contains no market outcomes.")}</p><p class="help">Coverage is limited to what Polymarket actually lists and exposes. No full player-prop catalog or same-game parlay price is assumed.</p></div>${diagnostics}`;
    return;
  }
  if (!selected.length) { $("polymarket-markets").innerHTML = `<div class="empty-panel">No actual markets match these filters.</div>${diagnostics}`; return; }
  const groups = new Map();
  selected.forEach((market) => { const key = market.event_id || market.event_title || market.event_name || "Other NBA markets"; if (!groups.has(key)) groups.set(key, []); groups.get(key).push(market); });
  $("polymarket-markets").innerHTML = (orderBookUnavailable ? '<div class="polymarket-orderbook-note"><strong>Executable order-book quotes unavailable</strong><span>Listed markets and indicative metadata prices remain visible. Best asks, executable size and combined SGP pricing are not inferred.</span></div>' : "") + [...groups].map(([, rows]) => `<section class="panel polymarket-event"><div class="panel-heading"><div><span class="eyebrow">POLYMARKET / EXCHANGE QUOTES</span><h2>${escapeHTML(rows[0].event_title || rows[0].event_name || "NBA event")}</h2></div><span class="badge muted">${rows.length} OUTCOMES</span></div><div class="data-table-wrap"><table class="data-table polymarket-table"><thead><tr><th>MARKET / SELECTION</th><th>AMERICAN ODDS</th><th>DECIMAL</th><th>BUY ASK / SHARE</th><th>SPREAD</th><th>ASK DEPTH</th></tr></thead><tbody>${rows.map(renderPolymarketRow).join("")}</tbody></table></div><details class="draft-method"><summary>Quote timestamps & source receipts</summary>${rows.map((market) => `<div class="game-source">${escapeHTML(market.question || market.player_name || market.id)} · ${escapeHTML(market.outcome)}<br>Order book observed ${escapeHTML(shortDate(market.observed_at))}<br>Metadata retrieved ${escapeHTML(shortDate(market.retrieved_at))}<br>${escapeHTML(market.source_url || "Source URL unavailable")}</div>`).join("")}</details></section>`).join("") + diagnostics;
}

function polymarketPrice(market, kind = "american") {
  const executable = kind === "american" ? market.american_odds : market.decimal_odds;
  const indicative = kind === "american" ? market.indicative_american_odds : market.indicative_decimal_odds;
  if (typeof executable === "number" && Number.isFinite(executable)) return kind === "american" ? americanOdds(executable) : num(executable, 3);
  if (typeof indicative === "number" && Number.isFinite(indicative)) return `${kind === "american" ? americanOdds(indicative) : num(indicative, 3)}<small class="indicative-price">INDICATIVE</small>`;
  return "Unavailable";
}

function polymarketQuoteStatus(market) {
  if (market.executable === true) return "Depth-backed public BUY ask · account trading not enabled";
  if (typeof market.best_ask === "number" && market.best_ask > 0 && market.best_ask < 1) return "Public BUY ask · requested size not established at this best price";
  if (market.indicative_price_basis === "INDICATIVE_MARKET_METADATA_NOT_EXECUTABLE" || ["INDICATIVE_MARKET_METADATA_NOT_EXECUTABLE", "QUOTE_ACCESS_UNAVAILABLE"].includes(market.quote_status)) return "Indicative metadata · order book unavailable";
  return `Quote status: ${market.quote_status || "UNVERIFIED"} · no trading enabled`;
}

function renderPolymarketRow(market) {
  const book = market.orderbook || {};
  const spread = market.spread ?? book.spread;
  const ask = market.best_ask;
  const levels = Array.isArray(book.ask_depth) ? book.ask_depth : [];
  const topShares = book.best_ask_shares ?? (typeof ask === "number" && levels.length ? levels.filter((level) => level.price === ask).reduce((sum, level) => sum + (Number(level.size) || 0), 0) : null);
  const totalShares = book.total_ask_shares ?? market.total_ask_shares;
  const depth = book.depth_quote;
  const hasActualPrice = typeof market.american_odds === "number" && Number.isFinite(market.american_odds);
  return `<tr><td><strong>${escapeHTML(market.player_name || market.question || "Unnamed market")}</strong><div class="player-meta">${escapeHTML(market.player_name ? `${SGP_STATS[market.stat] || market.stat || market.market_type || "Prop"} ${market.comparison || market.side || ""} ${typeof market.line === "number" ? market.line : ""}` : market.market_type || "Market type unavailable")} · ${escapeHTML(market.outcome || "Outcome unavailable")}</div><div class="polymarket-quote-status ${market.executable === true ? "depth-backed" : ""}">${escapeHTML(polymarketQuoteStatus(market))}</div></td><td class="stat-highlight">${polymarketPrice(market, "american")}${hasActualPrice ? '<small class="gross-price-label">GROSS · FEES EXCLUDED</small>' : ""}</td><td>${polymarketPrice(market, "decimal")}</td><td>${typeof ask === "number" ? `$${num(ask, 3)}` : "Unavailable"}<small class="depth-detail">$1 outcome share</small></td><td>${typeof spread === "number" ? `$${num(spread, 3)}` : "Unavailable"}</td><td>${typeof topShares === "number" ? `<strong>${num(topShares, 2)}</strong><small class="depth-detail">shares at best ask</small>` : "Unavailable"}${typeof totalShares === "number" ? `<small class="depth-detail">${num(totalShares, 2)} total ask shares</small>` : ""}${typeof book.requested_shares === "number" ? `<small class="depth-detail">Requested ${num(book.requested_shares, 2)} · min ${num(book.minimum_order_shares, 2)}</small>` : ""}${depth && typeof depth.gross_cost === "number" ? `<details class="depth-fill-details"><summary>Depth fill estimate</summary><span>${num(depth.shares, 2)} shares · gross cost $${num(depth.gross_cost, 3)} · VWAP $${num(depth.volume_weighted_price, 3)}. Fees, gas and subsequent slippage excluded.</span></details>` : ""}</td></tr>`;
}

async function loadTonight(refresh = false) {
  const requestId = ++state.tonightRequest;
  $("tonight-refresh").disabled = true;
  try {
    if (refresh) await api("/api/tonight/refresh", { method: "POST", body: "{}" });
    const tonight = await api("/api/tonight");
    if (requestId !== state.tonightRequest) return;
    state.tonight = tonight;
    renderTonight();
  } catch (error) {
    if (requestId !== state.tonightRequest) return;
    state.tonight = null;
    $("tonight-source").textContent = "Schedule unavailable. A provider failure does not mean there are no NBA games tonight.";
    $("tonight-games").innerHTML = `<div class="empty-panel"><h3>Tonight's schedule is unavailable</h3><p>${escapeHTML(error.message)}</p><p class="help">Refresh the schedule to retry. No fictional matchup or prior-date schedule is substituted.</p></div>`;
  } finally { if (requestId === state.tonightRequest) $("tonight-refresh").disabled = false; }
}

function renderTonight() {
  const tonight = state.tonight;
  if (!tonight) return;
  $("tonight-source").innerHTML = `${escapeHTML(tonight.date || "Date unavailable")} · Eastern time · ${escapeHTML(tonight.status)} · Retrieved ${escapeHTML(shortDate(tonight.retrieved_at))}<br>Schedule source: ${escapeHTML(tonight.source_url || "Unavailable")}`;
  if (tonight.status === "LAST_GOOD") {
    $("tonight-source").innerHTML += `<p class="tonight-stale"><strong>STALE · LAST VERIFIED SCHEDULE</strong> ${escapeHTML(tonight.reason || "Refresh failed; cached schedule shown.")} ${tonight.age_seconds !== undefined ? `Cache age ${escapeHTML(Math.round(tonight.age_seconds / 60))} minutes.` : "Check retrieval time before use."}</p>`;
  }
  if (tonight.status !== "AVAILABLE" && tonight.status !== "LAST_GOOD" && tonight.status !== "EMPTY_PROVIDER_SLATE") {
    $("tonight-games").innerHTML = `<div class="empty-panel"><h3>Schedule data unavailable</h3><p>${escapeHTML(tonight.reason || "The schedule provider did not return a verified response.")}</p><p class="help">This does not establish that no games are scheduled. Refresh to retry.</p></div>`;
    return;
  }
  renderNightlyRequirements(tonight.nightly_requirements);
  const games = tonight.games || [];
  const familyCount = games.reduce((sum, game) => sum + (game.sgp?.draft_families || []).filter((family) => (family.legs || []).length > 0).length, 0);
  const playerCount = games.reduce((sum, game) => sum + (game.context?.players || []).length, 0);
  const publishedCount = games.reduce((sum, game) => sum + (game.sgp?.published_props || []).length, 0);
  $("tonight-summary").innerHTML = `<strong>${games.length}</strong> games <span>·</span> <strong>${playerCount}</strong> current roster entries <span>·</span> <strong>${familyCount}</strong> constructed drafts${publishedCount ? `<span>·</span> <strong>${publishedCount}</strong> FanDuel published props <small class="slate-price-note">partial coverage · price timing unverified</small>` : ""}`;
  if (!games.length) {
    $("tonight-games").innerHTML = `<div class="empty-panel"><h3>No games in this provider response</h3><p>Provider response for ${escapeHTML(tonight.date)} in America/New_York contained no events. This alone does not establish that the NBA has no games tonight.</p></div>`;
    return;
  }
  $("tonight-games").innerHTML = games.map((game) => renderTonightGame(game, tonight)).join("");
}

function isPregame(game) {
  return !game.completed && (game.phase || game.status) === "pre" && Date.parse(game.tipoff) > Date.now();
}

function renderNightlyRequirements(requirements) {
  if (!requirements) {
    $("nightly-requirements").innerHTML = '<div class="nightly-coverage pending"><div><span class="eyebrow">NIGHTLY PRIORITY</span><h3>5- and 8-leg research families</h3><p>Refresh construction to report source-backed availability for the current slate.</p></div><span class="badge muted">COVERAGE NOT REPORTED</span></div>';
    return;
  }
  const counts = requirements.available_by_size || {};
  const missing = requirements.missing_sizes || [];
  $("nightly-requirements").innerHTML = `<div class="nightly-coverage ${requirements.status === "AVAILABLE" ? "available" : "pending"}"><div><span class="eyebrow">NIGHTLY 5 / 8 PRIORITY</span><h3>Family-specific construction</h3><p>Higher-line anchors are combined with support selections. Each size can choose different player thresholds.</p></div><div class="nightly-size-counts">${(requirements.required_sizes || [5,8]).map((size) => `<div><strong>${counts[String(size)] === undefined ? "—" : escapeHTML(counts[String(size)])}</strong><span>${escapeHTML(size)}-leg families</span></div>`).join("")}</div><span class="badge ${requirements.status === "AVAILABLE" ? "" : "warning"}">${escapeHTML(requirements.status || "UNVERIFIED")}</span><p class="nightly-coverage-note">${missing.length ? `Missing source-valid sizes: ${missing.map((size) => escapeHTML(size)).join(", ")}. ` : ""}Coverage is a research deliverable, not a win guarantee or a joint probability. Support legs may have nonpositive research edge estimates.</p></div>`;
}

function renderFamilyRole(role) {
  if (!role) return "";
  const normalized = String(role).toLowerCase();
  return `<small class="family-role ${normalized === "anchor" ? "anchor" : "support"}">${escapeHTML(normalized === "anchor" ? "ANCHOR" : normalized === "support" ? "SUPPORT" : role)}</small>`;
}

function renderFamilyOptimization(family) {
  const optimization = family.optimization;
  if (!optimization) return "";
  const negative = optimization.negative_mean_proxy_legs;
  const nonpositive = optimization.nonpositive_uncertainty_legs;
  const shortfall = optimization.anchor_shortfall;
  const coverageWarning = (typeof negative === "number" && negative > 0) || (typeof nonpositive === "number" && nonpositive > 0) || (typeof shortfall === "number" && shortfall > 0) || family.status === "BEST_AVAILABLE_COVERAGE";
  return `<div class="family-optimization ${coverageWarning ? "coverage-warning" : ""}"><div class="family-optimization-heading"><span>${coverageWarning ? "BEST_AVAILABLE_COVERAGE" : "FAMILY-SPECIFIC RESEARCH"}</span><strong>${optimization.anchors_selected === undefined ? "—" : escapeHTML(optimization.anchors_selected)} / ${optimization.anchor_target === undefined ? "—" : escapeHTML(optimization.anchor_target)} anchors</strong></div><p>${coverageWarning ? "Some selections fill research coverage despite nonpositive estimates or an anchor shortfall. This family is not presented as all-positive edge." : "Anchors and support legs are optimized for this family size; larger families need not contain the smaller family's exact thresholds."}</p><div class="optimization-counts"><span>Negative mean proxy: ${typeof negative === "number" ? negative : "Unreported"}</span><span>Nonpositive uncertainty score: ${typeof nonpositive === "number" ? nonpositive : "Unreported"}</span><span>Anchor shortfall: ${typeof shortfall === "number" ? shortfall : "Unreported"}</span></div><small>Marginal support objective only. No joint success probability or verified expected return is inferred.</small></div>`;
}

function renderTonightGame(game, tonight) {
  const sgp = game.sgp || {};
  const context = game.context || {};
  const players = context.players || [];
  const families = [...(sgp.draft_families || [])].sort((a, b) => { const order = {5:0,8:1,3:2}; return (order[a.leg_count] ?? 9) - (order[b.leg_count] ?? 9); });
  const tickets = sgp.tickets || [];
  const offered = sgp.offered_props || [];
  const published = sgp.published_props || [];
  const forecasts = (sgp.forecasts || []).filter((forecast) => forecast.synthetic === false);
  const historyGames = (game.history?.home_games || 0) + (game.history?.away_games || 0);
  const leaders = [...players].filter((player) => (player.historical?.games || 0) > 0).sort((a, b) => (b.historical.pts || 0) - (a.historical.pts || 0)).slice(0, 3);
  const draftCount = families.filter((family) => (family.legs || []).length > 0).length;
  return `<section class="panel tonight-card"><div class="tonight-card-top"><span class="eyebrow">${escapeHTML(easternTime(game.tipoff))}</span><span class="badge ${game.completed ? "muted" : ""}">${escapeHTML(game.status_detail || game.status || "Status unavailable")}</span></div><div class="tonight-matchup"><div class="tonight-team"><div class="team-abbr">${escapeHTML(game.away?.abbreviation || abbr(game.away?.name))}</div><div><h2>${escapeHTML(game.away?.name || "Away team unavailable")}</h2><span>AWAY</span></div></div><span class="tonight-at">@</span><div class="tonight-team"><div class="team-abbr">${escapeHTML(game.home?.abbreviation || abbr(game.home?.name))}</div><div><h2>${escapeHTML(game.home?.name || "Home team unavailable")}</h2><span>HOME</span></div></div></div><div class="game-readiness"><span class="${historyGames ? "ready" : "pending"}">History <strong>${historyGames ? `${historyGames} team-games` : "pending"}</strong></span><span class="${players.length ? "ready" : "pending"}">Roster <strong>${players.length ? `${players.length} players` : "pending"}</strong></span><span class="${offered.length ? "ready" : "pending"}">FanDuel lines <strong>${offered.length ? `${offered.length} offered` : published.length ? `${published.length} published` : "not captured"}</strong></span></div>${leaders.length ? `<div class="game-leaders">${leaders.map((player) => `<div><strong>${escapeHTML(player.name)}</strong><span>${num(player.historical.pts)} PTS <span class="leader-divider">/</span> ${num(player.historical.reb)} REB <span class="leader-divider">/</span> ${num(player.historical.ast)} AST</span><small>${escapeHTML(player.historical.games)} games · ${num(player.historical.minutes)} min · ${escapeHTML(player.availability || "UNVERIFIED")}</small></div>`).join("")}</div>` : `<div class="game-context-empty">${players.length ? `${players.length} roster entries loaded. Historical player aggregates are being matched by player and team.` : "Load current rosters and player history to construct this matchup's drafts."}</div>`}<div class="game-primary-actions"><button class="button ${isPregame(game) ? "primary" : "secondary"}" type="button" data-game-drafts="${escapeHTML(game.id)}" ${isPregame(game) ? "" : "disabled"}>${draftCount ? "Rebuild drafts" : "Generate drafts"} →</button><span class="game-draft-count">${draftCount ? `${draftCount} draft families` : "3 / 5 / 8-leg construction"}</span>${isPregame(game) && forecasts.length ? `<button class="button text" type="button" data-tonight-build="${escapeHTML(game.id)}">Evaluate forecast →</button>` : ""}</div>${renderPublishedMarkets(published)}<details class="game-section" ${families.some((family) => (family.legs || []).length) ? "open" : ""}><summary><span>SGP drafts</span><small>${draftCount ? `${draftCount} constructed` : "Awaiting context"}</small></summary>${families.length ? `${sgp.draft_stale ? '<p class="draft-stale-note">Saved drafts need refresh: current context or source freshness changed.</p>' : ""}<div class="draft-family-list">${families.map((family) => renderDraftFamily({ ...family, selection_mode: family.selection_mode || sgp.draft_selection_mode }, game.id)).join("")}</div>` : '<p class="game-section-note">Generate source-backed 3, 5, and 8-leg drafts from current roster identities and historical production. Thresholds remain analytical until matched to offered FanDuel lines.</p>'}</details><details class="game-section"><summary><span>Players</span><small>${players.length ? `${players.length} roster entries` : "Not loaded"}</small></summary>${renderGamePlayers(game)}</details><details class="game-section" ${tickets.length ? "open" : ""}><summary><span>Saved evaluations</span><small>${tickets.length ? `${tickets.length} saved` : "No tickets yet"}</small></summary>${tickets.length ? tickets.map((ticket) => renderTonightTicket(ticket)).join("") : '<p class="game-section-note">No evaluated SGP tickets yet. Constructed drafts and probability-evaluated tickets are kept distinct.</p>'}</details><details class="game-section"><summary><span>Sources & limits</span><small>${escapeHTML(context.status || "Context pending")}</small></summary><p class="game-section-note">${escapeHTML(context.reason || "Roster membership is distinct from confirmed game availability. Missing injury mapping or minutes restrictions stay unverified.")}</p><ul class="warning-list">${(sgp.block_reasons || []).map((reason) => `<li>${escapeHTML(reason)}</li>`).join("")}</ul><div class="game-source">${escapeHTML(game.source_url || tonight.source_url || "Source unavailable")}<br>${escapeHTML(context.source_url || (typeof context.source === "object" ? context.source?.url || context.source?.provider : context.source) || "Current roster source pending")}</div></details><div id="tonight-game-note-${escapeHTML(game.id)}" class="help" hidden></div></section>`;
}

function renderDraftFamily(family, gameId, expanded = false) {
  const legs = family.legs || [];
  const count = family.leg_count ?? legs.length;
  const modeLabel = family.selection_mode === "aggressive" ? "AGGRESSIVE" : family.selection_mode === "standard" ? "CORE" : "SAVED MODE UNRECORDED";
  const offeredCount = legs.filter((leg) => leg.threshold_kind === "OFFERED_LINE").length;
  return `<details class="draft-family-shell" ${expanded || count === 5 || (!family.optimization && count === 3) ? "open" : ""}><summary><span>${escapeHTML(count)} legs</span><small>${legs.length ? offeredCount ? `${offeredCount} source-bound lines` : "Analytical draft" : "Insufficient data"}</small></summary><article class="draft-family"><div class="draft-family-heading"><div><strong>${escapeHTML(count)} legs</strong><span>${escapeHTML(family.status || "DRAFT")}</span></div><span class="badge ${legs.length ? "" : "muted"}">${legs.length ? offeredCount === legs.length ? "SOURCE-BOUND OFFERED LINES" : offeredCount ? "MIXED LINE COVERAGE" : "ANALYTICAL DRAFT" : family.selection_mode === "aggressive" ? "QUOTE / DATA GATE" : "INSUFFICIENT DATA"}</span></div>${renderFamilyOptimization(family)}${legs.length ? `<ol class="draft-leg-list">${legs.map((leg) => { const descriptive = leg.descriptive || {}; const hits = descriptive.hits ?? leg.historical_hits; const games = descriptive.games ?? leg.sample_games; const rate = descriptive.hit_rate ?? leg.historical_hit_rate; return `<li><div><strong>${escapeHTML(leg.player_name || leg.player_id)} ${renderFamilyRole(leg.family_role)}</strong><span>${escapeHTML(SGP_STATS[leg.stat] || leg.stat)} <b>${leg.side === "at_least" ? `${num(leg.line, 0)}+` : `${escapeHTML(leg.side)} ${num(leg.line)}`}</b></span><small class="draft-leg-source">${leg.threshold_kind === "OFFERED_LINE" ? "Source-bound offered line · provider authenticity unverified" : "Analytical threshold · not offered"}${leg.minutes?.mean !== undefined ? ` · ${num(leg.minutes.mean)} min avg` : ""}</small>${renderSelectedLegPrice(leg)}${renderLegDiagnostics(leg)}${aggressiveProxyNote(leg)}</div><div class="draft-leg-evidence"><strong>${hits !== undefined && games !== undefined ? `${escapeHTML(hits)}/${escapeHTML(games)}` : "Sample unavailable"}</strong><span>${typeof rate === "number" ? `${pct(rate, 0)} historical hits` : "Descriptive history only"}</span>${leg.recent5?.length ? `<small>Last ${leg.recent5.length}: ${leg.recent5.map((row) => escapeHTML(row.value)).join(" · ")}</small>` : ""}</div></li>`; }).join("")}</ol><div class="draft-disclosure"><span>${offeredCount ? `${offeredCount}/${legs.length} source-bound offered lines; source assertions unverified` : "Analytical thresholds · not offered FanDuel lines"}</span><span>Joint chance unavailable <span>·</span> Combined ticket price unavailable</span></div><button class="button text" type="button" data-draft-builder="${escapeHTML(gameId)}" data-draft-family="${escapeHTML(count)}">Open draft in builder →</button>` : `<p class="game-section-note">${escapeHTML(family.block_reasons?.[0] || "Not enough eligible, source-backed candidates to fill this family.")}</p>`}<details class="draft-method"><summary>Construction notes</summary><ul class="warning-list">${(family.block_reasons || []).map((reason) => `<li>${escapeHTML(reason)}</li>`).join("")}</ul><p class="help">Historical hit rates describe the observed sample. They are not a calibrated chance for this next game or an instruction to bet.</p></details></article></details>`;
}

function renderGamePlayers(game) {
  const players = game.context?.players || [];
  if (!players.length) return '<p class="game-section-note">Current roster context is not loaded. Generate drafts or refresh players to retrieve the source snapshot.</p>';
  return `<div class="data-table-wrap game-player-table"><table class="data-table"><thead><tr><th>PLAYER</th><th>PTS</th><th>REB</th><th>AST</th><th>MIN</th><th>GAMES</th></tr></thead><tbody>${players.map((player) => `<tr><td>${escapeHTML(player.name)}<div class="player-meta">${escapeHTML(player.position || "Position unavailable")} · ${escapeHTML(player.team_id === game.home?.id ? game.home?.abbreviation || game.home?.name : player.team_id === game.away?.id ? game.away?.abbreviation || game.away?.name : "Team unverified")} · ${escapeHTML(player.injury_status || player.availability || "Availability unverified")}</div></td><td>${num(player.historical?.pts)}</td><td>${num(player.historical?.reb)}</td><td>${num(player.historical?.ast)}</td><td>${num(player.historical?.minutes)}</td><td>${player.historical?.games === undefined ? "—" : escapeHTML(player.historical.games)}</td></tr>`).join("")}</tbody></table></div><p class="game-section-note">Historical averages use same-team player records before this game. Roster membership does not confirm a starter, expected minutes, or injury clearance.</p>`;
}

function offerAmerican(offer) {
  if (!offer || typeof offer !== "object") return "Unavailable";
  const quoted = americanOdds(offer.american_odds);
  if (quoted !== "Unavailable") return quoted;
  const decimal = offer.decimal_odds;
  if (typeof decimal !== "number" || !Number.isFinite(decimal) || decimal <= 1) return "Unavailable";
  return americanOdds(decimal >= 2 ? 100 * (decimal - 1) : -100 / (decimal - 1));
}

function offerVenue(offer) {
  const name = offer?.sportsbook || offer?.provider || "Venue unverified";
  return String(name).toLowerCase() === "polymarket" ? "Polymarket" : String(name).toLowerCase() === "fanduel" ? "FanDuel" : String(name);
}

function renderSelectedLegPrice(leg) {
  const offer = leg.offer;
  if (!offer) return '<div class="selected-leg-price no-price"><strong>Price unavailable</strong><span>Analytical threshold</span></div>';
  const venue = offerVenue(offer);
  return `<div class="selected-leg-price"><strong>${offerAmerican(offer)}</strong><span>${escapeHTML(venue)}${typeof offer.decimal_odds === "number" ? ` - decimal ${num(offer.decimal_odds, 3)}` : ""}</span><small>${venue === "Polymarket" ? "Gross BUY-ask equivalent; fees excluded. Separate exchange contracts, no combined SGP quote." : "Individual offered selection; exact combined ticket price remains separate."}</small></div>`;
}

function renderLegDiagnostics(leg) {
  const evidence = leg.selection_evidence || {};
  const ladder = leg.offered_alt_ladder || [];
  if (!Object.keys(evidence).length && !ladder.length) return "";
  const ladderHTML = ladder.map((row) => {
    const offer = row.offer || { american_odds: row.american_odds, decimal_odds: row.decimal_odds, sportsbook: row.sportsbook || leg.offer?.sportsbook };
    const hits = row.historical_hits ?? row.hits;
    const games = row.sample_games ?? row.descriptive?.games;
    const reason = row.reason || (row.block_reasons || []).join("; ");
    const math = row.aggressive_evidence || row;
    return `<li><div class="alt-ladder-price"><span>${escapeHTML(row.side || leg.side || "")} ${num(row.line)}</span><strong>${offerAmerican(offer)}</strong></div><small>${escapeHTML(offerVenue(offer))}${typeof offer.decimal_odds === "number" ? ` - decimal ${num(offer.decimal_odds, 3)}` : ""} - ${typeof hits === "number" && typeof games === "number" ? `${hits}/${games} historical hits` : "Historical support unavailable"}</small>${isUpdatedAggressiveEvidence(math) ? renderAggressiveRanking(math, true) : ""}${reason ? `<small>${escapeHTML(reason)}</small>` : ""}</li>`;
  }).join("");
  return `<details class="leg-ladder-evidence"><summary>Selection support & higher lines</summary><div>${escapeHTML(leg.selection_basis || evidence.ranking || "Selection basis unavailable")}</div>${leg.historical_distribution ? `<div>Prior mean ${num(leg.historical_distribution.mean)} - range ${num(leg.historical_distribution.minimum, 0)} to ${num(leg.historical_distribution.maximum, 0)}</div>` : ""}<div>Participation: ${escapeHTML(leg.availability || "UNVERIFIED")} - calibrated next-game chance unavailable</div>${ladder.length ? `<ul class="alt-ladder-list">${ladderHTML}</ul>` : '<div>No source-bound offered alternate ladder captured.</div>'}<div>Higher thresholds generally reduce historical hit support. Aggressive mode does not imply a validated edge.</div></details>`;
}

function aggressiveProxyNote(leg) {
  const evidence = leg.aggressive_evidence;
  if (!evidence) return "";
  if (isUpdatedAggressiveEvidence(evidence)) {
    const sd = typeof evidence.probability_sd === "number" ? `${num(evidence.probability_sd * 100, 2)} pp` : "Unavailable";
    return `<div class="aggressive-proxy-note updated-proxy"><strong>Uncalibrated market-anchored research</strong><span>Market-shrunk estimate ${pct(evidence.proxy_probability)} - quote reference ${pct(evidence.quote_reference_probability)}</span><span>Probability SD ${sd} - uncertainty-adjusted estimate ${pct(evidence.uncertainty_adjusted_probability)}</span>${renderAggressiveRanking(evidence)}${typeof evidence.legacy_probability === "number" ? `<details class="legacy-proxy"><summary>Legacy historical proxy</summary><span>${pct(evidence.legacy_probability)} - retained for comparison; not the current market-shrunk estimate.</span></details>` : ""}<span>Ranking combines market shrinkage, uncertainty and available entry fees. These research estimates are uncalibrated; no demonstrated accuracy or approved price edge is claimed.</span></div>`;
  }
  return `<div class="aggressive-proxy-note"><strong>Uncalibrated aggressive research proxy</strong><span>Proxy ${pct(evidence.proxy_probability)} · quoted break-even ${pct(evidence.break_even_probability)}</span><span>Gross research return proxy ${typeof evidence.proxy_return_gross === "number" ? `${num(evidence.proxy_return_gross * 100, 2)}%` : "unavailable"}. This is a screening diagnostic, not a validated probability or approved price edge.</span></div>`;
}

function isUpdatedAggressiveEvidence(evidence) {
  return evidence && (evidence.ranking_basis !== undefined || evidence.ranking_score !== undefined || evidence.quote_reference_probability !== undefined);
}

function renderAggressiveRanking(evidence, compact = false) {
  const feeAdjusted = evidence.ranking_basis === "FEE_ADJUSTED_RESEARCH";
  const fee = typeof evidence.fee_per_share === "number" ? `$${num(evidence.fee_per_share, 4)}/share` : "Unavailable";
  const entry = typeof evidence.entry_cost === "number" ? `$${num(evidence.entry_cost, 4)}/share` : "Unavailable";
  const feeReturn = typeof evidence.proxy_return_after_entry_fee === "number" ? `${num(evidence.proxy_return_after_entry_fee * 100, 2)}%` : "Unavailable";
  const basis = feeAdjusted ? "FEE-ADJUSTED RESEARCH" : evidence.ranking_basis === "GROSS_RESEARCH_ONLY_FEES_UNAVAILABLE" ? "GROSS RESEARCH - FEES UNAVAILABLE" : evidence.ranking_basis || "RANKING BASIS UNAVAILABLE";
  return `<div class="aggressive-ranking ${compact ? "compact" : ""}"><span class="ranking-basis">${escapeHTML(basis)}</span><span>Uncertainty-adjusted ranking score ${num(evidence.ranking_score, 4)}</span><span>Entry fee ${fee} - entry cost ${entry}</span><span>Research return after entry fee ${feeReturn}</span>${!feeAdjusted ? '<span class="missing-fee-note">A net-of-entry-fee ranking cannot be claimed where fees are unavailable.</span>' : ""}</div>`;
}

function renderPublishedMarkets(markets) {
  if (!markets?.length) return "";
  return `<details class="game-section published-markets" open><summary><span>FanDuel published props</span><small>${markets.length} captured · timing unverified</small></summary><div class="data-table-wrap"><table class="data-table"><thead><tr><th>PLAYER / MARKET</th><th>LINE</th><th>PUBLISHED ODDS</th></tr></thead><tbody>${markets.map((market) => `<tr><td>${escapeHTML(market.player_name || market.player_id || "Player unavailable")}<div class="player-meta">${escapeHTML(SGP_STATS[market.stat] || market.stat || "Market unavailable")}</div></td><td>${escapeHTML((market.side || "").replaceAll("_", " "))} ${num(market.line)}</td><td class="stat-highlight">${typeof market.american_odds === "number" ? `${market.american_odds > 0 ? "+" : ""}${num(market.american_odds, 0)}` : num(market.decimal_odds, 3)}</td></tr>`).join("")}</tbody></table></div><p class="published-market-note">Published by FanDuel Research. No verified quote update clock; these prices are not confirmed current offers. Exact combined SGP price unavailable.</p><details class="draft-method"><summary>Published source receipts</summary>${markets.map((market) => `<div class="game-source">${escapeHTML(market.source_url || "Source unavailable")}<br>Article published ${escapeHTML(shortDate(market.article_published_at))}<br>Retrieved ${escapeHTML(shortDate(market.retrieved_at))} · Price freshness ${escapeHTML(market.price_freshness || "UNKNOWN")}${market.event_time_discrepancy_seconds ? `<br>Event time discrepancy ${escapeHTML(market.event_time_discrepancy_seconds)} seconds; matchup binding requires review.` : ""}${market.source_sha256 ? `<br>SHA-256 · ${escapeHTML(market.source_sha256)}` : ""}</div>`).join("")}</details></details>`;
}

async function refreshFanDuelOdds() {
  $("tonight-odds-refresh").disabled = true;
  $("tonight-odds-refresh").textContent = "Fetching FanDuel sources…";
  message("Fetching available FanDuel market publications and preserving quote timing and source provenance…");
  try {
    const receipt = await api("/api/odds/refresh", { method: "POST", body: "{}" });
    await loadTonight();
    message(`FanDuel source refresh complete${typeof receipt.offers_count === "number" ? ` · ${receipt.offers_count} published props captured` : ""}. Partial coverage; price update times are unverified and exact combined SGP pricing is unavailable.`);
  } catch (error) { message(error.message, true); }
  finally { $("tonight-odds-refresh").disabled = false; $("tonight-odds-refresh").textContent = "Refresh FanDuel odds ↻"; }
}

async function generateGameDrafts(id) {
  if (state.draftBuildBusy) return;
  const game = state.tonight?.games?.find((candidate) => candidate.id === id);
  if (!game || !isPregame(game)) return message("This game's pregame window is closed. Existing drafts remain inspectable.", true);
  state.draftBuildBusy = true;
  const button = [...document.querySelectorAll("[data-game-drafts]")].find((candidate) => candidate.dataset.gameDrafts === id);
  if (button) { button.disabled = true; button.textContent = "Building from sources…"; }
  message("Retrieving current team context and constructing source-backed draft families…");
  try {
    await api(`/api/games/${encodeURIComponent(id)}/drafts?selection_mode=${selectionMode()}`, { method: "POST", body: "{}" });
    await loadTonight();
    message("Draft families refreshed. Review the actual legs and historical evidence for each game.");
  } catch (error) { message(error.message, true); }
  finally { state.draftBuildBusy = false; if (button) { button.disabled = false; button.textContent = "Generate drafts →"; } }
}

async function buildAllTonightDrafts() {
  if (state.draftBuildBusy) return;
  state.draftBuildBusy = true;
  $("tonight-build-all").disabled = true;
  $("tonight-build-all").textContent = "Refreshing players & building…";
  message("Updating current rosters and constructing tonight's source-backed drafts. This can take a moment.");
  try {
    let job = await api(`/api/tonight/build?selection_mode=${selectionMode()}`, { method: "POST", body: "{}" });
    const jobId = job.job_id || job.id;
    if (job.status === "RUNNING" && !jobId) throw new Error("Build started without a trackable job ID");
    for (let attempt = 0; job.status === "RUNNING" && attempt < 180; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      job = await api(`/api/build-jobs/${encodeURIComponent(jobId)}`);
      const progress = job.games_built ?? job.completed_games ?? job.completed ?? job.processed;
      message(`${job.stage || "Constructing source-backed drafts"}${typeof progress === "number" ? ` · ${progress} games built` : "…"}`);
    }
    if (job.status === "FAILED") throw new Error(job.error || job.reason || "Draft build failed; inspect source coverage and retry");
    if (job.status === "RUNNING") throw new Error("The build is still running. Refresh tonight's games to inspect its latest persisted results.");
    await loadTonight();
    message(`Tonight's player context and draft families refreshed${typeof job.games_built === "number" ? ` · ${job.games_built} games built` : ""}${job.errors?.length ? ` · ${job.errors.length} games need source review (inspect individual coverage)` : ""}.`);
  } catch (error) { message(error.message, true); }
  finally { state.draftBuildBusy = false; $("tonight-build-all").disabled = false; $("tonight-build-all").textContent = "Refresh players & build drafts →"; }
}

function openDraftBuilder(gameId, count) {
  const game = state.tonight?.games?.find((candidate) => candidate.id === gameId);
  const family = game?.sgp?.draft_families?.find((candidate) => Number(candidate.leg_count ?? candidate.legs?.length) === Number(count));
  if (!game || !family || !family.legs?.length) return message("This draft family has no source-backed legs yet. Generate it from the game card.", true);
  state.selectedDraft = { game, family };
  if (state.marketGameId !== game.id) { state.marketLegs = []; state.marketGameId = game.id; }
  renderDraftWorkspace();
  tab("sgp");
}

function renderDraftWorkspace() {
  const selected = state.selectedDraft;
  if (!selected) return;
  const { game, family } = selected;
  if (!family) {
    $("draft-workspace").innerHTML = `<div class="builder-game-heading"><div><span class="eyebrow">${escapeHTML(easternTime(game.tipoff))}</span><h2>${escapeHTML(game.away?.name)} <span>@</span> ${escapeHTML(game.home?.name)}</h2></div><button class="button text" type="button" data-switch-tab="tonight">← All games</button></div><section class="panel"><span class="eyebrow">FANDUEL / MANUAL SOURCE SNAPSHOT</span><h2>Add the offered lines you can see.</h2><p class="help">Enter the player markets and prices displayed in your FanDuel app. Save several lines together, then rebuild the matchup's draft families against those exact selections.</p></section><div id="manual-market-workspace"></div>`;
    renderMarketForm(game);
    return;
  }
  $("draft-workspace").innerHTML = `<div class="builder-game-heading"><div><span class="eyebrow">${escapeHTML(easternTime(game.tipoff))}</span><h2>${escapeHTML(game.away?.name)} <span>@</span> ${escapeHTML(game.home?.name)}</h2></div><button class="button text" type="button" data-switch-tab="tonight">← All games</button></div><div class="builder-draft-layout"><section class="panel"><div class="panel-heading"><div><span class="eyebrow">SOURCE-BACKED CONSTRUCTION</span><h2>${escapeHTML(family.leg_count ?? family.legs.length)}-leg draft</h2></div><span class="badge">ANALYTICAL THRESHOLDS</span></div>${renderDraftFamily(family, game.id, true)}<div class="builder-next-step"><h3>Next: match the actual offered legs.</h3><p>These thresholds were selected from historical production. Automatic offered-line coverage is shown for this game. A matching line and exact combined ticket quote are required before comparing value.</p></div></section><aside class="panel builder-receipt"><span class="eyebrow">TICKET STATUS</span><h2>Constructed. Unpriced.</h2><ul class="compact-list"><li><span>Selected legs</span><span>${family.legs.length}</span></li><li><span>FanDuel lines matched</span><span>Unverified</span></li><li><span>Joint forecast probability</span><span>Unavailable</span></li><li><span>Exact combined quote</span><span>Unavailable</span></li></ul><p class="help">Historical hit counts are descriptive. They are not multiplied into a parlay probability.</p>${(game.sgp?.forecasts || []).some((forecast) => forecast.synthetic === false) ? `<button class="button secondary" type="button" data-tonight-build="${escapeHTML(game.id)}">Open matching forecast →</button>` : '<div class="panel-note">A game-specific frozen forecast is required for shared-sample joint evaluation.</div>'}</aside></div>`;
  $("draft-workspace").insertAdjacentHTML("beforeend", '<div id="manual-market-workspace"></div>');
  renderMarketForm(game);
}

function openMarketBuilder(gameId) {
  const game = state.tonight?.games?.find((candidate) => candidate.id === gameId);
  if (!game) return;
  state.selectedDraft = { game, family: null };
  if (state.marketGameId !== game.id) { state.marketLegs = []; state.marketGameId = game.id; }
  renderDraftWorkspace();
  tab("sgp");
}

function renderMarketForm(game) {
  const players = game.context?.players || [];
  const destination = $("manual-market-workspace");
  if (!destination) return;
  destination.innerHTML = `<details class="manual-source-fallback"><summary>Advanced source entry (manual fallback)</summary><section class="panel manual-market-panel"><div class="panel-heading"><div><span class="eyebrow">COPY FROM YOUR FANDUEL APP</span><h2>Offered lines</h2></div><span class="badge muted">MANUAL SNAPSHOT</span></div>${players.length ? `<form id="market-leg-form" class="manual-market-controls"><label>Player<select id="market-player" required>${option("", "Choose current roster player")}${players.map((player) => option(player.id, `${player.name} · ${player.team_id === game.home.id ? game.home.abbreviation || game.home.name : game.away.abbreviation || game.away.name}`)).join("")}</select></label><label>Market<select id="market-stat">${Object.entries(SGP_STATS).map(([id, label]) => option(id, label)).join("")}</select></label><label>Selection<select id="market-side"><option value="over">Over</option><option value="under">Under</option><option value="at_least">At least</option></select></label><label>Exact offered line<input id="market-line" type="number" min="0" step="0.5" required placeholder="Enter displayed line"></label><label>Odds format<select id="market-odds-format"><option value="american">American (+ / −)</option><option value="decimal">Decimal</option></select></label><label>Exact offered price<input id="market-price" type="number" step="any" required placeholder="Enter displayed odds"></label><button class="button secondary" type="submit">Add offered leg +</button></form><div id="market-basket"></div><form id="market-save-form" class="market-save-form"><label>Time you observed these lines (UTC)<input id="market-observed" type="datetime-local" step="1" required value="${escapeHTML(new Date().toISOString().slice(0, 19))}"></label><label>Source reference<input id="market-reference" type="text" minlength="5" maxlength="1000" required placeholder="FanDuel app screen or betslip reference"></label><label class="market-confirm"><input id="market-confirm" type="checkbox" required><span>I copied these exact lines and prices from FanDuel and confirmed the observation time.</span></label><div class="market-save-actions"><button id="market-save" class="button primary" type="submit">Save lines & rebuild drafts →</button><span class="help">Current roster matched · source authenticity is unverified</span></div></form><div id="market-receipt" role="status" aria-live="polite"></div>` : '<p class="game-section-note">Load this game’s current roster before entering player markets. Historical team membership is not substituted.</p>'}<p class="help">This saves all entered legs as one snapshot. It does not place a bet. Individual prices are never multiplied into a combined SGP price; that requires the exact ticket quote.</p></section></details>`;
  if (players.length) {
    $("market-leg-form").addEventListener("submit", addMarketLeg);
    $("market-save-form").addEventListener("submit", saveMarketSnapshot);
    renderMarketBasket();
  }
}

function decimalPrice(value, format) {
  const price = Number(value);
  if (value === "" || !Number.isFinite(price)) throw new Error("Enter the exact offered price");
  if (format === "decimal") {
    if (price <= 1) throw new Error("Decimal odds must exceed 1");
    return price;
  }
  if (!Number.isInteger(price) || (price > -100 && price < 100)) throw new Error("American odds require +100 or greater, or −100 or lower");
  return price > 0 ? 1 + price / 100 : 1 + 100 / Math.abs(price);
}

function addMarketLeg(event) {
  event.preventDefault();
  const game = state.selectedDraft?.game;
  const player = game?.context?.players?.find((candidate) => candidate.id === $("market-player").value);
  if (!player) return message("Choose a player from this game's current source roster.", true);
  const lineValue = $("market-line").value;
  const line = Number(lineValue);
  const side = $("market-side").value;
  if (lineValue === "" || !Number.isFinite(line) || line < 0 || (side === "at_least" ? !Number.isInteger(line) : !Number.isInteger(line * 2) || Number.isInteger(line))) return message("Use a half-line for over/under, or an integer for at least. Enter the exact offered selection.", true);
  let price;
  try { price = decimalPrice($("market-price").value, $("market-odds-format").value); }
  catch (error) { return message(error.message, true); }
  const leg = { player_id: player.id, team_id: player.team_id, stat: $("market-stat").value, side, line, decimal_odds: price };
  if (state.marketLegs.some((existing) => existing.player_id === leg.player_id && existing.stat === leg.stat && existing.side === leg.side && existing.line === leg.line)) return message("This exact selection is already entered. Remove it to replace its price.", true);
  state.marketLegs.push(leg);
  renderMarketBasket();
  $("market-line").value = "";
  $("market-price").value = "";
  message("");
}

function renderMarketBasket() {
  const game = state.selectedDraft?.game;
  $("market-basket").innerHTML = state.marketLegs.length ? `<div class="data-table-wrap market-basket-table"><table class="data-table"><thead><tr><th>ENTERED PLAYER / MARKET</th><th>SELECTION</th><th>DECIMAL PRICE</th><th></th></tr></thead><tbody>${state.marketLegs.map((leg, index) => `<tr><td>${escapeHTML(game.context.players.find((player) => player.id === leg.player_id)?.name || leg.player_id)}<div class="player-meta">${escapeHTML(SGP_STATS[leg.stat] || leg.stat)}</div></td><td>${escapeHTML(leg.side.replaceAll("_", " "))} ${num(leg.line, leg.side === "at_least" ? 0 : 1)}</td><td>${num(leg.decimal_odds, 4)}</td><td><button class="button text" type="button" data-remove-market-leg="${index}">Remove ×</button></td></tr>`).join("")}</tbody></table></div>` : '<p class="game-section-note">No offered lines entered. Add as many actual selections as you want in this snapshot.</p>';
}

async function saveMarketSnapshot(event) {
  event.preventDefault();
  const game = state.selectedDraft?.game;
  if (!game || !state.marketLegs.length) return message("Add at least one actual offered leg before saving.", true);
  if (!$("market-confirm").checked) return message("Confirm that you copied the exact FanDuel selections and observation time.", true);
  const observed = $("market-observed").value;
  const reference = $("market-reference").value.trim();
  if (!observed || !Number.isFinite(Date.parse(observed + "Z")) || reference.length < 5) return message("Enter the observation time and a usable source reference.", true);
  const payload = { game_id: game.id, home_id: game.home.id, away_id: game.away.id, tipoff: game.tipoff, observed_at: new Date(observed + "Z").toISOString(), sportsbook: "fanduel", source_reference: reference, legs: state.marketLegs };
  $("market-save").disabled = true;
  try {
    const receipt = await api("/api/markets/import", { method: "POST", body: JSON.stringify(payload) });
    $("market-receipt").innerHTML = `<div class="market-receipt"><strong>${escapeHTML(receipt.lines_imported)} offered lines saved</strong><span>${escapeHTML(receipt.authenticity || "CALLER_ASSERTION_UNVERIFIED")}</span><small>Receipt ${escapeHTML(receipt.id)}</small></div>`;
    try {
      await api(`/api/games/${encodeURIComponent(game.id)}/drafts`, { method: "POST", body: "{}" });
      await loadTonight();
      message("Exact entered lines saved and game drafts rebuilt. Sources remain caller assertions until independently verified.");
    } catch (error) { message(`Lines were saved. Draft rebuild needs review: ${error.message}`, true); }
  } catch (error) { message(error.message, true); }
  finally { $("market-save").disabled = false; }
}

function renderTonightTicket(ticket) {
  return `<article class="tonight-ticket"><div class="tonight-ticket-heading"><strong>Saved ticket ${escapeHTML(String(ticket.id || ticket.forecast_id || "").slice(0, 8))}</strong><span class="badge ${ticket.synthetic ? "warning" : "muted"}">${ticket.synthetic ? "SYNTHETIC · BLOCKED" : "RESEARCH ONLY"}</span></div><ul class="tonight-ticket-legs">${(ticket.legs || []).map((leg) => `<li><span>${escapeHTML(leg.player_name || leg.player_id)} · ${escapeHTML(SGP_STATS[leg.stat] || leg.stat)}</span><span>${escapeHTML((leg.side || "").replaceAll("_", " "))} ${num(leg.line, leg.side === "at_least" ? 0 : 1)}</span></li>`).join("")}</ul><div class="tonight-ticket-metrics"><span>Joint model probability <strong>${pct(ticket.joint_probability)}</strong></span><span>Exact quote <strong>${ticket.quote_status === "VALID" && ticket.quote && !ticket.synthetic ? num(ticket.quote.decimal_odds, 3) : "Unavailable"}</strong></span><span>Actionability <strong class="orange-text">RESEARCH ONLY</strong></span></div><p class="help">${escapeHTML(shortDate(ticket.created_at || ticket.provenance?.as_of))} · Forecast ${escapeHTML(String(ticket.forecast_id || "").slice(0, 12))} · ${escapeHTML(ticket.sample_count ?? "—")} shared samples</p><details class="readiness-details"><summary>Ticket assumptions & blockers</summary><ul class="warning-list">${(ticket.betting_block_reasons || []).map((reason) => `<li>${escapeHTML(reason)}</li>`).join("")}</ul></details></article>`;
}

async function buildTonightSGP(id) {
  const game = state.tonight?.games?.find((candidate) => candidate.id === id);
  if (!game) return;
  const pregame = !game.completed && (game.phase || game.status) === "pre" && Date.parse(game.tipoff) > Date.now();
  const forecast = pregame && (game.sgp?.forecasts || []).find((candidate) => candidate.synthetic === false);
  if (!forecast) {
    const note = $(`tonight-game-note-${id}`);
    if (note) {
      note.hidden = false;
      note.textContent = game.completed ? "This game is completed. New pregame SGP drafting is unavailable; saved tickets remain inspectable." : `No current non-demo forecast and verified player lines are available for this game. ${(game.sgp?.block_reasons || []).join(" ")} No historical roster is used as a current rotation substitute.`;
    }
    return;
  }
  tab("sgp", false);
  await loadSGPForecasts();
  if (![...$("sgp-forecast").options].some((entry) => entry.value === forecast.id)) {
    $("sgp-forecast").insertAdjacentHTML("beforeend", option(forecast.id, `${game.home?.name || game.home?.id} vs ${game.away?.name || game.away?.id} · ${shortDate(game.tipoff)} · ${forecast.id.slice(0, 8)}`));
  }
  $("sgp-forecast").value = forecast.id;
  await chooseSGPForecast();
  const engine = document.querySelector(".forecast-engine");
  if (engine) engine.open = true;
}

function renderReadiness() {
  const source = state.sources || {};
  const authentic = (state.status?.datasets || []).filter((d) => !d.synthetic);
  const games = source.history?.games ?? authentic.reduce((sum, d) => sum + (d.games_count || 0), 0);
  const reasons = source.readiness?.reasons || [];
  const odds = source.odds || {};
  $("data-readiness").innerHTML = `<div class="readiness-header"><span class="eyebrow">AUTHENTIC DATA + SIGNAL READINESS</span><span class="badge warning">NO ACTIONABLE SIGNAL</span></div><div class="readiness-grid"><div><span>HISTORY PROVIDER</span><strong>${escapeHTML(source.stats_provider || "Checking…")}</strong><small>Source-backed historical box scores</small></div><div><span>NON-DEMO COVERAGE</span><strong>${escapeHTML(games)} games</strong><small>${authentic.length} imported snapshots · ${authentic.reduce((sum, d) => sum + (d.rotation_count || 0), 0)} rotation records</small></div><div><span>ODDS CONNECTION</span><strong>${escapeHTML(odds.status || (odds.configured ? "CONFIGURED" : "UNAVAILABLE"))}</strong><small>${odds.configured ? "Configuration alone does not verify quote coverage" : "No exact SGP price feed connected"}</small></div><div><span>CALIBRATED EDGE</span><strong class="orange-text">UNVERIFIED</strong><small>Independent validation required</small></div></div>${reasons.length ? `<details class="readiness-details"><summary>Readiness gaps (${reasons.length})</summary><ul class="warning-list">${reasons.map((r) => `<li>${escapeHTML(typeof r === "string" ? r : JSON.stringify(r))}</li>`).join("")}</ul></details>` : '<p class="help">Historical observations, current availability, market quotes, and independent calibration are separate coverage requirements.</p>'}`;
  const injury = source.injuries;
  $("data-readiness").innerHTML += `<div class="readiness-evidence"><div><span class="eyebrow">${escapeHTML(source.target_sportsbook || "FanDuel")} · COMBINED TICKET</span><p>${odds.configured ? "Provider configuration detected; exact offered combined ticket coverage remains unverified." : "No odds subscription connected. Enter an exact current combined FanDuel quote in the SGP builder; no prices are assumed."}</p></div><div><span class="eyebrow">OFFICIAL AVAILABILITY EVIDENCE</span><p>${injury ? `${escapeHTML(injury.status || "UNVERIFIED")} · Published ${escapeHTML(shortDate(injury.report_published_at))} · Retrieved ${escapeHTML(shortDate(injury.retrieved_at))}. Identity mapping ${escapeHTML(injury.identity_mapping || "UNREVIEWED")}; automatic roster eligibility ${injury.eligible_for_automatic_roster_gate === true ? "requires verified review" : "blocked"}.` : "No official report receipt available. Current eligibility and restrictions are unavailable."}</p>${injury?.sha256 ? `<details><summary>Inspect report receipt</summary><div class="hash">SHA-256 · ${escapeHTML(injury.sha256)}<br>Source · ${escapeHTML(injury.source_url || "Unavailable")}</div></details>` : ""}</div></div>`;
}

async function loadSGPForecasts() {
  try {
    const result = await api("/api/forecasts");
    const forecasts = Array.isArray(result) ? result : result.forecasts || [];
    const selected = $("sgp-forecast").value;
    $("sgp-forecast").innerHTML = option("", "Choose a forecast explicitly") + forecasts.map((f) => option(f.id, `${f.synthetic ? "SYNTHETIC · " : ""}${f.request.home_id} vs ${f.request.away_id} · ${shortDate(f.request.tipoff)} · ${f.id.slice(0, 8)}`)).join("");
    if (forecasts.some((f) => f.id === selected)) $("sgp-forecast").value = selected;
    else if (state.sgpForecast) clearSGPForecast();
  } catch (error) { $("sgp-forecast-context").textContent = error.message; }
}

function clearSGPForecast() {
  state.sgpForecast = null;
  state.sgpLegs = [];
  $("sgp-player").innerHTML = option("", "Choose a forecast first");
  $("sgp-forecast-context").textContent = "No forecast selected. Authentic historical stats alone do not supply a current injury or rotation snapshot.";
  $("sgp-results").innerHTML = "";
  $("sgp-quote").value = "";
  renderSGPLegs();
}

async function chooseSGPForecast() {
  const requestId = ++state.sgpRequest;
  const id = $("sgp-forecast").value;
  clearSGPForecast();
  if (!id) return;
  try {
    const f = await api(`/api/forecasts/${encodeURIComponent(id)}`);
    if (requestId !== state.sgpRequest) return;
    state.sgpForecast = f;
    $("sgp-player").innerHTML = option("", "Choose a player") + f.result.players.map((p) => option(p.id, `${p.name} · ${p.position}`)).join("");
    $("sgp-forecast-context").innerHTML = `<span class="badge ${f.synthetic ? "warning" : "muted"}">${f.synthetic ? "SYNTHETIC BLOCK · FICTIONAL INPUTS" : "UNCALIBRATED RESEARCH"}</span><p>${escapeHTML(f.request.home_id)} vs ${escapeHTML(f.request.away_id)} · ${escapeHTML(f.request.simulations)} shared samples · Cutoff ${escapeHTML(shortDate(f.request.cutoff))}</p><p>${f.synthetic ? "Demo probabilities cannot support an authentic NBA betting signal. Price and expected value remain blocked." : "Imported history does not verify current injuries, lineups, settlement rules or predictive calibration."}</p>`;
  } catch (error) { if (requestId === state.sgpRequest) $("sgp-forecast-context").textContent = error.message; }
}

const SGP_STATS = { pts: "Points", reb: "Rebounds", ast: "Assists", fg3m: "Made threes", stl: "Steals", blk: "Blocks", tov: "Turnovers", pra: "PTS + REB + AST", points_rebounds: "PTS + REB", points_assists: "PTS + AST", rebounds_assists: "REB + AST" };

function renderSGPLegs() {
  $("sgp-legs").innerHTML = state.sgpLegs.length ? `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>PLAYER</th><th>STATISTIC</th><th>CONDITION</th><th></th></tr></thead><tbody>${state.sgpLegs.map((leg, i) => {
    const player = state.sgpForecast?.result.players.find((p) => p.id === leg.player_id);
    return `<tr><td>${escapeHTML(player?.name || leg.player_id)}</td><td>${escapeHTML(SGP_STATS[leg.stat] || leg.stat)}</td><td>${escapeHTML(leg.side.replaceAll("_", " "))} ${num(leg.line, leg.side === "at_least" ? 0 : 1)}</td><td><button class="button text" type="button" data-remove-sgp-leg="${i}" aria-label="Remove leg ${i + 1}">Remove ×</button></td></tr>`;
  }).join("")}</tbody></table></div>` : '<p class="help">Add at least two legs from the same frozen game. Lines are entered explicitly; none are invented.</p>';
}

function addSGPLeg(event) {
  event.preventDefault();
  if (!state.sgpForecast) return message("Choose an existing frozen forecast explicitly before adding legs.", true);
  const lineText = $("sgp-line").value;
  const side = $("sgp-side").value;
  const line = Number(lineText);
  const playerId = $("sgp-player").value;
  if (!playerId || lineText === "" || !Number.isFinite(line) || line < 0) return message("Choose a player and enter a nonnegative market line.", true);
  if (side === "at_least" ? !Number.isInteger(line) : !Number.isInteger(line * 2) || Number.isInteger(line)) return message("Over and under require a half-line (such as x.5). At least requires an integer threshold.", true);
  const leg = { player_id: playerId, stat: $("sgp-stat").value, side, line };
  if (state.sgpLegs.some((existing) => JSON.stringify(existing) === JSON.stringify(leg))) return message("This exact leg is already in the builder.", true);
  if (state.sgpLegs.length >= 12) return message("The builder supports at most 12 legs.", true);
  state.sgpLegs.push(leg);
  $("sgp-results").innerHTML = "";
  renderSGPLegs();
  message("");
}

async function evaluateSGP() {
  if (!state.sgpForecast || !$("sgp-forecast").value) return message("Choose an existing frozen forecast explicitly.", true);
  if (state.sgpLegs.length < 2) return message("Add at least two same-game legs before evaluating.", true);
  let quote = null;
  const quoteText = $("sgp-quote").value.trim();
  if (quoteText) {
    try {
      quote = JSON.parse(quoteText);
      if (!quote || Array.isArray(quote) || typeof quote !== "object") throw new Error("Quote must be a JSON object");
    } catch (error) { return message(`Invalid quote JSON: ${error.message}`, true); }
  }
  $("sgp-evaluate").disabled = true;
  const forecastId = state.sgpForecast.id;
  const legsSnapshot = JSON.stringify(state.sgpLegs);
  try {
    const result = await api("/api/sgp/evaluate", { method: "POST", body: JSON.stringify({ forecast_id: forecastId, legs: state.sgpLegs, quote }) });
    if (forecastId !== state.sgpForecast?.id || legsSnapshot !== JSON.stringify(state.sgpLegs)) return;
    renderSGPResult(result);
    message("Joint probability evaluated from shared game samples. Research only; no actionable betting signal is approved.");
  } catch (error) { message(error.message, true); }
  finally { $("sgp-evaluate").disabled = false; }
}

function renderSGPResult(result) {
  const dependence = result.dependence || {};
  const weakest = result.weakest_leg;
  const quote = result.quote;
  const hasPrice = result.quote_status === "VALID" && quote && !result.synthetic;
  const interval95 = result.interval_95 || [];
  $("sgp-results").innerHTML = `<div class="source-banner synthetic"><span class="status-mark"></span><div><strong>NO ACTIONABLE SIGNAL</strong><span>${result.synthetic ? "SYNTHETIC DATA BLOCK · This is a fictional software demonstration." : "Uncalibrated model · verified current availability and independent validation required."}</span></div><span class="badge warning">RESEARCH ONLY</span></div><div class="kpi-grid"><div class="kpi teal"><div class="kpi-label">JOINT MODEL PROBABILITY</div><div class="kpi-value">${pct(result.joint_probability)}</div><div class="kpi-sub">95% Monte Carlo interval ${pct(interval95[0])}–${pct(interval95[1])}</div></div><div class="kpi"><div class="kpi-label">WEAKEST LEG</div><div class="kpi-value">${pct(weakest?.probability)}</div><div class="kpi-sub">${escapeHTML(weakest?.player_name || "Unavailable")} · ${escapeHTML(weakest?.stat || "")}</div></div><div class="kpi"><div class="kpi-label">DEPENDENCE LIFT</div><div class="kpi-value">${num(dependence.lift_vs_independence, 2)}${typeof dependence.lift_vs_independence === "number" ? "×" : ""}</div><div class="kpi-sub">Joint / independence diagnostic</div></div><div class="kpi"><div class="kpi-label">EXACT COMBINED PRICE</div><div class="kpi-value">${hasPrice ? num(quote.decimal_odds, 3) : "UNAVAILABLE"}</div><div class="kpi-sub">${hasPrice ? `${escapeHTML(quote.sportsbook)} · decimal odds` : escapeHTML(result.synthetic ? "Blocked for synthetic inputs" : result.quote_status || "MISSING")}</div></div></div><section class="panel"><div class="panel-heading"><div><span class="eyebrow">CORRELATION-AWARE TICKET</span><h2>Every leg, in the same game</h2></div><span class="badge muted">${escapeHTML(result.sample_count)} SHARED SAMPLES</span></div><div class="data-table-wrap"><table class="data-table"><thead><tr><th>PLAYER</th><th>STATISTIC</th><th>CONDITION</th><th>MODEL PROB.</th><th>95% MC INTERVAL</th></tr></thead><tbody>${(result.legs || []).map((leg) => `<tr><td>${escapeHTML(leg.player_name || leg.player_id)}</td><td>${escapeHTML(SGP_STATS[leg.stat] || leg.stat)}</td><td>${escapeHTML(leg.side.replaceAll("_", " "))} ${num(leg.line, leg.side === "at_least" ? 0 : 1)}</td><td class="stat-highlight">${pct(leg.probability)}</td><td>${pct(leg.interval_95?.[0])}–${pct(leg.interval_95?.[1])}</td></tr>`).join("")}</tbody></table></div><div class="panel-note">Independence product ${pct(dependence.independence_product_diagnostic)} is a diagnostic, never the SGP estimate. Shared-sample joint probability accounts for simulated dependence. Monte Carlo intervals do not include model error.</div><details class="advanced-controls"><summary>Exact leg keys for quote matching <span>+</span></summary><div class="hash">${escapeHTML(JSON.stringify((result.legs || []).map((leg) => leg.key), null, 2))}</div></details></section><div class="two-columns equal-columns"><section class="panel"><div class="panel-heading"><div><span class="eyebrow">PRICE + VALUE</span><h2>Measure against an exact quote</h2></div><span class="badge muted">${escapeHTML(result.pricing_status || "NOT_MEASURABLE")}</span></div><ul class="compact-list"><li><span>Quote status</span><span>${escapeHTML(result.quote_status)}</span></li><li><span>Break-even probability</span><span>${hasPrice ? pct(result.break_even_probability) : "NOT MEASURABLE"}</span></li><li><span>Illustrative expected return / unit</span><span>${hasPrice && typeof result.expected_return_per_unit === "number" ? `${num(result.expected_return_per_unit * 100, 2)}%` : "NOT MEASURABLE"}</span></li><li><span>Scenario robustness</span><span>${escapeHTML(result.scenario_robustness?.status || "UNAVAILABLE")}</span></li></ul>${quote ? `<p class="help">Quote ${escapeHTML(quote.quote_id)} · ${escapeHTML(quote.sportsbook)} · Observed ${escapeHTML(shortDate(quote.observed_at))}. Provider/source assertions require independent verification.</p>` : ""}<ul class="warning-list">${(result.quote_reasons || []).map((reason) => `<li>${escapeHTML(reason)}</li>`).join("")}</ul></section><section class="panel"><div class="panel-heading"><div><span class="eyebrow">BETTING READINESS</span><h2>Why this remains research</h2></div><span class="badge warning">NO ACTIONABLE SIGNAL</span></div><ul class="warning-list">${(result.betting_block_reasons || []).map((reason) => `<li>${escapeHTML(reason)}</li>`).join("")}</ul><p class="help">${escapeHTML(result.scenario_robustness?.reason || "Availability and minutes scenarios have not been independently validated.")}</p></section></div><section class="panel"><div class="panel-heading"><div><span class="eyebrow">FROZEN FORECAST PROVENANCE</span><h2>Inputs behind the ticket</h2></div></div><ul class="compact-list"><li><span>Forecast</span><span>${escapeHTML(result.forecast_id)}</span></li><li><span>Model</span><span>${escapeHTML(result.provenance?.model_version)}</span></li><li><span>Information cutoff</span><span>${escapeHTML(shortDate(result.provenance?.cutoff))}</span></li><li><span>Random seed</span><span>${escapeHTML(result.provenance?.seed)}</span></li></ul><div class="hash">${escapeHTML(result.provenance?.feature_hash || result.provenance?.input_hash || "Feature hash unavailable")}</div><ul class="warning-list">${(result.limitations || []).map((reason) => `<li>${escapeHTML(reason)}</li>`).join("")}</ul></section>`;
}

function renderScenarios() {
  $("scenario-list").innerHTML = state.scenarios.map((s, i) => {
    const player = (state.dataset?.players || []).find((p) => p.id === s.player_id);
    return `<span class="scenario-chip">${escapeHTML(player?.name || s.player_id)} · ${s.active_probability === undefined ? "Active: source" : `Active ${pct(s.active_probability, 0)}`} · ${s.minutes_cap === undefined ? "Cap: source" : `${num(s.minutes_cap)} min cap`}<button type="button" data-remove-scenario="${i}" aria-label="Remove scenario for ${escapeHTML(player?.name || s.player_id)}">×</button></span>`;
  }).join("");
}

function addScenario() {
  const id = $("scenario-player").value;
  const active = $("scenario-active").value;
  const cap = $("scenario-cap").value;
  if (!id || (active === "" && cap === "")) return message("Choose a player and supply an active probability or a minutes cap.", true);
  if (!$("scenario-active").checkValidity() || !$("scenario-cap").checkValidity()) return message("Use an active probability from 0 to 1 and a minutes cap from 0 to 48.", true);
  const scenario = { player_id: id };
  if (active !== "") scenario.active_probability = Number(active);
  if (cap !== "") scenario.minutes_cap = Number(cap);
  state.scenarios = state.scenarios.filter((s) => s.player_id !== id).concat(scenario);
  renderScenarios();
  message("");
}

function histogram(values, label, color = "#78e3cf") {
  const data = values.filter((v) => typeof v === "number" && Number.isFinite(v));
  if (!data.length) return '<div class="empty-panel">No simulation samples available.</div>';
  const min = Math.floor(Math.min(...data));
  const max = Math.max(min + 1, Math.ceil(Math.max(...data)));
  const bins = Array.from({ length: Math.min(25, max - min + 1) }, () => 0);
  const binWidth = (max - min + 1) / bins.length;
  data.forEach((v) => { bins[Math.min(bins.length - 1, Math.floor((v - min) / binWidth))]++; });
  const peak = Math.max(...bins, 1);
  const w = 560, h = 185, left = 24, right = 12, bottom = 28, top = 12;
  const step = (w - left - right) / bins.length;
  const chartHeight = h - top - bottom;
  let svg = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${escapeHTML(label)} histogram from ${data.length} simulations"><title>${escapeHTML(label)} distribution; ${data.length} samples, range ${min} to ${max}</title>`;
  for (const fraction of [0, .5, 1]) {
    const y = top + chartHeight * fraction;
    svg += `<line x1="${left}" y1="${y}" x2="${w - right}" y2="${y}" stroke="#24303a" stroke-dasharray="3 5"/>`;
  }
  bins.forEach((count, i) => {
    const height = count / peak * chartHeight;
    svg += `<rect x="${left + i * step + 1}" y="${top + chartHeight - height}" width="${Math.max(1, step - 3)}" height="${height}" rx="2" fill="${color}" opacity="${.4 + count / peak * .5}"><title>${num(min + i * binWidth, 0)}–${num(min + (i + 1) * binWidth, 0)}: ${count} simulations</title></rect>`;
  });
  for (const fraction of [0, .25, .5, .75, 1]) {
    svg += `<text x="${left + fraction * (w - left - right)}" y="${h - 6}" text-anchor="${fraction === 0 ? "start" : fraction === 1 ? "end" : "middle"}" fill="#81929f" font-size="10" font-family="Consolas,monospace">${Math.round(min + fraction * (max - min))}</text>`;
  }
  return svg + "</svg>";
}

function boxTable(players, full = false) {
  const stats = full ? ["pts", "reb", "ast", "fg3m", "stl", "blk", "tov", "minutes"] : ["pts", "reb", "ast", "fg3m", "minutes"];
  const labels = { pts: "PTS", reb: "REB", ast: "AST", fg3m: "3PM", stl: "STL", blk: "BLK", tov: "TOV", minutes: "MIN" };
  return `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>PLAYER</th>${stats.map((s) => `<th>${labels[s]}</th>`).join("")}${full ? "<th>PTS 80% INTERVAL</th>" : ""}</tr></thead><tbody>${players.map((p) => `<tr><td><button class="text-link" data-player-detail="${escapeHTML(p.id)}">${escapeHTML(p.name)}</button><div class="player-meta">${escapeHTML(p.position)} · ${escapeHTML(teamName(p.team_id))}</div></td>${stats.map((s) => `<td class="${s === "pts" ? "stat-highlight" : ""}">${num(p.stats?.[s]?.mean)}</td>`).join("")}${full ? `<td>${interval(p.stats?.pts)}</td>` : ""}</tr>`).join("")}</tbody></table></div>`;
}

function teamName(id) {
  return state.forecast?.model?.teams?.find((t) => t.id === id)?.name || state.dataset?.teams?.find((t) => t.id === id)?.name || id;
}

function unavailable(title, body) {
  return `<div class="unavailable-card"><span class="unavailable-icon">⊘</span><div><h3>${escapeHTML(title)}</h3><span class="badge muted">UNAVAILABLE</span><p>${escapeHTML(body)}</p></div></div>`;
}

function renderForecast() {
  const f = state.forecast, r = f.result, m = f.model, req = f.request;
  const home = teamName(req.home_id), away = teamName(req.away_id);
  const players = [...r.players].sort((a, b) => (b.stats?.pts?.mean || 0) - (a.stats?.pts?.mean || 0));
  $("forecast-empty").hidden = true;
  $("forecast-content").hidden = false;
  $("forecast-content").innerHTML = `
    <div class="section-intro"><div><span class="eyebrow">FROZEN FORECAST / ${escapeHTML(f.id.slice(0, 12))}</span><p>${escapeHTML(shortDate(req.tipoff))} · ${escapeHTML(req.simulations)} simulations · ${f.synthetic ? "Fictional inputs" : "Imported inputs"}</p></div><a class="button text" href="/api/forecasts/${encodeURIComponent(f.id)}/export" download>Export frozen JSON ↗</a></div>
    <div class="kpi-grid"><div class="kpi teal"><div class="kpi-label">HOME WIN PROBABILITY <span>↗</span></div><div class="kpi-value">${pct(r.home_win_probability)}</div><div class="kpi-sub">Monte Carlo SE ${pct(r.monte_carlo_standard_error)}</div></div><div class="kpi"><div class="kpi-label">EXPECTED MARGIN</div><div class="kpi-value">${r.margin.mean > 0 ? "+" : ""}${num(r.margin.mean)}</div><div class="kpi-sub">Home − away · 80% ${interval(r.margin)}</div></div><div class="kpi"><div class="kpi-label">EXPECTED TOTAL</div><div class="kpi-value">${num(r.total.mean)}</div><div class="kpi-sub">80% interval ${interval(r.total)}</div></div><div class="kpi"><div class="kpi-label">POSSESSIONS / TEAM</div><div class="kpi-value">${num((r.mean_possessions?.[0] + r.mean_possessions?.[1]) / 2)}</div><div class="kpi-sub">Overtime chance ${pct(r.overtime_probability)}</div></div></div>
    <div class="two-columns equal-columns"><section class="panel"><div class="panel-heading"><div><span class="eyebrow">01 / GAME OUTCOME</span><h2>From events to a final score</h2></div><span class="badge ${f.synthetic ? "warning" : "muted"}">${f.synthetic ? "SYNTHETIC DEMO" : "BASELINE ESTIMATE"}</span></div><div class="matchup"><div class="team-side"><div class="team-abbr">${escapeHTML(abbr(home))}</div><div class="team-label">${escapeHTML(home)}</div><div class="matchup-score">${num(r.home_score.mean)}<small>Home · expected points</small></div></div><div class="matchup-vs">VS</div><div class="team-side away"><div class="team-abbr">${escapeHTML(abbr(away))}</div><div class="team-label">${escapeHTML(away)}</div><div class="matchup-score">${num(r.away_score.mean)}<small>Away · expected points</small></div></div></div><div class="probability-bar"><span style="width:${bounded(r.home_win_probability * 100, 0, 100)}%"></span></div><div class="probability-labels"><span>${pct(r.home_win_probability)} HOME</span><span>${pct(r.away_win_probability)} AWAY</span></div><div class="panel-note">Scores are simulation means. Sample size reduces simulation noise; it does not establish model accuracy.</div></section><section class="panel"><div class="panel-heading"><div><span class="eyebrow">03 / PREDICTION DISTRIBUTIONS</span><h2>The range, not just the point</h2></div><span>Home − away</span></div><div class="chart-wrap">${histogram(r.score_samples.map((s) => s[0] - s[1]), "Final score margin")}</div><div class="chart-caption"><span class="legend">Simulated margin</span><span>Middle 80%: ${interval(r.margin)} points</span></div><div class="panel-note">Mean ${num(r.margin.mean)} · Median ${num(r.margin.median)} · Middle 90% ${num(r.margin.p05, 0)}–${num(r.margin.p95, 0)}</div></section></div>
    <section class="panel"><div class="panel-heading"><div><span class="eyebrow">02 / PLAYER BOX SCORES</span><h2>Every point belongs to a player</h2></div><button class="button text" data-switch-tab="players">All players & distributions →</button></div>${boxTable(players.slice(0, 8))}<div class="panel-note">Means across all simulations, including inactive outcomes. Select a player for intervals and threshold probabilities.</div></section>
    <div class="two-columns equal-columns"><section class="panel"><div class="panel-heading"><div><span class="eyebrow">08 / MODEL EXPLANATION</span><h2>What shaped this run</h2></div><span class="badge muted">EXPERIMENTAL</span></div><ul class="compact-list"><li><span>Historical games before cutoff</span><span>${escapeHTML(m.games_used)}</span></li><li><span>Last eligible completed game</span><span>${escapeHTML(shortDate(m.last_game_at))}</span></li><li><span>Information cutoff</span><span>${escapeHTML(shortDate(req.cutoff))}</span></li><li><span>Scenario overrides</span><span>${req.scenarios?.length || 0}</span></li><li><span>Availability draws rejected for infeasible minutes</span><span>${escapeHTML(r.availability_rejections)}</span></li></ul><p class="help">Availability is conditional on a feasible five-player rotation. The baseline uses empirical shrinkage and explicit priors; no validated causal attribution is available.</p></section><section class="panel"><div class="panel-heading"><div><span class="eyebrow">04 + 09 / COVERAGE BOUNDARIES</span><h2>Know what is missing</h2></div></div>${unavailable("Defender matchup & live state", "No licensed defender assignments or live in-game feed are connected. Forecasts are pregame simulations; tactical coverage and live win probability are not measured.")}</section></div>
    <section class="panel"><div class="panel-heading"><div><span class="eyebrow">SOURCE + MODEL AUDIT</span><h2>Inspect the assumptions</h2></div><button class="button text" data-switch-tab="audit">Open forecast audit →</button></div><ul class="warning-list">${(m.warnings || []).map((w) => `<li>${escapeHTML(w)}</li>`).join("")}</ul><div class="hash">Feature hash · ${escapeHTML(m.feature_hash)}</div></section>`;
  renderPlayers();
}

function renderPlayers(selectedId) {
  const f = state.forecast;
  if (!f) return;
  const r = f.result;
  const selected = r.players.find((p) => p.id === selectedId) || [...r.players].sort((a, b) => b.stats.pts.mean - a.stats.pts.mean)[0];
  const teams = f.model.teams;
  $("players-content").className = "";
  $("players-content").innerHTML = `<section class="panel"><div class="panel-heading"><div><span class="eyebrow">02 / JOINT PLAYER OUTPUTS</span><h2>Projected box scores</h2></div><span>Means · inactive outcomes included</span></div>${boxTable(r.players, true)}</section><div class="two-columns equal-columns"><section class="panel"><div class="panel-heading"><div><span class="eyebrow">03 / THRESHOLD PROBABILITIES</span><h2>${escapeHTML(selected?.name)}</h2></div><span class="badge muted">SIMULATED</span></div><div class="distribution-controls"><label>Player<select id="distribution-player">${r.players.map((p) => option(p.id, p.name)).join("")}</select></label><label>Statistic<select id="distribution-stat"><option value="pts">Points</option><option value="reb">Rebounds</option><option value="ast">Assists</option><option value="fg3m">Made threes</option><option value="minutes">Minutes</option></select></label></div><div id="player-histogram" class="chart-wrap"></div><div id="player-interval" class="chart-caption"></div><ul class="compact-list">${Object.entries(selected?.thresholds || {}).map(([k, v]) => `<li><span>${escapeHTML(({ pts_ge_20: "20+ points", pts_ge_30: "30+ points", reb_ge_10: "10+ rebounds", ast_ge_10: "10+ assists", fg3m_ge_3: "3+ made threes" })[k] || k)}</span><span>${pct(v)}</span></li>`).join("")}</ul></section><section class="panel"><div class="panel-heading"><div><span class="eyebrow">07 / CROSS-STAT CORRELATIONS</span><h2>A connected game</h2></div><span>Pearson correlation</span></div>${correlationGrid(r.correlations)}<p class="help">Four game outputs from the same simulation draws. Full player covariance and correlation matrices are included in the frozen JSON export.</p></section></div><div class="roster-grid">${teams.map((t) => `<section class="panel"><div class="panel-heading"><div><span class="eyebrow">05 + 06 / ROTATION + AVAILABILITY</span><h2>${escapeHTML(t.name)}</h2></div><span>Mean minutes</span></div>${r.players.filter((p) => p.team_id === t.id).sort((a, b) => b.stats.minutes.mean - a.stats.minutes.mean).map((p) => `<div class="rotation-bar"><span class="rotation-name">${escapeHTML(p.name)}</span><span class="rotation-track"><span style="width:${bounded(p.stats.minutes.mean / 48 * 100, 0, 100)}%"></span></span><span class="rotation-number">${num(p.stats.minutes.mean)}</span></div>`).join("")}<div class="panel-note">Five players on court. Regulation totals 240 team minutes; overtime adds 25. Availability and minute restrictions come from the frozen rotation snapshots and scenario overrides.</div></section>`).join("")}</div><section class="panel"><div class="panel-heading"><div><span class="eyebrow">06 / SCENARIO INPUTS</span><h2>Availability & restrictions</h2></div></div><div class="data-table-wrap"><table class="data-table"><thead><tr><th>PLAYER</th><th>ACTIVE PROB.</th><th>INPUT MINUTES</th><th>MINUTES CAP</th><th>KNOWN AT</th></tr></thead><tbody>${teams.flatMap((t) => t.players).map((p) => `<tr><td>${escapeHTML(p.name)}</td><td>${pct(p.active_probability, 0)}</td><td>${num(p.expected_minutes)}</td><td>${num(p.minutes_cap)}</td><td>${escapeHTML(shortDate(p.known_at))}</td></tr>`).join("")}</tbody></table></div></section><section class="panel"><div class="panel-heading"><div><span class="eyebrow">REPRESENTATIVE SIMULATION / EVENT LEDGER</span><h2>One game behind the distribution</h2></div><span>Sample 1 · ${escapeHTML(r.representative_game?.events?.length || 0)} events</span></div><p class="help">This is one simulated game, not an observed NBA game. The full ledger and lineup stints are available in the export.</p><div class="event-view">${(r.representative_game?.events || []).slice(0, 100).map((e) => { const p = r.players.find((x) => x.id === e.player_id); const seconds = Math.max(0, e.clock_seconds || 0); return `<div class="event-row"><span class="event-time">P${escapeHTML(e.period)} ${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}</span><span>${escapeHTML(p?.name || e.player_id)} · ${escapeHTML(e.type?.replaceAll("_", " "))}${e.assist_id ? " · assisted" : ""}${e.rebound_id ? " · rebound resolved" : ""}</span><span>${e.points ? `+${escapeHTML(e.points)}` : "—"}</span></div>`; }).join("")}</div></section>`;
  $("distribution-player").value = selected?.id || "";
  $("distribution-player").addEventListener("change", (event) => renderPlayers(event.target.value));
  $("distribution-stat").addEventListener("change", renderPlayerHistogram);
  renderPlayerHistogram();
}

function renderHistoricalPlayers(profiles) {
  if (state.forecast) return;
  const players = profiles.players || [];
  $("players-content").className = "";
  const labels = ["Points / game", "Minutes / game", "True shooting %", "3PT FG%", "Assist-to-turnover ratio", "Games played"];
  $("players-content").innerHTML = `<section class="panel"><div class="panel-heading"><div><span class="eyebrow">OBSERVED HISTORY / PLAYER PROFILES</span><h2>Historical player evidence</h2></div><span class="badge ${state.dataset?.synthetic ? "warning" : "muted"}">${state.dataset?.synthetic ? "FICTIONAL INPUTS" : "SOURCE-BACKED HISTORY"}</span></div><p class="help">These are descriptive aggregates before the selected cutoff, not next-game forecasts. Current roster membership and injury eligibility are unavailable without verified rotation snapshots.</p><div class="data-table-wrap" style="margin-top:24px"><table class="data-table"><thead><tr><th>PLAYER</th><th>PTS / GAME</th><th>MIN / GAME</th><th>TS%</th><th>3PT%</th><th>AST / TOV</th><th>GAMES</th></tr></thead><tbody>${players.map((player) => { const metrics = metricArray(player.metrics); const values = labels.map((label) => metrics.find((metric) => metric.label === label)?.value); return `<tr><td><button class="text-link" data-historical-player="${escapeHTML(player.id)}">${escapeHTML(player.name)}</button><div class="player-meta">${escapeHTML(player.position || "Position unavailable")} · ${player.team_id ? escapeHTML(teamName(player.team_id)) : "Current team unverified"}</div></td>${values.map((value, index) => `<td class="${index === 0 ? "stat-highlight" : ""}">${num(value, index === 5 ? 0 : 1)}</td>`).join("")}</tr>`; }).join("")}</tbody></table></div><div class="panel-note">Select a player for metric definitions, sample coverage, freshness and provenance. Missing metrics stay unavailable.</div></section>`;
}

function renderPlayerHistogram() {
  const player = state.forecast?.result.players.find((p) => p.id === $("distribution-player")?.value);
  const stat = $("distribution-stat")?.value || "pts";
  if (!player) return;
  $("player-histogram").innerHTML = histogram(player.samples?.[stat] || [], `${player.name} ${stat}`);
  $("player-interval").innerHTML = `<span>Mean ${num(player.stats[stat]?.mean)}</span><span>Middle 80% ${interval(player.stats[stat])}</span>`;
}

function correlationGrid(correlations) {
  if (!correlations?.matrix?.length) return '<div class="empty-panel">No correlation matrix available.</div>';
  const labels = ["Home score", "Away score", "Margin", "Total"];
  return `<div class="correlation-grid" style="grid-template-columns:repeat(4,1fr)">${correlations.matrix.slice(0, 4).map((row, i) => row.slice(0, 4).map((value, j) => `<div class="correlation-cell" title="${labels[i]} / ${labels[j]}: ${num(value, 3)}" style="background:${value >= 0 ? `rgba(67,159,137,${.15 + bounded(value, 0, 1) * .6})` : `rgba(190,123,75,${.15 + bounded(-value, 0, 1) * .6})`}">${num(value, 2)}</div>`).join("")).join("")}</div><div class="correlation-axes">Rows & columns: home score / away score / margin / total</div>`;
}

function updateMetricEntities() {
  const scope = $("metric-scope").value;
  const entities = scope === "team" ? state.dataset?.teams || [] : (state.dataset?.players || []).filter((p) => scope === "player" || p.position === scope);
  $("metric-entity").innerHTML = option("", "Definitions only") + entities.map((p) => option(p.id, p.name)).join("");
  if (entities.length) $("metric-entity").value = entities[0].id;
}

async function loadMetrics() {
  const requestId = ++state.metricRequest;
  const params = new URLSearchParams({ scope: $("metric-scope").value });
  if (state.dataset) params.set("dataset_id", state.dataset.id);
  if ($("metric-entity").value) params.set("entity_id", $("metric-entity").value);
  if (!$("cutoff").value || !Number.isFinite(Date.parse($("cutoff").value + "Z"))) {
    $("metric-content").innerHTML = '<div class="empty-panel">Set a valid information cutoff in the timing controls.</div>';
    return;
  }
  params.set("cutoff", new Date($("cutoff").value + "Z").toISOString());
  $("metric-content").innerHTML = '<p class="help loading">Loading metric definitions…</p>';
  try {
    const result = await api(`/api/metrics?${params}`);
    if (requestId !== state.metricRequest) return;
    state.metrics = result.metrics || [];
    $("metric-coverage").textContent = `${result.coverage?.available ?? state.metrics.filter((m) => m.value !== null && m.value !== undefined).length} / ${result.coverage?.total ?? state.metrics.length} available`;
    renderMetrics();
  } catch (error) { if (requestId === state.metricRequest) $("metric-content").innerHTML = `<div class="empty-panel">${escapeHTML(error.message)}</div>`; }
}

function renderMetrics() {
  const query = $("metric-search").value.trim().toLowerCase();
  const showUnavailable = $("metric-show-unavailable").checked;
  const metrics = state.metrics.filter((m) => (showUnavailable || (m.value !== null && m.value !== undefined)) && `${m.label} ${m.category} ${m.required_source}`.toLowerCase().includes(query)).sort((a, b) => Number(a.value === null || a.value === undefined) - Number(b.value === null || b.value === undefined));
  const groups = new Map();
  metrics.forEach((metric) => { const key = metric.category || "Metrics"; if (!groups.has(key)) groups.set(key, []); groups.get(key).push(metric); });
  $("metric-content").innerHTML = [...groups].map(([category, items]) => `<div class="metric-category">${escapeHTML(category.toUpperCase())}</div><div class="data-table-wrap"><table class="data-table metric-table"><thead><tr><th>METRIC / DEFINITION</th><th>VALUE</th><th>STATUS</th><th>SOURCE + COVERAGE</th></tr></thead><tbody>${items.map((m) => `<tr><td>${escapeHTML(m.label)}<div class="metric-definition">${escapeHTML(m.formula || m.formula_basis || m.implementation || m.source_reference || "")}</div></td><td class="metric-value">${typeof m.value === "number" ? num(m.value, 3) : m.value === null || m.value === undefined ? "—" : escapeHTML(m.value)}</td><td class="metric-status ${m.value === null || m.value === undefined ? "unavailable" : ""}">${escapeHTML(m.status || "UNAVAILABLE")}</td><td>${escapeHTML(m.source || m.required_source?.replaceAll("_", " ") || "Unavailable")}${m.source ? `<div class="metric-definition">${escapeHTML(m.sample_games ?? 0)} games · ${escapeHTML(shortDate(m.known_at))}</div>` : ""}${m.uncertainty ? `<div class="metric-definition">${escapeHTML(m.uncertainty)}</div>` : ""}</td></tr>`).join("")}</tbody></table></div>`).join("") || '<div class="empty-panel">No metrics match this search.</div>';
}

function cohortEvaluation(cohorts) {
  return Object.entries(cohorts || {}).map(([name, cohort]) => {
    const scalarMetrics = Object.entries(cohort.metrics || {}).filter(([, value]) => typeof value === "number");
    const gameMetrics = Object.entries(cohort.metrics || {}).filter(([, value]) => value && typeof value === "object");
    const players = Object.entries(cohort.players || {});
    return `<div class="metric-category">${escapeHTML(name.toUpperCase())} COHORT · ${escapeHTML(cohort.count)} FORECASTS · ${escapeHTML(cohort.distinct_games)} DISTINCT GAMES</div><ul class="compact-list">${scalarMetrics.map(([key, value]) => `<li><span>${escapeHTML(key.replaceAll("_", " "))}</span><span>${num(value, 4)}</span></li>`).join("")}</ul>${gameMetrics.length ? `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>GAME OUTPUT</th><th>MAE</th><th>RMSE</th><th>80% COVERAGE</th><th>CRPS</th></tr></thead><tbody>${gameMetrics.map(([key, value]) => `<tr><td>${escapeHTML(key.replaceAll("_", " "))}</td><td>${num(value.mae, 3)}</td><td>${num(value.rmse, 3)}</td><td>${pct(value.coverage_80)}</td><td>${num(value.crps, 3)}</td></tr>`).join("")}</tbody></table></div>` : ""}${players.length ? `<details class="advanced-controls"><summary>Player evaluation by statistic <span>+</span></summary><div class="data-table-wrap"><table class="data-table"><thead><tr><th>STATISTIC</th><th>MAE</th><th>80% COVERAGE</th><th>CRPS</th></tr></thead><tbody>${players.map(([key, value]) => `<tr><td>${escapeHTML(key.toUpperCase())}</td><td>${num(value.absolute_error, 3)}</td><td>${pct(value.coverage_80)}</td><td>${num(value.crps, 3)}</td></tr>`).join("")}</tbody></table></div></details>` : ""}<p class="help">${name === "synthetic" ? "Self-generated synthetic outcomes are a software check; they cannot establish predictive skill." : name === "retrospective" ? "Historical replay uses supplied source timestamps. It does not demonstrate a forecast was made before the game." : "Prospective timing classification is based on recorded timestamps; independent outcome and source verification are still required."} Repeated scenarios for one game are not independent observations.</p>`;
  }).join("");
}

async function loadProfiles() {
  const requestId = ++state.profileRequest;
  if (!state.dataset) { $("teams-content").className = "empty-panel"; $("teams-content").textContent = "Select a dataset to inspect source-backed team profiles."; return; }
  if (!$("cutoff").value || !Number.isFinite(Date.parse($("cutoff").value + "Z"))) {
    $("teams-content").className = "empty-panel";
    $("teams-content").textContent = "Set a valid information cutoff in the timing controls.";
    return;
  }
  const datasetId = state.dataset.id;
  const params = new URLSearchParams({ dataset_id: datasetId, cutoff: new Date($("cutoff").value + "Z").toISOString() });
  try {
    const profiles = await api(`/api/profiles?${params}`);
    if (state.dataset?.id !== datasetId || requestId !== state.profileRequest) return;
    state.profiles = profiles;
    renderHistoricalPlayers(profiles);
    $("teams-content").className = "";
    $("teams-content").innerHTML = `<div class="section-intro"><div><span class="eyebrow">SOURCE-BACKED INTELLIGENCE</span><p>Eligible history before the selected information cutoff. Missing values remain unavailable.</p></div><span class="badge ${state.dataset.synthetic ? "warning" : "muted"}">${state.dataset.synthetic ? "FICTIONAL INPUTS" : "IMPORTED INPUTS"}</span></div><div class="profile-grid">${(profiles.teams || []).map((team) => { const metrics = metricArray(team.metrics).filter((m) => m.value !== undefined && m.value !== null); return `<section class="panel"><div class="profile-title"><div class="team-abbr">${escapeHTML(abbr(team.name))}</div><div><h2>${escapeHTML(team.name)}</h2><p>${escapeHTML(team.id)} · ${metrics.length} available metrics</p></div></div><ul class="compact-list">${metrics.slice(0, 12).map((m) => `<li><span>${escapeHTML(m.label || m.id)}</span><span>${typeof m.value === "number" ? num(m.value, 2) : escapeHTML(m.value)}</span></li>`).join("") || '<li><span>No measured metrics available at this cutoff.</span></li>'}</ul><div class="panel-note">${escapeHTML(team.sample_size !== undefined ? `Sample size: ${team.sample_size}` : "See the metric library for availability and source requirements.")}</div><button class="button text" data-team-metrics="${escapeHTML(team.id)}">Inspect metric coverage →</button></section>`; }).join("")}</div><div class="two-columns equal-columns"><section class="panel"><div class="panel-heading"><div><span class="eyebrow">MATCHUP INTELLIGENCE</span><h2>Tracking coverage</h2></div></div>${unavailable("Schemes & defender assignments", "Drop, switch, blitz, zone, player gravity and individual defensive assignments need tracking or custom event charting. No observed values are inferred from box scores.")}</section><section class="panel"><div class="panel-heading"><div><span class="eyebrow">LINEUP INTELLIGENCE</span><h2>Five-player context</h2></div></div><p class="help">The simulator assigns five players to every rotation stint and constrains team minutes. Historical lineup chemistry, adjusted five-man ratings and learned coaching substitutions require play-by-play reconstruction and independent validation.</p>${state.forecast ? `<button class="button secondary" data-switch-tab="players" style="margin-top:24px">Inspect simulated rotations →</button>` : ""}</section></div>`;
  } catch (error) { if (requestId === state.profileRequest) { $("teams-content").className = "empty-panel"; $("teams-content").textContent = error.message; } }
}

async function loadAudit() {
  try {
    const [historyRows, evaluation] = await Promise.all([api("/api/forecasts"), api("/api/evaluation")]);
    const forecasts = Array.isArray(historyRows) ? historyRows : historyRows.forecasts || [];
    const count = evaluation.forecasts_scored ?? 0;
    const metrics = Object.entries(evaluation.metrics || {}).filter(([, value]) => typeof value === "number");
    $("audit-content").className = "";
    $("audit-content").innerHTML = `<div class="kpi-grid"><div class="kpi"><div class="kpi-label">FROZEN FORECASTS</div><div class="kpi-value">${forecasts.length}</div><div class="kpi-sub">Latest saved runs</div></div><div class="kpi"><div class="kpi-label">SCORED FORECASTS</div><div class="kpi-value">${escapeHTML(count)}</div><div class="kpi-sub">Matched to recorded outcomes</div></div><div class="kpi"><div class="kpi-label">CALIBRATION STATUS</div><div class="kpi-value" style="font-size:16px;letter-spacing:0">${count ? "EXPERIMENTAL" : "NOT MEASURABLE"}</div><div class="kpi-sub">Independent validation required</div></div><div class="kpi"><div class="kpi-label">LIVE DATA</div><div class="kpi-value" style="font-size:16px;letter-spacing:0">UNAVAILABLE</div><div class="kpi-sub">Pregame research engine</div></div></div><section class="panel"><div class="panel-heading"><div><span class="eyebrow">10 / OUTCOME EVALUATION</span><h2>Evidence before confidence</h2></div><span class="badge muted">${escapeHTML(evaluation.status || "NOT MEASURABLE")}</span></div>${metrics.length ? `<ul class="compact-list">${metrics.map(([k, v]) => `<li><span>${escapeHTML(k.replaceAll("_", " "))}</span><span>${num(v, 4)}</span></li>`).join("")}</ul>` : '<p class="help">No scored outcomes available. Accuracy, calibration and interval coverage cannot be measured from simulation output alone.</p>'}<p class="help">Demo forecasts do not establish predictive skill. Retrospective source timestamps are provider assertions; prospective accuracy requires forecasts frozen before tipoff and independent outcomes.</p></section><section class="panel"><div class="panel-heading"><div><span class="eyebrow">FROZEN FORECAST HISTORY</span><h2>Every run is inspectable</h2></div><span>${forecasts.length} saved runs</span></div>${forecasts.length ? forecasts.map((f) => `<div class="audit-row"><div><h3>${escapeHTML(f.request?.home_id)} vs ${escapeHTML(f.request?.away_id)}</h3><p>${escapeHTML(f.id.slice(0, 16))} · ${escapeHTML(shortDate(f.created_at))} · ${escapeHTML(f.request?.simulations)} samples</p></div><span class="badge ${f.synthetic ? "warning" : "muted"}">${f.synthetic ? "SYNTHETIC" : "IMPORTED"}</span><div class="audit-actions"><button class="button text" data-load-forecast="${escapeHTML(f.id)}">Inspect →</button><a class="button text" href="/api/forecasts/${encodeURIComponent(f.id)}/export" download>JSON ↗</a></div></div>`).join("") : '<div class="empty-panel">No frozen forecasts yet. Run a simulation to create the first audit record.</div>'}</section>${state.forecast ? `<section class="panel"><div class="panel-heading"><div><span class="eyebrow">CURRENT FORECAST / MODEL PROVENANCE</span><h2>Reproduce this result</h2></div></div><ul class="compact-list"><li><span>Forecast ID</span><span>${escapeHTML(state.forecast.id)}</span></li><li><span>Model version</span><span>${escapeHTML(state.forecast.model.version)}</span></li><li><span>Random seed</span><span>${escapeHTML(state.forecast.request.seed)}</span></li><li><span>Source retrieved/imported after cutoff</span><span>${state.forecast.model.retrospective_source ? "YES · retrospective source" : "No"}</span></li></ul><div class="hash">${escapeHTML(state.forecast.model.feature_hash)}</div><ul class="warning-list">${(state.forecast.model.warnings || []).map((w) => `<li>${escapeHTML(w)}</li>`).join("")}</ul></section>` : ""}`;
    if (count) {
      const evaluationPanel = $("audit-content").querySelector(".panel");
      if (!metrics.length) evaluationPanel.querySelector("p.help")?.remove();
      evaluationPanel.insertAdjacentHTML("beforeend", cohortEvaluation(evaluation.cohorts));
    }
  } catch (error) { $("audit-content").className = "empty-panel"; $("audit-content").textContent = error.message; }
}

async function inspectForecast(id) {
  try {
    const forecast = await api(`/api/forecasts/${encodeURIComponent(id)}`);
    if (state.status?.datasets.some((d) => d.id === forecast.request.dataset_id)) {
      $("dataset").value = forecast.request.dataset_id;
      chooseDataset();
    }
    state.forecast = forecast;
    $("home-team").value = forecast.request.home_id;
    $("away-team").value = forecast.request.away_id;
    $("cutoff").value = forecast.request.cutoff.replace(/\+00:00$|Z$/, "").slice(0, 19);
    $("tipoff").value = forecast.request.tipoff.replace(/\+00:00$|Z$/, "").slice(0, 19);
    $("simulations").value = forecast.request.simulations;
    $("seed").value = forecast.request.seed;
    state.scenarios = forecast.request.scenarios || [];
    renderScenarios();
    renderForecast();
    tab("overview");
    message("Loaded the immutable saved forecast. Change inputs and run again to create a new record.");
  } catch (error) { message(error.message, true); }
}

document.addEventListener("click", (event) => {
  const nav = event.target.closest("[data-tab], [data-switch-tab]");
  if (nav) tab(nav.dataset.tab || nav.dataset.switchTab);
  const remove = event.target.closest("[data-remove-scenario]");
  if (remove) { state.scenarios.splice(Number(remove.dataset.removeScenario), 1); renderScenarios(); }
  const removeSGP = event.target.closest("[data-remove-sgp-leg]");
  if (removeSGP) { state.sgpLegs.splice(Number(removeSGP.dataset.removeSgpLeg), 1); renderSGPLegs(); $("sgp-results").innerHTML = ""; }
  const player = event.target.closest("[data-player-detail]");
  if (player) { renderPlayers(player.dataset.playerDetail); tab("players"); $("distribution-player")?.scrollIntoView({ block: "center", behavior: "smooth" }); }
  const team = event.target.closest("[data-team-metrics]");
  if (team) { $("metric-scope").value = "team"; updateMetricEntities(); $("metric-entity").value = team.dataset.teamMetrics; tab("metrics"); }
  const historicalPlayer = event.target.closest("[data-historical-player]");
  if (historicalPlayer) { $("metric-scope").value = "player"; updateMetricEntities(); $("metric-entity").value = historicalPlayer.dataset.historicalPlayer; tab("metrics"); }
  const forecast = event.target.closest("[data-load-forecast]");
  if (forecast) inspectForecast(forecast.dataset.loadForecast);
  const tonightGame = event.target.closest("[data-tonight-build]");
  if (tonightGame) buildTonightSGP(tonightGame.dataset.tonightBuild).catch((error) => message(error.message, true));
  const gameDrafts = event.target.closest("[data-game-drafts]");
  if (gameDrafts) generateGameDrafts(gameDrafts.dataset.gameDrafts).catch((error) => message(error.message, true));
  const draft = event.target.closest("[data-draft-builder]");
  if (draft) openDraftBuilder(draft.dataset.draftBuilder, draft.dataset.draftFamily);
  const removeMarket = event.target.closest("[data-remove-market-leg]");
  if (removeMarket) { state.marketLegs.splice(Number(removeMarket.dataset.removeMarketLeg), 1); renderMarketBasket(); }
});

$("dataset").addEventListener("change", chooseDataset);
$("add-scenario").addEventListener("click", addScenario);
$("metric-scope").addEventListener("change", () => { updateMetricEntities(); loadMetrics(); });
$("metric-entity").addEventListener("change", loadMetrics);
$("metric-search").addEventListener("input", renderMetrics);
$("metric-show-unavailable").addEventListener("change", renderMetrics);
$("sgp-forecast").addEventListener("change", chooseSGPForecast);
$("sgp-leg-form").addEventListener("submit", addSGPLeg);
$("sgp-evaluate").addEventListener("click", evaluateSGP);
$("tonight-refresh").addEventListener("click", () => loadTonight(true));
$("tonight-build-all").addEventListener("click", buildAllTonightDrafts);
$("tonight-odds-refresh").addEventListener("click", refreshFanDuelOdds);
$("polymarket-refresh").addEventListener("click", () => loadPolymarket(true));
$("polymarket-type").addEventListener("change", renderPolymarket);
$("polymarket-search").addEventListener("input", renderPolymarket);
$("ml-refresh").addEventListener("click", loadModels);
$("builder-selection-mode").addEventListener("change", () => message(selectionMode() === "aggressive" ? "Aggressive construction selected for future builds: higher source-bound lines require executable quote evidence. The build remains blocked where those quotes are unavailable. Saved drafts retain their recorded mode." : "Core construction selected for future builds. Existing saved selections are unchanged."));
$("open-research-controls").addEventListener("click", () => { $("advanced-research").open = true; $("advanced-research").scrollIntoView({ behavior: "smooth", block: "start" }); });
$("cutoff").addEventListener("change", () => { if (state.tab === "metrics") loadMetrics(); if (state.tab === "teams" || (state.tab === "players" && !state.forecast)) loadProfiles(); });

$("demo-button").addEventListener("click", async () => {
  setBusy(true, "Loading demo…");
  message("");
  try {
    const result = await api("/api/demo", { method: "POST", body: "{}" });
    await loadStatus(result.dataset_id);
    message("Fictional demo loaded. These inputs are synthetic. Choose a matchup and run a simulation.");
  } catch (error) { message(error.message, true); }
  finally { setBusy(false); }
});

$("forecast-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.dataset) return message("Select a dataset or load the fictional demo first.", true);
  if ($("home-team").value === $("away-team").value) return message("Choose two different teams.", true);
  const request = { dataset_id: state.dataset.id, home_id: $("home-team").value, away_id: $("away-team").value,
    cutoff: new Date($("cutoff").value + "Z").toISOString(), tipoff: new Date($("tipoff").value + "Z").toISOString(),
    simulations: Number($("simulations").value), seed: Number($("seed").value), scenarios: state.scenarios };
  setBusy(true);
  message("Simulating event histories and freezing the forecast. Larger runs can take a moment.");
  try {
    state.forecast = await api("/api/forecasts", { method: "POST", body: JSON.stringify(request) });
    renderForecast();
    tab("overview");
    message(`Forecast frozen · ${request.simulations} simulated games · ${state.forecast.synthetic ? "Synthetic inputs; not an NBA forecast." : "Experimental baseline; predictive accuracy is unverified."}`);
  } catch (error) { message(error.message, true); }
  finally { setBusy(false); }
});

const now = new Date();
$("cutoff").value = new Date(now.getTime() - 1000).toISOString().slice(0, 19);
$("tipoff").value = new Date(now.getTime() + 86400000).toISOString().slice(0, 19);
function updateClock() { $("utc-clock").textContent = new Date().toISOString().slice(11, 19) + " UTC"; }
updateClock();
setInterval(updateClock, 1000);
tab(location.hash.slice(1) || "tonight");
loadStatus().catch((error) => message(error.message, true));
loadSources();
