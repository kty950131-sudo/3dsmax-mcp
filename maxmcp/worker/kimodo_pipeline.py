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
    # 앞부분만 잘라 읽지 않는다. SOMA 뼈대는 HIERARCHY 가 14KB 를 넘어
    # `Frames:` 가 4096자 컷 밖에 있었고, 성공한 생성을 여기서 떨어뜨렸다
    # (2026-08-24 실측, 세 번째 15분 낭비). 파일이 200KB 급이라 통짜로 읽는다.
    match = re.search(r"Frames:\s*(\d+)", path.read_text(encoding="utf-8", errors="replace"))
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

    def _docker_cp(self, container_path: str, dest: Path) -> bool:
        """컨테이너에서 산출물을 꺼낸다. 성공하면 True."""
        result = subprocess.run(
            ["docker", "compose", "cp", f"demo:{container_path}", str(dest)],
            cwd=str(self._kimodo_dir),
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0 and dest.is_file()

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
            # text=True 만 주면 Windows 는 cp949 로 읽는다. docker 출력의 UTF-8
            # 진행바 문자(0xe2…)에서 리더 스레드가 죽어 파이프라인이 통째로
            # 실패했다(08-24 실측). 콘솔 출력은 기록용이라 깨진 글자는 바꿔치운다.
            encoding="utf-8",
            errors="replace",
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
            # 서버에는 error_code 만 남아 실패가 장님이 된다. 전체 출력을 로컬에
            # 남겨 둔다 — 워크스페이스는 청소되므로 kimodo 쪽에 쓴다.
            try:
                log_dir = self._kimodo_dir / "output"
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / f"{tag}.error.log").write_text(
                    f"exit={process.returncode}\n--- stdout ---\n{stdout or ''}\n--- stderr ---\n{stderr or ''}",
                    encoding="utf-8",
                )
            except OSError:
                pass
            raise RuntimeError((stderr or stdout or "kimodo_gen failed").strip()[-2000:])

        # 표본 1개면 kimodo_gen 은 폴더가 아니라 **단일 파일**(`output/<tag>.bvh`)로
        # 저장한다 — `<tag>/` 폴더에 `_00` 접미사가 붙는 것은 다표본 때다. 그리고
        # 호스트 output 마운트는 믿을 수 없다: 컨테이너 안에는 BVH 가 있는데
        # 호스트에는 안 보이는 것을 실측했다(08-24, 두 번 18분씩 낭비). 그래서
        # 호스트를 먼저 훑되, 없으면 docker compose cp 로 컨테이너에서 꺼낸다.
        on_stage("converting", 65)
        bvh_path = workspace / f"{source_path.stem}_kimodo.bvh"
        out_dir = self._kimodo_dir / "output" / tag
        flat = self._kimodo_dir / "output" / f"{tag}.bvh"
        found = (
            sorted(out_dir.glob("*_00.bvh")) or sorted(out_dir.rglob("*.bvh"))
            if out_dir.is_dir()
            else []
        ) or ([flat] if flat.is_file() else [])
        if found:
            shutil.copy2(found[0], bvh_path)
        elif not any(
            self._docker_cp(container_path, bvh_path)
            for container_path in (
                f"/workspace/output/{tag}.bvh",
                f"/workspace/output/{tag}/{tag}_00.bvh",
            )
        ):
            raise RuntimeError(f"Kimodo 가 BVH 를 만들지 않았습니다: {out_dir}")
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
