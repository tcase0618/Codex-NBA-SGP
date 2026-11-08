"""Archive public NBA contract metadata and available BUY ask books; no trading."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nba_quant.polymarket import refresh_polymarket_odds
from nba_quant.slate import load_slate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="output/polymarket")
    args = parser.parse_args()
    result = refresh_polymarket_odds(args.output, slate=load_slate(refresh=True))
    print(json.dumps({k: result.get(k) for k in ("status", "coverage", "errors", "hash", "snapshot_path")}, indent=2))


if __name__ == "__main__":
    main()
