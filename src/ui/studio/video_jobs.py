"""Single-worker RTMW3D video-to-BVH jobs for BVH Studio."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import threading
import uuid
from typing import Any, Callable

from src.rtmw3d.motion import convert_rtmw3d_file
from src.rtmw3d.runtime import Rtmw3dReadiness, build_rtmw3d_command, default_readiness

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
TERMINAL_STATES = {"blocked", "failed", "cancelled", "complete"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class VideoJobController:
    """Own one extractor subprocess and expose lock-protected snapshots."""

    def __init__(
        self,
        readiness: Callable[[], Rtmw3dReadiness] = default_readiness,
        process_factory: Callable[..., Any] = subprocess.Popen,
        converter: Callable[[Path, Path], int] = convert_rtmw3d_file,
    ) -> None:
        self._readiness = readiness
        self._process_factory = process_factory
        self._converter = converter
        self._lock = threading.Lock()
        self._job: dict[str, Any] | None = None
        self._thread: threading.Thread | None = None
        self._process: Any = None

    @property
    def current_id(self) -> str:
        with self._lock:
            return "" if self._job is None else str(self._job["id"])

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        video = Path(str(payload.get("video", ""))).resolve()
        library = Path(str(payload.get("library", ""))).resolve()
        if video.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError("지원하지 않는 영상 형식입니다")
        if not video.is_file():
            raise ValueError(f"영상 파일을 찾을 수 없습니다: {video}")
        with self._lock:
            if self._job and self._job["status"] not in TERMINAL_STATES:
                raise RuntimeError("다른 영상 변환이 실행 중입니다")

        report = self._readiness()
        job: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "video": str(video),
            "library": str(library),
            "status": "queued" if report.ready else "blocked",
            "stage": "queued" if report.ready else "sdk_check",
            "progress": 0,
        }
        if not report.ready:
            job.update(
                environment=str(report.environment),
                repository=str(report.repository),
                missing_files=list(report.missing_files),
                help_path=str(Path(__file__).resolve().parents[3] / "docs" / "RTMW3D.md"),
            )
        with self._lock:
            self._job = job
        if report.ready:
            self._thread = threading.Thread(
                target=self._run, args=(report,), name="bvh-studio-video", daemon=True
            )
            self._thread.start()
        return self.status(job["id"])

    def status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if self._job is None or self._job["id"] != job_id:
                raise KeyError("영상 작업을 찾을 수 없습니다")
            return dict(self._job)

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if self._job is None or self._job["id"] != job_id:
                raise KeyError("영상 작업을 찾을 수 없습니다")
            if self._job["status"] in TERMINAL_STATES:
                return dict(self._job)
            self._job.update(status="cancelled", stage="cancelled")
            process = self._process
        if process is not None:
            process.terminate()
        return self.status(job_id)

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _set(self, **values: Any) -> bool:
        with self._lock:
            if self._job is None or self._job["status"] == "cancelled":
                return False
            self._job.update(values)
            return True

    def _run(self, report: Rtmw3dReadiness) -> None:
        try:
            with self._lock:
                assert self._job is not None
                video = Path(self._job["video"])
                library = Path(self._job["library"])
            library.mkdir(parents=True, exist_ok=True)
            body_path = library / f"{video.stem}_rtmw3d.json"
            bvh_path = library / f"{video.stem}_rtmw3d_tpose.bvh"
            trace_path = library / f"{video.stem}_rtmw3d_trace.json"
            command = build_rtmw3d_command(video, body_path, report)
            if not self._set(status="extracting", stage="extracting", progress=15):
                return
            process = self._process_factory(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with self._lock:
                self._process = process
            stdout, stderr = process.communicate()
            with self._lock:
                self._process = None
                cancelled = self._job is None or self._job["status"] == "cancelled"
            if cancelled:
                return
            if process.returncode != 0:
                raise RuntimeError((stderr or stdout or "RTMW3D extractor failed").strip())
            if not body_path.is_file():
                raise RuntimeError("RTMW3D 추출기가 관절 JSON을 만들지 않았습니다")
            if not self._set(status="converting", stage="converting", progress=65):
                return
            frame_count = self._converter(body_path, bvh_path)
            if not bvh_path.is_file():
                raise RuntimeError("BVH 변환 결과가 없습니다")
            self._set(status="validating", stage="validating", progress=85)
            trace = {
                "backend": "OpenMMLab RTMW3D-L",
                "source_video": str(video),
                "rtmw3d_json": str(body_path),
                "bvh": str(bvh_path),
                "frame_count": frame_count,
                "command": command,
                "sha256": {
                    "video": _sha256(video), "rtmw3d": _sha256(body_path), "bvh": _sha256(bvh_path)
                },
            }
            trace_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
            self._set(
                status="complete", stage="complete", progress=100,
                rtmw3d_path=str(body_path), bvh_path=str(bvh_path),
                trace_path=str(trace_path), frame_count=frame_count,
            )
        except Exception as exc:
            self._set(status="failed", stage="failed", error=f"{type(exc).__name__}: {exc}")
