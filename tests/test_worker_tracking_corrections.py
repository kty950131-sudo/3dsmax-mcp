import json
from pathlib import Path

import pytest

from maxmcp.rtmw3d.motion import BODY23_NAMES
from maxmcp.worker.tracking_corrections import (
    apply_tracking_corrections,
    load_correction_snapshot,
    read_subject_box,
)


def tracking_payload() -> dict[str, object]:
    keypoints = {
        # z 는 카메라 앞이 음수다(추출기가 [x, -y, -z] 로 적는다). 보정 변환이 깊이 -z 를 쓴다.
        joint: [float(index), float(index + 1), -float(index + 2)]
        for index, joint in enumerate(BODY23_NAMES)
    }
    image_keypoints = {
        joint: [float(index * 10), float(index * 5)]
        for index, joint in enumerate(BODY23_NAMES)
    }
    return {
        "schema": "artoke.rtmw3d.v1",
        "source_video": "walk.mp4",
        "fps": 30,
        "image_size": {"width": 1920, "height": 1080},
        "frames": [{
            "index": 0,
            "keypoints": keypoints,
            "image_keypoints": image_keypoints,
            "scores": {joint: 0.9 for joint in BODY23_NAMES},
        }],
    }


def write_inputs(tmp_path: Path, edits: object) -> tuple[Path, Path, Path]:
    source = tmp_path / "original.json"
    source.write_text(json.dumps(tracking_payload()), encoding="utf-8")
    edits_path = tmp_path / "edits.json"
    edits_path.write_text(json.dumps(edits), encoding="utf-8")
    return source, edits_path, tmp_path / "corrected.json"


def test_applies_body23_corrections_without_mutating_source(tmp_path: Path) -> None:
    source, edits, output = write_inputs(tmp_path, [{
        "frame": 0,
        "joint": "left_wrist",
        "x": 310.5,
        "y": 205.0,
        "state": "manual",
    }])
    original = source.read_bytes()

    snapshot = load_correction_snapshot(edits)
    result_path = apply_tracking_corrections(source, snapshot.image_edits, output)

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["frames"][0]["image_keypoints"]["left_wrist"] == [310.5, 205.0]
    # 픽셀 이동량 → 같은 깊이의 미터 이동량(RTMW3D 핀홀 f=1145.049/1143.781).
    # 픽셀을 미터 칸에 그대로 넣던 예전 동작은 관절을 수백 m 밖으로 던졌다.
    src = tracking_payload()["frames"][0]
    u0, v0 = src["image_keypoints"]["left_wrist"]
    px, py, pz = src["keypoints"]["left_wrist"]
    depth = -pz
    x, y, z = result["frames"][0]["keypoints"]["left_wrist"]
    assert z == pz
    assert abs(x - (px + (310.5 - u0) * depth / 1145.04940459)) < 1e-9
    assert abs(y - (py - (205.0 - v0) * depth / 1143.78109572)) < 1e-9
    assert source.read_bytes() == original
    assert result_path == output


@pytest.mark.parametrize(
    ("edits", "message"),
    [
        ({"imageEdits": {"frame": 0}}, "array"),
        ([{"frame": 1, "joint": "left_wrist", "x": 1, "y": 2, "state": "manual"}], "frame"),
        ([{"frame": 0, "joint": "pelvis", "x": 1, "y": 2, "state": "manual"}], "joint"),
        ([{"frame": 0, "joint": "left_wrist", "x": -1, "y": 2, "state": "manual"}], "coordinate"),
        ([{"frame": 0, "joint": "left_wrist", "x": float("nan"), "y": 2, "state": "manual"}], "coordinate"),
        ([{"frame": 0, "joint": "left_wrist", "x": 1, "y": 2, "state": []}], "state"),
        ([
            {"frame": 0, "joint": "left_wrist", "x": 1, "y": 2, "state": "manual"},
            {"frame": 0, "joint": "left_wrist", "x": 3, "y": 4, "state": "propagated"},
        ], "duplicate"),
    ],
)
def test_rejects_invalid_correction_documents(
    tmp_path: Path,
    edits: object,
    message: str,
) -> None:
    source, edits_path, output = write_inputs(tmp_path, edits)

    with pytest.raises(ValueError, match=message):
        snapshot = load_correction_snapshot(edits_path)
        apply_tracking_corrections(source, snapshot.image_edits, output)

    assert not output.exists()


def test_refuses_to_overwrite_the_immutable_source(tmp_path: Path) -> None:
    source, edits, _output = write_inputs(tmp_path, [])

    with pytest.raises(ValueError, match="source"):
        snapshot = load_correction_snapshot(edits)
        apply_tracking_corrections(source, snapshot.image_edits, source)


def test_atomic_output_never_reuses_or_removes_source_named_like_legacy_temp(
    tmp_path: Path,
) -> None:
    output = tmp_path / "corrected.json"
    source = tmp_path / "corrected.json.part"
    source.write_text(json.dumps(tracking_payload()), encoding="utf-8")
    original = source.read_bytes()
    edits = tmp_path / "edits.json"
    edits.write_text("[]", encoding="utf-8")

    snapshot = load_correction_snapshot(edits)
    apply_tracking_corrections(source, snapshot.image_edits, output)

    assert source.read_bytes() == original
    assert output.exists()


def test_loads_new_snapshot_without_losing_pose_edits(tmp_path: Path) -> None:
    pose_edit = {
        "frame": 0,
        "joint": "left_elbow",
        "rotation": [0.0, 0.0, 0.0, 1.0],
    }
    _source, edits, _output = write_inputs(tmp_path, {
        "imageEdits": [],
        "poseEdits": [pose_edit],
    })

    snapshot = load_correction_snapshot(edits)

    assert snapshot.image_edits == ()
    assert snapshot.pose_edits == (pose_edit,)


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"imageEdits": []},
        {"imageEdits": [], "poseEdits": [], "extra": []},
        {"imageEdits": {}, "poseEdits": []},
        {"imageEdits": [], "poseEdits": {}},
    ],
)
def test_rejects_malformed_new_snapshots(tmp_path: Path, document: object) -> None:
    _source, edits, _output = write_inputs(tmp_path, document)

    with pytest.raises(ValueError, match="snapshot"):
        load_correction_snapshot(edits)


def test_accepts_site_shaped_document_with_subject_box(tmp_path: Path) -> None:
    edits = [{"frame": 0, "joint": "left_wrist", "x": 1, "y": 2, "state": "manual"}]
    source, edits_path, output = write_inputs(tmp_path, {"imageEdits": edits, "poseEdits": []})

    snapshot = load_correction_snapshot(edits_path)
    apply_tracking_corrections(source, snapshot.image_edits, output)

    assert output.exists()


def test_read_subject_box(tmp_path: Path) -> None:
    edits_path = tmp_path / "edits.json"
    edits_path.write_text(json.dumps({"imageEdits": [], "poseEdits": []}), encoding="utf-8")
    assert read_subject_box(edits_path) is None

    edits_path.write_text(json.dumps({
        "imageEdits": [],
        "poseEdits": [],
        "subjectBox": {"frame": 3, "x1": 10, "y1": 20, "x2": 110.5, "y2": 220},
    }), encoding="utf-8")
    assert load_correction_snapshot(edits_path).image_edits == ()
    assert read_subject_box(edits_path) == (3, (10.0, 20.0, 110.5, 220.0))

    edits_path.write_text(json.dumps({"subjectBox": {"frame": 0, "x1": 5, "y1": 5, "x2": 5, "y2": 9}}), encoding="utf-8")
    with pytest.raises(ValueError, match="subject box"):
        read_subject_box(edits_path)
