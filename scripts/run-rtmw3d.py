"""Run RTMW3D-L on one video and write Artoke's compact 23-joint JSON.

RTMW3D is top-down: it wants a box around ONE person and returns that person's
pose. Passing the whole frame as the box, which this script used to do, makes
the model pick whichever person it finds most salient, and that choice changes
from frame to frame. That is why the skeleton jumped between people. It was
never a tracking failure; there was no detection step at all.

So a person detector runs first, one box is chosen as the subject, and the
subject is followed forward by box overlap. torchvision is used rather than
mmdet because `mmcv._ext` is missing in this environment.
"""

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


def build_frame_record(index, raw_points, raw_scores, smoothed_points, image_points) -> dict:
    keypoints = {}
    image_keypoints = {}
    scores = {}
    for point_index, name in enumerate(BODY23_NAMES):
        raw = raw_points[point_index]
        smoothed = smoothed_points[point_index]
        keypoints[name] = [
            float(smoothed[0]),
            -float(smoothed[1]),
            -float(smoothed[2]),
        ]
        image_keypoints[name] = [float(image_points[point_index][0]), float(image_points[point_index][1])]
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
    parser.add_argument(
        "--seed", action="append", default=[], metavar="FRAME:x1,y1,x2,y2",
        help="주인공 상자를 직접 지정한다. 여러 번 줄 수 있고, 추적이 끊긴 프레임에서 다시 잡을 때 쓴다.")
    parser.add_argument(
        "--detect-score", type=float, default=0.7,
        help="사람 검출로 인정할 확신도 하한")
    parser.add_argument(
        "--no-detect", action="store_true",
        help="검출을 끄고 예전처럼 화면 전체를 상자로 쓴다. 사람이 한 명뿐인 영상의 대조군용이다.")
    return parser.parse_args()


def parse_seed(text):
    """`60:100,50,300,700` 을 (60, [100,50,300,700]) 로 읽는다."""
    frame, _, box = text.partition(":")
    values = [float(v) for v in box.split(",")]
    if len(values) != 4:
        raise ValueError(f"상자는 x1,y1,x2,y2 네 값이어야 합니다: {text}")
    return int(frame), values


def iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    overlap = (x2 - x1) * (y2 - y1)
    area_a = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1e-6, (b[2] - b[0]) * (b[3] - b[1]))
    return overlap / (area_a + area_b - overlap)


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

    # 사람 검출기. mmdet 은 mmcv._ext 가 없어 못 쓰므로 torchvision 을 쓴다.
    detector = None
    if not args.no_detect:
        from torchvision.models.detection import (
            fasterrcnn_resnet50_fpn_v2, FasterRCNN_ResNet50_FPN_V2_Weights)
        from torchvision.transforms.functional import to_tensor
        detector = fasterrcnn_resnet50_fpn_v2(
            weights=FasterRCNN_ResNet50_FPN_V2_Weights.COCO_V1).eval().to("cuda:0")
    PERSON_LABEL = 1

    seeds = dict(parse_seed(text) for text in args.seed)

    def detect_people(image):
        tensor = to_tensor(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)).to("cuda:0")
        with torch.no_grad():
            found = detector([tensor])[0]
        keep = (found["labels"] == PERSON_LABEL) & (found["scores"] > args.detect_score)
        return found["boxes"][keep].cpu().numpy()

    frames = []
    previous = None
    index = 0
    image_size = None
    subject = None          # 지금 따라가는 상자
    carried = []            # 검출이 없어 직전 상자를 이어 쓴 프레임
    reseeded = []           # 씨앗으로 다시 잡은 프레임
    selection = None        # 무엇을 주인공으로 골랐는지 기록해 둔다
    while True:
        ok, image = capture.read()
        if not ok:
            break
        height, width = image.shape[:2]
        image_size = {"width": int(width), "height": int(height)}

        if detector is None:
            box = [0.0, 0.0, float(width), float(height)]
        else:
            if index in seeds:
                box = seeds[index]
                if index > 0:
                    reseeded.append(index)
                if selection is None:
                    selection = {"frame": index, "box": list(box), "source": "seed"}
            else:
                people = detect_people(image)
                if subject is None:
                    # 첫 주인공. 씨앗이 없으면 가장 크게 잡힌 사람을 쓴다.
                    if len(people) == 0:
                        raise RuntimeError(f"{index}번 프레임에서 사람을 찾지 못했습니다. --seed 로 상자를 주십시오")
                    areas = [(b[2] - b[0]) * (b[3] - b[1]) for b in people]
                    box = [float(v) for v in people[int(np.argmax(areas))]]
                    selection = {"frame": index, "box": list(box), "source": "largest"}
                else:
                    # 직전 상자와 가장 많이 겹치는 검출로 잇는다.
                    scored = [(iou(subject, b), b) for b in people]
                    best = max(scored, key=lambda pair: pair[0]) if scored else (0.0, None)
                    if best[0] < 0.2:
                        # 놓쳤다. 직전 상자를 이어 쓴다 — 톱다운은 상자만 있으면
                        # 자세를 내므로 쓰러진 구간에서도 추정이 이어진다.
                        box = [float(v) for v in subject]
                        carried.append(index)
                    else:
                        box = [float(v) for v in best[1]]
            # 상자 떨림이 자세 떨림으로 번지지 않게 가볍게 고른다.
            box = box if subject is None else [
                float(s) * 0.4 + float(c) * 0.6 for s, c in zip(subject, box)]
            subject = [float(v) for v in box]

        bbox = np.array([box], dtype=np.float32)
        result = inference_topdown(model, image, bbox)[0].pred_instances
        raw_points = result.keypoints[0, :23]
        raw_scores = result.keypoint_scores[0, :23]
        # `keypoints` 는 미터 단위 3D 다. 화면 좌표는 `transformed_keypoints` 에 있다.
        # 예전에는 3D 의 x·y 를 화면 좌표로 적어서, 겹쳐 보기가 모든 관절을 왼쪽 위
        # 구석에 그렸다. 상자 39~855 픽셀에 대해 이 값이 57~834 로 들어온다.
        image_points = result.transformed_keypoints[0, :23]
        current = raw_points.copy()
        if previous is not None:
            reliable = raw_scores[:, None] >= 0.2
            smoothed = previous * 0.35 + current * 0.65
            current = np.where(reliable, smoothed, previous)
        previous = current
        frames.append(build_frame_record(index, raw_points, raw_scores, current, image_points))
        index += 1
    capture.release()
    if carried:
        print(f"검출을 놓쳐 직전 상자를 이어 쓴 프레임 {len(carried)}개: {carried[:20]}")
    if reseeded:
        print(f"씨앗으로 다시 잡은 프레임: {reseeded}")
    if not frames:
        raise RuntimeError("추론할 영상 프레임이 없습니다")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "schema": "artoke.rtmw3d.v1",
        "backend": "OpenMMLab RTMW3D-L",
        "source_video": str(args.input.resolve()),
        "fps": fps,
        "coordinate_system": "right-handed Y-up metres",
        "image_size": image_size,
        "person_selection": selection,
        "carried_frames": carried,
        "frames": frames,
    }, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
