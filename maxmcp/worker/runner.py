"""Single-GPU ARTOKE worker orchestration."""

from __future__ import annotations

from enum import Enum
import json
from pathlib import Path
import sys
import threading
import traceback
from typing import Any, Callable

from maxmcp.worker.kimodo_pipeline import KimodoPipeline, is_prompt_source
from maxmcp.worker.postprocess_bridge import convert_with_postprocess
from maxmcp.rtmw3d.runtime import Rtmw3dReadiness, default_readiness
from maxmcp.worker.api_client import ArtokeApiClient, UploadTarget, WorkerApiError
from maxmcp.worker.artifacts import (
    LocalArtifact,
    build_artifacts,
    download_source,
    upload_signed_artifact,
)
from maxmcp.worker.motion_pipeline import MotionPipeline, PipelineArtifacts, PipelineCancelled
from maxmcp.worker.tracking_corrections import apply_tracking_corrections, read_subject_box
from maxmcp.worker.workspace import JobWorkspace, cleanup_stale


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
        correction_applier: Callable[[Path, Path, Path], Path] = apply_tracking_corrections,
        converter: Callable[[Path, Path], int] = convert_with_postprocess,
        heartbeat_interval: float = 20.0,
    ) -> None:
        self._api = api
        self._readiness = readiness
        self._cache_root = cache_root
        self._pipeline_factory = pipeline_factory
        self._downloader = downloader
        self._artifact_builder = artifact_builder
        self._uploader = uploader
        self._correction_applier = correction_applier
        self._converter = converter
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
            or (
                Path(claim.source_filename).suffix.lower() not in VIDEO_EXTENSIONS
                # 프롬프트 작업(.kimodo.json)도 정당한 소스다 — 이 문지기가 영상만
                # 알던 시절의 잔재로 첫 프롬프트 작업을 즉시 거부했다(08-24 실측).
                and not is_prompt_source(claim.source_filename)
            )
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
        pipeline: Any | None = None

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
                    if pipeline is not None:
                        pipeline.cancel()
                    return
                if response.cancel_requested:
                    cancelled.set()
                    if pipeline is not None:
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
                if not claim.download_url:
                    # 로컬 앱으로 넣은 작업은 서버에 원본이 없을 수 있다. 관절 교정만이면
                    # 원본 없이도 되지만, 주인공 재추출은 원본이 있어야 한다(08-26 실측:
                    # downloadUrl=null 인 채로 같은 작업을 5초마다 다시 잡는 고리에 빠졌다).
                    if claim.edit_revision <= 0:
                        raise ValueError("claim has no source download url")
                else:
                    self._downloader(claim.download_url, source)
                if cancelled.is_set():
                    raise PipelineCancelled()
                if claim.edit_revision > 0:
                    phase = "correction_download_failed"
                    if claim.tracking_url is None or claim.edits_url is None:
                        raise ValueError("correction claim is incomplete")
                    original_tracking = workspace.path / "original.rtmw3d.json"
                    edits = workspace.path / "tracking.edits.json"
                    self._downloader(claim.tracking_url, original_tracking)
                    self._downloader(claim.edits_url, edits)
                    if cancelled.is_set():
                        raise PipelineCancelled()

                    subject = read_subject_box(edits)
                    if subject is not None and not claim.download_url:
                        phase = "source_unavailable"
                        raise ValueError("subject re-extraction needs the source video")
                    if subject is not None:
                        # 주인공 다시 지정: 관절 교정이 아니라 그 상자를 씨앗으로
                        # 원본 영상에서 처음부터 다시 추출한다(2026-08-25, 궁수→기수 갈아탐).
                        phase = "pipeline_failed"
                        seed_frame, (x1, y1, x2, y2) = subject
                        pipeline = self._pipeline_factory(report)
                        pipeline_result = pipeline.run(
                            source,
                            workspace.path,
                            update_stage,
                            cancelled.is_set,
                            extra_args=("--seed", f"{seed_frame}:{x1:.1f},{y1:.1f},{x2:.1f},{y2:.1f}"),
                        )
                    else:
                        phase = "correction_failed"
                        update_stage("converting", 65)
                        corrected = workspace.path / "corrected.rtmw3d.json"
                        self._correction_applier(original_tracking, edits, corrected)
                        bvh = workspace.path / "corrected.bvh"
                        frame_count = self._converter(corrected, bvh)
                        trace = workspace.path / "corrected.trace.json"
                        trace.write_text(json.dumps({
                            "backend": "OpenMMLab RTMW3D-L",
                            "editRevision": claim.edit_revision,
                        }), encoding="utf-8")
                        pipeline_result = PipelineArtifacts(
                            corrected,
                            bvh,
                            trace,
                            frame_count,
                        )
                else:
                    phase = "pipeline_failed"
                    # 문장 입력 작업은 트래킹이 아니라 Kimodo 생성으로 간다.
                    # 겉모양(PipelineArtifacts)이 같아 게시 쪽은 갈래를 모른다.
                    pipeline = (
                        KimodoPipeline()
                        if is_prompt_source(claim.source_filename)
                        else self._pipeline_factory(report)
                    )
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
                    edit_revision=claim.edit_revision,
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
                self._api.publish(claim.job_id, manifest, claim.edit_revision)
                return RunResult.COMPLETED
        except PipelineCancelled:
            if lease_lost.is_set() or heartbeat_failed.is_set():
                return RunResult.LEASE_LOST
            self._api.finish_cancelled(claim.job_id)
            return RunResult.CANCELLED
        except WorkerApiError as exc:
            if exc.status == 409:
                return RunResult.LEASE_LOST
            print(f"[artoke-worker] job {claim.job_id} failed at {phase}: {exc}", file=sys.stderr)
            self._api.finish_failed(claim.job_id, phase)
            return RunResult.FAILED
        except Exception:
            if lease_lost.is_set():
                return RunResult.LEASE_LOST
            # 원인을 남긴다 — 예전엔 조용히 삼켜서 "downloading 에서 멈춤"만 보였다(08-26).
            print(f"[artoke-worker] job {claim.job_id} failed at {phase}:", file=sys.stderr)
            traceback.print_exc()
            self._api.finish_failed(claim.job_id, phase)
            return RunResult.FAILED
        finally:
            stop_heartbeat.set()
            if pipeline is not None:
                pipeline.cancel()
            heartbeat.join(2)
