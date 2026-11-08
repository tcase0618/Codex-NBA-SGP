# Blueprint coverage

The source blueprint is a system design, not a supplied historical dataset or trained model. This implementation delivers a runnable research baseline and records unsupported capabilities explicitly.

| Blueprint area | Implemented | Remaining |
|---|---|---|
| A1–A3 player intelligence | Position/archetype schema, 100 universal + 100 specialized definition slots, source-backed box-score aggregates | Operational definitions/provider mappings for all other slots; role mixture estimation |
| A4 team intelligence | 240 definition slots, entity profiles, shooting, rebounding, pace and ratings from imported box scores | Full 30-team historical dataset; schedule-adjusted strength, tracking/play-type/context/lineup-history metrics |
| A5 data foundation | Immutable raw canonical datasets, games/player_game/team aggregates, source metadata, rotation snapshots, metric definitions, frozen forecasts/outcomes | Provider adapters; historical possession/PBP reconstruction, substitution and shot chart tables, licensed tracking |
| B1–B5 player opportunity | Empirical-Bayes player rates, active scenarios, cap-constrained stochastic minutes, event-linked shots/assists/rebounds | Trained availability/minutes/usage/shot-quality models; functional-role interactions |
| B6 team forecasting | Empirical pace; opponent defensive adjustment; joint score/winner sampling | Opponent-adjusted fitted strengths, travel/rest, learned team/player blend |
| B7 event simulation | Five-player lineups, shared event ledger, regulation/OT, scoring/assists/rebounds/turnover consistency | Exact shot/game clock, foulouts, and-ones, technicals, goaltending, timeouts, intentional fouls, tactical substitutions |
| B8 distributions | 10 player stat distributions, thresholds, score intervals, winner sampling error and covariance | Calibrated model uncertainty, more robust joint-tail validation |
| B9 scenarios/matchups | Availability/cap overrides; position and archetype display; explicit unavailable defender tracking | Scenario mixture calibration, defender/coverage effects, learned lineup chemistry |
| B10 training | Descriptive historical counts with fixed shrinkage priors | Fitted classifiers/regressions/multinomial models and tuning on historical holdouts |
| B11 validation | Cutoff tests, expanding-window replay, immutable scoring, proper scores and reliability bins | Independent NBA sample, segment evaluation, drift alerts, fitted calibration, production acceptance thresholds |
| B12 production | Local server, source checks, reproducible export, unit/integration/ledger tests | Production security/ops, monitoring, recovery drill, deployment |
| B13 ten terminal panels | Outcome, boxes, distributions, rotations, scenarios, covariance, baseline drivers, frozen audit; unavailable states for matchup/live | Real matchup intelligence and live state updates |

## Data states

- `DERIVED`: implemented descriptive formula over imported box scores, with source and sample count.
- `ESTIMATED`: available formula using a possession/free-throw approximation.
- `SYNTHETIC_DERIVED`: formula over fictional generated data; never an NBA observation.
- `UNAVAILABLE`: missing input or unimplemented formula, represented by null.
- `NOT MEASURABLE`: no scored forecasts; no accuracy claim.

The catalog contains planned metric slots, not 440 completed analytical models. Source requirements in the catalog are preliminary routing hints; each unavailable slot requires a reviewed definition and provider mapping before activation.

## Validation strategy

Unit tests independently rebuild scores and box scores from the event ledger instead of copying internal calculations. They verify five-player assignments, regulation and overtime minutes, actual tie resolution, shot and assist eligibility, rebound conservation, restrictions and deterministic seeds. Provenance tests exercise actual persistence triggers, replacement attacks, corrupted content hashes and information available at cutoff. API integration tests cover import → forecast → persisted reload/export → profiles. Evaluation tests check scoring calculations and cohort boundaries.

No real-NBA accuracy, calibration, ROI or operational readiness should be inferred from these software checks.
