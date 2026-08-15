from io import BytesIO
from pathlib import Path
import os
import threading
from uuid import UUID

import pytest

from maxmcp.local_ingest.session import (
    CompanionSession,
    SessionRejected,
    UploadRejected,
)
from maxmcp.worker.workspace import JobWorkspace


JOB_ID = "00000000-0000-4000-8000-000000000008"


class _ShortStream:
    def read(self, size: int) -> bytes:
        return b"x" * min(size, 3) if size > 4 else b""


class _BlockingStream:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def read(self, size: int) -> bytes:
        self.started.set()
        self.release.wait(timeout=2)
        return b"x" * min(size, 4)


class _BrokenStream:
    def read(self, _size: int) -> bytes:
        raise OSError("disconnected")


def test_browser_cookie_and_csrf_are_separate_unpredictable_values(tmp_path: Path) -> None:
    session = CompanionSession.create(tmp_path, JOB_ID)

    assert session.claim_browser(None) is True
    assert session.claim_browser(session.browser_cookie) is False
    assert session.browser_cookie != session.csrf_token
    assert len(session.browser_cookie) >= 32
    assert len(session.csrf_token) >= 32

    with pytest.raises(SessionRejected, match="browser_session"):
        session.claim_browser("another-browser")
    session.close()


def test_mutation_requires_matching_cookie_and_csrf(tmp_path: Path) -> None:
    session = CompanionSession.create(tmp_path, JOB_ID)
    session.claim_browser(None)

    session.authorize_mutation(session.browser_cookie, session.csrf_token)
    with pytest.raises(SessionRejected, match="browser_session"):
        session.authorize_mutation("wrong", session.csrf_token)
    with pytest.raises(SessionRejected, match="csrf"):
        session.authorize_mutation(session.browser_cookie, "wrong")
    session.close()


def test_source_streams_to_generated_name_and_keeps_bounded_display_name(tmp_path: Path) -> None:
    session = CompanionSession.create(tmp_path, JOB_ID, chunk_size=4)
    payload = b"0123456789"

    snapshot = session.receive_source(
        BytesIO(payload),
        content_length=len(payload),
        display_name="  ../a\\b\x00  sample movie.mp4  ",
    )

    assert snapshot.state == "accepted"
    source = next(session.workspace.path.iterdir())
    assert source.parent == session.workspace.path
    UUID(source.name)
    assert session.display_name == ".._a_b_ sample movie.mp4"
    assert not list(session.workspace.path.glob("*.part"))
    with session.open_verified_source() as lease:
        assert lease.stream.read() == payload
    session.close()


def test_oversize_is_rejected_before_any_file_is_created(tmp_path: Path) -> None:
    session = CompanionSession.create(tmp_path, JOB_ID, max_source_bytes=8)

    with pytest.raises(UploadRejected, match="source_too_large"):
        session.receive_source(BytesIO(b"123456789"), 9, "clip.mp4")

    assert list(session.workspace.path.iterdir()) == []
    session.close()


def test_incomplete_stream_removes_partial_file(tmp_path: Path) -> None:
    session = CompanionSession.create(tmp_path, JOB_ID, chunk_size=4)

    with pytest.raises(UploadRejected, match="incomplete_upload"):
        session.receive_source(_ShortStream(), 12, "clip.mp4")

    assert list(session.workspace.path.iterdir()) == []
    session.receive_source(BytesIO(b"retry"), 5, "retry.mp4")
    with session.open_verified_source() as lease:
        assert lease.stream.read() == b"retry"
    session.close()


def test_write_error_releases_source_reservation_for_retry(tmp_path: Path) -> None:
    session = CompanionSession.create(tmp_path, JOB_ID, chunk_size=4)

    with pytest.raises(UploadRejected, match="source_write_failed"):
        session.receive_source(_BrokenStream(), 4, "clip.mp4")

    session.receive_source(BytesIO(b"good"), 4, "retry.mp4")
    with session.open_verified_source() as lease:
        assert lease.stream.read() == b"good"
    session.close()


def test_second_source_is_rejected_deterministically(tmp_path: Path) -> None:
    session = CompanionSession.create(tmp_path, JOB_ID)
    session.receive_source(BytesIO(b"one"), 3, "one.mp4")

    with pytest.raises(UploadRejected, match="source_already_selected"):
        session.receive_source(BytesIO(b"two"), 3, "two.mp4")
    session.close()


def test_cancel_during_upload_removes_partial_and_workspace(tmp_path: Path) -> None:
    session = CompanionSession.create(tmp_path, JOB_ID, chunk_size=4)
    stream = _BlockingStream()
    result: list[str] = []

    def receive() -> None:
        try:
            session.receive_source(stream, 8, "clip.mp4")
        except UploadRejected as exc:
            result.append(exc.code)

    thread = threading.Thread(target=receive)
    thread.start()
    assert stream.started.wait(timeout=1)
    session.cancel()
    stream.release.set()
    thread.join(timeout=2)

    assert result == ["cancelled"]
    assert not session.workspace.path.exists()


def test_repeated_cancel_never_cleans_before_blocked_receive_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = CompanionSession.create(tmp_path, JOB_ID, chunk_size=4)
    stream = _BlockingStream()
    original = JobWorkspace.cleanup
    cleanup_calls = 0
    outcome: list[str] = []

    def counted_cleanup(job_workspace: JobWorkspace) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        original(job_workspace)

    monkeypatch.setattr(JobWorkspace, "cleanup", counted_cleanup)

    def receive() -> None:
        try:
            session.receive_source(stream, 8, "clip.mp4")
        except UploadRejected as exc:
            outcome.append(exc.code)

    thread = threading.Thread(target=receive)
    thread.start()
    assert stream.started.wait(timeout=1)

    assert session.cancel().state == "cancelling"
    assert session.cancel().state == "cancelling"
    assert cleanup_calls == 0
    assert session.workspace.path.exists()

    stream.release.set()
    thread.join(timeout=2)

    assert outcome == ["cancelled"]
    assert cleanup_calls == 1
    assert session.snapshot().state == "cancelled"
    assert not session.workspace.path.exists()


def test_replaced_workspace_symlink_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = CompanionSession.create(tmp_path, JOB_ID)
    workspace_path = session.workspace.path
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace_path.rmdir()
    try:
        workspace_path.symlink_to(outside, target_is_directory=True)
    except OSError:
        workspace_path.mkdir()
        original_is_symlink = Path.is_symlink

        def fake_is_symlink(path: Path) -> bool:
            return path == workspace_path or original_is_symlink(path)

        monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)

    with pytest.raises(UploadRejected, match="unsafe_workspace"):
        session.receive_source(BytesIO(b"video"), 5, "clip.mp4")

    assert list(outside.iterdir()) == []


def test_cleanup_failure_is_retained_and_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = CompanionSession.create(tmp_path, JOB_ID)
    workspace = session.workspace.path
    original = JobWorkspace.cleanup
    attempts = 0

    def flaky_cleanup(job_workspace: JobWorkspace) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("locked")
        original(job_workspace)

    monkeypatch.setattr(JobWorkspace, "cleanup", flaky_cleanup)

    first = session.cancel()
    assert first.state == "cleanup_required"
    assert first.cleaned is False
    assert workspace.exists()
    second = session.cancel()
    assert second.state == "cancelled"
    assert second.cleaned is True
    assert not workspace.exists()


def test_concurrent_cancel_runs_only_one_workspace_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = CompanionSession.create(tmp_path, JOB_ID)
    original = JobWorkspace.cleanup
    cleanup_started = threading.Event()
    release_cleanup = threading.Event()
    calls = 0

    def slow_cleanup(job_workspace: JobWorkspace) -> None:
        nonlocal calls
        calls += 1
        cleanup_started.set()
        release_cleanup.wait(timeout=2)
        original(job_workspace)

    monkeypatch.setattr(JobWorkspace, "cleanup", slow_cleanup)
    first_result: list[object] = []
    first = threading.Thread(target=lambda: first_result.append(session.cancel()))
    first.start()
    assert cleanup_started.wait(timeout=1)

    second = session.cancel()
    assert second.state == "cancelling"
    assert calls == 1
    release_cleanup.set()
    first.join(timeout=2)

    assert first_result[0].state == "cancelled"
    assert session.snapshot().state == "cancelled"


def test_cancel_at_finalization_cannot_also_accept_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = CompanionSession.create(tmp_path, JOB_ID)
    reached_finalization = threading.Event()
    release_finalization = threading.Event()
    original = session._validate_open_file
    outcome: list[str] = []
    validation_count = 0

    def barrier(
        path: Path,
        descriptor: int,
        *,
        expected_size: int | None = None,
    ) -> None:
        nonlocal validation_count
        validation_count += 1
        original(path, descriptor, expected_size=expected_size)
        if validation_count == 2:
            reached_finalization.set()
            release_finalization.wait(timeout=2)

    monkeypatch.setattr(session, "_validate_open_file", barrier)

    def receive() -> None:
        try:
            session.receive_source(BytesIO(b"video"), 5, "clip.mp4")
            outcome.append("accepted")
        except UploadRejected as exc:
            outcome.append(exc.code)

    thread = threading.Thread(target=receive)
    thread.start()
    assert reached_finalization.wait(timeout=1)
    cancellation = session.cancel()
    assert cancellation.state == "cancelling"
    release_finalization.set()
    thread.join(timeout=2)

    assert outcome == ["cancelled"]
    assert session.snapshot().state == "cancelled"
    assert not session.workspace.path.exists()


def test_verified_source_lease_revalidates_and_reads_accepted_bytes(tmp_path: Path) -> None:
    session = CompanionSession.create(tmp_path, JOB_ID)
    session.receive_source(BytesIO(b"verified"), 8, "clip.mp4")

    with session.open_verified_source() as lease:
        assert lease.size_bytes == 8
        assert lease.stream.read() == b"verified"

    session.close()


def test_verified_source_refuses_replaced_path_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = CompanionSession.create(tmp_path, JOB_ID)
    session.receive_source(BytesIO(b"verified"), 8, "clip.mp4")
    source = next(session.workspace.path.iterdir())
    original_stat = Path.stat
    real_stat = original_stat(source, follow_symlinks=False)
    changed = list(real_stat)
    changed[1] += 1
    replacement_stat = os.stat_result(changed)

    def replaced_stat(path: Path, *, follow_symlinks: bool = True):
        if path == source and not follow_symlinks:
            return replacement_stat
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", replaced_stat)
    with pytest.raises(UploadRejected, match="source_identity_changed"):
        with session.open_verified_source():
            pass

    assert session.snapshot().state == "cleanup_required"
    monkeypatch.undo()
    assert session.close().state == "closed"


def test_verified_source_refuses_replaced_workspace_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = CompanionSession.create(tmp_path, JOB_ID)
    session.receive_source(BytesIO(b"verified"), 8, "clip.mp4")
    workspace = session.workspace.path
    original_stat = Path.stat
    real_stat = original_stat(workspace, follow_symlinks=False)
    changed = list(real_stat)
    changed[1] += 1
    replacement_stat = os.stat_result(changed)

    def replaced_stat(path: Path, *, follow_symlinks: bool = True):
        if path == workspace and not follow_symlinks:
            return replacement_stat
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", replaced_stat)
    with pytest.raises(UploadRejected, match="source_identity_changed"):
        with session.open_verified_source():
            pass

    assert session.snapshot().state == "cleanup_required"
    monkeypatch.undo()
    assert session.close().state == "closed"


def test_accepted_source_handle_closes_before_cleanup_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = CompanionSession.create(tmp_path, JOB_ID)
    session.receive_source(BytesIO(b"verified"), 8, "clip.mp4")
    original = JobWorkspace.cleanup
    attempts = 0

    def flaky_cleanup(job_workspace: JobWorkspace) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("locked")
        original(job_workspace)

    monkeypatch.setattr(JobWorkspace, "cleanup", flaky_cleanup)
    assert session.close().state == "cleanup_required"
    assert session.close().state == "closed"
    with pytest.raises(UploadRejected, match="source_unavailable"):
        with session.open_verified_source():
            pass


def test_verified_source_stream_open_failure_enters_safe_cleanup_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = CompanionSession.create(tmp_path, JOB_ID)
    session.receive_source(BytesIO(b"verified"), 8, "clip.mp4")
    original_fdopen = os.fdopen

    def fail_read_stream(descriptor: int, mode: str, **kwargs: object):
        if mode == "rb":
            raise OSError("stream unavailable")
        return original_fdopen(descriptor, mode, **kwargs)

    monkeypatch.setattr(os, "fdopen", fail_read_stream)
    with pytest.raises(UploadRejected, match="source_identity_changed"):
        with session.open_verified_source():
            pass

    assert session.snapshot().state == "cleanup_required"
    monkeypatch.undo()
    assert session.close().state == "closed"
