# NBA prediction engine: engineering report

## Delivery status

Implemented and running locally: an NBA-only AutoGluon/XGBoost research pipeline integrated into the existing terminal. It operates without Codex, Claude, an LLM, GPU, or a paid API. Open the **Predictions & models** navigation tab at http://127.0.0.1:8020/.

**Production predictive approval remains BLOCKED.** Installation, training, persistence and serving succeeded; a validated prospective advantage did not. No wager execution or production promotion occurred. Existing aggressive parlay construction and the standard-under/over-alternative policy remain intact.

## Architecture

The pre-implementation audit is in ML-ARCHITECTURE-AUDIT.md. Existing FastAPI/Pydantic/NumPy, SQLite/WAL, vanilla frontend, source validation and immutable records were preserved. Added logical modules handle PIT features, target registry, offline training/evaluation, versioned serving and audit persistence. No new service or database server was introduced.

`ml_features` retains original dataset revisions rather than relying on a latest-only merged snapshot. Strict inputs require completion and original knowledge before cutoff. Late source retrieval/import timestamps further prevent prospective eligibility. Explicit event-time replay preserves original timestamps and remains retrospective. Features and outcome labels are separate. Entire Eastern-date NBA slates stay in one fold, including West Coast games across UTC midnight. Earlier groups whose labels are unavailable at the next fold boundary are purged.

`ml_engine` compares historical/recent baselines, AutoGluon TabularPredictor and standalone XGBoost. AutoGluon uses limited RF/ExtraTrees/LightGBM candidates and a tuning-based weighted ensemble, without bagging, stacking, neural models or duplicate XGBoost. The specialist supports regression/binary classification, bounded depth tuning, regularization, validation early stopping and native JSON persistence with the selected iteration count. Champion selection uses tuning only; calibration and final test are separate. Final-test comparisons cannot change that run's champion.

The final framework adds code hashes, train-only feature summaries, candidate inference and stronger governance. Authentic replay results below remain frozen; they were not rerun to improve scores after inspecting the holdout. Their manifests predate some final diagnostic fields. Final-code package/restoration checks use a separate synthetic fixture cohort.

## Target inventory

| Scope | Implemented targets | Current model status |
|---|---|---|
| Player | points, rebounds, assists, threes made/attempted, steals, blocks, turnovers, minutes, FGA, FTA, PRA | Points trained retrospectively; other targets configurable but untrained |
| Team/game | team points, opponent points, margin, total, win | Win trained retrospectively; others untrained |
| Estimated performance | pace, offensive/defensive rating proxies | Implemented when both teams' FGA/FTA/OREB/TOV exist; untrained |

There are **20 targets**. Rebounds and PRA are derived exactly. Possessions use the explicitly labeled 0.44-FTA approximation, not measured tracking/play-by-play. Missing inputs suppress unsupported targets; they never become fabricated zeros. NFL/NHL modules were not introduced.

Data audit: 116 distinct authentic finals, five immutable datasets including one excluded synthetic dataset. Strict mode produces **zero player rows** because historical pregame rosters are absent, and **32 team rows from 16 games**, below the 20 independent-game training gate. Both strict training attempts failed safely. Replay produces 1,368 player rows and132 team rows after history checks. Its observed-box participant population adds selection bias and is not a verified pregame roster.

## Features and uncertainty

Shared transformations include rolling/recent production, standard deviations, per-minute rates, minutes/opportunity history, own/opponent scoring and estimated efficiency/pace, rest, short rest, home/away and prediction horizon. Defaults are ten prior games and three eligible history games. Timestamped roster/rotation fields are optional with individual missing flags. No historical injury, lineup or tracking feature is invented.

Regression uses dedicated-calibration absolute-residual conformal intervals, configurable at training time. Count estimates and interval lower bounds cannot be negative; margins can. Minutes respect the canonical supported 100-minute upper bound. Binary outputs use Platt calibration only with sufficient class support. Metrics include MAE/RMSE/bias, Brier/log loss, empirical interval coverage and reliability bins. Label dispersion and zero frequency are recorded; a universal Poisson assumption is avoided. Temporal drift and within-game dependence can invalidate nominal conformal coverage.

Threshold-exceedance probabilities remain **UNAVAILABLE** until a full outcome distribution is validated. Independent models are not a coherent joint box-score forecast. No multiplication of marginal probabilities is presented as a joint SGP probability. Existing coherent simulation remains separate.

## Actual out-of-time replay results

Season2026-27; retrospective event-time reconstruction. Final holdout: **16 independent games**, Nov4-Nov5 Eastern, comprising370 player rows and32 team-perspective rows. These rows are not independent games.

| Candidate | Points MAE | Win raw log loss |
|---|---:|---:|
| Historical baseline | 4.1940 | 0.7113 |
| Recent baseline | 4.5135 | 4.9068 |
| AutoGluon | 4.7142 | 0.7185 |
| XGBoost | 4.5346 | 0.6777 |

Lower is better. XGBoost won tuning selection for both targets, but **lost to the historical points baseline on the holdout**. After dedicated calibration, win log loss was0.6929 and Brier0.24988. Raw candidate comparison and calibrated champion metrics describe different outputs.

Points RMSE5.8169, bias+0.2857, empirical90% interval coverage93.51%, mean clipped interval width18.31 points. The wide intervals and small independent sample do not establish actionable advantage. Splits:32 training,4 tuning,14 calibration,16 test games. An initial within-night split failed safely; grouping whole Eastern slates resolved the empty tuning fold while preserving purging.

Frozen versions: points `20261106T214514-023833916fe2`; win `20261106T214522-2636bd398278`. Manifests, checksums, models and frozen evaluations are under output/ml/models. Synthetic fixtures are separately stored under output/ml/software-acceptance.

Existing simulator comparison is not measurable without adequate historical rotation evidence. Expanding and rolling backtest infrastructure is implemented with disjoint test-day windows. Actual expanding replay ran two folds per target across34 distinct test games:18 and16 games. Points champions had MAE4.8207 and4.6332, against historical baselines4.6445 and4.1940. Calibrated win log losses were0.6992 and0.7057. These additional runs reuse the previously examined historical archive: they are exploratory and do not create fresh untouched prospective evidence. Reports stay separate; shared games across reports are never pooled as independent observations. Multi-season and meaningful role segmentation remain uncompleted.

## Installation and training

From this repository in PowerShell:

```powershell
python -m venv .venv-ml
.\.venv-ml\Scripts\python.exe -m pip install -e '.[ml,test]'
.\.venv-ml\Scripts\python.exe -m pip check
.\.venv-ml\Scripts\python.exe -m nba_quant.ml_cli audit
.\.venv-ml\Scripts\python.exe -m nba_quant.ml_cli train --target pts --target win --mode strict --time-limit 90 --cpus 1 --interval-level 0.9
.\.venv-ml\Scripts\python.exe -m nba_quant.ml_cli backtest --target pts --mode retrospective_event_time --initial-days 6 --test-days 2 --time-limit 30
```

Strict training currently refuses insufficient history. Explicit research replay uses `--mode retrospective_event_time`. Repeat `--target` for additional supported targets. Defaults:60 seconds per target, one CPU,600MB currently available RAM; configured maxima600 seconds/two CPUs. Imports, importance and serialization add wall time. Optional PyTorch/CUDA detection warnings occurred, but CPU tree models trained successfully without PyTorch.

Backtests expand training history by default; `--window-days` selects rolling history and `--baselines-only` omits optional ML training. Each fold has separate training, tuning, calibration and test windows. Test windows cannot overlap within a report. Reports and fold models are persisted under output/ml/backtests; later folds may train on earlier completed holdout games. Model sizes/CPU/time are bounded; no hard OS memory isolation or general runtime SLA is claimed.

`--if-new-data` supports scheduled invocation and skips only a previously successful identical data/configuration/code hash. An OS process lock prevents concurrent training. Windows Task Scheduler can invoke the same executable/module with absolute database/output paths and this repository as its working directory. Scheduling remains disabled; no silent daily training was registered.

Actual resolved versions are in docs/requirements-ml-windows.lock.txt (portable package pins) and output/ml/requirements-lock.txt (original environment receipt). ML extras pin AutoGluon1.6.3 and XGBoost3.2.0; the original application environment was not upgraded. References: [AutoGluon installation](https://auto.gluon.ai/stable/install.html), [TabularPredictor training](https://auto.gluon.ai/stable/api/autogluon.tabular.TabularPredictor.fit.html), [XGBoost persistence and early stopping](https://xgboost.readthedocs.io/en/stable/python/python_intro.html).

## Inference and operations

```powershell
.\Start-NBAQuant.ps1
.\.venv-ml\Scripts\python.exe -m nba_quant.ml_cli status
.\.venv-ml\Scripts\python.exe -m nba_quant.ml_cli predict output/ml/live-shadow-request.json
.\.venv-ml\Scripts\python.exe -m nba_quant.ml_cli reconcile
```

The launcher prefers .venv-ml when present. Versioned routes: GET `/api/ml/v1/status`, `/api/ml/v1/models`, `/api/ml/v1/backtests`, `/api/ml/v1/history`; POST `/api/ml/v1/predict`, `/api/ml/v1/reconcile`. No training runs during frontend requests.

Requests specify NBA sport, explicit model version, exact game/home/away identity, team/player ID, target, tipoff, as_of and pregame horizon. Future cutoffs, post-tipoff requests, mismatches and unavailable required features fail closed. Current schedule and player roster must be source-verified and fresh. Forecasts return provenance/freshness, model ID/version, generation time, intervals/probabilities when supported, explicit shadow status and error indicators. An optional threshold deliberately returns unavailable until validated distributions exist.

Successful forecasts append request, input hash, provenance, latency and descriptive drift flags where train summaries exist. Failed inference/training is recorded. No stale prediction cache exists: inputs and artifact checksums are rechecked. Reconciliation appends matched actual labels without editing forecasts. Monitoring explicitly covers the latest100 operations/predictions. Calibration trends can be inspected across stored versions; this is not a comprehensive monitoring/alerting service.

## Governance, recovery and security

Before/after content hashes match for all original records:5 datasets,1 forecast,1 SGP evaluation,96 draft bundles. Three new append-only SQLite tables hold ML predictions, operations and outcomes. Existing origin checks, CSP and localhost binding apply. No administrative web training/promotion endpoint or new credential was added.

Artifacts must come from trusted local training. Paths, symlinks, manifests and hashes are checked before loading. Checksums detect corruption, not replacement of both artifacts and receipts. AutoGluon includes executable pickle persistence: never load untrusted/downloaded model directories. Incomplete/corrupt runs are isolated from usable registry results.

Versions start in shadow mode. Hash-chained registry events use a process lock. Explicit promotion requires strict timing, calibration/coverage gates,100 prospective games across20 Eastern day cohorts, at least5% paired improvement, and a positive lower95% day-block bootstrap improvement bound. Evidence remains locally supplied operator input, not independently authenticated upstream proof. No model qualifies. Serving remains shadow-only; automatic production routing awaits a validated operating policy.

```powershell
.\.venv-ml\Scripts\python.exe -m nba_quant.ml_cli registry shadow <model-id>
.\.venv-ml\Scripts\python.exe -m nba_quant.ml_cli registry promote <model-id> --evidence <audited-evidence.json>
.\.venv-ml\Scripts\python.exe -m nba_quant.ml_cli registry rollback <previously-promoted-model-id>
```

Rollback rejects never-promoted versions; retained research versions can still be selected explicitly. AutoGluon and XGBoost use Apache-2.0 licenses. Public endpoint availability does not establish ESPN/NBA/market-data redistribution rights; those rights were not verified. No paid service or external deployment was introduced.

## Measurements and testing

Windows11 Home/Python3.11.15; eight logical CPUs,16.76GB RAM total. Available memory was about0.95GB at audit and1.42GB during runtime verification; final free disk15.78GB. Two authentic model directories total2.80MB. Small synthetic regression/binary train-save-new-process-restore acceptance runs took16.91s/15.69s; these timings do not establish an NBA training SLA or predictive accuracy.

Both packages installed/imported; pip check passed. Both trained regression and binary fixtures. Champion, AutoGluon and XGBoost predictions matched after separate-process restoration. Synthetic promotion was blocked. The real application was restarted; its saved real-game shadow forecast survived and the same request produced identical predictions. Original record hashes remained unchanged. Dashboard harness and JavaScript syntax checks passed. Browser visual QA remains UNVERIFIED. No broker/wager action or external deployment occurred.

Final suite: **590 passed in20.16 seconds**, with the existing Starlette/httpx deprecation warning. Exact output and receipts: output/ml/full-tests.txt, application-verification.json, software-acceptance/verification.json, dashboard-check.txt. Reconciliation tests cover exact teams/tipoff, idempotence and future-outcome rejection; forecast tests reject schedule observations after cutoff. Synthetic checks establish software behavior only.

## Implementation inventory

Created: nba_quant/ml_features.py, ml_engine.py, ml_service.py, ml_cli.py, ml_backtest.py; tests/test_ml_features.py, test_ml_engine.py, test_ml_service.py, test_ml_backtest.py; scripts/check_ml_runtime.py; architecture audit, dependency lock and this report.

Modified: nba_quant/api.py; existing frontend HTML/JS/CSS; pyproject.toml; .gitignore; Start-NBAQuant.ps1; README.md. Evidence/model outputs are under output/ml and excluded from source tracking.

## Remaining work

1. Collect durable historical pregame roster/availability observations and more verified, appropriately licensed seasons with true observation timestamps.
2. Run larger multi-season chronological/expanding-window and role-segment evaluations against baselines and the existing simulator. Reserve a new untouched final period; do not tune repeatedly on these16 games.
3. Accumulate prospective frozen shadow predictions/outcomes and evaluate calibration, coverage and drift before production eligibility.
4. Validate threshold distributions and coherent joint targets before connecting ML probabilities to SGP construction.
5. Establish deployment-specific production routing, alerts, scheduling and recovery after validation. No external deployment environment was verified.

The research software is usable now. Production forecasting acceptance remains incomplete for these specific data and validation reasons.
