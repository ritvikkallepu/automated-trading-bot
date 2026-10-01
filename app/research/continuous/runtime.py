"""Isolated research dashboard and supervised collector. Never loads trading .env."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

from .config import load_config
from .health import check_health
from .store import HistoryStore


@contextmanager
def single_supervisor(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def research_server(root, port, config):
    from app.config import Settings
    from app.dashboard.server import DashboardHTTPServer, DashboardRequestHandler
    from app.dashboard.state import DashboardDefaults

    class ResearchHandler(DashboardRequestHandler):
        def do_POST(self):
            if not urlparse(self.path).path.startswith("/api/research"):
                self._send_json({"error": "Research-only process: trading controls disabled"}, status=403)
                return
            super().do_POST()

        def do_GET(self):
            path = urlparse(self.path).path
            if path.startswith("/api/") and not (path.startswith("/api/research") or path in {"/api/health", "/api/status"}):
                self._send_json({"error": "Research-only process"}, status=403)
                return
            super().do_GET()

        def log_message(self, format, *args):
            if args and str(args[1] if len(args) > 1 else "") not in {"200", "304"}:
                logging.getLogger(__name__).info(format, *args)

    server = DashboardHTTPServer(
        ("127.0.0.1", port), settings=Settings(), defaults=DashboardDefaults(),
        research_directory=root/"scans", outcome_directory=root/"outcomes",
        continuous_autostart=True, continuous_config=config)
    server.RequestHandlerClass = ResearchHandler
    return server


def serve(args):
    server = research_server(args.data_dir, args.port, args.config)
    stopped = threading.Event()
    def shutdown(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    def parent_watch():
        watchdog = ParentWatchdog()
        while not stopped.wait(15):
            missing = supervisor_missing(server.continuous_research.store, os.getpid(), int(time.time()*1000))
            if watchdog.expired(missing):
                shutdown()
                return
    if args.supervised:
        threading.Thread(target=parent_watch, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"Research-only dashboard http://127.0.0.1:{args.port}/#research", flush=True)
    try:
        server.serve_forever()
    finally:
        stopped.set()
        server.server_close()


def supervisor_missing(store, child_pid, now_ms):
    state = store.state("supervisor", {})
    return state.get("child_pid") != child_pid or now_ms-state.get("heartbeat_ms", 0) > 90_000


class ParentWatchdog:
    def __init__(self):
        self.missing_checks = 0

    def expired(self, missing):
        # Count actual checks, not elapsed wall time across laptop sleep/resume.
        self.missing_checks = self.missing_checks+1 if missing else 0
        return self.missing_checks >= 6


def worker_overdue(store, cfg, now_ms):
    if not store.state("research_enabled", True):
        return False
    status = store.state("worker_status", {})
    expected = status.get("cycle_started_ms") if status.get("phase") == "scanning" else status.get("next_cycle_ms")
    return bool(expected and now_ms-expected > (cfg["network"]["cycle_budget_seconds"]+120)*1000)


def supervise(args):
    root = args.data_dir
    root.mkdir(parents=True, exist_ok=True)
    logs = root/"logs"
    logs.mkdir(exist_ok=True)
    handler = RotatingFileHandler(logs/"research.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s", force=True)
    logger = logging.getLogger(__name__)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    store = HistoryStore(root/"research"/"research.sqlite3")
    child, reader = None, None
    def output(process):
        for line in process.stdout:
            logger.info("worker: %s", line.rstrip())
    with single_supervisor(root/"supervisor.lock"):
        try:
            while not stop.is_set():
                cfg = load_config(args.config)
                started_ms = int(time.time()*1000)
                command = [sys.executable, "-u", "-m", "app.research.continuous.runtime", "--serve", "--supervised",
                           "--data-dir", str(root), "--port", str(args.port)]
                if args.config:
                    command += ["--config", str(args.config)]
                child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, encoding="utf-8", errors="replace")
                reader = threading.Thread(target=output, args=(child,), daemon=True)
                reader.start()
                logger.info("Research worker started pid=%s", child.pid)
                store.set_state("supervisor", {"pid": os.getpid(), "child_pid": child.pid,
                                               "started_ms": started_ms, "heartbeat_ms": started_ms, "mode": "research_only"})
                while not stop.wait(15):
                    store.set_state("supervisor", {"pid": os.getpid(), "child_pid": child.pid,
                                                   "started_ms": started_ms, "heartbeat_ms": int(time.time()*1000), "mode": "research_only"})
                    alive = child.poll() is None
                    health = check_health(store, cfg, process_alive=alive, started_ms=started_ms)
                    if not alive:
                        logger.warning("Research worker exited code=%s; retrying", child.returncode)
                        break
                    # Give a new worker its own full startup window, ignoring old process timestamps.
                    if time.time()*1000-started_ms > (cfg["network"]["cycle_budget_seconds"]+120)*1000 and worker_overdue(store, cfg, int(time.time()*1000)):
                        logger.error("Research worker stalled (%s); restarting", health["status"])
                        break
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=45)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=10)
                reader.join(timeout=5)
                # A dead worker cannot release its lease. Remove only its research lease after it exits.
                lease = store.state("worker_lease", {})
                worker = store.state("worker_status", {})
                if lease.get("owner") and worker.get("process_id") == child.pid and worker.get("owner") == lease["owner"]:
                    store.release(lease["owner"])
                if stop.wait(30):
                    break
        finally:
            if child and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
            logger.info("Research supervisor stopped")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/research_runtime"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--port", type=int, default=63332)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serve", action="store_true")
    mode.add_argument("--compact", action="store_true")
    parser.add_argument("--supervised", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.data_dir = args.data_dir.resolve()
    if args.config:
        args.config = args.config.resolve()
    load_config(args.config)
    if args.compact:
        # This lock also prevents migration while our supervisor is running.
        with single_supervisor(args.data_dir/"supervisor.lock"):
            store = HistoryStore(args.data_dir/"research"/"research.sqlite3")
            name = datetime.now(timezone.utc).strftime("research-before-compact-%Y%m%dT%H%M%S%fZ.sqlite3")
            print(json.dumps(store.compact(args.data_dir/"backups"/name), indent=2))
    elif args.serve:
        serve(args)
    else:
        supervise(args)


if __name__ == "__main__":
    main()
