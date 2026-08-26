from pathlib import Path

import pytest

from maxmcp.local_ingest.__main__ import (
    EXIT_ALREADY_RUNNING,
    EXIT_EXCHANGE_FAILED,
    EXIT_FILE_DIALOG_FAILED,
    EXIT_INTERNAL_FAILURE,
    EXIT_INVALID_URI,
    EXIT_SUCCESS,
    IngestUriError,
    main,
    parse_ingest_uri,
)
from maxmcp.local_ingest.api_client import LocalIngestApiError, LocalSession
from maxmcp.local_ingest.dialog import FileDialogError
from maxmcp.local_ingest.runner import LocalRunRejected, LocalRunResult
from maxmcp.local_ingest.session import SessionSnapshot, UploadRejected


TOKEN = "a" * 64
VALID_URI = f"artoke-motion://ingest?token={TOKEN}"
SESSION_ID = "0f0e0d0c-0b0a-4908-8706-050403020100"


class FakeLock:
    def __init__(self, acquired: bool = True) -> None:
        self.acquired = acquired
        self.acquire_calls = 0
        self.released = False

    def acquire(self) -> bool:
        self.acquire_calls += 1
        return self.acquired

    def release(self) -> None:
        self.released = True


class FakeApi:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.exchanged: list[str] = []

    def exchange(self, handoff_token: str) -> LocalSession:
        self.exchanged.append(handoff_token)
        if self.fail:
            raise LocalIngestApiError("ARTOKE exchange response is invalid")
        return LocalSession(session_id=SESSION_ID, expires_at="2026-08-16T00:00:00Z")


class FakeSession:
    def __init__(self, receive_error: BaseException | None = None) -> None:
        self.receive_error = receive_error
        self.received: list[tuple[int, str]] = []
        self.display_name: str | None = None
        self.closed = False

    def receive_source(
        self, stream: object, content_length: int, display_name: str
    ) -> SessionSnapshot:
        if self.receive_error is not None:
            raise self.receive_error
        assert stream.read(1)  # type: ignore[attr-defined]
        self.received.append((content_length, display_name))
        self.display_name = display_name
        return SessionSnapshot(state="accepted", size_bytes=content_length, cleaned=False)

    def close(self) -> SessionSnapshot:
        self.closed = True
        return SessionSnapshot(state="closed", size_bytes=None, cleaned=True)


class FakeRunner:
    def __init__(self, result: LocalRunResult | None = None, error: BaseException | None = None) -> None:
        self.result = result
        self.error = error
        self.names: list[str] = []

    def run(self, name: str) -> LocalRunResult:
        self.names.append(name)
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class Harness:
    def __init__(
        self,
        *,
        lock: FakeLock | None = None,
        api: FakeApi | None = None,
        session: FakeSession | None = None,
        runner: FakeRunner | None = None,
        picked: object = "unset",
        session_error: BaseException | None = None,
    ) -> None:
        self.lock = lock if lock is not None else FakeLock()
        self.api = api if api is not None else FakeApi()
        self.session = session if session is not None else FakeSession()
        self.runner = runner if runner is not None else FakeRunner(
            LocalRunResult("completed", "5a3c2f10-9d8e-4b7a-a6f5-e4d3c2b1a091")
        )
        self.picked = picked
        self.session_error = session_error
        self.lock_roots: list[Path] = []
        self.session_arguments: list[tuple[Path, str]] = []
        self.runner_arguments: list[tuple[object, object]] = []
        self.stale_roots: list[Path] = []
        self.pick_calls = 0

    def lock_factory(self, cache_root: Path) -> FakeLock:
        self.lock_roots.append(cache_root)
        return self.lock

    def api_factory(self, base_url: str) -> FakeApi:
        return self.api

    def session_factory(self, cache_root: Path, session_id: str) -> FakeSession:
        if self.session_error is not None:
            raise self.session_error
        self.session_arguments.append((cache_root, session_id))
        return self.session

    def runner_factory(self, session: object, api: object) -> FakeRunner:
        self.runner_arguments.append((session, api))
        return self.runner

    def file_picker(self) -> Path | None:
        self.pick_calls += 1
        if isinstance(self.picked, BaseException):
            raise self.picked
        assert self.picked != "unset"
        return self.picked  # type: ignore[return-value]

    def stale_cleaner(self, cache_root: Path) -> list[Path]:
        self.stale_roots.append(cache_root)
        return []

    def run(self, argv: list[str] | None = None) -> int:
        return main(
            argv if argv is not None else ["ingest", VALID_URI],
            api_factory=self.api_factory,
            session_factory=self.session_factory,
            runner_factory=self.runner_factory,
            lock_factory=self.lock_factory,
            file_picker=self.file_picker,
            stale_cleaner=self.stale_cleaner,
        )


@pytest.fixture
def video(tmp_path: Path) -> Path:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"x" * 64)
    return source


def test_parse_ingest_uri_accepts_the_server_produced_launch_url() -> None:
    assert parse_ingest_uri(VALID_URI) == TOKEN


def test_parse_ingest_uri_accepts_a_normalized_root_path() -> None:
    assert parse_ingest_uri(f"artoke-motion://ingest/?token={TOKEN}") == TOKEN


@pytest.mark.parametrize(
    "uri",
    [
        None,
        123,
        "",
        f"https://ingest?token={TOKEN}",
        f"ARTOKE-MOTION://ingest?token={TOKEN}",
        f"artoke-motion://user@ingest?token={TOKEN}",
        f"artoke-motion://ingest:80?token={TOKEN}",
        f"artoke-motion://evil?token={TOKEN}",
        f"artoke-motion://ingest/extra?token={TOKEN}",
        f"artoke-motion://ingest?token={TOKEN}#fragment",
        f"artoke-motion://ingest?token={TOKEN}&token={TOKEN}",
        f"artoke-motion://ingest?token={TOKEN}&extra=1",
        "artoke-motion://ingest?token=",
        "artoke-motion://ingest?token=%zz" + "a" * 60,
        "artoke-motion://ingest?token=%61" + "a" * 63,
        f"artoke-motion://ingest?token={TOKEN}\n",
        f"artoke-motion://ingest?token={'A' * 64}",
        f"artoke-motion://ingest?token={'g' * 64}",
        f"artoke-motion://ingest?token={'a' * 63}",
        f"artoke-motion://ingest?token={'a' * 65}",
        f"artoke-motion://ingest?token={TOKEN}" + "a" * 4096,
    ],
)
def test_parse_ingest_uri_rejects_noncanonical_uris(uri: object) -> None:
    with pytest.raises(IngestUriError):
        parse_ingest_uri(uri)


def test_parse_ingest_uri_error_never_reflects_the_uri() -> None:
    hostile = f"artoke-motion://evil?token={TOKEN}"
    with pytest.raises(IngestUriError) as exc:
        parse_ingest_uri(hostile)
    assert TOKEN not in str(exc.value)
    assert "evil" not in str(exc.value)


def test_successful_ingest_feeds_selection_processes_and_cleans_up(video: Path) -> None:
    harness = Harness(picked=video)
    assert harness.run() == EXIT_SUCCESS
    assert harness.api.exchanged == [TOKEN]
    assert harness.pick_calls == 1
    assert harness.session.received == [(64, "clip.mp4")]
    assert harness.runner.names == ["clip.mp4"]
    assert harness.runner_arguments == [(harness.session, harness.api)]
    assert harness.session.closed
    assert harness.lock.released


def test_session_workspace_uses_the_exchanged_session_id(
    tmp_path: Path, video: Path
) -> None:
    harness = Harness(picked=video)
    cache_root = tmp_path / "cache"
    assert (
        harness.run(["ingest", VALID_URI, "--cache-root", str(cache_root)])
        == EXIT_SUCCESS
    )
    assert harness.session_arguments == [(cache_root, SESSION_ID)]
    assert harness.lock_roots == [cache_root]


def test_invalid_uri_exits_before_locking_or_exchanging(capsys: pytest.CaptureFixture[str]) -> None:
    harness = Harness()
    exit_code = harness.run(["ingest", f"artoke-motion://evil?token={TOKEN}"])
    assert exit_code == EXIT_INVALID_URI
    assert harness.lock.acquire_calls == 0
    assert harness.api.exchanged == []
    assert harness.pick_calls == 0
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert "evil" not in output.out + output.err


def test_second_instance_exits_without_exchanging() -> None:
    harness = Harness(lock=FakeLock(acquired=False))
    assert harness.run() == EXIT_ALREADY_RUNNING
    assert harness.api.exchanged == []
    assert harness.pick_calls == 0
    assert harness.session_arguments == []
    assert harness.stale_roots == []


def test_startup_removes_stale_work_only_after_locking(
    tmp_path: Path, video: Path
) -> None:
    harness = Harness(picked=video)
    cache_root = tmp_path / "cache"
    assert harness.run(["ingest", VALID_URI, "--cache-root", str(cache_root)]) == EXIT_SUCCESS
    assert harness.stale_roots == [cache_root]


def test_exchange_failure_skips_session_and_dialog(capsys: pytest.CaptureFixture[str]) -> None:
    harness = Harness(api=FakeApi(fail=True))
    assert harness.run() == EXIT_EXCHANGE_FAILED
    assert harness.session_arguments == []
    assert harness.pick_calls == 0
    assert harness.lock.released
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err


def test_session_creation_failure_is_internal_and_skips_dialog() -> None:
    harness = Harness(session_error=OSError("workspace unavailable"))
    assert harness.run() == EXIT_INTERNAL_FAILURE
    assert harness.pick_calls == 0
    assert harness.lock.released


def test_dialog_failure_cleans_up() -> None:
    harness = Harness(picked=FileDialogError("file_dialog_failed"))
    assert harness.run() == EXIT_FILE_DIALOG_FAILED
    assert harness.session.closed
    assert harness.lock.released
    assert harness.runner.names == []


def test_picker_cancel_exits_cleanly_without_processing() -> None:
    harness = Harness(picked=None)
    assert harness.run() == EXIT_SUCCESS
    assert harness.runner.names == []
    assert harness.session.received == []
    assert harness.session.closed
    assert harness.lock.released


def test_unsupported_extension_fails_before_reading(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = tmp_path / "notes.txt"
    document.write_bytes(b"x" * 64)
    harness = Harness(picked=document)
    assert harness.run() == EXIT_INTERNAL_FAILURE
    assert harness.session.received == []
    assert harness.runner.names == []
    assert harness.session.closed
    assert harness.lock.released
    assert "video_container_mismatch" in capsys.readouterr().out


def test_unreadable_selection_is_internal_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = Harness(picked=tmp_path / "missing.mp4")
    assert harness.run() == EXIT_INTERNAL_FAILURE
    assert harness.session.received == []
    assert harness.runner.names == []
    assert harness.lock.released
    assert "source_read_failed" in capsys.readouterr().out


def test_rejected_source_is_internal_failure_with_stable_code(
    video: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = Harness(
        picked=video,
        session=FakeSession(receive_error=UploadRejected("source_too_large")),
    )
    assert harness.run() == EXIT_INTERNAL_FAILURE
    assert harness.runner.names == []
    assert harness.session.closed
    assert harness.lock.released
    assert "source_too_large" in capsys.readouterr().out


def test_runner_rejection_is_internal_and_message_stays_safe(
    video: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = Harness(
        picked=video,
        runner=FakeRunner(error=LocalRunRejected("local_processing_failed")),
    )
    assert harness.run() == EXIT_INTERNAL_FAILURE
    assert harness.session.closed
    assert harness.lock.released
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert TOKEN not in output
    assert str(video) not in output


def test_cancelled_run_result_exits_cleanly(video: Path) -> None:
    harness = Harness(
        picked=video,
        runner=FakeRunner(LocalRunResult("cancelled", None)),
    )
    assert harness.run() == EXIT_SUCCESS


@pytest.mark.parametrize("state", ["publication_pending", "cleanup_required", "publication_conflict"])
def test_non_final_run_results_are_internal_failures(video: Path, state: str) -> None:
    harness = Harness(
        picked=video,
        runner=FakeRunner(
            LocalRunResult(state, "5a3c2f10-9d8e-4b7a-a6f5-e4d3c2b1a091")
        ),
    )
    assert harness.run() == EXIT_INTERNAL_FAILURE
    assert harness.lock.released


def test_failure_paths_use_distinct_exit_codes() -> None:
    codes = {
        EXIT_INVALID_URI,
        EXIT_ALREADY_RUNNING,
        EXIT_EXCHANGE_FAILED,
        EXIT_FILE_DIALOG_FAILED,
        EXIT_INTERNAL_FAILURE,
    }
    assert len(codes) == 5
    assert EXIT_SUCCESS not in codes
