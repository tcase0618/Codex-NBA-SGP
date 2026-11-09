"""Command line: python -m nba_quant.intel <command>

  acquire START END        cache ESPN summaries for completed games (YYYY-MM-DD)
  build                    ingest the cache into the warehouse (incremental)
  coverage                 print warehouse coverage
  backtest START END       walk-forward backtest (ISO datetimes), writes output/backtest/*.json
  project HOME AWAY CUTOFF simulate one matchup (team IDs or abbreviations)
  injuries                 fetch official pregame NBA injury reports for every warehouse game
  parlays [YYYY-MM-DD]     correlation-aware parlays for a day's unplayed games (writes output/parlays/)
"""
import argparse
import json
import sys
from datetime import date

from .acquire import DEFAULT_CACHE, acquire
from .warehouse import DEFAULT_DB, Warehouse


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m nba_quant.intel")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--cache", default=str(DEFAULT_CACHE))
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("acquire"); a.add_argument("start"); a.add_argument("end")
    sub.add_parser("build")
    sub.add_parser("coverage")
    b = sub.add_parser("backtest"); b.add_argument("start"); b.add_argument("end")
    b.add_argument("--sims", type=int, default=2000); b.add_argument("--workers", type=int, default=4)
    b.add_argument("--max-games", type=int, default=None)
    sub.add_parser("injuries")
    pl = sub.add_parser("parlays"); pl.add_argument("day", nargs="?"); pl.add_argument("--sims", type=int, default=20000)
    p = sub.add_parser("project"); p.add_argument("home"); p.add_argument("away"); p.add_argument("cutoff")
    p.add_argument("--sims", type=int, default=10000); p.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)
    if args.cmd == "acquire":
        r = acquire(date.fromisoformat(args.start), date.fromisoformat(args.end), args.cache)
        print(json.dumps({"events": len(r["events"]), "failures": r["failures"]}, indent=1))
        return
    wh = Warehouse(args.db)
    if args.cmd == "build":
        s = wh.build(args.cache)
        print(json.dumps({k: v for k, v in s.items() if k != "errors"} | {"errors": s["errors"][:20]}, indent=1))
    elif args.cmd == "coverage":
        print(json.dumps(wh.coverage(), indent=1))
    elif args.cmd == "backtest":
        from . import backtest as B
        run = B.run(wh, args.start, args.end, sims=args.sims, workers=args.workers, max_games=args.max_games)
        report = B.score(wh, run["forecasts"])
        path = B.save(wh, run, report)
        print(json.dumps(report.get("game", report), indent=1)[:4000])
        print(f"saved {path}")
    elif args.cmd == "injuries":
        from .injuries import acquire_and_ingest
        from .injuries import build_availability_rows
        stats = acquire_and_ingest(wh, wh.query("SELECT id, tipoff FROM games ORDER BY tipoff"))
        stats["availability_rows"] = build_availability_rows(wh)
        print(json.dumps(stats, indent=1))
    elif args.cmd == "parlays":
        from datetime import datetime as dt_, timezone as tz_
        from zoneinfo import ZoneInfo
        from .acquire import scoreboard
        from .engine import Engine
        from .injuries import refresh_latest
        from .parlays import build_slate, save
        from .projection import Projector
        day = date.fromisoformat(args.day) if args.day else dt_.now(tz_.utc).astimezone(ZoneInfo("America/New_York")).date()
        print("latest injury report:", refresh_latest(wh))
        names = {t["id"]: t for t in wh.query("SELECT * FROM teams")}
        slate = build_slate(Projector(Engine(wh)), scoreboard(day, refresh=True), names, sims=args.sims)
        print("saved", *save(slate))
    elif args.cmd == "project":
        from .engine import Engine
        from .projection import Projector
        teams = {t["abbreviation"]: t["id"] for t in wh.query("SELECT id, abbreviation FROM teams")}
        home, away = teams.get(args.home, args.home), teams.get(args.away, args.away)
        out = Projector(Engine(wh)).project(home, away, args.cutoff, sims=args.sims, seed=args.seed)
        print(json.dumps({k: out[k] for k in ("home_win_probability", "margin", "total", "pace", "calibration", "seconds")}, indent=1))


if __name__ == "__main__":
    sys.exit(main())
