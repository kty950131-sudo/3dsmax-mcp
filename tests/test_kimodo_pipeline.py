"""Kimodo 갈래. 핵심 — 소스 판별, 명령 구성, BVH 회수, 러너 라우팅."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from maxmcp.worker.kimodo_pipeline import KimodoPipeline, is_prompt_source


def spec(tmp_path: Path) -> Path:
    p = tmp_path / "prompt.kimodo.json"
    p.write_text(json.dumps({
        "schema": "artoke.kimodo.prompt.v1",
        "prompt": "A person waves. A person bows.",
        "durations": [2.0, 2.0], "seed": 42,
    }), encoding="utf-8")
    return p


class FakeProcess:
    def __init__(self, command, out_root: Path, **_):
        self.returncode = 0
        prompt_i = command.index("kimodo_gen") + 1
        tag = command[command.index("--output") + 1].split("/", 1)[1]
        out = out_root / "output" / tag
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{tag}_00.bvh").write_text(
            "HIERARCHY\nROOT Hips\n{\n}\nMOTION\nFrames: 120\nFrame Time: 0.0333\n",
            encoding="utf-8")
        self.command = command
        assert command[prompt_i] == "A person waves. A person bows."

    def communicate(self):
        return "", ""


def test_is_prompt_source():
    assert is_prompt_source("prompt.kimodo.json")
    assert is_prompt_source("PROMPT.KIMODO.JSON")
    assert not is_prompt_source("clip.mp4")
    assert not is_prompt_source("data.json")   # 확장자를 못 박은 이유


def test_runs_kimodo_and_collects_bvh(tmp_path):
    kimodo = tmp_path / "kimodo"; kimodo.mkdir()
    made = {}
    def factory(command, **kw):
        proc = FakeProcess(command, kimodo, **kw)
        made["command"] = command
        return proc
    pipe = KimodoPipeline(process_factory=factory, kimodo_dir=kimodo)
    result = pipe.run(spec(tmp_path), tmp_path / "ws", lambda *_: None, lambda: False)
    assert result.frame_count == 120
    assert result.bvh.is_file()
    # 시드·비트 길이가 명령에 그대로 실린다 — 재현성의 근거다
    cmd = made["command"]
    assert cmd[cmd.index("--seed") + 1] == "42"
    assert cmd[cmd.index("--duration") + 1] == "2 2"
    assert cmd[cmd.index("--num_samples") + 1] == "1"
    # rtmw3d_json 자리에는 프롬프트 JSON 이 그대로 간다 — 생성 기록이 곧 출처
    assert result.rtmw3d_json.name == "prompt.kimodo.json"
    trace = json.loads(result.trace.read_text(encoding="utf-8"))
    assert trace["seed"] == 42 and trace["prompt"].startswith("A person waves")


def test_collects_flat_single_sample_bvh(tmp_path):
    # num_samples 1 이면 kimodo_gen 은 `output/<tag>.bvh` 단일 파일로 저장한다.
    # 폴더만 뒤지다가 성공한 생성을 "BVH 없음"으로 오판했다 (2026-08-24 실측).
    kimodo = tmp_path / "kimodo"; kimodo.mkdir()
    class FlatOutput:
        returncode = 0
        def __init__(self, command, **_):
            tag = command[command.index("--output") + 1].split("/", 1)[1]
            out = kimodo / "output"
            out.mkdir(parents=True, exist_ok=True)
            (out / f"{tag}.bvh").write_text(
                "HIERARCHY\nROOT Hips\n{\n}\nMOTION\nFrames: 90\nFrame Time: 0.0333\n",
                encoding="utf-8")
        def communicate(self): return "", ""
    pipe = KimodoPipeline(process_factory=FlatOutput, kimodo_dir=kimodo)
    result = pipe.run(spec(tmp_path), tmp_path / "ws", lambda *_: None, lambda: False)
    assert result.frame_count == 90
    assert result.bvh.is_file()


def test_missing_bvh_raises(tmp_path):
    kimodo = tmp_path / "kimodo"; kimodo.mkdir()
    class NoOutput:
        returncode = 0
        def __init__(self, *a, **k): pass
        def communicate(self): return "", ""
    pipe = KimodoPipeline(process_factory=NoOutput, kimodo_dir=kimodo)
    with pytest.raises(RuntimeError, match="BVH"):
        pipe.run(spec(tmp_path), tmp_path / "ws", lambda *_: None, lambda: False)
