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
from uuid import UUID, uuid4

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
from maxmcp.worker.workspace import WorkspaceProcessLock


_LOCAL_RUN_LOCK = threading.Lock()
_COPY_CHUNK = 1024 * 1024
_OUTPUT_HEADROOM = 512 * 1024 * 1024
_CONTENT_TYPES = {
    "bvh": "application/octet-stream",
    "rtmw3d_json": "application/gzip",
    "thumbnail": "image/webp",
    "metadata": "application/json",
}
_ARTIFACT_NAMES = {
    "bvh": "motion.bvh",
    "rtmw3d_json": "motion.rtmw3d.json.gz",
    "thumbnail": "thumbnail.webp",
    "metadata": "metadata.json",
}
_NETWORK_ERRORS = (LocalIngestApiError, HTTPError, URLError, TimeoutError, HTTPException)


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
        try:
            info = item.path.stat(follow_symlinks=False)
            attributes = getattr(info, "st_file_attributes", 0)
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        except OSError:
            raise LocalRunRejected("artifact_set_invalid") from None
        if (
            not stat.S_ISREG(info.st_mode)
            or item.path.is_symlink()
            or (hasattr(item.path, "is_junction") and item.path.is_junction())
            or attributes & reparse
            or item.path.resolve() != item.path
        ):
            raise LocalRunRejected("artifact_set_invalid")
        size = info.st_size
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


def _error_status(error: BaseException) -> int | None:
    if isinstance(error, HTTPError):
        return error.code
    status = getattr(error, "status", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _retryable_network_error(error: BaseException) -> bool:
    status = _error_status(error)
    if status is not None:
        return status in {408, 425, 429} or status >= 500
    if isinstance(error, LocalIngestApiError):
        return str(error) == "ARTOKE API request failed"
    return isinstance(error, (URLError, TimeoutError, HTTPException))


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
        self._state_lock = threading.Lock()
        self._publication_acknowledged = False
        self._pending_job_id: str | None = None

    def cancel(self) -> None:
        with self._state_lock:
            if self._publication_acknowledged:
                return
            self._cancelled.set()
        self._pipeline.cancel()

    def _acquire_run_locks(self) -> WorkspaceProcessLock:
        if not _LOCAL_RUN_LOCK.acquire(blocking=False):
            raise LocalRunRejected("local_job_in_progress")
        process_lock = WorkspaceProcessLock(self._session.workspace.root)
        try:
            if not process_lock.acquire():
                raise LocalRunRejected("local_job_in_progress")
            return process_lock
        except BaseException:
            _LOCAL_RUN_LOCK.release()
            raise

    @staticmethod
    def _release_run_locks(process_lock: WorkspaceProcessLock) -> None:
        process_lock.release()
        _LOCAL_RUN_LOCK.release()

    def run(self, name: str) -> LocalRunResult:
        process_lock = self._acquire_run_locks()
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
                self._upload_source(job_id, source, video.content_type)
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
                    self._upload_artifacts(job_id, artifacts, frozen)
                except LocalRunRejected:
                    raise LocalRunRejected("publication_failed") from None
                except _NETWORK_ERRORS as exc:
                    if _retryable_network_error(exc):
                        self._write_retry(job_id, frozen, "upload_pending")
                        self._pending_job_id = job_id
                        retained = True
                        return LocalRunResult("publication_pending", job_id)
                    raise LocalRunRejected("publication_failed") from None
                self._write_retry(job_id, frozen, "publish_indeterminate")
                try:
                    published = self._retry(lambda: self._api.publish(job_id, 0, frozen))
                except _NETWORK_ERRORS as exc:
                    if _retryable_network_error(exc):
                        self._pending_job_id = job_id
                        retained = True
                        return LocalRunResult("publication_pending", job_id)
                    raise LocalRunRejected("publication_failed") from None
                if published.job_id != job_id or published.status != "completed":
                    raise LocalRunRejected("publication_failed")
                with self._state_lock:
                    self._publication_acknowledged = True

            return self._finalize_acknowledged(job_id)
        except PipelineCancelled:
            if job_id is not None:
                try:
                    self._api.finish_cancelled(job_id)
                except (LocalIngestApiError, HTTPError, URLError, TimeoutError, HTTPException):
                    pass
            cleanup = self._session.cancel()
            if cleanup.state == "cleanup_required" or not cleanup.cleaned:
                return LocalRunResult("cleanup_required", job_id, cleanup_required=True)
            return LocalRunResult("cancelled", job_id)
        except (ProbeRejected, UploadRejected) as exc:
            if self._fail_and_clean(job_id, exc.code):
                raise LocalRunRejected("cleanup_required") from None
            raise LocalRunRejected(exc.code) from None
        except LocalRunRejected as exc:
            if job_id is not None and not retained:
                terminal_code = (
                    "publication_failed"
                    if exc.code == "publication_failed"
                    else "local_processing_failed"
                )
                if self._fail_and_clean(job_id, terminal_code):
                    raise LocalRunRejected("cleanup_required") from None
            raise
        except (RuntimeError, ValueError, OSError):
            if self._fail_and_clean(job_id, "local_processing_failed"):
                raise LocalRunRejected("cleanup_required") from None
            raise LocalRunRejected("local_processing_failed") from None
        finally:
            workspace.release()
            self._release_run_locks(process_lock)

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

    def _upload_source(self, job_id: str, source: Path, content_type: str) -> None:
        """Copy the source video to ARTOKE so the editor can overlay it.

        The server keeps it for 24 hours after publication.
        """
        try:
            upload = self._retry(lambda: self._api.authorize_source_upload(job_id))
        except _NETWORK_ERRORS:
            raise LocalRunRejected("source_upload_failed") from None
        try:
            self._retry(lambda: self._signed_uploader(upload.upload_url, source, content_type))
            self._retry(lambda: self._api.complete_source_upload(job_id))
        except _NETWORK_ERRORS:
            raise LocalRunRejected("source_upload_failed") from None

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
        with self._state_lock:
            acknowledged = self._publication_acknowledged
        if self._cancelled.is_set() and not acknowledged:
            raise PipelineCancelled()

    def _retry(self, operation: Callable[[], Any]) -> Any:
        for attempt in range(3):
            self._ensure_not_cancelled()
            try:
                return operation()
            except _NETWORK_ERRORS as exc:
                if not _retryable_network_error(exc) or attempt == 2:
                    raise
                self._sleeper(0.1 * (attempt + 1))
        raise AssertionError("unreachable")

    def _upload_artifacts(
        self,
        job_id: str,
        artifacts: Sequence[LocalArtifact],
        frozen: tuple[dict[str, object], ...],
    ) -> None:
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

    def _write_retry(
        self,
        job_id: str,
        manifest: Sequence[Mapping[str, object]],
        phase: str,
    ) -> None:
        if phase not in {"upload_pending", "publish_indeterminate"}:
            raise LocalRunRejected("retry_manifest_invalid")
        payload = {
            "schema": "artoke.local.retry.v1",
            "phase": phase,
            "jobId": job_id,
            "editRevision": 0,
            "artifacts": list(manifest),
        }
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        target = self._session.workspace.path / "retry.json"
        temporary = self._session.workspace.path / f".{uuid4()}.retry.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        for optional in ("O_BINARY", "O_NOINHERIT", "O_NOFOLLOW"):
            flags |= int(getattr(os, optional, 0))
        descriptor: int | None = None
        try:
            descriptor = os.open(temporary, flags, 0o600)
            written = 0
            while written < len(encoded):
                count = os.write(descriptor, encoded[written:])
                if count <= 0:
                    raise OSError
                written += count
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary, target)
            if os.name != "nt":
                directory = os.open(self._session.workspace.path, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise LocalRunRejected("retry_manifest_invalid") from None

    def retry_publication(self) -> LocalRunResult:
        """Retry within the live scoped API session; credentials are never persisted."""
        process_lock = self._acquire_run_locks()
        workspace = self._session.workspace
        job_id = self._pending_job_id
        try:
            workspace.acquire()
            if job_id is None:
                raise LocalRunRejected("retry_unavailable")
            artifacts, frozen, phase = self._read_retry(job_id)
            if phase == "upload_pending":
                try:
                    self._upload_artifacts(job_id, artifacts, frozen)
                except _NETWORK_ERRORS as exc:
                    if _retryable_network_error(exc):
                        return LocalRunResult("publication_pending", job_id)
                    raise LocalRunRejected("publication_failed") from None
                self._write_retry(job_id, frozen, "publish_indeterminate")
            try:
                published = self._retry(lambda: self._api.publish(job_id, 0, frozen))
            except _NETWORK_ERRORS as exc:
                if _retryable_network_error(exc):
                    return LocalRunResult("publication_pending", job_id)
                if phase == "publish_indeterminate":
                    return LocalRunResult("publication_conflict", job_id)
                raise LocalRunRejected("publication_failed") from None
            if published.job_id != job_id or published.status != "completed":
                if phase == "publish_indeterminate":
                    return LocalRunResult("publication_conflict", job_id)
                raise LocalRunRejected("publication_failed")
            with self._state_lock:
                self._publication_acknowledged = True
            self._pending_job_id = None
            return self._finalize_acknowledged(job_id)
        except LocalRunRejected as exc:
            if job_id is not None and exc.code != "retry_unavailable":
                if self._fail_and_clean(job_id, "publication_failed"):
                    raise LocalRunRejected("cleanup_required") from None
            if exc.code in {"retry_manifest_invalid", "retry_unavailable"}:
                raise
            raise LocalRunRejected("publication_failed") from None
        finally:
            workspace.release()
            self._release_run_locks(process_lock)

    def _read_retry(
        self, job_id: str
    ) -> tuple[
        tuple[LocalArtifact, ...],
        tuple[dict[str, object], ...],
        str,
    ]:
        target = self._session.workspace.path / "retry.json"
        try:
            info = target.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size > 64 * 1024
                or target.is_symlink()
                or target.resolve() != target
            ):
                raise ValueError
            body = json.loads(target.read_bytes())
            if not isinstance(body, dict) or set(body) != {
                "schema", "phase", "jobId", "editRevision", "artifacts"
            }:
                raise ValueError
            if body["schema"] != "artoke.local.retry.v1" or body["editRevision"] != 0:
                raise ValueError
            phase = body["phase"]
            if phase not in {"upload_pending", "publish_indeterminate"}:
                raise ValueError
            if not isinstance(body["jobId"], str):
                raise ValueError
            parsed_id = UUID(body["jobId"])
            if str(parsed_id) != job_id or body["jobId"] != job_id:
                raise ValueError
            manifest = body["artifacts"]
            if not isinstance(manifest, list) or len(manifest) != 4:
                raise ValueError
            expected: dict[str, dict[str, object]] = {}
            for raw in manifest:
                if not isinstance(raw, dict) or set(raw) != {"kind", "sizeBytes", "sha256", "formatVersion"}:
                    raise ValueError
                kind = raw["kind"]
                if kind not in _ARTIFACT_NAMES or kind in expected:
                    raise ValueError
                if (
                    not isinstance(raw["sizeBytes"], int)
                    or isinstance(raw["sizeBytes"], bool)
                    or raw["sizeBytes"] <= 0
                    or not isinstance(raw["sha256"], str)
                    or len(raw["sha256"]) != 64
                    or any(character not in "0123456789abcdef" for character in raw["sha256"])
                    or not isinstance(raw["formatVersion"], str)
                    or not 1 <= len(raw["formatVersion"]) <= 40
                ):
                    raise ValueError
                expected[kind] = raw
            if set(expected) != set(_ARTIFACT_NAMES):
                raise ValueError
            artifact_dir = self._session.workspace.path / "artifacts"
            if artifact_dir.resolve() != artifact_dir or artifact_dir.parent != self._session.workspace.path:
                raise ValueError
            artifacts = tuple(
                LocalArtifact(
                    kind,
                    artifact_dir / _ARTIFACT_NAMES[kind],
                    expected[kind]["sizeBytes"],
                    expected[kind]["sha256"],
                    expected[kind]["formatVersion"],
                )
                for kind in _ARTIFACT_NAMES
            )
            frozen = _manifest(artifacts)
            if tuple(expected[item.kind] for item in artifacts) != frozen:
                raise ValueError
            return artifacts, frozen, phase
        except (
            OSError,
            ValueError,
            TypeError,
            AttributeError,
            json.JSONDecodeError,
            LocalRunRejected,
        ):
            raise LocalRunRejected("retry_manifest_invalid") from None

    def _finalize_acknowledged(self, job_id: str) -> LocalRunResult:
        cleanup = self._session.close()
        if cleanup.state != "closed" or not cleanup.cleaned:
            return LocalRunResult("cleanup_required", job_id, cleanup_required=True)
        try:
            acknowledged = self._retry(lambda: self._api.acknowledge_cleanup(job_id))
        except _NETWORK_ERRORS:
            return LocalRunResult("cleanup_required", job_id, cleanup_required=True)
        if not acknowledged:
            return LocalRunResult("cleanup_required", job_id, cleanup_required=True)
        return LocalRunResult("completed", job_id)

    def _fail_and_clean(self, job_id: str | None, code: str) -> bool:
        if job_id is not None:
            try:
                self._api.finish_failed(job_id, code if code.replace("_", "").isalnum() else "local_processing_failed")
            except _NETWORK_ERRORS:
                pass
        cleanup = self._session.close()
        return cleanup.state != "closed" or not cleanup.cleaned
