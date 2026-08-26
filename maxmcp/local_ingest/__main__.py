"""Command-line entry point for the ARTOKE local motion ingest companion."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
from typing import Callable, Sequence

from maxmcp.local_ingest.api_client import LocalIngestApiClient, LocalIngestApiError
from maxmcp.local_ingest.dialog import FileDialogError, pick_video_file
from maxmcp.local_ingest.runner import (
    LocalIngestRunner,
    LocalRunRejected,
    LocalRunResult,
)
from maxmcp.local_ingest.session import CompanionSession, UploadRejected
from maxmcp.rtmw3d.runtime import default_readiness
from maxmcp.worker.motion_pipeline import MotionPipeline
from maxmcp.worker.workspace import WorkspaceProcessLock, cleanup_stale


DEFAULT_CACHE = Path.home() / "AppData" / "Local" / "ARTOKE" / "local-ingest"

EXIT_SUCCESS = 0
EXIT_INVALID_URI = 2
EXIT_ALREADY_RUNNING = 3
EXIT_EXCHANGE_FAILED = 4
EXIT_FILE_DIALOG_FAILED = 5
EXIT_INTERNAL_FAILURE = 6

_MAX_URI_LENGTH = 256
_URI_PREFIXES = ("artoke-motion://ingest?", "artoke-motion://ingest/?")
_TOKEN_QUERY = re.compile(r"token=([0-9a-f]{64})")
_SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".avi"}


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


def _default_session(cache_root: Path, session_id: str) -> CompanionSession:
    return CompanionSession.create(cache_root, session_id)


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
    session_factory: Callable[[Path, str], object] = _default_session,
    runner_factory: Callable[[object, object], object] = _default_runner,
    lock_factory: Callable[[Path], object] = _default_lock,
    file_picker: Callable[[], Path | None] = pick_video_file,
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

    session = None
    try:
        try:
            stale_cleaner(args.cache_root)
        except (OSError, RuntimeError):
            pass

        try:
            api = api_factory(args.url)
            handoff = api.exchange(token)
        except (LocalIngestApiError, ValueError):
            _report("handoff_exchange_failed")
            return EXIT_EXCHANGE_FAILED
        finally:
            del token

        try:
            session = session_factory(args.cache_root, handoff.session_id)
        except (OSError, RuntimeError, ValueError):
            _report("internal_failure")
            return EXIT_INTERNAL_FAILURE

        try:
            selected = file_picker()
        except FileDialogError:
            _report("file_dialog_failed")
            return EXIT_FILE_DIALOG_FAILED
        if selected is None:
            return EXIT_SUCCESS

        rejection = _feed_selected_source(session, selected)
        if rejection is not None:
            _report(rejection)
            return EXIT_INTERNAL_FAILURE

        return _process_accepted(session, api, runner_factory)
    finally:
        if session is not None:
            try:
                session.close()
            except (OSError, RuntimeError):
                pass
        try:
            lock.release()
        except (OSError, RuntimeError):
            pass


def _feed_selected_source(session, selected: Path) -> str | None:
    if selected.suffix.lower() not in _SUPPORTED_EXTENSIONS:
        return "video_container_mismatch"
    try:
        with open(selected, "rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            session.receive_source(stream, size, selected.name)
    except UploadRejected as exc:
        return exc.code
    except OSError:
        return "source_read_failed"
    return None


def _process_accepted(session, api, runner_factory) -> int:
    name = session.display_name or "Local motion"
    try:
        runner = runner_factory(session, api)
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
