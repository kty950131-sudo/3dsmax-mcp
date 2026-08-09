"""Validate RTMW3D keypoints and bake them into a Biped-compatible BVH."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.helpers.bvh import BvhFile, BvhJoint, serialize_bvh, unwrap_angles

BODY23_NAMES = (
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip", "left_knee",
    "right_knee", "left_ankle", "right_ankle", "left_big_toe",
    "left_small_toe", "left_heel", "right_big_toe", "right_small_toe",
    "right_heel",
)

Vector = tuple[float, float, float]
Quaternion = tuple[float, float, float, float]


@dataclass(frozen=True)
class Rtmw3dFrame:
    index: int
    keypoints: tuple[Vector, ...]
    scores: tuple[float, ...]


@dataclass(frozen=True)
class Rtmw3dMotion:
    source_video: str
    fps: float
    frames: tuple[Rtmw3dFrame, ...]


def _vector(value: Any, label: str) -> Vector:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{label} must contain three numbers")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{label} contains a non-finite number")
    return result


def load_rtmw3d(path: str | Path) -> Rtmw3dMotion:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != "artoke.rtmw3d.v1":
        raise ValueError("schema must be 'artoke.rtmw3d.v1'")
    source_video = data.get("source_video")
    if not isinstance(source_video, str) or not source_video:
        raise ValueError("source_video must be a non-empty string")
    fps = float(data.get("fps", 0))
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("fps must be positive")
    raw_frames = data.get("frames")
    if not isinstance(raw_frames, list) or not raw_frames:
        raise ValueError("frames must be a non-empty list")
    frames: list[Rtmw3dFrame] = []
    required = set(BODY23_NAMES)
    for index, raw in enumerate(raw_frames):
        if not isinstance(raw, dict) or raw.get("index") != index:
            raise ValueError("frame indexes must be consecutive and start at zero")
        points = raw.get("keypoints")
        scores = raw.get("scores")
        if not isinstance(points, dict) or set(points) != required:
            raise ValueError(f"frame {index} must contain exactly 23 keypoints")
        if not isinstance(scores, dict) or set(scores) != required:
            raise ValueError(f"frame {index} must contain exactly 23 scores")
        score_values = tuple(float(scores[name]) for name in BODY23_NAMES)
        if not all(math.isfinite(score) and 0 <= score <= 1 for score in score_values):
            raise ValueError(f"frame {index} scores must be within 0..1")
        frames.append(Rtmw3dFrame(
            index,
            tuple(_vector(points[name], f"frame {index}.{name}") for name in BODY23_NAMES),
            score_values,
        ))
    return Rtmw3dMotion(source_video, fps, tuple(frames))


def _add(a: Vector, b: Vector) -> Vector:
    return a[0] + b[0], a[1] + b[1], a[2] + b[2]


def _sub(a: Vector, b: Vector) -> Vector:
    return a[0] - b[0], a[1] - b[1], a[2] - b[2]


def _scale(a: Vector, value: float) -> Vector:
    return a[0] * value, a[1] * value, a[2] * value


def _length(a: Vector) -> float:
    return math.sqrt(sum(value * value for value in a))


def _normalize(a: Vector) -> Vector:
    length = _length(a)
    return (0.0, 1.0, 0.0) if length < 1e-8 else _scale(a, 1.0 / length)


def _cross(a: Vector, b: Vector) -> Vector:
    return a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]


def _q_mul(a: Quaternion, b: Quaternion) -> Quaternion:
    ax, ay, az, aw = a; bx, by, bz, bw = b
    return (
        aw*bx + ax*bw + ay*bz - az*by,
        aw*by - ax*bz + ay*bw + az*bx,
        aw*bz + ax*by - ay*bx + az*bw,
        aw*bw - ax*bx - ay*by - az*bz,
    )


def _q_inverse(q: Quaternion) -> Quaternion:
    return -q[0], -q[1], -q[2], q[3]


def _q_rotate(q: Quaternion, value: Vector) -> Vector:
    rotated = _q_mul(_q_mul(q, (value[0], value[1], value[2], 0.0)), _q_inverse(q))
    return rotated[0], rotated[1], rotated[2]


def _from_to(source: Vector, target: Vector) -> Quaternion:
    a, b = _normalize(source), _normalize(target)
    dot = max(-1.0, min(1.0, sum(x*y for x, y in zip(a, b))))
    if dot > 0.999999:
        return 0.0, 0.0, 0.0, 1.0
    if dot < -0.999999:
        axis = _normalize(_cross(a, (1.0, 0.0, 0.0)))
        if _length(axis) < 1e-6:
            axis = _normalize(_cross(a, (0.0, 0.0, 1.0)))
        return axis[0], axis[1], axis[2], 0.0
    axis = _cross(a, b)
    q = (axis[0], axis[1], axis[2], 1.0 + dot)
    norm = math.sqrt(sum(value * value for value in q))
    return tuple(value / norm for value in q)


def _to_zxy(q: Quaternion) -> tuple[float, float, float]:
    x, y, z, w = q
    r01 = 2*(x*y-z*w); r11 = 1-2*(x*x+z*z)
    r20 = 2*(x*z-y*w); r21 = 2*(y*z+x*w); r22 = 1-2*(x*x+y*y)
    xa = math.asin(max(-1.0, min(1.0, r21)))
    if abs(math.cos(xa)) > 1e-8:
        za, ya = math.atan2(-r01, r11), math.atan2(-r20, r22)
    else:
        za, ya = math.atan2(2*(x*y+z*w), 1-2*(y*y+z*z)), 0.0
    return tuple(math.degrees(value) for value in (za, xa, ya))


def _points(frame: Rtmw3dFrame) -> dict[str, Vector]:
    points = dict(zip(BODY23_NAMES, frame.keypoints))
    pelvis = _scale(_add(points["left_hip"], points["right_hip"]), 0.5)
    neck = _scale(_add(points["left_shoulder"], points["right_shoulder"]), 0.5)
    points["pelvis"] = pelvis
    points["torso"] = _add(pelvis, _scale(_sub(neck, pelvis), 0.55))
    points["neck"] = neck
    return points


def _hierarchy(first: dict[str, Vector]) -> BvhJoint:
    cm = lambda value: value * 100.0
    distance = lambda a, b: cm(_length(_sub(first[a], first[b])))
    arm = lambda side: BvhJoint(
        f"{side.title()}Collar", ((1 if side == "left" else -1) * distance(f"{side}_shoulder", "neck")/2, 0, 0), ["Zrotation", "Xrotation", "Yrotation"], [
            BvhJoint(f"{side.title()}UpArm", ((1 if side == "left" else -1) * distance(f"{side}_shoulder", "neck")/2, 0, 0), ["Zrotation", "Xrotation", "Yrotation"], [
                BvhJoint(f"{side.title()}LowArm", ((1 if side == "left" else -1) * distance(f"{side}_elbow", f"{side}_shoulder"), 0, 0), ["Zrotation", "Xrotation", "Yrotation"], [
                    BvhJoint(f"{side.title()}Hand", ((1 if side == "left" else -1) * distance(f"{side}_wrist", f"{side}_elbow"), 0, 0), ["Zrotation", "Xrotation", "Yrotation"])
                ])
            ])
        ])
    leg = lambda side: BvhJoint(
        f"{side.title()}UpLeg", ((1 if side == "left" else -1) * distance(f"{side}_hip", "pelvis"), 0, 0), ["Zrotation", "Xrotation", "Yrotation"], [
            BvhJoint(f"{side.title()}LowLeg", (0, -distance(f"{side}_knee", f"{side}_hip"), 0), ["Zrotation", "Xrotation", "Yrotation"], [
                BvhJoint(f"{side.title()}Foot", (0, -distance(f"{side}_ankle", f"{side}_knee"), 0), ["Zrotation", "Xrotation", "Yrotation"], [
                    BvhJoint(f"{side.title()}Toe", (0, 0, distance(f"{side}_big_toe", f"{side}_ankle")), ["Zrotation", "Xrotation", "Yrotation"])
                ])
            ])
        ])
    neck = BvhJoint("Neck", (0, distance("neck", "torso"), 0), ["Zrotation", "Xrotation", "Yrotation"], [arm("left"), arm("right")])
    chest = BvhJoint("Chest", (0, distance("torso", "pelvis"), 0), ["Zrotation", "Xrotation", "Yrotation"], [neck])
    return BvhJoint("Hips", (0, 0, 0), ["Xposition", "Yposition", "Zposition", "Zrotation", "Xrotation", "Yrotation"], [chest, leg("left"), leg("right")])


_DIRECTION = {
    "Hips": ((0,1,0), "pelvis", "torso"), "Chest": ((0,1,0), "torso", "neck"),
    "Neck": ((1,0,0), "neck", "left_shoulder"),
    "LeftUpArm": ((1,0,0), "left_shoulder", "left_elbow"),
    "LeftLowArm": ((1,0,0), "left_elbow", "left_wrist"),
    "RightUpArm": ((-1,0,0), "right_shoulder", "right_elbow"),
    "RightLowArm": ((-1,0,0), "right_elbow", "right_wrist"),
    "LeftUpLeg": ((0,-1,0), "left_hip", "left_knee"),
    "LeftLowLeg": ((0,-1,0), "left_knee", "left_ankle"),
    "LeftFoot": ((0,0,1), "left_ankle", "left_big_toe"),
    "RightUpLeg": ((0,-1,0), "right_hip", "right_knee"),
    "RightLowLeg": ((0,-1,0), "right_knee", "right_ankle"),
    "RightFoot": ((0,0,1), "right_ankle", "right_big_toe"),
}


def rtmw3d_to_bvh(motion: Rtmw3dMotion) -> str:
    first = _points(motion.frames[0])
    root = _hierarchy(first)
    origin = first["pelvis"]
    rows: list[list[float]] = []

    def visit(joint: BvhJoint, points: dict[str, Vector], parent_world: Quaternion, row: list[float]) -> None:
        if joint.name == "Hips":
            row.extend(value * 100.0 for value in _sub(points["pelvis"], origin))
        direction = _DIRECTION.get(joint.name)
        if direction is None:
            local = (0.0, 0.0, 0.0, 1.0)
        else:
            rest, start, end = direction
            target_world = _sub(points[end], points[start])
            target_local = _q_rotate(_q_inverse(parent_world), target_world)
            local = _from_to(rest, target_local)
        row.extend(_to_zxy(local))
        world = _q_mul(parent_world, local)
        for child in joint.children:
            visit(child, points, world, row)

    for frame in motion.frames:
        row: list[float] = []
        visit(root, _points(frame), (0.0, 0.0, 0.0, 1.0), row)
        rows.append(row)
    for column in range(3, len(rows[0])):
        values = unwrap_angles([row[column] for row in rows])
        for row, value in zip(rows, values):
            row[column] = value
    return serialize_bvh(BvhFile(root, 1.0 / motion.fps, rows))


def convert_rtmw3d_file(source: str | Path, output: str | Path) -> int:
    motion = load_rtmw3d(source)
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rtmw3d_to_bvh(motion), encoding="utf-8")
    return len(motion.frames)
