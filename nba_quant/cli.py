import os
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .demo import make_demo
from .evaluation import aggregate
from .schemas import Dataset, ForecastRequest, utc
from .service import forecast, get_dataset, record_outcome
from .store import Store


def emit(value, output=None):
    serialized = json.dumps(value, indent=2, allow_nan=False)
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(serialized, encoding="utf-8")
        print(str(path.resolve()))
    else:
        print(serialized)


def main():
    parser = argparse.ArgumentParser(description="NBA Quant: experimental basketball research")
    parser.add_argument("--db", default="data/nba_quant.sqlite")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8020)
    sub.add_parser("demo", help="Import explicitly synthetic history and rotations")
    injury = sub.add_parser("injuries", help="Archive official NBA injury PDF; no inferred active probabilities")
    injury.add_argument("--url", help="Explicit official PDF URL if index access is unavailable")
    injury.add_argument("--output-dir", default="output/official-injuries")
    odds_events = sub.add_parser("odds-events", help="Read NBA events using THE_ODDS_API_KEY")
    odds_events.add_argument("--output")
    odds_props = sub.add_parser("odds-props", help="Read actual individual props, not a combined SGP quote")
    odds_props.add_argument("event_id")
    odds_props.add_argument("--bookmakers", default="fanduel")
    odds_props.add_argument("--output")
    imp = sub.add_parser("import", help="Validate and persist a canonical source JSON file")
    imp.add_argument("file")
    template = sub.add_parser("example", help="Export synthetic canonical input as a format example")
    template.add_argument("--output", default="output/example-dataset.json")
    predict = sub.add_parser("forecast")
    predict.add_argument("request", help="ForecastRequest JSON file")
    predict.add_argument("--output")
    export = sub.add_parser("export")
    export.add_argument("forecast_id")
    export.add_argument("--output")
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--output")
    wf = sub.add_parser("walk-forward", help="Retrospective expanding-window evaluation; immutable forecasts")
    wf.add_argument("dataset_id")
    wf.add_argument("--min-history", type=int, default=10)
    wf.add_argument("--limit", type=int, default=20)
    wf.add_argument("--simulations", type=int, default=100)
    wf.add_argument("--seed", type=int, default=42)
    wf.add_argument("--output", default="output/walk-forward.json")
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn
        from .api import create_app
        os.environ.setdefault("NBA_INTEL_WARM", "1")
        uvicorn.run(create_app(args.db), host="127.0.0.1", port=args.port)
        return
    store = Store(args.db)
    if args.command == "injuries":
        from .injuries import refresh_report
        emit(refresh_report(args.output_dir, source_url=args.url))
    elif args.command == "odds-events":
        from .odds import OddsAPI
        emit(OddsAPI().events(), args.output)
    elif args.command == "odds-props":
        from .odds import OddsAPI
        emit(OddsAPI().props(args.event_id, bookmakers=args.bookmakers), args.output)
    elif args.command == "demo":
        emit({"dataset_id": store.import_dataset(make_demo()), "synthetic": True})
    elif args.command == "import":
        dataset = Dataset.model_validate_json(Path(args.file).read_text(encoding="utf-8-sig"))
        emit({"dataset_id": store.import_dataset(dataset), "synthetic": dataset.synthetic})
    elif args.command == "example":
        emit(make_demo().model_dump(mode="json"), args.output)
    elif args.command == "forecast":
        req = ForecastRequest.model_validate_json(Path(args.request).read_text(encoding="utf-8-sig"))
        emit(forecast(store, req), args.output)
    elif args.command == "export":
        payload = store.forecast(args.forecast_id)
        if payload is None:
            parser.error("Unknown forecast")
        emit(payload, args.output)
    elif args.command == "evaluate":
        emit(aggregate(store.outcomes()), args.output)
    elif args.command == "walk-forward":
        if args.min_history < 0 or not 1 <= args.limit <= 5000:
            parser.error("min-history must be nonnegative and limit must be 1..5000")
        d = get_dataset(store, args.dataset_id)
        games = sorted(d["games"], key=lambda g: utc(g["tipoff"]))
        completed, skipped = [], []
        for g in games:
            cutoff = utc(g["tipoff"]) - timedelta(hours=1)
            known = [h for h in games if utc(h["known_at"]) <= cutoff and utc(h["completed_at"]) < cutoff]
            if len(known) < args.min_history:
                continue
            if len(completed) >= args.limit:
                break
            try:
                req = ForecastRequest(dataset_id=d["id"], home_id=g["home_id"], away_id=g["away_id"],
                    tipoff=g["tipoff"], cutoff=cutoff, simulations=args.simulations, seed=args.seed + len(completed))
                f = forecast(store, req)
                scored = record_outcome(store, f["id"], d["id"], g["id"])
                completed.append({"forecast_id": f["id"], **scored})
            except ValueError as exc:
                skipped.append({"game_id": g["id"], "reason": str(exc)})
        emit({"mode": "RETROSPECTIVE WALK-FORWARD", "evaluated_at": datetime.now(timezone.utc).isoformat(),
              "dataset_id": d["id"], "completed": completed, "skipped": skipped,
              "evaluation": aggregate(completed)}, args.output)


if __name__ == "__main__":
    main()
