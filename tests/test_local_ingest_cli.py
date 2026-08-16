from pathlib import Path

import pytest

from maxmcp.local_ingest.__main__ import (
    EXIT_ALREADY_RUNNING,
    EXIT_BROWSER_LAUNCH_FAILED,
    EXIT_EXCHANGE_FAILED,
    EXIT_INTERNAL_FAILURE,
    EXIT_INVALID_URI,
    EXIT_SUCCESS,
    IngestUriError,
    main,
    parse_ingest_uri,
)
from maxmcp.local_ingest.api_client import LocalIngestApiError, LocalSession
from maxmcp.local_ingest.runner import LocalRunRejected, LocalRunResult
from maxmcp.local_ingest.session import SessionSnapshot


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
    def __init__(self, states: list[str], display_name: str = "clip.mp4") -> None:
        self._states = list(states)
        self.display_name = display_name
        self.closed = False

    def snapshot(self) -> SessionSnapshot:
        state = self._states.pop(0) if len(self._states) > 1 else self._states[0]
        return SessionSnapshot(
            state=state,
            size_bytes=None,
            cleaned=state in {"cancelled", "closed"},
        )

    def close(self) -> SessionSnapshot:
        self.closed = True
        return SessionSnapshot(state="closed", size_bytes=None, cleaned=True)


class FakeServer:
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.origin = "http://127.0.0.1:53211"
        self.serve_failed = False
        self.serve_calls = 0
        self.closed = False

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        self.serve_calls += 1

    def close(self) -> SessionSnapshot:
        self.closed = True
        return self.session.close()


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
        session_states: list[str] | None = None,
        lock: FakeLock | None = None,
        api: FakeApi | None = None,
        runner: FakeRunner | None = None,
        browser_result: object = True,
        server_error: BaseException | None = None,
    ) -> None:
        self.lock = lock if lock is not None else FakeLock()
        self.api = api if api is not None else FakeApi()
        self.session = FakeSession(session_states or ["ready", "accepted"])
        self.server: FakeServer | None = None
        self.runner = runner if runner is not None else FakeRunner(
            LocalRunResult("completed", "5a3c2f10-9d8e-4b7a-a6f5-e4d3c2b1a091")
        )
        self.browser_urls: list[str] = []
        self.browser_result = browser_result
        self.server_error = server_error
        self.lock_roots: list[Path] = []
        self.server_arguments: list[tuple[Path, str]] = []
        self.runner_arguments: list[tuple[object, object]] = []
        self.stale_roots: list[Path] = []

    def lock_factory(self, cache_root: Path) -> FakeLock:
        self.lock_roots.append(cache_root)
        return self.lock

    def api_factory(self, base_url: str) -> FakeApi:
        return self.api

    def server_factory(self, cache_root: Path, session_id: str) -> FakeServer:
        if self.server_error is not None:
            raise self.server_error
        self.server_arguments.append((cache_root, session_id))
        self.server = FakeServer(self.session)
        return self.server

    def runner_factory(self, session: object, api: object) -> FakeRunner:
        self.runner_arguments.append((session, api))
        return self.runner

    def browser_opener(self, url: str) -> bool:
        self.browser_urls.append(url)
        if isinstance(self.browser_result, BaseException):
            raise self.browser_result
        return bool(self.browser_result)

    def stale_cleaner(self, cache_root: Path) -> list[Path]:
        self.stale_roots.append(cache_root)
        return []

    def run(self, argv: list[str] | None = None) -> int:
        return main(
            argv if argv is not None else ["ingest", VALID_URI],
            api_factory=self.api_factory,
            server_factory=self.server_factory,
            runner_factory=self.runner_factory,
            lock_factory=self.lock_factory,
            browser_opener=self.browser_opener,
            sleeper=lambda _seconds: None,
            stale_cleaner=self.stale_cleaner,
        )


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


def test_successful_ingest_serves_processes_and_cleans_up() -> None:
    harness = Harness()
    assert harness.run() == EXIT_SUCCESS
    assert harness.api.exchanged == [TOKEN]
    assert harness.server is not None
    assert harness.server.serve_calls == 1
    assert harness.browser_urls == [f"{harness.server.origin}/"]
    assert harness.runner.names == ["clip.mp4"]
    assert harness.runner_arguments == [(harness.session, harness.api)]
    assert harness.server.closed
    assert harness.lock.released


def test_server_workspace_uses_the_exchanged_session_id(tmp_path: Path) -> None:
    harness = Harness()
    cache_root = tmp_path / "cache"
    assert (
        harness.run(["ingest", VALID_URI, "--cache-root", str(cache_root)])
        == EXIT_SUCCESS
    )
    assert harness.server_arguments == [(cache_root, SESSION_ID)]
    assert harness.lock_roots == [cache_root]


def test_invalid_uri_exits_before_locking_or_exchanging(capsys: pytest.CaptureFixture[str]) -> None:
    harness = Harness()
    exit_code = harness.run(["ingest", f"artoke-motion://evil?token={TOKEN}"])
    assert exit_code == EXIT_INVALID_URI
    assert harness.lock.acquire_calls == 0
    assert harness.api.exchanged == []
    assert harness.browser_urls == []
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert "evil" not in output.out + output.err


def test_second_instance_exits_without_exchanging() -> None:
    harness = Harness(lock=FakeLock(acquired=False))
    assert harness.run() == EXIT_ALREADY_RUNNING
    assert harness.api.exchanged == []
    assert harness.browser_urls == []
    assert harness.server is None
    assert harness.stale_roots == []


def test_startup_removes_stale_work_only_after_locking(tmp_path: Path) -> None:
    harness = Harness()
    cache_root = tmp_path / "cache"
    assert harness.run(["ingest", VALID_URI, "--cache-root", str(cache_root)]) == EXIT_SUCCESS
    assert harness.stale_roots == [cache_root]


def test_exchange_failure_skips_server_and_browser(capsys: pytest.CaptureFixture[str]) -> None:
    harness = Harness(api=FakeApi(fail=True))
    assert harness.run() == EXIT_EXCHANGE_FAILED
    assert harness.server is None
    assert harness.browser_urls == []
    assert harness.lock.released
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err


def test_server_startup_failure_is_internal_and_skips_browser() -> None:
    harness = Harness(server_error=OSError("bind failed"))
    assert harness.run() == EXIT_INTERNAL_FAILURE
    assert harness.browser_urls == []
    assert harness.lock.released


def test_browser_launch_returning_false_cleans_up() -> None:
    harness = Harness(browser_result=False)
    assert harness.run() == EXIT_BROWSER_LAUNCH_FAILED
    assert harness.server is not None
    assert harness.server.closed
    assert harness.lock.released
    assert harness.runner.names == []


def test_browser_launch_error_cleans_up() -> None:
    import webbrowser

    harness = Harness(browser_result=webbrowser.Error("no browser"))
    assert harness.run() == EXIT_BROWSER_LAUNCH_FAILED
    assert harness.server is not None
    assert harness.server.closed
    assert harness.lock.released


def test_cancelled_session_exits_cleanly_without_processing() -> None:
    harness = Harness(session_states=["ready", "ready", "cancelled"])
    assert harness.run() == EXIT_SUCCESS
    assert harness.runner.names == []
    assert harness.server is not None
    assert harness.server.closed
    assert harness.lock.released


def test_cleanup_required_session_state_is_internal_failure() -> None:
    harness = Harness(session_states=["ready", "cleanup_required"])
    assert harness.run() == EXIT_INTERNAL_FAILURE
    assert harness.runner.names == []
    assert harness.lock.released


def test_serve_failure_is_internal_failure() -> None:
    harness = Harness(session_states=["ready"])

    original_factory = harness.server_factory

    def failing_server_factory(cache_root: Path, session_id: str) -> FakeServer:
        server = original_factory(cache_root, session_id)
        server.serve_failed = True
        return server

    harness.server_factory = failing_server_factory  # type: ignore[method-assign]
    assert harness.run() == EXIT_INTERNAL_FAILURE
    assert harness.lock.released


def test_runner_rejection_is_internal_and_message_stays_safe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    harness = Harness(runner=FakeRunner(error=LocalRunRejected("local_processing_failed")))
    assert harness.run() == EXIT_INTERNAL_FAILURE
    assert harness.server is not None
    assert harness.server.closed
    assert harness.lock.released
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert TOKEN not in output
    assert "127.0.0.1" not in output


def test_cancelled_run_result_exits_cleanly() -> None:
    harness = Harness(
        runner=FakeRunner(LocalRunResult("cancelled", None))
    )
    assert harness.run() == EXIT_SUCCESS


@pytest.mark.parametrize("state", ["publication_pending", "cleanup_required", "publication_conflict"])
def test_non_final_run_results_are_internal_failures(state: str) -> None:
    harness = Harness(
        runner=FakeRunner(
            LocalRunResult(state, "5a3c2f10-9d8e-4b7a-a6f5-e4d3c2b1a091")
        )
    )
    assert harness.run() == EXIT_INTERNAL_FAILURE
    assert harness.lock.released


def test_failure_paths_use_distinct_exit_codes() -> None:
    codes = {
        EXIT_INVALID_URI,
        EXIT_ALREADY_RUNNING,
        EXIT_EXCHANGE_FAILED,
        EXIT_BROWSER_LAUNCH_FAILED,
        EXIT_INTERNAL_FAILURE,
    }
    assert len(codes) == 5
    assert EXIT_SUCCESS not in codes
