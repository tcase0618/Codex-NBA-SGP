"""Read-only ESPN acquisition with a gzip response cache.

ESPN (site.api.espn.com) is a secondary statistics provider, not the NBA's
official feed. Each cached response keeps the URL, retrieval time and SHA-256 of
the original bytes so derived warehouse rows remain traceable to a source.
"""
import gzip
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
MAX_BYTES = 20_000_000
DEFAULT_CACHE = Path("output/espn-cache")


def _check(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "site.api.espn.com" or not parsed.path.startswith("/apis/site/v2/sports/basketball/nba/"):
        raise ValueError("Only the fixed HTTPS ESPN NBA endpoint is supported")


def cache_path(cache_dir, url):
    return Path(cache_dir) / f"{hashlib.sha256(url.encode()).hexdigest()[:40]}.json.gz"


def fetch(url, cache_dir=DEFAULT_CACHE, refresh=False, retries=3):
    """Return (payload, provenance). Completed-game summaries are immutable, so
    the cache is reused unless refresh=True (schedules/scoreboards for future
    dates should be refreshed)."""
    _check(url)
    path = cache_path(cache_dir, url)
    if path.exists() and not refresh:
        record = json.loads(gzip.decompress(path.read_bytes()))
        return record["payload"], record["provenance"]
    last = None
    for attempt in range(retries):
        try:
            with urlopen(Request(url, headers={"User-Agent": "CaseNBAQuant/1.0 (research)"}), timeout=30) as response:
                if urlparse(response.geturl()).netloc != "site.api.espn.com":
                    raise ValueError("Unexpected provider redirect")
                raw = response.read(MAX_BYTES + 1)
            break
        except Exception as error:  # network errors: bounded exponential backoff
            last = error
            time.sleep(2 ** attempt)
    else:
        raise last
    if len(raw) > MAX_BYTES:
        raise ValueError("Provider response exceeds bounded read limit")
    payload = json.loads(raw)
    provenance = {"provider": "ESPN", "provider_class": "secondary_statistics_provider", "url": url,
                  "retrieved_at": datetime.now(timezone.utc).isoformat(),
                  "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(gzip.compress(json.dumps({"payload": payload, "provenance": provenance}).encode(), 6))
    tmp.replace(path)
    return payload, provenance


def scoreboard(day, cache_dir=DEFAULT_CACHE, refresh=None):
    stamp = day.strftime("%Y%m%d")
    # Refresh dates that are not safely in the past.
    if refresh is None:
        refresh = day >= (datetime.now(timezone.utc) - timedelta(days=2)).date()
    payload, _ = fetch(f"{BASE}/scoreboard?dates={stamp}&limit=100", cache_dir, refresh)
    return payload.get("events", [])


def summary(event_id, cache_dir=DEFAULT_CACHE, refresh=False):
    if not str(event_id).isdigit():
        raise ValueError("ESPN event IDs are numeric")
    return fetch(f"{BASE}/summary?event={event_id}", cache_dir, refresh)


def completed_events(start, end, cache_dir=DEFAULT_CACHE, season_types=(2, 3)):
    """Completed regular-season/playoff events between two dates inclusive."""
    events, day = [], start
    while day <= end:
        for e in scoreboard(day, cache_dir):
            comp = e["competitions"][0]
            if e.get("season", {}).get("type") in season_types and comp["status"]["type"].get("completed"):
                events.append({"id": e["id"], "date": e["date"], "season": e["season"]["year"],
                               "season_type": e["season"]["type"]})
        day += timedelta(days=1)
    return events


def acquire(start, end, cache_dir=DEFAULT_CACHE, workers=4, progress=print):
    events = completed_events(start, end, cache_dir)
    progress(f"{len(events)} completed events between {start} and {end}")
    failures = []

    def one(e):
        try:
            summary(e["id"], cache_dir)
        except Exception as error:
            failures.append({"id": e["id"], "error": repr(error)})

    with ThreadPoolExecutor(workers) as pool:
        for i, _ in enumerate(pool.map(one, events)):
            if i % 100 == 0:
                progress(f"  fetched {i}/{len(events)}")
    return {"events": events, "failures": failures}


if __name__ == "__main__":
    import sys
    a, b = (date.fromisoformat(x) for x in sys.argv[1:3])
    result = acquire(a, b)
    print(json.dumps({"events": len(result["events"]), "failures": result["failures"][:20]}))
