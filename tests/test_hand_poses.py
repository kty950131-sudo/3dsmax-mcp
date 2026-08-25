"""hand_poses — .cpy 파싱과 TGA→PNG. 실제 손 포즈 컬렉션(assets/)을 상대로 돈다."""

import os
import struct
import zlib

import pytest

from maxmcp.ui.studio import hand_poses


@pytest.fixture(scope="module")
def cpy_bytes():
    assert os.path.isfile(hand_poses.DEFAULT_CPY)
    with open(hand_poses.DEFAULT_CPY, "rb") as handle:
        return handle.read()


def test_parse_finds_all_34_postures_in_file_order(cpy_bytes):
    poses = hand_poses.parse_cpy(cpy_bytes)
    assert [p.name for p in poses] == [f"RFing{i:02d}" for i in range(1, 35)]
    assert [p.index for p in poses] == list(range(1, 35))
    assert all(p.tga.endswith(b"TRUEVISION-XFILE.\x00") for p in poses)


def test_tga_to_png_decodes_rle_thumbnail(cpy_bytes):
    pose = hand_poses.parse_cpy(cpy_bytes)[0]
    png = hand_poses.tga_to_png(pose.tga)
    assert png.startswith(b"\x89PNG")
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (139, 113)
    # IDAT 를 풀면 줄마다 필터 바이트 1 + RGBA 4×폭 이어야 한다
    idat_len = struct.unpack(">I", png[33:37])[0]
    raw = zlib.decompress(png[41 : 41 + idat_len])
    assert len(raw) == height * (1 + width * 4)


def test_tga_to_png_rejects_paletted():
    header = bytes([0, 1, 1]) + bytes(15)
    with pytest.raises(ValueError):
        hand_poses.tga_to_png(header)


def test_pose_cards_caches_png_and_returns_data_urls(tmp_path):
    cards = hand_poses.pose_cards(cache_dir=str(tmp_path))
    assert len(cards) == 34
    assert cards[0]["name"] == "RFing01" and cards[0]["index"] == 1
    assert cards[0]["image"].startswith("data:image/png;base64,")
    cached = [d for d in (tmp_path / "hand_poses").rglob("*.png")]
    assert len(cached) == 34
    # 두 번째 호출은 캐시를 읽는다 — 파일 시각이 그대로다
    stamp = {p: p.stat().st_mtime_ns for p in cached}
    again = hand_poses.pose_cards(cache_dir=str(tmp_path))
    assert again == cards
    assert {p: p.stat().st_mtime_ns for p in cached} == stamp


# ---- 어느 쪽에서 복사했는가 (미러 여부를 여기서 정한다) --------------------
# Biped 복사 포스처는 원본 노드를 기억한다. 그래서 "왼쪽에 넣기" 가 미러인지
# 아닌지는 원본이 어느 쪽이냐에 따라 뒤집힌다. 이름의 첫 글자가 그 단서다.


@pytest.mark.parametrize(
    "name, expected",
    [
        ("RFing01", "right"),
        ("rFing01", "right"),
        ("LArm01", "left"),
        ("lArm01", "left"),
        ("Hand01", ""),
        ("", ""),
    ],
)
def test_source_side_reads_the_leading_letter(name, expected):
    assert hand_poses.source_side(name) == expected


@pytest.mark.parametrize(
    "source, target, expected",
    [
        # 오른쪽에서 복사한 것
        ("right", "right", [False]),   # 그대로
        ("right", "left", [True]),     # 미러
        # 왼쪽에서 복사한 것 — 뒤집힌다
        ("left", "left", [False]),     # 그대로
        ("left", "right", [True]),     # 미러
        # 양쪽은 원본과 무관하게 둘 다
        ("right", "both", [False, True]),
        ("left", "both", [False, True]),
        ("", "both", [False, True]),
        # 이름으로 모르면 "원본은 오른쪽" 으로 가정한다
        ("", "right", [False]),
        ("", "left", [True]),
    ],
)
def test_opposites_flip_with_the_source_side(source, target, expected):
    assert hand_poses.opposites_for(source, target) == expected


def test_left_sourced_posture_is_not_mirrored_when_going_left():
    """LArm01 을 왼쪽에 넣을 때 미러를 걸면 오른팔로 가 버린다 — 이 실수를 막는다."""
    assert hand_poses.opposites_for(hand_poses.source_side("LArm01"), "left") == [False]
    assert hand_poses.opposites_for(hand_poses.source_side("LArm01"), "right") == [True]


# ---- 선반 두 개 -------------------------------------------------------------


def test_shelves_point_at_their_own_files():
    hand = hand_poses.shelf_info("hand")
    body = hand_poses.shelf_info("body")
    assert hand["path"] != body["path"]
    assert hand["collection"] == "hand_pose" and body["collection"] == "body_pose"
    # 손 포즈만 좌우 미러가 있다. 전신 포즈를 뒤집는 것은 다른 뜻이다.
    assert hand["sides"] is True and body["sides"] is False
    assert hand["kind"] == "posture" and body["kind"] == "pose"


def test_unknown_shelf_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        hand_poses.shelf_info("elbow")


def test_missing_file_gives_empty_shelf_not_an_error(tmp_path):
    """전신 포즈 파일은 사용자가 처음 저장할 때 생긴다. 그때까지 선반을 열 수
    없으면 저장 단추에 닿을 수조차 없다."""
    assert hand_poses.pose_cards(str(tmp_path / "없는파일.cpy")) == []


# ---- 애니 → 라이브러리 파일 이름 --------------------------------------------
# save_anim_to_library 자체는 Max 가 있어야 돌지만, 이름을 다듬는 부분은 순수
# 함수라 여기서 고정한다. 이름 칸은 JS 가 어떤 문자열로도 부를 수 있는 경계다.


def test_clean_stem_keeps_the_file_inside_the_library():
    from maxmcp.ui.studio.biped_export import _clean_stem

    # 경로 구분자를 지워서 라이브러리 밖으로 나가지 못하게 한다
    assert _clean_stem(r"..\밖으로") == "밖으로"
    assert _clean_stem("sub/걷기") == "sub걷기"
    # 확장자는 붙이든 말든 같은 결과
    assert _clean_stem("  달리기.bvh ") == "달리기"
    assert _clean_stem("달리기") == "달리기"
    # 윈도우가 거부하는 글자와 빈 이름
    assert _clean_stem('a<b>c:d"e|f?g*h') == "abcdefgh"
    assert _clean_stem("   ") == ""
    assert _clean_stem(".") == ""
    assert _clean_stem(None) == ""
