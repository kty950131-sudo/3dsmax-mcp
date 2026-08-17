import json
import os

import pytest
from pathlib import Path

from maxmcp.helpers.bvh import parse_bvh
from maxmcp.ui.studio.library import cache_path
from maxmcp.ui.studio.skeleton import bones, fk, project
from maxmcp.ui.studio.thumb import (
    PLAYBACK_FRAMES,
    build_pose_data,
    load_pose_data,
    pose_distance,
    pose_vector,
    poster_index,
)
from maxmcp.ui.studio.timemap import build_time_map

STUDIO_PAGE = Path(__file__).parents[1] / "maxmcp" / "ui" / "studio" / "web" / "studio_draft.html"


def test_studio_page_exposes_video_import_controls() -> None:
    html = STUDIO_PAGE.read_text(encoding="utf-8")
    assert 'data-action="add-video"' in html
    assert 'class="video-job-card"' in html
    assert "start_video_job" in html
    assert "video_job_status" in html

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

# 핵심 포즈 선택용. 1번 프레임은 0번과 거의 같고(Xrot 2도) 2번만 크게 다르다(Xrot 90도).
# 균등 간격이면 1번이 뽑히지만, 최원점 표집이면 2번이 뽑혀야 한다.
THREE_FRAME_ONE_EXTREME = """HIERARCHY
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
Frames: 3
Frame Time: 0.033333
0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0
0.0 0.0 0.0 0.0 0.0 2.0 0.0 0.0 0.0
0.0 0.0 0.0 0.0 0.0 90.0 0.0 0.0 0.0
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


from maxmcp.ui.studio.library import Clip, cache_path, extract_tags, scan


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
                importlib.import_module("maxmcp.ui.studio.compat")
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
    from maxmcp.ui.studio.winpick import _pick_max_window

    widgets = [
        FakeWidget(class_name="QmaxApplicationWindow"),
    ]
    result = _pick_max_window(widgets)
    assert result is widgets[0]


def test_pick_max_window_finds_plain_qmainwindow_when_qmax_absent() -> None:
    from maxmcp.ui.studio.winpick import _pick_max_window

    widgets = [
        FakeWidget(class_name="SomeOtherWindow"),
        FakeWidget(class_name="QMainWindow", inherits_qmainwindow=True),
    ]
    result = _pick_max_window(widgets)
    assert result is widgets[1]


def test_pick_max_window_returns_none_when_neither_present() -> None:
    from maxmcp.ui.studio.winpick import _pick_max_window

    widgets = [
        FakeWidget(class_name="SomeDialog"),
        FakeWidget(class_name="AnotherDialog"),
    ]
    result = _pick_max_window(widgets)
    assert result is None


def test_pick_max_window_skips_widget_with_parent() -> None:
    from maxmcp.ui.studio.winpick import _pick_max_window

    parent = FakeWidget(class_name="Parent")
    widgets = [
        FakeWidget(class_name="QmaxApplicationWindow", parent_obj=parent),
        FakeWidget(class_name="QMainWindow", inherits_qmainwindow=True),
    ]
    result = _pick_max_window(widgets)
    assert result is widgets[1]


def test_pick_max_window_handles_deleted_c_object() -> None:
    from maxmcp.ui.studio.winpick import _pick_max_window

    widgets = [
        FakeWidget(raise_on_meta=True),  # Will raise on metaObject()
        FakeWidget(class_name="QMainWindow", inherits_qmainwindow=True),
    ]
    result = _pick_max_window(widgets)
    assert result is widgets[1]


def test_pick_max_window_prefers_qmax_over_qmainwindow() -> None:
    from maxmcp.ui.studio.winpick import _pick_max_window

    qmax_window = FakeWidget(class_name="QmaxApplicationWindow")
    qmain_window = FakeWidget(class_name="QMainWindow", inherits_qmainwindow=True)
    widgets = [qmax_window, qmain_window]
    result = _pick_max_window(widgets)
    assert result is qmax_window


def test_pick_max_window_empty_list_returns_none() -> None:
    from maxmcp.ui.studio.winpick import _pick_max_window

    result = _pick_max_window([])
    assert result is None


def test_pose_vector_is_root_relative() -> None:
    # 같은 자세를 100 만큼 옮겨도 벡터는 같아야 한다 (이동 성분 제거)
    a = {"Hips": (0.0, 0.0, 0.0), "Head": (0.0, 10.0, 0.0)}
    b = {"Hips": (100.0, 0.0, 0.0), "Head": (100.0, 10.0, 0.0)}
    assert pose_vector(a) == pytest.approx(pose_vector(b))


def test_pose_vector_orders_joints_by_name() -> None:
    # 삽입 순서가 달라도 같은 벡터가 나와야 한다
    a = {"Hips": (0.0, 0.0, 0.0), "Aaa": (1.0, 2.0, 3.0)}
    assert pose_vector(a) == pytest.approx((1.0, 2.0, 3.0, 0.0, 0.0, 0.0))


def test_pose_vector_empty() -> None:
    assert pose_vector({}) == ()


def test_pose_distance_zero_for_same_pose() -> None:
    v = pose_vector({"Hips": (0.0, 0.0, 0.0), "Head": (0.0, 10.0, 0.0)})
    assert pose_distance(v, v) == pytest.approx(0.0)


def test_pose_distance_is_squared_euclidean() -> None:
    assert pose_distance((0.0, 0.0), (3.0, 4.0)) == pytest.approx(25.0)


def test_poster_index_picks_the_outlier_pose() -> None:
    # 0,1 번은 거의 같고 2 번만 크게 다르다 -> 정지 썸네일은 2 번이어야 한다
    rest = {"Hips": (0.0, 0.0, 0.0), "Head": (0.0, 10.0, 0.0)}
    near = {"Hips": (0.0, 0.0, 0.0), "Head": (0.1, 10.0, 0.0)}
    far = {"Hips": (0.0, 0.0, 0.0), "Head": (0.0, 0.0, 10.0)}
    assert poster_index([rest, near, far]) == 2


def test_poster_index_empty() -> None:
    assert poster_index([]) == 0


def test_poster_index_single_pose() -> None:
    assert poster_index([{"Hips": (0.0, 0.0, 0.0)}]) == 0


def test_poster_index_ignores_translation() -> None:
    # 같은 자세가 멀리 이동한 것뿐이면 대표 프레임이 되지 않는다
    rest = {"Hips": (0.0, 0.0, 0.0), "Head": (0.0, 10.0, 0.0)}
    moved = {"Hips": (500.0, 0.0, 0.0), "Head": (500.0, 10.0, 0.0)}
    bent = {"Hips": (0.0, 0.0, 0.0), "Head": (0.0, 0.0, 10.0)}
    assert poster_index([rest, moved, bent]) == 2


def test_playback_frames_are_evenly_spaced(tmp_path) -> None:
    # 재생이 부드러우려면 균등 간격이어야 한다 (극점 표집이면 튄다)
    clip = tmp_path / "three.bvh"
    clip.write_text(THREE_FRAME_ONE_EXTREME, encoding="utf-8")
    data = build_pose_data(str(clip))
    assert len(data["poses"]) == 3  # 3 프레임 클립은 전부 쓴다
    assert 0 <= data["poster"] < len(data["poses"])


def test_build_pose_data_returns_expected_shape(tmp_path) -> None:
    clip = tmp_path / "two_joint.bvh"
    clip.write_text(TWO_JOINT, encoding="utf-8")
    data = build_pose_data(str(clip))
    assert data["frames"] == 2
    assert data["frame_time"] == pytest.approx(0.033333)
    assert list(data["bones"]) == [("Hips", "Head")]
    assert len(data["poses"]) == 2  # key_pose_indices(2 프레임) == [0, 1]
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


# ---- 기존 바이패드 리타깃 (retarget_clip) ----
# maxbridge 는 pymxs 전용이지만 _rt() 를 통해서만 런타임을 만지므로
# 가짜 런타임을 꽂아 Max 밖에서도 분기 로직을 검증할 수 있다.

from unittest.mock import MagicMock

from maxmcp.ui.studio import maxbridge


def _fake_rt(load_ok: bool = True, figure_mode: bool = False) -> MagicMock:
    rt = MagicMock()
    rt.classOf.return_value = rt.Vertical_Horizontal_Turn
    controller = rt.getTMController.return_value
    controller.figureMode = figure_mode
    controller.mixerMode = False
    rt.biped.loadMocapFile.return_value = load_ok
    rt.biped.numLayers.return_value = 0
    rt.getNodeByName.return_value.name = "Bip_walk"
    return rt


def test_studio_page_exposes_retarget_controls() -> None:
    html = STUDIO_PAGE.read_text(encoding="utf-8")
    assert 'data-field="target"' in html
    assert "retarget_clip" in html
    assert "새 바이패드 생성" in html


def test_retarget_clip_requires_existing_biped(tmp_path, monkeypatch) -> None:
    src = tmp_path / "clip.bvh"
    src.write_text(TWO_JOINT, encoding="utf-8")
    rt = _fake_rt()
    rt.getNodeByName.return_value = None
    monkeypatch.setattr(maxbridge, "_rt", lambda: rt)
    msg = maxbridge.retarget_clip(str(src), "Bip_none", convert=False)
    assert msg.startswith("ERROR")
    rt.biped.loadMocapFile.assert_not_called()


def test_retarget_clip_loads_onto_existing_controller(tmp_path, monkeypatch) -> None:
    src = tmp_path / "clip.bvh"
    src.write_text(TWO_JOINT, encoding="utf-8")
    rt = _fake_rt()
    monkeypatch.setattr(maxbridge, "_rt", lambda: rt)
    msg = maxbridge.retarget_clip(str(src), "Bip_walk", convert=False)
    assert msg.startswith("OK")
    rt.biped.createNew.assert_not_called()
    rt.biped.loadMocapFile.assert_called_once()
    rt.delete.assert_not_called()


def test_retarget_clip_rejects_figure_mode(tmp_path, monkeypatch) -> None:
    src = tmp_path / "clip.bvh"
    src.write_text(TWO_JOINT, encoding="utf-8")
    rt = _fake_rt(figure_mode=True)
    monkeypatch.setattr(maxbridge, "_rt", lambda: rt)
    msg = maxbridge.retarget_clip(str(src), "Bip_walk", convert=False)
    assert msg.startswith("ERROR")
    rt.biped.loadMocapFile.assert_not_called()


def test_retarget_clip_keeps_biped_on_load_failure(tmp_path, monkeypatch) -> None:
    src = tmp_path / "clip.bvh"
    src.write_text(TWO_JOINT, encoding="utf-8")
    rt = _fake_rt(load_ok=False)
    monkeypatch.setattr(maxbridge, "_rt", lambda: rt)
    msg = maxbridge.retarget_clip(str(src), "Bip_walk", convert=False)
    assert msg.startswith("ERROR")
    # 기존 바이패드는 사용자 소유다 — 실패해도 지우지 않는다 (import_clip 과 다른 점)
    rt.delete.assert_not_called()


def test_retarget_clip_bakes_current_position_into_offset(tmp_path, monkeypatch) -> None:
    src = tmp_path / "clip.bvh"
    src.write_text(TWO_JOINT, encoding="utf-8")
    rt = _fake_rt()
    rt.getNodeByName.return_value.transform.position.x = 42.0
    seen: dict = {}

    def fake_convert(path, x_offset=0.0, speed=1.0, trim=(0.0, 1.0), time_map=None):
        seen["x_offset"] = x_offset
        return path, True

    monkeypatch.setattr(maxbridge, "convert_clip", fake_convert)
    monkeypatch.setattr(maxbridge, "_rt", lambda: rt)
    msg = maxbridge.retarget_clip(str(src), "Bip_walk", convert=True)
    assert msg.startswith("OK")
    # 제자리 유지: 대상 바이패드의 현재 X 를 변환 오프셋으로 굽는다
    assert seen["x_offset"] == 42.0


def test_curve_panels_use_figma_design_tokens() -> None:
    """커브 패널은 피그마 정본(BVH Studio / Tracking Editor) 디자인 언어를 따른다.

    스펙: docs/superpowers/specs/2026-08-10-bvh-studio-2d-tracking-editor-design.md
    — 배경 #08090B, 정상 #48A9C5, 선택 #F5B642, Noto Sans KR, Ease Out 160–240ms.
    """
    html = STUDIO_PAGE.read_text(encoding="utf-8").lower()
    assert "#08090b" in html
    assert "#f5b642" in html
    assert "noto sans kr" in html


# ---- 라이브러리 분류 (artoke-manifest.json 사이드카) ----

def test_scan_attaches_category_from_sidecar(tmp_path) -> None:
    (tmp_path / "artoke_run-jump.bvh").write_text(TWO_JOINT, encoding="utf-8")
    (tmp_path / "my-local-take.bvh").write_text(TWO_JOINT, encoding="utf-8")
    (tmp_path / "artoke-manifest.json").write_text(
        json.dumps(
            {
                "categories": [
                    {"slug": "locomotion", "label": "이동",
                     "subs": [{"slug": "run", "label": "달리기"}]}
                ],
                "motions": [
                    {"name": "run-jump.bvh", "category": "locomotion", "sub": "run"}
                ],
            }
        ),
        encoding="utf-8",
    )
    from maxmcp.ui.studio.library import scan

    clips = {c.stem: c for c in scan(str(tmp_path))}
    assert clips["artoke_run-jump"].category == "locomotion"
    assert clips["artoke_run-jump"].sub == "run"
    # 로컬에서 만든 클립은 매니페스트에 없다 — 미분류로 남는다
    assert clips["my-local-take"].category is None


def test_scan_without_sidecar_leaves_clips_unshelved(tmp_path) -> None:
    (tmp_path / "solo.bvh").write_text(TWO_JOINT, encoding="utf-8")
    from maxmcp.ui.studio.library import scan

    clips = scan(str(tmp_path))
    assert clips[0].category is None and clips[0].sub is None


def test_studio_page_groups_clips_by_category() -> None:
    html = STUDIO_PAGE.read_text(encoding="utf-8")
    assert "미분류" in html
    assert "clip-group" in html
    # list_clips 가 {clips, categories} 를 돌려주는 새 형태를 쓴다
    assert "data.clips" in html


# ---- 클립 삭제 (로컬 전용 — 사이트에는 아무 요청도 보내지 않는다) ----

def test_delete_clip_removes_bvh_and_biped_sibling(tmp_path) -> None:
    clip = tmp_path / "old-take.bvh"
    clip.write_text(TWO_JOINT, encoding="utf-8")
    (tmp_path / "old-take_biped.bvh").write_text(TWO_JOINT, encoding="utf-8")
    from maxmcp.ui.studio.library import delete_clip

    out = delete_clip(str(tmp_path), str(clip))
    assert sorted(out["removed"]) == ["old-take.bvh", "old-take_biped.bvh"]
    assert not clip.exists()
    assert not (tmp_path / "old-take_biped.bvh").exists()


def test_delete_clip_refuses_paths_outside_folder(tmp_path) -> None:
    outside = tmp_path / "outside.bvh"
    outside.write_text(TWO_JOINT, encoding="utf-8")
    library_dir = tmp_path / "lib"
    library_dir.mkdir()
    from maxmcp.ui.studio.library import delete_clip

    with pytest.raises(ValueError):
        delete_clip(str(library_dir), str(outside))
    assert outside.exists()


def test_delete_clip_refuses_non_bvh(tmp_path) -> None:
    target = tmp_path / "artoke-manifest.json"
    target.write_text("{}", encoding="utf-8")
    from maxmcp.ui.studio.library import delete_clip

    with pytest.raises(ValueError):
        delete_clip(str(tmp_path), str(target))
    assert target.exists()


def test_studio_page_exposes_delete_confirm() -> None:
    html = STUDIO_PAGE.read_text(encoding="utf-8")
    assert "정말로 삭제하겠습니다" in html
    assert "delete_clip" in html
    assert "contextmenu" in html
    assert "홈페이지에는 영향" in html


# ---- 제자리 표시 (가로 이동 고정) ----
# kimodo/SOMA 배치: Root 는 180프레임 내내 0 이고 Hips 가 이동을 들고 있다.
# 여기서는 X·Z 로 나아가면서 Y 로 한 번 튀어오른다 — 도약은 살아야 한다.
TRAVELLING = """HIERARCHY
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
      End Site
      {
        OFFSET 0.0 10.0 0.0
      }
    }
  }
}
MOTION
Frames: 3
Frame Time: 0.033333
0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0
0.0 0.0 0.0 0.0 0.0 0.0 250.0 30.0 500.0 0.0 0.0 0.0 0.0 0.0 0.0
0.0 0.0 0.0 0.0 0.0 0.0 500.0 0.0 1000.0 0.0 0.0 0.0 0.0 0.0 0.0
"""


def _travel_clip(tmp_path):
    clip = tmp_path / "travel.bvh"
    clip.write_text(TRAVELLING, encoding="utf-8")
    return str(clip)


def test_travel_joint_finds_the_carrier_not_the_root(tmp_path) -> None:
    """이동은 루트가 아니라 Hips 가 들고 있다 — 위치 채널을 보고 찾는다."""
    from maxmcp.helpers.bvh import parse_bvh
    from maxmcp.ui.studio.skeleton import travel_joint

    bvh = parse_bvh(TRAVELLING)
    assert travel_joint(bvh) == "Hips"


def test_build_pose_data_locks_horizontal_travel(tmp_path) -> None:
    data = build_pose_data(_travel_clip(tmp_path))
    xs = [pose["Hips"][0] for pose in data["poses"]]
    zs = [pose["Hips"][2] for pose in data["poses"]]
    assert max(xs) - min(xs) < 1e-6
    assert max(zs) - min(zs) < 1e-6


def test_build_pose_data_keeps_vertical_motion(tmp_path) -> None:
    """제자리로 붙들어도 도약은 남아야 한다 — 걷어내는 것은 가로 성분뿐이다."""
    data = build_pose_data(_travel_clip(tmp_path))
    ys = [pose["Hips"][1] for pose in data["poses"]]
    assert round(max(ys) - min(ys)) == 30


def test_build_pose_data_bounds_shrink_to_the_body(tmp_path) -> None:
    """이동을 걷어내야 바운즈가 몸 크기가 된다 — 아니면 캐릭터가 점처럼 그려진다."""
    data = build_pose_data(_travel_clip(tmp_path))
    min_x, _, max_x, _ = data["bounds"]
    assert max_x - min_x < 100


def test_build_pose_data_leaves_a_still_clip_alone(tmp_path) -> None:
    clip = tmp_path / "still.bvh"
    clip.write_text(TWO_JOINT, encoding="utf-8")
    data = build_pose_data(str(clip))
    assert len(data["poses"]) == 2


def test_studio_page_exposes_category_filter() -> None:
    html = STUDIO_PAGE.read_text(encoding="utf-8")
    assert 'data-field="category"' in html
    assert "전체" in html


def test_studio_header_splits_actions_from_filters() -> None:
    """헤더는 두 줄이다 — 하는 일(동작)과 보는 범위(필터)를 섞지 않는다."""
    html = STUDIO_PAGE.read_text(encoding="utf-8")
    assert 'class="topbar"' in html
    assert 'class="filterbar"' in html


def test_studio_page_shows_categories_as_chips() -> None:
    """카테고리는 드롭다운에 숨기지 않고 헤더에 펼쳐 둔다 (한 번 클릭)."""
    html = STUDIO_PAGE.read_text(encoding="utf-8")
    assert "cat-chip" in html
    assert "renderCategoryBar" in html
    # 드롭다운은 걷어낸다 — 같은 필터가 두 곳에 있으면 상태가 갈린다
    assert "<select data-field=\"category\"" not in html


def test_clip_card_shows_length_instead_of_repeating_the_name() -> None:
    """카드 둘째 줄은 파일명을 다시 쓰지 않는다 — 길이를 보여준다.

    태그는 stem 을 "_" 로 쪼갠 것이라(artoke_run-b -> artoke · run-b) 첫 줄과
    같은 정보였다. 길이는 클립을 고를 때 실제로 필요한 정보다.
    """
    html = STUDIO_PAGE.read_text(encoding="utf-8")
    assert "clipMeta" in html
    assert 'clip.tags.join(" · ")' not in html


def test_side_panel_scrolls_so_import_controls_are_reachable() -> None:
    """오른쪽 열이 창보다 길면 스크롤한다 — 임포트 버튼이 잘리면 못 쓴다."""
    html = STUDIO_PAGE.read_text(encoding="utf-8")
    side = html.split(".side {", 1)[1].split("}", 1)[0]
    assert "overflow-y" in side


def test_studio_page_can_collapse_the_curve_editors() -> None:
    """곡선은 접힌다 — 창 높이의 20%를 상시 먹는데 클립을 고르는 동안은 할 일이 없다."""
    html = STUDIO_PAGE.read_text(encoding="utf-8")
    assert 'data-action="toggle-curves"' in html
    assert "curve-panel" in html
    # 접은 선택은 기억한다. 매번 다시 접어야 하면 접는 의미가 없다.
    assert "localStorage" in html


def test_collapsed_curves_advertise_a_speed_curve_that_is_not_identity() -> None:
    """접어도 속도 곡선이 임포트에 걸려 있으면 보여야 한다.

    speedPoints 는 time_map 으로 임포트에 실제로 전달된다. 접어서 안 보이면
    "왜 이 클립만 느리게 들어오지"의 원인을 찾을 방법이 사라진다.
    """
    html = STUDIO_PAGE.read_text(encoding="utf-8")
    assert "curve-summary" in html
    assert "curveSummary" in html


def test_timeline_is_not_collapsible() -> None:
    """타임라인은 40px 뿐이고 트림 상태를 늘 보여준다 — 숨길 이유가 없다."""
    html = STUDIO_PAGE.read_text(encoding="utf-8")
    assert 'data-action="toggle-timeline"' not in html


# ---- 팔 간격 곡선 연결 (임포트와 같은 동작으로 적용) ----

def test_import_clip_applies_arm_space_to_the_biped_it_made(tmp_path, monkeypatch) -> None:
    """속도 곡선처럼 팔 간격도 임포트 한 번으로 걸린다.

    바이패드 이름을 JS 로 되돌려 다시 호출하게 하면, 이름이 겹치거나 임포트가
    부분 실패했을 때 엉뚱한 바이패드에 레이어가 얹힌다. 만든 자리에서 건다.
    """
    src = tmp_path / "clip.bvh"
    src.write_text(TWO_JOINT, encoding="utf-8")
    rt = _fake_rt()
    monkeypatch.setattr(maxbridge, "_rt", lambda: rt)
    msg = maxbridge.import_clip(
        str(src), "Bip_x", convert=False, x_offset=0.0, arm_points=[(0, 12.0), (30, 20.0)]
    )
    assert msg.startswith("OK")
    rt.biped.createLayer.assert_called_once()
    assert rt.biped.addNewKey.call_count == 4  # 좌우 팔 × 키 2개


def test_import_clip_without_arm_points_touches_no_layer(tmp_path, monkeypatch) -> None:
    src = tmp_path / "clip.bvh"
    src.write_text(TWO_JOINT, encoding="utf-8")
    rt = _fake_rt()
    monkeypatch.setattr(maxbridge, "_rt", lambda: rt)
    maxbridge.import_clip(str(src), "Bip_x", convert=False, x_offset=0.0)
    rt.biped.createLayer.assert_not_called()


def test_import_clip_ignores_a_flat_zero_arm_curve(tmp_path, monkeypatch) -> None:
    """곡선을 0 에 둔 채 임포트하면 레이어를 만들지 않는다 — 기본값이 곧 '안 씀'이다."""
    src = tmp_path / "clip.bvh"
    src.write_text(TWO_JOINT, encoding="utf-8")
    rt = _fake_rt()
    monkeypatch.setattr(maxbridge, "_rt", lambda: rt)
    maxbridge.import_clip(
        str(src), "Bip_x", convert=False, x_offset=0.0, arm_points=[(0, 0.0), (30, 0.0)]
    )
    rt.biped.createLayer.assert_not_called()


def test_retarget_clip_applies_arm_space_too(tmp_path, monkeypatch) -> None:
    src = tmp_path / "clip.bvh"
    src.write_text(TWO_JOINT, encoding="utf-8")
    rt = _fake_rt()
    monkeypatch.setattr(maxbridge, "_rt", lambda: rt)
    msg = maxbridge.retarget_clip(str(src), "Bip_walk", convert=False, arm_points=[(0, 15.0)])
    assert msg.startswith("OK")
    rt.biped.createLayer.assert_called_once()


def test_studio_page_sends_the_arm_curve_with_the_import() -> None:
    html = STUDIO_PAGE.read_text(encoding="utf-8")
    assert "arm_points" in html
    assert "armKeysFromCurve" in html
    # 팔 간격도 이제 임포트에 실제로 걸리므로 접힘 요약이 알려야 한다
    assert "팔 간격" in html.split("function curveSummary")[1].split("\n}")[0]


def test_studio_page_links_blend_preview() -> None:
    """블렌드 검수는 사이트 뷰어가 본체다 — 스튜디오는 여는 버튼만 단다(스펙)."""
    html = STUDIO_PAGE.read_text(encoding="utf-8")
    assert 'data-action="blend-preview"' in html
    assert "viewer?blend=locomotion" in html
    assert "open_external" in html


def test_studio_page_exposes_arbitrary_direction_import() -> None:
    """45도 간격 여덟 개로는 30도를 줄 수 없다 — 굳혀서 넣는 컨트롤이 있어야 한다."""
    html = STUDIO_PAGE.read_text(encoding="utf-8")
    assert 'data-action="blend-import"' in html
    assert 'data-field="blend-angle"' in html
    assert 'data-field="blend-speed"' in html
    assert "bake_blend" in html
    assert "blend_tiers" in html


def test_studio_blend_import_reuses_the_normal_import_path() -> None:
    """굳힌 파일도 같은 importPath 를 타야 트림·미러·대상·배치 간격이 갈리지 않는다."""
    html = STUDIO_PAGE.read_text(encoding="utf-8")
    assert html.count("function importPath(") == 1
    # 클립 임포트와 블렌드 임포트, 두 곳에서 부른다
    assert html.count("importPath(") == 3
    # 새 임포트 경로를 만들지 않았다는 증거: retarget/import 호출이 그 함수 안에만 있다
    body = html.split("function importPath(")[1].split("\nfunction ")[0]
    assert 'bcall("retarget_clip"' in body
    assert 'bcall("import_clip"' in body
    assert html.count('bcall("retarget_clip"') == 1
    assert html.count('bcall("import_clip"') == 1


def test_studio_blend_import_does_not_reuse_the_source_timeline_edits() -> None:
    """트림·커브는 6초 소스에 대해 만든 값이라 한 사이클 결과에 걸면 대부분을 잘라낸다."""
    html = STUDIO_PAGE.read_text(encoding="utf-8")
    handler = html.split('action === "blend-import"')[1].split('action === "blend-preview"')[0]
    assert "trim_start" not in handler
    assert "time_map" not in handler
    assert "arm_points" not in handler


def test_studio_page_says_why_blending_is_unavailable() -> None:
    """세트나 위상이 없으면 조용히 감추지 않고 이유를 적는다."""
    html = STUDIO_PAGE.read_text(encoding="utf-8")
    body = html.split("function renderBlendControls")[1].split("\nfunction ")[0]
    assert "8방향 세트가 없어" in body
    assert "phase.json" in body
