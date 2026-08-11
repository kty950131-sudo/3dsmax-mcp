import json
from pathlib import Path

import pytest

from src.rtmw3d.motion import BODY23_NAMES
from src.worker.tracking_corrections import apply_tracking_corrections


def tracking_payload() -> dict[str, object]:
    keypoints = {
        joint: [float(index), float(index + 1), float(index + 2)]
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

    result_path = apply_tracking_corrections(source, edits, output)

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["frames"][0]["image_keypoints"]["left_wrist"] == [310.5, 205.0]
    assert result["frames"][0]["keypoints"]["left_wrist"] == [310.5, -205.0, 11.0]
    assert source.read_bytes() == original
    assert result_path == output


@pytest.mark.parametrize(
    ("edits", "message"),
    [
        ({"frame": 0}, "array"),
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
        apply_tracking_corrections(source, edits_path, output)

    assert not output.exists()


def test_refuses_to_overwrite_the_immutable_source(tmp_path: Path) -> None:
    source, edits, _output = write_inputs(tmp_path, [])

    with pytest.raises(ValueError, match="source"):
        apply_tracking_corrections(source, edits, source)


def test_atomic_output_never_reuses_or_removes_source_named_like_legacy_temp(
    tmp_path: Path,
) -> None:
    output = tmp_path / "corrected.json"
    source = tmp_path / "corrected.json.part"
    source.write_text(json.dumps(tracking_payload()), encoding="utf-8")
    original = source.read_bytes()
    edits = tmp_path / "edits.json"
    edits.write_text("[]", encoding="utf-8")

    apply_tracking_corrections(source, edits, output)

    assert source.read_bytes() == original
    assert output.exists()
