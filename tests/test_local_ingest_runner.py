from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import json
import threading

import pytest

from maxmcp.local_ingest.api_client import LocalIngestApiError, UploadAuthorization
from maxmcp.local_ingest.probe import VideoProbe
from maxmcp.local_ingest.runner import LocalIngestRunner, LocalRunRejected
from maxmcp.local_ingest.session import CompanionSession
from maxmcp.worker.artifacts import LocalArtifact
from maxmcp.worker.motion_pipeline import PipelineArtifacts, PipelineCancelled
from maxmcp.worker.workspace import JobWorkspace


JOB_ID = "00000000-0000-4000-8000-000000000009"


class _Api:
    def __init__(self) -> None:
        self.calls = []
        self.publish_error: Exception | None = None

    def create_job(self, metadata):
        self.calls.append(("create", metadata))
        return SimpleNamespace(job_id=JOB_ID, status="queued")

    def report_progress(self, job_id, stage, progress):
        self.calls.append(("progress", stage, progress))
        return SimpleNamespace(cancel_requested=False)

    def authorize_uploads(self, job_id):
        self.calls.append(("authorize",))
        return tuple(
            UploadAuthorization(kind, f"https://storage.test/{kind}?secret=value")
            for kind in ("bvh", "rtmw3d_json", "thumbnail", "metadata")
        )

    def publish(self, job_id, revision, manifest):
        self.calls.append(("publish", revision, manifest))
        if self.publish_error:
            raise self.publish_error
        return SimpleNamespace(job_id=job_id, status="completed")

    def acknowledge_cleanup(self, job_id):
        self.calls.append(("cleanup",))
        return True

    def finish_cancelled(self, job_id):
        self.calls.append(("cancelled",))
        return SimpleNamespace(job_id=job_id, status="cancelled")

    def finish_failed(self, job_id, code):
        self.calls.append(("failed", code))
        return SimpleNamespace(job_id=job_id, status="failed")


class _Pipeline:
    def __init__(self, *, cancel=False) -> None:
        self.cancelled = False
        self.raise_cancel = cancel

    def run(self, video, workspace, on_stage, cancelled):
        if self.raise_cancel:
            raise PipelineCancelled()
        on_stage("extracting", 15)
        body = workspace / "body.json"; body.write_text("{}", encoding="utf-8")
        bvh = workspace / "motion.bvh"; bvh.write_text("HIERARCHY\nMOTION\nFrames: 1\nFrame Time: 0.0333333\n", encoding="utf-8")
        trace = workspace / "trace.json"; trace.write_text('{"backend":"test"}', encoding="utf-8")
        on_stage("converting", 65); on_stage("validating", 85)
        return PipelineArtifacts(body, bvh, trace, 1)

    def cancel(self):
        self.cancelled = True


def _session(tmp_path: Path) -> CompanionSession:
    session = CompanionSession.create(tmp_path / "cache", JOB_ID)
    session.receive_source(BytesIO(b"source-video"), 12, "clip.mp4")
    return session


def _probe(*_args, **_kwargs) -> VideoProbe:
    return VideoProbe(4.0, "mp4", "h264", 1920, 1080, "video/mp4", ".mp4")


def _build(video, pipeline, output, duration, **_kwargs):
    output.mkdir()
    artifacts = []
    for kind, name in (
        ("bvh", "motion.bvh"),
        ("rtmw3d_json", "motion.rtmw3d.json.gz"),
        ("thumbnail", "thumbnail.webp"),
        ("metadata", "metadata.json"),
    ):
        path = output / name; path.write_bytes(kind.encode())
        from maxmcp.worker.artifacts import sha256_file
        artifacts.append(LocalArtifact(kind, path, path.stat().st_size, sha256_file(path)))
    return tuple(artifacts)


def _runner(tmp_path: Path, **overrides) -> tuple[LocalIngestRunner, _Api, CompanionSession]:
    api = overrides.pop("api", _Api())
    session = overrides.pop("session", _session(tmp_path))
    runner = LocalIngestRunner(
        session,
        api,
        overrides.pop("pipeline", _Pipeline()),
        probe=overrides.pop("probe", _probe),
        artifact_builder=overrides.pop("artifact_builder", _build),
        signed_uploader=overrides.pop("signed_uploader", lambda *_a: None),
        disk_usage=overrides.pop("disk_usage", lambda _p: SimpleNamespace(free=10**12)),
        sleeper=overrides.pop("sleeper", lambda _s: None),
        **overrides,
    )
    return runner, api, session


def test_runner_processes_uploads_publishes_then_deletes_and_acknowledges(tmp_path: Path) -> None:
    uploads = []
    runner, api, session = _runner(
        tmp_path,
        signed_uploader=lambda url, path, content_type: uploads.append((url, path.name, content_type)),
    )
    workspace = session.workspace.path

    result = runner.run("My motion")

    assert result.state == "completed"
    assert not workspace.exists()
    progress = [(call[1], call[2]) for call in api.calls if call[0] == "progress"]
    assert progress == sorted(progress, key=lambda item: item[1])
    assert [stage for stage, _ in progress] == [
        "downloading", "extracting", "converting", "validating", "uploading"
    ]
    assert len(uploads) == 4
    assert [call[0] for call in api.calls][-2:] == ["publish", "cleanup"]


def test_runner_materializes_exact_verified_bytes_under_uuid_name(tmp_path: Path) -> None:
    observed = {}

    def inspect(path, display_name):
        observed["name"] = path.name
        observed["bytes"] = path.read_bytes()
        observed["display"] = display_name
        return _probe()

    runner, _api, _session_value = _runner(tmp_path, probe=inspect)
    runner.run("Motion")

    stem, suffix = observed["name"].split(".", 1)
    from uuid import UUID
    UUID(stem)
    assert suffix == "mp4"
    assert observed["bytes"] == b"source-video"
    assert observed["display"] == "clip.mp4"


def test_runner_rejects_disk_full_before_materializing_or_network(tmp_path: Path) -> None:
    runner, api, session = _runner(
        tmp_path, disk_usage=lambda _p: SimpleNamespace(free=1)
    )

    with pytest.raises(LocalRunRejected, match="insufficient_disk_space"):
        runner.run("Motion")

    assert api.calls == []
    assert len(list(session.workspace.path.iterdir())) == 1
    session.close()


def test_runner_retries_upload_network_failure_with_bound_and_no_secret_file(tmp_path: Path) -> None:
    attempts = 0

    def flaky(_url, _path, _content_type):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TimeoutError("https://storage.test/?token=secret")

    runner, _api, _session_value = _runner(tmp_path, signed_uploader=flaky)
    assert runner.run("Motion").state == "completed"
    assert attempts == 6


def test_publication_failure_retains_source_and_secret_free_retry_metadata(tmp_path: Path) -> None:
    api = _Api(); api.publish_error = LocalIngestApiError("request failed", status=503)
    runner, _api, session = _runner(tmp_path, api=api)
    workspace = session.workspace.path

    result = runner.run("Motion")

    assert result.state == "publication_pending"
    assert workspace.exists()
    retry = json.loads((workspace / "retry.json").read_text(encoding="utf-8"))
    rendered = json.dumps(retry)
    assert retry["jobId"] == JOB_ID
    assert "secret" not in rendered
    assert "clip.mp4" not in rendered
    assert str(workspace) not in rendered
    session.close()


def test_upload_failure_retains_source_and_bounded_retry_metadata(tmp_path: Path) -> None:
    attempts = 0
    def offline(*_args):
        nonlocal attempts
        attempts += 1
        raise TimeoutError("offline")
    runner, _api, session = _runner(tmp_path, signed_uploader=offline)

    result = runner.run("Motion")

    assert result.state == "publication_pending"
    assert attempts == 3
    assert (session.workspace.path / "retry.json").is_file()
    session.close()


def test_completed_ack_uses_the_same_frozen_manifest_uploaded(tmp_path: Path) -> None:
    uploaded = {}
    def upload(_url, path, _content_type):
        from maxmcp.worker.artifacts import sha256_file
        uploaded[path.name] = (path.stat().st_size, sha256_file(path))
    runner, api, _session_value = _runner(tmp_path, signed_uploader=upload)

    assert runner.run("Motion").state == "completed"
    publication = next(call for call in api.calls if call[0] == "publish")
    manifest = publication[2]
    names = {
        "bvh": "motion.bvh", "rtmw3d_json": "motion.rtmw3d.json.gz",
        "thumbnail": "thumbnail.webp", "metadata": "metadata.json",
    }
    assert all(
        uploaded[names[item["kind"]]] == (item["sizeBytes"], item["sha256"])
        for item in manifest
    )


def test_runner_rejects_nonmonotonic_or_unknown_pipeline_progress(tmp_path: Path) -> None:
    class BadProgress(_Pipeline):
        def run(self, video, workspace, on_stage, cancelled):
            on_stage("extracting", 40)
            on_stage("converting", 30)
            raise AssertionError("runner must reject before continuing")
    runner, _api, _session_value = _runner(tmp_path, pipeline=BadProgress())

    with pytest.raises(LocalRunRejected, match="progress_invalid"):
        runner.run("Motion")


def test_pipeline_validation_failure_is_safe_terminal_and_cleans_source(tmp_path: Path) -> None:
    class Broken(_Pipeline):
        def run(self, *_args, **_kwargs):
            raise RuntimeError("C:\\private\\clip.mp4 failed")
    runner, api, session = _runner(tmp_path, pipeline=Broken())
    workspace = session.workspace.path

    with pytest.raises(LocalRunRejected, match="local_processing_failed") as raised:
        runner.run("Motion")

    assert "private" not in str(raised.value)
    assert not workspace.exists()
    assert ("failed", "local_processing_failed") in api.calls


def test_cancellation_terminates_pipeline_cleans_and_stops_future_upload_calls(tmp_path: Path) -> None:
    pipeline = _Pipeline(cancel=True)
    runner, api, session = _runner(tmp_path, pipeline=pipeline)
    workspace = session.workspace.path

    result = runner.run("Motion")

    assert result.state == "cancelled"
    assert not workspace.exists()
    assert [call[0] for call in api.calls][-1] == "cancelled"
    assert not any(call[0] in {"authorize", "publish", "cleanup"} for call in api.calls)


def test_cancel_from_another_thread_terminates_active_pipeline(tmp_path: Path) -> None:
    started = threading.Event(); release = threading.Event()

    class Blocking(_Pipeline):
        def run(self, *_args, **_kwargs):
            started.set(); release.wait(2); raise PipelineCancelled()
        def cancel(self):
            super().cancel(); release.set()

    pipeline = Blocking()
    runner, _api, _session_value = _runner(tmp_path, pipeline=pipeline)
    results = []
    thread = threading.Thread(target=lambda: results.append(runner.run("Motion")))
    thread.start(); assert started.wait(1)
    runner.cancel(); thread.join(2)
    assert pipeline.cancelled is True
    assert results[0].state == "cancelled"


def test_process_wide_lock_rejects_second_concurrent_local_job(tmp_path: Path) -> None:
    started = threading.Event(); release = threading.Event()

    class Blocking(_Pipeline):
        def run(self, *_args, **_kwargs):
            started.set(); release.wait(2); raise PipelineCancelled()

    first, _api1, _session1 = _runner(tmp_path / "one", pipeline=Blocking())
    second, _api2, session2 = _runner(tmp_path / "two")
    thread = threading.Thread(target=lambda: first.run("one")); thread.start()
    assert started.wait(1)
    with pytest.raises(LocalRunRejected, match="local_job_in_progress"):
        second.run("two")
    release.set(); thread.join(2); session2.close()


def test_cleanup_failure_is_truthfully_reported_without_cleanup_ack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, api, session = _runner(tmp_path)
    monkeypatch.setattr(JobWorkspace, "cleanup", lambda _self: (_ for _ in ()).throw(OSError("locked")))

    result = runner.run("Motion")

    assert result.state == "cleanup_required"
    assert not any(call[0] == "cleanup" for call in api.calls)
