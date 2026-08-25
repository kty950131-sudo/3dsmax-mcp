import json
from pathlib import Path

import pytest

from maxmcp.rtmw3d.motion import BODY23_NAMES
from maxmcp.worker.tracking_corrections import apply_tracking_corrections


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

    result_path = apply_tracking_corrections(source, edits, output)

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
