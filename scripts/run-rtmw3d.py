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

    frames = []
    previous = None
    index = 0
    while True:
        ok, image = capture.read()
        if not ok:
            break
        height, width = image.shape[:2]
        bbox = np.array([[0.0, 0.0, float(width), float(height)]], dtype=np.float32)
        result = inference_topdown(model, image, bbox)[0].pred_instances
        raw_points = result.keypoints[0, :23]
        raw_scores = result.keypoint_scores[0, :23]
        current = np.stack((raw_points[:, 0], -raw_points[:, 1], -raw_points[:, 2]), axis=1)
        if previous is not None:
            reliable = raw_scores[:, None] >= 0.2
            smoothed = previous * 0.35 + current * 0.65
            current = np.where(reliable, smoothed, previous)
        previous = current
        frames.append({
            "index": index,
            "keypoints": {name: current[i].astype(float).tolist() for i, name in enumerate(BODY23_NAMES)},
            "scores": {name: float(max(0.0, min(1.0, raw_scores[i]))) for i, name in enumerate(BODY23_NAMES)},
        })
        index += 1
    capture.release()
    if not frames:
        raise RuntimeError("추론할 영상 프레임이 없습니다")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "schema": "artoke.rtmw3d.v1",
        "backend": "OpenMMLab RTMW3D-L",
        "source_video": str(args.input.resolve()),
        "fps": fps,
        "coordinate_system": "right-handed Y-up metres",
        "frames": frames,
    }, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
