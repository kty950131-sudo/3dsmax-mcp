import pytest

from src.helpers.bvh import (
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
)

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
    # root Xposition is channel 0; original values 1.0 / 1.1
    assert bvh.frames[0][0] == pytest.approx(241.0)
    assert bvh.frames[1][0] == pytest.approx(241.1)
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
    # second source frame survives (root stripped: first value is Hips X 1.1)
    assert bvh.frames[0][0] == pytest.approx(1.1)


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
    out = prepare_for_biped(KIMODO_STYLE, prune=("LeftEye",), speed=2.0)
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
