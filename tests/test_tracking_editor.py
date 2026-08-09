import json
from pathlib import Path

import pytest

from src.helpers.bvh import parse_bvh
from src.rtmw3d.motion import BODY23_NAMES
from src.ui.studio.tracking_editor import TrackingSession


def _pose(offset: float = 0.0) -> dict[str, list[float]]:
    points = {name: [offset, 1.0, 0.25] for name in BODY23_NAMES}
    points.update({
        "nose": [offset, 1.75, 0.25],
        "left_shoulder": [offset + 0.25, 1.5, 0.25],
        "right_shoulder": [offset - 0.25, 1.5, 0.25],
        "left_elbow": [offset + 0.55, 1.4, 0.25],
        "right_elbow": [offset - 0.55, 1.4, 0.25],
        "left_wrist": [offset + 0.85, 1.3, 0.25],
        "right_wrist": [offset - 0.85, 1.3, 0.25],
        "left_hip": [offset + 0.12, 1.0, 0.25],
        "right_hip": [offset - 0.12, 1.0, 0.25],
        "left_knee": [offset + 0.12, 0.55, 0.25],
        "right_knee": [offset - 0.12, 0.55, 0.25],
        "left_ankle": [offset + 0.12, 0.1, 0.25],
        "right_ankle": [offset - 0.12, 0.1, 0.25],
        "left_big_toe": [offset + 0.12, 0.05, 0.45],
        "right_big_toe": [offset - 0.12, 0.05, 0.45],
    })
    return points


def _write_motion(path: Path, frame_count: int = 4, image_points: bool = True) -> None:
    frames = []
    for index in range(frame_count):
        frame = {
            "index": index,
            "keypoints": _pose(index * 0.1),
            "scores": {name: 0.8 for name in BODY23_NAMES},
        }
        if image_points:
            frame["image_keypoints"] = {
                name: [100.0 + joint * 3 + index, 200.0 + joint * 2]
                for joint, name in enumerate(BODY23_NAMES)
            }
        frames.append(frame)
    payload = {
        "schema": "artoke.rtmw3d.v1",
        "source_video": str(path.with_suffix(".mp4")),
        "fps": 30.0,
        "frames": frames,
    }
    if image_points:
        payload["image_size"] = {"width": 1920, "height": 1080}
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_manual_edit_preserves_depth_confidence_and_source(tmp_path: Path) -> None:
    source = tmp_path / "clip_rtmw3d.json"
    _write_motion(source)
    original = source.read_bytes()
    session = TrackingSession.open(source)
    original_z = session.frame(0)["keypoints"]["left_wrist"][2]

    result = session.set_point(0, "left_wrist", 120.0, 80.0)

    frame = session.frame(0)
    assert result["kind"] == "manual"
    assert frame["image_keypoints"]["left_wrist"] == [120.0, 80.0]
    assert frame["keypoints"]["left_wrist"] == [120.0, -80.0, original_z]
    assert frame["scores"]["left_wrist"] == 0.8
    assert source.read_bytes() == original


@pytest.mark.parametrize("joint", ["", "unknown"])
def test_manual_edit_rejects_unknown_joint(tmp_path: Path, joint: str) -> None:
    source = tmp_path / "clip_rtmw3d.json"
    _write_motion(source)
    session = TrackingSession.open(source)

    with pytest.raises(ValueError, match="joint"):
        session.set_point(0, joint, 1.0, 2.0)


def test_manual_edit_rejects_invalid_frame_and_coordinate(tmp_path: Path) -> None:
    source = tmp_path / "clip_rtmw3d.json"
    _write_motion(source)
    session = TrackingSession.open(source)

    with pytest.raises(IndexError, match="frame"):
        session.set_point(99, "left_wrist", 1.0, 2.0)
    with pytest.raises(ValueError, match="finite"):
        session.set_point(0, "left_wrist", float("nan"), 2.0)


def test_copy_reset_and_linear_propagation(tmp_path: Path) -> None:
    source = tmp_path / "clip_rtmw3d.json"
    _write_motion(source)
    session = TrackingSession.open(source)
    original = session.frame(0)["image_keypoints"]["left_wrist"]
    session.set_point(0, "left_wrist", original[0] + 30.0, original[1] - 12.0)

    propagated = session.propagate(0, 3, "left_wrist")

    assert propagated == {"updated": [1, 2, 3], "stopped_at": None}
    for index, expected in enumerate(((30.0, -12.0), (20.0, -8.0), (10.0, -4.0), (0.0, 0.0))):
        point = session.frame(index)["image_keypoints"]["left_wrist"]
        source_point = 100.0 + BODY23_NAMES.index("left_wrist") * 3 + index, 200.0 + BODY23_NAMES.index("left_wrist") * 2
        assert point == pytest.approx([source_point[0] + expected[0], source_point[1] + expected[1]])

    session.copy_to_next(0, "left_wrist")
    assert session.frame(1)["edit_kinds"]["left_wrist"] == "copied"
    session.reset_point(1, "left_wrist")
    assert "left_wrist" not in session.frame(1)["edit_kinds"]


def test_propagation_stops_before_manual_edit(tmp_path: Path) -> None:
    source = tmp_path / "clip_rtmw3d.json"
    _write_motion(source)
    session = TrackingSession.open(source)
    start = session.frame(0)["image_keypoints"]["left_wrist"]
    session.set_point(0, "left_wrist", start[0] + 30.0, start[1] - 12.0)
    session.set_point(2, "left_wrist", 500.0, 400.0)

    result = session.propagate(0, 3, "left_wrist")

    assert result == {"updated": [1], "stopped_at": 2}
    assert session.frame(2)["image_keypoints"]["left_wrist"] == [500.0, 400.0]


def test_save_writes_edited_json_bvh_and_trace_without_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "clip_rtmw3d.json"
    _write_motion(source, frame_count=2)
    original = source.read_bytes()
    session = TrackingSession.open(source)
    session.set_point(0, "left_wrist", 120.0, 80.0)

    result = session.save(tmp_path / "library")

    edited_json = Path(result["rtmw3d_path"])
    edited_bvh = Path(result["bvh_path"])
    trace = json.loads(Path(result["trace_path"]).read_text(encoding="utf-8"))
    assert edited_json.name == "clip_rtmw3d_edited.json"
    assert edited_bvh.name == "clip_rtmw3d_edited_tpose.bvh"
    assert parse_bvh(edited_bvh.read_text(encoding="utf-8")).frames
    assert trace["edit_count"] == 1
    assert source.read_bytes() == original


def test_legacy_motion_can_open_but_cannot_save(tmp_path: Path) -> None:
    source = tmp_path / "legacy_rtmw3d.json"
    _write_motion(source, image_points=False)
    session = TrackingSession.open(source)

    assert session.frame(0)["image_keypoints"]["left_wrist"]
    with pytest.raises(RuntimeError, match="re-extract"):
        session.save(tmp_path)
