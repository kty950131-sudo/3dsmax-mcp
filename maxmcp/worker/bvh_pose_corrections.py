"""Apply canonical frame pose deltas to a generated BVH artifact."""

from __future__ import annotations

import math
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from maxmcp.helpers.bvh import BvhFile, BvhJoint, parse_bvh, serialize_bvh


CANONICAL_TO_BVH = {
    "root": "Hips",
    "chest": "Chest",
    "neck": "Neck",
    "left_shoulder": "LeftUpArm",
    "left_elbow": "LeftLowArm",
    "left_wrist": "LeftHand",
    "right_shoulder": "RightUpArm",
    "right_elbow": "RightLowArm",
    "right_wrist": "RightHand",
    "left_hip": "LeftUpLeg",
    "left_knee": "LeftLowLeg",
    "left_ankle": "LeftFoot",
    "left_toe": "LeftToe",
    "right_hip": "RightUpLeg",
    "right_knee": "RightLowLeg",
    "right_ankle": "RightFoot",
    "right_toe": "RightToe",
}

_POSE_FIELDS = {"frame", "joint", "rotation"}
_POSE_FIELDS_WITH_TRANSLATION = _POSE_FIELDS | {"translation"}
_ROTATION_CHANNELS = ["Zrotation", "Xrotation", "Yrotation"]

Quaternion = tuple[float, float, float, float]


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _quaternion(value: Any) -> Quaternion:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("Quaternion must contain four numbers")
    x, y, z, w = (_finite_number(item, "Quaternion") for item in value)
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if norm < 1e-8:
        raise ValueError("Quaternion must have a non-zero norm")
    return (x / norm, y / norm, z / norm, w / norm)


def _multiply(left: Quaternion, right: Quaternion) -> Quaternion:
    lx, ly, lz, lw = left
    rx, ry, rz, rw = right
    return (
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
        lw * rw - lx * rx - ly * ry - lz * rz,
    )


def _axis_quaternion(axis: str, degrees: float) -> Quaternion:
    half = math.radians(degrees) / 2.0
    sine = math.sin(half)
    cosine = math.cos(half)
    if axis == "X":
        return (sine, 0.0, 0.0, cosine)
    if axis == "Y":
        return (0.0, sine, 0.0, cosine)
    return (0.0, 0.0, sine, cosine)


def _zxy_to_quaternion(z: float, x: float, y: float) -> Quaternion:
    result: Quaternion = (0.0, 0.0, 0.0, 1.0)
    for axis, degrees in (("Z", z), ("X", x), ("Y", y)):
        result = _multiply(result, _axis_quaternion(axis, degrees))
    return _quaternion(result)


def _quaternion_to_zxy(value: Quaternion) -> tuple[float, float, float]:
    x, y, z, w = _quaternion(value)
    m11 = 1.0 - 2.0 * (y * y + z * z)
    m12 = 2.0 * (x * y - z * w)
    m21 = 2.0 * (x * y + z * w)
    m22 = 1.0 - 2.0 * (x * x + z * z)
    m31 = 2.0 * (x * z - y * w)
    m32 = 2.0 * (y * z + x * w)
    m33 = 1.0 - 2.0 * (x * x + y * y)

    x_angle = math.asin(max(-1.0, min(1.0, m32)))
    if abs(m32) < 0.9999999:
        y_angle = math.atan2(-m31, m33)
        z_angle = math.atan2(-m12, m22)
    else:
        y_angle = 0.0
        z_angle = math.atan2(m21, m11)
    return tuple(math.degrees(angle) for angle in (z_angle, x_angle, y_angle))


def _joint_columns(root: BvhJoint) -> dict[str, tuple[BvhJoint, int]]:
    result: dict[str, tuple[BvhJoint, int]] = {}
    column = 0

    def visit(joint: BvhJoint) -> None:
        nonlocal column
        if joint.name in result:
            raise ValueError(f"BVH joint name is duplicated: {joint.name}")
        result[joint.name] = (joint, column)
        column += len(joint.channels)
        for child in joint.children:
            visit(child)

    visit(root)
    return result


def _validate_edit(
    edit: Any,
    frame_count: int,
) -> tuple[int, str, Quaternion, tuple[float, float, float] | None]:
    if not isinstance(edit, Mapping) or set(edit) not in (
        _POSE_FIELDS,
        _POSE_FIELDS_WITH_TRANSLATION,
    ):
        raise ValueError("pose edit is invalid")
    frame = edit["frame"]
    if (
        isinstance(frame, bool)
        or not isinstance(frame, int)
        or frame < 0
        or frame >= frame_count
    ):
        raise ValueError("pose edit frame is out of range")
    joint = edit["joint"]
    if not isinstance(joint, str) or joint not in CANONICAL_TO_BVH:
        raise ValueError("pose edit joint is invalid")
    rotation = _quaternion(edit["rotation"])

    translation = None
    if "translation" in edit:
        if joint != "root":
            raise ValueError("only the root may have translation")
        value = edit["translation"]
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise ValueError("root translation must contain three numbers")
        translation = tuple(
            _finite_number(item, "root translation") for item in value
        )
    return frame, joint, rotation, translation


def _apply_edit(
    bvh: BvhFile,
    columns: dict[str, tuple[BvhJoint, int]],
    frame: int,
    canonical_joint: str,
    delta: Quaternion,
    translation: tuple[float, float, float] | None,
) -> None:
    bvh_name = CANONICAL_TO_BVH[canonical_joint]
    entry = columns.get(bvh_name)
    if entry is None:
        raise ValueError(f"pose edit joint is absent from BVH: {canonical_joint}")
    joint, first_column = entry
    rotation_channels = [
        channel for channel in joint.channels if channel.endswith("rotation")
    ]
    if rotation_channels != _ROTATION_CHANNELS:
        raise ValueError(
            f"unsupported BVH rotation channel order for {bvh_name}: "
            f"{' '.join(rotation_channels)}"
        )

    rotation_columns = [
        first_column + joint.channels.index(channel)
        for channel in _ROTATION_CHANNELS
    ]
    row = bvh.frames[frame]
    base = _zxy_to_quaternion(*(row[column] for column in rotation_columns))
    composed = _multiply(base, delta)
    for column, degrees in zip(
        rotation_columns,
        _quaternion_to_zxy(composed),
        strict=True,
    ):
        row[column] = degrees

    if translation is not None:
        for axis, channel in enumerate(("Xposition", "Yposition", "Zposition")):
            if channel not in joint.channels:
                raise ValueError(f"root BVH channel is missing: {channel}")
            row[first_column + joint.channels.index(channel)] += translation[axis] * 100.0


def apply_bvh_pose_corrections(
    source_bvh: Path,
    pose_edits: Sequence[Mapping[str, Any]],
    output_bvh: Path,
) -> Path:
    """Write a corrected BVH without modifying the original artifact."""

    source = Path(source_bvh)
    output = Path(output_bvh)
    if source.resolve() == output.resolve():
        raise ValueError("corrected output must not overwrite source")

    bvh = parse_bvh(source.read_text(encoding="utf-8"))
    columns = _joint_columns(bvh.root)
    seen: set[tuple[int, str]] = set()
    for raw_edit in pose_edits:
        frame, joint, rotation, translation = _validate_edit(
            raw_edit,
            len(bvh.frames),
        )
        key = (frame, joint)
        if key in seen:
            raise ValueError("duplicate pose edit")
        seen.add(key)
        _apply_edit(bvh, columns, frame, joint, rotation, translation)

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
            stream.write(serialize_bvh(bvh))
        parse_bvh(temporary.read_text(encoding="utf-8"))
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output
