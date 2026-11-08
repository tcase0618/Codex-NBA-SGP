"""Bounded, read-only acquisition. No current rotations or injury claims."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nba_quant.providers import acquire_history, archived_fetcher, fetch_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD, at most 31 calendar days")
    parser.add_argument("--max-games", type=int, default=30)
    parser.add_argument("--output", default="output/authentic-history")
    parser.add_argument("--reuse-archives", action="store_true", help="Reuse hash-verified historical responses, preserving original retrieval timestamps")
    args = parser.parse_args()
    fetcher = archived_fetcher(args.output) if args.reuse_archives else fetch_json
    result = acquire_history(args.start, args.end, args.output, args.max_games, fetcher)
    print(json.dumps({k: result.get(k) for k in (
        "status", "games_accepted", "teams_acquired", "players_acquired", "games_considered",
        "dataset_path", "manifest_path", "coverage_start", "coverage_end")}, indent=2))
    return 0 if result["games_accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
