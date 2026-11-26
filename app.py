"""Conventional application entry point.

`service` is a ready-to-use in-memory service.  Run `python app.py` to expose it
over HTTP; set QUOTA_SAMPLES=samples to preload NDJSON sources.
"""
from __future__ import annotations

import os

from quota_service import QuotaService
from quota_service.http_app import create_server, main

service = QuotaService()
if os.getenv("QUOTA_SAMPLES"):
    service.load_ndjson_dir(os.environ["QUOTA_SAMPLES"])


def create_app(host: str = "127.0.0.1", port: int = 8080, samples: str | None = None):
    if samples:
        service.load_ndjson_dir(samples)
    return create_server(service, host, port)


if __name__ == "__main__":
    main()
