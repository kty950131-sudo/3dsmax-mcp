"""문장 입력 작업을 Kimodo 로 돌려 BVH 를 낸다 (텍스트→모션 1단계).

소스가 영상이 아니라 `*.kimodo.json`(프롬프트·비트 길이·시드)이다. 러너가
파일 이름으로 갈래를 태운다. MotionPipeline 과 같은 `PipelineArtifacts` 를
돌려주므로 게시 쪽은 이 갈래를 모른다.

산출물 대응:
- bvh          Kimodo 출력 (`--bvh --bvh_standard_tpose`, 표본 1개)
- rtmw3d_json  프롬프트 JSON 그대로 — 생성 기록(프롬프트·시드)이 곧 출처다
- trace        실행 기록 (명령·sha256)

시간이 오래 걸린다(1프롬프트 20~25분, 대부분 CPU 텍스트 인코딩). 진행바로
추정하지 말라는 것이 kimodo 운용 규칙이고, 하트비트는 러너의 별도 스레드가
계속 보내므로 리스가 끊기지는 않는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from maxmcp.worker.motion_pipeline import PipelineArtifacts, PipelineCancelled

KIMODO_DIR = Path(os.environ.get("ARTOKE_KIMODO_DIR", r"C:\work\Ai\kimodo"))
PROMPT_SUFFIX = ".kimodo.json"


def is_prompt_source(filename: str) -> bool:
    return filename.lower().endswith(PROMPT_SUFFIX)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bvh_frames(path: Path) -> int:
    match = re.search(r"Frames:\s*(\d+)", path.read_text(encoding="utf-8", errors="replace")[:4096])
    if not match:
        raise RuntimeError("Kimodo BVH 에 Frames 가 없습니다")
    return int(match.group(1))


class KimodoPipeline:
    """MotionPipeline 과 같은 겉모양. 안에서는 docker 의 kimodo_gen 을 돈다."""

    def __init__(
        self,
        process_factory: Callable[..., Any] = subprocess.Popen,
        kimodo_dir: Path = KIMODO_DIR,
    ) -> None:
        self._process_factory = process_factory
        self._kimodo_dir = kimodo_dir
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
        source_path: Path,
        workspace: Path,
        on_stage: Callable[[str, int], None],
        cancelled: Callable[[], bool],
    ) -> PipelineArtifacts:
        if self._cancelled.is_set() or cancelled():
            raise PipelineCancelled()
        workspace.mkdir(parents=True, exist_ok=True)
        spec = json.loads(source_path.read_text(encoding="utf-8"))
        prompt = str(spec["prompt"]).strip()
        durations = [float(v) for v in spec["durations"]]
        seed = int(spec.get("seed", 0))
        model = str(spec.get("model", "Kimodo-SOMA-RP-v1.1"))
        steps = int(spec.get("diffusion_steps", 100))
        if not prompt or not durations:
            raise RuntimeError("프롬프트가 비어 있습니다")

        tag = f"job_{source_path.stem.replace('.', '_')}_{seed}"
        command = [
            "docker", "compose", "exec", "-T", "demo", "kimodo_gen", prompt,
            "--model", model,
            "--duration", " ".join(f"{d:g}" for d in durations),
            "--diffusion_steps", str(steps),
            "--num_samples", "1",
            "--seed", str(seed),
            "--bvh", "--bvh_standard_tpose",
            "--output", f"output/{tag}",
        ]

        on_stage("extracting", 15)
        process = self._process_factory(
            command,
            cwd=str(self._kimodo_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with self._lock:
            self._process = process
        try:
            stdout, stderr = process.communicate()
        except BaseException:
            process.terminate()
            if hasattr(process, "wait"):
                process.wait()
            raise
        finally:
            with self._lock:
                self._process = None
        if self._cancelled.is_set() or cancelled():
            raise PipelineCancelled()
        if process.returncode != 0:
            raise RuntimeError((stderr or stdout or "kimodo_gen failed").strip()[-2000:])

        # 표본 1개 규칙이라 `<tag>_00.bvh` 하나다. 경로가 바뀌어도 잡히게 훑는다.
        out_dir = self._kimodo_dir / "output" / tag
        found = sorted(out_dir.glob("*_00.bvh")) or sorted(out_dir.rglob("*.bvh"))
        if not found:
            raise RuntimeError(f"Kimodo 가 BVH 를 만들지 않았습니다: {out_dir}")

        on_stage("converting", 65)
        bvh_path = workspace / f"{source_path.stem}_kimodo.bvh"
        shutil.copy2(found[0], bvh_path)
        frame_count = _bvh_frames(bvh_path)

        on_stage("validating", 85)
        trace_path = workspace / f"{source_path.stem}_kimodo_trace.json"
        trace_path.write_text(json.dumps({
            "backend": f"NVIDIA {model}",
            "prompt": prompt,
            "durations": durations,
            "seed": seed,
            "command": command,
            "frame_count": frame_count,
            "sha256": {"prompt": _sha256(source_path), "bvh": _sha256(bvh_path)},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return PipelineArtifacts(source_path, bvh_path, trace_path, frame_count)
