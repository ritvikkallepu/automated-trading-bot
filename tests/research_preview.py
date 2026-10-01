"""Safe dashboard harness: no environment files, credentials, or restored loops."""
import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from app.config import Settings
from app.dashboard.server import DashboardHTTPServer
from app.dashboard.state import DashboardDefaults
from app.research.market_scanner import MarketScanner, ScanConfig
from app.research.scan_service import ResearchScanService
from app.research.outcome_tracker import ResearchOutcomeService
from tests.test_market_research import FakeData


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--data-dir")
    args = parser.parse_args()
    temporary = TemporaryDirectory() if not args.data_dir else None
    try:
        root = Path(args.data_dir or temporary.name)
        server = DashboardHTTPServer(
            ("127.0.0.1", args.port), settings=Settings(), defaults=DashboardDefaults(),
            research_directory=root / "scans", outcome_directory=root / "outcomes",
            continuous_autostart=not args.fixture,
        )
        if args.fixture:
            from app.research.continuous.service import ResearchEngine
            from app.research.continuous.config import load_config
            from tests.test_continuous_research import ContinuousFixture
            continuous = server.continuous_research
            continuous.data_factory = lambda cfg, stop: ContinuousFixture(cfg, stop, now_ms=int(time.time()*1000))
            cfg = load_config()
            cfg["levels"]["trigger"] = "breakout"
            result = ResearchEngine(continuous.store, ContinuousFixture(now_ms=int(time.time()*1000)), cfg).cycle()
            result["warnings"].append("SYNTHETIC TEST FIXTURE")
            continuous._snapshot = result
            continuous._research_stats = continuous._statistics()
            class FixtureScanner:
                def scan(self, config, progress):
                    progress("Synthetic fixture scan")
                    time.sleep(0.3)
                    result = MarketScanner(FakeData()).scan(config)
                    result.update(as_of_ms=int(time.time()*1000), completed_ms=int(time.time()*1000))
                    result["warnings"] = ["SYNTHETIC TEST FIXTURE: not market advice"]
                    return result
            server.research.close()
            server.research_outcomes.close()
            server.research = ResearchScanService(root / "scans", scanner_factory=FixtureScanner)
            server.research_outcomes = ResearchOutcomeService(
                root / "scans", root / "outcomes", autostart=False,
            )
        print(f"PREVIEW_URL=http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    main()
