"""Validate and apply owned web corrections to immutable RTMW3D output."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from src.rtmw3d.motion import BODY23_NAMES, load_rtmw3d


_EDIT_FIELDS = {"frame", "joint", "x", "y", "state"}
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
        if correction["state"] not in _EDIT_STATES:
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
        image_keypoints[joint] = [x, y]
        z = target["keypoints"][joint][2]
        target["keypoints"][joint] = [x, -y, z]

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".part")
    try:
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        load_rtmw3d(temporary)
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return output
