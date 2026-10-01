"""A bounded background worker independent of the trading loop."""
from __future__ import annotations

from copy import deepcopy
import json
import logging
from pathlib import Path
import threading
import time
from uuid import uuid4

from app.research.market_scanner import MarketScanner, PublicMarketData, ScanConfig


class ResearchScanService:
    def __init__(self, directory: Path = Path("data/research_scans"), *, scanner_factory=None):
        self.directory = directory
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread = None
        self._last_start = None
        self._factory = scanner_factory or (lambda: MarketScanner(PublicMarketData(cancelled=self._cancel.is_set)))
        self._state = {"status": "idle", "progress": "No scan started", "snapshot": None, "error": None}

    def status(self):
        with self._lock:
            result = deepcopy(self._state)
        snapshot = result.get("snapshot")
        result["stale"] = bool(snapshot and time.time() * 1000 - snapshot["as_of_ms"] > 300_000)
        result["retry_after_seconds"] = max(0, round(60 - (time.monotonic() - self._last_start))) if self._last_start is not None else 0
        return result

    def start(self, config: ScanConfig):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise ValueError("A research scan is already running")
            if self._last_start is not None and time.monotonic() - self._last_start < 60:
                raise ValueError("Wait 60 seconds between research scans")
            self._cancel.clear()
            self._last_start = time.monotonic()
            self._state.update(status="running", progress="Connecting to public feeds", error=None)
            self._thread = threading.Thread(target=self._run, args=(config,), daemon=True, name="research-scan")
            self._thread.start()

    def _progress(self, value):
        with self._lock:
            self._state["progress"] = value

    def _run(self, config):
        try:
            snapshot = self._factory().scan(config, self._progress)
            if self._cancel.is_set():
                raise ValueError("Research scan cancelled")
            snapshot["scan_id"] = uuid4().hex
            self.directory.mkdir(parents=True, exist_ok=True)
            target = self.directory / (snapshot["scan_id"] + ".json")
            temp = target.with_suffix(".tmp")
            try:
                temp.write_text(json.dumps(snapshot, allow_nan=False), encoding="utf-8")
                temp.replace(target)
            finally:
                temp.unlink(missing_ok=True)
            with self._lock:
                self._state.update(status="complete", progress="Scan complete", snapshot=snapshot, error=None)
        except Exception as exc:
            logging.getLogger(__name__).warning("Research scan failed: %s", exc)
            with self._lock:
                self._state.update(status="cancelled" if self._cancel.is_set() else "error", error=str(exc), progress="Scan did not complete")

    def cancel(self):
        self._cancel.set()

    def close(self):
        self.cancel()
        if self._thread is not None:
            self._thread.join(timeout=10)
