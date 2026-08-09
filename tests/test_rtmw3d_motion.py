import json
from pathlib import Path

import pytest

from src.helpers.bvh import parse_bvh
from src.rtmw3d.motion import BODY23_NAMES, convert_rtmw3d_file, load_rtmw3d, rtmw3d_to_bvh


def _pose() -> dict[str, list[float]]:
    pose = {name: [0.0, 1.0, 0.0] for name in BODY23_NAMES}
    pose.update({
        "nose": [0.0, 1.75, 0.0],
        "left_shoulder": [0.25, 1.5, 0.0], "right_shoulder": [-0.25, 1.5, 0.0],
        "left_elbow": [0.55, 1.5, 0.0], "right_elbow": [-0.55, 1.5, 0.0],
        "left_wrist": [0.85, 1.5, 0.0], "right_wrist": [-0.85, 1.5, 0.0],
        "left_hip": [0.12, 1.0, 0.0], "right_hip": [-0.12, 1.0, 0.0],
        "left_knee": [0.12, 0.55, 0.0], "right_knee": [-0.12, 0.55, 0.0],
        "left_ankle": [0.12, 0.1, 0.0], "right_ankle": [-0.12, 0.1, 0.0],
        "left_big_toe": [0.12, 0.05, 0.2], "right_big_toe": [-0.12, 0.05, 0.2],
    })
    return pose


def _payload() -> dict:
    first = _pose()
    second = {name: [p[0] + 0.1, p[1], p[2]] for name, p in first.items()}
    return {
        "schema": "artoke.rtmw3d.v1", "source_video": "clip.mp4", "fps": 30.0,
        "frames": [
            {"index": 0, "keypoints": first, "scores": {name: 1.0 for name in BODY23_NAMES}},
            {"index": 1, "keypoints": second, "scores": {name: 1.0 for name in BODY23_NAMES}},
        ],
    }


def _payload_with_image_points() -> dict:
    payload = _payload()
    payload["image_size"] = {"width": 1920, "height": 1080}
    for frame in payload["frames"]:
        frame["image_keypoints"] = {
            name: [float(index * 10), float(index * 5)]
            for index, name in enumerate(BODY23_NAMES)
        }
    return payload


def test_identity_pose_exports_biped_bvh(tmp_path: Path) -> None:
    source = tmp_path / "motion.json"
    source.write_text(json.dumps(_payload()), encoding="utf-8")

    parsed = parse_bvh(rtmw3d_to_bvh(load_rtmw3d(source)))

    assert parsed.root.name == "Hips"
    assert len(parsed.frames) == 2
    assert parsed.frame_time == pytest.approx(1 / 30)
    assert parsed.frames[1][0] == pytest.approx(10.0)


def test_converter_writes_valid_bvh(tmp_path: Path) -> None:
    source = tmp_path / "motion.json"
    output = tmp_path / "motion_rtmw3d_tpose.bvh"
    source.write_text(json.dumps(_payload()), encoding="utf-8")

    assert convert_rtmw3d_file(source, output) == 2
    assert parse_bvh(output.read_text(encoding="utf-8")).root.name == "Hips"


def test_loader_retains_image_space_keypoints(tmp_path: Path) -> None:
    source = tmp_path / "motion.json"
    source.write_text(json.dumps(_payload_with_image_points()), encoding="utf-8")

    motion = load_rtmw3d(source)

    assert motion.image_size == (1920, 1080)
    assert motion.frames[0].image_keypoints is not None
    assert motion.frames[0].image_keypoints[0] == (0.0, 0.0)


def test_loader_rejects_partial_image_space_data(tmp_path: Path) -> None:
    payload = _payload_with_image_points()
    del payload["frames"][1]["image_keypoints"]
    source = tmp_path / "motion.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="every frame"):
        load_rtmw3d(source)
