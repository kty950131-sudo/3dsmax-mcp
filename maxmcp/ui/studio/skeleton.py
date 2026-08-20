"""BVH 채널값 → 조인트 월드 좌표 (순수 파이썬, numpy 미사용)."""

import math
from typing import Optional, Sequence

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
        # 위치 채널은 OFFSET 에 더하는 델타가 아니라 그 축의 로컬 좌표 **그 자체**다.
        # Blender BVH 익스포터(fbx_to_bvh 가 쓴다)는 자식 관절 위치 채널에 쉬는
        # 자세의 OFFSET 과 거의 같은 절대값을 넣는데, 여기에 OFFSET 을 또 더하면
        # 뼈가 두 배가 된다 — 실측으로 fk 가 잰 허벅지 0.7515 는 오프셋 0.3757 의
        # 정확히 2배였고, 두 값이 미묘하게 다른 관절(엘렌 무릎: z 0.0039 vs
        # 0.0004)에서는 자세가 틀어져 프리뷰가 역관절을 그렸다. Blender 임포터와
        # 원본 FBX 를 정답으로 두고 맞췄다(무릎각 27도 -> 36.12도).
        local_list = list(joint.offset)
        rot = _IDENTITY
        for name in joint.channels:
            value = row[col]
            axis = name[0].upper()
            if name.lower().endswith("position"):
                local_list[{"X": 0, "Y": 1, "Z": 2}[axis]] = value
            else:
                rot = _mul(rot, _rot(axis, value))
            col += 1

        local = tuple(local_list)
        world = _apply(parent_rot, local)  # type: ignore[arg-type]
        pos = tuple(parent_pos[i] + world[i] for i in range(3))
        out[joint.name] = pos  # type: ignore[assignment]
        world_rot = _mul(parent_rot, rot)
        for child in joint.children:
            visit(child, pos, world_rot)  # type: ignore[arg-type]

    # 컬럼 순서는 _column_map 과 같은 전위 순회이므로 col 을 따라가면 된다
    visit(bvh.root, (0.0, 0.0, 0.0), _IDENTITY)
    return out


def travel_joint(bvh: BvhFile) -> Optional[str]:
    """가로 이동을 실제로 들고 있는 조인트 이름. 없으면 None.

    루트가 들고 있다고 가정하면 안 된다 — kimodo/SOMA 출력은 ``Root`` 를 전 프레임
    0 으로 두고 자식 ``Hips`` 가 이동을 들고 있다.

    그렇다고 월드 좌표의 이동폭이 가장 큰 조인트를 고르면 안 된다: 달리기에서는
    손발이 몸통보다 더 크게 움직여서 팔다리 스윙을 이동으로 착각한다. 그래서
    월드 좌표가 아니라 **위치 채널의 값**만 본다.
    """
    best_name: Optional[str] = None
    best_span = 0.0
    col = 0

    def visit(joint: BvhJoint) -> None:
        nonlocal col, best_name, best_span
        columns = {
            name[0].upper(): col + i
            for i, name in enumerate(joint.channels)
            if name.lower().endswith("position")
        }
        col += len(joint.channels)
        for axis in ("X", "Z"):  # 가로 두 축만 — Y 는 도약이라 이동이 아니다
            index = columns.get(axis)
            if index is None:
                continue
            values = [row[index] for row in bvh.frames]
            span = max(values) - min(values)
            if span > best_span:
                best_span, best_name = span, joint.name
        for child in joint.children:
            visit(child)

    visit(bvh.root)
    return best_name


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
