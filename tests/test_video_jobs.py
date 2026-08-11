import json
from pathlib import Path

import pytest

from maxmcp.rtmw3d.runtime import Rtmw3dReadiness
from maxmcp.ui.studio.video_jobs import VideoJobController


def _readiness(root: Path, ready: bool) -> Rtmw3dReadiness:
    return Rtmw3dReadiness(
        ready, root / "env", root / "mmpose", root / "models",
        () if ready else ("rtmw3d-l checkpoint",),
    )


def test_job_blocks_when_rtmw3d_is_missing(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    controller = VideoJobController(readiness=lambda: _readiness(tmp_path, False))

    job = controller.start({"video": str(video), "library": str(tmp_path / "library")})

    assert job["status"] == "blocked"
    assert job["stage"] == "sdk_check"
    assert "rtmw3d-l checkpoint" in job["missing_files"]


def test_rejects_unsupported_video_extension(tmp_path: Path) -> None:
    video = tmp_path / "clip.txt"
    video.write_text("x", encoding="utf-8")
    controller = VideoJobController(readiness=lambda: _readiness(tmp_path, True))

    with pytest.raises(ValueError, match="지원하지 않는 영상"):
        controller.start({"video": str(video), "library": str(tmp_path)})


def test_completed_job_writes_bvh_and_trace(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    library = tmp_path / "library"

    class FinishedProcess:
        returncode = 0

        def communicate(self):
            body = {
                    "schema": "artoke.rtmw3d.v1",
                "source_video": str(video),
                "fps": 30,
                "reference_pose": {},
                "frames": [],
            }
            Path(command[-1]).write_text(json.dumps(body), encoding="utf-8")
            return "", ""

        def terminate(self):
            pass

    command: list[str] = []

    def process_factory(args, **_kwargs):
        command[:] = args
        return FinishedProcess()

    def converter(source: Path, target: Path) -> int:
        assert source.name == "clip_rtmw3d.json"
        target.write_text("HIERARCHY\nMOTION\n", encoding="utf-8")
        return 12

    controller = VideoJobController(
        readiness=lambda: _readiness(tmp_path, True),
        process_factory=process_factory,
        converter=converter,
    )
    started = controller.start({"video": str(video), "library": str(library)})
    controller.wait(2)
    job = controller.status(started["id"])

    assert job["status"] == "complete"
    assert Path(job["bvh_path"]).name == "clip_rtmw3d_tpose.bvh"
    assert Path(job["trace_path"]).is_file()
    assert job["frame_count"] == 12
    trace = json.loads(Path(job["trace_path"]).read_text(encoding="utf-8"))
    assert trace["backend"] == "OpenMMLab RTMW3D-L"


def test_second_running_job_is_rejected(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")

    class WaitingProcess:
        returncode = None

        def communicate(self):
            gate.wait(2)
            self.returncode = 1
            return "", "stopped"

        def terminate(self):
            gate.set()

    import threading
    gate = threading.Event()
    controller = VideoJobController(
        readiness=lambda: _readiness(tmp_path, True),
        process_factory=lambda *_args, **_kwargs: WaitingProcess(),
    )
    payload = {"video": str(video), "library": str(tmp_path / "library")}
    controller.start(payload)

    with pytest.raises(RuntimeError, match="실행 중"):
        controller.start(payload)
    controller.cancel(controller.current_id)
