"""`library.scan` 이 FBX 도 카드로 세운다.

BVH 만 훑던 선반에 FBX 를 더한다. 같은 이름의 `.bvh` 가 곁에 있으면(변환 산출물)
그 카드 하나로 충분하니 FBX 쪽은 세우지 않는다 — 안 그러면 같은 클립이 두 장 뜬다.
"""

import os

from maxmcp.ui.studio import library


def _touch(folder, name, text="x"):
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def test_scan_lists_an_fbx_as_a_clip(tmp_path) -> None:
    _touch(str(tmp_path), "Female Start Walking.fbx")

    clips = library.scan(str(tmp_path))

    assert [c.stem for c in clips] == ["Female Start Walking"]
    assert clips[0].path.lower().endswith(".fbx")
    assert clips[0].local is True


def test_scan_skips_an_fbx_whose_bvh_sibling_exists(tmp_path) -> None:
    """변환이 끝난 뒤에는 BVH 카드 하나만 보인다."""
    _touch(str(tmp_path), "walk.fbx")
    _touch(str(tmp_path), "walk.bvh")

    clips = library.scan(str(tmp_path))

    assert [c.path.lower()[-4:] for c in clips] == [".bvh"]


def test_scan_still_hides_biped_conversion_outputs(tmp_path) -> None:
    _touch(str(tmp_path), "walk.bvh")
    _touch(str(tmp_path), "walk_biped.bvh")
    _touch(str(tmp_path), "jump.fbx")

    stems = sorted(c.stem for c in library.scan(str(tmp_path)))

    assert stems == ["jump", "walk"]


def test_scan_keeps_bvh_and_fbx_sorted_together(tmp_path) -> None:
    _touch(str(tmp_path), "b.fbx")
    _touch(str(tmp_path), "a.bvh")
    _touch(str(tmp_path), "c.bvh")

    assert [c.stem for c in library.scan(str(tmp_path))] == ["a", "b", "c"]
