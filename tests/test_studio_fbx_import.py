"""`maxmcp.ui.studio.fbx_import` — 스튜디오가 FBX 를 받아들이는 길.

스튜디오의 모든 경로(카드 미리보기·임포트·리타깃)는 BVH 텍스트를 읽는다. FBX 는
그 자리에서 곁에 `<이름>.bvh` 를 만들어 두고 그 경로로 갈아탄다. 변환은 Blender
헤드리스라 몇 초 걸리므로, 이미 있고 원본보다 새로우면 다시 만들지 않는다.
"""

import os
import time

import pytest

from maxmcp.ui.studio import fbx_import


class FakeRunner:
    """Blender 를 실제로 띄우지 않고, 부르기만 기록한 뒤 결과 파일을 쓴다."""

    def __init__(self, succeed: bool = True, stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.succeed = succeed
        self.stderr = stderr

    def __call__(self, command, **_options):
        self.calls.append(list(command))
        if self.succeed:
            dst = command[command.index("--dst") + 1]
            with open(dst, "w", encoding="utf-8") as handle:
                handle.write("HIERARCHY\nROOT Hips\n{\n}\nMOTION\nFrames: 0\nFrame Time: 0.033\n")
        return type("R", (), {"returncode": 0 if self.succeed else 1,
                              "stdout": "", "stderr": self.stderr})()


def _fbx(tmp_path, name="walk.fbx"):
    path = tmp_path / name
    path.write_bytes(b"fbx")
    return str(path)


def test_bvh_sibling_sits_next_to_the_fbx(tmp_path) -> None:
    fbx = _fbx(tmp_path)
    assert fbx_import.bvh_sibling(fbx) == str(tmp_path / "walk.bvh")


def test_converts_when_no_sibling_exists(tmp_path) -> None:
    fbx = _fbx(tmp_path)
    runner = FakeRunner()

    out = fbx_import.ensure_bvh(fbx, blender="blender.exe", runner=runner)

    assert out == str(tmp_path / "walk.bvh")
    assert os.path.isfile(out)
    assert len(runner.calls) == 1
    command = " ".join(runner.calls[0])
    assert "--background" in command and "fbx_to_bvh.py" in command
    assert "--file" in command and "--dst" in command


def test_reuses_a_fresh_sibling_without_running_blender(tmp_path) -> None:
    fbx = _fbx(tmp_path)
    bvh = tmp_path / "walk.bvh"
    bvh.write_text("HIERARCHY", encoding="utf-8")
    # 원본보다 새로운 것으로 만든다
    later = time.time() + 10
    os.utime(str(bvh), (later, later))
    runner = FakeRunner()

    out = fbx_import.ensure_bvh(fbx, blender="blender.exe", runner=runner)

    assert out == str(bvh)
    assert runner.calls == []


def test_reconverts_when_the_fbx_is_newer_than_the_sibling(tmp_path) -> None:
    """원본을 다시 내보냈으면 옛 BVH 를 쓰면 안 된다."""
    fbx = _fbx(tmp_path)
    bvh = tmp_path / "walk.bvh"
    bvh.write_text("old", encoding="utf-8")
    earlier = time.time() - 100
    os.utime(str(bvh), (earlier, earlier))
    runner = FakeRunner()

    fbx_import.ensure_bvh(fbx, blender="blender.exe", runner=runner)

    assert len(runner.calls) == 1


def test_failure_surfaces_blenders_message(tmp_path) -> None:
    fbx = _fbx(tmp_path)
    runner = FakeRunner(succeed=False, stderr="RESULT\t1/1\twalk\tFAIL\tno armature")

    with pytest.raises(RuntimeError) as info:
        fbx_import.ensure_bvh(fbx, blender="blender.exe", runner=runner)

    assert "no armature" in str(info.value)


def test_missing_output_is_a_failure_even_with_exit_zero(tmp_path) -> None:
    """Blender 는 스크립트가 죽어도 0 으로 끝날 수 있다. 파일이 없으면 실패다."""
    fbx = _fbx(tmp_path)

    def runner(command, **_options):
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with pytest.raises(RuntimeError):
        fbx_import.ensure_bvh(fbx, blender="blender.exe", runner=runner)


def test_resolve_passes_bvh_through_untouched(tmp_path) -> None:
    bvh = tmp_path / "run.bvh"
    bvh.write_text("HIERARCHY", encoding="utf-8")

    def forbidden(*_a, **_k):
        raise AssertionError("BVH 는 변환하지 않는다")

    assert fbx_import.resolve_clip_path(str(bvh), blender="b", runner=forbidden) == str(bvh)


def test_resolve_converts_fbx(tmp_path) -> None:
    fbx = _fbx(tmp_path)
    runner = FakeRunner()

    out = fbx_import.resolve_clip_path(fbx, blender="blender.exe", runner=runner)

    assert out.lower().endswith(".bvh")
    assert len(runner.calls) == 1


def test_blender_exe_prefers_the_environment_variable(monkeypatch, tmp_path) -> None:
    exe = tmp_path / "blender.exe"
    exe.write_bytes(b"")
    monkeypatch.setenv("ARTOKE_BLENDER_EXE", str(exe))
    assert fbx_import.blender_exe() == str(exe)


def test_blender_exe_returns_none_when_nothing_is_installed(monkeypatch) -> None:
    monkeypatch.delenv("ARTOKE_BLENDER_EXE", raising=False)
    assert fbx_import.blender_exe(search=[]) is None


def test_ensure_bvh_explains_when_blender_is_missing(tmp_path, monkeypatch) -> None:
    # 이 기계에는 Blender 가 실제로 있어서, 자동 탐색을 막아야 "없는" 상황이 된다.
    monkeypatch.delenv("ARTOKE_BLENDER_EXE", raising=False)
    monkeypatch.setattr(fbx_import, "blender_exe", lambda **_k: None)
    fbx = _fbx(tmp_path)

    with pytest.raises(RuntimeError) as info:
        fbx_import.ensure_bvh(fbx, blender=None, runner=FakeRunner())

    assert "Blender" in str(info.value)
