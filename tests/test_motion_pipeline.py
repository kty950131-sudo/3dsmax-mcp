import json
from pathlib import Path
import threading

import pytest

from maxmcp.rtmw3d.runtime import Rtmw3dReadiness
from maxmcp.worker.motion_pipeline import (
    MotionPipeline,
    PipelineCancelled,
)


def _readiness(root: Path) -> Rtmw3dReadiness:
    return Rtmw3dReadiness(True, root / "env", root / "mmpose", root / "models", ())


def test_pipeline_generates_json_bvh_and_trace_in_stage_order(tmp_path: Path) -> None:
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"video")
    stages: list[tuple[str, int]] = []

    class Process:
        returncode = 0

        def communicate(self):
            Path(command[-1]).write_text(
                json.dumps({"schema": "artoke.rtmw3d.v1", "fps": 30, "frames": []}),
                encoding="utf-8",
            )
            return "", ""

        def terminate(self):
            raise AssertionError("successful work must not be terminated")

    command: list[str] = []

    def process_factory(args, **_kwargs):
        command[:] = args
        return Process()

    def converter(_source: Path, target: Path) -> int:
        target.write_text("HIERARCHY\nMOTION\n", encoding="utf-8")
        return 12

    pipeline = MotionPipeline(
        _readiness(tmp_path),
        process_factory=process_factory,
        converter=converter,
    )

    result = pipeline.run(
        video,
        tmp_path / "job",
        lambda stage, progress: stages.append((stage, progress)),
        lambda: False,
    )

    assert stages == [("extracting", 15), ("converting", 65), ("validating", 85)]
    assert result.rtmw3d_json.is_file()
    assert result.bvh.is_file()
    assert result.trace.is_file()
    assert result.frame_count == 12
    trace = json.loads(result.trace.read_text(encoding="utf-8"))
    rendered = json.dumps(trace)
    assert str(video) not in rendered
    assert str(result.rtmw3d_json) not in rendered
    assert "command" not in trace
    assert "source_video" not in trace
    assert "video" not in trace.get("sha256", {})


def test_pipeline_stops_before_start_when_cancelled(tmp_path: Path) -> None:
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"video")
    pipeline = MotionPipeline(
        _readiness(tmp_path),
        process_factory=lambda *_args, **_kwargs: pytest.fail("must not start"),
    )

    with pytest.raises(PipelineCancelled):
        pipeline.run(video, tmp_path / "job", lambda *_: None, lambda: True)


def test_pipeline_terminates_active_extractor(tmp_path: Path) -> None:
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"video")
    started = threading.Event()
    released = threading.Event()

    class Process:
        returncode = -1

        def communicate(self):
            started.set()
            released.wait(2)
            return "", "cancelled"

        def terminate(self):
            released.set()

    pipeline = MotionPipeline(
        _readiness(tmp_path),
        process_factory=lambda *_args, **_kwargs: Process(),
    )
    errors: list[BaseException] = []

    def run() -> None:
        try:
            pipeline.run(video, tmp_path / "job", lambda *_: None, lambda: False)
        except BaseException as exc:  # captured for the worker thread assertion
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(1)
    pipeline.cancel()
    thread.join(2)

    assert len(errors) == 1
    assert isinstance(errors[0], PipelineCancelled)


def test_cancel_racing_process_publication_terminates_new_process(tmp_path: Path) -> None:
    video = tmp_path / "walk.mp4"; video.write_bytes(b"video")
    entered = threading.Event(); release = threading.Event(); terminated = threading.Event()
    class Process:
        returncode = -1
        def communicate(self): return "", "cancelled"
        def terminate(self): terminated.set()
    def factory(*_args, **_kwargs):
        entered.set(); release.wait(1); return Process()
    pipeline = MotionPipeline(_readiness(tmp_path), process_factory=factory)
    errors: list[BaseException] = []
    thread = threading.Thread(target=lambda: _capture_run(pipeline, video, tmp_path / "job", errors))
    thread.start(); assert entered.wait(1); pipeline.cancel(); release.set(); thread.join(2)
    assert terminated.is_set()
    assert isinstance(errors[0], PipelineCancelled)


def _capture_run(pipeline, video, workspace, errors):
    try: pipeline.run(video, workspace, lambda *_: None, lambda: False)
    except BaseException as exc: errors.append(exc)


def test_communicate_failure_terminates_and_waits_before_release(tmp_path: Path) -> None:
    video = tmp_path / "walk.mp4"; video.write_bytes(b"video")
    calls: list[str] = []
    class Process:
        returncode = -1
        def communicate(self): raise OSError("pipe failed")
        def terminate(self): calls.append("terminate")
        def wait(self): calls.append("wait")
    pipeline = MotionPipeline(_readiness(tmp_path), process_factory=lambda *_a, **_k: Process())
    with pytest.raises(OSError):
        pipeline.run(video, tmp_path / "job", lambda *_: None, lambda: False)
    assert calls == ["terminate", "wait"]


def test_pipeline_uses_the_unreal_pose_when_the_bridge_succeeds(tmp_path: Path) -> None:
    """언리얼이 있으면 변환기는 합친 파일을 읽어야 한다 — 그래야 뼈 길이가 산다."""
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"video")
    hybrid = tmp_path / "job" / "walk_ue_hybrid.json"
    seen: list[Path] = []

    class Process:
        returncode = 0

        def communicate(self):
            Path(command[-1]).write_text(
                json.dumps({"schema": "artoke.rtmw3d.v1", "fps": 30, "frames": []}),
                encoding="utf-8",
            )
            return "", ""

    command: list[str] = []

    def process_factory(args, **_kwargs):
        command[:] = args
        return Process()

    def converter(source: Path, target: Path) -> int:
        seen.append(source)
        target.write_text("HIERARCHY\nMOTION\n", encoding="utf-8")
        return 7

    def hybrid_bridge(_video, _body, workspace, _on_stage, _cancelled):
        workspace.mkdir(parents=True, exist_ok=True)
        hybrid.write_text("{}", encoding="utf-8")
        return hybrid

    pipeline = MotionPipeline(
        _readiness(tmp_path),
        process_factory=process_factory,
        converter=converter,
        hybrid=hybrid_bridge,
    )
    result = pipeline.run(video, tmp_path / "job", lambda *_: None, lambda: False)

    assert seen == [hybrid]
    trace = json.loads(result.trace.read_text(encoding="utf-8"))
    assert trace["pose_source"] == "unreal-hybrid"


def test_pipeline_falls_back_to_rtmw3d_when_the_bridge_declines(tmp_path: Path) -> None:
    """언리얼이 없거나 깨져도 워커는 끝까지 간다."""
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"video")
    seen: list[Path] = []

    class Process:
        returncode = 0

        def communicate(self):
            Path(command[-1]).write_text(
                json.dumps({"schema": "artoke.rtmw3d.v1", "fps": 30, "frames": []}),
                encoding="utf-8",
            )
            return "", ""

    command: list[str] = []

    def process_factory(args, **_kwargs):
        command[:] = args
        return Process()

    def converter(source: Path, target: Path) -> int:
        seen.append(source)
        target.write_text("HIERARCHY\nMOTION\n", encoding="utf-8")
        return 7

    pipeline = MotionPipeline(
        _readiness(tmp_path),
        process_factory=process_factory,
        converter=converter,
        hybrid=lambda *_args: None,
    )
    result = pipeline.run(video, tmp_path / "job", lambda *_: None, lambda: False)

    assert seen == [result.rtmw3d_json]
    trace = json.loads(result.trace.read_text(encoding="utf-8"))
    assert trace["pose_source"] == "rtmw3d"


def test_pipeline_survives_a_bridge_that_raises(tmp_path: Path) -> None:
    """다리가 터져도 파이프라인이 같이 죽으면 안 된다."""
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"video")

    class Process:
        returncode = 0

        def communicate(self):
            Path(command[-1]).write_text(
                json.dumps({"schema": "artoke.rtmw3d.v1", "fps": 30, "frames": []}),
                encoding="utf-8",
            )
            return "", ""

    command: list[str] = []

    def process_factory(args, **_kwargs):
        command[:] = args
        return Process()

    def boom(*_args):
        raise RuntimeError("언리얼이 터졌다")

    pipeline = MotionPipeline(
        _readiness(tmp_path),
        process_factory=process_factory,
        converter=lambda _s, t: (t.write_text("HIERARCHY\n", encoding="utf-8"), 3)[1],
        hybrid=boom,
    )
    result = pipeline.run(video, tmp_path / "job", lambda *_: None, lambda: False)

    trace = json.loads(result.trace.read_text(encoding="utf-8"))
    assert trace["pose_source"] == "rtmw3d"
