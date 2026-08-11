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
