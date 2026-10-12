"""CLI for the daily jobs, e.g. from a Railway cron service:  python -m app.jobs send"""

from __future__ import annotations

import json
import sys

from .notify import AISensyClient, DailyJobs
from .portfolio import PortfolioAdvisor
from .service import RecommendationService
from .config import Settings
from .store import Store


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in ("prepare", "send", "tick"):
        print("usage: python -m app.jobs prepare|send|tick", file=sys.stderr)
        return 2
    settings = Settings()
    service = RecommendationService(settings, Store(settings.data_dir / "app.db"))
    jobs = DailyJobs(PortfolioAdvisor(service), AISensyClient())
    result = jobs.tick() if argv[0] == "tick" else jobs.run(argv[0])
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
