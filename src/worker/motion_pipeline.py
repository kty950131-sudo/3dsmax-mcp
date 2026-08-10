"""UI-independent RTMW3D video-to-BVH pipeline."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import subprocess
import threading
from typing import Any, Callable

from src.rtmw3d.motion import convert_rtmw3d_file
from src.rtmw3d.runtime import Rtmw3dReadiness, build_rtmw3d_command


class PipelineCancelled(RuntimeError):
    """Raised when the caller cancels pipeline work."""


@dataclass(frozen=True)
class PipelineArtifacts:
    rtmw3d_json: Path
    bvh: Path
    trace: Path
    frame_count: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class MotionPipeline:
    """Run one RTMW3D extractor process and convert its output to BVH."""

    def __init__(
        self,
        readiness: Rtmw3dReadiness,
        process_factory: Callable[..., Any] = subprocess.Popen,
        converter: Callable[[Path, Path], int] = convert_rtmw3d_file,
    ) -> None:
        self._readiness = readiness
        self._process_factory = process_factory
        self._converter = converter
        self._lock = threading.Lock()
        self._process: Any = None
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            process = self._process
        if process is not None:
            process.terminate()

    def run(
        self,
        video_path: Path,
        workspace: Path,
        on_stage: Callable[[str, int], None],
        cancelled: Callable[[], bool],
    ) -> PipelineArtifacts:
        if self._cancelled.is_set() or cancelled():
            raise PipelineCancelled()
        workspace.mkdir(parents=True, exist_ok=True)
        body_path = workspace / f"{video_path.stem}_rtmw3d.json"
        bvh_path = workspace / f"{video_path.stem}_rtmw3d_tpose.bvh"
        trace_path = workspace / f"{video_path.stem}_rtmw3d_trace.json"
        command = build_rtmw3d_command(video_path, body_path, self._readiness)

        on_stage("extracting", 15)
        process = self._process_factory(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with self._lock:
            self._process = process
        try:
            stdout, stderr = process.communicate()
        finally:
            with self._lock:
                self._process = None
        if self._cancelled.is_set() or cancelled():
            raise PipelineCancelled()
        if process.returncode != 0:
            raise RuntimeError((stderr or stdout or "RTMW3D extractor failed").strip())
        if not body_path.is_file():
            raise RuntimeError("RTMW3D 추출기가 관절 JSON을 만들지 않았습니다")

        on_stage("converting", 65)
        frame_count = self._converter(body_path, bvh_path)
        if self._cancelled.is_set() or cancelled():
            raise PipelineCancelled()
        if not bvh_path.is_file():
            raise RuntimeError("BVH 변환 결과가 없습니다")

        on_stage("validating", 85)
        trace = {
            "backend": "OpenMMLab RTMW3D-L",
            "source_video": str(video_path),
            "rtmw3d_json": str(body_path),
            "bvh": str(bvh_path),
            "frame_count": frame_count,
            "command": command,
            "sha256": {
                "video": _sha256(video_path),
                "rtmw3d": _sha256(body_path),
                "bvh": _sha256(bvh_path),
            },
        }
        trace_path.write_text(
            json.dumps(trace, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return PipelineArtifacts(body_path, bvh_path, trace_path, frame_count)
