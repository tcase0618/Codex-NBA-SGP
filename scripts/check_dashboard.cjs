"use strict";

// Lightweight DOM assertions only; deliberately no browser automation or layout claim.
// Generate a fresh isolated fixture with: python scripts/check_dashboard.py
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const path = require("node:path");

if (!process.argv[2]) throw new Error("Pass a JSON fixture path, or run python scripts/check_dashboard.py");
const fixture = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const elements = new Map();
const calls = [];

function mockElement(id) {
  const children = new Map();
  const listeners = new Map();
  return {
    id, value: "", innerHTML: "", textContent: "", hidden: false, dataset: {},
    classList: { toggle() {} }, listeners, removed: false, options: [],
    addEventListener(event, callback) { listeners.set(event, callback); },
    setAttribute() {}, checkValidity() { return true; }, scrollIntoView() {},
    remove() { this.removed = true; },
    querySelector(selector) {
      if (!children.has(selector)) children.set(selector, mockElement(`${id}:${selector}`));
      return children.get(selector);
    },
    insertAdjacentHTML(_position, html) { this.innerHTML += html; },
  };
}

function element(id) {
  if (!elements.has(id)) elements.set(id, mockElement(id));
  return elements.get(id);
}

const document = {
  getElementById: element,
  querySelectorAll() { return []; },
  querySelector(selector) { return element(`selector:${selector}`); },
  addEventListener() {},
};
const context = {
  document, location: { hash: "#overview" }, history: { replaceState() {} },
  setInterval() {}, setTimeout(callback) { callback(); }, console, URLSearchParams,
  fetch: async (url, options = {}) => {
    calls.push({ url, options });
    let data;
    if (url === "/api/status") data = fixture.status;
    else if (url === "/api/demo") data = { dataset_id: fixture.status.datasets[0].id };
    else if (url === "/api/forecasts") data = options.method === "POST" ? fixture.forecast : fixture.history;
    else if (url === "/api/evaluation") data = fixture.evaluation;
    else if (url === "/api/sources") data = fixture.sources;
    else if (url === "/api/tonight" || url === "/api/tonight/refresh") data = fixture.tonight;
    else if (url.startsWith("/api/tonight/build")) data = { status: "RUNNING", job_id: "ui-build-job" };
    else if (url === "/api/build-jobs/ui-build-job") data = { status: "COMPLETE", job_id: "ui-build-job", games_built: 1, errors: [] };
    else if (/^\/api\/games\/[^/]+\/drafts(?:\?.*)?$/.test(url)) data = { status: "DRAFT_UNPRICED" };
    else if (url === "/api/markets/import") data = { id: "ui-market-receipt", lines_imported: JSON.parse(options.body).legs.length, authenticity: "CALLER_ASSERTION_UNVERIFIED", actionable: false };
    else if (url === "/api/odds/refresh") data = { status: "AVAILABLE", published_props: [] };
    else if (url === "/api/polymarket" || url === "/api/polymarket/refresh") data = fixture.polymarket || { status: "UNAVAILABLE", markets: [], errors: [] };
    else if (url === "/api/sgp/evaluate") data = fixture.sgp;
    else if (url.startsWith("/api/metrics")) data = fixture.metrics;
    else if (url.startsWith("/api/profiles")) data = fixture.profiles;
    else if (url.startsWith("/api/forecasts/")) data = fixture.forecast;
    else throw new Error(`Unexpected API call: ${url}`);
    return { ok: true, json: async () => data };
  },
};

vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, "../nba_quant/static/app.js"), "utf8"), context);
const evaluate = (code) => vm.runInContext(code, context);

async function main() {
  // Let the app's startup status request settle before testing render functions.
  await new Promise((resolve) => setImmediate(resolve));
  evaluate(`state.status=${JSON.stringify(fixture.status)}; state.dataset=state.status.datasets[0]; state.forecast=${JSON.stringify(fixture.forecast)}; renderForecast(); renderSource();`);
  assert(element("forecast-content").innerHTML.includes("From events to a final score"));
  assert(element("forecast-content").innerHTML.includes("SYNTHETIC DEMO"));
  assert(element("source-banner").innerHTML.includes("FICTIONAL DATA"));
  assert(element("players-content").innerHTML.includes("Harbor Player"));
  assert(element("player-histogram").innerHTML.includes("<svg"));
  assert(element("players-content").innerHTML.includes("Availability & restrictions"));

  evaluate(`state.metrics=${JSON.stringify(fixture.metrics.metrics)}; renderMetrics();`);
  assert(element("metric-content").innerHTML.includes("SYNTHETIC_DERIVED"));
  assert(!element("metric-content").innerHTML.includes("UNAVAILABLE"));
  element("metric-show-unavailable").checked = true;
  evaluate("renderMetrics()");
  assert(element("metric-content").innerHTML.includes("UNAVAILABLE"));
  assert(evaluate('escapeHTML("<img src=x onerror=alert(1)>")').startsWith("&lt;img"));

  const malicious = structuredClone(fixture.forecast);
  malicious.result.players[0].name = "<script>alert(1)</script>";
  evaluate(`state.forecast=${JSON.stringify(malicious)}; renderForecast();`);
  assert(!element("players-content").innerHTML.includes("<script>alert(1)</script>"));
  assert(element("players-content").innerHTML.includes("&lt;script&gt;alert(1)&lt;/script&gt;"));
  evaluate(`state.forecast=${JSON.stringify(fixture.forecast)};`);

  await evaluate("loadProfiles()");
  assert(element("teams-content").innerHTML.includes("Harbor Comets"));
  evaluate(`state.forecast=null; renderHistoricalPlayers(${JSON.stringify(fixture.profiles)});`);
  assert(element("players-content").innerHTML.includes("Historical player evidence"));
  assert(element("players-content").innerHTML.includes("Harbor Player"));
  assert(element("players-content").innerHTML.includes("data-historical-player"));
  evaluate(`state.forecast=${JSON.stringify(fixture.forecast)};`);
  await evaluate("loadAudit()");
  assert(element("audit-content").innerHTML.includes("NOT MEASURABLE"));

  const cohort = {
    synthetic: {
      count: 3, distinct_games: 2,
      metrics: { brier: 0.2, margin: { mae: 3, rmse: 4, coverage_80: 0.8, crps: 2 } },
      players: { pts: { absolute_error: 2, coverage_80: 0.8, crps: 1 } },
    },
  };
  const cohortHTML = evaluate(`cohortEvaluation(${JSON.stringify(cohort)})`);
  assert(cohortHTML.includes("3 FORECASTS"));
  assert(cohortHTML.includes("2 DISTINCT GAMES"));
  assert(cohortHTML.includes("80.0%"));
  fixture.evaluation = { status: "EXPERIMENTAL", forecasts_scored: 3, metrics: {}, cohorts: cohort };
  await evaluate("loadAudit()");
  assert(element("audit-content").querySelector(".panel").innerHTML.includes("SYNTHETIC COHORT"));
  assert(element("audit-content").querySelector(".panel").querySelector("p.help").removed);

  // Validate the actual registered submit handler, not just static render helpers.
  const request = fixture.forecast.request;
  element("dataset").value = request.dataset_id;
  element("home-team").value = request.home_id;
  element("away-team").value = request.away_id;
  element("cutoff").value = request.cutoff.slice(0, 19);
  element("tipoff").value = request.tipoff.slice(0, 19);
  element("seed").value = String(request.seed);
  element("simulations").value = String(request.simulations);
  await element("forecast-form").listeners.get("submit")({ preventDefault() {} });
  const submitted = calls.findLast((call) => call.url === "/api/forecasts" && call.options.method === "POST");
  assert(submitted);
  assert.equal(JSON.parse(submitted.options.body).dataset_id, request.dataset_id);
  assert.equal(JSON.parse(submitted.options.body).simulations, 20);
  assert(element("message").textContent.includes("Forecast frozen"));

  // SGP requires an explicit saved forecast and preserves the synthetic use block.
  await evaluate("loadSources()");
  assert(element("data-readiness").innerHTML.includes("NO ACTIONABLE SIGNAL"));
  element("sgp-forecast").value = "";
  evaluate("clearSGPForecast()");
  assert.equal(evaluate("state.sgpForecast"), null);
  await evaluate("evaluateSGP()");
  assert(element("message").textContent.includes("Choose an existing frozen forecast explicitly"));
  element("sgp-forecast").value = fixture.forecast.id;
  await element("sgp-forecast").listeners.get("change")();
  assert(element("sgp-forecast-context").innerHTML.includes("SYNTHETIC BLOCK"));
  for (const leg of fixture.sgp_request.legs) {
    element("sgp-player").value = leg.player_id;
    element("sgp-stat").value = leg.stat;
    element("sgp-side").value = leg.side;
    element("sgp-line").value = String(leg.line);
    element("sgp-leg-form").listeners.get("submit")({ preventDefault() {} });
  }
  assert.equal(evaluate("state.sgpLegs.length"), 2);
  assert(element("sgp-legs").innerHTML.includes("Harbor Player"));
  await element("sgp-evaluate").listeners.get("click")();
  const sgpSubmitted = calls.findLast((call) => call.url === "/api/sgp/evaluate");
  assert.deepEqual(JSON.parse(sgpSubmitted.options.body), fixture.sgp_request);
  assert(element("sgp-results").innerHTML.includes("JOINT MODEL PROBABILITY"));
  assert(element("sgp-results").innerHTML.includes("NO ACTIONABLE SIGNAL"));
  assert(element("sgp-results").innerHTML.includes("SYNTHETIC DATA BLOCK"));
  assert(element("sgp-results").innerHTML.includes("NOT MEASURABLE"));
  assert(!element("sgp-results").innerHTML.includes("APPROVED"));
  const legsBefore = evaluate("state.sgpLegs.length");
  element("sgp-side").value = "over";
  element("sgp-line").value = "10";
  element("sgp-leg-form").listeners.get("submit")({ preventDefault() {} });
  assert.equal(evaluate("state.sgpLegs.length"), legsBefore);
  assert(element("message").textContent.includes("half-line"));
  element("sgp-quote").value = "{bad JSON";
  const callsBeforeBadQuote = calls.length;
  await element("sgp-evaluate").listeners.get("click")();
  assert.equal(calls.length, callsBeforeBadQuote);
  assert(element("message").textContent.includes("Invalid quote JSON"));
  element("sgp-quote").value = "";

  // Real schedule endpoint enrichment was captured with an isolated provider fixture.
  await evaluate("loadTonight()");
  assert(element("tonight-games").innerHTML.includes("Harbor Comets"));
  assert(element("tonight-games").innerHTML.includes("No tickets yet"));
  assert(element("tonight-games").innerHTML.includes("Generate drafts"));
  assert(element("tonight-games").innerHTML.includes("FanDuel lines"));
  assert(element("tonight-games").innerHTML.includes("not captured"));
  await element("tonight-refresh").listeners.get("click")();
  assert(calls.some((call) => call.url === "/api/tonight/refresh" && call.options.method === "POST"));
  const ticketSlate = structuredClone(fixture.tonight);
  ticketSlate.games[0].sgp.tickets = [fixture.sgp, { ...fixture.sgp, id: "second-ticket-test" }];
  evaluate(`state.tonight=${JSON.stringify(ticketSlate)}; renderTonight();`);
  assert(element("tonight-games").innerHTML.includes("2 saved"));
  assert(element("tonight-games").innerHTML.includes("Saved ticket second-t"));
  assert(element("tonight-games").innerHTML.includes("SYNTHETIC · BLOCKED"));
  // ESPN status names and normalized phase must both be represented; a raw
  // STATUS_SCHEDULED name must not accidentally disable the pregame builder.
  const phaseSlate = structuredClone(fixture.tonight);
  phaseSlate.games[0].sgp.forecasts = [{ id: fixture.forecast.id, synthetic: false, request: fixture.forecast.request }];
  evaluate(`state.tonight=${JSON.stringify(phaseSlate)}; renderTonight();`);
  assert(element("tonight-games").innerHTML.includes("Evaluate forecast →"));
  await evaluate('buildTonightSGP("ui-test-game")');
  assert.equal(element("sgp-forecast").value, fixture.forecast.id);
  assert.equal(evaluate("state.sgpForecast.id"), fixture.forecast.id);
  phaseSlate.games[0].phase = "in";
  phaseSlate.games[0].status = "STATUS_IN_PROGRESS";
  evaluate(`state.tonight=${JSON.stringify(phaseSlate)}; renderTonight();`);
  assert(!element("tonight-games").innerHTML.includes("Evaluate forecast →"));
  const staleSlate = { ...ticketSlate, status: "LAST_GOOD", age_seconds: 300, reason: "Test refresh failure" };
  evaluate(`state.tonight=${JSON.stringify(staleSlate)}; renderTonight();`);
  assert(element("tonight-source").innerHTML.includes("STALE"));
  assert(element("tonight-games").innerHTML.includes("Harbor Comets"));
  evaluate('state.tonight={status:"UNAVAILABLE",reason:"Test provider unavailable",games:[]}; renderTonight();');
  assert(element("tonight-games").innerHTML.includes("does not establish that no games"));
  evaluate('state.tonight={status:"EMPTY_PROVIDER_SLATE",date:"fixture-date",games:[]}; renderTonight();');
  assert(element("tonight-games").innerHTML.includes("No games in this provider response"));
  assert(element("tonight-games").innerHTML.includes("does not establish that the NBA has no games"));

  // Render actual construction-module outputs from the explicitly fictional
  // test fixture. No handcrafted tickets substitute for the construction path.
  const productSlate = { ...fixture.tonight, games: [fixture.product_game] };
  evaluate(`state.tonight=${JSON.stringify(productSlate)}; renderTonight();`);
  assert(element("tonight-games").innerHTML.includes("Fictional H-0"));
  assert(element("tonight-games").innerHTML.includes("3 legs"));
  assert(element("tonight-games").innerHTML.includes("5 legs"));
  assert(element("tonight-games").innerHTML.includes("8 legs"));
  assert(element("tonight-games").innerHTML.includes("5/5"));
  assert(element("tonight-games").innerHTML.includes("historical hits"));
  assert(element("tonight-games").innerHTML.includes("not offered FanDuel lines"));
  assert(element("tonight-games").innerHTML.includes("Joint chance unavailable"));
  assert(element("tonight-summary").innerHTML.includes("3</strong> constructed drafts"));
  evaluate('openDraftBuilder("tonight-fixture",3)');
  assert(element("draft-workspace").innerHTML.includes("3-leg draft"));
  assert(element("draft-workspace").innerHTML.includes("Constructed. Unpriced."));
  assert(element("manual-market-workspace").innerHTML.startsWith('<details class="manual-source-fallback">'));
  await evaluate('generateGameDrafts("tonight-fixture")');
  assert(calls.some((call) => call.url === "/api/games/tonight-fixture/drafts?selection_mode=aggressive" && call.options.method === "POST"));
  await element("tonight-build-all").listeners.get("click")();
  assert(calls.some((call) => call.url === "/api/tonight/build?selection_mode=aggressive" && call.options.method === "POST"));
  assert(calls.some((call) => call.url === "/api/build-jobs/ui-build-job"));
  assert.equal(element("tonight-build-all").disabled, false);
  const publication = { player_name: "Fictional Test Player", stat: "pts", side: "over", line: 20.5,
    american_odds: -110, decimal_odds: 1 + 100 / 110, price_freshness: "UNKNOWN",
    source_url: "fixture://published-odds-test", source_sha256: "test-fixture-hash", retrieved_at: fixture.forecast.created_at };
  const publishedHTML = evaluate(`renderPublishedMarkets([${JSON.stringify(publication)}])`);
  assert(publishedHTML.includes("-110"));
  assert(publishedHTML.includes("not confirmed current offers"));
  assert(publishedHTML.includes("Price freshness UNKNOWN"));
  assert(publishedHTML.includes("combined SGP price unavailable"));
  await element("tonight-odds-refresh").listeners.get("click")();
  assert(calls.some((call) => call.url === "/api/odds/refresh" && call.options.method === "POST"));
  assert.equal(element("tonight-odds-refresh").disabled, false);

  // Polymarket prices remain separate exchange data. Indicative Gamma metadata
  // is visible with a warning and never promoted into an executable quote.
  assert.equal(evaluate("americanOdds(116)"), "+116");
  assert.equal(evaluate("americanOdds(-122)"), "-122");
  assert.equal(evaluate("americanOdds(null)"), "Unavailable");
  const polyFixture = {
    status: "PARTIAL", provider: "Polymarket", retrieved_at: fixture.forecast.created_at,
    markets: [{ id: "ui-poly-player", event_id: "ui-poly-event", event_title: "Fictional NBA fixture",
      question: "Fictional player points", market_type: "player_prop", player_name: "<script>Fake name</script>",
      stat: "pts", side: "over", line: 20.5, outcome: "Yes", american_odds: null, decimal_odds: null,
      indicative_american_odds: 116, indicative_decimal_odds: 2.16, best_ask: null, spread: null,
      liquidity: 1234, tradeable: false, quote_status: "INDICATIVE_MARKET_METADATA_NOT_EXECUTABLE",
      source_url: "fixture://polymarket-ui", observed_at: fixture.forecast.created_at }],
    errors: [{ reason: "Fixture order-book HTTP 403" }],
  };
  fixture.polymarket = polyFixture;
  await evaluate("loadPolymarket()");
  assert(element("polymarket-markets").innerHTML.includes("+116"));
  assert(element("polymarket-markets").innerHTML.includes("INDICATIVE"));
  assert(element("polymarket-markets").innerHTML.includes("Executable order-book quotes unavailable"));
  assert(element("polymarket-markets").innerHTML.includes("order book unavailable"));
  assert(!element("polymarket-markets").innerHTML.includes("<script>Fake name</script>"));
  assert(element("polymarket-markets").innerHTML.includes("&lt;script&gt;"));
  assert(element("polymarket-summary").innerHTML.includes("1</strong> player-prop outcomes"));
  const depthFixture = { ...polyFixture.markets[0], player_name: "Fictional quoted player",
    american_odds: 100, decimal_odds: 2, best_ask: 0.5, executable: true, tradeable: false,
    quote_status: "ASK_DEPTH_AVAILABLE", quote_basis: "ORDERBOOK_BEST_ASK",
    orderbook: { best_bid: 0.48, spread: 0.02, best_ask_shares: 12.5, total_ask_shares: 137,
      minimum_order_shares: 5, requested_shares: 5, best_ask_has_requested_depth: true,
      ask_depth: [{ price: 0.5, size: 12.5 }],
      depth_quote: { shares: 5, gross_cost: 2.5, volume_weighted_price: 0.5 } } };
  const depthHTML = evaluate(`renderPolymarketRow(${JSON.stringify(depthFixture)})`);
  assert(depthHTML.includes("+100"));
  assert(depthHTML.includes("Depth-backed public BUY ask"));
  assert(depthHTML.includes("account trading not enabled"));
  assert(depthHTML.includes("GROSS · FEES EXCLUDED"));
  assert(depthHTML.includes("$0.020"));
  assert(depthHTML.includes("12.50"));
  assert(depthHTML.includes("137.00 total ask shares"));
  assert(depthHTML.includes("gross cost $2.500"));
  assert(!depthHTML.includes("quote unverified"));
  delete depthFixture.orderbook.best_ask_shares;
  assert(evaluate(`renderPolymarketRow(${JSON.stringify(depthFixture)})`).includes("12.50"));
  const pricedLeg = { ...fixture.product_game.sgp.draft_families[0].legs[0], threshold_kind: "OFFERED_LINE",
    offer: { sportsbook: "Polymarket", american_odds: 223, decimal_odds: 3.23 },
    offered_alt_ladder: [{ side: "over", line: 9.5, hits: 3, sample_games: 7,
      offer: { sportsbook: "Polymarket", american_odds: 223, decimal_odds: 3.23 } },
    { side: "over", line: 10.5, hits: 2, sample_games: 7,
      offer: { sportsbook: "Polymarket", decimal_odds: 3.85 } }] };
  const pricedFamily = { leg_count: 3, status: "DRAFT_UNPRICED", selection_mode: "aggressive", legs: [pricedLeg], block_reasons: [] };
  const pricedHTML = evaluate(`renderDraftFamily(${JSON.stringify(pricedFamily)},"ui-fixture")`);
  assert(pricedHTML.includes("+223"));
  assert(pricedHTML.includes("+285"));
  assert(pricedHTML.includes("Polymarket"));
  assert(pricedHTML.includes("Gross BUY-ask equivalent; fees excluded"));
  assert(pricedHTML.includes("Combined ticket price unavailable"));
  assert(pricedHTML.includes("selected-leg-price"));
  assert.equal(evaluate('offerAmerican({decimal_odds:1.8})'), "-125");
  assert.equal(evaluate('offerAmerican({decimal_odds:4.76})'), "+376");
  assert.equal(evaluate('offerAmerican({decimal_odds:2})'), "+100");
  const updatedEvidence = { status: "UNCALIBRATED_RESEARCH_PROXY", proxy_probability: 0.43,
    quote_reference_probability: 0.38, probability_sd: 0.06, uncertainty_adjusted_probability: 0.4,
    fee_per_share: 0.01, entry_cost: 0.36, proxy_return_after_entry_fee: 0.43 / 0.36 - 1,
    ranking_score: 0.4 / 0.36 - 1, ranking_basis: "FEE_ADJUSTED_RESEARCH", legacy_probability: 0.8 };
  const updatedHTML = evaluate(`aggressiveProxyNote({aggressive_evidence:${JSON.stringify(updatedEvidence)}})`);
  assert(updatedHTML.includes("market-anchored research"));
  assert(updatedHTML.includes("Market-shrunk estimate 43.0%"));
  assert(updatedHTML.includes("quote reference 38.0%"));
  assert(updatedHTML.includes("Probability SD 6.00 pp"));
  assert(updatedHTML.includes("uncertainty-adjusted estimate 40.0%"));
  assert(updatedHTML.includes("ranking score 0.1111"));
  assert(updatedHTML.includes("$0.0100/share"));
  assert(updatedHTML.includes("$0.3600/share"));
  assert(updatedHTML.includes("19.44%"));
  assert(updatedHTML.includes("FEE-ADJUSTED RESEARCH"));
  assert(updatedHTML.includes("Legacy historical proxy"));
  assert(!updatedHTML.includes("Gross research return proxy"));
  const missingFeeHTML = evaluate(`aggressiveProxyNote({aggressive_evidence:${JSON.stringify({ ...updatedEvidence,
    fee_per_share: null, proxy_return_after_entry_fee: null, ranking_basis: "GROSS_RESEARCH_ONLY_FEES_UNAVAILABLE" })}})`);
  assert(missingFeeHTML.includes("GROSS RESEARCH - FEES UNAVAILABLE"));
  assert(missingFeeHTML.includes("Entry fee Unavailable"));
  assert(missingFeeHTML.includes("Research return after entry fee Unavailable"));
  assert(missingFeeHTML.includes("cannot be claimed where fees are unavailable"));
  const legacyHTML = evaluate('aggressiveProxyNote({aggressive_evidence:{proxy_probability:0.8,break_even_probability:0.5,proxy_return_gross:0.6}})');
  assert(legacyHTML.includes("Gross research return proxy 60.00%"));
  assert(!legacyHTML.includes("market-anchored research"));
  const updatedLadderLeg = { ...pricedLeg, offered_alt_ladder: [{ ...updatedEvidence,
    side: "over", line: 9.5, hits: 3, sample_games: 7, block_reasons: ["Test wide-book policy"],
    offer: { sportsbook: "Polymarket", american_odds: 223, decimal_odds: 3.23 } }] };
  const updatedLadderHTML = evaluate(`renderLegDiagnostics(${JSON.stringify(updatedLadderLeg)})`);
  assert(updatedLadderHTML.includes("+223"));
  assert(updatedLadderHTML.includes("ranking score 0.1111"));
  assert(updatedLadderHTML.includes("Test wide-book policy"));
  const coverageFamily = { ...pricedFamily, leg_count: 5,
    legs: [{ ...pricedLeg, family_role: "anchor" }, { ...pricedLeg, player_id: "ui-support", player_name: "Fictional support", family_role: "support" }],
    optimization: { anchor_target: 2, anchors_selected: 1, anchor_shortfall: 1,
      negative_mean_proxy_legs: 1, nonpositive_uncertainty_legs: 2,
      objective: "MARGINAL_SUPPORT_OBJECTIVE_NOT_JOINT_PROBABILITY" } };
  const coverageHTML = evaluate(`renderDraftFamily(${JSON.stringify(coverageFamily)},"ui-coverage")`);
  assert(coverageHTML.includes("BEST_AVAILABLE_COVERAGE"));
  assert(coverageHTML.includes('family-role anchor'));
  assert(coverageHTML.includes('family-role support'));
  assert(coverageHTML.includes("1 / 2 anchors"));
  assert(coverageHTML.includes("Negative mean proxy: 1"));
  assert(coverageHTML.includes("Nonpositive uncertainty score: 2"));
  assert(coverageHTML.includes("not presented as all-positive edge"));
  assert(coverageHTML.includes("No joint success probability"));
  assert(coverageHTML.includes("+223"));
  assert(!evaluate(`renderDraftFamily(${JSON.stringify(pricedFamily)},"ui-old")`).includes("BEST_AVAILABLE_COVERAGE"));
  evaluate('renderNightlyRequirements({required_sizes:[5,8],available_by_size:{"5":2,"8":1},missing_sizes:[],status:"AVAILABLE"})');
  assert(element("nightly-requirements").innerHTML.includes("5-leg families"));
  assert(element("nightly-requirements").innerHTML.includes("8-leg families"));
  assert(element("nightly-requirements").innerHTML.includes("not a win guarantee"));
  evaluate('renderNightlyRequirements({required_sizes:[5,8],available_by_size:{"5":1,"8":0},missing_sizes:[8],status:"PARTIAL"})');
  assert(element("nightly-requirements").innerHTML.includes("Missing source-valid sizes: 8"));
  assert(element("nightly-requirements").innerHTML.includes("PARTIAL"));
  evaluate('renderNightlyRequirements(null)');
  assert(element("nightly-requirements").innerHTML.includes("COVERAGE NOT REPORTED"));
  await element("polymarket-refresh").listeners.get("click")();
  assert(calls.some((call) => call.url === "/api/polymarket/refresh" && call.options.method === "POST"));
  element("polymarket-type").value = "winner";
  element("polymarket-type").listeners.get("change")();
  assert(element("polymarket-markets").innerHTML.includes("No actual markets match"));
  element("polymarket-type").value = "all";
  evaluate('state.polymarket={status:"UNAVAILABLE",markets:[],errors:[{reason:"No quote fixture"}]}; renderPolymarket();');
  assert(element("polymarket-markets").innerHTML.includes("No NBA market lines returned"));
  assert(!element("polymarket-markets").innerHTML.includes("+116"));
  element("builder-selection-mode").value = "standard";
  evaluate(`state.tonight=${JSON.stringify(productSlate)};`);
  await evaluate('generateGameDrafts("tonight-fixture")');
  assert(calls.some((call) => call.url === "/api/games/tonight-fixture/drafts?selection_mode=standard"));
  assert.equal(evaluate("selectionMode()"), "standard");

  // Cleared timing controls must stop metric/profile requests, rather than silently use now.
  element("cutoff").value = "";
  const previousCallCount = calls.length;
  await evaluate("loadMetrics()");
  await evaluate("loadProfiles()");
  assert.equal(calls.length, previousCallCount);
  assert(element("metric-content").innerHTML.includes("valid information cutoff"));
  assert(element("teams-content").textContent.includes("valid information cutoff"));

  console.log("Dashboard checks passed: existing forecast/provenance/SGP checks; available-first metrics; tonight cards; actual construction-module 3/5/8-leg drafts; builder and bulk jobs; FanDuel source labels; Polymarket read-only refresh, signed indicative odds, unavailable order-book state, HTML escaping, filters and empty coverage; standard/aggressive query wiring.");
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
