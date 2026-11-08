# Codex NBA SGP

A source-aware NBA research terminal with a same-game combination builder, AutoGluon/XGBoost shadow forecasts, chronological evaluation, and a local dashboard. Python3.11+, FastAPI, SQLite, NumPy and vanilla JavaScript; no LLM is required at runtime.

**Research status:** model agreement is a screening heuristic, not a calibrated hit probability or guaranteed win. No model is currently approved for production wagering. Public Polymarket reads provide individual contracts, not a quoted combined SGP payout. Historical observations, forecasts and synthetic demonstrations remain distinct.

## Intelligence terminal (play-by-play platform)

`/terminal` serves the **Case NBA Quant Intelligence Platform**. It covers player, team, lineup and matchup intelligence, a vectorized possession simulator (1k/10k/50k sims), and walk-forward model audit. It is built on reconstructed ESPN play-by-play: possessions, five-man stints, shot coordinates, RAPM and xFG. Data and models never leave the machine, and no LLM runs at runtime.

```bash
python -m nba_quant.intel acquire 2025-10-21 2026-06-25   # cache ESPN summaries
python -m nba_quant.intel build                           # build output/warehouse.sqlite
python -m nba_quant serve --port 8020                     # open http://127.0.0.1:8020/terminal
python -m nba_quant.intel backtest 2025-11-17T12:00Z 2026-06-25T00:00Z
```

See [docs/INTELLIGENCE-PLATFORM.md](docs/INTELLIGENCE-PLATFORM.md) for the audit, architecture, metric classes, validation protocol, measured results and limitations.

## Quick start

```powershell
git clone https://github.com/tcase0618/Codex-NBA-SGP.git
cd Codex-NBA-SGP
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[test]'
.\.venv\Scripts\python.exe -m nba_quant serve --port 8020
```

Open http://127.0.0.1:8020/. A fresh clone starts without private historical datasets, local database records or model artifacts. The terminal labels unavailable inputs explicitly. Use the selected interpreter with `-m nba_quant demo` for an explicitly synthetic demonstration, or import verified canonical data for real research.

For optional ML, create `.venv-ml` and install `.[ml,test]`; Start-NBAQuant.ps1 prefers it when present. PDF analysis/export scripts use the optional `.[reports]` extra and require their documented inputs.

## Main workflows

- **Tonight/Builder:** source-bound3/5/8-leg research combinations. Unders require a provider-identified standard line; alternate lines are over-only.
- **Predictions & models:** target readiness, versioned AutoGluon/XGBoost/baseline comparisons, chronological backtests and calibration evidence. Model training runs offline, never inside normal frontend requests.
- **Sources and evaluation:** preserved observation cutoffs, immutable forecasts/outcomes, source hashes, freshness checks, safe unavailable states and no wagering execution.
- **Pricing:** public Polymarket discovery/CLOB reads; optional The Odds API integration and explicitly labeled published FanDuel evidence. No subscription or credential is required for the demo. Freshness and coverage depend on actual provider access.

See the [engineering report](docs/ML-ENGINEERING-REPORT.md) for measured experiments, training/inference commands and limitations; [repository boundaries](docs/REPOSITORY.md) for fresh-clone setup and private runtime-data handling. Dated metrics in historical reports are not fresh-clone coverage claims.

## What works

- Catalog of all **440 blueprint metric slots**: 100 universal, 20 per position, 240 team. Source-backed box-score formulas are active where inputs exist; unsupported slots remain `UNAVAILABLE` with null values.
- Validated source imports: IDs, timestamps, scoring identities, team/player minutes, makes/attempts, cross-team steals/blocks and assist bounds. Original source payloads are content-addressed and immutable.
- Cutoff-filtered empirical player shooting/usage/assist/rebound rates with shrinkage, team pace and diagnostic offensive/defensive ratings. Rotation and availability snapshots and explicit scenario overrides.
- Joint Monte Carlo event engine: five-player lineups, minutes caps, turnovers, steals, shots, blocks, shooting fouls, free throws, event-linked assists, live rebounds, second chances and actual overtime. Player scoring and minutes reconcile to team outcomes.
- Player distributions and thresholds, score/margin/total intervals, win probabilities and Monte Carlo confidence intervals, cross-stat covariance/correlation, representative event/lineup ledger and empirical participation rates.
- Frozen forecasts with input/source/feature hashes, model version, request, seed and full output export. Immutable matching outcomes; Brier/log loss, MAE/RMSE, empirical CRPS, interval coverage and reliability bins. Synthetic, retrospective and prospective cohorts stay separate.
- Expanding-window retrospective evaluation using only completed/known games before each forecast cutoff. Rotation snapshots must also exist before that cutoff.

## Import real history

The canonical import format is the `Dataset` schema in [`nba_quant/schemas.py`](nba_quant/schemas.py); the API exposes the full JSON schema. Generate an example:

```powershell
python -m nba_quant example --output output/example-dataset.json
python -m nba_quant import path/to/your-validated-history.json
```

The example is synthetic. Supply actual provider source, source URL, retrieval time, entity IDs, historical final games with complete box scores, and time-stamped rotation/availability snapshots. Use consistent IDs across seasons and trades. Each dataset is a complete immutable snapshot, so later imports cannot silently revise earlier forecast sources. Forecast requests explicitly select their dataset. All datetimes require a timezone.

`known_at` means when the record was actually available, not the date of the game. Backdating it to tipoff leaks future information. Caller-supplied historical timestamps are not independently authenticated. If retrieval or actual local import follows a historical cutoff, the run is labeled retrospective. A current roster must never be passed as historical knowledge. Missing outcome player rows are excluded from player scoring, never assumed to be DNP zeros.

Official source references: [NBA Stats](https://www.nba.com/stats/), [NBA definitions](https://www.nba.com/stats/help/glossary), [NBA scoring and timing rules](https://official.nba.com/rule-no-5-scoring-and-timing/). Regulation uses four 12-minute periods and overtime uses five-minute periods. The baseline clock is approximate, not an official rules implementation. A direct NBA CDN box-score fetch returned HTTP403. The implemented read-only ESPN adapter supplies authentic secondary-provider history through validated canonical imports; it does not establish an official NBA data connection or pregame eligibility.

## Reproduce and score

```powershell
python -m nba_quant demo
python -m nba_quant forecast path/to/request.json --output output/forecast.json
python -m nba_quant walk-forward DATASET_ID --min-history 10 --limit 20 --simulations 100
python -m nba_quant evaluate --output output/evaluation.json
python -m pytest -q
```

A request contains `dataset_id`, `home_id`, `away_id`, timezone-aware `tipoff` and `cutoff`, `simulations` (20–5000), `seed`, and optional `scenarios` with `player_id`, `active_probability` and/or `minutes_cap`. Cutoff must precede tipoff and cannot be in the future. The walk-forward command freezes every forecast before attaching its outcome. Historical local replay is not prospective evidence. Demo history is generated by the same baseline: its evaluation tests the pipeline, not predictive skill.

To score a saved forecast against an imported final game, POST `{ "dataset_id": "...", "game_id": "..." }` to `/api/forecasts/FORECAST_ID/outcome`. Teams and tipoff must match exactly; synthetic and observed data cannot cross. Outcome rows are immutable. Keep separate datasets for corrected sources and review discrepancies rather than overwriting evidence.

## Model boundaries

These are fixed, transparent research priors, not a trained production model. Team offense is diagnostic; player empirical shooting/usage and opponent defensive estimates drive scoring. Default action labels, zones and archetypes are diagnostics, not learned scheme/defender effects. No injury classifier, trained minutes regression, foulout/and-one/intentional-foul rules, tactical substitutions or live conditioning is implemented. Rotation granularity is half a minute; restricted caps round down to that grid. `minutes_cap=48` means unrestricted into overtime; values below 48 are whole-game caps. An infeasible overtime roster fails instead of breaking restrictions. Availability draws are conditioned on sufficient players/minutes to field a legal game; realized rates are reported and can differ from input marginals.

See [`docs/IMPLEMENTATION.md`](docs/IMPLEMENTATION.md) for blueprint coverage and remaining work. SQLite is local persistence, not a multi-user production deployment. No secrets or provider credentials are required for the demo.
