"""주인공 상자 트랙으로 그 사람만 담은 영상을 만든다.

  python scripts/crop-subject.py --tracking out.json --input in.mp4 --output crop.mp4
                                 [--mode follow|fixed] [--pad 0.35] [--size 720]

언리얼 MetaHuman 은 화면에 사람이 여럿이면 누구를 따라갈지 고를 수단이 없다
(MetaHumanPerformance 멤버 47개에 상자·인물 번호가 없다). 그래서 **한 명만 남긴
영상**을 만들어 넣는다.

두 방식이 있고 얻는 것이 다르다.

  follow  창이 사람을 따라다닌다. 사람이 늘 가운데 같은 크기로 보인다.
          자세는 정확하지만 **얼마나 걸어갔는지가 사라진다** — 언리얼이 화면 속
          크기로 깊이를 재기 때문이다. 궤적은 RTMW3D 쪽에서 다시 넣는다.
  fixed   전체 동선을 덮는 창 하나로 고정한다. 궤적은 살지만 동선이 넓으면
          창 안에 다른 사람이 들어온다.

⚠️ 상자를 그대로 쓰지 않고 `--pad` 만큼 넓힌다. 딱 맞게 자르면 팔을 뻗을 때
손이 화면 밖으로 나가고, 언리얼이 그 관절을 놓친다.

`--isolate` 를 주면 주인공만 남긴다. 창을 좁히는 대신 이렇게 하는 이유:
창이 좁으면 손이 잘리고, 창이 넓으면 다른 사람이 들어온다. 지우면 창 크기와
무관하게 주인공만 남는다. 두 겹으로 지운다.

  1. Mask R-CNN 인스턴스 마스크로 **다른 사람의 픽셀**을 지운다. 주인공 상자와
     가장 많이 겹치는 검출이 주인공이고, 나머지 사람의 마스크가 지울 자리다.
     사각형만으로는 주인공 옆에 붙어 선 사람을 못 갈라서(f90 의 초록 전신 잔존
     2.03% 실측) 픽셀 단위 마스크가 필요하다.
  2. 주인공 상자(여유 포함) 밖을 마저 지운다. 마스크 검출이 놓친 사람(누운
     인물)도 상자 밖이라면 여기서 지워진다.

지우는 색은 흰색 같은 임의 단색이 아니라 **화면 가장자리의 시간 중앙값**
(= 실제 바닥색)이다. 픽셀별 중앙값으로 사람 없는 배경판을 만드는 방법도
검토했으나, 이 영상처럼 인물이 절반 넘는 프레임을 누운 채로 보내면 중앙값에
누운 사람이 그대로 남아서 쓰지 않는다.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--tracking", type=Path, required=True, help="run-rtmw3d.py 가 낸 JSON")
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--mode", choices=("follow", "fixed"), default="follow")
    p.add_argument("--pad", type=float, default=0.35, help="상자를 이만큼 넓힌다")
    p.add_argument("--size", type=int, default=720, help="내보낼 정사각 한 변")
    p.add_argument("--isolate", action="store_true",
                   help="주인공 상자 밖을 배경색으로 지운다 (follow 전용)")
    p.add_argument("--isolate-pad", type=float, default=0.12,
                   help="지우지 않고 남길 상자 여유")
    return p.parse_args()


def background_color(input_path, w, h, strip=40, samples=16):
    """화면 네 가장자리 픽셀의 시간 중앙값. 고정 카메라에서 이 띠는 늘 배경이다."""
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(str(input_path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    step = max(1, total // samples)
    pixels = []
    index = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if index % step == 0:
            pixels.append(frame[:strip].reshape(-1, 3))
            pixels.append(frame[-strip:].reshape(-1, 3))
            pixels.append(frame[:, :strip].reshape(-1, 3))
            pixels.append(frame[:, -strip:].reshape(-1, 3))
        index += 1
    cap.release()
    return np.median(np.concatenate(pixels), axis=0)


def iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    overlap = (x2 - x1) * (y2 - y1)
    area_a = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    area_b = max(1e-6, (b[2] - b[0]) * (b[3] - b[1]))
    return overlap / (area_a + area_b - overlap)


def isolate_mask(box, pad, w, h):
    """주인공을 남길 영역. 경계가 눈에 띄지 않게 나중에 흐린다."""
    x1, y1, x2, y2 = box
    mx, my = (x2 - x1) * pad, (y2 - y1) * pad
    return (int(max(0, x1 - mx)), int(max(0, y1 - my)),
            int(min(w, x2 + mx)), int(min(h, y2 + my)))


def padded(box, pad, w, h):
    """상자를 넓히고 정사각으로 만든 뒤 화면 안으로 밀어 넣는다."""
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    side = max(x2 - x1, y2 - y1) * (1 + pad)
    side = min(side, min(w, h))          # 화면보다 클 수는 없다
    half = side / 2
    cx = min(max(cx, half), w - half)    # 밖으로 나가면 안으로 민다
    cy = min(max(cy, half), h - half)
    return cx - half, cy - half, side


def main():
    args = parse_args()
    data = json.loads(args.tracking.read_text(encoding="utf-8"))
    size = data.get("image_size") or {}
    w, h = int(size.get("width", 0)), int(size.get("height", 0))
    if not (w and h):
        raise SystemExit("tracking JSON 에 image_size 가 없습니다")

    boxes = [f.get("box") for f in data["frames"]]
    if not all(boxes):
        raise SystemExit("프레임마다 box 가 없습니다. run-rtmw3d.py 를 다시 돌리십시오")

    if args.isolate and args.mode == "fixed":
        raise SystemExit("--isolate 는 follow 모드에서만 구현되어 있습니다")

    if args.mode == "fixed":
        # 전체 동선을 덮는 상자 하나
        union = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                 max(b[2] for b in boxes), max(b[3] for b in boxes)]
        x, y, side = padded(union, args.pad, w, h)
        vf = f"crop={side:.0f}:{side:.0f}:{x:.0f}:{y:.0f},scale={args.size}:{args.size}"
        print(f"고정 창 {side:.0f}px, 원본의 {100*side*side/(w*h):.0f}% 를 담는다")
    else:
        # 프레임마다 창이 움직인다. ffmpeg 의 sendcmd 로 crop 을 프레임 단위로
        # 몰아 보려 했으나 필터그래프 파싱에서 막혔다. 프레임을 직접 잘라 이어
        # 붙이는 편이 단순하고, 어느 프레임이 어디서 잘렸는지도 확실하다.
        import cv2
        import numpy as np

        fill = None
        segment = None
        if args.isolate:
            fill = background_color(args.input, w, h)
            print(f"배경색(BGR) = {fill.round(0)}")

            import torch
            from torchvision.models.detection import (
                maskrcnn_resnet50_fpn, MaskRCNN_ResNet50_FPN_Weights)
            from torchvision.transforms.functional import to_tensor
            device = "cuda:0" if torch.cuda.is_available() else "cpu"
            seg_model = maskrcnn_resnet50_fpn(
                weights=MaskRCNN_ResNet50_FPN_Weights.COCO_V1).eval().to(device)

            def segment(frame, subject_box):
                """주인공이 아닌 사람들의 지울 마스크(uint8 0/255)를 돌려준다."""
                tensor = to_tensor(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).to(device)
                with torch.no_grad():
                    found = seg_model([tensor])[0]
                keep = (found["labels"] == 1) & (found["scores"] > 0.5)
                det_boxes = found["boxes"][keep].cpu().numpy()
                det_masks = found["masks"][keep, 0].cpu().numpy()
                if len(det_boxes) == 0:
                    return None
                overlaps = [iou(subject_box, b) for b in det_boxes]
                subject_at = int(np.argmax(overlaps)) if max(overlaps, default=0) > 0.2 else -1
                others = [m for k, m in enumerate(det_masks) if k != subject_at]
                if not others:
                    return None
                erase = (np.max(np.stack(others), axis=0) > 0.5).astype(np.uint8)
                # 마스크를 넓혀 발끝·윤곽 잔해까지 지운다. 딱 맞게 지우면 사람
                # 모양 테두리가 남아 그 유령이 다시 사람으로 검출된다(0.79 실측).
                erase = cv2.dilate(erase, np.ones((21, 21), np.uint8))
                # 타인의 상자 전체를 지우는 것도 시험했으나 쓰지 않는다. 상자가
                # 주인공과 겹치는 프레임에서 주인공 마스크가 조금만 불완전해도
                # inpaint 가 주인공 주변을 뭉개서, 주인공 검출이 끊기는 프레임이
                # 9개 생겼다. 마스크가 놓친 가림 인물은 주인공 바로 뒤라서
                # 중앙에 고정된 트래커를 뺏을 가능성이 낮다고 보고 남겨 둔다.
                if subject_at >= 0:
                    erase[det_masks[subject_at] > 0.5] = 0  # 주인공 픽셀은 보호한다
                return erase * 255

        sides = [padded(b, args.pad, w, h)[2] for b in boxes]
        side = int(min(max(sides), min(w, h)))
        cap = cv2.VideoCapture(str(args.input))
        fps = cap.get(cv2.CAP_PROP_FPS) or float(data.get("fps") or 30)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # cv2 의 mp4v(MPEG-4 Part 2)는 언리얼 Capture Manager 가 거부한다
        # ("Video file format is unsupported"). 일단 임시 파일에 쓰고 마지막에
        # ffmpeg 로 H.264 로 다시 굽는다.
        raw_path = args.output.with_suffix(".raw.mp4")
        writer = cv2.VideoWriter(
            str(raw_path), cv2.VideoWriter_fourcc(*"mp4v"), fps,
            (args.size, args.size))
        written = 0
        for box in boxes:
            ok, frame = cap.read()
            if not ok:
                break
            if fill is not None:
                # 1겹: 다른 사람의 픽셀을 주변 바닥에서 채워(inpaint) 없앤다.
                # 단색으로 덮으면 사람 모양 실루엣이 남아 검출기가 유령을 잡는다.
                others = segment(frame, box)
                if others is not None:
                    frame = cv2.inpaint(frame, others, 5, cv2.INPAINT_TELEA)
                # 2겹: 주인공 상자(여유 포함) 밖을 배경색으로 마저 덮는다.
                # 마스크 검출이 놓친 사람(누운 인물)을 여기서 지운다. 경계는
                # 흐려서 딱딱한 합성선을 만들지 않는다.
                bx1, by1, bx2, by2 = isolate_mask(box, args.isolate_pad, w, h)
                keep = np.zeros((h, w), np.float32)
                keep[by1:by2, bx1:bx2] = 1.0
                keep = cv2.GaussianBlur(keep, (31, 31), 0)[..., None]
                frame = (frame * keep + fill * (1.0 - keep)).astype(np.uint8)
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            x = int(min(max(cx - side / 2, 0), w - side))
            y = int(min(max(cy - side / 2, 0), h - side))
            tile = frame[y:y + side, x:x + side]
            writer.write(cv2.resize(tile, (args.size, args.size)))
            written += 1
        cap.release()
        writer.release()
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(raw_path), "-c:v", "libx264", "-crf", "18",
             "-pix_fmt", "yuv420p", "-an", str(args.output)],
            capture_output=True, text=True)
        raw_path.unlink(missing_ok=True)
        if result.returncode != 0:
            print(result.stderr[-1200:])
            raise SystemExit("ffmpeg 재인코딩 실패")
        print(f"움직이는 창 {side}px · {written}프레임 · {args.size}x{args.size}")
        print(f"저장 {args.output}")
        return

    args.output.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-i", str(args.input), "-vf", vf,
           "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-an", str(args.output)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr[-1200:])
        raise SystemExit("ffmpeg 실패")
    print(f"저장 {args.output}")


if __name__ == "__main__":
    main()
