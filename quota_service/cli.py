"""Command line adapter.

Examples:
    python -m quota_service load samples
    python -m quota_service call view.admin '{}'
    python -m quota_service serve --samples samples
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import QuotaServiceError
from .http_app import main as http_main
from .service import QuotaService


def _print(value: Any) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, sort_keys=False)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quota_service")
    sub = parser.add_subparsers(dest="command", required=True)

    load = sub.add_parser("load", help="load an NDJSON source directory")
    load.add_argument("directory")

    call = sub.add_parser("call", help="invoke a service action with JSON payload")
    call.add_argument("action")
    call.add_argument("payload", nargs="?", default="{}")
    call.add_argument("--samples", default=None)

    scenario = sub.add_parser("scenario", help="load samples and print the built-in end-to-end scenario")
    scenario.add_argument("--samples", default="samples")

    serve = sub.add_parser("serve", help="run HTTP service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--samples", default=None)

    args = parser.parse_args(argv)
    service = QuotaService()
    try:
        if args.command == "serve":
            http_main(["--host", args.host, "--port", str(args.port), *(["--samples", args.samples] if args.samples else [])])
            return 0
        if args.command == "load":
            _print(service.load_ndjson_dir(Path(args.directory)))
            return 0
        if args.samples:
            service.load_ndjson_dir(args.samples)
        if args.command == "call":
            payload = json.loads(args.payload)
            _print(service.call(args.action, payload))
            return 0
        if args.command == "scenario":
            from .scenario import run_sample_scenario

            _print(run_sample_scenario(service, Path(args.samples)))
            return 0
    except QuotaServiceError as exc:
        _print(exc.to_dict())
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
