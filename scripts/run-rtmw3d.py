"""Run RTMW3D-L on one video and write Artoke's compact 23-joint JSON.

RTMW3D is top-down: it wants a box around ONE person and returns that person's
pose. Passing the whole frame as the box, which this script used to do, makes
the model pick whichever person it finds most salient, and that choice changes
from frame to frame. That is why the skeleton jumped between people. It was
never a tracking failure; there was no detection step at all.

So a person detector runs first, one box is chosen as the subject, and the
subject is followed forward frame to frame. torchvision is used rather than
mmdet because `mmcv._ext` is missing in this environment.

Box overlap alone is not enough to follow the subject: when two people cross,
the wrong box can overlap more. So each candidate is scored by three signals
folded into one number: overlap with the last box, overlap with a constant-
velocity prediction of it, and colour-histogram similarity to the subject's
appearance. A candidate that looks nothing like the subject is rejected even
if it overlaps.
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
    parser.add_argument(
        "--list-people", type=int, metavar="FRAME", default=None,
        help="그 프레임에서 검출된 사람에 번호를 붙여 보여 주고 끝낸다. --preview 로 그림도 낸다.")
    parser.add_argument(
        "--preview", type=Path, default=None,
        help="--list-people 이 번호를 그려 넣은 그림을 여기에 저장한다.")
    parser.add_argument(
        "--pick", action="append", default=[], metavar="FRAME:N",
        help="그 프레임의 N 번 후보를 주인공으로 삼는다. --list-people 로 번호를 먼저 본다.")
    parser.add_argument(
        "--select", action="store_true",
        help="첫 프레임을 창으로 띄워 후보를 클릭해 고른다. 화면이 있는 곳에서만 쓴다.")
    return parser.parse_args()


def parse_pick(text):
    """`0:2` 를 (0, 2) 로 읽는다."""
    frame, _, number = text.partition(":")
    return int(frame), int(number)


def read_frame(path, index):
    import cv2
    capture = cv2.VideoCapture(str(path))
    capture.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, image = capture.read()
    capture.release()
    if not ok:
        raise RuntimeError(f"{index}번 프레임을 읽지 못했습니다")
    return image


def annotate(image, boxes):
    """후보마다 상자와 번호를 그린다. 번호가 곧 --pick 에 넣을 값이다."""
    import cv2
    canvas = image.copy()
    for number, box in enumerate(boxes):
        x1, y1, x2, y2 = (int(v) for v in box)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 220, 255), 3)
        cv2.rectangle(canvas, (x1, y1 - 42), (x1 + 56, y1), (0, 220, 255), -1)
        cv2.putText(canvas, str(number), (x1 + 10, y1 - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.3, (0, 0, 0), 3)
    return canvas


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


def box_center(box):
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def shift_box(box, velocity):
    """등속 예측: 직전 상자를 속도만큼 밀어 이번 프레임의 자리를 짐작한다."""
    dx, dy = velocity
    return [box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy]


def clamp_box(box, width, height):
    x1 = min(max(0.0, box[0]), width - 2.0)
    y1 = min(max(0.0, box[1]), height - 2.0)
    x2 = min(max(x1 + 1.0, box[2]), float(width))
    y2 = min(max(y1 + 1.0, box[3]), float(height))
    return [x1, y1, x2, y2]


def color_histogram(image, box):
    """상자 가운데 부분의 HSV 색 분포. 겉모습 비교의 재료다."""
    import cv2
    height, width = image.shape[:2]
    # 상자 가장자리는 배경이 섞이므로 가운데 60% 만 쓴다.
    margin_x, margin_y = (box[2] - box[0]) * 0.2, (box[3] - box[1]) * 0.2
    x1 = int(max(0, box[0] + margin_x))
    x2 = int(min(width, box[2] - margin_x))
    y1 = int(max(0, box[1] + margin_y))
    y2 = int(min(height, box[3] - margin_y))
    if x2 <= x1 or y2 <= y1:
        return None
    patch = cv2.cvtColor(image[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
    # 흰 바닥 같은 무채색 배경이 두 상자를 다 지배하면 남남끼리도 상관이 높게
    # 나온다. 실제로 주인공이 넘어져 상자에 바닥이 많이 들어오자 다른 사람의
    # 유사도가 0.17 에서 0.83 까지 올랐다. 채도나 밝기가 낮은 픽셀은 빼고 센다.
    mask = cv2.inRange(patch, (0, 40, 40), (180, 255, 255))
    if cv2.countNonZero(mask) < 50:
        return None     # 색이 있는 픽셀이 없으면 겉모습을 논할 수 없다
    histogram = cv2.calcHist([patch], [0, 1], mask, [30, 32], [0, 180, 0, 256])
    cv2.normalize(histogram, histogram)
    return histogram


def appearance_similarity(reference, histogram):
    """색 분포의 상관을 0~1 로 접는다. 비교할 것이 없으면 중립인 0.5 를 준다."""
    import cv2
    if reference is None or histogram is None:
        return 0.5
    return max(0.0, min(1.0, cv2.compareHist(reference, histogram, cv2.HISTCMP_CORREL)))


def group_ranges(values):
    """[3,4,5,9] 를 [[3,5],[9,9]] 로 묶는다. 끊긴 구간을 사람이 읽기 좋게 한다."""
    ranges = []
    for value in values:
        if ranges and value == ranges[-1][1] + 1:
            ranges[-1][1] = value
        else:
            ranges.append([value, value])
    return ranges


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

    def detect_people(image):
        tensor = to_tensor(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)).to("cuda:0")
        with torch.no_grad():
            found = detector([tensor])[0]
        keep = (found["labels"] == PERSON_LABEL) & (found["scores"] > args.detect_score)
        order = found["scores"][keep].argsort(descending=True)
        return found["boxes"][keep][order].cpu().numpy()

    # 후보를 보여 주고 끝내는 길. 화면이 없어도 번호를 고를 수 있게 한다.
    if args.list_people is not None:
        if detector is None:
            raise SystemExit("--no-detect 와 함께 쓸 수 없습니다")
        image = read_frame(args.input, args.list_people)
        people = detect_people(image)
        print(f"{args.list_people}번 프레임에서 사람 {len(people)}명")
        for number, box in enumerate(people):
            print("  %d  x %4.0f~%4.0f  y %4.0f~%4.0f  (폭 %3.0f 높이 %3.0f)"
                  % (number, box[0], box[2], box[1], box[3], box[2] - box[0], box[3] - box[1]))
        if args.preview:
            args.preview.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(args.preview), annotate(image, people))
            print(f"그림 저장 {args.preview}")
        print(f"고르려면: --pick {args.list_people}:<번호>")
        return

    seeds = dict(parse_seed(text) for text in args.seed)

    # 번호로 고른 것을 상자로 바꿔 씨앗에 넣는다. --seed 와 같은 자리에서 쓰인다.
    for frame_index, number in (parse_pick(text) for text in args.pick):
        people = detect_people(read_frame(args.input, frame_index))
        if number >= len(people):
            raise SystemExit(f"{frame_index}번 프레임에는 후보가 {len(people)}명뿐입니다")
        seeds[frame_index] = [float(v) for v in people[number]]

    # 창을 띄워 클릭으로 고르는 길. 화면이 있는 곳에서만 쓴다.
    if args.select:
        if detector is None:
            raise SystemExit("--no-detect 와 함께 쓸 수 없습니다")
        pick_frame = 0
        image = read_frame(args.input, pick_frame)
        people = detect_people(image)
        chosen = {}

        def on_click(event, x, y, flags, _):
            if event != cv2.EVENT_LBUTTONDOWN:
                return
            for number, box in enumerate(people):
                if box[0] <= x <= box[2] and box[1] <= y <= box[3]:
                    chosen["box"] = [float(v) for v in box]
                    chosen["number"] = number

        window = "주인공을 클릭하십시오 (Esc 로 취소)"
        cv2.imshow(window, annotate(image, people))
        cv2.setMouseCallback(window, on_click)
        while "box" not in chosen:
            if cv2.waitKey(30) == 27:
                break
        cv2.destroyWindow(window)
        if "box" not in chosen:
            raise SystemExit("고르지 않았습니다")
        print(f"{chosen['number']}번 후보를 골랐습니다")
        seeds[pick_frame] = chosen["box"]

    frames = []
    previous = None
    index = 0
    image_size = None
    subject = None          # 지금 따라가는 상자
    velocity = [0.0, 0.0]   # 상자 중심의 프레임당 이동. 등속 예측의 재료다
    reference_hist = None   # 주인공의 겉모습(색 분포) 기준
    carried = []            # 검출을 놓쳐 예측 상자를 이어 쓴 프레임
    ambiguous = []          # 두 후보의 점수가 비슷해 갈아탈 위험이 있던 프레임
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
                # 씨앗은 사용자가 확정한 자리다. 속도와 겉모습 기준을 여기서 다시 잡는다.
                box = [float(v) for v in seeds[index]]
                if index > 0:
                    reseeded.append(index)
                if selection is None:
                    selection = {"frame": index, "box": list(box), "source": "seed"}
                velocity = [0.0, 0.0]
                reference_hist = color_histogram(image, box)
                subject = list(box)
            elif subject is None:
                # 첫 주인공. 씨앗이 없으면 가장 크게 잡힌 사람을 쓴다.
                people = detect_people(image)
                if len(people) == 0:
                    raise RuntimeError(f"{index}번 프레임에서 사람을 찾지 못했습니다. --seed 로 상자를 주십시오")
                areas = [(b[2] - b[0]) * (b[3] - b[1]) for b in people]
                box = [float(v) for v in people[int(np.argmax(areas))]]
                selection = {"frame": index, "box": list(box), "source": "largest"}
                velocity = [0.0, 0.0]
                reference_hist = color_histogram(image, box)
                subject = list(box)
            else:
                people = detect_people(image)
                predicted = shift_box(subject, velocity)
                # 세 근거(직전 상자 겹침, 등속 예측 겹침, 겉모습)를 한 점수로 합친다.
                # 교차 순간에는 겹침만으로 남의 상자가 이길 수 있어서, 겉모습이
                # 확연히 다른 후보는 겹쳐도 애초에 후보에서 뺀다.
                scored = []
                for candidate in people:
                    iou_now = iou(subject, candidate)
                    iou_predicted = iou(predicted, candidate)
                    if max(iou_now, iou_predicted) < 0.05:
                        continue    # 화면 반대편의 사람이 겉모습만으로 붙는 일을 막는다
                    histogram = color_histogram(image, candidate)
                    similarity = appearance_similarity(reference_hist, histogram)
                    if reference_hist is not None and similarity < 0.15:
                        continue
                    score = 0.3 * iou_now + 0.3 * iou_predicted + 0.4 * similarity
                    scored.append((score, histogram, [float(v) for v in candidate]))
                scored.sort(key=lambda item: item[0], reverse=True)
                if not scored or scored[0][0] < 0.3:
                    # 놓쳤다. 예측 상자를 이어 쓴다 — 톱다운은 상자만 있으면
                    # 자세를 내므로 쓰러진 구간에서도 추정이 이어진다.
                    box = clamp_box(predicted, width, height)
                    carried.append(index)
                    velocity = [v * 0.8 for v in velocity]
                    subject = list(box)
                else:
                    best_score, best_hist, best_box = scored[0]
                    unambiguous = len(scored) == 1 or best_score - scored[1][0] >= 0.15
                    if not unambiguous:
                        ambiguous.append(index)
                    # 상자 떨림이 자세 떨림으로 번지지 않게 가볍게 고른다.
                    box = [s * 0.4 + c * 0.6 for s, c in zip(subject, best_box)]
                    delta = [n - o for n, o in zip(box_center(box), box_center(subject))]
                    velocity = [0.7 * v + 0.3 * d for v, d in zip(velocity, delta)]
                    # 겉모습 기준은 확실할 때만 천천히 따라간다. 교차 중에 남의
                    # 색이 스며들면 기준 자체가 오염되기 때문이다.
                    if best_hist is not None:
                        if reference_hist is None:
                            reference_hist = best_hist
                        elif unambiguous:
                            reference_hist = reference_hist * 0.9 + best_hist * 0.1
                            cv2.normalize(reference_hist, reference_hist)
                    subject = list(box)

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
        text = ", ".join(f"{a}~{b}" if a != b else str(a) for a, b in group_ranges(carried))
        print(f"검출을 놓쳐 예측 상자를 이어 쓴 프레임 {len(carried)}개: {text}")
        print("길게 이어지는 구간은 시작 프레임을 --list-people 로 살펴보고 --pick 이나 --seed 로 다시 잡으십시오")
    if ambiguous:
        text = ", ".join(f"{a}~{b}" if a != b else str(a) for a, b in group_ranges(ambiguous))
        print(f"두 후보의 점수가 비슷해 갈아탈 위험이 있던 프레임 {len(ambiguous)}개: {text}")
        print("결과를 겹쳐 보기로 확인하고, 틀렸으면 그 프레임에 --pick 이나 --seed 를 주어 다시 잡으십시오")
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
        "ambiguous_frames": ambiguous,
        "frames": frames,
    }, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
