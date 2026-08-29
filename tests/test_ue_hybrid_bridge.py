"""언리얼 자세를 얹는 다리의 회귀 시험.

다리가 지켜야 하는 것은 둘이다. 될 때는 네 단계를 정해진 순서로 밟고, 안 될 때는
워커를 멈추지 않고 원래 RTMW3D 결과로 물러난다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from maxmcp.worker.ue_hybrid_bridge import (
    UeHybridReadiness,
    apply_ue_hybrid,
    check_ue_hybrid,
)


def _tracking(path: Path, frames: int = 3, fps: float = 30.0) -> Path:
    path.write_text(
        json.dumps({
            "schema": "artoke.rtmw3d.v1",
            "fps": fps,
            "frames": [{"index": i} for i in range(frames)],
        }),
        encoding="utf-8",
    )
    return path


def _installed(root: Path) -> UeHybridReadiness:
    """네 조각이 모두 있는 설치를 흉내낸다."""
    editor = root / "UE" / "UnrealEditor-Cmd.exe"
    project = root / "UE" / "Probe.uproject"
    market = root / "market"
    python = root / "venv" / "python.exe"
    crop = root / "mcp" / "crop-subject.py"
    for path in (editor, project, python, crop):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    (market / "scripts").mkdir(parents=True, exist_ok=True)
    for name in ("ue-process-footage.py", "ue-export-performance.py", "ue-hybrid-merge.mts"):
        (market / "scripts" / name).write_text("x", encoding="utf-8")
    return check_ue_hybrid(editor, project, market, python, crop)


class Recorder:
    """명령을 기록하고, 미리 정해 둔 결과를 돌려준다."""

    def __init__(self, fail_at: int | None = None, writes: dict[int, str] | None = None):
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self._fail_at = fail_at
        self._writes = writes or {}

    def __call__(self, command, env=None, **_options):
        index = len(self.calls)
        self.calls.append(list(command))
        self.envs.append(dict(env or {}))
        target = self._writes.get(index)
        if target:
            Path(target).write_text("{}", encoding="utf-8")
        code = 1 if index == self._fail_at else 0
        return code, "", "boom" if code else ""


def test_readiness_lists_every_missing_piece(tmp_path: Path) -> None:
    readiness = check_ue_hybrid(
        tmp_path / "nope.exe",
        tmp_path / "nope.uproject",
        tmp_path / "nomarket",
        tmp_path / "nopython.exe",
        tmp_path / "nocrop.py",
    )
    assert not readiness.ready
    # 무엇이 없는지 사람이 읽을 수 있어야 한다 — 설치 안내가 이 목록으로 나간다
    assert "UnrealEditor-Cmd" in " ".join(readiness.missing)
    assert len(readiness.missing) >= 4


def test_readiness_is_ready_when_everything_is_installed(tmp_path: Path) -> None:
    readiness = _installed(tmp_path)
    assert readiness.ready
    assert readiness.missing == ()


def test_runs_four_steps_in_order_and_returns_the_merged_file(tmp_path: Path) -> None:
    readiness = _installed(tmp_path)
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"v")
    body = _tracking(tmp_path / "walk_rtmw3d.json", frames=189, fps=30.0)
    workspace = tmp_path / "job"
    workspace.mkdir()
    hybrid = workspace / "walk_ue_hybrid.json"
    # 3번째(내보내기)가 performance.json 을, 4번째(병합)가 결과를 쓴다
    runner = Recorder(writes={
        2: str(workspace / "walk_performance.json"),
        3: str(hybrid),
    })
    stages: list[str] = []

    result = apply_ue_hybrid(
        video, body, workspace, readiness,
        lambda stage, _progress: stages.append(stage),
        lambda: False,
        runner=runner,
    )

    assert result == hybrid
    assert len(runner.calls) == 4
    assert "crop-subject.py" in " ".join(runner.calls[0])
    assert "ue-process-footage.py" in " ".join(runner.calls[1])
    assert "ue-export-performance.py" in " ".join(runner.calls[2])
    assert "ue-hybrid-merge.mts" in " ".join(runner.calls[3])
    assert stages  # 오래 걸리는 작업이라 진행을 알려야 한다


def test_processing_needs_commandlet_rendering_and_dx12(tmp_path: Path) -> None:
    """-nullrhi 로는 D3D12 를 못 잡아 파이프라인이 DISABLED 로 꺼진다(2026-08-21 실측).

    반대로 읽기만 하는 내보내기는 -nullrhi 로 충분하고, 그래야 빠르다.
    """
    readiness = _installed(tmp_path)
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"v")
    body = _tracking(tmp_path / "walk_rtmw3d.json")
    workspace = tmp_path / "job"
    workspace.mkdir()
    runner = Recorder(writes={2: str(workspace / "walk_performance.json"),
                              3: str(workspace / "walk_ue_hybrid.json")})

    apply_ue_hybrid(video, body, workspace, readiness,
                    lambda *_: None, lambda: False, runner=runner)

    process = " ".join(runner.calls[1])
    assert "-AllowCommandletRendering" in process
    assert "-dx12" in process
    assert "-nullrhi" not in process
    assert "-nullrhi" in " ".join(runner.calls[2])


def test_processing_range_is_half_open_over_the_real_frame_count(tmp_path: Path) -> None:
    """`상한 - 하한` 이 실제 프레임 수와 같아야 한다. 어긋나면 다 계산하고 버린다."""
    readiness = _installed(tmp_path)
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"v")
    body = _tracking(tmp_path / "walk_rtmw3d.json", frames=189, fps=30.0)
    workspace = tmp_path / "job"
    workspace.mkdir()
    runner = Recorder(writes={2: str(workspace / "walk_performance.json"),
                              3: str(workspace / "walk_ue_hybrid.json")})

    apply_ue_hybrid(video, body, workspace, readiness,
                    lambda *_: None, lambda: False, runner=runner)

    env = runner.envs[1]
    assert env["ARTOKE_UE_FRAME_COUNT"] == "189"
    assert env["ARTOKE_UE_FPS"] == "30"


def test_falls_back_to_rtmw3d_when_a_step_fails(tmp_path: Path) -> None:
    readiness = _installed(tmp_path)
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"v")
    body = _tracking(tmp_path / "walk_rtmw3d.json")
    workspace = tmp_path / "job"
    workspace.mkdir()
    runner = Recorder(fail_at=1)

    result = apply_ue_hybrid(video, body, workspace, readiness,
                             lambda *_: None, lambda: False, runner=runner)

    assert result is None
    # 실패해도 워커는 멈추지 않는다. 다만 왜 물러났는지는 남긴다
    report = json.loads((workspace / "walk_ue_hybrid.report.json").read_text(encoding="utf-8"))
    assert report["applied"] is False
    assert "ue-process-footage" in report["failed_step"]
    assert report["reason"]


def test_falls_back_when_merge_produces_nothing(tmp_path: Path) -> None:
    """반환 코드가 0 이어도 파일이 없으면 성공이 아니다."""
    readiness = _installed(tmp_path)
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"v")
    body = _tracking(tmp_path / "walk_rtmw3d.json")
    workspace = tmp_path / "job"
    workspace.mkdir()
    runner = Recorder(writes={2: str(workspace / "walk_performance.json")})

    result = apply_ue_hybrid(video, body, workspace, readiness,
                             lambda *_: None, lambda: False, runner=runner)

    assert result is None


def test_does_nothing_when_unreal_is_not_installed(tmp_path: Path) -> None:
    readiness = check_ue_hybrid(
        tmp_path / "a.exe", tmp_path / "b.uproject", tmp_path / "c",
        tmp_path / "d.exe", tmp_path / "e.py",
    )
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"v")
    body = _tracking(tmp_path / "walk_rtmw3d.json")
    workspace = tmp_path / "job"
    workspace.mkdir()

    def forbidden(*_args, **_options):
        raise AssertionError("언리얼이 없으면 아무 명령도 돌리지 않는다")

    assert apply_ue_hybrid(video, body, workspace, readiness,
                           lambda *_: None, lambda: False, runner=forbidden) is None


def test_stops_between_steps_when_cancelled(tmp_path: Path) -> None:
    """언리얼 처리는 10분이 넘는다. 취소를 늦게 보면 그만큼 헛돈다."""
    readiness = _installed(tmp_path)
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"v")
    body = _tracking(tmp_path / "walk_rtmw3d.json")
    workspace = tmp_path / "job"
    workspace.mkdir()
    runner = Recorder()

    result = apply_ue_hybrid(video, body, workspace, readiness,
                             lambda *_: None, lambda: True, runner=runner)

    assert result is None
    assert runner.calls == []
