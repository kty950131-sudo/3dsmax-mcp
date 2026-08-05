import json
import os

import pytest

from src.helpers.bvh import parse_bvh
from src.ui.studio.library import cache_path
from src.ui.studio.skeleton import bones, fk, project
from src.ui.studio.thumb import build_pose_data, load_pose_data, sample_indices
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


# Hips branches into two independent arms, each with its own child joint and
# its own 3 rotation channels. Only LeftArm carries a rotation, so a broken
# traversal that misaligns sibling subtrees with the motion-row column order
# (Task 4 review finding: fk() was never tested against branching data) would
# make LeftHand miss its rotation, or make either hand read the wrong
# sibling's channels entirely.
BRANCHING = """HIERARCHY
ROOT Hips
{
  OFFSET 0.0 0.0 0.0
  CHANNELS 6 Xposition Yposition Zposition Zrotation Yrotation Xrotation
  JOINT LeftArm
  {
    OFFSET -5.0 0.0 0.0
    CHANNELS 3 Zrotation Yrotation Xrotation
    JOINT LeftHand
    {
      OFFSET -3.0 0.0 0.0
      CHANNELS 3 Zrotation Yrotation Xrotation
      End Site
      {
        OFFSET -1.0 0.0 0.0
      }
    }
  }
  JOINT RightArm
  {
    OFFSET 5.0 0.0 0.0
    CHANNELS 3 Zrotation Yrotation Xrotation
    JOINT RightHand
    {
      OFFSET 3.0 0.0 0.0
      CHANNELS 3 Zrotation Yrotation Xrotation
      End Site
      {
        OFFSET 1.0 0.0 0.0
      }
    }
  }
}
MOTION
Frames: 1
Frame Time: 0.033333
0.0 0.0 0.0 0.0 0.0 0.0 90.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0
"""


def test_fk_handles_branching_hierarchy() -> None:
    # Column order (pre-order, children in list order):
    #   Hips[0:6] LeftArm[6:9] LeftHand[9:12] RightArm[12:15] RightHand[15:18]
    # Hand-derived by walking fk()'s own algorithm:
    #   Hips: identity rotation, zero translation -> (0, 0, 0)
    #   LeftArm: offset (-5,0,0) under Hips's identity rotation -> (-5, 0, 0)
    #     (a joint's own rotation channels affect its CHILDREN, not itself)
    #   LeftHand: offset (-3,0,0) rotated 90deg about Z (LeftArm's channel):
    #     Rz(90) . (-3,0,0) = (0*-3 + -1*0, 1*-3 + 0*0, 0) = (0, -3, 0)
    #     plus LeftArm's world pos (-5,0,0) -> (-5, -3, 0)
    #   RightArm: offset (5,0,0), no rotation anywhere above it -> (5, 0, 0)
    #   RightHand: offset (3,0,0), unrotated -> (5,0,0)+(3,0,0) = (8, 0, 0)
    bvh = parse_bvh(BRANCHING)
    pos = fk(bvh, 0)
    assert pos["Hips"] == pytest.approx((0.0, 0.0, 0.0))
    assert pos["LeftArm"] == pytest.approx((-5.0, 0.0, 0.0))
    assert pos["LeftHand"] == pytest.approx((-5.0, -3.0, 0.0))
    assert pos["RightArm"] == pytest.approx((5.0, 0.0, 0.0))
    assert pos["RightHand"] == pytest.approx((8.0, 0.0, 0.0))


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


from src.ui.studio.library import Clip, cache_path, extract_tags, scan


def test_extract_tags_splits_on_underscore() -> None:
    assert extract_tags("artoke_spin-kick") == ("artoke", "spin-kick")


def test_extract_tags_drops_numeric_suffix() -> None:
    assert extract_tags("attack-combo_00") == ("attack-combo",)


def test_extract_tags_single_token() -> None:
    assert extract_tags("run2") == ("run2",)


def test_scan_excludes_biped_conversions(tmp_path) -> None:
    (tmp_path / "run2.bvh").write_text("x", encoding="utf-8")
    (tmp_path / "run2_biped.bvh").write_text("x", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    clips = scan(str(tmp_path))
    assert [c.stem for c in clips] == ["run2"]


def test_scan_sorts_by_stem(tmp_path) -> None:
    for name in ("zebra.bvh", "alpha.bvh"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    assert [c.stem for c in scan(str(tmp_path))] == ["alpha", "zebra"]


def test_scan_missing_folder_returns_empty() -> None:
    assert scan("Z:/definitely/not/here") == []


def test_cache_path_is_outside_library(tmp_path) -> None:
    clip = str(tmp_path / "run2.bvh")
    out = cache_path(clip, str(tmp_path / "cache"))
    assert out.endswith(".json")
    assert "cache" in out


def test_cache_path_differs_per_clip(tmp_path) -> None:
    a = cache_path(str(tmp_path / "a.bvh"), "C:/cache")
    b = cache_path(str(tmp_path / "b.bvh"), "C:/cache")
    assert a != b


def test_compat_raises_clear_error_without_pyside() -> None:
    import importlib

    try:
        import PySide6  # noqa: F401
    except ImportError:
        try:
            import PySide2  # noqa: F401
        except ImportError:
            with pytest.raises(ImportError, match="3ds Max"):
                importlib.import_module("src.ui.studio.compat")
            return
    pytest.skip("PySide 가 있는 환경 — Max 내부에서 확인한다")


# Tests for _pick_max_window helper
class FakeWidget:
    """Duck-type widget for testing widget selection."""

    def __init__(
        self,
        class_name: str | None = None,
        parent_obj: "FakeWidget | None" = None,
        is_window_val: bool = True,
        inherits_qmainwindow: bool = False,
        raise_on_meta: bool = False,
    ) -> None:
        self.class_name = class_name
        self.parent_obj = parent_obj
        self.is_window_val = is_window_val
        self.inherits_qmainwindow = inherits_qmainwindow
        self.raise_on_meta = raise_on_meta

    def parent(self) -> "FakeWidget | None":
        return self.parent_obj

    def metaObject(self) -> "FakeMetaObject":
        if self.raise_on_meta:
            raise RuntimeError("Deleted C++ object")
        return FakeMetaObject(self.class_name)

    def isWindow(self) -> bool:
        return self.is_window_val

    def inherits(self, class_name: str) -> bool:
        if class_name == "QMainWindow":
            return self.inherits_qmainwindow
        return False


class FakeMetaObject:
    """Duck-type metaObject for testing."""

    def __init__(self, class_name: str | None) -> None:
        self.class_name = class_name

    def className(self) -> str:
        if self.class_name is None:
            raise RuntimeError("Null metaObject")
        return self.class_name


def test_pick_max_window_finds_qmax_application_window() -> None:
    from src.ui.studio.winpick import _pick_max_window

    widgets = [
        FakeWidget(class_name="QmaxApplicationWindow"),
    ]
    result = _pick_max_window(widgets)
    assert result is widgets[0]


def test_pick_max_window_finds_plain_qmainwindow_when_qmax_absent() -> None:
    from src.ui.studio.winpick import _pick_max_window

    widgets = [
        FakeWidget(class_name="SomeOtherWindow"),
        FakeWidget(class_name="QMainWindow", inherits_qmainwindow=True),
    ]
    result = _pick_max_window(widgets)
    assert result is widgets[1]


def test_pick_max_window_returns_none_when_neither_present() -> None:
    from src.ui.studio.winpick import _pick_max_window

    widgets = [
        FakeWidget(class_name="SomeDialog"),
        FakeWidget(class_name="AnotherDialog"),
    ]
    result = _pick_max_window(widgets)
    assert result is None


def test_pick_max_window_skips_widget_with_parent() -> None:
    from src.ui.studio.winpick import _pick_max_window

    parent = FakeWidget(class_name="Parent")
    widgets = [
        FakeWidget(class_name="QmaxApplicationWindow", parent_obj=parent),
        FakeWidget(class_name="QMainWindow", inherits_qmainwindow=True),
    ]
    result = _pick_max_window(widgets)
    assert result is widgets[1]


def test_pick_max_window_handles_deleted_c_object() -> None:
    from src.ui.studio.winpick import _pick_max_window

    widgets = [
        FakeWidget(raise_on_meta=True),  # Will raise on metaObject()
        FakeWidget(class_name="QMainWindow", inherits_qmainwindow=True),
    ]
    result = _pick_max_window(widgets)
    assert result is widgets[1]


def test_pick_max_window_prefers_qmax_over_qmainwindow() -> None:
    from src.ui.studio.winpick import _pick_max_window

    qmax_window = FakeWidget(class_name="QmaxApplicationWindow")
    qmain_window = FakeWidget(class_name="QMainWindow", inherits_qmainwindow=True)
    widgets = [qmax_window, qmain_window]
    result = _pick_max_window(widgets)
    assert result is qmax_window


def test_pick_max_window_empty_list_returns_none() -> None:
    from src.ui.studio.winpick import _pick_max_window

    result = _pick_max_window([])
    assert result is None


def test_sample_indices_spreads_evenly() -> None:
    assert sample_indices(12, 12) == list(range(12))


def test_sample_indices_downsamples() -> None:
    out = sample_indices(100, 12)
    assert len(out) == 12
    assert out[0] == 0
    assert out[-1] == 99
    assert all(b > a for a, b in zip(out, out[1:]))


def test_sample_indices_short_clip() -> None:
    assert sample_indices(3, 12) == [0, 1, 2]


def test_sample_indices_single_frame() -> None:
    assert sample_indices(1, 12) == [0]


def test_sample_indices_rejects_non_positive_total() -> None:
    with pytest.raises(ValueError, match="positive"):
        sample_indices(0, 12)


def test_build_pose_data_returns_expected_shape(tmp_path) -> None:
    clip = tmp_path / "two_joint.bvh"
    clip.write_text(TWO_JOINT, encoding="utf-8")
    data = build_pose_data(str(clip))
    assert data["frames"] == 2
    assert data["frame_time"] == pytest.approx(0.033333)
    assert list(data["bones"]) == [("Hips", "Head")]
    assert len(data["poses"]) == 2  # sample_indices(2, 12) == [0, 1]
    assert data["poses"][0]["Hips"] == pytest.approx([0.0, 0.0, 0.0])
    assert data["poses"][1]["Hips"] == pytest.approx([0.0, 100.0, 0.0])
    assert len(data["bounds"]) == 4


def test_build_pose_data_survives_json_roundtrip(tmp_path) -> None:
    # The cache stores this dict via json.dump / json.load. Tuples (bones
    # pairs) become lists on the way back out, so nothing downstream may
    # depend on tuple identity or a tuple/list type distinction.
    clip = tmp_path / "two_joint.bvh"
    clip.write_text(TWO_JOINT, encoding="utf-8")
    data = build_pose_data(str(clip))
    reloaded = json.loads(json.dumps(data))
    assert reloaded["bones"] == [list(pair) for pair in data["bones"]]
    assert reloaded["poses"] == data["poses"]
    assert reloaded["bounds"] == data["bounds"]
    assert reloaded["frames"] == data["frames"]


def test_load_pose_data_writes_cache_file(tmp_path) -> None:
    clip = tmp_path / "two_joint.bvh"
    clip.write_text(TWO_JOINT, encoding="utf-8")
    cache_dir = tmp_path / "cache"
    data = load_pose_data(str(clip), str(cache_dir))
    assert os.path.exists(cache_path(str(clip), str(cache_dir)))
    assert data["frames"] == 2


def test_load_pose_data_reuses_cache_when_clip_unchanged(tmp_path) -> None:
    clip = tmp_path / "two_joint.bvh"
    clip.write_text(TWO_JOINT, encoding="utf-8")
    cache_dir = tmp_path / "cache"
    load_pose_data(str(clip), str(cache_dir))
    stored = cache_path(str(clip), str(cache_dir))
    # Tamper with the cache file directly. If the second call reuses the
    # cache verbatim (rather than recomputing), this sentinel comes back.
    with open(stored, encoding="utf-8") as handle:
        cached = json.load(handle)
    cached["frames"] = 999
    with open(stored, "w", encoding="utf-8") as handle:
        json.dump(cached, handle)
    data = load_pose_data(str(clip), str(cache_dir))
    assert data["frames"] == 999


def test_load_pose_data_recomputes_when_clip_mtime_changes(tmp_path) -> None:
    clip = tmp_path / "two_joint.bvh"
    clip.write_text(TWO_JOINT, encoding="utf-8")
    cache_dir = tmp_path / "cache"
    load_pose_data(str(clip), str(cache_dir))
    stored = cache_path(str(clip), str(cache_dir))
    with open(stored, encoding="utf-8") as handle:
        cached = json.load(handle)
    cached["frames"] = 999
    with open(stored, "w", encoding="utf-8") as handle:
        json.dump(cached, handle)
    # Bump the clip's mtime so the stale cache is rejected.
    future = os.path.getmtime(str(clip)) + 5
    os.utime(str(clip), (future, future))
    data = load_pose_data(str(clip), str(cache_dir))
    assert data["frames"] == 2
