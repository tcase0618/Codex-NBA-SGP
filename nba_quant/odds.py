"""The Odds API v4 reader. Single-leg markets never stand in for an SGP quote."""
import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .schemas import utc

HOST = "https://api.the-odds-api.com"
MARKETS = {"player_points": "pts", "player_rebounds": "reb", "player_assists": "ast",
    "player_threes": "fg3m", "player_blocks": "blk", "player_steals": "stl", "player_turnovers": "tov",
    "player_points_rebounds_assists": "pra", "player_points_rebounds": "points_rebounds",
    "player_points_assists": "points_assists", "player_rebounds_assists": "rebounds_assists"}
MARKETS.update({key + "_alternate": value for key, value in list(MARKETS.items())})


class OddsAPI:
    def __init__(self, api_key=None, output_dir="output/odds", transport=None):
        self._key = api_key if api_key is not None else os.environ.get("THE_ODDS_API_KEY", "")
        self.output_dir = Path(output_dir)
        self.transport = transport

    @property
    def configured(self):
        return bool(self._key.strip())

    def _read(self, path, params):
        if not self.configured:
            raise ValueError("THE_ODDS_API_KEY is not configured; no prices were requested or invented")
        safe_url = HOST + path + "?" + urllib.parse.urlencode(params)
        query = {**params, "apiKey": self._key}
        secret_url = HOST + path + "?" + urllib.parse.urlencode(query)
        try:
            if self.transport:
                raw = self.transport(secret_url)
            else:
                with urllib.request.urlopen(secret_url, timeout=30) as response:
                    raw = response.read(10_000_001)
        except urllib.error.HTTPError as exc:
            raise ValueError(f"Odds provider returned HTTP {exc.code}; verify subscription access and quota") from None
        except (urllib.error.URLError, TimeoutError):
            raise ValueError("Odds provider connection failed; no price snapshot was recorded") from None
        except Exception:
            raise ValueError("Odds provider transport failed; no credential-bearing error was retained") from None
        if len(raw) > 10_000_000:
            raise ValueError("Odds provider response exceeds size limit")
        # API keys must not enter persisted content, exceptions or source receipts.
        if self._key.encode() in raw:
            raise ValueError("Provider response unexpectedly contains credential text; refused archive")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ValueError("Odds provider returned malformed JSON") from None
        now = datetime.now(timezone.utc).isoformat()
        sha = hashlib.sha256(raw).hexdigest()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        archive = self.output_dir / f"{sha}.json"
        archive.write_bytes(raw)
        receipt = {"provider": "The Odds API", "source_url": safe_url, "retrieved_at": now,
            "known_at": now, "sha256": sha, "raw_path": str(archive.resolve()),
            "scope": "individual_event_and_leg_markets", "combined_sgp_quote": "UNAVAILABLE"}
        (self.output_dir / f"{sha}.receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        return payload, receipt

    def events(self):
        payload, receipt = self._read("/v4/sports/basketball_nba/events", {})
        if not isinstance(payload, list):
            raise ValueError("Unexpected events response")
        return {"events": payload, "provenance": receipt}

    def props(self, event_id, bookmakers="fanduel,draftkings", markets=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", event_id):
            raise ValueError("Invalid odds provider event ID")
        requested = list(markets or ["player_points", "player_rebounds", "player_assists", "player_threes",
                                    "player_points_alternate", "player_rebounds_alternate",
                                    "player_assists_alternate", "player_threes_alternate"])
        if not requested or set(requested) - MARKETS.keys():
            raise ValueError("Unsupported player markets")
        if not re.fullmatch(r"[a-z0-9_,]{1,200}", bookmakers):
            raise ValueError("Invalid bookmaker list")
        payload, receipt = self._read(f"/v4/sports/basketball_nba/events/{event_id}/odds",
            {"regions": "us", "markets": ",".join(requested), "bookmakers": bookmakers, "oddsFormat": "decimal", "dateFormat": "iso"})
        if not isinstance(payload, dict) or payload.get("id") != event_id or payload.get("sport_key") != "basketball_nba":
            raise ValueError("Odds event identity mismatch")
        lines = []
        for book in payload.get("bookmakers", []):
            for market in book.get("markets", []):
                if market.get("key") not in MARKETS:
                    continue
                stamp = market.get("last_update") or book.get("last_update")
                if not stamp or utc(stamp) > utc(receipt["retrieved_at"]):
                    raise ValueError("Odds observation timestamp is missing or in the future")
                for outcome in market.get("outcomes", []):
                    if outcome.get("name") not in {"Over", "Under"}:
                        continue
                    price, line = outcome.get("price"), outcome.get("point")
                    if type(price) not in (int, float) or not math.isfinite(price) or not 1 < price < 100000 or type(line) not in (int, float) or not math.isfinite(line) or not 0 <= line < 1000:
                        raise ValueError("Malformed offered prop price or line")
                    if not outcome.get("description"):
                        raise ValueError("Player identity missing from prop")
                    lines.append({"provider": "The Odds API", "provider_event_id": event_id, "sportsbook": book["key"],
                        "source_player_name": outcome["description"], "player_id": None,
                        "identity_status": "UNMAPPED", "stat": MARKETS[market["key"]],
                        "market_key": market["key"], "alternate": market["key"].endswith("_alternate"),
                        "line_type": "alternate" if market["key"].endswith("_alternate") else "standard",
                        "line_type_source": "PROVIDER_MARKET_KEY",
                        "side": outcome["name"].lower(), "line": line, "decimal_odds": price,
                        "observed_at": stamp, "retrieved_at": receipt["retrieved_at"],
                        "source_url": receipt["source_url"], "source_sha256": receipt["sha256"]})
        result = {"event": {k: payload.get(k) for k in ("id", "sport_key", "commence_time", "home_team", "away_team")},
            "lines": lines, "provenance": receipt, "combined_sgp_quote": None,
            "identity_mapping": "Explicit reviewed provider-to-model player crosswalk required before construction"}
        (self.output_dir / "latest-props.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result
