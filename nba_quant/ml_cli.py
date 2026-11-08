"""Offline NBA training and audit commands; safe to invoke from an OS scheduler."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

from .store import Store, canonical, digest


@contextmanager
def training_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "training.lock").open("a+b") as lock:
        lock.seek(0)
        if sys.platform == "win32":
            import msvcrt
            if lock.read(1) == b"":
                lock.write(b"0"); lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ValueError("Another training process holds the lock") from exc
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            lock.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description="NBA offline ML research pipeline")
    parser.add_argument("--db", default="data/nba_quant.sqlite")
    parser.add_argument("--root", default="output/ml")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("audit")
    commands.add_parser("status")
    train = commands.add_parser("train")
    train.add_argument("--target", action="append", required=True)
    train.add_argument("--mode", choices=["strict", "retrospective_event_time"], default="strict")
    train.add_argument("--time-limit", type=int, default=60)
    train.add_argument("--cpus", type=int, default=1)
    train.add_argument("--min-free-memory-mb", type=int, default=600)
    train.add_argument("--backends", nargs="+", choices=["autogluon", "xgboost"], default=["autogluon", "xgboost"])
    train.add_argument("--if-new-data", action="store_true")
    train.add_argument("--interval-level", type=float, default=.9)
    pred = commands.add_parser("predict")
    pred.add_argument("request", help="MLRequest JSON; strict current game binding")
    commands.add_parser("reconcile")
    registry = commands.add_parser("registry")
    registry.add_argument("action", choices=["shadow", "promote", "rollback"])
    registry.add_argument("model_id")
    registry.add_argument("--evidence", help="Local audited prospective evidence JSON; promotion remains gated")
    backtest = commands.add_parser("backtest")
    backtest.add_argument("--target", required=True)
    backtest.add_argument("--mode", choices=["strict", "retrospective_event_time"], default="strict")
    backtest.add_argument("--initial-days", type=int, default=20)
    backtest.add_argument("--test-days", type=int, default=5)
    backtest.add_argument("--window-days", type=int)
    backtest.add_argument("--time-limit", type=int, default=60)
    backtest.add_argument("--cpus", type=int, default=1)
    backtest.add_argument("--baselines-only", action="store_true")
    args = parser.parse_args()
    from .ml_features import build_training_rows
    from .ml_service import MLService, MLRequest
    store = Store(args.db)
    service = MLService(store, args.root)
    if args.command == "status":
        result = service.status()
    elif args.command == "audit":
        datasets = store.datasets()
        result = {mode: build_training_rows(datasets, mode=mode)["audit"] for mode in ("strict", "retrospective_event_time")}
    elif args.command == "predict":
        result = service.predict(MLRequest.model_validate_json(Path(args.request).read_text(encoding="utf-8-sig")))
    elif args.command == "reconcile":
        result = service.reconcile()
    elif args.command == "registry":
        from .ml_engine import registry_event
        evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8-sig")) if args.evidence else None
        result = registry_event(args.root, args.action, args.model_id, evidence=evidence)
    elif args.command == "backtest":
        from .ml_backtest import run_walk_forward
        import psutil
        if not 5 <= args.time_limit <= 600 or not 1 <= args.cpus <= 2:
            parser.error("time-limit must be 5..600 seconds, cpus 1..2")
        if not args.baselines_only and psutil.virtual_memory().available < 600*1024**2:
            parser.error("Insufficient available RAM for ML backtesting")
        with training_lock(Path(args.root)):
            bundle = build_training_rows(store.datasets(), mode=args.mode)
            result = run_walk_forward(bundle, args.target, output_dir=Path(args.root)/"backtests",
                initial_days=args.initial_days, test_days=args.test_days, window_days=args.window_days,
                time_limit=args.time_limit, num_cpus=args.cpus,
                backends=() if args.baselines_only else ("autogluon", "xgboost"))
    else:
        if not 5 <= args.time_limit <= 600 or not 1 <= args.cpus <= 2:
            parser.error("time-limit must be 5..600 seconds, cpus 1..2")
        if not .6 <= args.interval_level <= .99:
            parser.error("interval-level must be .6..99")
        from .ml_engine import train_run
        import psutil
        if psutil.virtual_memory().available < args.min_free_memory_mb * 1024**2:
            parser.error("Insufficient available RAM for the configured training budget")
        root = Path(args.root)
        with training_lock(root):
            datasets = store.datasets()
            bundle = build_training_rows(datasets, mode=args.mode)
            config_hash = digest({"datasets": [d["id"] for d in datasets], "targets": sorted(args.target),
                "mode": args.mode, "backends": args.backends, "time_limit": args.time_limit,
                "cpus": args.cpus, "interval_level": args.interval_level,
                "feature_version": bundle.get("audit", {}).get("feature_version"),
                "code_hash": digest([Path(__file__).with_name(name).read_text(encoding="utf-8")
                    for name in ("ml_features.py", "ml_engine.py")])})
            receipt_path = root / "last_successful_training.json"
            previous = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
            if args.if_new_data and previous.get("config_hash") == config_hash:
                result = {"status": "UNCHANGED", "config_hash": config_hash}
            else:
                runs, failures = [], []
                for target in args.target:
                    started = time.monotonic()
                    try:
                        manifest = train_run(bundle, target, output_dir=root, backends=tuple(args.backends),
                            time_limit=args.time_limit, num_cpus=args.cpus, alpha=1-args.interval_level)
                        runs.append(manifest)
                        service.append("ml_operations", {"operation": "training", "status": "COMPLETED",
                            "target": target, "model_id": manifest.get("id", manifest.get("model_id")),
                            "duration_seconds": time.monotonic()-started, "config_hash": config_hash})
                    except Exception as exc:
                        failures.append({"target": target, "error": str(exc)})
                        service.append("ml_operations", {"operation": "training", "status": "FAILED",
                            "target": target, "reason": str(exc), "config_hash": config_hash})
                result = {"status": "COMPLETED" if not failures else "PARTIAL" if runs else "FAILED",
                    "runs": runs, "failures": failures, "audit": bundle["audit"], "config_hash": config_hash}
                if not failures:
                    temp = receipt_path.with_suffix(".tmp")
                    temp.write_text(canonical({"config_hash": config_hash, "created_at": datetime.now(timezone.utc).isoformat()}))
                    temp.replace(receipt_path)
    print(json.dumps(result, indent=2, allow_nan=False))
    if result.get("status") in {"FAILED", "PARTIAL"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
