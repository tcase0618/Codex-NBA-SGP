"""Acquire public FanDuel-published market components without a subscription.

Research articles can embed genuine market/selection IDs and prices, but their
publication/retrieval clocks are not the market's price-update clock. These
offers are never promoted to a verified-current sportsbook or joint SGP quote.
"""
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .schemas import utc
from .store import canonical, digest

INDEX_URL = "https://www.fanduel.com/research/nba"
SPORTSBOOK_URL = "https://sportsbook.fanduel.com/navigation/nba"
EASTERN = ZoneInfo("America/New_York")


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.current = None
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and (attributes.get("id") == "__NEXT_DATA__" or attributes.get("type") == "application/ld+json"):
            self.current = {"id": attributes.get("id"), "text": ""}

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += data

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.scripts.append(self.current)
            self.current = None


def fetch_html(url, output_dir):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not (
        parsed.netloc == "www.fanduel.com" and parsed.path.startswith("/research/") or url == SPORTSBOOK_URL):
        raise ValueError("Only exact primary public FanDuel research/sportsbook URLs are supported")
    with urlopen(Request(url, headers={"User-Agent": "CaseCapitalNBAQuant/0.1 research"}), timeout=20) as response:
        if urlparse(response.geturl()).netloc != parsed.netloc:
            raise ValueError("Unexpected FanDuel redirect host")
        raw = response.read(3_000_001)
        if len(raw) > 3_000_000:
            raise ValueError("FanDuel response exceeds bounded size limit")
        status = response.status
    sha = hashlib.sha256(raw).hexdigest()
    path = Path(output_dir) / "raw" / f"{sha}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != sha:
        raise ValueError("Existing FanDuel archive hash mismatch")
    if not path.exists():
        with path.open("xb") as stream:
            stream.write(raw)
    return raw.decode("utf-8"), {"provider": "FanDuel", "source_url": url, "http_status": status,
        "retrieved_at": datetime.now(timezone.utc).isoformat(), "sha256": sha, "raw_path": str(path.resolve())}


def _json_scripts(html):
    parser = _Scripts()
    parser.feed(html)
    return [(script["id"], json.loads(script["text"])) for script in parser.scripts]


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _decimal(american):
    if not isinstance(american, str) or not re.fullmatch(r"[+-]\d+", american) or abs(int(american)) < 100:
        raise ValueError("Invalid American odds")
    value = int(american)
    return round(1 + (value / 100 if value > 0 else 100 / -value), 8)


def _market(market, selection):
    label = market["marketName"]
    plus = re.fullmatch(r"(\d+)\+ (Threes|Made Threes|Rebounds|Points|Assists)", label, re.I)
    if plus:
        stat = {"threes": "fg3m", "made threes": "fg3m", "rebounds": "reb", "points": "pts", "assists": "ast"}[plus.group(2).lower()]
        return stat, "at_least", float(plus.group(1)), selection["runnerName"]
    runner = re.fullmatch(r"(.+) (Over|Under)", selection["runnerName"])
    formatted = re.fullmatch(r"([OU]) (\d+(?:\.\d+)?)", str(selection.get("formattedHandicap")))
    if not runner or not formatted or formatted.group(1) != runner.group(2)[0]:
        raise ValueError("Unsupported or ambiguous player market/comparator")
    if "Pts + Reb + Ast" in label:
        stat = "pra"
    elif label.endswith(" - Points"):
        stat = "pts"
    elif label.endswith(" - Rebounds"):
        stat = "reb"
    elif label.endswith(" - Assists"):
        stat = "ast"
    elif label.endswith(" - Made Threes"):
        stat = "fg3m"
    else:
        raise ValueError("Unsupported player statistic")
    line = float(formatted.group(2))
    if not math.isfinite(line) or float(selection["handicap"]) != line:
        raise ValueError("Player line differs from formatted source handicap")
    return stat, runner.group(2).lower(), line, runner.group(1)


def parse_research_article(html, source):
    actual_now = datetime.now(timezone.utc)
    parsed_url = urlparse(source["source_url"])
    if source.get("provider") != "FanDuel" or parsed_url.scheme != "https" or parsed_url.netloc != "www.fanduel.com" or not parsed_url.path.startswith("/research/"):
        raise ValueError("Only primary FanDuel Research article provenance is supported")
    if utc(source["retrieved_at"]) > actual_now:
        raise ValueError("FanDuel retrieval timestamp cannot be in the future")
    if hashlib.sha256(html.encode()).hexdigest() != source["sha256"]:
        raise ValueError("FanDuel article content differs from source hash")
    data = next((value for key, value in _json_scripts(html) if key == "__NEXT_DATA__"), None)
    if not data:
        raise ValueError("FanDuel page has no supported structured article data")
    article = data.get("props", {}).get("pageProps", {}).get("article")
    if not article or article.get("sport", {}).get("slug") != "nba":
        raise ValueError("Expected an explicitly identified NBA research article")
    published = article["firstPublishedAt"]
    modified = article.get("publishedDate")
    if utc(published) > utc(source["retrieved_at"]) or modified and utc(modified) > utc(source["retrieved_at"]):
        raise ValueError("Article publication timestamp is in the future")
    body = json.loads(article["body"])
    offers, errors, summaries = [], [], []
    for block in body:
        if block.get("listItem") == "bullet":
            text = "".join(child.get("text", "") for child in block.get("children", []))
            if re.search(r"\([+-]\d+\)", text):
                summaries.append(text)
        if block.get("_type") != "dynamicOdds" or block.get("oddsLeague", {}).get("name") != "NBA":
            continue
        try:
            event = block["odds"]["event"]
            tipoff = utc(event["time"]).isoformat()
            for market in block["odds"]["props"]:
                for selection in market["selections"]:
                    try:
                        stat, side, line, player = _market(market, selection)
                        price = _decimal(selection["odds"])
                        offers.append({"bookmaker": "FanDuel", "provider": "FanDuel Research",
                            "source_kind": "PRIMARY_PUBLISHED_MARKET_COMPONENT", "player_name": player,
                            "selection_name": selection["runnerName"], "stat": stat, "side": side,
                            "line": line, "american_odds": int(selection["odds"]), "decimal_odds": price,
                            "source_url": source["source_url"], "source_sha256": source["sha256"],
                            "retrieved_at": source["retrieved_at"], "article_published_at": published,
                            "article_modified_at": modified, "quote_updated_at": None,
                            "price_freshness": "UNKNOWN", "eligible_as_current_quote": False,
                            "provider_event_id": str(block["event"]["id"]), "event_name": block["event"]["name"],
                            "event_tipoff": tipoff, "home_team": event["homeTeam"]["name"],
                            "away_team": event["awayTeam"]["name"], "event_url": event["eventUrl"],
                            "market_id": str(market["marketId"]), "external_market_id": market.get("externalMarketId"),
                            "market_name": market["marketName"], "selection_id": str(selection["selectionId"]),
                            "market_status": market["marketStatus"], "runner_status": selection["runnerStatus"],
                            "in_play": market.get("inPlay"), "sgp_eligible": market.get("sgmMarket") is True,
                            "article_title": article["title"]})
                    except (KeyError, TypeError, ValueError) as exc:
                        errors.append({"block": block.get("_key"), "market_id": market.get("marketId"), "reason": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            errors.append({"block": block.get("_key"), "reason": str(exc)})
    return {"offers": offers, "errors": errors, "editorial_summaries": summaries,
            "article_published_at": published, "article_modified_at": modified,
            "source_url": source["source_url"], "article_title": article["title"]}


def refresh_fanduel_odds(output_dir="output/fanduel", fetcher=fetch_html, max_articles=3):
    if not 1 <= max_articles <= 3:
        raise ValueError("At most three current-day articles may be acquired per refresh")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    sources, attempts, articles, offers, errors = [], [], [], [], []
    try:
        shell, source = fetcher(SPORTSBOOK_URL, directory)
        sources.append(source)
        attempts.append({"source_url": SPORTSBOOK_URL, "http_status": source.get("http_status"),
                         "result": "PUBLIC_APP_SHELL; no server-rendered market quote extraction supported"})
    except (OSError, ValueError) as exc:
        attempts.append({"source_url": SPORTSBOOK_URL, "result": str(exc)})
    try:
        index, source = fetcher(INDEX_URL, directory)
        sources.append(source)
        candidates = {}
        for _, value in _json_scripts(index):
            for item in _walk(value):
                url = item.get("url")
                title = str(item.get("name", ""))
                published = item.get("datePublished")
                if isinstance(url, str) and url.startswith("https://www.fanduel.com/research/") and "nba" in title.lower() and "prop" in title.lower() and published:
                    time = utc(published)
                    if time <= now and time.astimezone(EASTERN).date() == now.astimezone(EASTERN).date():
                        candidates[url] = time
        for url in sorted(candidates, key=lambda u: candidates[u], reverse=True)[:max_articles]:
            try:
                html, article_source = fetcher(url, directory)
                sources.append(article_source)
                parsed = parse_research_article(html, article_source)
                articles.append(parsed)
                offers.extend(parsed["offers"])
                errors.extend(parsed["errors"])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append({"source_url": url, "reason": str(exc)})
        if not candidates:
            errors.append({"source_url": INDEX_URL, "reason": "No current-Eastern-date NBA prop article discovered in primary structured index"})
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append({"source_url": INDEX_URL, "reason": str(exc)})
    unique = {(o["provider_event_id"], o["market_id"], o["selection_id"]): o for o in offers}
    result = {"status": "PARTIAL_PUBLISHED" if unique else "UNAVAILABLE", "provider": "FanDuel",
        "retrieved_at": datetime.now(timezone.utc).isoformat(), "date": now.astimezone(EASTERN).date().isoformat(),
        "offers": list(unique.values()), "offers_count": len(unique), "verified_current_quotes": 0,
        "price_freshness": "UNKNOWN", "joint_sgp_quote": None, "sources": sources,
        "attempts": attempts, "articles": articles, "errors": errors,
        "note": "Automatically acquired primary FanDuel Research market components. Partial coverage only. Market price-update timestamps are not supplied: retrieval and article publication do not establish quote freshness. Editorial prose and structured components can differ; both are preserved. No combined SGP payout or live full-book feed is claimed."}
    key = digest(result)
    path = directory / f"published-{key}.json"
    path.write_text(canonical(result), encoding="utf-8")
    receipt = {"hash": key, "path": str(path.resolve()), "retrieved_at": result["retrieved_at"]}
    (directory / "latest.json").write_text(canonical(receipt), encoding="utf-8")
    return {**result, "hash": key, "snapshot_path": str(path.resolve())}


def load_fanduel_odds(output_dir="output/fanduel"):
    base = {"status": "UNAVAILABLE", "offers": [], "offers_count": 0,
            "verified_current_quotes": 0, "price_freshness": "UNKNOWN", "joint_sgp_quote": None}
    pointer = Path(output_dir) / "latest.json"
    if not pointer.exists():
        return {**base, "reason": "No automatic FanDuel acquisition has been recorded"}
    try:
        receipt = json.loads(pointer.read_text(encoding="utf-8"))
        raw = Path(receipt["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt["hash"]:
            raise ValueError("FanDuel snapshot hash mismatch")
        result = json.loads(raw)
        if utc(result["retrieved_at"]) > datetime.now(timezone.utc):
            raise ValueError("FanDuel snapshot observation is in the future")
        for source in result["sources"]:
            if hashlib.sha256(Path(source["raw_path"]).read_bytes()).hexdigest() != source["sha256"]:
                raise ValueError("FanDuel source archive hash mismatch")
        return {**result, "hash": receipt["hash"], "snapshot_path": receipt["path"]}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {**base, "reason": str(exc)}
