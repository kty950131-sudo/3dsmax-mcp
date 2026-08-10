"""Single-GPU ARTOKE worker orchestration."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
import threading
from typing import Any, Callable

from src.rtmw3d.runtime import Rtmw3dReadiness, default_readiness
from src.worker.api_client import ArtokeApiClient, UploadTarget, WorkerApiError
from src.worker.artifacts import (
    LocalArtifact,
    build_artifacts,
    download_source,
    upload_signed_artifact,
)
from src.worker.motion_pipeline import MotionPipeline, PipelineCancelled
from src.worker.workspace import JobWorkspace, cleanup_stale


class RunResult(Enum):
    IDLE = "idle"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    LEASE_LOST = "lease_lost"


CONTENT_TYPES = {
    "bvh": "application/octet-stream",
    "rtmw3d_json": "application/json",
    "thumbnail": "image/webp",
    "metadata": "application/json",
}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi"}


def _upload(target: UploadTarget, artifact: LocalArtifact) -> None:
    upload_signed_artifact(
        target.signed_url,
        artifact.path,
        CONTENT_TYPES[artifact.kind],
    )


class ArtokeWorker:
    def __init__(
        self,
        api: ArtokeApiClient,
        readiness: Callable[[], Rtmw3dReadiness] = default_readiness,
        cache_root: Path = Path.home() / "AppData" / "Local" / "ARTOKE" / "worker",
        *,
        pipeline_factory: Callable[[Rtmw3dReadiness], Any] = MotionPipeline,
        downloader: Callable[..., Path] = download_source,
        artifact_builder: Callable[..., tuple[LocalArtifact, ...]] = build_artifacts,
        uploader: Callable[[UploadTarget, LocalArtifact], None] = _upload,
        heartbeat_interval: float = 20.0,
    ) -> None:
        self._api = api
        self._readiness = readiness
        self._cache_root = cache_root
        self._pipeline_factory = pipeline_factory
        self._downloader = downloader
        self._artifact_builder = artifact_builder
        self._uploader = uploader
        self._heartbeat_interval = heartbeat_interval

    def run_forever(self, stop_event: threading.Event) -> None:
        cleanup_stale(self._cache_root)
        error_delay = 5.0
        while not stop_event.is_set():
            try:
                self.run_once()
                delay = 5.0
                error_delay = 5.0
            except WorkerApiError:
                delay = error_delay
                error_delay = min(error_delay * 2, 30.0)
            if stop_event.wait(delay):
                return

    def run_once(self) -> RunResult:
        report = self._readiness()
        if not report.ready:
            return RunResult.BLOCKED
        claim = self._api.claim()
        if claim is None:
            return RunResult.IDLE
        if (
            Path(claim.source_filename).name != claim.source_filename
            or Path(claim.source_filename).suffix.lower() not in VIDEO_EXTENSIONS
        ):
            self._api.finish_failed(claim.job_id, "invalid_source_filename")
            return RunResult.FAILED

        cancelled = threading.Event()
        lease_lost = threading.Event()
        heartbeat_failed = threading.Event()
        stop_heartbeat = threading.Event()
        state_lock = threading.Lock()
        stage = "downloading"
        progress = 0
        pipeline = self._pipeline_factory(report)

        def update_stage(next_stage: str, next_progress: int) -> None:
            nonlocal stage, progress
            with state_lock:
                stage, progress = next_stage, next_progress

        def heartbeat_loop() -> None:
            while not stop_heartbeat.wait(self._heartbeat_interval):
                with state_lock:
                    current = (stage, progress)
                try:
                    response = self._api.heartbeat(claim.job_id, *current)
                except WorkerApiError as exc:
                    if exc.status == 409:
                        lease_lost.set()
                    else:
                        heartbeat_failed.set()
                    cancelled.set()
                    pipeline.cancel()
                    return
                if response.cancel_requested:
                    cancelled.set()
                    pipeline.cancel()
                    return

        heartbeat = threading.Thread(
            target=heartbeat_loop,
            name="artoke-worker-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        phase = "source_download_failed"
        try:
            with JobWorkspace.open(self._cache_root, claim.job_id) as workspace:
                source = workspace.path / claim.source_filename
                self._downloader(claim.download_url, source)
                phase = "pipeline_failed"
                pipeline_result = pipeline.run(
                    source,
                    workspace.path,
                    update_stage,
                    cancelled.is_set,
                )
                if lease_lost.is_set():
                    return RunResult.LEASE_LOST
                if heartbeat_failed.is_set():
                    return RunResult.LEASE_LOST
                if cancelled.is_set():
                    raise PipelineCancelled()

                phase = "artifact_failed"
                update_stage("validating", 85)
                artifacts = self._artifact_builder(
                    source,
                    pipeline_result,
                    workspace.path / "result",
                    claim.duration_seconds,
                )
                targets = {item.kind: item for item in self._api.authorize_uploads(claim.job_id)}
                if set(targets) != {item.kind for item in artifacts}:
                    raise ValueError("upload target mismatch")

                phase = "upload_failed"
                update_stage("uploading", 90)
                manifest: list[dict[str, object]] = []
                for artifact in artifacts:
                    target = targets[artifact.kind]
                    self._uploader(target, artifact)
                    manifest.append({
                        "kind": artifact.kind,
                        "objectPath": target.object_path,
                        "sizeBytes": artifact.size_bytes,
                        "sha256": artifact.sha256,
                        "formatVersion": artifact.format_version,
                    })
                stop_heartbeat.set()
                heartbeat.join(2)
                if lease_lost.is_set():
                    return RunResult.LEASE_LOST
                if cancelled.is_set() or heartbeat_failed.is_set():
                    raise PipelineCancelled()
                self._api.publish(claim.job_id, manifest)
                return RunResult.COMPLETED
        except PipelineCancelled:
            if lease_lost.is_set() or heartbeat_failed.is_set():
                return RunResult.LEASE_LOST
            self._api.finish_cancelled(claim.job_id)
            return RunResult.CANCELLED
        except Exception:
            if lease_lost.is_set():
                return RunResult.LEASE_LOST
            self._api.finish_failed(claim.job_id, phase)
            return RunResult.FAILED
        finally:
            stop_heartbeat.set()
            pipeline.cancel()
            heartbeat.join(2)
