"""Minimal BVH parsing/rewriting for 3ds Max Biped mocap import.

``biped.loadMocapFile`` silently returns false (no dialog, no error) unless
the BVH matches Character Studio's expectations, verified empirically on Max
2026 with kimodo/SMPL-X exports:

* No static dummy root above the hips (kimodo wraps ``ROOT Root`` ->
  ``JOINT Hips``, both with 6 channels).
* Joint names must come from CS's fixed vocabulary. Notably ``Spine``/
  ``Spine1`` are NOT recognized: extra spine links must be named ``Chest``,
  ``Chest2``, ``Chest3``; neck chains ``Neck``, ``Neck1``; limbs
  ``LeftCollar``/``LeftUpArm``/``LeftLowArm``, ``LeftUpLeg``/``LeftLowLeg``,
  ``LeftToe``. One unrecognized required-chain name rejects the whole file.
* Joints CS has no slot for (eyes, jaw, SMPL-X finger chains, ``*End`` leaf
  joints) are best pruned.

:func:`prepare_for_biped` applies all three fixes. A file whose rest skeleton
is not Y-up (kimodo's non-tpose export lays bones along +X) still loads but
converts to a mangled pose — detect via :func:`has_upright_spine` and prefer
the ``*_tpose`` export.
"""

from dataclasses import dataclass, field
from typing import Optional, Sequence

from maxmcp.helpers.quat import euler_to_quat, quat_mul, quat_rotate, quat_to_euler

_STATIC_EPS = 1e-4

# Joints Character Studio has no slot for; SMPL-X style exports carry them.
DEFAULT_BIPED_PRUNE = (
    "Jaw", "LeftEye", "RightEye", "HeadEnd",
    "LeftHandThumb1", "LeftHandIndex1", "LeftHandMiddle1",
    "LeftHandRing1", "LeftHandPinky1",
    "RightHandThumb1", "RightHandIndex1", "RightHandMiddle1",
    "RightHandRing1", "RightHandPinky1",
    "LeftToeEnd", "RightToeEnd",
)


@dataclass
class BvhJoint:
    name: str
    offset: tuple[float, float, float]
    channels: list[str]
    children: list["BvhJoint"] = field(default_factory=list)
    end_site: Optional[tuple[float, float, float]] = None


@dataclass
class BvhFile:
    root: BvhJoint
    frame_time: float
    frames: list[list[float]]


def parse_bvh(text: str) -> BvhFile:
    """Parse BVH text into a joint tree plus per-frame channel rows."""
    hier_part, sep, motion_part = text.partition("MOTION")
    if not sep:
        raise ValueError("BVH missing MOTION section")

    tokens = hier_part.split()
    if not tokens or tokens[0] != "HIERARCHY":
        raise ValueError("BVH missing HIERARCHY header")
    pos = 1
    if pos >= len(tokens) or tokens[pos] != "ROOT":
        raise ValueError("BVH missing ROOT joint")

    def parse_joint(idx: int) -> tuple[BvhJoint, int]:
        # tokens[idx] is "ROOT" or "JOINT"; next token is the name
        name = tokens[idx + 1]
        idx += 2
        if tokens[idx] != "{":
            raise ValueError(f"Expected '{{' after joint {name}")
        idx += 1
        if tokens[idx] != "OFFSET":
            raise ValueError(f"Expected OFFSET in joint {name}")
        offset = (float(tokens[idx + 1]), float(tokens[idx + 2]), float(tokens[idx + 3]))
        idx += 4
        channels: list[str] = []
        if tokens[idx] == "CHANNELS":
            count = int(tokens[idx + 1])
            channels = tokens[idx + 2 : idx + 2 + count]
            idx += 2 + count
        joint = BvhJoint(name=name, offset=offset, channels=list(channels))
        while tokens[idx] != "}":
            if tokens[idx] == "JOINT":
                child, idx = parse_joint(idx)
                joint.children.append(child)
            elif tokens[idx] == "End" and tokens[idx + 1] == "Site":
                if tokens[idx + 2] != "{" or tokens[idx + 3] != "OFFSET":
                    raise ValueError(f"Malformed End Site in joint {name}")
                joint.end_site = (
                    float(tokens[idx + 4]),
                    float(tokens[idx + 5]),
                    float(tokens[idx + 6]),
                )
                if tokens[idx + 7] != "}":
                    raise ValueError(f"Malformed End Site in joint {name}")
                idx += 8
            else:
                raise ValueError(f"Unexpected token '{tokens[idx]}' in joint {name}")
        return joint, idx + 1

    root, _ = parse_joint(pos)

    frame_time = 0.0
    frame_count = 0
    frames: list[list[float]] = []
    for line in motion_part.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("Frames:"):
            frame_count = int(line.split(":", 1)[1])
        elif line.startswith("Frame Time:"):
            frame_time = float(line.split(":", 1)[1])
        else:
            frames.append([float(v) for v in line.split()])

    total_channels = _channel_count(root)
    for i, row in enumerate(frames):
        if len(row) != total_channels:
            raise ValueError(
                f"Frame {i} has {len(row)} values, expected {total_channels}"
            )
    if frame_count and frame_count != len(frames):
        raise ValueError(f"Frames: header says {frame_count}, found {len(frames)}")

    return BvhFile(root=root, frame_time=frame_time, frames=frames)


def serialize_bvh(bvh: BvhFile) -> str:
    """Write a BvhFile back to BVH text."""
    lines = ["HIERARCHY"]

    def fmt(v: float) -> str:
        return f"{v:.6f}"

    def write_joint(joint: BvhJoint, depth: int, keyword: str) -> None:
        pad = "  " * depth
        lines.append(f"{pad}{keyword} {joint.name}")
        lines.append(f"{pad}{{")
        inner = "  " * (depth + 1)
        lines.append(f"{inner}OFFSET {fmt(joint.offset[0])} {fmt(joint.offset[1])} {fmt(joint.offset[2])}")
        if joint.channels:
            lines.append(f"{inner}CHANNELS {len(joint.channels)} " + " ".join(joint.channels))
        for child in joint.children:
            write_joint(child, depth + 1, "JOINT")
        if joint.end_site is not None:
            lines.append(f"{inner}End Site")
            lines.append(f"{inner}{{")
            site = "  " * (depth + 2)
            lines.append(f"{site}OFFSET {fmt(joint.end_site[0])} {fmt(joint.end_site[1])} {fmt(joint.end_site[2])}")
            lines.append(f"{inner}}}")
        if not joint.children and joint.end_site is None:
            # BVH leaves must carry an End Site
            lines.append(f"{inner}End Site")
            lines.append(f"{inner}{{")
            site = "  " * (depth + 2)
            lines.append(f"{site}OFFSET 0.000000 0.000000 0.000000")
            lines.append(f"{inner}}}")
        lines.append(f"{pad}}}")

    write_joint(bvh.root, 0, "ROOT")
    lines.append("MOTION")
    lines.append(f"Frames: {len(bvh.frames)}")
    lines.append(f"Frame Time: {bvh.frame_time:.8f}")
    for row in bvh.frames:
        lines.append(" ".join(f"{v:.6f}" for v in row))
    return "\n".join(lines) + "\n"


def _channel_count(joint: BvhJoint) -> int:
    return len(joint.channels) + sum(_channel_count(c) for c in joint.children)


def _column_map(root: BvhJoint) -> dict[int, tuple[int, int]]:
    """Map id(joint) -> (first_column, channel_count) in hierarchy order."""
    mapping: dict[int, tuple[int, int]] = {}
    col = 0

    def visit(joint: BvhJoint) -> None:
        nonlocal col
        mapping[id(joint)] = (col, len(joint.channels))
        col += len(joint.channels)
        for child in joint.children:
            visit(child)

    visit(root)
    return mapping


def strip_static_root(bvh: BvhFile) -> BvhFile:
    """Drop a static wrapper root (all channels ~0 every frame) above the real root.

    Returns a new BvhFile; returns the input unchanged when the pattern does
    not apply (root is already the real root, wrapper is animated, or wrapper
    has multiple children).
    """
    root = bvh.root
    if len(root.children) != 1 or not root.channels:
        return bvh
    n = len(root.channels)
    for row in bvh.frames:
        if any(abs(v) > _STATIC_EPS for v in row[:n]):
            return bvh

    child = root.children[0]
    new_root = BvhJoint(
        name=child.name,
        offset=(
            root.offset[0] + child.offset[0],
            root.offset[1] + child.offset[1],
            root.offset[2] + child.offset[2],
        ),
        channels=list(child.channels),
        children=child.children,
        end_site=child.end_site,
    )
    new_frames = [row[n:] for row in bvh.frames]
    return BvhFile(root=new_root, frame_time=bvh.frame_time, frames=new_frames)


def prune_joints(bvh: BvhFile, names: tuple[str, ...]) -> BvhFile:
    """Remove named subtrees (and their motion columns). Root cannot be pruned."""
    if not names:
        return bvh
    columns = _column_map(bvh.root)
    dropped: list[tuple[int, int]] = []

    def copy_joint(joint: BvhJoint) -> BvhJoint:
        kept: list[BvhJoint] = []
        for child in joint.children:
            if child.name in names:
                _collect_columns(child, columns, dropped)
                continue
            kept.append(copy_joint(child))
        end_site = joint.end_site
        if joint.children and not kept and end_site is None:
            # all children pruned: keep the first child's offset as the bone tip
            end_site = joint.children[0].offset
        return BvhJoint(
            name=joint.name,
            offset=joint.offset,
            channels=list(joint.channels),
            children=kept,
            end_site=end_site,
        )

    new_root = copy_joint(bvh.root)
    if not dropped:
        return bvh
    drop_cols = set()
    for start, count in dropped:
        drop_cols.update(range(start, start + count))
    new_frames = [
        [v for i, v in enumerate(row) if i not in drop_cols] for row in bvh.frames
    ]
    return BvhFile(root=new_root, frame_time=bvh.frame_time, frames=new_frames)


def _axis_values(joint: BvhJoint, row: Sequence[float], start: int, suffix: str):
    """채널 이름으로 축별 값을 읽는다. 채널 순서를 위치로 가정하지 않는다 —
    ``Zrotation Yrotation Xrotation`` 과 ``Xrotation Yrotation Zrotation`` 이
    같은 열 순서를 뜻하지 않기 때문이다."""
    axes = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    for index, channel in enumerate(joint.channels):
        if channel.endswith(suffix):
            axes[channel[0].upper()] = row[start + index]
    return axes["X"], axes["Y"], axes["Z"]


def _write_axis_values(joint: BvhJoint, row: list, start: int, suffix: str, values) -> None:
    axes = dict(zip("XYZ", values))
    for index, channel in enumerate(joint.channels):
        if channel.endswith(suffix):
            row[start + index] = axes[channel[0].upper()]


def merge_into_parent(bvh: BvhFile, name: str) -> BvhFile:
    """관절 하나를 부모에 합치고 그 자식들을 부모에 직접 붙인다.

    Character Studio 가 모르는 이름이 필수 체인 중간에 끼어 있으면 파일 전체가
    거부된다. Max Biped 에서 온 클립의 ``Bip001`` / ``Bip001 Pelvis`` 처럼 실제로는
    한 관절인 쌍이 그렇다 — BVH 표준에서 그 둘은 ``Hips`` 하나다.

    동작은 버리지 않는다. 합성은 정확하다::

        R_parent' = R_parent · R_child
        T_parent' = T_parent + R_parent · (O_child + T_child)

    회전은 사원수로 합성한다(각도를 더하면 축이 달라 틀린다). 자식의 rest 오프셋이
    부모 회전에 딸려 도는 몫은 프레임마다 부모의 위치 채널에 흡수시킨다 — 이 항을
    빼먹으면 부모가 회전할 때만 어긋나서, 가만히 선 클립에서는 안 보이고 도는
    클립에서만 틀린다.

    합칠 관절의 자식들은 오프셋도 회전도 그대로 둔다. 위 식이 그 앞의 변환을
    똑같이 재현하므로 손댈 이유가 없다.
    """
    parent = None
    target = None

    def find(joint: BvhJoint) -> None:
        nonlocal parent, target
        for child in joint.children:
            if child.name == name:
                parent, target = joint, child
                return
            find(child)

    find(bvh.root)
    if target is None:
        if bvh.root.name == name:
            raise ValueError(f"루트({name})는 부모가 없어 합칠 수 없습니다")
        return bvh
    if not any(c.endswith("position") for c in parent.channels):
        raise ValueError(
            f"{parent.name} 에 위치 채널이 없어 {name} 의 오프셋을 흡수할 수 없습니다"
        )

    columns = _column_map(bvh.root)
    parent_start, _ = columns[id(parent)]
    target_start, target_count = columns[id(target)]

    new_frames: list[list[float]] = []
    for row in bvh.frames:
        merged = list(row)
        px, py, pz = _axis_values(parent, row, parent_start, "rotation")
        cx, cy, cz = _axis_values(target, row, target_start, "rotation")
        parent_quat = euler_to_quat(px, py, pz)
        combined = quat_mul(parent_quat, euler_to_quat(cx, cy, cz))
        _write_axis_values(parent, merged, parent_start, "rotation", quat_to_euler(combined))

        tx, ty, tz = _axis_values(target, row, target_start, "position")
        carried = quat_rotate(
            parent_quat,
            (target.offset[0] + tx, target.offset[1] + ty, target.offset[2] + tz),
        )
        moved = _axis_values(parent, row, parent_start, "position")
        _write_axis_values(
            parent, merged, parent_start, "position",
            (moved[0] + carried[0], moved[1] + carried[1], moved[2] + carried[2]),
        )
        new_frames.append([v for i, v in enumerate(merged)
                           if not target_start <= i < target_start + target_count])

    def copy_joint(joint: BvhJoint) -> BvhJoint:
        children: list[BvhJoint] = []
        for child in joint.children:
            # 합칠 관절 자리에 그 자식들이 그대로 들어선다 — 순서를 지켜야 남은
            # 관절들의 열 순서가 프레임 자르기와 어긋나지 않는다.
            if child is target:
                children.extend(copy_joint(grand) for grand in child.children)
            else:
                children.append(copy_joint(child))
        return BvhJoint(
            name=joint.name,
            offset=joint.offset,
            channels=list(joint.channels),
            children=children,
            end_site=joint.end_site,
        )

    return BvhFile(root=copy_joint(bvh.root), frame_time=bvh.frame_time, frames=new_frames)


def _collect_columns(
    joint: BvhJoint,
    columns: dict[int, tuple[int, int]],
    out: list[tuple[int, int]],
) -> None:
    out.append(columns[id(joint)])
    for child in joint.children:
        _collect_columns(child, columns, out)


def _joint_names(joint: BvhJoint) -> set[str]:
    names = {joint.name}
    for child in joint.children:
        names |= _joint_names(child)
    return names


def _biped_rename_map(names: set[str]) -> dict[str, str]:
    """Map kimodo/SMPL-X joint names onto Character Studio's BVH vocabulary.

    Conditional so files already using CS names pass through untouched.
    """
    mapping: dict[str, str] = {}
    if "Spine1" in names and "Spine" not in names:
        mapping["Spine1"] = "Chest"
        if "Spine2" in names:
            mapping["Spine2"] = "Chest2"
        if "Chest" in names:
            mapping["Chest"] = "Chest3"
    if "Neck1" in names and "Neck" not in names:
        mapping["Neck1"] = "Neck"
        if "Neck2" in names:
            mapping["Neck2"] = "Neck1"
    for side in ("Left", "Right"):
        if f"{side}Shoulder" in names and f"{side}Collar" not in names:
            mapping[f"{side}Shoulder"] = f"{side}Collar"
        if f"{side}Arm" in names and f"{side}UpArm" not in names:
            mapping[f"{side}Arm"] = f"{side}UpArm"
        if f"{side}ForeArm" in names:
            mapping[f"{side}ForeArm"] = f"{side}LowArm"
        if f"{side}Leg" in names and f"{side}UpLeg" not in names:
            mapping[f"{side}Leg"] = f"{side}UpLeg"
        if f"{side}Shin" in names:
            mapping[f"{side}Shin"] = f"{side}LowLeg"
        if f"{side}ToeBase" in names:
            mapping[f"{side}ToeBase"] = f"{side}Toe"
    return mapping


def rename_for_biped(bvh: BvhFile) -> BvhFile:
    """Apply the CS vocabulary rename map (one-shot, collision-safe)."""
    mapping = _biped_rename_map(_joint_names(bvh.root))
    if not mapping:
        return bvh

    def copy_joint(joint: BvhJoint) -> BvhJoint:
        return BvhJoint(
            name=mapping.get(joint.name, joint.name),
            offset=joint.offset,
            channels=list(joint.channels),
            children=[copy_joint(c) for c in joint.children],
            end_site=joint.end_site,
        )

    return BvhFile(
        root=copy_joint(bvh.root), frame_time=bvh.frame_time, frames=bvh.frames
    )


def has_upright_spine(text: str) -> bool:
    """True when the rest skeleton's spine points up (Y-major offset).

    kimodo's non-tpose export lays bones along +X; such files load onto a
    biped but produce mangled poses. Prefer the ``*_tpose`` export.
    """
    bvh = strip_static_root(parse_bvh(text))
    spine = next(
        (
            c
            for c in bvh.root.children
            if any(k in c.name.lower() for k in ("spine", "chest", "abdomen"))
        ),
        None,
    )
    if spine is None and bvh.root.children:
        spine = bvh.root.children[0]
    if spine is None:
        return True
    return abs(spine.offset[1]) >= abs(spine.offset[0])


def offset_root(bvh: BvhFile, offset: tuple[float, float, float]) -> BvhFile:
    """Add a constant world offset to the root's position channels (all frames)."""
    if offset == (0.0, 0.0, 0.0):
        return bvh
    axis_col = {}
    for i, ch in enumerate(bvh.root.channels):
        low = ch.lower()
        if low == "xposition":
            axis_col[0] = i
        elif low == "yposition":
            axis_col[1] = i
        elif low == "zposition":
            axis_col[2] = i
    new_frames = []
    for row in bvh.frames:
        new_row = list(row)
        for axis, col in axis_col.items():
            new_row[col] += offset[axis]
        new_frames.append(new_row)
    return BvhFile(root=bvh.root, frame_time=bvh.frame_time, frames=new_frames)


def retime(bvh: BvhFile, speed: float) -> BvhFile:
    """Scale playback speed by stretching the frame time (Mixamo Overdrive)."""
    if speed == 1.0:
        return bvh
    if speed <= 0:
        raise ValueError(f"speed must be positive, got {speed}")
    return BvhFile(
        root=bvh.root, frame_time=bvh.frame_time / speed, frames=bvh.frames
    )


def unwrap_angles(values: list[float]) -> list[float]:
    """오일러 각 수열을 연속으로 편다.

    ±180° 경계를 넘는 지점에서 360° 를 더하거나 빼, 인접 프레임 차이가
    항상 최단 경로가 되게 한다. 보간 전에 반드시 거쳐야 한다.
    """
    if not values:
        return []
    out = [values[0]]
    offset = 0.0
    for prev, cur in zip(values, values[1:]):
        delta = cur - prev
        if delta > 180.0:
            offset -= 360.0
        elif delta < -180.0:
            offset += 360.0
        out.append(cur + offset)
    return out


def _flat_channels(root: BvhJoint) -> list[str]:
    """모션 행의 컬럼 순서대로 채널 이름을 나열한다 (_column_map 과 같은 순회)."""
    names: list[str] = []

    def visit(joint: BvhJoint) -> None:
        names.extend(joint.channels)
        for child in joint.children:
            visit(child)

    visit(root)
    return names


def warp(bvh: BvhFile, time_map: Sequence[float]) -> BvhFile:
    """time_map[i] = 출력 프레임 i 가 가져올 원본 프레임 위치(소수 허용).

    ``retime`` 은 frame_time 만 바꾸므로 균일 속도만 표현할 수 있다. 구간별로
    다른 속도(타임워프)는 이 함수로 프레임을 재샘플해야 한다. frame_time 은
    유지되고 프레임 수만 달라진다.

    회전 채널은 언랩 후 보간하므로 출력값이 ±180° 를 벗어날 수 있다. 이는
    의도된 동작이다 — 연속적인 각도 수열이 소비자 입장에서 더 안전하다.
    """
    if not time_map:
        raise ValueError("time_map must not be empty")
    if not bvh.frames:
        raise ValueError("bvh has no frames")
    for prev, cur in zip(time_map, time_map[1:]):
        if cur < prev:
            raise ValueError(f"time_map must be non-decreasing: {prev} -> {cur}")

    n = len(bvh.frames)
    if len(time_map) == n and all(t == i for i, t in enumerate(time_map)):
        return bvh  # 항등 사상 — 원본을 손대지 않는다

    channels = _flat_channels(bvh.root)
    columns = [list(col) for col in zip(*bvh.frames)]
    prepared = [
        unwrap_angles(col) if name.lower().endswith("rotation") else col
        for name, col in zip(channels, columns)
    ]

    frames: list[list[float]] = []
    for t in time_map:
        clamped = min(max(t, 0.0), float(n - 1))
        lo = int(clamped)
        hi = min(lo + 1, n - 1)
        frac = clamped - lo
        frames.append([col[lo] + (col[hi] - col[lo]) * frac for col in prepared])
    return BvhFile(root=bvh.root, frame_time=bvh.frame_time, frames=frames)


def trim(bvh: BvhFile, start_frac: float, end_frac: float) -> BvhFile:
    """Keep only the [start_frac, end_frac] slice of the clip (Mixamo Trim)."""
    if (start_frac, end_frac) == (0.0, 1.0):
        return bvh
    if not 0.0 <= start_frac < end_frac <= 1.0:
        raise ValueError(f"invalid trim range: {start_frac}..{end_frac}")
    n = len(bvh.frames)
    start = int(n * start_frac)
    end = max(start + 1, int(n * end_frac))
    return BvhFile(
        root=bvh.root, frame_time=bvh.frame_time, frames=bvh.frames[start:end]
    )


def prepare_for_biped(
    text: str,
    prune: tuple[str, ...] = (),
    offset: tuple[float, float, float] = (0.0, 0.0, 0.0),
    speed: float = 1.0,
    trim_range: tuple[float, float] = (0.0, 1.0),
    time_map: Optional[Sequence[float]] = None,
) -> str:
    """Rewrite BVH text so 3ds Max biped.loadMocapFile accepts it.

    ``time_map`` 을 주면 균일 ``speed`` 대신 비균일 타임워프를 적용한다.
    인덱스는 트림된 뒤 클립 기준이다. 주지 않으면 기존 경로 그대로다.
    """
    bvh = parse_bvh(text)
    bvh = strip_static_root(bvh)
    bvh = prune_joints(bvh, prune)
    bvh = rename_for_biped(bvh)
    bvh = offset_root(bvh, offset)
    bvh = trim(bvh, trim_range[0], trim_range[1])
    if time_map is None:
        bvh = retime(bvh, speed)
    else:
        bvh = warp(bvh, time_map)
    return serialize_bvh(bvh)
