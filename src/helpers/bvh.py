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
from typing import Optional

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


def prepare_for_biped(
    text: str,
    prune: tuple[str, ...] = (),
    offset: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> str:
    """Rewrite BVH text so 3ds Max biped.loadMocapFile accepts it."""
    bvh = parse_bvh(text)
    bvh = strip_static_root(bvh)
    bvh = prune_joints(bvh, prune)
    bvh = rename_for_biped(bvh)
    bvh = offset_root(bvh, offset)
    return serialize_bvh(bvh)
