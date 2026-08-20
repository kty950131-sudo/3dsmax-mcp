from pathlib import Path

import pytest

import maxmcp.helpers.bvh as bvh_mod

from maxmcp.helpers.bvh import (
    _biped_rename_map,
    has_upright_spine,
    parse_bvh,
    prepare_for_biped,
    prune_joints,
    rename_for_biped,
    serialize_bvh,
    strip_static_root,
    unwrap_angles,
    warp,

    wrap_static_root,
    recenter_ground,
    fold_constant_positions,
)


def test_bvh_maxscript_embedded_python_uses_maxmcp_package() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "maxscript" / "bvh_biped_ui.ms"
    ).read_text(encoding="utf-8")

    assert "import src." not in script
    assert "from src." not in script
    assert "import maxmcp.helpers.bvh as _bvh" in script
    assert "from maxmcp.helpers.bvh import prepare_for_biped" in script
    assert "import maxmcp.helpers.github_sync as _gs" in script


# kimodo-style: static wrapper root above a 6-channel Hips, extra eye joint.
KIMODO_STYLE = """HIERARCHY
ROOT Root
{
  OFFSET 0.0 0.0 0.0
  CHANNELS 6 Xposition Yposition Zposition Zrotation Yrotation Xrotation
  JOINT Hips
  {
    OFFSET 0.0 100.0 0.0
    CHANNELS 6 Xposition Yposition Zposition Zrotation Yrotation Xrotation
    JOINT Head
    {
      OFFSET 0.0 20.0 0.0
      CHANNELS 3 Zrotation Yrotation Xrotation
      JOINT LeftEye
      {
        OFFSET 2.0 5.0 3.0
        CHANNELS 3 Zrotation Yrotation Xrotation
      }
      JOINT HeadEnd
      {
        OFFSET 0.0 10.0 0.0
        CHANNELS 3 Zrotation Yrotation Xrotation
      }
    }
  }
}
MOTION
Frames: 2
Frame Time: 0.03333333
0.0 0.0 0.0 0.0 -0.0 0.0 1.0 99.0 2.0 89.0 -4.9 90.2 1.0 2.0 3.0 0.5 0.5 0.5 0.1 0.2 0.3
0.0 0.0 0.0 0.0 -0.0 0.0 1.1 99.1 2.1 89.1 -4.8 90.3 1.1 2.1 3.1 0.6 0.6 0.6 0.2 0.3 0.4
"""

# Fixture for rotation channel crossing: Zrotation goes 179 -> -179 between frames.
# Used to test unwrap + interpolation integration in warp().
ROTATION_CROSSING = """HIERARCHY
ROOT Hips
{
  OFFSET 0.0 0.0 0.0
  CHANNELS 3 Xposition Yposition Zrotation
}
MOTION
Frames: 2
Frame Time: 0.03333333
0.0 0.0 179.0
0.0 0.0 -179.0
"""

# Golden output from pre-Task-2 code (commit a012cbb) with prepare_for_biped(KIMODO_STYLE, prune=("LeftEye",), speed=2.0)
# This proves backward compatibility for existing MAXScript caller.
PREPARE_GOLDEN_SPEED2 = """HIERARCHY
ROOT Hips
{
  OFFSET 0.000000 100.000000 0.000000
  CHANNELS 6 Xposition Yposition Zposition Zrotation Yrotation Xrotation
  JOINT Head
  {
    OFFSET 0.000000 20.000000 0.000000
    CHANNELS 3 Zrotation Yrotation Xrotation
    JOINT HeadEnd
    {
      OFFSET 0.000000 10.000000 0.000000
      CHANNELS 3 Zrotation Yrotation Xrotation
      End Site
      {
        OFFSET 0.000000 0.000000 0.000000
      }
    }
  }
}
MOTION
Frames: 2
Frame Time: 0.01666667
1.000000 99.000000 2.000000 89.000000 -4.900000 90.200000 1.000000 2.000000 3.000000 0.100000 0.200000 0.300000
1.100000 99.100000 2.100000 89.100000 -4.800000 90.300000 1.100000 2.100000 3.100000 0.200000 0.300000 0.400000
"""


def test_parse_kimodo_style() -> None:
    bvh = parse_bvh(KIMODO_STYLE)
    assert bvh.root.name == "Root"
    assert bvh.root.children[0].name == "Hips"
    assert len(bvh.frames) == 2
    assert len(bvh.frames[0]) == 21
    assert bvh.frame_time == pytest.approx(0.03333333)


def test_parse_rejects_bad_frame_width() -> None:
    broken = KIMODO_STYLE.replace(
        "0.0 0.0 0.0 0.0 -0.0 0.0 1.1 99.1 2.1 89.1 -4.8 90.3 1.1 2.1 3.1 0.6 0.6 0.6 0.2 0.3 0.4",
        "1.0 2.0",
    )
    with pytest.raises(ValueError, match="expected 21"):
        parse_bvh(broken)


def test_strip_static_root_promotes_hips() -> None:
    bvh = strip_static_root(parse_bvh(KIMODO_STYLE))
    assert bvh.root.name == "Hips"
    assert bvh.root.offset == (0.0, 100.0, 0.0)
    # first 6 (static Root) columns dropped
    assert len(bvh.frames[0]) == 15
    assert bvh.frames[0][0] == pytest.approx(1.0)
    assert bvh.frames[0][1] == pytest.approx(99.0)


def test_strip_static_root_keeps_animated_wrapper() -> None:
    animated = KIMODO_STYLE.replace(
        "0.0 0.0 0.0 0.0 -0.0 0.0 1.0", "5.0 0.0 0.0 0.0 -0.0 0.0 1.0"
    )
    bvh = strip_static_root(parse_bvh(animated))
    assert bvh.root.name == "Root"


def test_prune_joints_drops_subtree_and_columns() -> None:
    bvh = strip_static_root(parse_bvh(KIMODO_STYLE))
    pruned = prune_joints(bvh, ("LeftEye",))
    head = pruned.root.children[0]
    assert [c.name for c in head.children] == ["HeadEnd"]
    # LeftEye's 3 columns removed: 15 - 3 = 12
    assert len(pruned.frames[0]) == 12
    # HeadEnd values survive intact
    assert pruned.frames[0][-3:] == pytest.approx([0.1, 0.2, 0.3])


def test_prune_all_children_adds_end_site() -> None:
    bvh = strip_static_root(parse_bvh(KIMODO_STYLE))
    pruned = prune_joints(bvh, ("LeftEye", "HeadEnd"))
    head = pruned.root.children[0]
    assert head.children == []
    assert head.end_site == (2.0, 5.0, 3.0)


def test_roundtrip_reparses() -> None:
    bvh = parse_bvh(KIMODO_STYLE)
    again = parse_bvh(serialize_bvh(bvh))
    assert again.root.name == bvh.root.name
    for got, want in zip(again.frames, bvh.frames):
        assert got == pytest.approx(want)


def test_prepare_for_biped_end_to_end() -> None:
    out = prepare_for_biped(KIMODO_STYLE, prune=("Jaw", "LeftEye", "RightEye"))
    bvh = parse_bvh(out)
    assert bvh.root.name == "Hips"
    assert "LeftEye" not in out
    assert len(bvh.frames[0]) == 12


def test_rename_map_kimodo_names() -> None:
    names = {
        "Hips", "Spine1", "Spine2", "Chest", "Neck1", "Neck2", "Head",
        "LeftShoulder", "LeftArm", "LeftForeArm", "LeftHand",
        "LeftLeg", "LeftShin", "LeftFoot", "LeftToeBase",
    }
    m = _biped_rename_map(names)
    assert m["Spine1"] == "Chest"
    assert m["Spine2"] == "Chest2"
    assert m["Chest"] == "Chest3"
    assert m["Neck1"] == "Neck"
    assert m["Neck2"] == "Neck1"
    assert m["LeftShoulder"] == "LeftCollar"
    assert m["LeftArm"] == "LeftUpArm"
    assert m["LeftForeArm"] == "LeftLowArm"
    assert m["LeftLeg"] == "LeftUpLeg"
    assert m["LeftShin"] == "LeftLowLeg"
    assert m["LeftToeBase"] == "LeftToe"
    # renamed set has no collisions
    renamed = (names - m.keys()) | set(m.values())
    assert len(renamed) == len(names)


def test_rename_map_leaves_cs_names_alone() -> None:
    names = {
        "Hips", "Chest", "Chest2", "Neck", "Neck1", "Head",
        "LeftCollar", "LeftUpArm", "LeftLowArm", "LeftHand",
        "LeftUpLeg", "LeftLowLeg", "LeftFoot", "LeftToe",
    }
    assert _biped_rename_map(names) == {}


def test_rename_for_biped_rewrites_tree() -> None:
    bvh = rename_for_biped(strip_static_root(parse_bvh(KIMODO_STYLE)))
    # Hips -> Head chain: Head keeps its name, frames untouched
    assert bvh.root.name == "Hips"
    assert bvh.root.children[0].name == "Head"
    assert len(bvh.frames[0]) == 15


def test_offset_root_shifts_all_frames() -> None:
    out = prepare_for_biped(KIMODO_STYLE, offset=(240.0, 0.0, 0.0))
    bvh = parse_bvh(out)
    # 재중심으로 frame0 X 가 0 이 된 뒤 offset 이 얹힌다 (원본 1.0 / 1.1)
    assert bvh.frames[0][0] == pytest.approx(240.0)
    assert bvh.frames[1][0] == pytest.approx(240.1)
    # Y/Z untouched
    assert bvh.frames[0][1] == pytest.approx(99.0)


def test_retime_scales_frame_time() -> None:
    out = prepare_for_biped(KIMODO_STYLE, speed=2.0)
    bvh = parse_bvh(out)
    assert bvh.frame_time == pytest.approx(0.03333333 / 2.0)
    assert len(bvh.frames) == 2


def test_retime_rejects_zero_speed() -> None:
    with pytest.raises(ValueError, match="positive"):
        prepare_for_biped(KIMODO_STYLE, speed=0.0)


def test_trim_slices_frames() -> None:
    out = prepare_for_biped(KIMODO_STYLE, trim_range=(0.5, 1.0))
    bvh = parse_bvh(out)
    assert len(bvh.frames) == 1
    # 재중심(frame0 X 1.0 -> 0.0) 뒤 두 번째 원본 프레임이 남는다
    assert bvh.frames[0][0] == pytest.approx(0.1)


def test_trim_rejects_inverted_range() -> None:
    with pytest.raises(ValueError, match="invalid trim"):
        prepare_for_biped(KIMODO_STYLE, trim_range=(0.8, 0.2))


def test_has_upright_spine() -> None:
    assert has_upright_spine(KIMODO_STYLE)  # Head offset is Y-major
    x_major = KIMODO_STYLE.replace(
        "OFFSET 0.0 20.0 0.0", "OFFSET 20.0 0.5 0.0"
    )
    assert not has_upright_spine(x_major)


def test_unwrap_angles_crosses_180() -> None:
    # 179 -> -179 는 -358 도 이동이 아니라 +2 도 이동이다
    assert unwrap_angles([179.0, -179.0]) == pytest.approx([179.0, 181.0])


def test_unwrap_angles_passes_through_smooth_run() -> None:
    assert unwrap_angles([0.0, 10.0, 20.0]) == pytest.approx([0.0, 10.0, 20.0])


def test_unwrap_angles_empty() -> None:
    assert unwrap_angles([]) == []


def test_warp_identity_returns_original_frames() -> None:
    bvh = parse_bvh(KIMODO_STYLE)
    out = warp(bvh, [0.0, 1.0])
    assert out.frames == bvh.frames
    assert out.frame_time == bvh.frame_time


def test_warp_halves_frame_count() -> None:
    bvh = parse_bvh(KIMODO_STYLE)
    out = warp(bvh, [0.0])
    assert len(out.frames) == 1
    assert out.frames[0] == bvh.frames[0]


def test_warp_interpolates_midpoint() -> None:
    bvh = parse_bvh(KIMODO_STYLE)
    out = warp(bvh, [0.0, 0.5, 1.0])
    assert len(out.frames) == 3
    # 6번 컬럼(Hips Xposition)은 1.0 -> 1.1 이므로 중간은 1.05
    assert out.frames[1][6] == pytest.approx(1.05)


def test_warp_rejects_decreasing_time_map() -> None:
    bvh = parse_bvh(KIMODO_STYLE)
    with pytest.raises(ValueError, match="non-decreasing"):
        warp(bvh, [1.0, 0.0])


def test_warp_rejects_empty_time_map() -> None:
    bvh = parse_bvh(KIMODO_STYLE)
    with pytest.raises(ValueError, match="empty"):
        warp(bvh, [])


def test_warp_clamps_out_of_range() -> None:
    bvh = parse_bvh(KIMODO_STYLE)
    out = warp(bvh, [-5.0, 99.0])
    assert out.frames[0] == bvh.frames[0]
    assert out.frames[1] == bvh.frames[-1]


def test_unwrap_angles_multi_wrap_same_direction() -> None:
    # Verify accumulated offset exceeds 360° with repeated wraps in same direction.
    # Raw deltas: +170 (no wrap), -190 (wrap, offset +=360), +170 (no wrap),
    # -190 (wrap, offset +=360). Accumulated offset reaches 720 by the end.
    # Input: [0, 170, -20, 150, -40]
    # Expected: [0, 170, 340, 510, 680] (each value offset by accumulated 360k)
    assert unwrap_angles([0.0, 170.0, -20.0, 150.0, -40.0]) == pytest.approx(
        [0.0, 170.0, 340.0, 510.0, 680.0]
    )


def test_warp_interpolates_rotation_crossing_180() -> None:
    # Rotation channel crossing ±180° must unwrap before interpolation.
    # Zrotation goes 179 -> -179 (short path is +2°, not -358°).
    # Midpoint of unwrapped [179, 181] at t=0.5 is 180° (continuous).
    bvh = parse_bvh(ROTATION_CROSSING)
    out = warp(bvh, [0.0, 0.5, 1.0])
    assert len(out.frames) == 3
    # Column 2 is Zrotation; at t=0.5 it should interpolate the unwrapped
    # value (179 + 181) / 2 = 180, NOT the raw (179 + (-179)) / 2 = 0.
    assert out.frames[1][2] == pytest.approx(180.0)


def test_prepare_golden_backward_compat() -> None:
    # 회귀 방어: 기존 호출자(bvh_biped_ui.ms)의 출력이 프리-Task2 코드의 바이트와 정확히 일치해야 한다.
    out = prepare_for_biped(KIMODO_STYLE, prune=("LeftEye",), speed=2.0, recenter=False)
    assert out == PREPARE_GOLDEN_SPEED2


def test_prepare_without_time_map_is_deterministic() -> None:
    # time_map=None 일 때 결정적(deterministic)이어야 한다.
    baseline = prepare_for_biped(KIMODO_STYLE, prune=("LeftEye",), speed=2.0)
    with_none = prepare_for_biped(
        KIMODO_STYLE, prune=("LeftEye",), speed=2.0, time_map=None
    )
    assert with_none == baseline


def test_prepare_with_time_map_resamples() -> None:
    out = prepare_for_biped(KIMODO_STYLE, time_map=[0.0, 0.5, 1.0])
    assert "Frames: 3" in out


def test_prepare_time_map_ignores_speed() -> None:
    # time_map 이 있으면 speed 는 적용되지 않는다 (frame_time 유지)
    out = prepare_for_biped(KIMODO_STYLE, speed=4.0, time_map=[0.0, 1.0])
    assert "Frame Time: 0.03333333" in out


def test_wrap_static_root_is_the_inverse_of_stripping_it() -> None:
    """원점 루트는 임포트 때 다시 벗겨져야 한다 — 안 벗겨지면 CS 가 파일을 거부한다."""
    stripped = strip_static_root(parse_bvh(KIMODO_STYLE))
    wrapped = wrap_static_root(stripped)

    assert wrapped.root.name == "Root"
    assert wrapped.root.offset == (0.0, 0.0, 0.0)
    assert [c.name for c in wrapped.root.children] == ["Hips"]
    # 채널이 전 프레임 0 이어야 strip 이 다시 걸린다
    assert all(v == 0.0 for row in wrapped.frames for v in row[:6])
    assert len(wrapped.frames[0]) == len(stripped.frames[0]) + 6

    back = strip_static_root(wrapped)
    assert back.root.name == "Hips"
    assert back.root.offset == stripped.root.offset
    assert back.frames == stripped.frames


def test_wrap_static_root_leaves_an_existing_root_alone() -> None:
    """두 번 씌우면 Root 위에 Root 가 생겨 계층만 깊어진다."""
    already = parse_bvh(KIMODO_STYLE)
    assert wrap_static_root(already) is already


def test_recenter_ground_moves_frame0_to_the_origin() -> None:
    """시작점이 원점이어야 배치 간격이 원점 기준이 된다. 높이는 데이터다."""
    bvh = strip_static_root(parse_bvh(KIMODO_STYLE))
    # 궤적을 원점 밖으로 옮겨 둔다
    for row in bvh.frames:
        row[0] += 7.0   # Xposition
        row[2] -= 3.0   # Zposition
    moved = recenter_ground(bvh)

    x0 = moved.root.offset[0] + moved.frames[0][0]
    z0 = moved.root.offset[2] + moved.frames[0][2]
    assert x0 == pytest.approx(0.0, abs=1e-9)
    assert z0 == pytest.approx(0.0, abs=1e-9)
    # 평행이동이다 — 프레임 간 이동량은 그대로
    dx = [b[0] - a[0] for a, b in zip(bvh.frames, moved.frames)]
    assert all(d == pytest.approx(dx[0]) for d in dx)
    # 높이(Y)는 손대지 않는다
    assert [r[1] for r in moved.frames] == [r[1] for r in bvh.frames]


def test_prepare_for_biped_starts_at_the_origin() -> None:
    out = prepare_for_biped(KIMODO_STYLE, prune=())
    bvh = parse_bvh(out)
    ix = bvh.root.channels.index("Xposition")
    iz = bvh.root.channels.index("Zposition")
    assert bvh.root.offset[0] + bvh.frames[0][ix] == pytest.approx(0.0, abs=1e-9)
    assert bvh.root.offset[2] + bvh.frames[0][iz] == pytest.approx(0.0, abs=1e-9)


# ---- 크기 정규화 (rig_height / scale_bvh / prepare_for_biped(target_height=)) ----
# 게임에서 뽑은 BVH 는 미터로(엘렌 1.2, 레미엘 1.1~2.3), Kimodo 는 센티미터로
# (176.4) 나온다. biped.loadMocapFile 은 바이패드를 파일 치수에 맞추므로, 그대로
# 넣으면 1.2 짜리 바이패드가 생긴다 — 임포트 전에 키를 맞춰야 한다.

SCALE_SRC = """HIERARCHY
ROOT Hips
{
  OFFSET 0.0 1.0 0.0
  CHANNELS 6 Xposition Yposition Zposition Zrotation Xrotation Yrotation
  JOINT Head
  {
    OFFSET 0.0 0.6 0.0
    CHANNELS 3 Zrotation Xrotation Yrotation
    End Site
    {
      OFFSET 0.0 0.4 0.0
    }
  }
}
MOTION
Frames: 2
Frame Time: 0.033333
0.0 1.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0
2.0 1.5 -1.0 0.0 0.0 0.0 0.0 0.0 0.0
"""


def test_rig_height_measures_the_rest_skeleton() -> None:
    # 이 픽스처엔 다리가 없다. 엉덩이(1.0) 에서 머리끝(1.0+0.6+0.4=2.0) 까지
    # 세로 범위가 1.0 이다 — 실제 리그는 발이 엉덩이 아래로 내려가 그 범위가
    # 곧 발끝~머리끝 키가 된다(엘렌 1.2, Kimodo 176.4 를 이 식으로 쟀다).
    bvh = bvh_mod.parse_bvh(SCALE_SRC)
    assert bvh_mod.rig_height(bvh) == pytest.approx(1.0)


def test_scale_bvh_scales_offsets_and_translation_together() -> None:
    """뼈 길이만 키우고 이동을 안 키우면 발이 미끄러진다 — 둘은 같이 간다."""
    bvh = bvh_mod.scale_bvh(bvh_mod.parse_bvh(SCALE_SRC), 100.0)
    assert bvh.root.offset[1] == pytest.approx(100.0)
    assert bvh.root.children[0].offset[1] == pytest.approx(60.0)
    assert bvh.root.children[0].end_site[1] == pytest.approx(40.0)
    # 루트 이동 채널도 같은 배율
    assert bvh.frames[1][0] == pytest.approx(200.0)
    assert bvh.frames[1][1] == pytest.approx(150.0)
    assert bvh.frames[1][2] == pytest.approx(-100.0)


def test_scale_bvh_leaves_rotations_alone() -> None:
    src = SCALE_SRC.replace(
        "2.0 1.5 -1.0 0.0 0.0 0.0 0.0 0.0 0.0",
        "2.0 1.5 -1.0 30.0 0.0 0.0 45.0 0.0 0.0",
    )
    bvh = bvh_mod.scale_bvh(bvh_mod.parse_bvh(src), 100.0)
    assert bvh.frames[1][3] == pytest.approx(30.0)  # 루트 Zrotation
    assert bvh.frames[1][6] == pytest.approx(45.0)  # Head Zrotation


def test_prepare_for_biped_scales_a_metre_rig_up_to_the_target() -> None:
    """엘렌·레미엘(미터)이 170 짜리 바이패드로 들어오게 하는 경로."""
    out = prepare_for_biped(SCALE_SRC, target_height=170.0)
    scaled = bvh_mod.parse_bvh(out)
    assert bvh_mod.rig_height(scaled) == pytest.approx(170.0, rel=1e-6)


def test_prepare_for_biped_leaves_size_alone_without_a_target() -> None:
    # 기존 호출자는 그대로 동작해야 한다
    out = prepare_for_biped(SCALE_SRC)
    assert bvh_mod.rig_height(bvh_mod.parse_bvh(out)) == pytest.approx(1.0)


def test_target_height_does_not_scale_the_placement_offset() -> None:
    """배치 오프셋은 씬 단위다 — 파일 배율에 딸려 커지면 자리가 어긋난다."""
    out = prepare_for_biped(SCALE_SRC, offset=(50.0, 0.0, 0.0), target_height=170.0)
    scaled = bvh_mod.parse_bvh(out)
    # 0프레임에서 재중심된 뒤 오프셋만 얹히므로 X 는 정확히 50 이다
    assert scaled.frames[0][0] == pytest.approx(50.0)


def test_rig_height_of_zero_is_left_untouched() -> None:
    """키를 못 재는 파일(전부 0)을 0 으로 나누지 않는다."""
    flat = SCALE_SRC.replace("OFFSET 0.0 1.0 0.0", "OFFSET 0.0 0.0 0.0").replace(
        "OFFSET 0.0 0.6 0.0", "OFFSET 0.0 0.0 0.0"
    ).replace("OFFSET 0.0 0.4 0.0", "OFFSET 0.0 0.0 0.0")
    out = prepare_for_biped(flat, target_height=170.0)
    assert bvh_mod.rig_height(bvh_mod.parse_bvh(out)) == pytest.approx(0.0)


# 게임 추출 FBX 스타일: 조인트마다 위치 채널이 붙고, 그 상수가 OFFSET 과 어긋난다.
GAME_FBX_STYLE = """HIERARCHY
ROOT Hips
{
  OFFSET 0.0 1.0 0.0
  CHANNELS 6 Xposition Yposition Zposition Zrotation Yrotation Xrotation
  JOINT Chest
  {
    OFFSET 0.0 2.0 0.0
    CHANNELS 6 Xposition Yposition Zposition Zrotation Yrotation Xrotation
    JOINT LeftHand
    {
      OFFSET 3.0 0.0 0.0
      CHANNELS 6 Xposition Yposition Zposition Zrotation Yrotation Xrotation
      End Site
      {
        OFFSET 0.0 -1.0 0.0
      }
    }
  }
}
MOTION
Frames: 2
Frame Time: 0.033333
0 5 0 0 0 0  0 2 0 1 0 0  1 1 2 3 0 0
0 6 0 0 0 0  0 2 0 2 0 0  1 1 2 4 0 0
"""


def test_fold_constant_positions_moves_constant_into_offset() -> None:
    folded = fold_constant_positions(parse_bvh(GAME_FBX_STYLE))

    chest = folded.root.children[0]
    hand = chest.children[0]

    # 상수 위치가 OFFSET 을 대신한다 — 손은 (3,0,0) 이 아니라 실제 바인드 (1,1,2).
    assert hand.offset == pytest.approx((1.0, 1.0, 2.0))
    assert chest.offset == pytest.approx((0.0, 2.0, 0.0))
    # 접힌 조인트는 회전만 남는다.
    assert hand.channels == ["Zrotation", "Yrotation", "Xrotation"]
    assert chest.channels == ["Zrotation", "Yrotation", "Xrotation"]


def test_fold_constant_positions_keeps_root_motion() -> None:
    folded = fold_constant_positions(parse_bvh(GAME_FBX_STYLE))

    # 루트는 움직이는 채널이라 그대로 둔다.
    assert folded.root.channels[:3] == ["Xposition", "Yposition", "Zposition"]
    assert folded.root.offset == pytest.approx((0.0, 1.0, 0.0))
    assert [row[1] for row in folded.frames] == pytest.approx([5.0, 6.0])
    # 회전 키는 한 프레임도 잃지 않는다.
    assert [row[-3] for row in folded.frames] == pytest.approx([3.0, 4.0])


def test_fold_constant_positions_leaves_standard_bvh_alone() -> None:
    standard = parse_bvh(KIMODO_STYLE)
    assert fold_constant_positions(standard) is standard


def test_prepare_for_biped_folds_offsets() -> None:
    out = prepare_for_biped(GAME_FBX_STYLE, recenter=False)

    # 비루트 조인트에는 위치 채널이 남지 않는다 — 루트 하나만 6채널이다.
    assert out.count("CHANNELS 6") == 1
    assert out.count("CHANNELS 3") == 2
