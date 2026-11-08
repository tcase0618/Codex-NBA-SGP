import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .schemas import Dataset
from .schemas import utc


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS datasets(
                    id TEXT PRIMARY KEY, imported_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS forecasts(
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outcomes(
                    forecast_id TEXT PRIMARY KEY REFERENCES forecasts(id),
                    recorded_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sgp_evaluations(
                    id TEXT PRIMARY KEY, forecast_id TEXT NOT NULL REFERENCES forecasts(id),
                    created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS draft_bundles(
                    id TEXT PRIMARY KEY, game_id TEXT NOT NULL, tipoff TEXT NOT NULL,
                    created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS market_snapshots(
                    id TEXT PRIMARY KEY, game_id TEXT NOT NULL, tipoff TEXT NOT NULL,
                    created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS immutable_drafts BEFORE UPDATE ON draft_bundles
                    BEGIN SELECT RAISE(ABORT, 'Frozen draft'); END;
                CREATE TRIGGER IF NOT EXISTS no_delete_drafts BEFORE DELETE ON draft_bundles
                    BEGIN SELECT RAISE(ABORT, 'Frozen draft'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_markets BEFORE UPDATE ON market_snapshots
                    BEGIN SELECT RAISE(ABORT, 'Frozen market'); END;
                CREATE TRIGGER IF NOT EXISTS no_delete_markets BEFORE DELETE ON market_snapshots
                    BEGIN SELECT RAISE(ABORT, 'Frozen market'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_sgp BEFORE UPDATE ON sgp_evaluations
                    BEGIN SELECT RAISE(ABORT, 'Frozen SGP'); END;
                CREATE TRIGGER IF NOT EXISTS no_delete_sgp BEFORE DELETE ON sgp_evaluations
                    BEGIN SELECT RAISE(ABORT, 'Frozen SGP'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_forecasts BEFORE UPDATE ON forecasts
                    BEGIN SELECT RAISE(ABORT, 'Frozen forecast'); END;
                CREATE TRIGGER IF NOT EXISTS no_delete_forecasts BEFORE DELETE ON forecasts
                    BEGIN SELECT RAISE(ABORT, 'Frozen forecast'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_datasets BEFORE UPDATE ON datasets
                    BEGIN SELECT RAISE(ABORT, 'Immutable source'); END;
                CREATE TRIGGER IF NOT EXISTS no_delete_datasets BEFORE DELETE ON datasets
                    BEGIN SELECT RAISE(ABORT, 'Immutable source'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_outcomes BEFORE UPDATE ON outcomes
                    BEGIN SELECT RAISE(ABORT, 'Immutable outcome'); END;
                CREATE TRIGGER IF NOT EXISTS no_delete_outcomes BEFORE DELETE ON outcomes
                    BEGIN SELECT RAISE(ABORT, 'Immutable outcome'); END;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA foreign_keys=ON")
        # REPLACE deletes the conflicting row; recursive triggers keep frozen rows
        # protected from that alternate mutation path on this connection.
        db.execute("PRAGMA recursive_triggers=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_dataset(self, dataset: Dataset):
        payload = dataset.model_dump(mode="json")
        key = digest(payload)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO datasets VALUES(?,?,?)", (
                key, datetime.now(timezone.utc).isoformat(), canonical(payload)))
        return key

    def datasets(self):
        with self.connect() as db:
            results = []
            for row in db.execute("SELECT * FROM datasets ORDER BY imported_at, id"):
                payload = json.loads(row[2])
                if digest(payload) != row[0]:
                    raise ValueError("Persisted source content hash mismatch")
                results.append({"id": row[0], "imported_at": row[1], **payload})
            return results

    def save_forecast(self, payload):
        with self.connect() as db:
            db.execute("INSERT INTO forecasts VALUES(?,?,?)", (
                payload["id"], payload["created_at"], canonical(payload)))

    def forecasts(self, limit=100):
        with self.connect() as db:
            if limit is None:
                return [json.loads(r[0]) for r in db.execute("SELECT payload FROM forecasts ORDER BY created_at DESC")]
            return [json.loads(r[0]) for r in db.execute(
                "SELECT payload FROM forecasts ORDER BY created_at DESC LIMIT ?", (limit,))]

    def forecast(self, key):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM forecasts WHERE id=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def record_outcome(self, forecast_id, payload):
        with self.connect() as db:
            db.execute("INSERT INTO outcomes VALUES(?,?,?)", (
                forecast_id, datetime.now(timezone.utc).isoformat(), canonical(payload)))

    def outcomes(self):
        with self.connect() as db:
            return [{"forecast_id": r[0], **json.loads(r[1])} for r in db.execute(
                "SELECT forecast_id,payload FROM outcomes ORDER BY recorded_at")]

    def save_sgp(self, payload):
        with self.connect() as db:
            db.execute("INSERT INTO sgp_evaluations VALUES(?,?,?,?)", (
                payload["id"], payload["forecast_id"], payload["created_at"], canonical(payload)))

    def sgp(self, key):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM sgp_evaluations WHERE id=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def sgps(self, forecast_ids):
        keys = set(forecast_ids)
        if not keys:
            return []
        with self.connect() as db:
            # Bound tickets to exact saved forecasts; a team-name match is insufficient.
            return [json.loads(row[1]) for row in db.execute(
                "SELECT forecast_id,payload FROM sgp_evaluations ORDER BY created_at DESC, id") if row[0] in keys]

    def save_drafts(self, payload):
        with self.connect() as db:
            db.execute("INSERT INTO draft_bundles VALUES(?,?,?,?,?)", (
                payload["id"], payload["game_id"], utc(payload["tipoff"]).isoformat(), payload["created_at"], canonical(payload)))

    def latest_drafts(self, game_id, tipoff):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM draft_bundles WHERE game_id=? AND tipoff=? ORDER BY created_at DESC LIMIT 1", (game_id, utc(tipoff).isoformat())).fetchone()
            return json.loads(row[0]) if row else None

    def save_market(self, payload):
        with self.connect() as db:
            db.execute("INSERT INTO market_snapshots VALUES(?,?,?,?,?)", (
                payload["id"], payload["game_id"], utc(payload["tipoff"]).isoformat(), payload["created_at"], canonical(payload)))

    def latest_market(self, game_id, tipoff):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM market_snapshots WHERE game_id=? AND tipoff=? ORDER BY created_at DESC LIMIT 1", (game_id, utc(tipoff).isoformat())).fetchone()
            return json.loads(row[0]) if row else None
