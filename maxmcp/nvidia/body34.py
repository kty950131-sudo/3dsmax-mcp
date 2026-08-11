"""Convert NVIDIA Maxine 34-joint body tracks to Biped-compatible BVH."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from maxmcp.helpers.bvh import BvhFile, BvhJoint, serialize_bvh, unwrap_angles

BODY34_JOINTS = (
    "pelvis", "left_hip", "right_hip", "torso", "left_knee", "right_knee",
    "neck", "left_ankle", "right_ankle", "left_big_toe", "right_big_toe",
    "left_small_toe", "right_small_toe", "left_heel", "right_heel", "nose",
    "left_eye", "right_eye", "left_ear", "right_ear", "left_shoulder",
    "right_shoulder", "left_elbow", "right_elbow", "left_wrist", "right_wrist",
    "left_pinky_knuckle", "right_pinky_knuckle", "left_middle_tip",
    "right_middle_tip", "left_index_knuckle", "right_index_knuckle",
    "left_thumb_tip", "right_thumb_tip",
)

_SCHEMA = "artoke.nvidia-body34.v1"
_ROOT_CHANNELS = [
    "Xposition", "Yposition", "Zposition", "Zrotation", "Xrotation", "Yrotation"
]
_ROTATION_CHANNELS = ["Zrotation", "Xrotation", "Yrotation"]


@dataclass(frozen=True)
class BodyJoint:
    rotation_xyzw: tuple[float, float, float, float]
    confidence: float


@dataclass(frozen=True)
class BodyFrame:
    index: int
    root_translation: tuple[float, float, float]
    joints: tuple[BodyJoint, ...]


@dataclass(frozen=True)
class Body34Track:
    source_video: str
    fps: float
    reference_pose: tuple[tuple[float, float, float], ...]
    frames: tuple[BodyFrame, ...]


def _finite_vector(value: Any, size: int, label: str) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != size:
        raise ValueError(f"{label} must contain {size} numbers")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{label} contains a non-finite number")
    return result


def load_body34(path: str | Path) -> Body34Track:
    """Load and strictly validate an ``artoke.nvidia-body34.v1`` JSON file."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read Body34 JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != _SCHEMA:
        raise ValueError(f"schema must be {_SCHEMA!r}")
    source_video = data.get("source_video")
    if not isinstance(source_video, str) or not source_video:
        raise ValueError("source_video must be a non-empty string")
    try:
        fps = float(data.get("fps"))
    except (TypeError, ValueError) as exc:
        raise ValueError("fps must be a positive number") from exc
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("fps must be a positive number")

    reference_data = data.get("reference_pose")
    if not isinstance(reference_data, dict) or set(reference_data) != set(BODY34_JOINTS):
        raise ValueError("reference_pose must contain exactly the 34 NVIDIA joints")
    reference_pose = tuple(
        _finite_vector(reference_data[name], 3, f"reference_pose.{name}")
        for name in BODY34_JOINTS
    )

    frame_data = data.get("frames")
    if not isinstance(frame_data, list) or not frame_data:
        raise ValueError("frames must be a non-empty list")
    frames: list[BodyFrame] = []
    for expected_index, item in enumerate(frame_data):
        if not isinstance(item, dict) or item.get("index") != expected_index:
            raise ValueError("frame indexes must be consecutive and start at zero")
        joints_data = item.get("joints")
        if not isinstance(joints_data, dict) or set(joints_data) != set(BODY34_JOINTS):
            raise ValueError(f"frame {expected_index} must contain exactly 34 joints")
        joints: list[BodyJoint] = []
        for name in BODY34_JOINTS:
            joint = joints_data[name]
            if not isinstance(joint, dict):
                raise ValueError(f"frame {expected_index}.{name} must be an object")
            try:
                confidence = float(joint.get("confidence"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"frame {expected_index}.{name} confidence is invalid") from exc
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError(f"frame {expected_index}.{name} confidence must be 0..1")
            rotation = _finite_vector(
                joint.get("rotation_xyzw"), 4, f"frame {expected_index}.{name}.rotation_xyzw"
            )
            if confidence > 0 and math.sqrt(sum(v * v for v in rotation)) < 1e-12:
                raise ValueError(f"frame {expected_index}.{name} rotation is zero")
            joints.append(BodyJoint(rotation, confidence))
        frames.append(
            BodyFrame(
                expected_index,
                _finite_vector(item.get("root_translation"), 3, f"frame {expected_index}.root_translation"),
                tuple(joints),
            )
        )
    return Body34Track(source_video, fps, reference_pose, tuple(frames))


def _offset(track: Body34Track, child: str, parent: str, scale: float = 1.0) -> tuple[float, float, float]:
    lookup = dict(zip(BODY34_JOINTS, track.reference_pose))
    return tuple((a - b) * 100.0 * scale for a, b in zip(lookup[child], lookup[parent]))


def _joint(name: str, offset: tuple[float, float, float], children: list[BvhJoint] | None = None) -> BvhJoint:
    return BvhJoint(name, offset, list(_ROTATION_CHANNELS), children or [])


def _hierarchy(track: Body34Track) -> BvhJoint:
    left_hand = _joint("LeftHand", _offset(track, "left_wrist", "left_elbow"))
    left_low = _joint("LeftLowArm", _offset(track, "left_elbow", "left_shoulder"), [left_hand])
    left_up = _joint("LeftUpArm", _offset(track, "left_shoulder", "neck", 0.5), [left_low])
    left_collar = _joint("LeftCollar", _offset(track, "left_shoulder", "neck", 0.5), [left_up])
    right_hand = _joint("RightHand", _offset(track, "right_wrist", "right_elbow"))
    right_low = _joint("RightLowArm", _offset(track, "right_elbow", "right_shoulder"), [right_hand])
    right_up = _joint("RightUpArm", _offset(track, "right_shoulder", "neck", 0.5), [right_low])
    right_collar = _joint("RightCollar", _offset(track, "right_shoulder", "neck", 0.5), [right_up])
    neck = _joint("Neck", _offset(track, "neck", "torso"), [left_collar, right_collar])
    chest = _joint("Chest", _offset(track, "torso", "pelvis"), [neck])

    def leg(side: str) -> BvhJoint:
        toe = _joint(f"{side.title()}Toe", _offset(track, f"{side}_big_toe", f"{side}_ankle"))
        foot = _joint(f"{side.title()}Foot", _offset(track, f"{side}_ankle", f"{side}_knee"), [toe])
        low = _joint(f"{side.title()}LowLeg", _offset(track, f"{side}_knee", f"{side}_hip"), [foot])
        return _joint(f"{side.title()}UpLeg", _offset(track, f"{side}_hip", "pelvis"), [low])

    return BvhJoint("Hips", (0.0, 0.0, 0.0), list(_ROOT_CHANNELS), [chest, leg("left"), leg("right")])


def _quaternion_to_zxy(rotation: tuple[float, float, float, float]) -> tuple[float, float, float]:
    x, y, z, w = rotation
    norm = math.sqrt(x*x + y*y + z*z + w*w)
    x, y, z, w = x/norm, y/norm, z/norm, w/norm
    r01 = 2 * (x*y - z*w)
    r11 = 1 - 2 * (x*x + z*z)
    r20 = 2 * (x*z - y*w)
    r21 = 2 * (y*z + x*w)
    r22 = 1 - 2 * (x*x + y*y)
    x_angle = math.asin(max(-1.0, min(1.0, r21)))
    if abs(math.cos(x_angle)) > 1e-8:
        z_angle = math.atan2(-r01, r11)
        y_angle = math.atan2(-r20, r22)
    else:
        z_angle = math.atan2(2 * (x*y + z*w), 1 - 2 * (y*y + z*z))
        y_angle = 0.0
    return tuple(math.degrees(value) for value in (z_angle, x_angle, y_angle))


_MOTION_SOURCE = {
    "Hips": "pelvis", "Chest": "torso", "Neck": "neck",
    "LeftCollar": None, "RightCollar": None,
    "LeftUpArm": "left_shoulder", "LeftLowArm": "left_elbow", "LeftHand": "left_wrist",
    "RightUpArm": "right_shoulder", "RightLowArm": "right_elbow", "RightHand": "right_wrist",
    "LeftUpLeg": "left_hip", "LeftLowLeg": "left_knee", "LeftFoot": "left_ankle",
    "LeftToe": "left_big_toe", "RightUpLeg": "right_hip", "RightLowLeg": "right_knee",
    "RightFoot": "right_ankle", "RightToe": "right_big_toe",
}


def body34_to_bvh(track: Body34Track) -> str:
    """Convert a validated track to a centimetre, Y-up Biped BVH document."""
    root = _hierarchy(track)
    joint_indexes = {name: i for i, name in enumerate(BODY34_JOINTS)}
    previous = {name: (0.0, 0.0, 0.0, 1.0) for name in BODY34_JOINTS}
    rows: list[list[float]] = []

    def visit(joint: BvhJoint, frame: BodyFrame, row: list[float]) -> None:
        if joint.name == "Hips":
            row.extend(value * 100.0 for value in frame.root_translation)
        source = _MOTION_SOURCE[joint.name]
        if source is None:
            row.extend((0.0, 0.0, 0.0))
        else:
            body_joint = frame.joints[joint_indexes[source]]
            if body_joint.confidence > 0:
                previous[source] = body_joint.rotation_xyzw
            row.extend(_quaternion_to_zxy(previous[source]))
        for child in joint.children:
            visit(child, frame, row)

    for frame in track.frames:
        row: list[float] = []
        visit(root, frame, row)
        rows.append(row)
    for column in range(3, len(rows[0])):
        values = unwrap_angles([row[column] for row in rows])
        for row, value in zip(rows, values):
            row[column] = value
    return serialize_bvh(BvhFile(root, 1.0 / track.fps, rows))


def convert_body34_file(source: str | Path, output: str | Path) -> int:
    """Convert a Body34 JSON file and return its written frame count."""
    track = load_body34(source)
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body34_to_bvh(track), encoding="utf-8")
    return len(track.frames)
