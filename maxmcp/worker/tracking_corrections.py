"""Validate and apply owned web corrections to immutable RTMW3D output."""

from __future__ import annotations

import json
import math
from pathlib import Path
import tempfile
from typing import Any

from maxmcp.rtmw3d.motion import BODY23_NAMES, load_rtmw3d


_EDIT_FIELDS = {"frame", "joint", "x", "y", "state"}
RTMW3D_FOCAL = (1145.04940459, 1143.78109572)  # mmpose rtmpose3d 기본 카메라
_EDIT_STATES = {"manual", "propagated"}


def _coordinate(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("correction coordinate must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("correction coordinate must be finite and non-negative")
    return result


def apply_tracking_corrections(
    source_json: Path,
    edits_json: Path,
    output_json: Path,
) -> Path:
    """Write corrected tracking JSON without modifying the original artifact."""

    source = Path(source_json)
    edits_path = Path(edits_json)
    output = Path(output_json)
    if source.resolve() == output.resolve():
        raise ValueError("corrected output must not overwrite source")

    load_rtmw3d(source)
    document = json.loads(source.read_text(encoding="utf-8"))
    corrections = json.loads(edits_path.read_text(encoding="utf-8"))
    if not isinstance(corrections, list):
        raise ValueError("correction document must be an array")

    frames = document["frames"]
    seen: set[tuple[int, str]] = set()
    for correction in corrections:
        if not isinstance(correction, dict) or set(correction) != _EDIT_FIELDS:
            raise ValueError("correction entry is invalid")
        frame = correction["frame"]
        joint = correction["joint"]
        if (
            not isinstance(frame, int)
            or isinstance(frame, bool)
            or frame < 0
            or frame >= len(frames)
        ):
            raise ValueError("correction frame is out of range")
        if not isinstance(joint, str) or joint not in BODY23_NAMES:
            raise ValueError("correction joint is invalid")
        state = correction["state"]
        if not isinstance(state, str) or state not in _EDIT_STATES:
            raise ValueError("correction state is invalid")
        key = (frame, joint)
        if key in seen:
            raise ValueError("duplicate correction")
        seen.add(key)

        x = _coordinate(correction["x"])
        y = _coordinate(correction["y"])
        target = frames[frame]
        image_keypoints = target.get("image_keypoints")
        if not isinstance(image_keypoints, dict):
            raise ValueError("source frame has no image keypoints")
        # 편집 좌표는 픽셀, keypoints 는 미터다. 예전엔 픽셀을 미터 칸에 그대로
        # 넣어 관절이 수백 m 밖으로 날아가고 IK 가 팔다리를 접었다(2026-08-25 실측).
        # RTMW3D 의 핀홀 상수(f=1145.049, 1143.781)로 픽셀 이동량을 같은 깊이의
        # 미터 이동량으로 바꾼다. 깊이는 편집으로 알 수 없으니 그대로 둔다.
        previous_pixels = image_keypoints.get(joint)
        px, py, pz = target["keypoints"][joint]
        depth = -pz
        if not (isinstance(previous_pixels, list) and len(previous_pixels) == 2) or depth <= 0:
            raise ValueError("source frame has no usable camera depth for correction")
        u0, v0 = float(previous_pixels[0]), float(previous_pixels[1])
        image_keypoints[joint] = [x, y]
        target["keypoints"][joint] = [
            px + (x - u0) * depth / RTMW3D_FOCAL[0],
            py - (y - v0) * depth / RTMW3D_FOCAL[1],
            pz,
        ]

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(
                document,
                stream,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
        load_rtmw3d(temporary)
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output
