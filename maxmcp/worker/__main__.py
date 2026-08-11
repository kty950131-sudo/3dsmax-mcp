"""Command-line entry point for the ARTOKE desktop worker."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import threading
from typing import Callable, Sequence

from maxmcp.rtmw3d.runtime import Rtmw3dReadiness, default_readiness
from maxmcp.worker.api_client import ArtokeApiClient
from maxmcp.worker.credentials import CredentialError, load_worker_token
from maxmcp.worker.runner import ArtokeWorker
from maxmcp.worker.workspace import cleanup_stale


DEFAULT_CACHE = Path.home() / "AppData" / "Local" / "ARTOKE" / "worker"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="artoke-worker")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--url", default="https://artoke.com")
    run.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE)
    commands.add_parser("doctor")
    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    readiness: Callable[[], Rtmw3dReadiness] = default_readiness,
    token_loader: Callable[[], str] = load_worker_token,
    executable_finder: Callable[[str], str | None] = shutil.which,
) -> int:
    args = _parser().parse_args(argv)
    if args.command == "doctor":
        report = readiness()
        ffmpeg = executable_finder("ffmpeg")
        print(f"RTMW3D: {'ready' if report.ready else 'not ready'}")
        print(f"ffmpeg: {ffmpeg if ffmpeg else 'not found'}")
        if not report.ready:
            for missing in report.missing_files:
                print(f"missing: {missing}")
        return 0 if report.ready and ffmpeg else 1

    if args.command == "cleanup":
        removed = cleanup_stale(args.cache_root)
        print(f"Removed {len(removed)} stale job(s).")
        return 0

    try:
        token = token_loader()
    except CredentialError as exc:
        print(str(exc))
        return 1
    worker = ArtokeWorker(
        ArtokeApiClient(args.url, token),
        readiness,
        args.cache_root,
    )
    stop = threading.Event()
    try:
        worker.run_forever(stop)
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
