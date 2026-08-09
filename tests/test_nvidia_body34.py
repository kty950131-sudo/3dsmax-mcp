import json
from pathlib import Path

import pytest

from src.helpers.bvh import parse_bvh
from src.nvidia.body34 import (
    BODY34_JOINTS,
    body34_to_bvh,
    convert_body34_file,
    load_body34,
)


def _reference_positions() -> dict[str, list[float]]:
    positions = {name: [0.0, 1.0, 0.0] for name in BODY34_JOINTS}
    positions.update(
        {
            "pelvis": [0.0, 1.0, 0.0],
            "torso": [0.0, 1.25, 0.0],
            "neck": [0.0, 1.55, 0.0],
            "left_shoulder": [0.25, 1.5, 0.0],
            "left_elbow": [0.55, 1.5, 0.0],
            "left_wrist": [0.85, 1.5, 0.0],
            "right_shoulder": [-0.25, 1.5, 0.0],
            "right_elbow": [-0.55, 1.5, 0.0],
            "right_wrist": [-0.85, 1.5, 0.0],
            "left_hip": [0.12, 0.95, 0.0],
            "left_knee": [0.12, 0.55, 0.0],
            "left_ankle": [0.12, 0.1, 0.0],
            "left_big_toe": [0.12, 0.05, 0.18],
            "right_hip": [-0.12, 0.95, 0.0],
            "right_knee": [-0.12, 0.55, 0.0],
            "right_ankle": [-0.12, 0.1, 0.0],
            "right_big_toe": [-0.12, 0.05, 0.18],
        }
    )
    return positions


def _payload() -> dict[str, object]:
    identity_joints = {
        name: {"rotation_xyzw": [0.0, 0.0, 0.0, 1.0], "confidence": 1.0}
        for name in BODY34_JOINTS
    }
    return {
        "schema": "artoke.nvidia-body34.v1",
        "source_video": "C:/clips/dodge.mp4",
        "fps": 60.0,
        "reference_pose": _reference_positions(),
        "frames": [
            {"index": 0, "root_translation": [0.0, 0.0, 0.0], "joints": identity_joints},
            {"index": 1, "root_translation": [0.01, 0.0, 0.0], "joints": identity_joints},
        ],
    }


def _write_payload(path: Path, payload: dict[str, object] | None = None) -> Path:
    path.write_text(json.dumps(payload or _payload()), encoding="utf-8")
    return path


def test_rejects_wrong_schema(tmp_path: Path) -> None:
    path = _write_payload(tmp_path / "bad.json", {"schema": "wrong", "frames": []})

    with pytest.raises(ValueError, match="schema"):
        load_body34(path)


def test_identity_motion_exports_biped_hierarchy(tmp_path: Path) -> None:
    text = body34_to_bvh(load_body34(_write_payload(tmp_path / "body.json")))

    assert text.startswith("HIERARCHY\nROOT Hips")
    assert "JOINT Chest" in text
    assert "JOINT LeftUpArm" in text
    assert "Frames: 2" in text
    assert "Frame Time: 0.01666667" in text


def test_generated_bvh_reparses_and_preserves_root_translation(tmp_path: Path) -> None:
    text = body34_to_bvh(load_body34(_write_payload(tmp_path / "body.json")))

    parsed = parse_bvh(text)

    assert len(parsed.frames) == 2
    assert parsed.frame_time == pytest.approx(1 / 60)
    assert parsed.frames[1][0] == pytest.approx(1.0)


def test_explicit_zero_confidence_joint_carries_previous_rotation(tmp_path: Path) -> None:
    payload = _payload()
    frames = payload["frames"]
    assert isinstance(frames, list)
    frames[1]["root_translation"] = [0.0, 0.0, 0.0]
    frames[0]["joints"]["left_elbow"]["rotation_xyzw"] = [0.0, 0.0, 0.70710678, 0.70710678]
    frames[1]["joints"]["left_elbow"] = {
        "rotation_xyzw": [0.0, 0.0, 0.0, 0.0],
        "confidence": 0.0,
    }

    parsed = parse_bvh(
        body34_to_bvh(load_body34(_write_payload(tmp_path / "body.json", payload)))
    )

    assert parsed.frames[1] == pytest.approx(parsed.frames[0], abs=1e-5)


def test_convert_body34_file_writes_valid_bvh(tmp_path: Path) -> None:
    source = _write_payload(tmp_path / "body.json")
    output = tmp_path / "clip_nvidia_tpose.bvh"

    frame_count = convert_body34_file(source, output)

    assert frame_count == 2
    assert parse_bvh(output.read_text(encoding="utf-8")).frame_time == pytest.approx(1 / 60)
