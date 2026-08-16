"""Command-line entry point for the ARTOKE local motion ingest companion."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import threading
import time
from typing import Callable, Sequence
import webbrowser

from maxmcp.local_ingest.api_client import LocalIngestApiClient, LocalIngestApiError
from maxmcp.local_ingest.runner import (
    LocalIngestRunner,
    LocalRunRejected,
    LocalRunResult,
)
from maxmcp.local_ingest.server import CompanionHTTPServer
from maxmcp.rtmw3d.runtime import default_readiness
from maxmcp.worker.motion_pipeline import MotionPipeline
from maxmcp.worker.workspace import WorkspaceProcessLock, cleanup_stale


DEFAULT_CACHE = Path.home() / "AppData" / "Local" / "ARTOKE" / "local-ingest"

EXIT_SUCCESS = 0
EXIT_INVALID_URI = 2
EXIT_ALREADY_RUNNING = 3
EXIT_EXCHANGE_FAILED = 4
EXIT_BROWSER_LAUNCH_FAILED = 5
EXIT_INTERNAL_FAILURE = 6

_MAX_URI_LENGTH = 256
_URI_PREFIXES = ("artoke-motion://ingest?", "artoke-motion://ingest/?")
_TOKEN_QUERY = re.compile(r"token=([0-9a-f]{64})")
_POLL_INTERVAL_SECONDS = 0.2


class IngestUriError(ValueError):
    """A constant-message rejection that never reflects the launch URI."""

    def __init__(self) -> None:
        super().__init__("Local motion launch URI is invalid")


def parse_ingest_uri(uri: object) -> str:
    """Return the handoff token from a strictly canonical launch URI."""
    if not isinstance(uri, str) or not 1 <= len(uri) <= _MAX_URI_LENGTH:
        raise IngestUriError()
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in uri):
        raise IngestUriError()
    remainder = None
    for prefix in _URI_PREFIXES:
        if uri.startswith(prefix):
            remainder = uri[len(prefix) :]
            break
    if remainder is None:
        raise IngestUriError()
    match = _TOKEN_QUERY.fullmatch(remainder)
    if match is None:
        raise IngestUriError()
    return match.group(1)


def _report(code: str) -> None:
    print(f"artoke-motion: {code}")


def _default_lock(cache_root: Path) -> WorkspaceProcessLock:
    return WorkspaceProcessLock(cache_root / "companion")


def _default_api(base_url: str) -> LocalIngestApiClient:
    return LocalIngestApiClient(base_url)


def _default_server(cache_root: Path, session_id: str) -> CompanionHTTPServer:
    return CompanionHTTPServer(cache_root, session_id)


def _default_runner(session, api) -> LocalIngestRunner:
    report = default_readiness()
    if not report.ready:
        raise LocalRunRejected("rtmw3d_unavailable")
    return LocalIngestRunner(session, api, MotionPipeline(report))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="artoke-motion")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest")
    ingest.add_argument("uri")
    ingest.add_argument("--url", default="https://artoke.com")
    ingest.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    api_factory: Callable[[str], object] = _default_api,
    server_factory: Callable[[Path, str], object] = _default_server,
    runner_factory: Callable[[object, object], object] = _default_runner,
    lock_factory: Callable[[Path], object] = _default_lock,
    browser_opener: Callable[[str], bool] = webbrowser.open,
    sleeper: Callable[[float], None] = time.sleep,
    stale_cleaner: Callable[[Path], object] = cleanup_stale,
) -> int:
    args = _parser().parse_args(argv)
    try:
        token = parse_ingest_uri(args.uri)
    except IngestUriError:
        _report("invalid_launch_uri")
        return EXIT_INVALID_URI

    try:
        lock = lock_factory(args.cache_root)
        acquired = lock.acquire()
    except (OSError, RuntimeError, ValueError):
        _report("internal_failure")
        return EXIT_INTERNAL_FAILURE
    if not acquired:
        _report("companion_already_running")
        return EXIT_ALREADY_RUNNING

    server = None
    try:
        try:
            stale_cleaner(args.cache_root)
        except (OSError, RuntimeError):
            pass

        try:
            api = api_factory(args.url)
            session = api.exchange(token)
        except (LocalIngestApiError, ValueError):
            _report("handoff_exchange_failed")
            return EXIT_EXCHANGE_FAILED
        finally:
            del token

        try:
            server = server_factory(args.cache_root, session.session_id)
        except (OSError, RuntimeError, ValueError):
            _report("internal_failure")
            return EXIT_INTERNAL_FAILURE
        threading.Thread(target=server.serve_forever, daemon=True).start()

        try:
            opened = browser_opener(f"{server.origin}/")
        except webbrowser.Error:
            opened = False
        if not opened:
            _report("browser_launch_failed")
            return EXIT_BROWSER_LAUNCH_FAILED

        return _serve_until_terminal(server, api, runner_factory, sleeper)
    finally:
        if server is not None:
            try:
                server.close()
            except (OSError, RuntimeError):
                pass
        try:
            lock.release()
        except (OSError, RuntimeError):
            pass


def _serve_until_terminal(server, api, runner_factory, sleeper) -> int:
    try:
        while True:
            if server.serve_failed:
                _report("local_server_failed")
                return EXIT_INTERNAL_FAILURE
            snapshot = server.session.snapshot()
            if snapshot.state == "accepted":
                return _process_accepted(server, api, runner_factory)
            if snapshot.state in {"cancelled", "closed"}:
                return EXIT_SUCCESS
            if snapshot.state == "cleanup_required":
                _report("cleanup_required")
                return EXIT_INTERNAL_FAILURE
            sleeper(_POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        return EXIT_SUCCESS


def _process_accepted(server, api, runner_factory) -> int:
    name = server.session.display_name or "Local motion"
    try:
        runner = runner_factory(server.session, api)
        result: LocalRunResult = runner.run(name)
    except LocalRunRejected as exc:
        _report(exc.code)
        return EXIT_INTERNAL_FAILURE
    except (RuntimeError, ValueError, OSError):
        _report("local_processing_failed")
        return EXIT_INTERNAL_FAILURE
    if result.state in {"completed", "cancelled"}:
        return EXIT_SUCCESS
    _report(result.state)
    return EXIT_INTERNAL_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
