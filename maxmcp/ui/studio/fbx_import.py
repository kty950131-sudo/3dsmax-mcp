"""스튜디오가 FBX 를 받아들이는 길 — 곁에 BVH 를 만들어 두고 그 경로로 갈아탄다.

스튜디오의 모든 경로(카드 미리보기·임포트·리타깃)는 BVH 텍스트를 읽는다.
`biped.loadMocapFile` 도 BVH·CSM 만 받는다. 그래서 FBX 는 그 자리에서 곁에
`<이름>.bvh` 를 만들고, 이후는 전부 기존 BVH 길을 그대로 탄다. 새 길을 하나 더
내는 대신 입구에서 갈아타는 쪽이 고칠 곳이 적다(사장님 지시, 2026-08-30).

변환은 Blender 헤드리스(`scripts/fbx_to_bvh.py`)다. 몇 초 걸리므로 이미 있고
원본보다 새로우면 다시 만들지 않는다. 원본을 다시 내보냈으면(원본이 더 새로움)
옛 BVH 를 쓰면 안 되니 다시 만든다.

⚠️ Blender 는 스크립트가 죽어도 0 으로 끝날 수 있다. 종료 코드가 아니라
**결과 파일이 생겼는지**로 성공을 판정한다.
"""

from __future__ import annotations

import glob
import os
import subprocess
from typing import Callable, Optional, Sequence

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CONVERTER = os.path.join(REPO_ROOT, "scripts", "fbx_to_bvh.py")

# 새 판이 먼저 잡히게 내림차순으로 훑는다.
DEFAULT_SEARCH: tuple[str, ...] = (
    r"C:\Program Files\Blender Foundation\*\blender.exe",
    r"C:\Program Files (x86)\Blender Foundation\*\blender.exe",
)

Runner = Callable[..., object]


def blender_exe(search: Sequence[str] = DEFAULT_SEARCH) -> Optional[str]:
    """환경변수 `ARTOKE_BLENDER_EXE` 가 있으면 그것, 없으면 설치 폴더에서 가장 새 판."""
    env = os.environ.get("ARTOKE_BLENDER_EXE")
    if env and os.path.isfile(env):
        return env
    found: list[str] = []
    for pattern in search:
        found.extend(glob.glob(pattern))
    if not found:
        return None
    return sorted(found)[-1]


def is_fbx(path: str) -> bool:
    return path.lower().endswith(".fbx")


def bvh_sibling(fbx_path: str) -> str:
    stem, _ = os.path.splitext(fbx_path)
    return stem + ".bvh"


def _run(command: Sequence[str], **_options):
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def ensure_bvh(
    fbx_path: str,
    blender: Optional[str] = None,
    runner: Runner = _run,
) -> str:
    """FBX 곁의 BVH 경로를 돌려준다. 없거나 낡았으면 Blender 로 만든다."""
    if not os.path.isfile(fbx_path):
        raise RuntimeError(f"FBX 파일이 없습니다: {fbx_path}")
    dst = bvh_sibling(fbx_path)
    if os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(fbx_path):
        return dst

    # Max 안이면 Max 로 뽑는다. Blender 는 Max Biped FBX 의 애니를 잃기 때문이다.
    # (밖에서 도는 배치·테스트는 runner 를 주입하므로 이 자동 경로를 타지 않는다.)
    if runner is _run and blender is None:
        try:
            from maxmcp.ui.studio import fbx_max
            if fbx_max.available():
                info = fbx_max.convert(fbx_path, dst)
                if "error" in info:
                    raise RuntimeError(f"FBX 변환 실패: {os.path.basename(fbx_path)} — {info['error']}")
                return dst
        except ImportError:
            pass

    exe = blender if blender is not None else blender_exe()
    if not exe:
        raise RuntimeError(
            "Blender 를 찾지 못했습니다. FBX 를 읽으려면 Blender 가 필요합니다. "
            "설치하거나 ARTOKE_BLENDER_EXE 로 blender.exe 경로를 알려 주십시오."
        )

    command = [
        exe, "--background", "--python", CONVERTER, "--",
        "--file", fbx_path, "--dst", dst,
    ]
    result = runner(command)
    if not os.path.isfile(dst):
        detail = ""
        for stream in (getattr(result, "stderr", ""), getattr(result, "stdout", "")):
            text = (stream or "").strip()
            if text:
                # RESULT 줄이 있으면 그것이 가장 정확한 사유다
                lines = [ln for ln in text.splitlines() if ln.startswith("RESULT")] or text.splitlines()[-3:]
                detail = " / ".join(ln.strip() for ln in lines)
                break
        raise RuntimeError(f"FBX 변환 실패: {os.path.basename(fbx_path)} — {detail or '결과 파일이 생기지 않았습니다'}")
    return dst


def resolve_clip_path(path: str, blender: Optional[str] = None, runner: Runner = _run) -> str:
    """FBX 면 BVH 로 갈아탄 경로, 그 외는 그대로."""
    if is_fbx(path):
        return ensure_bvh(path, blender=blender, runner=runner)
    return path
