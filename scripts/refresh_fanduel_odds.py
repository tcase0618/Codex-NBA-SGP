"""Automatically archive primary public FanDuel NBA market components."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nba_quant.fanduel import refresh_fanduel_odds


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="output/fanduel")
    args = parser.parse_args()
    result = refresh_fanduel_odds(args.output)
    print(json.dumps({k: result[k] for k in ("status", "offers_count", "verified_current_quotes", "price_freshness", "snapshot_path", "errors")}, indent=2))
    for offer in result["offers"]:
        print(offer["player_name"], offer["stat"], offer["side"], offer["line"], offer["american_odds"])
    return 0 if result["offers"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
