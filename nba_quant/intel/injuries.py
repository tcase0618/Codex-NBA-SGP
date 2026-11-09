"""Official NBA injury reports (dated PDFs) -> warehouse `injuries`.

Source: ak-static.cms.nba.com/referee/injury/Injury-Report_YYYY-MM-DD_HH_MMAM|PM.pdf,
published every 15 minutes on game days (times are US Eastern). Each listing's
`known_at` is the report's publication time, so a forecast with cutoff C may use
only reports published at or before C. Teams marked "NOT YET SUBMITTED" carry no
information and are recorded as such, never as "everyone available".
"""
import gzip
import hashlib
import io
import json
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HOST = "ak-static.cms.nba.com"
CACHE = Path("output/injury-cache")
STATUSES = ("Out", "Doubtful", "Questionable", "Probable", "Available")
TEAMS = ["Atlanta Hawks", "Boston Celtics", "Brooklyn Nets", "Charlotte Hornets", "Chicago Bulls", "Cleveland Cavaliers",
         "Dallas Mavericks", "Denver Nuggets", "Detroit Pistons", "Golden State Warriors", "Houston Rockets", "Indiana Pacers",
         "LA Clippers", "Los Angeles Clippers", "Los Angeles Lakers", "Memphis Grizzlies", "Miami Heat", "Milwaukee Bucks",
         "Minnesota Timberwolves", "New Orleans Pelicans", "New York Knicks", "Oklahoma City Thunder", "Orlando Magic",
         "Philadelphia 76ers", "Phoenix Suns", "Portland Trail Blazers", "Sacramento Kings", "San Antonio Spurs",
         "Toronto Raptors", "Utah Jazz", "Washington Wizards"]
TEAM_TOKENS = sorted(((t.split(), t) for t in TEAMS), key=lambda x: -len(x[0]))
SUFFIX = re.compile(r"^(Jr|Sr|II|III|IV|V)\.?,$")
HEADER = re.compile(r"^(Injury|Report:|Page|of|Game|Date|Time|Matchup|Team|Player|Name|Current|Status|Reason)$")


FORMAT_SWITCH = datetime(2025, 12, 22, tzinfo=ET)  # hourly "_06PM" files before; quarter-hour "_06_00PM" after


def report_url(at_et, legacy=None):
    legacy = at_et < FORMAT_SWITCH if legacy is None else legacy
    if legacy:
        return f"https://{HOST}/referee/injury/Injury-Report_{at_et:%Y-%m-%d}_{at_et:%I}{at_et:%p}.pdf"
    return f"https://{HOST}/referee/injury/Injury-Report_{at_et:%Y-%m-%d}_{at_et:%I}_{at_et:%M}{at_et:%p}.pdf"


def floor_quarter(dt):
    return dt.replace(minute=dt.minute - dt.minute % 15, second=0, microsecond=0)


def fetch_pdf(url, cache=CACHE):
    """Return (bytes or None, provenance). None means the provider has no file (403/404)."""
    if urlparse(url).netloc != HOST:
        raise ValueError("Only the official NBA injury report host is supported")
    path = Path(cache) / (hashlib.sha256(url.encode()).hexdigest()[:40] + ".gz")
    if path.exists():
        rec = json.loads(gzip.decompress(path.read_bytes()))
        return (bytes.fromhex(rec["hex"]) if rec["hex"] else None), rec["provenance"]
    raw, status = None, None
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"User-Agent": "CaseNBAQuant/1.0 (research)"}), timeout=30) as r:
                raw, status = r.read(10_000_000), r.status
            break
        except HTTPError as e:
            if e.code in (403, 404):
                status = e.code
                break
            time.sleep(2 ** attempt)
        except Exception:
            time.sleep(2 ** attempt)
    if status is None:
        raise RuntimeError(f"Network failure for {url}")
    prov = {"url": url, "retrieved_at": datetime.now(timezone.utc).isoformat(), "status": status,
            "sha256": hashlib.sha256(raw).hexdigest() if raw else None}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps({"hex": raw.hex() if raw else "", "provenance": prov}).encode()))
    return raw, prov


def parse_report(raw):
    """Parse report bytes into {'teams': {team: {'submitted': bool, 'players': [...]}}}."""
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(raw))
    tokens = []
    for page in reader.pages:
        tokens += (page.extract_text() or "").split()
    return parse_tokens(tokens)


def parse_tokens(tokens):
    """Parse the report's reading-order token stream (see parse_report)."""
    teams, team = {}, None
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        matched = next((name for words, name in TEAM_TOKENS if tokens[i:i + len(words)] == words), None)
        if matched:
            team = "LA Clippers" if matched == "Los Angeles Clippers" else matched
            teams.setdefault(team, {"submitted": True, "players": []})
            i += len(matched.split())
            if tokens[i:i + 3] == ["NOT", "YET", "SUBMITTED"]:
                teams[team]["submitted"] = False
                i += 3
            continue
        if team and "," in tok and not tok.endswith(";"):
            # Find the status word, then anchor the name on the LAST comma token before it
            # (reason text can contain commas); suffix tokens (Jr., II, ...) pull in the surname.
            j = i
            while j < n and tokens[j] not in STATUSES and j - i < 12:
                j += 1
            if j < n and tokens[j] in STATUSES:
                c = max(k for k in range(i, j) if "," in tokens[k])
                start = c - 1 if SUFFIX.match(tokens[c]) and c - 1 >= 0 else c
                while start - 1 >= 0 and tokens[start - 1].endswith("-"):  # hyphenated surname split by the PDF
                    start -= 1
                prev = teams[team]["players"][-1] if teams[team]["players"] else None
                if prev is not None and start < i:  # surname token was consumed as reason text
                    prev["reason"] = " ".join(prev["reason"].split()[:-(i - start)])
                elif prev is not None and start > i:
                    prev["reason"] = (prev["reason"] + " " + " ".join(tokens[i:start])).strip()
                raw_name = re.sub(r"-\s+", "-", " ".join(tokens[start:j]))
                status = tokens[j]
                k, reason = j + 1, []
                while k < n and "," not in tokens[k] and not re.match(r"^\d\d:\d\d$", tokens[k]) and \
                        not any(tokens[k:k + len(w)] == w for w, _ in TEAM_TOKENS) and not HEADER.match(tokens[k]) and len(reason) < 25:
                    reason.append(tokens[k])
                    k += 1
                teams[team]["players"].append({"name": raw_name, "status": status, "reason": " ".join(reason)})
                i = k
                continue
        i += 1
    for v in teams.values():
        v["submitted"] = v["submitted"] or bool(v["players"])  # a team with listings did submit
    return {"teams": teams}


def _norm(name):
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    name = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", name)
    return re.sub(r"[^a-z]", "", name)


def to_first_last(report_name):
    if "," not in report_name:
        return report_name
    last, first = report_name.split(",", 1)
    return f"{first.strip()} {last.strip()}"


def reports_for_games(games, lead=timedelta(hours=1), back_steps=8):
    """Report slot (ET quarter hour at or before tipoff - lead) for each game."""
    plan = {}
    for g in games:
        tip = datetime.fromisoformat(g["tipoff"].replace("Z", "+00:00"))
        slot = floor_quarter((tip - lead).astimezone(ET))
        if slot < FORMAT_SWITCH:  # legacy reports were hourly
            slot = slot.replace(minute=0)
            plan[g["id"]] = [slot - timedelta(hours=k) for k in range(4)]
        else:
            plan[g["id"]] = [slot - timedelta(minutes=15 * k) for k in range(back_steps)]
    return plan


def acquire_and_ingest(wh, games, workers=6, progress=print):
    """Fetch the latest pregame report for each game and store parsed listings."""
    plan = reports_for_games(games)
    slots = sorted({s for v in plan.values() for s in v[:1]})
    progress(f"{len(games)} games -> {len(slots)} primary report slots")
    fetched = {}

    def get(slot):
        raw, prov = fetch_pdf(report_url(slot))
        return slot, raw, prov

    with ThreadPoolExecutor(workers) as pool:
        for i, (slot, raw, prov) in enumerate(pool.map(get, slots)):
            fetched[slot] = (raw, prov)
            if i % 200 == 0:
                progress(f"  {i}/{len(slots)} fetched")
    # Fallback slots where the primary was missing.
    missing = [g for g, v in plan.items() if fetched.get(v[0], (None,))[0] is None]
    for gid in missing:
        for slot in plan[gid][1:]:
            if slot not in fetched:
                raw, prov = fetch_pdf(report_url(slot))
                fetched[slot] = (raw, prov)
            if fetched[slot][0] is not None:
                plan[gid] = [slot]
                break
    ctx = _match_context(wh)
    stats = {"reports": 0, "listings": 0, "unmatched_names": set(), "parse_errors": 0, "no_report_games": 0}
    with wh.connect() as db:
        _ensure_tables(db)
        for slot, (raw, prov) in fetched.items():
            if raw is not None:
                _store_report(db, slot, raw, prov, ctx, stats)
        for gid, v in plan.items():
            slot = v[0]
            if fetched.get(slot, (None,))[0] is None:
                stats["no_report_games"] += 1
                continue
            db.execute("INSERT OR REPLACE INTO game_injury_report VALUES(?,?,?)", (
                gid, report_url(slot), slot.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")))
    stats["unmatched_names"] = sorted(stats["unmatched_names"])[:40] + ([f"... {len(stats['unmatched_names'])} total"] if len(stats["unmatched_names"]) > 40 else [])
    return stats


def _ensure_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS injury_reports(url TEXT PRIMARY KEY, known_at TEXT, report_date TEXT,
        sha256 TEXT, teams_submitted TEXT, teams_pending TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS game_injury_report(game_id TEXT PRIMARY KEY, url TEXT, known_at TEXT)""")


def _match_context(wh):
    players = wh.query("SELECT id, name, last_team_id FROM players")
    by_norm = {}
    for pl in players:
        by_norm.setdefault(_norm(pl["name"]), []).append(pl)
    return {"players": players, "by_norm": by_norm, "teams": {t["name"]: t["id"] for t in wh.query("SELECT id, name FROM teams")}}


def _store_report(db, slot, raw, prov, ctx, stats):
    try:
        parsed = parse_report(raw)
    except Exception:
        stats["parse_errors"] += 1
        return
    teams, players, by_norm = ctx["teams"], ctx["players"], ctx["by_norm"]
    known_at = slot.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    submitted = [t for t, v in parsed["teams"].items() if v["submitted"]]
    pending = [t for t, v in parsed["teams"].items() if not v["submitted"]]
    db.execute("INSERT OR REPLACE INTO injury_reports VALUES(?,?,?,?,?,?)", (
        prov["url"], known_at, slot.strftime("%Y-%m-%d"), prov["sha256"],
        json.dumps([teams.get(t) for t in submitted if teams.get(t)]), json.dumps([teams.get(t) for t in pending if teams.get(t)])))
    stats["reports"] += 1
    for tname, v in parsed["teams"].items():
        tid = teams.get(tname)
        for pl in v["players"]:
            cands = by_norm.get(_norm(to_first_last(pl["name"])), [])
            if len(cands) > 1:
                cands = [c for c in cands if c["last_team_id"] == tid] or cands
            if not cands and "," in pl["name"]:
                # Names split mid-word by the PDF (e.g. accents): same team, same first name,
                # and the report's surname fragment ends the full name.
                last, first = (s.strip() for s in pl["name"].split(",", 1))
                frag, fn = _norm(last), _norm(first)
                cands = [c for c in players if c["last_team_id"] == tid and _norm(c["name"]).startswith(fn)
                         and frag and _norm(c["name"]).endswith(frag)]
            if len(cands) != 1:
                stats["unmatched_names"].add(pl["name"])  # two-way/G League players often never appear in box scores
                continue
            db.execute("INSERT OR REPLACE INTO injuries VALUES(?,?,?,?,?,?)", (
                tid, cands[0]["id"], pl["status"], pl["reason"], known_at, prov["url"]))
            stats["listings"] += 1


def refresh_latest(wh, now=None, back_steps=16):
    """Fetch and store the most recent published report at or before `now` (live use)."""
    now = (now or datetime.now(timezone.utc)).astimezone(ET)
    slot = floor_quarter(now)
    for k in range(back_steps):
        s = slot - timedelta(minutes=15 * k)
        if s < FORMAT_SWITCH:
            s = s.replace(minute=0)
        raw, prov = fetch_pdf(report_url(s), cache=CACHE) if k else _fetch_fresh(report_url(s))
        if raw is not None:
            stats = {"reports": 0, "listings": 0, "unmatched_names": set(), "parse_errors": 0}
            with wh.connect() as db:
                _ensure_tables(db)
                _store_report(db, s, raw, prov, _match_context(wh), stats)
            return {"url": prov["url"], "known_at": s.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
                    "listings": stats["listings"]}
    return None


def _fetch_fresh(url):
    """The current quarter-hour slot may not exist yet; never cache a miss for it."""
    path = Path(CACHE) / (hashlib.sha256(url.encode()).hexdigest()[:40] + ".gz")
    raw, prov = fetch_pdf(url)
    if raw is None and path.exists():
        path.unlink()
    return raw, prov


def status_for(wh, team_id, game_date_report_url):
    """Listings for one team in one report: ({player_id: status}, submitted flag)."""
    rep = wh.query("SELECT * FROM injury_reports WHERE url = ?", (game_date_report_url,))
    if not rep:
        return None, False
    submitted = team_id in json.loads(rep[0]["teams_submitted"])
    rows = wh.query("SELECT player_id, status FROM injuries WHERE source_id = ? AND team_id = ?", (game_date_report_url, team_id))
    return {r["player_id"]: r["status"] for r in rows}, submitted


# ----------------------------------------------------------------- availability model inputs
def _role(mins):
    if not mins:
        return "none"
    m = sum(mins[-10:]) / len(mins[-10:])
    return "starter" if m >= 28 else "rotation" if m >= 16 else "bench" if m >= 6 else "deep"


def _streak_bucket(s):
    return "0" if s == 0 else "1" if s == 1 else "2-3" if s <= 3 else "4-9" if s <= 9 else "10+"


def report_for(wh, team_id, tipoff, cutoff):
    """Latest stored report for the game's ET date published at or before cutoff:
    returns ({player_id: status}, url) if this team had submitted, else (None, url|None)."""
    tip = datetime.fromisoformat(tipoff.replace("Z", "+00:00")).astimezone(ET)
    rep = wh.query("""SELECT * FROM injury_reports WHERE report_date = ? AND known_at <= ? ORDER BY known_at DESC LIMIT 1""",
                   (tip.strftime("%Y-%m-%d"), cutoff))
    if not rep:
        return None, None
    rep = rep[0]
    if team_id not in json.loads(rep["teams_submitted"]):
        return None, rep["url"]
    rows = wh.query("SELECT player_id, status FROM injuries WHERE source_id = ? AND team_id = ?", (rep["url"], team_id))
    return {r["player_id"]: r["status"] for r in rows}, rep["url"]


def build_availability_rows(wh, progress=print):
    """One row per (game, rostered player): pregame status bucket, role and streak
    (from prior games only) and whether the player appeared. Stored in
    `availability_rows`; a fit at cutoff C uses rows with tipoff < C."""
    with wh.connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS availability_rows(game_id TEXT, player_id TEXT, team_id TEXT, tipoff TEXT,
            status TEXT, role TEXT, streak TEXT, played INTEGER, PRIMARY KEY(game_id, player_id))""")
        db.execute("DELETE FROM availability_rows")
    games = wh.query("SELECT g.id, g.tipoff, g.home_id, g.away_id, r.url, r.known_at FROM games g "
                     "LEFT JOIN game_injury_report r ON r.game_id = g.id ORDER BY g.tipoff")
    pg = wh.query("SELECT game_id, player_id, team_id, minutes, dnp FROM player_games")
    by_game = {}
    for r in pg:
        by_game.setdefault(r["game_id"], {})[r["player_id"]] = r
    team_seq = {}
    for g in games:
        for t in (g["home_id"], g["away_id"]):
            team_seq.setdefault(t, []).append(g)
    listing = {}
    for r in wh.query("SELECT team_id, player_id, status, source_id FROM injuries"):
        listing.setdefault((r["source_id"], r["team_id"]), {})[r["player_id"]] = r["status"]
    submitted = {r["url"]: set(json.loads(r["teams_submitted"])) for r in wh.query("SELECT url, teams_submitted FROM injury_reports")}
    out = []
    for team, seq in team_seq.items():
        history = {}  # pid -> list of minutes (0 = missed) for games with this team since first appearance
        last_team_game = {}
        for idx, g in enumerate(seq):
            box = by_game.get(g["id"], {})
            # Roster candidates: appeared for this team within its previous 15 games.
            roster = {pid for pid, last in last_team_game.items() if idx - last <= 15}
            url = g["url"]
            status_map = listing.get((url, team), {}) if url and team in submitted.get(url, set()) else None
            for pid in roster:
                mins = [m for m in history[pid] if m > 0]
                streak = 0
                for m in reversed(history[pid]):
                    if m > 0:
                        break
                    streak += 1
                if status_map is None:
                    status = "NoReport"
                else:
                    status = status_map.get(pid, "NotListed")
                row = box.get(pid)
                played = int(bool(row and row["team_id"] == team and not row["dnp"] and row["minutes"] > 0))
                if row and row["team_id"] != team:
                    continue  # traded away
                out.append((g["id"], pid, team, g["tipoff"], status, _role(mins), _streak_bucket(streak), played))
            for pid, row in box.items():
                if row["team_id"] == team:
                    history.setdefault(pid, [])
                    last_team_game[pid] = idx
            for pid in list(history):
                if pid in roster or pid in box:
                    r = box.get(pid)
                    history[pid].append(r["minutes"] if r and r["team_id"] == team and not r["dnp"] else 0.0)
    with wh.connect() as db:
        db.executemany("INSERT OR REPLACE INTO availability_rows VALUES(?,?,?,?,?,?,?,?)", out)
    progress(f"{len(out)} availability rows")
    return len(out)


def fit_status_model(wh, cutoff, k=4.0):
    """Laplace-style smoothed P(appears) by (status, role) for listed players and
    by (NotListed, streak, role) for unlisted players on submitted teams."""
    rows = wh.query("""SELECT status, role, streak, COUNT(*) n, SUM(played) p FROM availability_rows
        WHERE tipoff < ? AND status != 'NoReport' GROUP BY status, role, streak""", (cutoff,))
    cells = {}
    for r in rows:
        key = f"NotListed|{r['streak']}|{r['role']}" if r["status"] == "NotListed" else f"{r['status']}|{r['role']}"
        c = cells.setdefault(key, [0, 0])
        c[0] += r["p"]
        c[1] += r["n"]
    by_status = {}
    for key, (p, n) in cells.items():
        s = key.split("|")[0]
        t = by_status.setdefault(s, [0, 0])
        t[0] += p
        t[1] += n
    table = {}
    for key, (p, n) in cells.items():
        sp, sn = by_status[key.split("|")[0]]
        prior = (sp + 1) / (sn + 2)
        table[key] = round((p + k * prior) / (n + k), 4)
    return {"family": "injury_status_availability", "trained_through": cutoff, "table": table,
            "cells": {k_: v[1] for k_, v in cells.items()},
            "status_rates": {s: round(v[0] / v[1], 4) for s, v in by_status.items() if v[1]}}
