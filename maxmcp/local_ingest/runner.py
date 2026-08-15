"""One-at-a-time local video processing and ARTOKE publication."""

from __future__ import annotations

from dataclasses import dataclass
from http.client import HTTPException
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import threading
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, build_opener
from uuid import uuid4

from maxmcp.local_ingest.api_client import (
    LocalIngestApiClient,
    LocalIngestApiError,
)
from maxmcp.local_ingest.probe import ProbeRejected, VideoProbe, probe_video
from maxmcp.local_ingest.session import CompanionSession, UploadRejected
from maxmcp.worker.artifacts import (
    LocalArtifact,
    build_artifacts,
    sha256_file,
    upload_signed_artifact,
)
from maxmcp.worker.motion_pipeline import MotionPipeline, PipelineCancelled


_LOCAL_RUN_LOCK = threading.Lock()
_COPY_CHUNK = 1024 * 1024
_OUTPUT_HEADROOM = 512 * 1024 * 1024
_CONTENT_TYPES = {
    "bvh": "application/octet-stream",
    "rtmw3d_json": "application/gzip",
    "thumbnail": "image/webp",
    "metadata": "application/json",
}


class LocalRunRejected(RuntimeError):
    """A stable local processing failure with no path or secret details."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LocalRunResult:
    state: str
    job_id: str | None
    cleanup_required: bool = False


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _upload_without_redirect(url: str, path: Path, content_type: str) -> None:
    opener = build_opener(_RejectRedirects()).open
    upload_signed_artifact(url, path, content_type, opener=opener)


def _manifest(artifacts: Sequence[LocalArtifact]) -> tuple[dict[str, object], ...]:
    if len(artifacts) != 4 or {item.kind for item in artifacts} != set(_CONTENT_TYPES):
        raise LocalRunRejected("artifact_set_invalid")
    result: list[dict[str, object]] = []
    for item in artifacts:
        if not item.path.is_file():
            raise LocalRunRejected("artifact_set_invalid")
        size = item.path.stat().st_size
        digest = sha256_file(item.path)
        if size != item.size_bytes or digest != item.sha256:
            raise LocalRunRejected("artifact_changed")
        result.append(
            {
                "kind": item.kind,
                "sizeBytes": size,
                "sha256": digest,
                "formatVersion": item.format_version,
            }
        )
    return tuple(result)


def _safe_processing_file(path: Path, workspace: Path, expected_size: int) -> None:
    try:
        info = path.stat(follow_symlinks=False)
        workspace_info = workspace.stat(follow_symlinks=False)
        attributes = getattr(info, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            path.parent != workspace
            or path.is_symlink()
            or (hasattr(path, "is_junction") and path.is_junction())
            or not stat.S_ISREG(info.st_mode)
            or info.st_size != expected_size
            or attributes & reparse
            or not stat.S_ISDIR(workspace_info.st_mode)
            or workspace.is_symlink()
            or path.resolve() != path
        ):
            raise OSError
    except OSError:
        raise LocalRunRejected("source_materialization_failed") from None


class LocalIngestRunner:
    """Consume one verified source, publish four artifacts, then clean locally."""

    def __init__(
        self,
        session: CompanionSession,
        api: LocalIngestApiClient,
        pipeline: MotionPipeline,
        *,
        probe: Callable[..., VideoProbe] = probe_video,
        artifact_builder: Callable[..., tuple[LocalArtifact, ...]] = build_artifacts,
        signed_uploader: Callable[[str, Path, str], None] = _upload_without_redirect,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session = session
        self._api = api
        self._pipeline = pipeline
        self._probe = probe
        self._artifact_builder = artifact_builder
        self._signed_uploader = signed_uploader
        self._disk_usage = disk_usage
        self._sleeper = sleeper
        self._cancelled = threading.Event()
        self._last_progress = -1
        self._last_stage = -1

    def cancel(self) -> None:
        self._cancelled.set()
        self._pipeline.cancel()

    def run(self, name: str) -> LocalRunResult:
        if not _LOCAL_RUN_LOCK.acquire(blocking=False):
            raise LocalRunRejected("local_job_in_progress")
        job_id: str | None = None
        workspace = self._session.workspace
        retained = False
        try:
            workspace.acquire()
            with self._session.open_verified_source() as lease:
                required = lease.size_bytes * 3 + _OUTPUT_HEADROOM
                try:
                    free = self._disk_usage(workspace.path).free
                except (OSError, AttributeError):
                    raise LocalRunRejected("disk_space_unavailable") from None
                if not isinstance(free, int) or isinstance(free, bool) or free < required:
                    raise LocalRunRejected("insufficient_disk_space")
                source, source_hash = self._materialize(lease.stream, lease.size_bytes, lease.display_name)
                video = self._probe(source, lease.display_name)
                if self._cancelled.is_set():
                    raise PipelineCancelled()
                job = self._api.create_job(
                    {
                        "name": name.strip()[:120] or "Local motion",
                        "filename": lease.display_name,
                        "contentType": video.content_type,
                        "sizeBytes": lease.size_bytes,
                        "durationSeconds": video.duration_seconds,
                        "sourceSha256": source_hash,
                    }
                )
                job_id = job.job_id
                self._progress(job_id, "downloading", 5)
                pipeline_workspace = workspace.path / "pipeline"
                pipeline_workspace.mkdir()
                pipeline_result = self._pipeline.run(
                    source,
                    pipeline_workspace,
                    lambda stage, percent: self._progress(job_id, stage, percent),
                    self._cancelled.is_set,
                )
                artifacts = self._artifact_builder(
                    source,
                    pipeline_result,
                    workspace.path / "artifacts",
                    video.duration_seconds,
                    edit_revision=0,
                )
                frozen = _manifest(artifacts)
                self._progress(job_id, "uploading", 90)
                try:
                    published = self._upload_and_publish(job_id, artifacts, frozen)
                except (LocalIngestApiError, HTTPError, URLError, TimeoutError, HTTPException):
                    self._write_retry(job_id, frozen)
                    retained = True
                    return LocalRunResult("publication_pending", job_id)
                if published.job_id != job_id or published.status != "completed":
                    self._write_retry(job_id, frozen)
                    retained = True
                    return LocalRunResult("publication_pending", job_id)

            cleanup = self._session.close()
            if cleanup.state != "closed" or not cleanup.cleaned:
                return LocalRunResult("cleanup_required", job_id, cleanup_required=True)
            try:
                acknowledged = self._retry(lambda: self._api.acknowledge_cleanup(job_id))
            except (LocalIngestApiError, HTTPError, URLError, TimeoutError, HTTPException):
                return LocalRunResult("cleanup_required", job_id, cleanup_required=True)
            if not acknowledged:
                return LocalRunResult("cleanup_required", job_id, cleanup_required=True)
            return LocalRunResult("completed", job_id)
        except PipelineCancelled:
            if job_id is not None:
                try:
                    self._api.finish_cancelled(job_id)
                except (LocalIngestApiError, HTTPError, URLError, TimeoutError, HTTPException):
                    pass
            self._session.cancel()
            return LocalRunResult("cancelled", job_id)
        except (ProbeRejected, UploadRejected) as exc:
            self._fail_and_clean(job_id, exc.code)
            raise LocalRunRejected(exc.code) from None
        except LocalRunRejected:
            if job_id is not None and not retained:
                self._fail_and_clean(job_id, "local_processing_failed")
            raise
        except (RuntimeError, ValueError, OSError):
            self._fail_and_clean(job_id, "local_processing_failed")
            raise LocalRunRejected("local_processing_failed") from None
        finally:
            workspace.release()
            _LOCAL_RUN_LOCK.release()

    def _materialize(self, stream: Any, expected_size: int, display_name: str) -> tuple[Path, str]:
        extension = Path(display_name).suffix.lower()
        if extension not in {".mp4", ".mov", ".avi"}:
            raise LocalRunRejected("video_container_mismatch")
        destination = self._session.workspace.path / f"{uuid4()}{extension}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        for optional in ("O_BINARY", "O_NOINHERIT", "O_NOFOLLOW"):
            flags |= int(getattr(os, optional, 0))
        digest = hashlib.sha256()
        remaining = expected_size
        try:
            descriptor = os.open(destination, flags, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                while remaining:
                    self._ensure_not_cancelled()
                    block = stream.read(min(_COPY_CHUNK, remaining))
                    if not isinstance(block, bytes) or not block:
                        raise OSError
                    output.write(block)
                    digest.update(block)
                    remaining -= len(block)
                if stream.read(1):
                    raise OSError
                output.flush()
                os.fsync(output.fileno())
            _safe_processing_file(destination, self._session.workspace.path, expected_size)
            return destination, digest.hexdigest()
        except PipelineCancelled:
            destination.unlink(missing_ok=True)
            raise
        except (OSError, ValueError):
            destination.unlink(missing_ok=True)
            raise LocalRunRejected("source_materialization_failed") from None

    def _progress(self, job_id: str, stage: str, percent: int) -> None:
        self._ensure_not_cancelled()
        stages = ("downloading", "extracting", "converting", "validating", "uploading")
        try:
            stage_index = stages.index(stage)
        except ValueError:
            raise LocalRunRejected("progress_invalid") from None
        if (
            not isinstance(percent, int)
            or isinstance(percent, bool)
            or not 0 <= percent < 100
            or stage_index < self._last_stage
            or percent < self._last_progress
        ):
            raise LocalRunRejected("progress_invalid")
        response = self._api.report_progress(job_id, stage, min(percent, 99))
        self._last_stage = stage_index
        self._last_progress = percent
        self._session.workspace.heartbeat()
        if response.cancel_requested:
            self.cancel()
            raise PipelineCancelled()

    def _ensure_not_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise PipelineCancelled()

    def _retry(self, operation: Callable[[], Any]) -> Any:
        for attempt in range(3):
            self._ensure_not_cancelled()
            try:
                return operation()
            except (LocalIngestApiError, HTTPError, URLError, TimeoutError, HTTPException) as exc:
                status = getattr(exc, "status", None)
                if isinstance(exc, HTTPError):
                    status = exc.code
                retryable = status is None or status in {408, 425, 429} or status >= 500
                if not retryable or attempt == 2:
                    raise
                self._sleeper(0.1 * (attempt + 1))
        raise AssertionError("unreachable")

    def _upload_and_publish(
        self,
        job_id: str,
        artifacts: Sequence[LocalArtifact],
        frozen: tuple[dict[str, object], ...],
    ) -> Any:
        authorizations = self._retry(lambda: self._api.authorize_uploads(job_id))
        by_kind = {item.kind: item.upload_url for item in authorizations}
        if set(by_kind) != set(_CONTENT_TYPES):
            raise LocalRunRejected("upload_authorization_invalid")
        for artifact in artifacts:
            self._ensure_not_cancelled()
            if _manifest(artifacts) != frozen:
                raise LocalRunRejected("artifact_changed")
            self._retry(
                lambda item=artifact: self._signed_uploader(
                    by_kind[item.kind], item.path, _CONTENT_TYPES[item.kind]
                )
            )
            if _manifest(artifacts) != frozen:
                raise LocalRunRejected("artifact_changed")
        self._ensure_not_cancelled()
        final_manifest = _manifest(artifacts)
        if final_manifest != frozen:
            raise LocalRunRejected("artifact_changed")
        return self._retry(lambda: self._api.publish(job_id, 0, final_manifest))

    def _write_retry(self, job_id: str, manifest: Sequence[Mapping[str, object]]) -> None:
        payload = {"schema": "artoke.local.retry.v1", "jobId": job_id, "editRevision": 0, "artifacts": list(manifest)}
        target = self._session.workspace.path / "retry.json"
        target.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")

    def _fail_and_clean(self, job_id: str | None, code: str) -> None:
        if job_id is not None:
            try:
                self._api.finish_failed(job_id, code if code.replace("_", "").isalnum() else "local_processing_failed")
            except (LocalIngestApiError, HTTPError, URLError, TimeoutError, HTTPException):
                pass
        self._session.close()
