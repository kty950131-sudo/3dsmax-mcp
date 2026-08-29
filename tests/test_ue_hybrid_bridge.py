"""언리얼 자세를 얹는 다리의 회귀 시험.

다리가 지켜야 하는 것은 둘이다. 될 때는 네 단계를 정해진 순서로 밟고, 안 될 때는
워커를 멈추지 않고 원래 RTMW3D 결과로 물러난다.
"""

from __future__ import annotations

import json
import subprocess
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
        # 언리얼 처리 단계는 종료 코드가 아니라 이 표시 파일로 판정된다
        if "ue-process-footage.py" in " ".join(command) and index != self._fail_at:
            Path(env["ARTOKE_UE_DONE"]).write_text(
                json.dumps({"body": True, "frames": 189}), encoding="utf-8"
            )
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


def test_unreal_steps_carry_a_generous_deadline(tmp_path: Path) -> None:
    """언리얼이 멈추면 워커가 영영 붙잡힌다. 실행 중에는 취소도 못 본다.

    제한은 넉넉해야 한다 — 10분으로 감쌌다가 처리 도중에 끊긴 적이 있다
    (2026-08-21). 실측이 프레임당 3.3초이므로 그 열 배를 준다.
    """
    readiness = _installed(tmp_path)
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"v")
    body = _tracking(tmp_path / "walk_rtmw3d.json", frames=189, fps=30.0)
    workspace = tmp_path / "job"
    workspace.mkdir()
    timeouts: list[float | None] = []

    def runner(command, env=None, timeout=None, **_options):
        timeouts.append(timeout)
        index = len(timeouts) - 1
        if index == 2:
            (workspace / "walk_performance.json").write_text("{}", encoding="utf-8")
        if index == 3:
            (workspace / "walk_ue_hybrid.json").write_text("{}", encoding="utf-8")
        return 0, "", ""

    apply_ue_hybrid(video, body, workspace, readiness,
                    lambda *_: None, lambda: False, runner=runner)

    assert all(t and t > 0 for t in timeouts)
    # 189프레임 x 3.3초 x 10 = 6237초. 그보다 짧으면 정상 처리를 끊는다
    assert timeouts[1] >= 189 * 33
    # 아무리 짧은 영상이어도 최소 한 시간은 준다 (기동만 4~11분이다)
    assert min(timeouts) >= 3600


def test_a_timed_out_step_falls_back_instead_of_hanging(tmp_path: Path) -> None:
    readiness = _installed(tmp_path)
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"v")
    body = _tracking(tmp_path / "walk_rtmw3d.json")
    workspace = tmp_path / "job"
    workspace.mkdir()

    def runner(command, env=None, timeout=None, **_options):
        raise subprocess.TimeoutExpired(list(command), timeout or 0)

    result = apply_ue_hybrid(video, body, workspace, readiness,
                             lambda *_: None, lambda: False, runner=runner)

    assert result is None
    report = json.loads((workspace / "walk_ue_hybrid.report.json").read_text(encoding="utf-8"))
    assert report["applied"] is False
    assert "시간" in report["reason"]


def test_ingest_name_changes_when_the_footage_changes(tmp_path: Path) -> None:
    """ue-process-footage.py 는 같은 이름의 캡처 데이터가 있으면 재사용한다.

    이름이 영상 내용과 무관하면, 다른 영상을 넣고도 낡은 푸티지를 쓴다.
    """
    readiness = _installed(tmp_path)
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"v")
    workspace = tmp_path / "job"
    workspace.mkdir()

    def name_for(frames: int) -> str:
        body = _tracking(tmp_path / "walk_rtmw3d.json", frames=frames)
        runner = Recorder(writes={2: str(workspace / "walk_performance.json"),
                                  3: str(workspace / "walk_ue_hybrid.json")})
        apply_ue_hybrid(video, body, workspace, readiness,
                        lambda *_: None, lambda: False, runner=runner)
        return runner.envs[1]["ARTOKE_UE_INGEST_NAME"]

    first, second = name_for(10), name_for(11)
    assert first != second
    # 언리얼 애셋 이름이라 영문·숫자만 쓴다
    assert first.isalnum()


def test_ingest_name_is_stable_for_the_same_job(tmp_path: Path) -> None:
    """같은 작업을 다시 돌리면 이름이 같아야 한다 — 재사용이 그때는 이득이다."""
    readiness = _installed(tmp_path)
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"v")
    workspace = tmp_path / "job"
    workspace.mkdir()
    body = _tracking(tmp_path / "walk_rtmw3d.json", frames=10)

    def once() -> str:
        runner = Recorder(writes={2: str(workspace / "walk_performance.json"),
                                  3: str(workspace / "walk_ue_hybrid.json")})
        apply_ue_hybrid(video, body, workspace, readiness,
                        lambda *_: None, lambda: False, runner=runner)
        return runner.envs[1]["ARTOKE_UE_INGEST_NAME"]

    assert once() == once()


# 서버가 받는 단계 이름은 닫힌 목록이다
# (script-market 의 src/lib/motions/schemas.ts, workerHeartbeatSchema).
ALLOWED_STAGES = {"downloading", "extracting", "converting", "validating", "uploading"}


def test_reported_stages_are_names_the_server_accepts(tmp_path: Path) -> None:
    """모르는 이름을 보내면 서버가 400 을 주고, 하트비트가 깨지면 작업이 취소된다.

    runner.py 의 heartbeat_loop 는 409 가 아닌 오류에서 heartbeat_failed 를 세우고
    pipeline.cancel() 을 부른다. 즉 이름 하나 잘못 보내면 작업 전체가 죽는다.
    """
    readiness = _installed(tmp_path)
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"v")
    body = _tracking(tmp_path / "walk_rtmw3d.json", frames=189)
    workspace = tmp_path / "job"
    workspace.mkdir()
    runner = Recorder(writes={2: str(workspace / "walk_performance.json"),
                              3: str(workspace / "walk_ue_hybrid.json")})
    seen: list[tuple[str, int]] = []

    apply_ue_hybrid(video, body, workspace, readiness,
                    lambda stage, progress: seen.append((stage, progress)),
                    lambda: False, runner=runner)

    assert seen, "오래 걸리는 작업이므로 진행을 알려야 한다"
    unknown = {stage for stage, _ in seen} - ALLOWED_STAGES
    assert unknown == set(), f"서버가 모르는 단계 이름: {unknown}"


def test_reported_progress_only_moves_forward(tmp_path: Path) -> None:
    """되돌아가는 진행률은 멈춘 것처럼 보인다. 추출 15 와 변환 65 사이에 들어야 한다."""
    readiness = _installed(tmp_path)
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"v")
    body = _tracking(tmp_path / "walk_rtmw3d.json", frames=189)
    workspace = tmp_path / "job"
    workspace.mkdir()
    runner = Recorder(writes={2: str(workspace / "walk_performance.json"),
                              3: str(workspace / "walk_ue_hybrid.json")})
    seen: list[tuple[str, int]] = []

    apply_ue_hybrid(video, body, workspace, readiness,
                    lambda stage, progress: seen.append((stage, progress)),
                    lambda: False, runner=runner)

    values = [progress for _, progress in seen]
    assert values == sorted(values)
    assert min(values) > 15 and max(values) < 65


def test_unreal_success_is_judged_by_the_work_product_not_the_exit_code(tmp_path: Path) -> None:
    """언리얼 커맨드릿은 일을 다 하고도 0 이 아닌 코드를 낸다(2026-08-29 실측).

    189프레임을 끝까지 처리하고 'LogExit: Exiting.' 으로 정상 종료했는데도 코드가
    0 이 아니었다. 엔진 기본 콘텐츠의 셰이더 컴파일 오류가 코드를 오염시킨다.
    그래서 언리얼 두 단계는 코드가 아니라 남긴 결과물로 판정한다.
    """
    readiness = _installed(tmp_path)
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"v")
    body = _tracking(tmp_path / "walk_rtmw3d.json", frames=189)
    workspace = tmp_path / "job"
    workspace.mkdir()

    def runner(command, env=None, timeout=None, **_options):
        text = " ".join(command)
        if "ue-process-footage.py" in text:
            Path(env["ARTOKE_UE_DONE"]).write_text(
                json.dumps({"body": True, "frames": 189}), encoding="utf-8"
            )
            return 3, "", "shader warnings"     # 일은 했는데 코드가 더럽다
        if "ue-export-performance.py" in text:
            (workspace / "walk_performance.json").write_text("{}", encoding="utf-8")
            return 1, "", "shader warnings"
        if "ue-hybrid-merge.mts" in text:
            (workspace / "walk_ue_hybrid.json").write_text("{}", encoding="utf-8")
        return 0, "", ""

    result = apply_ue_hybrid(video, body, workspace, readiness,
                             lambda *_: None, lambda: False, runner=runner)

    assert result == workspace / "walk_ue_hybrid.json"


def test_unreal_failure_is_caught_when_it_leaves_nothing(tmp_path: Path) -> None:
    """코드를 안 보는 대신, 결과물이 없으면 확실히 실패로 본다."""
    readiness = _installed(tmp_path)
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"v")
    body = _tracking(tmp_path / "walk_rtmw3d.json")
    workspace = tmp_path / "job"
    workspace.mkdir()

    def runner(command, env=None, timeout=None, **_options):
        return 0, "", ""      # 코드는 0 인데 아무것도 안 남겼다

    result = apply_ue_hybrid(video, body, workspace, readiness,
                             lambda *_: None, lambda: False, runner=runner)

    assert result is None
    report = json.loads((workspace / "walk_ue_hybrid.report.json").read_text(encoding="utf-8"))
    assert "ue-process-footage" in report["failed_step"]


def test_unreal_reports_when_the_body_track_came_back_empty(tmp_path: Path) -> None:
    """표시 파일이 있어도 몸 데이터가 없으면 얹을 자세가 없다."""
    readiness = _installed(tmp_path)
    video = tmp_path / "walk.mp4"
    video.write_bytes(b"v")
    body = _tracking(tmp_path / "walk_rtmw3d.json")
    workspace = tmp_path / "job"
    workspace.mkdir()

    def runner(command, env=None, timeout=None, **_options):
        if "ue-process-footage.py" in " ".join(command):
            Path(env["ARTOKE_UE_DONE"]).write_text(
                json.dumps({"body": False, "frames": 0}), encoding="utf-8"
            )
        return 0, "", ""

    assert apply_ue_hybrid(video, body, workspace, readiness,
                           lambda *_: None, lambda: False, runner=runner) is None
    report = json.loads((workspace / "walk_ue_hybrid.report.json").read_text(encoding="utf-8"))
    assert "몸" in report["reason"]
