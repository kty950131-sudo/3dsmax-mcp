import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run-rtmw3d.py"
SPEC = importlib.util.spec_from_file_location("run_rtmw3d", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_build_frame_keeps_image_pixels_separate_from_3d() -> None:
    raw = [[10.0, 20.0, 0.5] for _ in range(23)]
    smoothed = [[12.0, 18.0, 0.25] for _ in range(23)]

    frame = MODULE.build_frame_record(0, raw, [1.0] * 23, smoothed)

    assert frame["image_keypoints"]["nose"] == [10.0, 20.0]
    assert frame["keypoints"]["nose"] == [12.0, -18.0, -0.25]


def test_build_frame_clamps_scores() -> None:
    points = [[0.0, 0.0, 0.0] for _ in range(23)]
    scores = [-1.0, 2.0] + [0.5] * 21

    frame = MODULE.build_frame_record(0, points, scores, points)

    assert frame["scores"]["nose"] == 0.0
    assert frame["scores"]["left_eye"] == 1.0
