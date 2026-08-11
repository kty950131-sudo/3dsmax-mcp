"""BVH 채널값 → 조인트 월드 좌표 (순수 파이썬, numpy 미사용)."""

import math
from typing import Sequence

from maxmcp.helpers.bvh import BvhFile, BvhJoint

Vec3 = tuple[float, float, float]
Mat3 = tuple[Vec3, Vec3, Vec3]

_IDENTITY: Mat3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def _rot(axis: str, deg: float) -> Mat3:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    if axis == "X":
        return ((1.0, 0.0, 0.0), (0.0, c, -s), (0.0, s, c))
    if axis == "Y":
        return ((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c))
    return ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))


def _mul(a: Mat3, b: Mat3) -> Mat3:
    return tuple(
        tuple(sum(a[r][k] * b[k][c] for k in range(3)) for c in range(3))
        for r in range(3)
    )  # type: ignore[return-value]


def _apply(m: Mat3, v: Vec3) -> Vec3:
    return tuple(sum(m[r][c] * v[c] for c in range(3)) for r in range(3))  # type: ignore[return-value]


def fk(bvh: BvhFile, frame: int) -> dict[str, Vec3]:
    """frame 번째 프레임의 조인트별 월드 좌표를 구한다."""
    if not 0 <= frame < len(bvh.frames):
        raise ValueError(f"frame out of range: {frame}")
    row = bvh.frames[frame]
    out: dict[str, Vec3] = {}
    col = 0

    def visit(joint: BvhJoint, parent_pos: Vec3, parent_rot: Mat3) -> None:
        nonlocal col
        trans: Vec3 = (0.0, 0.0, 0.0)
        rot = _IDENTITY
        for name in joint.channels:
            value = row[col]
            axis = name[0].upper()
            if name.lower().endswith("position"):
                idx = {"X": 0, "Y": 1, "Z": 2}[axis]
                trans = tuple(  # type: ignore[assignment]
                    value if i == idx else trans[i] for i in range(3)
                )
            else:
                rot = _mul(rot, _rot(axis, value))
            col += 1

        local = tuple(joint.offset[i] + trans[i] for i in range(3))
        world = _apply(parent_rot, local)  # type: ignore[arg-type]
        pos = tuple(parent_pos[i] + world[i] for i in range(3))
        out[joint.name] = pos  # type: ignore[assignment]
        world_rot = _mul(parent_rot, rot)
        for child in joint.children:
            visit(child, pos, world_rot)  # type: ignore[arg-type]

    # 컬럼 순서는 _column_map 과 같은 전위 순회이므로 col 을 따라가면 된다
    visit(bvh.root, (0.0, 0.0, 0.0), _IDENTITY)
    return out


def bones(root: BvhJoint) -> list[tuple[str, str]]:
    """(부모 이름, 자식 이름) 쌍 목록. 그릴 뼈대다."""
    pairs: list[tuple[str, str]] = []

    def visit(joint: BvhJoint) -> None:
        for child in joint.children:
            pairs.append((joint.name, child.name))
            visit(child)

    visit(root)
    return pairs


def project(pos: Vec3, azimuth_deg: float) -> tuple[float, float]:
    """Y 축 둘레로 azimuth 만큼 돌린 뒤 정직교 투영한다. Y 가 화면 위."""
    rad = math.radians(azimuth_deg)
    x = pos[0] * math.cos(rad) + pos[2] * math.sin(rad)
    return (x, pos[1])


def bounds(positions: Sequence[Vec3], azimuth_deg: float) -> tuple[float, float, float, float]:
    """투영된 좌표의 (min_x, min_y, max_x, max_y). 썸네일 정규화용."""
    pts = [project(p, azimuth_deg) for p in positions]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))
