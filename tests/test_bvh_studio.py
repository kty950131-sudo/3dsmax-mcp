import pytest

from src.helpers.bvh import parse_bvh
from src.ui.studio.skeleton import bones, fk, project
from src.ui.studio.timemap import build_time_map

TWO_JOINT = """HIERARCHY
ROOT Hips
{
  OFFSET 0.0 0.0 0.0
  CHANNELS 6 Xposition Yposition Zposition Zrotation Yrotation Xrotation
  JOINT Head
  {
    OFFSET 0.0 10.0 0.0
    CHANNELS 3 Zrotation Yrotation Xrotation
    End Site
    {
      OFFSET 0.0 5.0 0.0
    }
  }
}
MOTION
Frames: 2
Frame Time: 0.033333
0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0
0.0 100.0 0.0 0.0 0.0 0.0 0.0 0.0 90.0
"""

# 부모 회전이 자식 위치에 실제로 반영되는지, 그리고 여러 축 회전이
# 채널 선언 순서대로 합성되는지 확인하는 픽스처.
# Hips 는 Yrotation 다음 Xrotation 순서로 채널을 선언한다(하드코딩된
# XYZ/ZYX 순서였다면 이 값에서 어긋난다).
ROTATION_ORDER = """HIERARCHY
ROOT Hips
{
  OFFSET 0.0 0.0 0.0
  CHANNELS 2 Yrotation Xrotation
  JOINT Child
  {
    OFFSET 0.0 0.0 10.0
    End Site
    {
      OFFSET 0.0 0.0 0.0
    }
  }
}
MOTION
Frames: 1
Frame Time: 0.033333
90.0 90.0
"""


def test_fk_rest_pose_stacks_offsets() -> None:
    bvh = parse_bvh(TWO_JOINT)
    pos = fk(bvh, 0)
    assert pos["Hips"] == pytest.approx((0.0, 0.0, 0.0))
    assert pos["Head"] == pytest.approx((0.0, 10.0, 0.0))


def test_fk_applies_root_translation() -> None:
    bvh = parse_bvh(TWO_JOINT)
    pos = fk(bvh, 1)
    assert pos["Hips"] == pytest.approx((0.0, 100.0, 0.0))


def test_fk_composes_rotation_channels_in_declaration_order() -> None:
    # Yrotation 90 다음 Xrotation 90 을 그 순서대로 합성해야 한다.
    # 순서를 바꿔 합성하면(Rx*Ry) Child 는 (10, 0, 0) 이 되어 어긋난다.
    bvh = parse_bvh(ROTATION_ORDER)
    pos = fk(bvh, 0)
    assert pos["Child"] == pytest.approx((0.0, -10.0, 0.0))


def test_bones_lists_parent_child_pairs() -> None:
    bvh = parse_bvh(TWO_JOINT)
    assert bones(bvh.root) == [("Hips", "Head")]


def test_project_front_view_keeps_x_and_y() -> None:
    assert project((3.0, 7.0, 0.0), 0.0) == pytest.approx((3.0, 7.0))


def test_project_side_view_uses_z() -> None:
    x, y = project((3.0, 7.0, 5.0), 90.0)
    assert x == pytest.approx(5.0)
    assert y == pytest.approx(7.0)


def test_flat_curve_is_identity() -> None:
    # 출력과 원본이 1:1 이면 항등 사상
    assert build_time_map([(0.0, 0.0), (1.0, 1.0)], 5) == pytest.approx(
        [0.0, 1.0, 2.0, 3.0, 4.0]
    )


def test_half_speed_doubles_output_frames() -> None:
    # 출력 2배 길이 동안 원본 전체를 소비 = 절반 속도
    out = build_time_map([(0.0, 0.0), (2.0, 1.0)], 5)
    assert len(out) == 9
    assert out[0] == pytest.approx(0.0)
    assert out[-1] == pytest.approx(4.0)


def test_result_is_always_non_decreasing() -> None:
    out = build_time_map([(0.0, 0.0), (0.5, 0.1), (1.0, 1.0)], 20)
    assert all(b >= a for a, b in zip(out, out[1:]))


def test_rejects_decreasing_control_points() -> None:
    with pytest.raises(ValueError, match="non-decreasing"):
        build_time_map([(0.0, 0.0), (1.0, 0.5), (2.0, 0.2)], 5)


def test_rejects_fewer_than_two_points() -> None:
    with pytest.raises(ValueError, match="at least two"):
        build_time_map([(0.0, 0.0)], 5)


def test_rejects_empty_source() -> None:
    with pytest.raises(ValueError, match="src_frames"):
        build_time_map([(0.0, 0.0), (1.0, 1.0)], 0)


def test_rejects_degenerate_curve_no_horizontal_span() -> None:
    # FINDING 1: Curve with no horizontal span produces frozen clip
    # All points share the same x, so _sample always returns points[0][1]
    with pytest.raises(ValueError, match="advance in output time"):
        build_time_map([(0.5, 0.0), (0.5, 1.0)], 5)


def test_single_frame_source() -> None:
    # FINDING 2: src_frames == 1 produces [0.0]
    # When last_frame == 0, out_ratio is clamped to 0.0, and result * 0 = 0.0
    out = build_time_map([(0.0, 0.0), (1.0, 1.0)], 1)
    assert len(out) == 1
    assert out[0] == pytest.approx(0.0)


def test_rejects_x_going_backwards() -> None:
    # MINOR A: Validate bx < ax (x-axis decreasing)
    with pytest.raises(ValueError, match="non-decreasing"):
        build_time_map([(0.0, 0.0), (1.0, 0.5), (0.5, 1.0)], 5)


def test_below_range_clamp_when_points_not_at_zero() -> None:
    # MINOR B: Control points don't start at x=0
    # When _sample is called with x < points[0][0], it returns points[0][1]
    out = build_time_map([(0.5, 0.25), (1.0, 1.0)], 5)
    # For first frame (i=0), out_ratio = 0/4 = 0.0
    # _sample(points, 0.0): 0.0 <= 0.5? yes, return 0.25
    # result[0] = 0.25 * 4 = 1.0
    assert out[0] == pytest.approx(1.0)
    # For last frame, out_ratio = 4/4 = 1.0
    # _sample(points, 1.0): 1.0 >= 1.0? yes, return 1.0
    # result[-1] = 1.0 * 4 = 4.0
    assert out[-1] == pytest.approx(4.0)
