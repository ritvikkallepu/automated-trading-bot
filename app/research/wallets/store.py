"""Transactional finalized evidence and immutable, point-in-time observations."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time

from app.research.continuous.store import encode, decode
from .rpc import EvidenceUnavailable


class WalletStore:
    def __init__(self, path, *, readonly=False):
        self.path = Path(path)
        self.readonly = readonly
        if readonly:
            if not self.path.is_file():
                raise FileNotFoundError("Wallet evidence history has not been created")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS wallet_streams (
                    scope TEXT PRIMARY KEY, chain TEXT NOT NULL, created_ms INTEGER NOT NULL,
                    config BLOB NOT NULL, next_height INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS wallet_blocks (
                    scope TEXT NOT NULL, height INTEGER NOT NULL, hash TEXT NOT NULL,
                    event_ms INTEGER, observed_ms INTEGER NOT NULL, raw BLOB NOT NULL,
                    diagnostics BLOB NOT NULL, PRIMARY KEY(scope,height));
                CREATE TABLE IF NOT EXISTS wallet_events (
                    scope TEXT NOT NULL, event_id TEXT NOT NULL, height INTEGER NOT NULL,
                    event_ms INTEGER, observed_ms INTEGER NOT NULL, payload BLOB NOT NULL,
                    PRIMARY KEY(scope,event_id));
                CREATE INDEX IF NOT EXISTS wallet_event_time ON wallet_events(scope,observed_ms);
                CREATE TABLE IF NOT EXISTS wallet_status (scope TEXT PRIMARY KEY, payload BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS wallet_empty_ranges (
                    scope TEXT NOT NULL, start_height INTEGER NOT NULL, end_height INTEGER NOT NULL,
                    observed_ms INTEGER NOT NULL, PRIMARY KEY(scope,start_height));
                CREATE TABLE IF NOT EXISTS wallet_leases (
                    scope TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_ms INTEGER NOT NULL);
            """)

    @contextmanager
    def connect(self):
        target = self.path.resolve().as_uri() + "?mode=ro" if self.readonly else str(self.path)
        db = sqlite3.connect(target, timeout=10, uri=self.readonly)
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self, scope, chain, config, height, now_ms):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO wallet_streams VALUES (?,?,?,?,?)",
                       (scope, chain, now_ms, encode(config), height))
        return self.cursor(scope)

    def cursor(self, scope):
        with self.connect() as db:
            row = db.execute("SELECT next_height FROM wallet_streams WHERE scope=?", (scope,)).fetchone()
        return row[0] if row else None

    def claim(self, scope, owner, now_ms, expires_ms):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT owner,expires_ms FROM wallet_leases WHERE scope=?", (scope,)).fetchone()
            if row and row[0] != owner and row[1] > now_ms:
                return False
            db.execute("INSERT OR REPLACE INTO wallet_leases VALUES (?,?,?)", (scope, owner, expires_ms))
        return True

    def release(self, scope, owner):
        with self.connect() as db:
            db.execute("DELETE FROM wallet_leases WHERE scope=? AND owner=?", (scope, owner))

    def latest_block(self, scope):
        with self.connect() as db:
            row = db.execute("SELECT height,hash FROM wallet_blocks WHERE scope=? ORDER BY height DESC LIMIT 1", (scope,)).fetchone()
        return {"height": row[0], "hash": row[1]} if row else None

    def commit_block(self, scope, expected_cursor, block, observed_ms, *, max_storage_bytes=None):
        height = block["height"]
        if height < expected_cursor or block["event_ms"] is not None and block["event_ms"] > observed_ms + 60_000:
            raise EvidenceUnavailable("Invalid block height or future timestamp")
        if max_storage_bytes is not None:
            size = 0
            for path in (self.path, Path(str(self.path) + "-wal")):
                try:
                    size += path.stat().st_size
                except FileNotFoundError:
                    pass
            # Reserve space for pages and the journal; stop rather than evict evidence.
            required = 2 * (len(encode(block["raw"])) + sum(len(encode(e)) for e in block["events"])) + 65536
            if size + required > max_storage_bytes:
                raise EvidenceUnavailable("Wallet storage budget reached; evidence retained, collection paused")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT hash,raw FROM wallet_blocks WHERE scope=? AND height=?", (scope, height)).fetchone()
            if existing:
                if existing[0] != block["hash"]:
                    raise EvidenceUnavailable("Finalized history conflict; manual reconciliation required")
                if decode(existing[1]) != block["raw"]:
                    raise EvidenceUnavailable("Finalized payload changed; checkpoint retained")
                return False
            cursor = db.execute("SELECT next_height FROM wallet_streams WHERE scope=?", (scope,)).fetchone()
            if not cursor or cursor[0] != expected_cursor:
                raise EvidenceUnavailable("Collector checkpoint changed; retry from stored checkpoint")
            last = db.execute("SELECT height,hash FROM wallet_blocks WHERE scope=? ORDER BY height DESC LIMIT 1", (scope,)).fetchone()
            if last and (block["parent_height"] != last[0] or block["parent_hash"] != last[1]):
                raise EvidenceUnavailable("Chain continuity conflict; checkpoint retained")
            db.execute("INSERT INTO wallet_blocks VALUES (?,?,?,?,?,?,?)",
                       (scope, height, block["hash"], block["event_ms"], observed_ms,
                        encode(block["raw"]), encode(block["diagnostics"])))
            for event in block["events"]:
                payload = {**event, "height": height, "block_hash": block["hash"], "finality": "finalized",
                           "event_ms": block["event_ms"], "first_observed_ms": observed_ms, "available_ms": observed_ms}
                old = db.execute("SELECT height,payload FROM wallet_events WHERE scope=? AND event_id=?", (scope, event["id"])).fetchone()
                if old:
                    if old[0] != height or decode(old[1]) != payload:
                        raise EvidenceUnavailable("Conflicting event identity; checkpoint retained")
                    continue
                db.execute("INSERT INTO wallet_events VALUES (?,?,?,?,?,?)",
                           (scope, event["id"], height, block["event_ms"], observed_ms, encode(payload)))
            db.execute("UPDATE wallet_streams SET next_height=? WHERE scope=?", (height + 1, scope))
        return True

    def events(self, scope, *, available_ms=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Event limit must be 1-1000")
        cutoff = int(time.time() * 1000) if available_ms is None else available_ms
        with self.connect() as db:
            rows = db.execute("""SELECT payload FROM wallet_events WHERE scope=? AND observed_ms<=?
                                 ORDER BY height DESC,event_id LIMIT ?""", (scope, cutoff, limit)).fetchall()
        return [decode(row[0]) for row in rows]

    def skip_empty_slots(self, scope, start, end, observed_ms):
        if end < start or not scope.startswith("solana:"):
            raise ValueError("Only confirmed empty Solana slot ranges may be skipped")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT next_height FROM wallet_streams WHERE scope=?", (scope,)).fetchone()
            if not row or row[0] != start:
                raise EvidenceUnavailable("Collector checkpoint changed; retry from stored checkpoint")
            db.execute("INSERT INTO wallet_empty_ranges VALUES (?,?,?,?)", (scope, start, end, observed_ms))
            db.execute("UPDATE wallet_streams SET next_height=? WHERE scope=?", (end + 1, scope))

    def set_status(self, scope, payload):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO wallet_status VALUES (?,?)", (scope, encode(payload)))

    def status(self, scope, stale_after_ms=60_000):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM wallet_status WHERE scope=?", (scope,)).fetchone()
            count = db.execute("SELECT count(*) FROM wallet_events WHERE scope=?", (scope,)).fetchone()[0]
        result = decode(row[0]) if row else {"status": "not_started", "checked_ms": None}
        result["stale"] = result["checked_ms"] is None or time.time() * 1000 - result["checked_ms"] > stale_after_ms
        result["event_count"] = count
        return result
