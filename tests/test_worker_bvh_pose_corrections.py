from __future__ import annotations

import json
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


# --- SOMA 18관절·ZYX (후처리를 거친 판) -------------------------------------
# 폴백만 알던 코드가 이 스켈레톤에서 통째로 터졌다(2026-08-28 진단). 두 판을 모두
# 받는지 여기서 못 박는다.
SOMA_BVH = """HIERARCHY
ROOT Hips
{
  OFFSET 0 0 0
  CHANNELS 6 Xposition Yposition Zposition Zrotation Yrotation Xrotation
  JOINT LeftArm
  {
    OFFSET 10 0 0
    CHANNELS 3 Zrotation Yrotation Xrotation
    JOINT LeftForeArm
    {
      OFFSET 20 0 0
      CHANNELS 3 Zrotation Yrotation Xrotation
      End Site
      {
        OFFSET 20 0 0
      }
    }
  }
}
MOTION
Frames: 2
Frame Time: 0.0333333
0 0 0 0 0 0 0 0 0 0 0 0
0 0 0 0 0 0 0 0 0 0 0 0
"""


def _write(tmp_path: Path, text: str, name: str = "source.bvh") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _quarter_turn_z() -> list[float]:
    half = math.radians(90.0) / 2.0
    return [0.0, 0.0, math.sin(half), math.cos(half)]


def test_applies_to_soma_skeleton_with_zyx_channels(tmp_path: Path) -> None:
    source = _write(tmp_path, SOMA_BVH)
    out = tmp_path / "corrected.bvh"
    apply_bvh_pose_corrections(
        source,
        [{"frame": 1, "joint": "left_elbow", "rotation": _quarter_turn_z()}],
        out,
    )
    bvh = parse_bvh(out.read_text(encoding="utf-8"))
    # LeftForeArm 의 Zrotation 은 루트 6 + LeftArm 3 = 9번째 열부터다.
    assert bvh.frames[1][9] == pytest.approx(90.0, abs=1e-6)
    assert bvh.frames[0][9] == pytest.approx(0.0, abs=1e-9)


def test_soma_skeleton_skips_joints_it_does_not_have(tmp_path: Path) -> None:
    """SOMA 18관절에는 발가락이 없다. 통째로 실패시키지 않고 건너뛰고 기록한다."""
    source = _write(tmp_path, SOMA_BVH)
    out = tmp_path / "corrected.bvh"
    apply_bvh_pose_corrections(
        source,
        [
            {"frame": 1, "joint": "left_toe", "rotation": _quarter_turn_z()},
            {"frame": 1, "joint": "left_elbow", "rotation": _quarter_turn_z()},
        ],
        out,
    )
    report = json.loads(
        out.with_suffix(".pose_corrections.json").read_text(encoding="utf-8")
    )
    assert report["skeleton"] == "soma"
    assert report["applied"] == 1
    assert any("left_toe" in reason for reason in report["skipped"])
    bvh = parse_bvh(out.read_text(encoding="utf-8"))
    assert bvh.frames[1][9] == pytest.approx(90.0, abs=1e-6)


def test_fallback_skeleton_is_still_recognised(tmp_path: Path) -> None:
    source = _write(tmp_path, BVH)
    out = tmp_path / "corrected.bvh"
    apply_bvh_pose_corrections(
        source,
        [{"frame": 1, "joint": "left_elbow", "rotation": _quarter_turn_z()}],
        out,
    )
    report = json.loads(
        out.with_suffix(".pose_corrections.json").read_text(encoding="utf-8")
    )
    assert report["skeleton"] == "fallback"
    assert report["applied"] == 1


def test_rejects_a_skeleton_it_does_not_know(tmp_path: Path) -> None:
    unknown = SOMA_BVH.replace("LeftArm", "Bone1").replace("LeftForeArm", "Bone2")
    source = _write(tmp_path, unknown)
    with pytest.raises(ValueError, match="unknown BVH skeleton"):
        apply_bvh_pose_corrections(
            source,
            [{"frame": 1, "joint": "left_elbow", "rotation": _quarter_turn_z()}],
            tmp_path / "corrected.bvh",
        )
