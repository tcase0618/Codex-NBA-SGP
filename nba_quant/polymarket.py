"""Public, read-only NBA market inventory and BUY-side order-book research.

Gamma metadata prices are explicitly indicative. Only a fresh, validated CLOB
ask book can produce a depth-backed quote. This module never authenticates,
places orders, requests RFQs or constructs joint/parlay prices.
"""
import hashlib
import json
import math
import re
import threading
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4
from zoneinfo import ZoneInfo

from .schemas import utc
from .store import canonical, digest

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
EASTERN = ZoneInfo("America/New_York")
PRICE_TTL = 300
STAT_TYPES = {"points": "pts", "rebounds": "reb", "assists": "ast", "threes": "fg3m", "steals": "stl", "blocks": "blk"}
TEAM_ALIASES = {"NY": "NYK", "NO": "NOP", "SA": "SAS", "GS": "GSW", "UTAH": "UTA", "WSH": "WAS"}


class PublicDataError(ValueError):
    def __init__(self, message, source=None, http_status=None):
        super().__init__(message)
        self.source = source
        self.http_status = http_status


def _now():
    return datetime.now(timezone.utc)


def fetch_public_json(url, output_dir):
    parsed = urlparse(url)
    allowed = parsed.scheme == "https" and (
        parsed.netloc == "gamma-api.polymarket.com" and parsed.path in {"/sports", "/events"}
        or parsed.netloc == "clob.polymarket.com" and parsed.path == "/book")
    if not allowed:
        raise ValueError("Only documented public Gamma sports/events and CLOB book GET requests are permitted")
    status, error = None, None
    try:
        with urlopen(Request(url, headers={"User-Agent": "CaseCapitalNBAQuant/0.1 public-data"}), timeout=20) as response:
            if urlparse(response.geturl()).netloc != parsed.netloc:
                raise ValueError("Unexpected Polymarket redirect host")
            status = response.status
            raw = response.read(20_000_001)
    except HTTPError as exc:
        status, error = exc.code, str(exc)
        raw = exc.read(100_001)
    if len(raw) > 20_000_000:
        raise ValueError("Polymarket response exceeds bounded read limit")
    sha = hashlib.sha256(raw).hexdigest()
    path = Path(output_dir) / "raw" / f"{sha}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        raise ValueError("Polymarket raw archive hash mismatch")
    if not path.exists():
        with path.open("xb") as stream:
            stream.write(raw)
    source = {"provider": "Polymarket", "source_url": url, "retrieved_at": _now().isoformat(),
              "sha256": sha, "raw_path": str(path.resolve()), "http_status": status}
    if error:
        raise PublicDataError(error, source, status)
    payload = json.loads(raw)
    source["content_sha256"] = digest(payload)
    return payload, source


def _list(value):
    result = json.loads(value) if isinstance(value, str) else value
    if not isinstance(result, list):
        raise ValueError("Expected outcome/asset identifier list")
    return result


def _finite(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not a numeric price or quantity")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite price or quantity")
    return result


def odds_from_price(price):
    p = _finite(price)
    if not 0 < p < 1:
        raise ValueError("Outcome share price must lie strictly between zero and one")
    american = 100 * (1 - p) / p if p <= .5 else -100 * p / (1 - p)
    return {"decimal_odds": round(1 / p, 8), "american_odds": round(american, 2)}


def parse_buy_book(book, asset_id, source, condition_id=None, requested_shares=5, max_age_seconds=PRICE_TTL):
    """Use minimum ask, never list order, midpoint, bid or last trade."""
    if str(book.get("asset_id")) != str(asset_id):
        raise ValueError("CLOB asset identity mismatch")
    if condition_id and book.get("market") != condition_id:
        raise ValueError("CLOB condition/market identity mismatch")
    timestamp = book.get("timestamp")
    try:
        observed = datetime.fromtimestamp(int(timestamp) / 1000, timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Missing or invalid CLOB source timestamp")
    retrieved = utc(source["retrieved_at"])
    if observed > retrieved + timedelta(seconds=5) or retrieved > _now():
        raise ValueError("CLOB quote observation or retrieval timestamp is in the future")
    age = (_now() - observed).total_seconds()
    if age > max_age_seconds or age < -5:
        return {"quote_status": "STALE", "observed_at": observed.isoformat(), "age_seconds": age,
                "american_odds": None, "decimal_odds": None, "best_ask": None, "tradeable": False}
    asks, bids = [], []
    for side, target in (("asks", asks), ("bids", bids)):
        levels = book.get(side)
        if not isinstance(levels, list):
            raise ValueError("CLOB book is missing a bid/ask list")
        for level in levels:
            price, size = _finite(level["price"]), _finite(level["size"])
            if not 0 < price < 1 or size < 0:
                raise ValueError("Invalid CLOB price or size")
            if size > 0:
                target.append({"price": price, "size": size})
    asks.sort(key=lambda x: x["price"])
    bids.sort(key=lambda x: x["price"], reverse=True)
    if asks and bids and bids[0]["price"] >= asks[0]["price"]:
        raise ValueError("Crossed CLOB snapshot requires refresh")
    if not asks:
        return {"quote_status": "NO_ASK_LIQUIDITY", "observed_at": observed.isoformat(), "age_seconds": age,
                "american_odds": None, "decimal_odds": None, "best_ask": None, "tradeable": False,
                "ask_depth": [], "total_ask_shares": 0}
    min_size = _finite(book.get("min_order_size", requested_shares))
    if min_size <= 0:
        raise ValueError("Minimum order shares must be positive")
    quantity = max(_finite(requested_shares), min_size)
    if quantity <= 0:
        raise ValueError("Positive requested/minimum order shares required")
    remaining, cost, worst = quantity, 0.0, None
    for level in asks:
        take = min(remaining, level["size"])
        cost += take * level["price"]
        remaining -= take
        worst = level["price"]
        if remaining <= 1e-9:
            break
    sufficient = remaining <= 1e-9
    top = asks[0]["price"]
    return {"quote_status": "ASK_DEPTH_AVAILABLE" if sufficient else "INSUFFICIENT_DEPTH",
            "observed_at": observed.isoformat(), "age_seconds": round(age, 3),
            "best_ask": top, "best_ask_shares": sum(l["size"] for l in asks if l["price"] == top),
            **odds_from_price(top), "best_bid": bids[0]["price"] if bids else None,
            "ask_depth": asks, "total_ask_shares": sum(l["size"] for l in asks),
            "minimum_order_shares": min_size, "requested_shares": quantity,
            "depth_quote": {"shares": quantity, "gross_cost": cost, "volume_weighted_price": cost / quantity,
                            "worst_fill_price": worst, **odds_from_price(cost / quantity)} if sufficient else None,
            "tradeable": False, "depth_available": sufficient,
            "best_ask_has_requested_depth": sum(l["size"] for l in asks if l["price"] == top) >= quantity,
            "cost_basis": "Gross BUY ask for a $1-winning outcome share; fees/gas/slippage after snapshot excluded",
            "source_book_hash": book.get("hash")}


def _abbreviation(value):
    text = str(value).upper()
    return TEAM_ALIASES.get(text, text)


def bind_event(event, slate):
    if slate is None:
        return {"binding_status": "UNBOUND_NO_SLATE", "game_id": None}
    teams = event.get("teams", [])
    if len(teams) != 2 or {t.get("ordering") for t in teams} != {"home", "away"}:
        return {"binding_status": "UNBOUND_TEAM_METADATA", "game_id": None}
    mapped = {t["ordering"]: _abbreviation(t.get("abbreviation")) for t in teams}
    try:
        time = utc(event["startTime"])
    except (KeyError, TypeError, ValueError):
        return {"binding_status": "UNBOUND_TIME", "game_id": None}
    matches = [g for g in slate.get("games", []) if utc(g["tipoff"]) == time
               and _abbreviation(g["home"].get("abbreviation")) == mapped["home"]
               and _abbreviation(g["away"].get("abbreviation")) == mapped["away"]]
    if len(matches) != 1:
        return {"binding_status": "UNBOUND_EXACT_TEAM_TIME", "game_id": None}
    game = matches[0]
    return {"binding_status": "EXACT_TEAM_TIME", "game_id": game["id"], "home_id": game["home"]["id"],
            "away_id": game["away"]["id"], "tipoff": game["tipoff"]}


def _market_rows(event, market, binding, source):
    outcomes = _list(market.get("outcomes"))
    version = market.get("version")
    if version not in {"v1", "v2"}:
        assets, asset_status = [], "UNSUPPORTED_VERSION"
    else:
        assets = _list(market.get("positionIds") if version == "v2" else market.get("clobTokenIds"))
        asset_status = "POSITION_ID" if version == "v2" else "CTF_TOKEN_ID"
    if assets and (len(outcomes) != len(assets) or len(set(assets)) != len(assets)):
        raise ValueError("Outcome/asset identifier alignment mismatch")
    prices = _list(market.get("outcomePrices", []))
    typ = market.get("sportsMarketType")
    question = str(market.get("question", ""))
    player_name, stat, line = None, STAT_TYPES.get(typ), market.get("line")
    prop = re.fullmatch(r"(.+): (Points|Rebounds|Assists|3-Pointers Made|Steals|Blocks) O/U (\d+(?:\.\d+)?)", question)
    if stat:
        if not prop or not math.isclose(float(prop.group(3)), _finite(line)):
            raise ValueError("Player market lacks an unambiguous source name/stat/line")
        player_name = prop.group(1)
        if outcomes != ["Yes", "No"]:
            raise ValueError("Unexpected player-prop outcome semantics")
        if f'more than {line}' not in market.get("description", ""):
            raise ValueError("Player-prop comparator is not confirmed by source resolution rules")
    rows = []
    for i, outcome in enumerate(outcomes):
        indicative = {}
        try:
            indicative_price = _finite(prices[i])
            indicative = {"indicative_price": indicative_price,
                **{f"indicative_{k}": v for k, v in odds_from_price(indicative_price).items()}}
        except (ValueError, TypeError, IndexError):
            indicative = {"indicative_price": None, "indicative_american_odds": None, "indicative_decimal_odds": None}
        rows.append({"provider": "Polymarket", "sportsbook": "Polymarket", "event_id": str(event["id"]),
            "provider_event_id": str(event["id"]), "event_slug": event["slug"], "event_name": event["title"],
            "event_tipoff": event.get("startTime"), **binding, "market_id": str(market["id"]),
            "market_type": typ, "question": question, "outcome": outcome, "outcome_index": i,
            "condition_id": market.get("conditionId"), "asset_id": str(assets[i]) if assets else None,
            "asset_identifier_type": asset_status, "protocol_version": version,
            "player_name": player_name, "player_id": None, "team_id": None, "stat": stat, "line": line,
            "line_type": "unknown", "line_type_source": "NO_STANDARD_VERSUS_ALTERNATE_MARKER",
            "side": "over" if stat and outcome == "Yes" else "under" if stat else outcome,
            "comparison": ">" if stat and outcome == "Yes" else "<=" if stat else None,
            "description": market.get("description"),
            "dnp_rule": "YES_RESOLVES_NO_IF_PLAYER_DOES_NOT_APPEAR" if "does not take the court" in market.get("description", "") else "UNREVIEWED",
            "settlement_rule_reviewed": False, "eligible_for_constructor": False,
            "active": market.get("active"), "closed": market.get("closed"),
            "accepting_orders": market.get("acceptingOrders"), "restricted": market.get("restricted"),
            "fees_enabled": market.get("feesEnabled"), "fee_schedule": market.get("feeSchedule"),
            "source_url": f"https://polymarket.com/event/{event['slug']}",
            "source_sha256": source["sha256"], "metadata_source": source,
            "retrieved_at": source["retrieved_at"], "metadata_updated_at": market.get("updatedAt"),
            "indicative_price_basis": "INDICATIVE_MARKET_METADATA_NOT_EXECUTABLE", **indicative,
            "observed_at": None, "american_odds": None, "decimal_odds": None, "best_ask": None,
            "executable": False, "quote_basis": None,
            "quote_status": "NOT_REQUESTED", "orderbook": None, "tradeable": False, "actionable": False,
            "joint_probability": None, "joint_quote": None})
    return rows


def _book_priority(rows):
    """Share bounded book requests across events, players and market types.

    Metadata prices only prioritize which books to request. They never become
    executable prices or a candidate probability. Request central, support and
    upside alternatives before consuming a player's entire ladder.
    """
    groups = {}
    for row in rows:
        key = (str(row["event_id"]), str(row["market_type"]), row.get("player_name") or "")
        groups.setdefault(key, {}).setdefault(str(row["market_id"]), []).append(row)
    event_groups = {}
    for (event, typ, player), markets in sorted(groups.items()):
        remaining = list(markets.values())
        ordered = []
        def distance(market, target):
            row = min(market, key=lambda r: r["outcome_index"])
            price = row.get("indicative_price")
            return (abs(price-target) if isinstance(price, (int, float)) and not isinstance(price, bool) else 2.,
                    float(row["line"]) if isinstance(row.get("line"), (int, float)) else 0., row["market_id"])
        for target in (.5, .8, .3):
            if remaining:
                chosen = min(remaining, key=lambda m: distance(m, target))
                ordered.append(chosen); remaining.remove(chosen)
        ordered.extend(sorted(remaining, key=lambda m: distance(m, .5)))
        event_groups.setdefault(event, []).append(deque(ordered))
    queues = []
    for event in sorted(event_groups):
        groups_for_event = deque(event_groups[event]); markets_for_event = deque()
        while groups_for_event:
            group = groups_for_event.popleft(); markets_for_event.append(group.popleft())
            if group: groups_for_event.append(group)
        queues.append(markets_for_event)
    queues = deque(queues)
    while queues:
        event_queue = queues.popleft()
        for row in sorted(event_queue.popleft(), key=lambda r: r["outcome_index"]):
            yield row
        if event_queue: queues.append(event_queue)


def refresh_polymarket_odds(output_dir="output/polymarket", slate=None, max_events=15, max_markets=600,
                            fetcher=fetch_public_json):
    if not 1 <= max_events <= 15 or not 1 <= max_markets <= 1500:
        raise ValueError("Use 1-15 events and 1-1500 market book requests")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    now = _now()
    day = datetime.fromisoformat(slate["date"]).date() if slate else now.astimezone(EASTERN).date()
    sources, errors, events, rows = [], [], [], []
    discovered = 0
    try:
        sports, source = fetcher(f"{GAMMA}/sports", out)
        sources.append(source)
        nba = [s for s in sports if s.get("sport") == "nba"]
        if len(nba) != 1:
            raise ValueError("NBA sport-series discovery is ambiguous")
        start = datetime.combine(day, datetime.min.time(), EASTERN)
        params = {"series_id": nba[0]["series"], "active": "true", "closed": "false", "limit": 100,
                  "end_date_min": start.astimezone(timezone.utc).isoformat(),
                  "end_date_max": (start + timedelta(days=1)).astimezone(timezone.utc).isoformat()}
        payload, source = fetcher(f"{GAMMA}/events?{urlencode(params)}", out)
        sources.append(source)
        selected = [e for e in payload if re.fullmatch(r"nba-[a-z0-9]+-[a-z0-9]+-\d{4}-\d{2}-\d{2}", str(e.get("slug")))
                    and utc(e["startTime"]).astimezone(EASTERN).date() == day
                    and e.get("active") is True and e.get("closed") is False]
        discovered = len(selected)
        for event in selected[:max_events]:
            binding = bind_event(event, slate)
            types = Counter()
            events.append({"id": str(event["id"]), "slug": event["slug"], "title": event["title"],
                           "tipoff": event.get("startTime"), "teams": event.get("teams"), **binding,
                           "source_url": f"https://polymarket.com/event/{event['slug']}",
                           "markets_discovered": len(event.get("markets", []))})
            for market in event.get("markets", []):
                types[str(market.get("sportsMarketType"))] += 1
                try:
                    rows.extend(_market_rows(event, market, binding, source))
                except (ValueError, KeyError, TypeError) as exc:
                    errors.append({"event_id": event["id"], "market_id": market.get("id"), "reason": str(exc)})
            events[-1]["market_type_counts"] = dict(types)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if getattr(exc, "source", None):
            sources.append(exc.source)
        errors.append({"stage": "GAMMA_DISCOVERY", "reason": str(exc)})
    # Inventory remains in display order; bounded book requests use a separate
    # round-robin so the first game cannot exhaust the entire night's budget.
    order = {"moneyline": 0, "points": 1, "rebounds": 2, "assists": 3, "threes": 4, "spreads": 5, "totals": 6}
    rows.sort(key=lambda r: (r["event_tipoff"], order.get(r["market_type"], 9), r["market_id"], r["outcome_index"]))
    access_blocked, attempted, market_ids = threading.Event(), 0, set()
    requested_rows = []
    for row in _book_priority(rows):
        if row["closed"] is not False or row["active"] is not True or row["accepting_orders"] is not True:
            row["quote_status"] = "MARKET_NOT_ACCEPTING_ORDERS"
            continue
        if not row["asset_id"]:
            row["quote_status"] = "UNSUPPORTED_ASSET_IDENTIFIER"
            continue
        if row["market_id"] not in market_ids and len(market_ids) >= max_markets:
            row["quote_status"] = "NOT_REQUESTED_BOUNDED_COVERAGE"
            continue
        market_ids.add(row["market_id"])
        requested_rows.append(row)

    def request_book(row):
        if access_blocked.is_set():
            row["quote_status"] = "QUOTE_ACCESS_UNAVAILABLE"
            return False, None, None
        try:
            book, source = fetcher(f"{CLOB}/book?{urlencode({'token_id': row['asset_id']})}", out)
            parsed = parse_buy_book(book, row["asset_id"], source, row["condition_id"])
            row.update({k: v for k, v in parsed.items() if k in {
                "observed_at", "american_odds", "decimal_odds", "best_ask", "quote_status"}})
            row.update(orderbook=parsed, book_source=source, quote_source_sha256=source["sha256"],
                       quote_retrieved_at=source["retrieved_at"],
                       executable=parsed["quote_status"] == "ASK_DEPTH_AVAILABLE" and parsed.get("best_ask_has_requested_depth") is True,
                       quote_basis="ORDERBOOK_BEST_ASK" if parsed["quote_status"] == "ASK_DEPTH_AVAILABLE" and parsed.get("best_ask_has_requested_depth") is True else None)
            return True, source, None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            row["quote_status"] = "QUOTE_ACCESS_UNAVAILABLE" if getattr(exc, "http_status", None) in {401, 403} else "BOOK_UNAVAILABLE"
            if getattr(exc, "source", None):
                row["book_error_source"] = exc.source
            if getattr(exc, "http_status", None) in {401, 403}:
                access_blocked.set()  # No retries, alternate hosts or access-control bypass.
            return True, getattr(exc, "source", None), {"stage": "CLOB_BOOK", "asset_id": row["asset_id"], "reason": str(exc)}

    def collect(result):
        nonlocal attempted
        requested, source, error = result
        attempted += int(requested)
        if source:
            sources.append(source)
        if error:
            errors.append(error)

    # One public GET establishes access; a 401/403 stops all further requests.
    # Working endpoints are read with bounded concurrency to preserve freshness.
    if requested_rows:
        collect(request_book(requested_rows[0]))
        with ThreadPoolExecutor(max_workers=8) as pool:
            for result in pool.map(request_book, requested_rows[1:]):
                collect(result)
    quote_count = sum(r["quote_status"] == "ASK_DEPTH_AVAILABLE" for r in rows)
    status = "QUOTE_ACCESS_UNAVAILABLE" if rows and access_blocked.is_set() else "AVAILABLE" if quote_count else "PARTIAL_METADATA_ONLY" if rows else "UNAVAILABLE"
    result = {"provider": "Polymarket", "date": day.isoformat(), "timezone": "America/New_York",
        "status": status, "retrieved_at": _now().isoformat(), "events": events, "markets": rows, "offers": rows,
        "sources": sources, "errors": errors, "execution": "NONE", "joint_sgp_quote": None,
        "coverage": {"events_discovered": discovered, "events_acquired": len(events),
            "events_not_acquired": max(0, discovered - len(events)), "markets_discovered": sum(e["markets_discovered"] for e in events),
            "outcome_rows": len(rows), "orderbooks_attempted": attempted, "depth_backed_outcomes": quote_count,
            "metadata_page_limit": 100, "all_league_coverage_claimed": False,
            "book_selection_policy": "Round-robin events/player-stat groups; metadata only prioritizes actual book requests",
            "player_prop_outcomes": sum(r["stat"] is not None for r in rows),
            "quote_status_counts": dict(Counter(r["quote_status"] for r in rows)),
            "binding_status_counts": dict(Counter(e["binding_status"] for e in events))},
        "documentation": ["https://docs.polymarket.com/market-data/discover-markets",
                          "https://docs.polymarket.com/market-data/prices-order-books"],
        "note": "Read-only Polymarket contracts, not FanDuel odds. Gamma prices are indicative only, never executable book quotes. BUY ask conversions are gross $1-outcome-share equivalents; fees and resolution/DNP rules differ from sportsbooks. No joint prices are manufactured by multiplying legs. Access-restricted book endpoints are not bypassed."}
    for source in sources:
        raw_source = Path(source["raw_path"]).read_bytes()
        if hashlib.sha256(raw_source).hexdigest() != source["sha256"]:
            raise ValueError("Polymarket acquired source bytes failed SHA256 verification")
        if source.get("content_sha256") and digest(json.loads(raw_source)) != source["content_sha256"]:
            raise ValueError("Polymarket acquired source canonical content hash mismatch")
    key = digest(result)
    path = out / f"snapshot-{key}.json"
    path.write_text(canonical(result), encoding="utf-8")
    pointer_temp = out / f"latest-{uuid4().hex}.tmp"
    pointer_temp.write_text(canonical({"hash": key, "path": str(path.resolve())}), encoding="utf-8")
    pointer_temp.replace(out / "latest.json")
    return {**result, "hash": key, "snapshot_path": str(path.resolve())}


def load_polymarket_odds(output_dir="output/polymarket", max_age_seconds=PRICE_TTL):
    base = {"provider": "Polymarket", "status": "UNAVAILABLE", "events": [], "markets": [], "offers": [],
            "coverage": {}, "execution": "NONE", "joint_sgp_quote": None}
    pointer = Path(output_dir) / "latest.json"
    if not pointer.exists():
        return {**base, "reason": "No public Polymarket NBA snapshot acquired"}
    try:
        receipt = json.loads(pointer.read_text(encoding="utf-8"))
        raw = Path(receipt["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt["hash"]:
            raise ValueError("Polymarket snapshot SHA256 mismatch")
        result = json.loads(raw)
        for source in result["sources"]:
            raw_source = Path(source["raw_path"]).read_bytes()
            if hashlib.sha256(raw_source).hexdigest() != source["sha256"]:
                raise ValueError("Polymarket source archive SHA256 mismatch")
            if utc(source["retrieved_at"]) > _now():
                raise ValueError("Polymarket source observation is in the future")
        age = (_now() - utc(result["retrieved_at"])).total_seconds()
        if age < 0:
            raise ValueError("Polymarket snapshot observation is in the future")
        result.update(hash=receipt["hash"], snapshot_path=receipt["path"], age_seconds=age,
                      stale=age > max_age_seconds, cache_max_age_seconds=max_age_seconds)
        if age > max_age_seconds:
            result.update(last_good_status=result["status"], status="LAST_GOOD")
        for row in result["markets"]:
            observed = row.get("observed_at")
            quote_age = (_now() - utc(observed)).total_seconds() if observed else None
            row["quote_age_seconds"] = quote_age
            if age > max_age_seconds or quote_age is not None and (quote_age > max_age_seconds or quote_age < -5):
                row.update(quote_status="STALE", american_odds=None, decimal_odds=None,
                           best_ask=None, eligible_for_constructor=False, executable=False, quote_basis=None)
                if row.get("orderbook"):
                    row["orderbook"].update(quote_status="STALE", american_odds=None, decimal_odds=None,
                                             best_ask=None, depth_quote=None, depth_available=False)
        result["offers"] = result["markets"]
        result["coverage"]["fresh_executable_best_ask_outcomes"] = sum(
            r.get("executable") is True for r in result["markets"])
        result["coverage"]["loaded_quote_status_counts"] = dict(Counter(r["quote_status"] for r in result["markets"]))
        return result
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {**base, "reason": str(exc)}
