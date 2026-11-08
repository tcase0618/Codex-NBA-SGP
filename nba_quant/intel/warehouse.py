"""Normalized analytical warehouse (SQLite) for the intelligence platform.

The immutable research store (`nba_quant.store`) keeps canonical source
snapshots for frozen SGP forecasts. This warehouse holds the play-by-play-level
analytical model: games, player games, play events, shots, possessions, lineup
stints, data sources, metric snapshots, model versions, predictions, simulation
runs and validation results. It is rebuildable from the ESPN response cache.

Entities with no authorized source (optical tracking, defender matchups) exist
as empty tables registered as UNAVAILABLE in `data_sources`; nothing is
synthesized into them.
"""
import gzip
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .parse import parse_summary
from .reconstruct import reconstruct

DEFAULT_DB = Path("output/warehouse.sqlite")
NBA_TEAM_IDS = frozenset(str(i) for i in range(1, 31))  # ESPN franchise IDs; excludes All-Star exhibitions
SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS data_sources(
  id TEXT PRIMARY KEY, provider TEXT, provider_class TEXT, url TEXT, retrieved_at TEXT,
  sha256 TEXT, bytes INTEGER, status TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS teams(id TEXT PRIMARY KEY, abbreviation TEXT, name TEXT, location TEXT);
CREATE TABLE IF NOT EXISTS players(id TEXT PRIMARY KEY, name TEXT, position TEXT, jersey TEXT,
  last_team_id TEXT, last_game_at TEXT);
CREATE TABLE IF NOT EXISTS games(
  id TEXT PRIMARY KEY, season INTEGER, season_type INTEGER, tipoff TEXT, home_id TEXT, away_id TEXT,
  home_score INTEGER, away_score INTEGER, periods INTEGER, neutral_site INTEGER, venue TEXT,
  attendance INTEGER, source_id TEXT, retrieved_at TEXT, pbp_valid INTEGER, lineup_repairs INTEGER,
  home_poss INTEGER, away_poss INTEGER, quality TEXT);
CREATE INDEX IF NOT EXISTS games_tipoff ON games(tipoff);
CREATE TABLE IF NOT EXISTS periods(game_id TEXT, period INTEGER, team_id TEXT, points INTEGER,
  possessions INTEGER, PRIMARY KEY(game_id, period, team_id));
CREATE TABLE IF NOT EXISTS player_games(
  game_id TEXT, player_id TEXT, team_id TEXT, opponent_id TEXT, tipoff TEXT, home INTEGER,
  starter INTEGER, dnp INTEGER, dnp_reason TEXT, ejected INTEGER, minutes REAL, pts INTEGER,
  fgm INTEGER, fga INTEGER, fg3m INTEGER, fg3a INTEGER, ftm INTEGER, fta INTEGER, oreb INTEGER,
  dreb INTEGER, reb INTEGER, ast INTEGER, tov INTEGER, stl INTEGER, blk INTEGER, pf INTEGER,
  plus_minus INTEGER, on_seconds REAL, on_pts_for INTEGER, on_pts_against INTEGER,
  on_poss_off INTEGER, on_poss_def INTEGER, on_oreb INTEGER, on_dreb INTEGER, on_opp_oreb INTEGER,
  on_opp_dreb INTEGER, PRIMARY KEY(game_id, player_id));
CREATE INDEX IF NOT EXISTS pg_player ON player_games(player_id, tipoff);
CREATE INDEX IF NOT EXISTS pg_team ON player_games(team_id, tipoff);
CREATE TABLE IF NOT EXISTS play_events(
  game_id TEXT, seq REAL, period INTEGER, clock REAL, elapsed REAL, type_id INTEGER, type TEXT,
  text TEXT, team_id TEXT, p1 TEXT, p2 TEXT, p3 TEXT, scoring INTEGER, score_value INTEGER,
  home_score INTEGER, away_score INTEGER, shooting INTEGER, points_attempted INTEGER, x REAL, y REAL,
  stint_index INTEGER, PRIMARY KEY(game_id, seq));
CREATE VIEW IF NOT EXISTS substitutions AS
  SELECT game_id, seq, period, clock, elapsed, team_id, p1 AS player_in, p2 AS player_out
  FROM play_events WHERE type = 'Substitution';
CREATE TABLE IF NOT EXISTS shots(
  game_id TEXT, seq INTEGER, tipoff TEXT, period INTEGER, clock REAL, elapsed REAL, team_id TEXT,
  opponent_id TEXT, shooter_id TEXT, assist_id TEXT, block_id TEXT, made INTEGER,
  points_attempted INTEGER, points INTEGER, x REAL, y REAL, distance_ft REAL, zone TEXT,
  corner_three INTEGER, angle_deg REAL, family TEXT, type TEXT, second_chance INTEGER,
  seconds_into_possession REAL, transition INTEGER, score_margin INTEGER, home INTEGER,
  team_attempt_heave INTEGER, possession_index INTEGER, offense_lineup TEXT, defense_lineup TEXT,
  PRIMARY KEY(game_id, seq));
CREATE INDEX IF NOT EXISTS shots_shooter ON shots(shooter_id, tipoff);
CREATE INDEX IF NOT EXISTS shots_team ON shots(team_id, tipoff);
CREATE INDEX IF NOT EXISTS shots_opp ON shots(opponent_id, tipoff);
CREATE TABLE IF NOT EXISTS possessions(
  game_id TEXT, possession_index INTEGER, tipoff TEXT, period INTEGER, offense_id TEXT, defense_id TEXT,
  start REAL, end REAL, duration REAL, start_type TEXT, end_reason TEXT, points INTEGER, fga INTEGER,
  fg3a INTEGER, fta INTEGER, oreb INTEGER, tov INTEGER, second_chance_points INTEGER,
  transition INTEGER, first_action REAL, stint_index INTEGER, offense_lineup TEXT, defense_lineup TEXT,
  start_margin INTEGER, PRIMARY KEY(game_id, possession_index));
CREATE INDEX IF NOT EXISTS poss_off ON possessions(offense_id, tipoff);
CREATE TABLE IF NOT EXISTS lineup_stints(
  game_id TEXT, stint_index INTEGER, tipoff TEXT, period INTEGER, start REAL, end REAL, seconds REAL,
  home_id TEXT, away_id TEXT, home_lineup TEXT, away_lineup TEXT, home_pts INTEGER, away_pts INTEGER,
  home_poss INTEGER, away_poss INTEGER, home_fga INTEGER, away_fga INTEGER, home_fg3a INTEGER,
  away_fg3a INTEGER, home_fta INTEGER, away_fta INTEGER, home_oreb INTEGER, away_oreb INTEGER,
  home_dreb INTEGER, away_dreb INTEGER, home_tov INTEGER, away_tov INTEGER, valid INTEGER,
  PRIMARY KEY(game_id, stint_index));
CREATE INDEX IF NOT EXISTS stints_tipoff ON lineup_stints(tipoff);
CREATE TABLE IF NOT EXISTS injuries(team_id TEXT, player_id TEXT, status TEXT, detail TEXT,
  known_at TEXT, source_id TEXT, PRIMARY KEY(player_id, known_at));
CREATE TABLE IF NOT EXISTS tracking_events(game_id TEXT, seq INTEGER, payload TEXT);
CREATE TABLE IF NOT EXISTS player_matchups(game_id TEXT, offense_id TEXT, defense_id TEXT,
  partial_possessions REAL, payload TEXT);
CREATE TABLE IF NOT EXISTS metric_definitions(id TEXT PRIMARY KEY, scope TEXT, category TEXT,
  label TEXT, status TEXT, formula TEXT, unit TEXT, source TEXT, denominator TEXT, requires TEXT);
CREATE TABLE IF NOT EXISTS metric_snapshots(key TEXT PRIMARY KEY, created_at TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS model_versions(id TEXT PRIMARY KEY, family TEXT, created_at TEXT,
  trained_through TEXT, params TEXT, metrics TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS model_predictions(id TEXT PRIMARY KEY, model_version TEXT, game_id TEXT,
  created_at TEXT, cutoff TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS simulation_runs(id TEXT PRIMARY KEY, created_at TEXT, game_key TEXT,
  simulations INTEGER, seed INTEGER, input_hash TEXT, seconds REAL, payload TEXT);
CREATE TABLE IF NOT EXISTS validation_results(id TEXT PRIMARY KEY, created_at TEXT, kind TEXT,
  model_version TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS market_lines(game_id TEXT PRIMARY KEY, provider TEXT, home_spread REAL, total REAL,
  home_moneyline REAL, away_moneyline REAL, source_id TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS ingest_log(game_id TEXT PRIMARY KEY, source_sha256 TEXT, ingested_at TEXT,
  status TEXT, error TEXT);
"""

UNAVAILABLE_SOURCES = [
    ("optical-tracking", "Licensed optical tracking (Second Spectrum/Hawk-Eye)",
     "No licensed access. Speed, distance, touches, defender distance, deflections, boxouts and matchup data are UNAVAILABLE."),
    ("nba-stats-api", "NBA Stats (stats.nba.com)",
     "Not reachable from this environment (connection timeout); cdn.nba.com returned HTTP 403. Play-type (Synergy) data is licensed."),
    ("historical-injury-reports", "Official NBA injury reports (historical)",
     "Not ingested. Historical availability uses only prior-game participation known before tipoff."),
]


class Warehouse:
    def __init__(self, path=DEFAULT_DB):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
            for key, name, note in UNAVAILABLE_SOURCES:
                db.execute("INSERT OR REPLACE INTO data_sources(id, provider, status, note) VALUES(?,?,?,?)",
                           (key, name, "UNAVAILABLE", note))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=60)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def query(self, sql, params=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, params)]

    def scalar(self, sql, params=()):
        with self.connect() as db:
            row = db.execute(sql, params).fetchone()
            return row[0] if row else None

    # ------------------------------------------------------------------ ingest
    def ingest(self, parsed, provenance):
        g = parsed["game"]
        rec = reconstruct(parsed)
        q = rec["quality"]
        home, away = g["home_id"], g["away_id"]
        periods = max([p["period"] for p in parsed["plays"]] or [4])
        tip = g["tipoff"]
        with self.connect() as db:
            for table in ("player_games", "play_events", "shots", "possessions", "lineup_stints", "periods"):
                db.execute(f"DELETE FROM {table} WHERE game_id=?", (g["id"],))
            db.execute("INSERT OR REPLACE INTO data_sources VALUES(?,?,?,?,?,?,?,?,?)", (
                provenance["sha256"], provenance["provider"], provenance["provider_class"], provenance["url"],
                provenance["retrieved_at"], provenance["sha256"], provenance.get("bytes"), "INGESTED", None))
            for t in parsed["teams"]:
                db.execute("INSERT OR REPLACE INTO teams VALUES(?,?,?,?)", (t["id"], t["abbreviation"], t["name"], t["location"]))
            for p in parsed["players"]:
                team = next((b["team_id"] for b in parsed["boxes"] if b["player_id"] == p["id"]), None)
                db.execute("""INSERT INTO players VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, position=CASE WHEN excluded.last_game_at >= players.last_game_at THEN excluded.position ELSE players.position END,
                    jersey=excluded.jersey,
                    last_team_id=CASE WHEN excluded.last_game_at >= players.last_game_at THEN excluded.last_team_id ELSE players.last_team_id END,
                    last_game_at=MAX(players.last_game_at, excluded.last_game_at)""",
                           (p["id"], p["name"], p["position"], p["jersey"], team, tip))
            db.execute("INSERT OR REPLACE INTO games VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                g["id"], g["season"], g["season_type"], tip, home, away, g["home_score"], g["away_score"], periods,
                int(g["neutral_site"]), g["venue"], g["attendance"], provenance["sha256"], provenance["retrieved_at"],
                int(q["valid"]), len(q.get("lineup_repairs", [])),
                q.get("possessions", {}).get(home), q.get("possessions", {}).get(away), json.dumps(q)))
            onoff = rec["player_onoff"]
            for b in parsed["boxes"]:
                o = onoff.get(b["player_id"], {})
                db.execute("INSERT INTO player_games VALUES(" + ",".join("?" * 36) + ")", (
                    g["id"], b["player_id"], b["team_id"], away if b["team_id"] == home else home, tip,
                    int(b["team_id"] == home), int(b["starter"]), int(b["dnp"]), b["dnp_reason"], int(b["ejected"]),
                    b["minutes"], b["pts"], b["fgm"], b["fga"], b["fg3m"], b["fg3a"], b["ftm"], b["fta"], b["oreb"],
                    b["dreb"], b["reb"], b["ast"], b["tov"], b["stl"], b["blk"], b["pf"], b["plus_minus"],
                    o.get("seconds"), o.get("pts_for"), o.get("pts_against"), o.get("poss_off"), o.get("poss_def"),
                    o.get("oreb"), o.get("dreb"), o.get("opp_oreb"), o.get("opp_dreb")))
            stint_of = {e["seq"]: e["stint_index"] for e in rec["events"]}
            db.executemany("INSERT INTO play_events VALUES(" + ",".join("?" * 21) + ")", [(
                p["game_id"], p["seq"], p["period"], p["clock"], p["elapsed"], p["type_id"], p["type"], p["text"],
                p["team_id"], p["p1"], p["p2"], p["p3"], int(p["scoring"]), p["score_value"], p["home_score"],
                p["away_score"], int(p["shooting"]), p["points_attempted"], p["x"], p["y"], stint_of.get(p["seq"]))
                for p in parsed["plays"]])
            db.executemany("INSERT INTO shots VALUES(" + ",".join("?" * 31) + ")", [(
                s["game_id"], s["seq"], tip, s["period"], s["clock"], s["elapsed"], s["team_id"], s["opponent_id"],
                s["shooter_id"], s["assist_id"], s["block_id"], int(s["made"]), s["points_attempted"], s["points"],
                s["x"], s["y"], s["distance_ft"], s["zone"], None if s["corner_three"] is None else int(s["corner_three"]),
                s["angle_deg"], s["family"], s["type"], int(s["second_chance"]), s["seconds_into_possession"],
                int(s["transition"]), s["score_margin"], int(s["home"]), int(s["team_attempt_heave"]),
                s["possession_index"], ",".join(s["offense_lineup"]), ",".join(s["defense_lineup"])) for s in rec["shots"]])
            db.executemany("INSERT INTO possessions VALUES(" + ",".join("?" * 24) + ")", [(
                p["game_id"], p["possession_index"], tip, p["period"], p["offense_id"], p["defense_id"], p["start"],
                p["end"], p["duration"], p["start_type"], p["end_reason"], p["points"], p["fga"], p["fg3a"], p["fta"],
                p["oreb"], p["tov"], p["second_chance_points"], int(p["transition"]), p["first_action"],
                p["stint_index"], ",".join(p["offense_lineup"]), ",".join(p["defense_lineup"]), p["start_margin"])
                for p in rec["possessions"]])
            db.executemany("INSERT INTO lineup_stints VALUES(" + ",".join("?" * 28) + ")", [(
                s["game_id"], s["stint_index"], tip, s["period"], s["start"], s["end"], s["seconds"], home, away,
                ",".join(s["home_lineup"]), ",".join(s["away_lineup"]), s["home_pts"], s["away_pts"], s["home_poss"],
                s["away_poss"], s["home_fga"], s["away_fga"], s["home_fg3a"], s["away_fg3a"], s["home_fta"],
                s["away_fta"], s["home_oreb"], s["away_oreb"], s["home_dreb"], s["away_dreb"], s["home_tov"],
                s["away_tov"], int(s["valid"])) for s in rec["stints"]])
            # Period scoring from scoring events (includes technical free throws).
            period_pts = {}
            for p in parsed["plays"]:
                if p["scoring"] and p["team_id"] in (home, away):
                    period_pts[(p["period"], p["team_id"])] = period_pts.get((p["period"], p["team_id"]), 0) + p["score_value"]
            for period in range(1, periods + 1):
                for t in (home, away):
                    db.execute("INSERT INTO periods VALUES(?,?,?,?,?)", (
                        g["id"], period, t, period_pts.get((period, t), 0),
                        sum(1 for p in rec["possessions"] if p["period"] == period and p["offense_id"] == t)))
            line = parsed.get("market_line")
            db.execute("DELETE FROM market_lines WHERE game_id=?", (g["id"],))
            if line:
                db.execute("INSERT INTO market_lines VALUES(?,?,?,?,?,?,?,?)", (
                    g["id"], line["provider"], line["home_spread"], line["total"], line["home_moneyline"],
                    line["away_moneyline"], provenance["sha256"],
                    "Provider-reported line embedded in the ESPN summary; observation time not stated. Benchmark only, never a model input."))
            db.execute("INSERT OR REPLACE INTO ingest_log VALUES(?,?,?,?,?)", (
                g["id"], provenance["sha256"], datetime.now(timezone.utc).isoformat(),
                "valid" if q["valid"] else "flagged", None))
        return q

    def ingested(self):
        with self.connect() as db:
            return {r[0]: r[1] for r in db.execute("SELECT game_id, source_sha256 FROM ingest_log WHERE status != 'rejected'")}

    def build(self, cache_dir, progress=print, nba_team_ids=NBA_TEAM_IDS):
        """Incrementally ingest every cached ESPN game summary."""
        done = self.ingested()
        stats = {"ingested": 0, "skipped": 0, "flagged": 0, "rejected": 0, "errors": []}
        for path in sorted(Path(cache_dir).glob("*.json.gz")):
            record = json.loads(gzip.decompress(path.read_bytes()))
            prov = record["provenance"]
            if "/summary?" not in prov["url"]:
                continue
            payload = record["payload"]
            gid = payload.get("header", {}).get("id")
            if gid and done.get(gid) == prov["sha256"]:
                stats["skipped"] += 1
                continue
            try:
                parsed = parse_summary(payload, prov)
                if parsed["game"]["season_type"] not in (2, 3):
                    continue
                if nba_team_ids and not {parsed["game"]["home_id"], parsed["game"]["away_id"]} <= nba_team_ids:
                    continue
                if not parsed["plays"] or len(parsed["boxes"]) < 10:
                    raise ValueError("Missing play-by-play or box score")
                q = self.ingest(parsed, prov)
                stats["ingested"] += 1
                stats["flagged"] += int(not q["valid"])
            except Exception as error:  # record and continue; never fabricate a game
                stats["rejected"] += 1
                stats["errors"].append({"path": path.name, "game_id": gid, "error": repr(error)[:200]})
                with self.connect() as db:
                    if gid:
                        db.execute("INSERT OR REPLACE INTO ingest_log VALUES(?,?,?,?,?)", (
                            gid, prov["sha256"], datetime.now(timezone.utc).isoformat(), "rejected", repr(error)[:500]))
            if (stats["ingested"] + stats["rejected"]) % 200 == 0 and stats["ingested"]:
                progress(f"  {stats['ingested']} ingested")
        with self.connect() as db:
            db.execute("DELETE FROM metric_snapshots")  # cached metrics depend on the game set
        return stats

    def coverage(self):
        q = self.query("""SELECT season, season_type, COUNT(*) games, SUM(pbp_valid) pbp_valid,
            MIN(tipoff) first, MAX(tipoff) last FROM games GROUP BY season, season_type ORDER BY season, season_type""")
        counts = {t: self.scalar(f"SELECT COUNT(*) FROM {t}") for t in (
            "games", "player_games", "play_events", "shots", "possessions", "lineup_stints", "players", "teams",
            "tracking_events", "player_matchups", "injuries")}
        rejected = self.query("SELECT game_id, error FROM ingest_log WHERE status='rejected'")
        sources = self.query("SELECT id, provider, status, note FROM data_sources WHERE status='UNAVAILABLE'")
        return {"seasons": q, "rows": counts, "rejected": rejected, "unavailable_sources": sources,
                "provider": "ESPN public site API (secondary statistics provider)"}
