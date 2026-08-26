import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run-rtmw3d.py"
SPEC = importlib.util.spec_from_file_location("run_rtmw3d", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_build_frame_keeps_image_pixels_separate_from_3d() -> None:
    raw = [[10.0, 20.0, 0.5] for _ in range(23)]
    smoothed = [[12.0, 18.0, 0.25] for _ in range(23)]

    image = [[10.0, 20.0] for _ in range(23)]

    frame = MODULE.build_frame_record(0, raw, [1.0] * 23, smoothed, image)

    assert frame["image_keypoints"]["nose"] == [10.0, 20.0]
    assert frame["keypoints"]["nose"] == [12.0, -18.0, -0.25]


def test_build_frame_clamps_scores() -> None:
    points = [[0.0, 0.0, 0.0] for _ in range(23)]
    scores = [-1.0, 2.0] + [0.5] * 21

    frame = MODULE.build_frame_record(0, points, scores, points, [[0.0, 0.0]] * 23)

    assert frame["scores"]["nose"] == 0.0
    assert frame["scores"]["left_eye"] == 1.0


def test_person_bbox_crops_to_reliable_body_points_with_padding() -> None:
    np = pytest.importorskip("numpy")
    points = np.zeros((23, 3), dtype=np.float32)
    scores = np.zeros(23, dtype=np.float32)
    points[:6, :2] = np.array([
        [100.0, 50.0], [300.0, 50.0], [100.0, 450.0],
        [300.0, 450.0], [200.0, 200.0], [200.0, 300.0],
    ])
    scores[:6] = 0.9

    bbox = MODULE.person_bbox(points, scores, 640, 480)

    assert bbox.tolist() == [60.0, 0.0, 340.0, 480.0]


def test_person_bbox_reports_failure_when_too_few_joints_are_reliable() -> None:
    np = pytest.importorskip("numpy")
    previous = np.array([40.0, 20.0, 400.0, 460.0], dtype=np.float32)
    points = np.zeros((23, 3), dtype=np.float32)
    scores = np.zeros(23, dtype=np.float32)
    scores[:3] = 0.9

    bbox = MODULE.person_bbox(points, scores, 640, 480, previous=previous)

    assert bbox is None


def test_person_bbox_rejects_points_outside_the_image() -> None:
    np = pytest.importorskip("numpy")
    points = np.full((23, 3), [900.0, 900.0, 0.0], dtype=np.float32)
    scores = np.full(23, 0.9, dtype=np.float32)

    bbox = MODULE.person_bbox(points, scores, 640, 480)

    assert bbox is None


def test_extract_prediction_arrays_keeps_camera_and_image_coordinates_separate() -> None:
    np = pytest.importorskip("numpy")

    class Prediction:
        keypoints = np.full((1, 23, 3), [1.0, 2.0, 3.0], dtype=np.float32)
        transformed_keypoints = np.full((1, 23, 2), [100.0, 200.0], dtype=np.float32)
        keypoint_scores = np.full((1, 23), 0.9, dtype=np.float32)
        keypoint_scores[0, 0] = np.nan

    pose_points, image_points, scores = MODULE.extract_prediction_arrays(Prediction())

    assert pose_points[0].tolist() == [1.0, 2.0, 3.0]
    assert image_points[0].tolist() == [100.0, 200.0]
    assert scores[0] == 0.0


def test_interpolates_a_low_confidence_joint_from_both_neighboring_frames() -> None:
    np = pytest.importorskip("numpy")
    points = np.zeros((3, 1, 3), dtype=np.float32)
    points[0, 0] = [0.0, 10.0, 20.0]
    points[1, 0] = [99.0, 99.0, 99.0]
    points[2, 0] = [2.0, 12.0, 22.0]
    scores = np.array([[1.0], [0.05], [1.0]], dtype=np.float32)

    interpolated = MODULE.interpolate_unreliable_points(points, scores)

    assert interpolated[1, 0].tolist() == [1.0, 11.0, 21.0]


def test_interpolation_replaces_an_all_unreliable_nonfinite_joint() -> None:
    np = pytest.importorskip("numpy")
    points = np.full((3, 1, 3), np.nan, dtype=np.float32)
    scores = np.zeros((3, 1), dtype=np.float32)

    interpolated = MODULE.interpolate_unreliable_points(points, scores)

    assert np.isfinite(interpolated).all()
    assert interpolated.tolist() == [[[0.0, 0.0, 0.0]]] * 3


def test_bidirectional_smoothing_damps_a_single_frame_spike_without_time_bias() -> None:
    np = pytest.importorskip("numpy")
    points = np.zeros((3, 1, 3), dtype=np.float32)
    points[1, 0, 0] = 4.0

    smoothed = MODULE.smooth_pose_sequence(points)

    assert smoothed[1, 0, 0] < 4.0
    assert smoothed[0, 0, 0] == smoothed[2, 0, 0]


def test_smoothing_preserves_hip_center_translation() -> None:
    np = pytest.importorskip("numpy")
    points = np.zeros((3, 23, 3), dtype=np.float32)
    centers = np.array([
        [0.0, 0.0, 0.0],
        [1.0, 2.0, 3.0],
        [4.0, 5.0, 6.0],
    ], dtype=np.float32)
    points[:, 11] = centers + [-0.2, 0.0, 0.0]
    points[:, 12] = centers + [0.2, 0.0, 0.0]

    smoothed = MODULE.smooth_pose_sequence(points)
    smoothed_centers = (smoothed[:, 11] + smoothed[:, 12]) * 0.5

    assert smoothed_centers == pytest.approx(centers)
