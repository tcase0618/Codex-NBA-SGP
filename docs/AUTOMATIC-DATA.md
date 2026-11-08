# Automatic data and source-built drafts

The main workflow is Tonight → draft family → Builder. A background build refreshes the verified schedule, current ESPN roster membership and reported injuries, primary FanDuel published market components, an optional connected odds feed, and source-built 3/5/8-leg families. Jobs expose actual stages and errors. Jobs are process-local; the resulting datasets, source archives, market snapshots and drafts are persisted separately.

## Verified current coverage

On November 4, 2026 the local runtime verified:

- All 107 completed games discovered in the requested current-season date window were requested. **100 accepted**, seven rejected for source inconsistencies/missing values. All 30 teams and 465 historical players are represented. This is not complete multiseason history.
- **28 current rosters, 486 players** across tonight's 14 matchups. The initial acquisition supplied 66 Out and 18 Day-To-Day statuses; later refreshes preserve their own changing source counts in the runtime evidence. The remaining players are not automatically called available; active-game status remains unverified. Roster sources expire after one hour and rescheduled games invalidate an earlier roster-to-event binding.
- **42 attempted draft families:** 40 constructed, two blocked. Each constructed family uses distinct players, both teams and mixed statistics; every leg includes actual prior-game sample IDs, minutes and recent results. Out, Questionable, Doubtful and ineligible membership statuses are excluded. Statistical milestone thresholds are labeled `ANALYTICAL_THRESHOLD_NOT_OFFERED` unless an exact offered market is available.
- **Four automatically fetched published FanDuel props**, with source bytes, market/selection identifiers, odds and article timestamps. Three matched the current roster/game identities; a suffix/name mismatch left one unbound rather than guessing. Published quotes are shown separately from current offered markets.

Evidence is persisted in `output/product-runtime-evidence.json`, `output/context/latest-2026-11-04.json`, `output/authentic-history/current-season-coverage.json` and `output/fanduel/latest.json`.

## FanDuel retrieval boundaries

`nba_quant/fanduel.py` reads primary FanDuel Research HTML and structured market components. Discovery is automatic for current Eastern-date NBA prop articles. The public sportsbook route returned blocked responses or an application shell during investigation; no full sportsbook market catalog was acquired. The Research category page was an article listing, not an all-player market table.

The structured components contain real prices but no quote-update timestamp. Article publication, modification and retrieval times are retained separately. Neither publication time nor a new download makes the underlying price current. Editorial text and structured market prices disagree in two cases; both are preserved for inspection. These snapshots cannot pass the five-minute current-quote gate or supply a combined SGP payout. Refresh is read-only and does not touch an account or betslip.

Current roster names and the named matchup/date are checked before displaying a published quote on a game. FanDuel's listed event times differ from ESPN's schedule by ten minutes in these records. A bounded, explicitly labeled 15-minute mapping window allows display; the original times and discrepancy remain visible. This does not authenticate an executable wager. Names that do not match uniquely remain unmapped.

## Connected current-market path

`nba_quant/connected_odds.py` automatically discovers The Odds API NBA events, requests FanDuel-only player markets, checks exact event/team/current-roster identities, and stores fresh source-bound selections. It refuses stale/future observations, wrong books, ambiguous names, missing source hashes and malformed prices. Player suffixes are not silently guessed. The response preserves provider event IDs and event-time differences.

This path is implemented and tested with injected fixtures. **No API credential is configured and no authenticated provider response has been verified.** An existing `THE_ODDS_API_KEY` in the server environment enables it during automatic refresh. No account was created, credential provisioned, subscription purchased, or quota charged in this work. Provider-reported current leg prices still do not supply a sportsbook's correlation-adjusted combined SGP price.

Manual source entry remains a collapsed advanced fallback, not the primary workflow.

## Meaning of a constructed draft

### Alternate-line preference

The user permits higher alternate targets when supported, rather than always selecting a low milestone. The connected provider now requests standard and alternate points, rebounds, assists and threes markets. Fresh source-bound integer milestones and half-line over/unders preserve their exact prices. The constructor evaluates the available ladder and chooses the highest threshold meeting its existing descriptive screen; other offered thresholds and their sample frequencies/break-even prices are retained for inspection. No observed mean is treated as the probability of exceeding that mean, and no matchup boost is fabricated. Fitted matchup distributions and price-aware value ranking are still unavailable.

The 40 tickets in the November 4 PDF had **zero source-priced legs**. Their average leg or combined ticket odds are therefore not measurable. A mean over a small, separately published quote sample must never be substituted for the prices of those tickets. American odds are not averaged directly; any sample mean is calculated in decimal odds with its population, observation time and freshness limits disclosed.

Analytical thresholds use fixed milestone ladders and prior descriptive frequency, with a minimum three positive-minute appearances for the current team and 18 mean historical minutes. This deliberately simple screening rule is **not a fitted probability model**. The family is not priced, does not multiply marginal hit rates, and never invents joint probability, expected value or a combined return. Selection effects, small samples, game-day availability, minutes restrictions, sportsbook eligibility, DNP/void rules and independent joint calibration remain unresolved.

The application has moved from empty game cards to populated player evidence and actual constructed draft legs. It has **not established actionable betting accuracy**. Every current family remains research-only. More simulations or additional displayed metrics do not change that status.
