"""Run RTMW3D-L on one video and write Artoke's compact 23-joint JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


BODY23_NAMES = (
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip", "left_knee",
    "right_knee", "left_ankle", "right_ankle", "left_big_toe",
    "left_small_toe", "left_heel", "right_big_toe", "right_small_toe",
    "right_heel",
)


def person_bbox(
    points,
    scores,
    image_width: int,
    image_height: int,
    previous=None,
):
    """Build a padded single-person crop from reliable image-space joints."""
    import numpy as np

    points = np.asarray(points, dtype=np.float32)
    scores = np.asarray(scores, dtype=np.float32)
    reliable = (scores >= 0.35) & np.isfinite(points[:, :2]).all(axis=1)
    if np.count_nonzero(reliable) < 6:
        return None

    visible = points[reliable, :2]
    minimum = visible.min(axis=0)
    maximum = visible.max(axis=0)
    span = maximum - minimum
    if np.any(span < 16.0):
        return None
    padding = span * 0.2
    current = np.array([
        max(0.0, minimum[0] - padding[0]),
        max(0.0, minimum[1] - padding[1]),
        min(float(image_width), maximum[0] + padding[0]),
        min(float(image_height), maximum[1] + padding[1]),
    ], dtype=np.float32)
    if (
        not np.isfinite(current).all()
        or current[2] - current[0] < 16.0
        or current[3] - current[1] < 16.0
    ):
        return None
    if previous is not None:
        current = np.asarray(previous, dtype=np.float32) * 0.25 + current * 0.75
    return current.astype(np.float32)


def extract_prediction_arrays(prediction):
    """Keep RTMW3D camera-space pose and image-space joints separate."""
    import numpy as np

    pose_points = np.asarray(prediction.keypoints[0, :23], dtype=np.float32)
    image_points = np.asarray(
        prediction.transformed_keypoints[0, :23, :2],
        dtype=np.float32,
    )
    scores = np.nan_to_num(
        np.asarray(prediction.keypoint_scores[0, :23], dtype=np.float32),
        nan=0.0,
        posinf=0.0,
        neginf=0.0,
    )
    return pose_points.copy(), image_points.copy(), scores.copy()


def interpolate_unreliable_points(points, scores, threshold: float = 0.2):
    """Fill low-confidence gaps from the nearest reliable frames on both sides."""
    import numpy as np

    points = np.asarray(points, dtype=np.float32)
    scores = np.asarray(scores, dtype=np.float32)
    output = points.copy()
    frame_indices = np.arange(points.shape[0], dtype=np.float32)
    for joint in range(points.shape[1]):
        reliable = (scores[:, joint] >= threshold) & np.isfinite(points[:, joint]).all(axis=1)
        known = np.flatnonzero(reliable)
        if known.size == 0:
            known = np.flatnonzero(np.isfinite(points[:, joint]).all(axis=1))
        if known.size == 0:
            output[:, joint] = 0.0
            continue
        for axis in range(points.shape[2]):
            output[:, joint, axis] = np.interp(
                frame_indices,
                known,
                points[known, joint, axis],
            )
    return output


def smooth_pose_sequence(points, alpha: float = 0.65):
    """Smooth root-relative pose without attenuating hip-center travel."""
    import numpy as np

    points = np.asarray(points, dtype=np.float32)
    if points.shape[0] < 3:
        return points.copy()

    root = None
    source = points
    if points.shape[1] > 12:
        root = (points[:, 11] + points[:, 12]) * 0.5
        source = points - root[:, None, :]

    forward = source.copy()
    backward = source.copy()
    for index in range(1, points.shape[0]):
        forward[index] = forward[index - 1] * (1.0 - alpha) + source[index] * alpha
    for index in range(points.shape[0] - 2, -1, -1):
        backward[index] = backward[index + 1] * (1.0 - alpha) + source[index] * alpha
    output = (forward + backward) * 0.5
    if root is not None:
        output += root[:, None, :]
    return output.astype(np.float32)


def build_frame_record(index, image_points, raw_scores, smoothed_points) -> dict:
    keypoints = {}
    image_keypoints = {}
    scores = {}
    for point_index, name in enumerate(BODY23_NAMES):
        image = image_points[point_index]
        smoothed = smoothed_points[point_index]
        keypoints[name] = [
            float(smoothed[0]),
            -float(smoothed[1]),
            -float(smoothed[2]),
        ]
        image_keypoints[name] = [float(image[0]), float(image[1])]
        scores[name] = float(max(0.0, min(1.0, raw_scores[point_index])))
    return {
        "index": index,
        "keypoints": keypoints,
        "image_keypoints": image_keypoints,
        "scores": scores,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mmpose-repo", type=Path, required=True)
    parser.add_argument("--checkpoint-dir", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    project = args.mmpose_repo / "projects" / "rtmpose3d"
    sys.path.insert(0, str(project))

    import cv2
    import numpy as np
    import torch

    # Official OpenMMLab checkpoints predate PyTorch's weights_only=True default.
    original_load = torch.load

    def compatible_load(*values, **options):
        options.setdefault("weights_only", False)
        return original_load(*values, **options)

    torch.load = compatible_load

    from mmpose.apis import inference_topdown, init_model
    from rtmpose3d import RTMW3DHead, SimCC3DLabel, TopdownPoseEstimator3D  # noqa: F401

    config = project / "configs" / "rtmw3d-l_8xb64_cocktail14-384x288.py"
    checkpoint = args.checkpoint_dir / "rtmw3d-l_8xb64_cocktail14-384x288-794dbc78_20240626.pth"
    model = init_model(str(config), str(checkpoint), device="cuda:0")

    capture = cv2.VideoCapture(str(args.input))
    if not capture.isOpened():
        raise RuntimeError(f"영상을 열 수 없습니다: {args.input}")
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    if not fps > 0:
        fps = 30.0

    pose_points_by_frame = []
    image_points_by_frame = []
    raw_scores_by_frame = []
    tracked_bbox = None
    missed_crop_frames = 0
    index = 0
    image_size = None
    while True:
        ok, image = capture.read()
        if not ok:
            break
        height, width = image.shape[:2]
        image_size = {"width": int(width), "height": int(height)}
        active_bbox = tracked_bbox if tracked_bbox is not None else np.array(
            [0.0, 0.0, float(width), float(height)],
            dtype=np.float32,
        )
        result = inference_topdown(model, image, active_bbox[None, :])[0].pred_instances
        pose_points, image_points, raw_scores = extract_prediction_arrays(result)

        next_bbox = person_bbox(
            image_points,
            raw_scores,
            width,
            height,
            previous=tracked_bbox,
        )
        if next_bbox is not None:
            tracked_bbox = next_bbox
            missed_crop_frames = 0
        else:
            missed_crop_frames += 1
            if missed_crop_frames >= 3:
                tracked_bbox = None

        pose_points_by_frame.append(pose_points)
        image_points_by_frame.append(image_points)
        raw_scores_by_frame.append(raw_scores.copy())
        index += 1
    capture.release()
    if not pose_points_by_frame:
        raise RuntimeError("추론할 영상 프레임이 없습니다")

    pose_points = np.stack(pose_points_by_frame)
    image_points = np.stack(image_points_by_frame)
    raw_scores = np.nan_to_num(
        np.stack(raw_scores_by_frame),
        nan=0.0,
        posinf=0.0,
        neginf=0.0,
    )
    interpolated_pose = interpolate_unreliable_points(pose_points, raw_scores)
    interpolated_image = interpolate_unreliable_points(image_points, raw_scores)
    smoothed = smooth_pose_sequence(interpolated_pose)
    if not np.isfinite(interpolated_image).all() or not np.isfinite(smoothed).all():
        raise RuntimeError("RTMW3D 결과에 유효하지 않은 좌표가 포함되어 있습니다")
    frames = [
        build_frame_record(
            index,
            interpolated_image[index],
            raw_scores[index],
            smoothed[index],
        )
        for index in range(len(pose_points_by_frame))
    ]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "schema": "artoke.rtmw3d.v1",
        "backend": "OpenMMLab RTMW3D-L",
        "source_video": str(args.input.resolve()),
        "fps": fps,
        "coordinate_system": "right-handed Y-up metres",
        "image_size": image_size,
        "frames": frames,
    }, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
