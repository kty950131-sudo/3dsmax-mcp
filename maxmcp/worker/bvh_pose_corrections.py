"""Apply canonical frame pose deltas to a generated BVH artifact."""

from __future__ import annotations

import json
import math
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from maxmcp.helpers.bvh import BvhFile, BvhJoint, parse_bvh, serialize_bvh


# 스켈레톤이 둘이다. 후처리(pose-prior)를 거치면 SOMA 18관절·ZYX 가 나오고, 후처리를
# 건너뛴 폴백 변환기는 Biped 호환 19관절·ZXY 를 낸다. 한쪽만 알면 다른 쪽에서
# "관절이 없다 / 채널 순서가 다르다" 로 통째로 터진다(2026-08-28 진단).
CANONICAL_TO_FALLBACK = {
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

# SOMA 18관절에는 목과 발가락이 없다. 그 자리에 키를 찍으면 얹을 곳이 없으므로
# 건너뛰고 옆에 기록을 남긴다 — 통째로 실패시키면 나머지 교정까지 버려진다.
CANONICAL_TO_SOMA = {
    "root": "Hips",
    "chest": "Chest",
    "left_shoulder": "LeftArm",
    "left_elbow": "LeftForeArm",
    "left_wrist": "LeftHand",
    "right_shoulder": "RightArm",
    "right_elbow": "RightForeArm",
    "right_wrist": "RightHand",
    "left_hip": "LeftLeg",
    "left_knee": "LeftShin",
    "left_ankle": "LeftFoot",
    "right_hip": "RightLeg",
    "right_knee": "RightShin",
    "right_ankle": "RightFoot",
}

# 옛 이름을 쓰는 곳이 있어 남겨 둔다(폴백이 기본이었다).
CANONICAL_TO_BVH = CANONICAL_TO_FALLBACK

_POSE_FIELDS = {"frame", "joint", "rotation"}
_POSE_FIELDS_WITH_TRANSLATION = _POSE_FIELDS | {"translation"}
_SUPPORTED_ORDERS = ("ZXY", "ZYX")

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


def _euler_to_quaternion(order: str, values: Sequence[float]) -> Quaternion:
    """채널에 적힌 순서대로 축 회전을 곱한다. BVH 는 왼쪽부터 차례로 적용한다."""
    result: Quaternion = (0.0, 0.0, 0.0, 1.0)
    for axis, degrees in zip(order, values, strict=True):
        result = _multiply(result, _axis_quaternion(axis, degrees))
    return _quaternion(result)


def _matrix(value: Quaternion) -> list[list[float]]:
    x, y, z, w = _quaternion(value)
    return [
        [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)],
        [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
        [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)],
    ]


def _quaternion_to_euler(order: str, value: Quaternion) -> tuple[float, float, float]:
    """`order` 순서의 각도로 되뽑는다. 짐벌(가운데 축 ±90°)에서는 첫 축으로 몰아준다."""
    m = _matrix(value)
    if order == "ZXY":
        x_angle = math.asin(max(-1.0, min(1.0, m[2][1])))
        if abs(m[2][1]) < 0.9999999:
            y_angle = math.atan2(-m[2][0], m[2][2])
            z_angle = math.atan2(-m[0][1], m[1][1])
        else:
            y_angle = 0.0
            z_angle = math.atan2(m[1][0], m[0][0])
        radians = (z_angle, x_angle, y_angle)
    elif order == "ZYX":
        y_angle = math.asin(max(-1.0, min(1.0, -m[2][0])))
        if abs(m[2][0]) < 0.9999999:
            x_angle = math.atan2(m[2][1], m[2][2])
            z_angle = math.atan2(m[1][0], m[0][0])
        else:
            x_angle = 0.0
            z_angle = math.atan2(-m[0][1], m[1][1])
        radians = (z_angle, y_angle, x_angle)
    else:  # pragma: no cover - 위에서 이미 걸러진다
        raise ValueError(f"unsupported rotation order: {order}")
    return tuple(math.degrees(angle) for angle in radians)


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


def choose_mapping(columns: Mapping[str, Any]) -> dict[str, str]:
    """BVH 에 있는 이름을 보고 어느 스켈레톤인지 가린다."""
    if "LeftUpArm" in columns:
        return CANONICAL_TO_FALLBACK
    if "LeftForeArm" in columns:
        return CANONICAL_TO_SOMA
    raise ValueError(
        "unknown BVH skeleton: expected LeftUpArm(폴백) 또는 LeftForeArm(SOMA)"
    )


POSE_EDIT_WINDOW = 5
"""키 하나가 앞뒤로 몇 프레임에 걸쳐 스며들지. 브라우저 미리보기와 같은 값이어야 한다
(src/lib/motion-pose-edits.ts 의 POSE_EDIT_WINDOW)."""

_IDENTITY: Quaternion = (0.0, 0.0, 0.0, 1.0)


def _slerp(a: Quaternion, b: Quaternion, t: float) -> Quaternion:
    dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]
    end = b
    if dot < 0.0:
        end = (-b[0], -b[1], -b[2], -b[3])
        dot = -dot
    if dot > 0.9995:
        out = [a[i] + (end[i] - a[i]) * t for i in range(4)]
        norm = math.sqrt(sum(v * v for v in out)) or 1.0
        return (out[0] / norm, out[1] / norm, out[2] / norm, out[3] / norm)
    theta = math.acos(dot)
    sin = math.sin(theta)
    wa = math.sin((1.0 - t) * theta) / sin
    wb = math.sin(t * theta) / sin
    return (
        a[0] * wa + end[0] * wb,
        a[1] * wa + end[1] * wb,
        a[2] * wa + end[2] * wb,
        a[3] * wa + end[3] * wb,
    )


def _lerp_translation(
    a: tuple[float, float, float] | None,
    b: tuple[float, float, float] | None,
    t: float,
) -> tuple[float, float, float] | None:
    if a is None and b is None:
        return None
    start = a or (0.0, 0.0, 0.0)
    end = b or (0.0, 0.0, 0.0)
    return tuple(start[i] + (end[i] - start[i]) * t for i in range(3))  # type: ignore[return-value]


def sample_pose_track(
    track: Sequence[tuple[int, Quaternion, tuple[float, float, float] | None]],
    frame: int,
    window: int = POSE_EDIT_WINDOW,
) -> tuple[Quaternion, tuple[float, float, float] | None] | None:
    """그 프레임에 얹을 값. 영향 밖이면 None.

    키를 **그 한 프레임에만** 얹던 예전 방식은 사람이 자세를 고칠 때마다 1프레임짜리
    튐을 하나씩 만들었다(작업 8b84816b 에서 사람이 찍은 키 12건과 튐 12개가 관절·프레임
    까지 일치, 2026-08-28). 키 사이는 잇고 바깥은 창 안에서 원래 자세로 되돌린다."""
    if not track:
        return None
    first_frame, first_rot, first_tr = track[0]
    last_frame, last_rot, last_tr = track[-1]
    if frame <= first_frame - window or frame >= last_frame + window:
        return None
    if frame < first_frame:
        t = (frame - (first_frame - window)) / window
        return _slerp(_IDENTITY, first_rot, t), _lerp_translation(None, first_tr, t)
    if frame > last_frame:
        t = (frame - last_frame) / window
        return _slerp(last_rot, _IDENTITY, t), _lerp_translation(last_tr, None, t)
    for (frame_a, rot_a, tr_a), (frame_b, rot_b, tr_b) in zip(track, track[1:]):
        if frame < frame_a or frame > frame_b:
            continue
        span = frame_b - frame_a
        t = 0.0 if span == 0 else (frame - frame_a) / span
        return _slerp(rot_a, rot_b, t), _lerp_translation(tr_a, tr_b, t)
    return first_rot, first_tr


def _apply_edit(
    bvh: BvhFile,
    columns: dict[str, tuple[BvhJoint, int]],
    mapping: Mapping[str, str],
    frame: int,
    canonical_joint: str,
    delta: Quaternion,
    translation: tuple[float, float, float] | None,
) -> str | None:
    """얹었으면 None, 이 스켈레톤에 없는 관절이면 그 이유를 돌려준다."""
    bvh_name = mapping.get(canonical_joint)
    if bvh_name is None:
        return f"{canonical_joint}: 이 스켈레톤에 대응 관절이 없다"
    entry = columns.get(bvh_name)
    if entry is None:
        return f"{canonical_joint}: BVH 에 {bvh_name} 가 없다"
    joint, first_column = entry
    rotation_channels = [
        channel for channel in joint.channels if channel.endswith("rotation")
    ]
    order = "".join(channel[0] for channel in rotation_channels)
    if order not in _SUPPORTED_ORDERS:
        raise ValueError(
            f"unsupported BVH rotation channel order for {bvh_name}: "
            f"{' '.join(rotation_channels)}"
        )

    rotation_columns = [
        first_column + joint.channels.index(channel)
        for channel in rotation_channels
    ]
    row = bvh.frames[frame]
    base = _euler_to_quaternion(order, [row[column] for column in rotation_columns])
    composed = _multiply(base, delta)
    for column, degrees in zip(
        rotation_columns,
        _quaternion_to_euler(order, composed),
        strict=True,
    ):
        row[column] = degrees

    if translation is not None:
        for axis, channel in enumerate(("Xposition", "Yposition", "Zposition")):
            if channel not in joint.channels:
                raise ValueError(f"root BVH channel is missing: {channel}")
            row[first_column + joint.channels.index(channel)] += translation[axis] * 100.0
    return None


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
    mapping = choose_mapping(columns)
    skipped: list[str] = []
    applied = 0
    seen: set[tuple[int, str]] = set()
    tracks: dict[str, list[tuple[int, Quaternion, tuple[float, float, float] | None]]] = {}
    for raw_edit in pose_edits:
        frame, joint, rotation, translation = _validate_edit(
            raw_edit,
            len(bvh.frames),
        )
        key = (frame, joint)
        if key in seen:
            raise ValueError("duplicate pose edit")
        seen.add(key)
        tracks.setdefault(joint, []).append((frame, rotation, translation))

    # 관절마다 키를 프레임 순서로 세우고, 창 안의 프레임에 보간한 값을 얹는다.
    for joint, track in tracks.items():
        track.sort(key=lambda item: item[0])
        low = max(0, track[0][0] - POSE_EDIT_WINDOW + 1)
        high = min(len(bvh.frames) - 1, track[-1][0] + POSE_EDIT_WINDOW - 1)
        reason: str | None = None
        for frame in range(low, high + 1):
            sampled = sample_pose_track(track, frame)
            if sampled is None:
                continue
            rotation, translation = sampled
            reason = _apply_edit(
                bvh, columns, mapping, frame, joint, rotation, translation,
            )
            if reason is not None:
                break
        if reason is None:
            applied += len(track)
        elif reason not in skipped:
            skipped.append(reason)

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
    # 무엇을 얹고 무엇을 건너뛰었는지 옆에 남긴다. 건너뛴 것을 조용히 삼키면
    # 사람이 찍은 키가 사라진 줄도 모른다(2026-08-28).
    report = {
        "skeleton": "fallback" if mapping is CANONICAL_TO_FALLBACK else "soma",
        "applied": applied,
        "skipped": skipped,
    }
    output.with_suffix(".pose_corrections.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return output
