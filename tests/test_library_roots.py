"""라이브러리 루트가 여러 출처를 정션(바로가기)으로 모아 둔 구성일 때의 스캔·삭제.

실제 구성(ani_ / ani_2)이 그렇다: 루트에는 .bvh 가 한 개도 없고 전부 하위
정션 안에 있다. 여기서 지키려는 것은 두 가지다.

1. 정션을 따라 들어가 클립을 찾는다 (안 따라가면 0개로 보인다).
2. 정션 안의 클립은 **지우지 않는다** (지우면 라이브러리가 아니라 원본이 사라진다).
"""

import os
import subprocess
import sys

import pytest

from maxmcp.ui.studio import library


def _bvh(path):
    path.write_text("HIERARCHY\nROOT Hips\n", encoding="utf-8")


def _link(link, target):
    """디렉터리 바로가기. 윈도우에서는 정션을 쓴다 — 심볼릭 링크와 달리 관리자
    권한도 개발자 모드도 필요 없고, 실제 라이브러리 구성(ani_/ani_2)도 정션이다."""
    if sys.platform == "win32":
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True
        )
        if done.returncode != 0:
            pytest.skip("정션을 만들 수 없습니다: " + done.stderr.decode("mbcs", "replace"))
        return
    link.symlink_to(target, target_is_directory=True)


@pytest.fixture
def roots(tmp_path):
    """원본 폴더 두 개와, 그것을 정션으로 모아 둔 라이브러리 루트."""
    origin_a = tmp_path / "origin-a"
    origin_b = tmp_path / "origin-b"
    origin_a.mkdir()
    origin_b.mkdir()
    _bvh(origin_a / "walk.bvh")
    _bvh(origin_a / "walk_biped.bvh")  # 변환 산출물 — 목록에서 빠져야 한다
    _bvh(origin_b / "run.bvh")

    root = tmp_path / "lib"
    root.mkdir()
    _bvh(root / "direct.bvh")  # 루트에 바로 놓인 클립
    _link(root / "a", origin_a)
    _link(root / "b", origin_b)
    return root, origin_a


def test_scan_follows_junctions_and_records_source(roots):
    root, _ = roots
    clips = library.scan(str(root))
    assert sorted((c.stem, c.source) for c in clips) == [
        ("direct", ""),
        ("run", "b"),
        ("walk", "a"),
    ]


def test_scan_skips_conversion_output(roots):
    root, _ = roots
    assert all(not c.stem.endswith("_biped") for c in library.scan(str(root)))


def test_scan_is_empty_without_recursion_target(tmp_path):
    """폴더가 없으면 빈 목록. 예외를 던지면 창이 통째로 멈춘다."""
    assert library.scan(str(tmp_path / "없는폴더")) == []


def test_delete_refuses_clips_inside_a_junction(roots):
    """정션 안의 클립을 지우면 원본이 사라진다 — 막고, 원본은 그대로 남아야 한다."""
    root, origin_a = roots
    target = str(root / "a" / "walk.bvh")
    with pytest.raises(ValueError, match="바로가기"):
        library.delete_clip(str(root), target)
    assert (origin_a / "walk.bvh").exists()


def test_delete_still_removes_a_real_clip_in_the_root(roots):
    root, _ = roots
    result = library.delete_clip(str(root), str(root / "direct.bvh"))
    assert result["removed"] == ["direct.bvh"]
    assert not (root / "direct.bvh").exists()


def test_delete_refuses_outside_the_library(roots, tmp_path):
    root, _ = roots
    outside = tmp_path / "outside.bvh"
    _bvh(outside)
    with pytest.raises(ValueError):
        library.delete_clip(str(root), str(outside))
    assert outside.exists()


def test_folder_policy_reads_the_no_upload_marker(tmp_path):
    assert library.folder_policy(str(tmp_path)) == {"upload": True, "note": ""}
    (tmp_path / library.NO_UPLOAD_NAME).write_text(
        "여기 자료는 올리지 않습니다.\n근거는 INDEX.md 를 보십시오.\n", encoding="utf-8"
    )
    policy = library.folder_policy(str(tmp_path))
    assert policy["upload"] is False
    assert policy["note"] == "여기 자료는 올리지 않습니다."


def test_load_shelf_merges_manifests_from_subfolders(roots):
    """정션마다 자기 매니페스트를 들고 온다 — 루트 것만 읽으면 전부 미분류가 된다."""
    root, _ = roots
    (root / "a" / library.LOCAL_SHELF_NAME).write_text(
        '{"categories": [{"slug": "locomotion", "label": "이동"}],'
        ' "motions": [{"name": "walk.bvh", "category": "locomotion", "sub": "walk"}]}',
        encoding="utf-8",
    )
    shelf = library.load_shelf(str(root))
    assert shelf["by_name"]["walk.bvh"] == ("locomotion", "walk", None)
    assert shelf["categories"] == [{"slug": "locomotion", "label": "이동"}]

    walk = next(c for c in library.scan(str(root)) if c.stem == "walk")
    assert (walk.category, walk.sub) == ("locomotion", "walk")


def test_scan_lists_each_file_once_even_if_two_junctions_point_at_it(roots, tmp_path):
    root, origin_a = roots
    _link(root / "a-again", origin_a)
    stems = [c.stem for c in library.scan(str(root))]
    assert stems.count("walk") == 1
