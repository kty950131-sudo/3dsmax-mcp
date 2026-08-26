from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import json
import threading
from urllib.error import HTTPError

import pytest

from maxmcp.local_ingest.api_client import (
    LocalIngestApiError,
    SourceUploadAuthorization,
    UploadAuthorization,
)
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
        self.publish_hook = None
        self.source_authorize_error: Exception | None = None
        self.source_complete_error: Exception | None = None

    def create_job(self, metadata):
        self.calls.append(("create", metadata))
        return SimpleNamespace(job_id=JOB_ID, status="queued")

    def report_progress(self, job_id, stage, progress):
        self.calls.append(("progress", stage, progress))
        return SimpleNamespace(cancel_requested=False)

    def authorize_source_upload(self, job_id):
        self.calls.append(("source_authorize",))
        if self.source_authorize_error:
            raise self.source_authorize_error
        return SourceUploadAuthorization("https://storage.test/source?secret=value")

    def complete_source_upload(self, job_id):
        self.calls.append(("source_complete",))
        if self.source_complete_error:
            raise self.source_complete_error
        return SimpleNamespace(job_id=job_id, status="processing")

    def authorize_uploads(self, job_id):
        self.calls.append(("authorize",))
        return tuple(
            UploadAuthorization(kind, f"https://storage.test/{kind}?secret=value")
            for kind in ("bvh", "rtmw3d_json", "thumbnail", "metadata")
        )

    def publish(self, job_id, revision, manifest):
        self.calls.append(("publish", revision, manifest))
        if self.publish_hook:
            self.publish_hook()
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
    assert len(uploads) == 5
    source_upload = uploads[0]
    assert source_upload[0] == "https://storage.test/source?secret=value"
    assert source_upload[1].endswith(".mp4")
    assert source_upload[2] == "video/mp4"
    names = [call[0] for call in api.calls]
    assert names.index("source_authorize") < names.index("source_complete")
    assert names.index("source_complete") < names.index("authorize")
    assert names[-2:] == ["publish", "cleanup"]


def test_runner_fails_when_source_authorization_is_unavailable(tmp_path: Path) -> None:
    uploads = []
    api = _Api()
    api.source_authorize_error = LocalIngestApiError("ARTOKE API returned HTTP 404", status=404)
    runner, api, _session_value = _runner(
        tmp_path,
        api=api,
        signed_uploader=lambda url, path, content_type: uploads.append(url),
    )

    with pytest.raises(LocalRunRejected, match="source_upload_failed"):
        runner.run("Motion")

    assert uploads == []
    names = [call[0] for call in api.calls]
    assert "source_complete" not in names
    assert ("failed", "local_processing_failed") in api.calls


def test_runner_fails_the_job_when_the_source_upload_is_rejected(tmp_path: Path) -> None:
    api = _Api()
    api.source_complete_error = LocalIngestApiError("ARTOKE API returned HTTP 409", status=409)
    runner, api, _session_value = _runner(tmp_path, api=api)

    with pytest.raises(LocalRunRejected, match="source_upload_failed"):
        runner.run("Motion")

    names = [call[0] for call in api.calls]
    assert "authorize" not in names
    assert ("failed", "local_processing_failed") in api.calls


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
    assert attempts == 7


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
    def offline(url, *_args):
        if "/source?" in url:
            return
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


def test_retry_publication_reads_strict_manifest_and_uses_fresh_upload_urls(tmp_path: Path) -> None:
    api = _Api(); api.publish_error = TimeoutError("response lost")
    uploaded = []
    runner, _api, session = _runner(
        tmp_path, api=api,
        signed_uploader=lambda url, path, content_type: uploaded.append((url, path.name)),
    )
    assert runner.run("Motion").state == "publication_pending"
    api.publish_error = None

    result = runner.retry_publication()

    assert result.state == "completed"
    assert len([call for call in api.calls if call[0] == "authorize"]) == 1
    assert len(uploaded) == 5
    assert len([item for item in uploaded if "/source?" in item[0]]) == 1
    assert not session.workspace.path.exists()


def test_publish_response_loss_replays_publish_before_any_reupload(tmp_path: Path) -> None:
    class CommittedButLost(_Api):
        def __init__(self):
            super().__init__(); self.publish_attempts = 0; self.authorize_attempts = 0
        def authorize_uploads(self, job_id):
            self.authorize_attempts += 1
            if self.authorize_attempts > 1:
                raise AssertionError("completed objects must not be reauthorized or reuploaded")
            return super().authorize_uploads(job_id)
        def publish(self, job_id, revision, manifest):
            self.calls.append(("publish", revision, manifest)); self.publish_attempts += 1
            if self.publish_attempts <= 3:
                raise TimeoutError("response lost after commit")
            return SimpleNamespace(job_id=job_id, status="completed")
    api = CommittedButLost()
    runner, _api, session = _runner(tmp_path, api=api)

    assert runner.run("Motion").state == "publication_pending"
    retry = json.loads((session.workspace.path / "retry.json").read_text(encoding="utf-8"))
    assert retry["phase"] == "publish_indeterminate"
    result = runner.retry_publication()

    assert result.state == "completed"
    assert api.authorize_attempts == 1
    assert not session.workspace.path.exists()


def test_upload_failure_persists_upload_pending_phase(tmp_path: Path) -> None:
    def artifact_offline(url, *_args):
        if "/source?" in url:
            return
        raise TimeoutError("offline")
    runner, _api, session = _runner(tmp_path, signed_uploader=artifact_offline)
    assert runner.run("Motion").state == "publication_pending"
    retry = json.loads((session.workspace.path / "retry.json").read_text(encoding="utf-8"))
    assert retry["phase"] == "upload_pending"
    session.close()


def test_upload_pending_retry_requests_fresh_urls_and_reuploads(tmp_path: Path) -> None:
    offline = True
    uploads = []
    def upload(url, path, _content_type):
        if "/source?" in url:
            return
        if offline:
            raise TimeoutError("offline")
        uploads.append((url, path.name))
    runner, api, _session_value = _runner(tmp_path, signed_uploader=upload)
    assert runner.run("Motion").state == "publication_pending"
    offline = False

    assert runner.retry_publication().state == "completed"
    assert len([call for call in api.calls if call[0] == "authorize"]) == 2
    assert len(uploads) == 4


def test_indeterminate_publish_conflict_stays_retained_without_reupload(tmp_path: Path) -> None:
    api = _Api(); api.publish_error = TimeoutError("lost")
    runner, _api, session = _runner(tmp_path, api=api)
    assert runner.run("Motion").state == "publication_pending"
    authorize_before = len([call for call in api.calls if call[0] == "authorize"])
    api.publish_error = LocalIngestApiError("conflict", status=409)

    result = runner.retry_publication()

    assert result.state == "publication_conflict"
    assert session.workspace.path.exists()
    assert len([call for call in api.calls if call[0] == "authorize"]) == authorize_before
    session.close()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda body: body.update({"extra": True}),
        lambda body: body.update({"editRevision": 1}),
        lambda body: body["artifacts"][0].update({"path": "../../escape"}),
        lambda body: body["artifacts"].pop(),
    ],
    ids=["extra-key", "wrong-revision", "path-key", "missing-artifact"],
)
def test_retry_publication_rejects_malformed_retry_file_and_cleans_terminally(
    tmp_path: Path, mutation
) -> None:
    api = _Api(); api.publish_error = TimeoutError("offline")
    runner, _api, session = _runner(tmp_path, api=api)
    assert runner.run("Motion").state == "publication_pending"
    retry = session.workspace.path / "retry.json"
    body = json.loads(retry.read_text(encoding="utf-8")); mutation(body)
    retry.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(LocalRunRejected, match="retry_manifest_invalid"):
        runner.retry_publication()

    assert not session.workspace.path.exists()


def test_retry_publication_rejects_non_utf8_retry_file_safely(tmp_path: Path) -> None:
    api = _Api(); api.publish_error = TimeoutError("offline")
    runner, _api, session = _runner(tmp_path, api=api)
    assert runner.run("Motion").state == "publication_pending"
    (session.workspace.path / "retry.json").write_bytes(b"\xff\xfe")
    with pytest.raises(LocalRunRejected, match="retry_manifest_invalid"):
        runner.retry_publication()
    assert not session.workspace.path.exists()


def test_retry_artifact_mutation_is_terminally_cleaned(tmp_path: Path) -> None:
    api = _Api(); api.publish_error = TimeoutError("offline")
    runner, _api, session = _runner(tmp_path, api=api)
    assert runner.run("Motion").state == "publication_pending"
    artifact = session.workspace.path / "artifacts" / "motion.bvh"
    artifact.write_bytes(b"changed")

    with pytest.raises(LocalRunRejected, match="retry_manifest_invalid"):
        runner.retry_publication()

    assert not session.workspace.path.exists()


def test_completed_publish_latches_before_racing_cancel_and_still_cleans(tmp_path: Path) -> None:
    api = _Api()
    runner, _api, session = _runner(tmp_path, api=api)
    api.publish_hook = runner.cancel

    result = runner.run("Motion")

    assert result.state == "completed"
    assert not session.workspace.path.exists()
    assert not any(call[0] == "cancelled" for call in api.calls)
    assert any(call[0] == "cleanup" for call in api.calls)


@pytest.mark.parametrize("status", [302, 400, 401, 403, 404, 409])
def test_deterministic_publication_failures_terminally_clean(tmp_path: Path, status: int) -> None:
    api = _Api(); api.publish_error = LocalIngestApiError("safe", status=status)
    runner, _api, session = _runner(tmp_path, api=api)
    workspace = session.workspace.path

    with pytest.raises(LocalRunRejected, match="publication_failed"):
        runner.run("Motion")

    assert not workspace.exists()
    assert not any(call[0] == "cleanup" for call in api.calls)
    assert ("failed", "publication_failed") in api.calls


@pytest.mark.parametrize("status", [408, 425, 429, 500, 503])
def test_retryable_publication_statuses_retain_for_retry(tmp_path: Path, status: int) -> None:
    api = _Api(); api.publish_error = LocalIngestApiError("safe", status=status)
    runner, _api, session = _runner(tmp_path, api=api)
    assert runner.run("Motion").state == "publication_pending"
    assert session.workspace.path.exists()
    session.close()


def test_signed_upload_redirect_is_terminal_and_never_retained(tmp_path: Path) -> None:
    def redirect(url, *_args):
        if "/source?" in url:
            return
        raise HTTPError("https://storage.test/?secret=value", 302, "redirect", {}, None)
    runner, api, session = _runner(tmp_path, signed_uploader=redirect)
    workspace = session.workspace.path

    with pytest.raises(LocalRunRejected, match="publication_failed"):
        runner.run("Motion")

    assert not workspace.exists()
    assert ("failed", "publication_failed") in api.calls


def test_invalid_upload_authorization_contract_is_terminal_publication_failure(tmp_path: Path) -> None:
    class BadAuthorization(_Api):
        def authorize_uploads(self, job_id):
            self.calls.append(("authorize",))
            return (UploadAuthorization("bvh", "https://storage.test/bvh"),)
    api = BadAuthorization()
    runner, _api, session = _runner(tmp_path, api=api)
    workspace = session.workspace.path

    with pytest.raises(LocalRunRejected, match="publication_failed"):
        runner.run("Motion")

    assert not workspace.exists()
    assert ("failed", "publication_failed") in api.calls


def test_cleanup_failure_during_terminal_error_surfaces_cleanup_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _Api(); api.publish_error = LocalIngestApiError("C:\\secret", status=403)
    runner, _api, session = _runner(tmp_path, api=api)
    monkeypatch.setattr(JobWorkspace, "cleanup", lambda _self: (_ for _ in ()).throw(OSError("C:\\private")))

    with pytest.raises(LocalRunRejected, match="cleanup_required") as raised:
        runner.run("Motion")

    assert "private" not in str(raised.value)


def test_cancellation_cleanup_failure_returns_cleanup_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, _api, session = _runner(tmp_path, pipeline=_Pipeline(cancel=True))
    monkeypatch.setattr(JobWorkspace, "cleanup", lambda _self: (_ for _ in ()).throw(OSError("locked")))

    result = runner.run("Motion")

    assert result.state == "cleanup_required"
    assert result.cleanup_required is True


@pytest.mark.parametrize("invalid_job_id", [None, 9, [], {}], ids=["null", "number", "list", "object"])
def test_retry_job_id_type_errors_normalize_and_clean_safely(tmp_path: Path, invalid_job_id: object) -> None:
    api = _Api(); api.publish_error = TimeoutError("offline")
    runner, _api, session = _runner(tmp_path, api=api)
    assert runner.run("Motion").state == "publication_pending"
    target = session.workspace.path / "retry.json"
    body = json.loads(target.read_text(encoding="utf-8")); body["jobId"] = invalid_job_id
    target.write_text(json.dumps(body), encoding="utf-8")

    with pytest.raises(LocalRunRejected, match="retry_manifest_invalid"):
        runner.retry_publication()

    assert not session.workspace.path.exists()
