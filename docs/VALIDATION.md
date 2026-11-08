# Validation evidence — 2026-11-04

## Current source-built product verification

- Full suite: **336 passed**, one existing installed Starlette/httpx deprecation warning. Alternate-line regressions verify a supported offered 30+ milestone can replace 15+, a sample mean of 30 does not force a 30+ selection, fractional milestones are not rounded, and alternate provider prices remain exact. No model or betting accuracy is implied.
- Reproducible dashboard harness passed against actual construction-module output and isolated API fixtures. It covers populated 3/5/8-leg draft families, current-roster history, Builder navigation, async jobs, source freshness and automatic published-price refresh.
- Actual local HTTP build completed for **14 games**, using **486 current roster records** and merged **100 accepted historical games**. It persisted 42 attempted families: **40 constructed, two blocked**. Draft legs were checked against current roster IDs, injury exclusions, both-team representation, distinct players and source sample counts.
- Automatic primary FanDuel acquisition obtained **four published props**; **three matched** current game/roster identities. There are **zero verified current prices** and no combined SGP quote. Every captured source's original timestamp and hash is retained. Article text/structured-price conflicts are visible.
- The connected current-market importer passed injected-provider tests for event, team, player, quote age, book, source hash and numeric validation. No credential is configured; **authenticated provider/runtime coverage is unverified**.
- `output/product-runtime-evidence.json` records the actual job and source-built outputs. `docs/AUTOMATIC-DATA.md` explains their practical limits. No wager was placed; joint predictive accuracy is **NOT MEASURABLE**. Browser visual layout remains **UNVERIFIED**.

## Authentic data and SGP extension

Tonight board extension: full suite **223 passed**; 25 slate/API checks and the dashboard harness passed after aligning the provider's status names with pregame/live/final phases. `python scripts/verify_tonight_runtime.py` verified the actual running server's **14 November 4 matchups**, Eastern date handling, latest served JavaScript, and separate per-game SGP sections. At verification there were **zero matching saved tickets and zero captured offered props**; the UI does not invent either. Evidence: `output/tonight-runtime-evidence.json`, `output/slates/live-verification.json` and the archived schedule response. Browser visual QA remains unverified.

Latest full suite: **198 passed**, one installed Starlette/httpx deprecation warning. Dashboard mocked-DOM integration harness passed, including SGP selection/submission, shared-sample probabilities, synthetic blocking, invalid lines/quotes, historical profiles and source readiness. Browser visual layout remains unverified.

Authentic ESPN acquisition imported **93 games, 30 teams, 464 players** with zero inferred rotation records. All archived source hashes were verified. Seven of the selected 100 games were rejected for score inconsistencies or missing statistics. See `output/authentic-history/import-verification.json` and its linked immutable manifest. Official injury PDF bytes were archived with SHA256 and identity mapping left unreviewed. No odds subscription is configured; no real offered FanDuel ticket or independent accuracy claim has been verified. [SGP implementation and gates](NBA-SGP.md).

The original baseline record below predates this extension; its statement that authentic history was absent is historical.

## Software checks

- `python -m pip install -e '.[test]'`: successful editable package installation. Package discovery explicitly includes `nba_quant` only.
- `python -m pytest -q`: **106 passed**, one installed Starlette/httpx compatibility deprecation warning. This is local software verification, not a predictive accuracy result.
- `node --check nba_quant/static/app.js`: passed.
- `python scripts/check_dashboard.py`: passed. Reproducible mocked-DOM harness exercises actual submit handlers, rendering, source labels, player tables, distributions, profiles, metric states, audit/cohort tables, HTML escaping and invalid cutoffs against a temporary SQLite/API fixture. This is not a browser visual check.
- `python -m compileall -q nba_quant scripts`: passed.
- `python scripts/verify_runtime.py`: actual localhost HTTP import/forecast/export/profile flow passed. A 250-simulation fictional forecast was saved and reloaded identically. Representative player scores and minutes reconciled to team totals.
- Installed CLI replay test created a separate temporary database, froze and scored two expanding-window forecasts and reloaded them via export; synthetic cohort retained and actual import-after-cutoff disclosed.

## Independent reviews

Three agents reviewed simulation accounting, provenance/evaluation, and dashboard behavior. Confirmed defects fixed during this work:

- Offensive rebound continuation generated a shot after the period horn.
- Immutable SQLite records could be replaced without recursive trigger enforcement.
- Historical replay classification omitted the actual import timestamp.
- Brier scoring clipped extreme probabilities; reliability bins omitted some exact decimal boundaries.
- Missing player outcomes needed explicit coverage counts; repeated target games needed distinct-game counts.
- Player per-game averages counted zero-minute DNP rows.
- Static paths and cohort evaluation UI needed alignment with the API.
- Installation initially discovered the local data folder as a package.

Simulation tests independently reconstruct every stat from events, check legal five-player schedules, regulation and real overtime, eligibility, restrictions, confidence intervals and deterministic replay. Provenance tests check future-known games/rotations, UTC handling, corruption detection and immutable update/delete/replacement. Evaluation tests compare CRPS against its brute-force definition and check cohort/outcome boundaries. API and CLI tests exercise persisted end-to-end flows.

## Persisted artifacts

`output/runtime-evidence.json` contains the runtime verification time, frozen forecast ID and checks. `output/latest-forecast.json` is the complete **synthetic** forecast and event ledger. `output/forecast-request.json` contains its reproducible request. SQLite stores the immutable dataset and frozen forecast; generated data/output files are ignored by Git.

## Unverified or unavailable

- Real NBA history: not imported. An attempted NBA CDN box-score fetch returned HTTP 403. Canonical JSON import is implemented; a working automatic provider adapter is not.
- Actual predictive accuracy: **NOT MEASURABLE**. Synthetic history was generated with the same baseline, so its scores cannot establish skill.
- Browser visual QA: **UNVERIFIED**. The browser tool reported local browser use disabled in Work settings. JavaScript syntax and rendering/event flows were checked with mocked DOM and real API-shaped fixtures; this does not verify layout in a browser.
- Licensed tracking, trained availability/minutes/tactical models, full official-rules simulation, live updates and production deployment remain outside this completed baseline. See `IMPLEMENTATION.md` for exact coverage.
