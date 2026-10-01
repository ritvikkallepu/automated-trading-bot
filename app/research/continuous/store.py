"""Append-only observations and publications with separate mutable replay results."""
import json
from pathlib import Path
import sqlite3
import time
import uuid
import zlib
import hashlib
from contextlib import closing, contextmanager


def encode(value):
    raw = json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))
    packed = b"CRZ1" + zlib.compress(raw.encode("utf-8"))
    return packed if len(packed) < len(raw.encode("utf-8")) else raw


def decode(value):
    if isinstance(value, bytes) and value.startswith(b"CRZ1"):
        value = zlib.decompress(value[4:])
    return json.loads(value)


TABLES = ("observations", "publications", "replays", "state", "closed_candles",
          "setups", "setup_events", "validation_samples", "validation_results")


class HistoryStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS observations (
                    id TEXT PRIMARY KEY, family TEXT, asset TEXT, fetched_ms INTEGER,
                    config_id TEXT, payload TEXT);
                CREATE INDEX IF NOT EXISTS observations_lookup
                    ON observations(family, asset, fetched_ms);
                CREATE TABLE IF NOT EXISTS publications (
                    id TEXT PRIMARY KEY, published_ms INTEGER, payload TEXT);
                CREATE INDEX IF NOT EXISTS publications_time ON publications(published_ms);
                CREATE TABLE IF NOT EXISTS replays (
                    id TEXT PRIMARY KEY, updated_ms INTEGER, payload TEXT);
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, payload TEXT);
                CREATE TABLE IF NOT EXISTS closed_candles (
                    asset TEXT, opened_ms INTEGER, payload TEXT,
                    PRIMARY KEY(asset, opened_ms));
                CREATE TABLE IF NOT EXISTS setups (
                    id TEXT PRIMARY KEY, symbol TEXT, published_ms INTEGER, payload TEXT);
                CREATE INDEX IF NOT EXISTS setups_symbol ON setups(symbol, published_ms);
                CREATE TABLE IF NOT EXISTS setup_events (
                    id TEXT PRIMARY KEY, setup_id TEXT, at_ms INTEGER, payload TEXT);
                CREATE TABLE IF NOT EXISTS validation_samples (
                    id TEXT PRIMARY KEY, symbol TEXT, published_ms INTEGER, payload TEXT);
                CREATE TABLE IF NOT EXISTS validation_results (
                    id TEXT PRIMARY KEY, payload TEXT);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def observe(self, family, asset, fetched_ms, config_id, payload):
        oid = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO observations VALUES (?,?,?,?,?,?)",
                       (oid, family, asset, fetched_ms, config_id, encode(payload)))
            if family == "market" and payload.get("status") == "available":
                cutoff = payload.get("closed_through_ms", -1)
                interval = payload.get("market_interval", "15m")
                asset_key = asset if interval == "15m" else f"{asset}|{interval}"
                confirmed = [r for r in payload["raw"]["candles"] if int(r[6]) <= cutoff]
                db.executemany("INSERT OR IGNORE INTO closed_candles VALUES (?,?,?)",
                               [(asset_key, int(r[0]), encode(r)) for r in confirmed])
        return oid

    def latest(self, family, asset, config_id=None):
        with self.connect() as db:
            q = "SELECT id,fetched_ms,payload FROM observations WHERE family=? AND asset=?"
            args = [family, asset]
            if config_id:
                q += " AND config_id=?"
                args.append(config_id)
            row = db.execute(q + " ORDER BY fetched_ms DESC, rowid DESC LIMIT 1", args).fetchone()
        return {"id": row[0], "fetched_ms": row[1], "data": decode(row[2])} if row else None

    def observation(self, oid):
        with self.connect() as db:
            row = db.execute("SELECT family,asset,fetched_ms,config_id,payload FROM observations WHERE id=?", (oid,)).fetchone()
        return {"id": oid, "family": row[0], "asset": row[1], "fetched_ms": row[2],
                "config_id": row[3], "data": decode(row[4])} if row else None

    def publish(self, snapshot, setups=(), events=(), samples=()):
        with self.connect() as db:
            db.execute("INSERT INTO publications VALUES (?,?,?)",
                       (snapshot["id"], snapshot["published_ms"], encode(snapshot)))
            db.executemany("INSERT OR REPLACE INTO setups VALUES (?,?,?,?)",
                           [(s["id"], s["symbol"], s["published_ms"], encode(s)) for s in setups])
            db.executemany("INSERT INTO setup_events VALUES (?,?,?,?)",
                           [(e["id"], e["setup_id"], e["at_ms"], encode(e)) for e in events])
            db.executemany("INSERT INTO validation_samples VALUES (?,?,?,?)",
                           [(s["id"], s["symbol"], s["published_ms"], encode(s)) for s in samples])

    def setups(self):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM setups ORDER BY published_ms, rowid").fetchall()
        return [decode(r[0]) for r in rows]

    def setup_events(self, setup_id=None):
        with self.connect() as db:
            query = "SELECT payload FROM setup_events"
            rows = db.execute(query + (" WHERE setup_id=?" if setup_id else "") + " ORDER BY at_ms, rowid",
                              (setup_id,) if setup_id else ()).fetchall()
        return [decode(r[0]) for r in rows]

    def validation_samples(self):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM validation_samples ORDER BY published_ms, rowid").fetchall()
        return [decode(r[0]) for r in rows]

    def save_validation(self, key, result):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO validation_results VALUES (?,?)", (key, encode(result)))

    def validation_results(self):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM validation_results ORDER BY rowid DESC").fetchall()
        return [decode(r[0]) for r in rows]

    def latest_publication(self):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM publications ORDER BY published_ms DESC, rowid DESC LIMIT 1").fetchone()
        return decode(row[0]) if row else None

    def publication_at(self, published_ms, config_id):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM publications WHERE published_ms=? ORDER BY rowid", (published_ms,)).fetchall()
        matches = [decode(r[0]) for r in rows]
        matches = [s for s in matches if s.get("config_id") == config_id]
        return matches[0] if len(matches) == 1 else None

    def publications_since(self, since_ms):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM publications WHERE published_ms>=? ORDER BY published_ms", (since_ms,)).fetchall()
        return [decode(r[0]) for r in rows]

    def state(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM state WHERE key=?", (key,)).fetchone()
        return decode(row[0]) if row else default

    def set_state(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, encode(value)))

    def save_replay(self, key, result):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO replays VALUES (?,?,?)", (key, int(time.time()*1000), encode(result)))

    def replay_results(self):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM replays ORDER BY updated_ms DESC").fetchall()
        return [decode(r[0]) for r in rows]

    def claim(self, owner, expires_ms):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM state WHERE key='worker_lease'").fetchone()
            lease = decode(row[0]) if row else {}
            if lease.get("expires_ms", 0) > time.time()*1000 and lease.get("owner") != owner:
                return False
            db.execute("INSERT OR REPLACE INTO state VALUES ('worker_lease',?)",
                       (encode({"owner": owner, "expires_ms": expires_ms}),))
        return True

    def release(self, owner):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM state WHERE key='worker_lease'").fetchone()
            if row and decode(row[0]).get("owner") == owner:
                db.execute("DELETE FROM state WHERE key='worker_lease'")

    def candles(self, symbol, since_ms=0, interval="15m"):
        if interval not in {"5m", "15m"}:
            raise ValueError(f"Unsupported market interval: {interval}")
        asset_key = symbol if interval == "15m" else f"{symbol}|{interval}"
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM closed_candles WHERE asset=? AND opened_ms>=? ORDER BY opened_ms", (asset_key,since_ms)).fetchall()
        return [decode(r[0]) for r in rows]

    def all_markets(self):
        with self.connect() as db:
            symbols = [r[0] for r in db.execute("SELECT DISTINCT asset FROM closed_candles")]
        return {s: {"candles": self.candles(s)} for s in symbols}

    def storage_status(self):
        # SQLite may remove the WAL between filesystem checks when a reader closes.
        try:
            wal_bytes = Path(str(self.path)+"-wal").stat().st_size
        except FileNotFoundError:
            wal_bytes = 0
        return {"database_bytes": self.path.stat().st_size,
                "wal_bytes": wal_bytes,
                "encoding": "lossless JSON / zlib", "last_compaction": self.state("last_compaction")}

    def compact(self, backup_path):
        """Offline, lossless migration. Stop the research worker before calling."""
        backup_path = Path(backup_path)
        if backup_path.exists() or backup_path.resolve() == self.path.resolve():
            raise ValueError("Choose a new backup filename")
        lease = self.state("worker_lease", {})
        if lease.get("expires_ms", 0) > time.time()*1000:
            raise ValueError("Stop the research worker before compacting")
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Database integrity check failed")
            with closing(sqlite3.connect(backup_path)) as backup:
                db.backup(backup)
            before = self.path.stat().st_size
            digests, counts = {}, {}
            db.execute("BEGIN IMMEDIATE")
            for table in TABLES:
                digest, count, cursor = hashlib.sha256(), 0, 0
                while True:
                    rows = db.execute(f"SELECT rowid,payload FROM {table} WHERE rowid>? ORDER BY rowid LIMIT 200", (cursor,)).fetchall()
                    if not rows:
                        break
                    updates = []
                    for rid, payload in rows:
                        value = decode(payload)
                        canonical = json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
                        digest.update(str(rid).encode()+b":"+canonical+b"\n")
                        updates.append((encode(value), rid))
                    db.executemany(f"UPDATE {table} SET payload=? WHERE rowid=?", updates)
                    cursor, count = rows[-1][0], count+len(rows)
                check = hashlib.sha256()
                for rid, payload in db.execute(f"SELECT rowid,payload FROM {table} ORDER BY rowid"):
                    canonical = json.dumps(decode(payload), sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
                    check.update(str(rid).encode()+b":"+canonical+b"\n")
                if check.digest() != digest.digest():
                    raise ValueError("Compaction verification failed; transaction rolled back")
                digests[table], counts[table] = digest.hexdigest(), count
        with self.connect() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("VACUUM")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Post-compaction integrity check failed; backup retained")
        result = {"at_ms": int(time.time()*1000), "before_bytes": before,
                  "after_bytes": self.path.stat().st_size, "rows": counts,
                  "verified_sha256": digests, "backup": str(backup_path.resolve())}
        self.set_state("last_compaction", result)
        return result
