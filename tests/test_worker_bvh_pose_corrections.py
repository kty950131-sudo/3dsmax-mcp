from __future__ import annotations

import math
from pathlib import Path

import pytest

from maxmcp.helpers.bvh import parse_bvh
from maxmcp.worker.bvh_pose_corrections import apply_bvh_pose_corrections


BVH = """HIERARCHY
ROOT Hips
{
  OFFSET 0 0 0
  CHANNELS 6 Xposition Yposition Zposition Zrotation Xrotation Yrotation
  JOINT LeftUpArm
  {
    OFFSET 10 0 0
    CHANNELS 3 Zrotation Xrotation Yrotation
    JOINT LeftLowArm
    {
      OFFSET 20 0 0
      CHANNELS 3 Zrotation Xrotation Yrotation
      End Site
      {
        OFFSET 20 0 0
      }
    }
  }
}
MOTION
Frames: 2
Frame Time: 0.03333333
0 0 0 0 0 0 1 2 3 0 0 0
10 20 30 0 0 0 4 5 6 0 0 0
"""


def write_bvh(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "original.bvh"
    source.write_text(BVH, encoding="utf-8")
    return source, tmp_path / "corrected.bvh"


def test_changes_only_the_keyed_joint_and_frame(tmp_path: Path) -> None:
    source, output = write_bvh(tmp_path)
    original = parse_bvh(source.read_text(encoding="utf-8"))

    result = apply_bvh_pose_corrections(source, [{
        "frame": 1,
        "joint": "left_elbow",
        "rotation": [math.sqrt(0.5), 0.0, 0.0, math.sqrt(0.5)],
    }], output)

    corrected = parse_bvh(result.read_text(encoding="utf-8"))
    assert corrected.frames[0] == original.frames[0]
    assert corrected.frames[1][:9] == original.frames[1][:9]
    assert corrected.frames[1][9] == pytest.approx(0.0, abs=1e-6)
    assert corrected.frames[1][10] == pytest.approx(90.0, abs=1e-6)
    assert corrected.frames[1][11] == pytest.approx(0.0, abs=1e-6)
    assert source.read_text(encoding="utf-8") == BVH


def test_converts_root_translation_from_meters_to_centimeters(tmp_path: Path) -> None:
    source, output = write_bvh(tmp_path)

    apply_bvh_pose_corrections(source, [{
        "frame": 1,
        "joint": "root",
        "rotation": [0.0, 0.0, 0.0, 1.0],
        "translation": [1.0, 0.0, 0.0],
    }], output)

    corrected = parse_bvh(output.read_text(encoding="utf-8"))
    assert corrected.frames[1][:3] == [110.0, 20.0, 30.0]
    assert corrected.frames[0][:3] == [0.0, 0.0, 0.0]


def test_composes_quaternions_across_the_180_degree_boundary(tmp_path: Path) -> None:
    source, output = write_bvh(tmp_path)
    parsed = parse_bvh(source.read_text(encoding="utf-8"))
    parsed.frames[1][9] = 170.0
    from maxmcp.helpers.bvh import serialize_bvh
    source.write_text(serialize_bvh(parsed), encoding="utf-8")

    angle = math.radians(20.0) / 2.0
    apply_bvh_pose_corrections(source, [{
        "frame": 1,
        "joint": "left_elbow",
        "rotation": [0.0, 0.0, math.sin(angle), math.cos(angle)],
    }], output)

    corrected = parse_bvh(output.read_text(encoding="utf-8"))
    assert corrected.frames[1][9] == pytest.approx(-170.0, abs=1e-6)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        ({"frame": 2, "joint": "left_elbow", "rotation": [0, 0, 0, 1]}, "frame"),
        ({"frame": 0, "joint": "unknown", "rotation": [0, 0, 0, 1]}, "joint"),
        ({"frame": 0, "joint": "left_elbow", "rotation": [0, 0, 0, 0]}, "Quaternion"),
        ({"frame": 0, "joint": "left_elbow", "rotation": [0, math.nan, 0, 1]}, "finite"),
        ({"frame": 0, "joint": "left_elbow", "rotation": [0, 0, 0, 1], "translation": [1, 0, 0]}, "root"),
    ],
)
def test_rejects_invalid_pose_edits(
    tmp_path: Path,
    edit: dict[str, object],
    message: str,
) -> None:
    source, output = write_bvh(tmp_path)

    with pytest.raises(ValueError, match=message):
        apply_bvh_pose_corrections(source, [edit], output)

    assert not output.exists()


def test_rejects_duplicate_keys_and_source_overwrite(tmp_path: Path) -> None:
    source, output = write_bvh(tmp_path)
    edit = {"frame": 0, "joint": "left_elbow", "rotation": [0, 0, 0, 1]}

    with pytest.raises(ValueError, match="duplicate"):
        apply_bvh_pose_corrections(source, [edit, edit], output)
    with pytest.raises(ValueError, match="source"):
        apply_bvh_pose_corrections(source, [], source)
