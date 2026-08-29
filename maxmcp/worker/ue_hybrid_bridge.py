"""RTMW3D 결과에 언리얼 자세를 얹는 다리. MotionPipeline 의 추출과 변환 사이에 든다.

왜 얹는가. RTMW3D 는 프레임마다 3D 점 23개를 아무 제약 없이 찍는다. 골격도 인체
모델도 없으므로 깊이를 매 프레임 새로 찍고, 그래서 뼈 길이가 흔들린다 —
0817.mp4 에서 아래팔이 53%, 가장 안정적인 넓적다리도 14% 였다(2026-08-29 실측).
실사 캡처는 5% 이하다. 늘었다 줄었다 하는 팔다리가 "접힘"으로 보이는 것이다.

언리얼 MetaHuman 은 SMPL-X 고정 스켈레톤을 영상에 맞춰 넣으므로 그 흔들림이
구조적으로 없다. 대신 크롭한 영상을 먹이기 때문에 이동 궤적과 화면 좌표가 없다.
그래서 자세는 언리얼, 궤적과 화면 좌표는 RTMW3D 에서 가져온다. 합치는 방법과
좌표 정합의 근거는 script-market 의 src/lib/ue-hybrid-merge.ts 에 적혀 있다.

실패하면 원래 RTMW3D 결과로 물러난다. 후처리 다리(postprocess_bridge)와 같은
원칙이다. 언리얼이 없거나 처리가 깨져도 워커가 멈추는 것보다 원본이라도 나가는
편이 낫다. 물러난 사유는 보고 파일에 남겨 나중에 볼 수 있게 한다.

⚠️ 이 단계는 느리다. 193프레임 처리에 629초가 걸렸다(2026-08-21 실측, 약 3.3초/프레임).
켤지 말지는 부르는 쪽이 정한다.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Callable, Sequence

# 진행률은 추출(15) 과 변환(65) 사이를 쓴다.
_STAGE_PROGRESS = {
    "isolating": 25,
    "unreal-tracking": 35,
    "unreal-export": 55,
    "merging": 60,
}

Runner = Callable[..., tuple[int, str, str]]

# 실측이 프레임당 3.3초다(193프레임 629초, 2026-08-21). 열 배를 준다.
# 짧은 영상이라도 기동에만 4~11분이 드니 바닥을 한 시간으로 둔다.
# ⚠️ 10분으로 감쌌다가 처리 도중에 끊긴 적이 있다. 넉넉해야 한다.
_SECONDS_PER_FRAME = 33.0
_MIN_DEADLINE_SECONDS = 3600.0


@dataclass(frozen=True)
class UeHybridReadiness:
    ready: bool
    editor: Path
    project: Path
    market: Path
    python: Path
    crop_script: Path
    missing: tuple[str, ...]

    @property
    def scripts(self) -> Path:
        return self.market / "scripts"


def check_ue_hybrid(
    editor: Path, project: Path, market: Path, python: Path, crop_script: Path
) -> UeHybridReadiness:
    """파일 단위로 무엇이 없는지 그대로 돌려준다 — 설치 안내가 이 목록으로 나간다."""
    required = (
        (editor, "UnrealEditor-Cmd.exe"),
        (project, "언리얼 프로젝트(.uproject)"),
        (python, "RTMW3D 파이썬"),
        (crop_script, "crop-subject.py"),
        (market / "scripts" / "ue-process-footage.py", "ue-process-footage.py"),
        (market / "scripts" / "ue-export-performance.py", "ue-export-performance.py"),
        (market / "scripts" / "ue-hybrid-merge.mts", "ue-hybrid-merge.mts"),
    )
    missing = tuple(label for path, label in required if not path.is_file())
    return UeHybridReadiness(
        not missing, editor, project, market, python, crop_script, missing
    )


def default_ue_readiness(project_root: Path | None = None) -> UeHybridReadiness:
    """환경변수로 경로를 받는다. 하나라도 비면 다리는 조용히 꺼진 것과 같다."""
    root = (project_root or Path(__file__).resolve().parents[2]).resolve()
    workspace = root.parent
    return check_ue_hybrid(
        Path(os.environ.get(
            "ARTOKE_UE_EDITOR_CMD",
            r"C:\Program Files\Epic Games\UE_5.8\Engine\Binaries\Win64\UnrealEditor-Cmd.exe",
        )),
        Path(os.environ.get("ARTOKE_UE_PROJECT", str(workspace / "ue-mocap-probe" / "MocapProbe.uproject"))),
        Path(os.environ.get("ARTOKE_SCRIPT_MARKET_DIR", str(workspace / "script-market"))),
        root / ".venv-rtmw3d" / "Scripts" / "python.exe",
        root / "scripts" / "crop-subject.py",
    )


def _run(command: Sequence[str], env=None, timeout=None, **_options) -> tuple[int, str, str]:
    finished = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return finished.returncode, finished.stdout or "", finished.stderr or ""


def _read_shape(tracking: Path) -> tuple[int, float]:
    data = json.loads(tracking.read_text(encoding="utf-8"))
    return len(data.get("frames", [])), float(data.get("fps") or 30.0)


def apply_ue_hybrid(
    video: Path,
    rtmw3d_json: Path,
    workspace: Path,
    readiness: UeHybridReadiness,
    on_stage: Callable[[str, int], None],
    cancelled: Callable[[], bool],
    runner: Runner = _run,
) -> Path | None:
    """합친 트래킹 파일의 경로를 돌려준다. 못 하면 None 을 돌려주고 사유를 남긴다."""
    stem = video.stem
    report_path = workspace / f"{stem}_ue_hybrid.report.json"

    def give_up(step: str, reason: str) -> None:
        report_path.write_text(
            json.dumps(
                {"applied": False, "failed_step": step, "reason": reason.strip()[:2000]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    if not readiness.ready:
        give_up("readiness", "없는 것: " + ", ".join(readiness.missing))
        return None
    if cancelled():
        return None

    frame_count, fps = _read_shape(rtmw3d_json)
    if frame_count <= 0:
        give_up("readiness", "RTMW3D 결과에 프레임이 없습니다")
        return None

    crop = workspace / f"{stem}_subject.mp4"
    performance = workspace / f"{stem}_performance.json"
    hybrid = workspace / f"{stem}_ue_hybrid.json"
    # ue-process-footage.py 는 같은 이름의 캡처 데이터가 있으면 재사용한다. 이름이
    # 영상 내용과 무관하면 다른 영상을 넣고도 낡은 푸티지를 쓰게 된다. 그래서 이름에
    # 지문을 붙인다 — 같은 작업이면 같고, 영상이 바뀌면 달라진다.
    label = "".join(ch for ch in stem.title() if ch.isalnum()) or "Subject"
    fingerprint = hashlib.sha256(
        f"{video.resolve()}|{frame_count}|{fps}".encode("utf-8")
    ).hexdigest()[:8]
    ingest_name = f"{label}{fingerprint}"

    # 언리얼 커맨드릿은 부모 환경을 물려받는다. 스크립트가 상수 대신 이 값들을 읽는다.
    unreal_env = {
        **os.environ,
        "ARTOKE_UE_VIDEO": str(crop),
        "ARTOKE_UE_INGEST_NAME": ingest_name,
        "ARTOKE_UE_FPS": str(int(round(fps))),
        # set_processing_range 는 반열린 구간이라 `상한 - 하한` 이 프레임 수와 같아야
        # 한다. 어긋나면 다 계산하고 나서 결과를 버린다(2026-08-21 실측).
        "ARTOKE_UE_FRAME_COUNT": str(frame_count),
        "ARTOKE_UE_PERFORMANCE": f"/Game/Artoke/PF_{ingest_name}",
        "ARTOKE_UE_OUT": str(performance),
    }
    # 언리얼이 멈추면 워커가 영영 붙잡힌다. 실행 중에는 취소도 못 본다.
    deadline = max(_MIN_DEADLINE_SECONDS, frame_count * _SECONDS_PER_FRAME)
    editor_base = [str(readiness.editor), str(readiness.project), "-run=pythonscript"]
    common = ["-unattended", "-nosplash", "-stdout"]

    steps: list[tuple[str, str, list[str], dict | None]] = [
        # 언리얼은 화면에 사람이 여럿이면 누구를 따라갈지 고를 수단이 없다. 그래서
        # 주인공만 남긴 영상을 만들어 넣는다. cv2 의 mp4v 는 Capture Manager 가
        # 거부하므로 crop-subject.py 가 H.264 로 다시 굽는다.
        ("isolating", "crop-subject.py", [
            str(readiness.python), str(readiness.crop_script),
            "--tracking", str(rtmw3d_json),
            "--input", str(video),
            "--output", str(crop),
            "--mode", "follow", "--isolate",
        ], None),
        # 처리에는 D3D12 가 필요하다. 커맨드릿은 기본으로 렌더링을 끄고 돌아서
        # GDynamicRHI 가 없고, 그러면 can_process 가 False 로 떨어진다.
        ("unreal-tracking", "ue-process-footage.py", [
            *editor_base,
            f"-script={readiness.scripts / 'ue-process-footage.py'}",
            *common, "-AllowCommandletRendering", "-dx12",
        ], unreal_env),
        # 내보내기는 저장된 결과를 읽기만 하므로 -nullrhi 로 충분하고 그만큼 빠르다.
        ("unreal-export", "ue-export-performance.py", [
            *editor_base,
            f"-script={readiness.scripts / 'ue-export-performance.py'}",
            *common, "-nullrhi",
        ], unreal_env),
        ("merging", "ue-hybrid-merge.mts", [
            "npx", "tsx", str(readiness.scripts / "ue-hybrid-merge.mts"),
            str(performance), str(rtmw3d_json), str(hybrid),
        ], {**os.environ, "npm_config_yes": "true"}),
    ]

    for stage, label, command, env in steps:
        if cancelled():
            return None
        on_stage(stage, _STAGE_PROGRESS[stage])
        try:
            code, out, err = runner(
                command, env=env, cwd=str(readiness.market), timeout=deadline
            )
        except subprocess.TimeoutExpired:
            give_up(label, f"{label} 가 제한 시간 {int(deadline)}초를 넘겼습니다")
            return None
        if code != 0:
            give_up(label, err or out or f"{label} 가 {code} 로 끝났습니다")
            return None

    # 반환 코드가 0 이어도 파일이 없으면 성공이 아니다. 언리얼 커맨드릿은 실패해도
    # 0 으로 끝나는 일이 있다.
    if not hybrid.is_file():
        give_up("ue-hybrid-merge.mts", "합친 결과 파일이 없습니다")
        return None

    report_path.write_text(
        json.dumps(
            {"applied": True, "frames": frame_count, "fps": fps, "source": hybrid.name},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return hybrid
