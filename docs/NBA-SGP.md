# NBA SGP builder — FanDuel

## Tonight's games

Open `http://127.0.0.1:8020/#tonight` for the nightly board. It uses an independently fetched ESPN scoreboard and Eastern calendar dates, including games whose UTC tipoff falls on the next day. Each matchup displays start/status, historical coverage, matching non-synthetic forecasts, and every saved SGP evaluation tied to that exact home/away pair and tipoff. Reversed teams, other dates, unknown datasets and fictional demo forecasts cannot populate a real-game card.

Refresh retrieves current schedule evidence. The source response is archived and verified; a failed refresh can show explicitly stale last-good evidence, never a fictional replacement. An empty provider response is distinct from an established league schedule. The board lists saved research evaluations, not all mathematically possible combinations or a live FanDuel catalog. With no current matching forecasts or captured offered props, it shows those gaps rather than inventing tickets. Completed/in-progress matchups cannot open a pregame construction flow.

## Current state

The builder is implemented for NBA research with FanDuel as the target sportsbook. It does not claim extreme predictive accuracy. Reliable probabilities require held-out calibration, current availability evidence and offered ticket prices. Authentic data is necessary; it does not alone establish a betting advantage.

Verified ingestion on November 4, 2026: **100 ESPN regular-season final games, 30 teams and 465 player identities**. All 107 discovered completed games were requested. Seven were rejected: six inconsistent final-event/header scores, one missing statistic. Current roster sources now cover 28 teams and 486 players. See [automatic data and construction](AUTOMATIC-DATA.md) for the current workflow and published FanDuel quotes. ESPN is a secondary statistics provider, not the official NBA feed.

The original response bytes are preserved with SHA256 hashes. Retrieval timestamps are actual observation times; they are never backdated to game time. Whole-minute values remain as supplied, with a declared minute-resolution tolerance rather than invented corrections. G/F positions remain broad source labels. No current roster, eligibility, injury probability or rotation is inferred from historical participation.

An official NBA injury PDF published November 4 at 4:45 p.m. Eastern was downloaded and hashed. The raw report is displayed as **UNREVIEWED** for player identity mapping and cannot automatically approve eligibility. The index HTTP request returned403; the explicit official PDF URL obtained from the verified official page was accessible. That file is a dated snapshot, not proof of the latest injury status.

## Ticket calculations

Choose an existing frozen forecast explicitly, then add 2–12 legs. Supported statistics: points, rebounds, assists, threes, steals, blocks, turnovers, PRA, points+rebounds, points+assists and rebounds+assists. Over/under uses half-lines; integer “at least” thresholds are supported. Pushes and sportsbook void/DNP/repricing rules are not modeled, so unsupported integer over/under markets are rejected. Overlapping component markets for the same player are rejected.

All leg outcomes are checked against the **same game sample index**. Ticket probability is the fraction of simulations where every leg succeeds. The product of marginal probabilities is displayed only as a dependence diagnostic. Wilson intervals quantify sampling noise, not forecasting accuracy. More draws cannot repair a bad model.

The current simulator remains an untrained empirical baseline. Its demonstrations are synthetic. The new historical dataset supports descriptive research; its lack of verified rotation snapshots intentionally blocks real-game forecasting. This boundary prevents copying old participants into a purported current lineup.

## FanDuel prices

No odds subscription or credential is configured. Automatic primary FanDuel acquisition retrieves a partial published prop selection, with unknown price-update times. No current offered line or combined FanDuel SGP price has been verified. Exact combined quote entry remains an advanced fallback; the primary path fetches available data automatically. Missing, stale, mismatched or synthetic prices cannot produce an actionable signal. Manual quote provenance remains a caller assertion until independently verified.

The optional [The Odds API v4 adapter](https://the-odds-api.com/liveapi/guides/v4/) reads event-level NBA props with FanDuel selected. Its individual leg prices do **not** provide a combined SGP price. Player names remain unmapped until a reviewed crosswalk associates provider IDs, team and event with model identities. There is no automatic fuzzy name join.

If a subscription is chosen later, set `THE_ODDS_API_KEY` securely in the server environment; never paste it into chat or a committed file. Requests may consume provider quota. The adapter keeps credential text out of source URLs, archives and exception messages. No subscription was purchased or account created.

```powershell
python -m nba_quant odds-events --output output/nba-odds-events.json
python -m nba_quant odds-props PROVIDER_EVENT_ID --bookmakers fanduel --output output/fanduel-props.json
```

Archive an explicitly verified official injury report:

```powershell
python -m nba_quant injuries --url 'https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-04_04_45PM.pdf'
```

Refresh historical data with bounded requests, inspect the rejection manifest, then import the resulting immutable dataset:

```powershell
python scripts/acquire_nba_history.py --start 2026-10-20 --end 2026-11-03 --max-games 100 --output output/authentic-history
python -m nba_quant import PATH_FROM_DATASET_MANIFEST
```

## What still gates actionability

1. Larger historical coverage and independently verified data quality, with historical availability/rotation snapshots available at each cutoff.
2. Trained minutes, opportunity and joint outcome models evaluated on held-out later games; no postgame participation filtering.
3. Prospective prop and **ticket-level** calibration, including uncertainty, market type/line, lineup changes, rare events and selection effects. Single-leg hit rates do not validate SGP probabilities.
4. Current event-specific eligibility, starting rotations, restrictions, injury scenarios and time-stamped offered markets.
5. Exact FanDuel combined prices and book-specific settlement/void rules, followed by quote-aware evaluation against a market baseline.

Every current SGP output remains `RESEARCH_ONLY`, `actionable=false`. The code intentionally cannot enable betting merely by supplying more simulations, a non-synthetic label, a quote, or an API key. No wager was placed and no stake sizing is provided.

## Verification

`python -m pytest -q` and `python scripts/check_dashboard.py` cover joint dependence, exact quote matching, synthetic blocking, source timestamps/hashes, immutable SGP snapshots, missing rotations, precision handling and UI requests. `python scripts/verify_authentic_runtime.py` checks the actual running server and archive bytes. Results are software and provenance evidence, not predictive accuracy.
