"""Acquire source-backed current roster membership and provider injury statuses."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nba_quant.context import refresh_context
from nba_quant.slate import load_slate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--day", help="Current Eastern date YYYY-MM-DD only")
    parser.add_argument("--output", default="output/context")
    parser.add_argument("--slate-output", default="output/slates")
    args = parser.parse_args()
    slate = load_slate(args.slate_output, args.day)
    result = refresh_context(slate, args.output)
    print(json.dumps({k: result.get(k) for k in (
        "date", "status", "retrieved_at", "hash", "snapshot_path", "counts", "errors",
        "membership_conflicts", "cross_source_comparison")}, indent=2))
    return 0 if result["status"] == "AVAILABLE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
