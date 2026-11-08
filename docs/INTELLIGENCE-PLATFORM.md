# Case NBA Quant Intelligence Platform

A play-by-play-driven NBA research terminal: player, team, lineup and matchup intelligence, a vectorized possession simulator, and walk-forward model audit. Python, SQLite, NumPy, FastAPI and vanilla JavaScript. **No LLM is used at runtime.**

Open `http://127.0.0.1:8020/terminal` after `python -m nba_quant serve`. The original SGP research workspace remains at `/`.

## 1. Repository audit (starting point)

The repository already had a source-aware SGP research terminal: an immutable provenance store (`nba_quant/store.py`), a pure-Python joint event simulator (`simulation.py`), walk-forward forecast scoring (`evaluation.py`), optional AutoGluon/XGBoost shadow models (`ml_*`), a read-only ESPN box-score adapter, Polymarket/FanDuel readers, and a 440-slot metric catalog in which almost every slot was `UNAVAILABLE`. Its data was box-score only: there were no possessions, lineups, shots or on/off splits.

Decisions:

- **Keep and reuse.** The SGP workflow, store, tests and CI are untouched. The new platform is a separate package, `nba_quant/intel/`, mounted on the same FastAPI app.
- **New data layer.** ESPN's public summary endpoint includes full play-by-play: substitutions, shot coordinates, shot types, and assist, block and steal credits. That makes real possessions, lineup stints, on/off splits, RAPM and an expected-FG model possible. NBA Stats (`stats.nba.com`) timed out from this environment, and `cdn.nba.com` returned HTTP 403.
- **New simulator.** The existing engine loops over games in Python (capped at 5,000 sims). The new engine is vectorized across simulations, so 50,000 sims finish in roughly 30 seconds.

## 2. Architecture

```
ESPN summaries (cached, SHA-256)  ->  acquire.py
        |
parse.py  (box, plays, market line)        reconstruct.py (lineups, stints, possessions, shots, validation)
        \______________________________________/
                         |
warehouse.py  (SQLite: games, periods, player_games, play_events, shots, possessions,
               lineup_stints, market_lines, metric_snapshots, model_versions,
               model_predictions, simulation_runs, validation_results, data_sources, ...)
                         |
models.py   xFG logistic | RAPM ridge | team ridge | lineup shrinkage
engine.py   100 universal + 100 positional + 240 team metrics, percentiles, SE, trends, archetypes
projection.py  availability model -> minutes -> rates -> four-factors anchoring -> team-ridge+RAPM target
sim.py      vectorized possession simulator (shared events -> boxes, scores, winner)
backtest.py walk-forward forecasts, scoring, walk-forward Platt recalibration, market benchmark
api.py      /api/intel/*  ->  static/terminal.{html,js,css}  (six workspaces)
```

Each layer feeds the next: player profiles feed the lineup models; lineup stints feed RAPM; RAPM and the team ridge model set each team's simulated efficiency; and one simulated event stream produces every player box, team score and the winner.

## 3. Data

| Item | Value (verified 2026-11-08) |
|---|---|
| Provider | ESPN public site API (secondary statistics provider, not the official NBA feed) |
| Games | 1,451 (2025-26 regular season 1,231 incl. Cup final; 2025-26 playoffs 81; 2026-27 through Nov 7: 139) |
| Play-by-play validated | 1,244 games pass every reconstruction check |
| Rows | 708k play events · 259k shots · 292k possessions · 50.5k lineup stints · 38k player games |
| Rejected | 4 provider 404s at acquisition; All-Star exhibitions excluded (franchise IDs 1–30 only) |

Commands:

```bash
python -m nba_quant.intel acquire 2025-10-21 2026-06-25   # cache summaries (gzip, SHA-256)
python -m nba_quant.intel acquire 2026-10-20 2026-11-07
python -m nba_quant.intel build                           # incremental ingest
python -m nba_quant.intel coverage
```

Runtime data lives under `output/` (gitignored) and is rebuilt from the cache. A fresh clone has no warehouse; the terminal says so and prints the commands above.

### Reconstruction rules and validation

- **Lineups.** Period-1 lineups are the box-score starters. Later periods are inferred by timestamp: a player who is subbed out, or acts, strictly before their first sub-in was on court at the period start. ESPN sometimes lists a substitution after an action recorded at the same clock, so timestamps are used instead of row order.
- **Missing substitutions.** When a player acts while recorded as on the bench, the player swapped out is the one whose next involvement is entering the game. Repairs are counted per game (median 2). Games with more than 8 repairs, or a player-minutes error of 1.5 minutes or more, are flagged.
- **Possessions.** A possession is a maximal run of events owned by one team inside a period. An and-one free throw stays in its possession; technical free throws are counted outside possessions. The reconstructed pace of 100.7 possessions/team/game matches the league.
- **NBA 2024-25 heave rule.** A missed shot from 36 ft or more in the final 3 seconds of quarters 1–3 is a team attempt, not a player FGA. This reconciled 222 → 48 FGA mismatches; the remainder are provider attribution errors.
- **Checks stored per game:** possession points + technical points = final score; stint points = final score; lineups always five; stint seconds = game length; shot count vs box FGA; player-minutes MAE.

### Unavailable sources (registered, never synthesized)

- Optical tracking: speed, distance, touches, defender distance, deflections, boxouts, matchups.
- Synergy play types: PnR, isolation, spot-up, post-up and similar PPP.
- Defensive coverage tagging: drop, switch, hedge, blitz, zone.
- Historical injury reports.

The `tracking_events` and `player_matchups` tables exist but are empty.

## 4. Metric engine

`nba_quant/intel/catalog.py` defines every slot in the specification. Each slot has a formula, unit, sample denominator, SE rule, direction, and one of five classes:

| Class | Meaning |
|---|---|
| OBSERVED | Provider box-score value |
| DERIVED | Deterministic formula over reconstructed play-by-play |
| ESTIMATED | Documented proxy (e.g. transition = first action ≤ 8 s after DREB/steal; catch-and-shoot = plain jump shots) |
| MODELED | Fitted-model output (xFG, RAPM, team ridge, minutes) |
| UNAVAILABLE | No authorized source; `null` plus a reason |

| Scope | Slots | Measurable |
|---|---|---|
| Universal player | 100 | 67 |
| Positional (PG/SG/SF/PF/C) | 5 × 20 | 38 |
| Team | 240 | 187 |

Each value carries: sample size, an approximate SE, a direction-adjusted league/position/role percentile, a last-10-games trend, the previous-season baseline, and an opponent-adjusted version where defined. Team metrics support season, venue, last-N and opponent filters, and report a rank and league average.

Archetypes are multi-label (14 roles) and assigned by documented thresholds on derived rates. They are labeled heuristic, not fitted.

## 5. Models (all fitted only on games before the cutoff)

| Family | Method |
|---|---|
| Shot quality (xFG) | L2 logistic, Newton/IRLS; distance hinges, zone, shot family, possession clock, second chance, transition, game state. Holdout (last 15% of shots): log loss 0.637 vs 0.689 base rate, well calibrated |
| RAPM | Possession-weighted ridge on stints, separate O/D coefficients + home court, λ = 3000 (APM λ = 300). Posterior SD reported |
| Team ratings | Time-decayed (60-day half-life) ridge of per-100 scoring on offense, opponent defense and home court; margin σ from residuals |
| Lineups | Adjusted net = (poss·net + 400·ΣRAPM)/(poss + 400); interaction = adjusted − talent prior |
| Availability | Laplace-smoothed P(plays) by missed-game streak × recent-minutes role, fitted on player-team sequences |
| Minutes | EWMA (half-life 5 games) shrunk to the window mean; SD from the player's history |

## 6. Simulation (`sim.py`)

- **Rotations.** 48 one-minute blocks (5 per OT period). Each block's five players are drawn by systematic PPS sampling with inclusion probability minutes/48. This always gives exactly five distinct players and 240 team minutes.
- **Minutes allocation.** Sampled minutes are trimmed from the bottom of the rotation first.
- **Possession flow.** Usage-weighted actor → turnover (with optional steal) | shooting-foul trip | FGA. A FGA takes its zone from the player's shrunk shot mix, and its make probability from shrunk zone FG% + the opponent's zone-defense logit shift + a team calibration shift. Then come blocks, assists (from four eligible teammates) and and-ones; misses go to a rebound decided by lineup rebounding balance; an offensive rebound continues the possession.
- **Team anchoring.** The calibration shift is solved from two pilot runs so each team's simulated points per possession hit the team-ridge matchup expectation plus the RAPM change implied by projected availability. Turnover and foul-trip rates are anchored to a four-factors matchup (team rate × opponent allowed rate ÷ league).
- **Asserted on every run.** Points = 2·FGM + 3PM + FTM; team score = Σ player points; minutes = 240 + 25·OT; assists ≤ FGM; steals ≤ opponent turnovers; blocks ≤ opponent misses.
- **Outputs.** Mean, median, SD and p05–p95 for every box stat and for PRA/PR/PA/RA/stocks summed inside each simulated game; threshold ladders; double/triple-double rates; joint correlations; quarter scoring; margin/total histograms; spread/total ladders; MC standard error.
- **Performance.** 1k sims ≈ 3 s, 10k ≈ 6 s, 50k ≈ 32 s (4 vCPU, including the two calibration pilots). Results are cached by input hash and seeded.
- **Limitations.** No foul-outs, bonus free throws, intentional fouling, shot clock or in-game score effects.

## 7. Validation protocol (`backtest.py`)

- Models and rate aggregates are refit weekly at Monday 12:00 UTC. Each game's roster, minutes and availability inputs use games that tipped at least 1 hour before it.
- Forecasts are stored in `model_predictions` before scoring.
- Platt recalibration is itself walk-forward (fit on earlier weeks only).
- DraftKings lines embedded in ESPN summaries are used **only as a benchmark**. Their observation time is not stated.

```bash
python -m nba_quant.intel backtest 2025-11-17T12:00Z 2026-06-25T00:00Z --sims 2000 --workers 3
```

Measured numbers are in section 8.

## 8. Results

Measured on 2026-11-08 with `possession-sim-1.0`, 2,000 simulations per game and weekly refits. Every number below is walk-forward on archived data, not a live prospective record. No historical injury reports were used.

### Games: 2025-26 (Nov 17 → Finals, 1,113 games, 0 forecast failures)

| Win probability | Log loss | Brier | Accuracy |
|---|---|---|---|
| Coin flip | 0.6931 | 0.2500 | — |
| Simulation (raw) | 0.6125 | 0.2117 | 66.4% |
| Team ridge ratings only | 0.6085 | 0.2097 | 68.8% |
| Simulation + walk-forward Platt (958 games) | **0.6041** (raw 0.6117 on the same games) | 0.2086 | — |
| DraftKings moneyline, de-vigged (1,055 games, benchmark) | **0.5764** | — | — |

| Scores | Model | Market (1,060 games) |
|---|---|---|
| Margin MAE | 11.84 | 11.19 |
| Total MAE | 15.20 | 14.58 |
| 80% interval coverage, margin / total | 86.7% / 81.6% | — |
| Bias, margin / total | +0.08 / +0.80 | — |

**Calibration finding.** The raw simulator is under-confident. Forecasts in the 0.6–0.7 bin won 71.6% (predicted 64.6%); the 0.7–0.8 bin won 84.5% (predicted 74.2%); the 0.3–0.4 bin won 22.1% (predicted 35.8%). The Platt fit on all 2025-26 forecasts has slope 1.57.

**Out-of-sample check.** Applied unchanged to the 2026-27 run (96 games, Oct 27 – Nov 7), it lowered log loss from 0.6279 to 0.6144. Live projections now report this calibrated probability next to the raw simulated share.

### Games: 2026-27 to date (96 games)

Raw log loss 0.6279 (ratings-only 0.6288), Brier 0.2186, margin MAE 11.89 (market 10.70), total MAE 15.06 (market 14.51).

### Players: 2025-26 (23,688 player-games that were played; MAE conditional on playing)

| Stat | Model MAE | Last-10 average MAE | 80% interval coverage (all rostered, DNP = 0) |
|---|---|---|---|
| Points | 4.68 | 4.65 | 88.1% |
| Rebounds | 2.00 | 1.93 | 89.6% |
| Assists | 1.43 | 1.37 | 91.7% |
| 3PM | 0.92 | 0.90 | 93.6% |
| Steals | 0.74 | 0.74 | 94.7% |
| Blocks | 0.54 | 0.51 | 95.4% |
| Turnovers | 0.91 | 0.91 | 94.2% |
| Minutes | 5.13 | 5.12 | 89.8% |

Availability (played vs not, 34% DNP base rate among rostered players): Brier 0.144, log loss 0.453.

### What this means, plainly

- **Game-level skill is real.** Log loss is well below a coin flip and stays that way out of sample.
- **The model does not beat the closing-quality market.** Both margin MAE and moneyline log loss trail the DraftKings line. Treat the model as a research and scenario tool, not an edge.
- **Player means are no better than a last-10-games average**, and the intervals are too wide (they cover 88–95% instead of 80%). The next work items are reducing simulator dispersion (per-player shooting noise and minutes SD) and adding real availability data.
- **No model was auto-promoted.** The calibrated win probability is the only post-hoc change, and it was validated out of sample before it was enabled.

## 9. Terminal workspaces

| Workspace | Content |
|---|---|
| A Player | Search; 100 universal + positional metrics with class, percentiles, SE, trend, opponent-adjusted value; archetypes; shot chart (makes, xFG, above-expected heat) with zone table; game log; next-game simulation |
| B Team | 240-slot explorer by category with rank and league average; venue/last-N/opponent filters; rolling net; quarter splits; top lineups; offense and allowed shot charts |
| C Lineups | Five-man units with raw/adjusted net, talent prior, chemistry, uncertainty, rebounding/turnover rates, role flags; include/exclude (injury substitution) filters; two-lineup comparison |
| D Matchup | Zone-by-zone offense vs defense edges, four factors, shot quality, transition, key players vs opponent zone defense, data-derived tactical notes |
| E Prediction | ESPN schedule picker; 1k/10k/50k sims; seed; cutoff; per-player availability and minutes-cap scenarios; score/win/margin/total, histograms, quarter table, team box, provenance and identity checks, player distributions, threshold explorer, correlations, drivers |
| F Audit | Walk-forward scores, calibration plots (raw and walk-forward Platt), market benchmark, segments, player MAE vs last-10 baseline, availability scoring, model registry, xFG reliability and coefficients |

## 10. Limitations and next priorities

1. **Historical injury reports.** Availability is modeled from participation only. Ingesting time-stamped official reports is the largest expected gain for player forecasts.
2. **Tracking and play-type data** require a license. 53 team, 33 universal and 62 positional slots stay `UNAVAILABLE` until then.
3. **Tuning.** Shrinkage constants, RAPM λ and simulator priors are fixed, documented choices that have not been tuned. Tuning them on the walk-forward set (with a held-out season) is the next modeling step.
4. **AutoGluon/XGBoost.** These remain in the existing shadow framework (`ml_*`) and are not wired into the intelligence stack. Any promotion must beat the walk-forward numbers above on the same games.
5. **Production.** This is a single-node SQLite deployment. Moving to Postgres + Parquet and scheduled ingestion (`acquire` + `build` nightly) is straightforward but not done.
