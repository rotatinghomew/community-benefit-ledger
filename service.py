"""Convenience import path: ``from service import QuotaService``."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from quota_service import *  # noqa: F401,E402
from quota_service.service import (  # noqa: E402
    CommunityQuotaService,
    Engine,
    LedgerService,
    QuotaEngine,
    QuotaService,
    Service,
)

__all__ = [
    "QuotaService",
    "Service",
    "Engine",
    "QuotaEngine",
    "LedgerService",
    "CommunityQuotaService",
]


if __name__ == "__main__":
    from quota_service.cli import main

    raise SystemExit(main())
