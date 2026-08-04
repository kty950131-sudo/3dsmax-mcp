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


def test_has_upright_spine() -> None:
    assert has_upright_spine(KIMODO_STYLE)  # Head offset is Y-major
    x_major = KIMODO_STYLE.replace(
        "OFFSET 0.0 20.0 0.0", "OFFSET 20.0 0.5 0.0"
    )
    assert not has_upright_spine(x_major)
