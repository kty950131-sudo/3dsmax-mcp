"""맥스 바이패드 → BVH.

맥스에는 BVH 익스포터가 없다(익스포터 목록에 Biovision 이 없고 Character Studio 도
`saveBipFile`·`saveFigFile` 뿐이다). 폴리싱한 클립을 꺼내려면 직접 써야 한다.

**관절 목록을 코드에 박지 않는다.** 그 캐릭터의 바이패드에서 역할×링크를 훑어
읽는다 — 실측상 `Bip001` 은 손가락 0x0, `Bip002` 는 1x1 이라 고정 목록은 한쪽이
반드시 틀린다. 설계 근거는 script-market 의
`docs/superpowers/specs/2026-08-20-biped-polished-clips-design.md`.
"""

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from maxmcp.helpers.bvh import BvhFile, BvhJoint, serialize_bvh
from maxmcp.helpers.quat import quat_to_euler

# CS 어휘에 맞춘 역할과 BVH 이름 규칙.
#
# ⚠️ **척추는 한 칸 밀린다.** 바이패드의 첫 척추 링크가 번호 없는 `Spine` 이라
# BVH 의 `Chest` 가 거기 붙는다(me볼트 `soma-본이름-매핑` 의 경고와 같다).
# 그래서 링크 1 → `Chest`, 2 → `Chest2` 로 이름이 하나씩 어긋난 채 대응한다.

#: 몸통 체인. 링크는 없어질 때까지 훑는다.
_SPINE = ("spine", lambda i: "Chest" if i == 1 else f"Chest{i}")
_NECK = ("neck", lambda i: "Neck" if i == 1 else f"Neck{i}")

_LEG_NAMES = ["UpLeg", "LowLeg", "Foot"]
_ARM_NAMES = ["Collar", "UpArm", "LowArm", "Hand"]

_ROOT_CHANNELS = [
    "Xposition", "Yposition", "Zposition",
    "Zrotation", "Yrotation", "Xrotation",
]
_JOINT_CHANNELS = ["Zrotation", "Yrotation", "Xrotation"]


@dataclass(frozen=True)
class _BipedJoint:
    name: str
    parent: Optional[str]
    node: object


def _node_key(rt, node: object) -> tuple[str, str]:
    try:
        handle = rt.getHandleByAnim(node)
        if handle is not None:
            return "handle", str(handle)
    except Exception:
        pass
    return "python", str(id(node))


def _walk(rt, controller, role: str, limit: int = 12):
    """그 역할의 링크를 없어질 때까지 돌려준다."""
    for link in range(1, limit + 1):
        try:
            node = rt.biped.getNode(controller, rt.Name(role), link=link)
        except Exception:
            node = None
        if node is None:
            return
        yield link, node


def _biped_joints(rt, controller) -> list[_BipedJoint]:
    """그 리그에 실제로 있는 링크만 부모 우선 순서로 읽는다."""
    pelvis = next(_walk(rt, controller, "pelvis", limit=1), None)
    if pelvis is None:
        return []

    joints: list[_BipedJoint] = []
    names_by_node: dict[tuple[str, str], str] = {}

    def add(name: str, fallback_parent: Optional[str], node: object) -> None:
        # 손가락·발가락은 한 역할 안에 여러 갈래가 평탄화되어 나온다. 실제 Max
        # 부모를 우선해야 한 손가락 끝이 다음 손가락 뿌리의 부모가 되지 않는다.
        actual_parent = names_by_node.get(
            _node_key(rt, getattr(node, "parent", None)), fallback_parent
        )
        joints.append(_BipedJoint(name, actual_parent, node))
        names_by_node[_node_key(rt, node)] = name

    add("Hips", None, pelvis[1])

    spine_top = "Hips"
    for link, node in _walk(rt, controller, "spine"):
        name = _SPINE[1](link)
        add(name, spine_top, node)
        spine_top = name

    neck_top = spine_top
    for link, node in _walk(rt, controller, "neck"):
        name = _NECK[1](link)
        add(name, neck_top, node)
        neck_top = name

    for _link, node in _walk(rt, controller, "head"):
        add("Head", neck_top, node)
        break

    for side, tag in (("l", "Left"), ("r", "Right")):
        arm_parent = spine_top
        for link, node in _walk(rt, controller, f"{side}arm"):
            label = _ARM_NAMES[link - 1] if link <= len(_ARM_NAMES) else f"Arm{link}"
            name = f"{tag}{label}"
            add(name, arm_parent, node)
            arm_parent = name

        leg_parent = "Hips"
        for link, node in _walk(rt, controller, f"{side}leg"):
            label = _LEG_NAMES[link - 1] if link <= len(_LEG_NAMES) else f"Leg{link}"
            name = f"{tag}{label}"
            add(name, leg_parent, node)
            leg_parent = name

        hand = f"{tag}Hand"
        previous = hand
        if any(j.name == hand for j in joints):
            for link, node in _walk(rt, controller, f"{side}fingers", limit=40):
                name = f"{tag}Finger{link}"
                add(name, previous, node)
                previous = name

        foot = f"{tag}Foot"
        previous = foot
        if any(j.name == foot for j in joints):
            for link, node in _walk(rt, controller, f"{side}toes", limit=40):
                name = f"{tag}Toe" if link == 1 else f"{tag}Toe{link}"
                add(name, previous, node)
                previous = name

    return joints


def biped_skeleton(rt, controller) -> list[tuple[str, Optional[str]]]:
    """(BVH 관절 이름, 부모 이름) 목록. 루트의 부모는 None.

    순서는 계층 순회 순서다 — 부모가 항상 자식보다 먼저 나온다. 트리를 세우는
    쪽이 그걸 기대한다.
    """
    return [(joint.name, joint.parent) for joint in _biped_joints(rt, controller)]


def _selected_joints(
    rt,
    controller,
    joints: Sequence[tuple[str, Optional[str]]],
) -> list[_BipedJoint]:
    available = {joint.name: joint for joint in _biped_joints(rt, controller)}
    selected: list[_BipedJoint] = []
    for name, parent in joints:
        if name not in available:
            raise ValueError(f"바이패드 링크를 찾을 수 없습니다: {name}")
        selected.append(_BipedJoint(name, parent, available[name].node))
    return selected


def _local_transform(rt, joint: _BipedJoint, nodes: dict[str, object]):
    if joint.parent is None:
        return joint.node.transform
    # 세계 회전을 복사하면 부모 회전이 두 번 들어간다. 여기서는 바이패드 본의
    # Max 로컬 기저를 읽고, 내보내기 직전에 BVH 기저로 바꾼다.
    return joint.node.transform * rt.inverse(nodes[joint.parent].transform)


def _xyz(value) -> tuple[float, float, float]:
    try:
        return float(value.x), float(value.y), float(value.z)
    except AttributeError:
        return float(value[0]), float(value[1]), float(value[2])


def _max_to_bvh_xyz(value) -> tuple[float, float, float]:
    """Max Z-up 벡터를 오른손 BVH Y-up 기저로 옮긴다."""
    x, y, z = _xyz(value)
    # 실측 정답지에서 Max 높이 Z가 BVH Y로 가고, LeftHand의 좌우 X 부호는
    # 원본 BVH와 같다. 남은 축을 -Y로 두어야 오른손계를 보존한다. 즉 Max의
    # -Y 앞쪽이 BVH +Z가 되는 X축 +90도 기저 회전이다.
    return x, z, -y


def biped_rest_offsets(
    rt,
    controller,
    joints: Sequence[tuple[str, Optional[str]]],
) -> dict[str, tuple[float, float, float]]:
    """피겨 모드의 부모 로컬 위치를 BVH OFFSET으로 읽는다."""
    controller.figureMode = True
    try:
        selected = _selected_joints(rt, controller, joints)
        nodes = {joint.name: joint.node for joint in selected}
        offsets: dict[str, tuple[float, float, float]] = {}
        for joint in selected:
            # 루트 이동은 매 프레임 위치 채널에 들어가므로 OFFSET에도 쓰면 두 번 더해진다.
            offsets[joint.name] = (
                (0.0, 0.0, 0.0)
                if joint.parent is None
                else _max_to_bvh_xyz(_local_transform(rt, joint, nodes).translation)
            )
        return offsets
    finally:
        # 저장 중 예외가 나도 Figure Mode에 남으면 이후 키 편집 자체가 달라진다.
        controller.figureMode = False


def _quat4(value) -> tuple[float, float, float, float]:
    q = (float(value.x), float(value.y), float(value.z), float(value.w))
    length = math.sqrt(sum(component * component for component in q))
    if length == 0.0:
        return 0.0, 0.0, 0.0, 1.0
    return tuple(component / length for component in q)  # type: ignore[return-value]


def _matrix_quat(matrix) -> tuple[float, float, float, float]:
    try:
        return _quat4(matrix.rotation)
    except (AttributeError, RuntimeError):
        return _quat4(matrix.rotationpart)


def _quat_rows(q: tuple[float, float, float, float]) -> tuple[tuple[float, ...], ...]:
    """Max의 행벡터 matrix3와 비교할 세 정규화 행을 만든다."""
    x, y, z, w = q
    return (
        (1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y + z * w), 2.0 * (x * z - y * w)),
        (2.0 * (x * y - z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z + x * w)),
        (2.0 * (x * z + y * w), 2.0 * (y * z - x * w), 1.0 - 2.0 * (x * x + y * y)),
    )


def _normalised(value) -> tuple[float, float, float]:
    xyz = _xyz(value)
    length = math.sqrt(sum(component * component for component in xyz))
    if length == 0.0:
        return xyz
    return tuple(component / length for component in xyz)  # type: ignore[return-value]


def _rotation_error(matrix, q: tuple[float, float, float, float]) -> float:
    actual = tuple(_normalised(getattr(matrix, f"row{i}")) for i in range(1, 4))
    expected = _quat_rows(q)
    return sum(
        math.sqrt(sum((a - b) ** 2 for a, b in zip(actual_row, expected_row)))
        for actual_row, expected_row in zip(actual, expected)
    )


def _uses_inverse_quaternion(matrix) -> bool:
    """matrix3.rotation과 그 켤레 중 실제 행렬을 재현하는 쪽을 고른다."""
    direct = _matrix_quat(matrix)
    inverse = (-direct[0], -direct[1], -direct[2], direct[3])
    return _rotation_error(matrix, inverse) < _rotation_error(matrix, direct)


def _basis() -> tuple[tuple[float, ...], ...]:
    """맥스 기저를 BVH 기저로 옮기는 행렬 P (열벡터 규약).

    `_max_to_bvh_xyz` 와 같은 대응을 행렬로 쓴 것이다 — 정의를 두 벌 두면 한쪽만
    고쳐져 조용히 갈라진다.
    """
    cols = [_max_to_bvh_xyz(v) for v in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))]
    return tuple(tuple(cols[c][r] for c in range(3)) for r in range(3))


def _mat_mul(a, b):
    return tuple(
        tuple(sum(a[r][k] * b[k][c] for k in range(3)) for c in range(3)) for r in range(3)
    )


def _transpose(m):
    return tuple(tuple(m[c][r] for c in range(3)) for r in range(3))


def _euler_zyx(m) -> tuple[float, float, float]:
    """열벡터 회전행렬에서 Rz*Ry*Rx 로 분해한 (z, y, x) 각도(도)."""
    # 짐벌락(|m[0][2]| ~ 1)에서는 z 를 0 으로 두고 나머지를 x 에 몰아 준다 —
    # 그 자세에서 z 와 x 는 같은 축을 돌리므로 어느 쪽에 실어도 같은 회전이다.
    sy = -m[2][0]
    sy = max(-1.0, min(1.0, sy))
    if abs(sy) > 0.999999:
        y = math.degrees(math.asin(sy))
        z = 0.0
        x = math.degrees(math.atan2(-m[1][2], m[1][1]))
    else:
        y = math.degrees(math.asin(sy))
        z = math.degrees(math.atan2(m[1][0], m[0][0]))
        x = math.degrees(math.atan2(m[2][1], m[2][2]))
    return z, y, x


def _rotation_channels(matrix, use_inverse: bool) -> list[float]:
    """맥스 로컬 회전을 BVH 기저의 ZYX 채널로 옮긴다.

    **사원수를 거치지 않는다.** 전에는 `matrix3.rotation` 의 벡터부만 축 치환
    했는데, 그러면 **180도 근처에서 회전이 사라졌다**: 그 각도는 w ~= 0 이라
    q 와 -q 가 같은 회전이라서 켤레 판별이 무의미해지고, 벡터부 조작이 회전을
    지운다. 실측으로 LeftUpLeg(로컬 회전이 ±180도 언저리)가 0,0,0 으로 뭉개져
    다리가 통째로 뒤집혔다 — FK 로 풀면 발이 머리 위(Y 180)로 갔다.

    행렬 행(`row1..3`)이 곧 회전행렬이므로 그걸 그대로 쓰면 켤레 모호성이
    애초에 생기지 않는다. 기저 이동은 정석대로 켤레변환한다: R_bvh = P R_max Pᵀ.

    `use_inverse` 는 더 이상 쓰지 않는다 — 행렬을 직접 읽으므로 판별할 것이 없다.
    호출부 호환을 위해 인자는 남긴다.
    """
    rows = tuple(_normalised(getattr(matrix, f"row{i}")) for i in range(1, 4))
    # 맥스는 행벡터 규약이라 행렬의 행이 기저의 상(像)이다. 열벡터 규약으로
    # 옮기려면 전치한다 — 우리 FK(`skeleton.py`)가 열벡터로 합성한다.
    r_max = _transpose(rows)
    p = _basis()
    r_bvh = _mat_mul(_mat_mul(p, r_max), _transpose(p))
    z, y, x = _euler_zyx(r_bvh)
    return [z, y, x]


def _current_layer(rt, controller) -> Optional[int]:
    try:
        return int(rt.biped.getCurrentLayer(controller))
    except Exception:
        return None


def biped_frames(
    rt,
    controller,
    joints: Sequence[tuple[str, Optional[str]]],
    start: int,
    end: int,
) -> list[list[float]]:
    """start..end를 포함해 루트 6채널, 나머지 3채널 행으로 샘플한다."""
    if end < start:
        return []

    selected = _selected_joints(rt, controller, joints)
    nodes = {joint.name: joint.node for joint in selected}
    original_layer = _current_layer(rt, controller)
    original_time = rt.sliderTime
    rows: list[list[float]] = []
    try:
        # ArmSpace는 보기용 보정이므로 키 범위와 샘플 모두 베이스 합성값이어야 한다.
        rt.biped.setCurrentLayer(controller, 0)
        rt.sliderTime = start
        first_local = _local_transform(rt, selected[0], nodes)
        use_inverse = _uses_inverse_quaternion(first_local)

        for frame in range(start, end + 1):
            rt.sliderTime = frame
            row: list[float] = []
            for joint in selected:
                local = _local_transform(rt, joint, nodes)
                if joint.parent is None:
                    row.extend(_max_to_bvh_xyz(local.translation))
                row.extend(_rotation_channels(local, use_inverse))
            rows.append(row)
        return rows
    finally:
        rt.sliderTime = original_time
        if original_layer is not None:
            rt.biped.setCurrentLayer(controller, original_layer)


def _key_range(
    rt,
    controller,
    joints: Sequence[tuple[str, Optional[str]]],
) -> Optional[tuple[int, int]]:
    selected = _selected_joints(rt, controller, joints)
    controllers = [controller]
    for name in ("vertical", "horizontal", "turning"):
        try:
            controllers.append(getattr(controller, name).controller)
        except Exception:
            pass
    for joint in selected:
        try:
            controllers.append(rt.getTMController(joint.node))
        except Exception:
            controllers.append(joint.node.controller)

    unique = []
    seen: set[int] = set()
    for item in controllers:
        if id(item) not in seen:
            seen.add(id(item))
            unique.append(item)

    original_layer = _current_layer(rt, controller)
    times: list[float] = []
    most_keys = 0
    try:
        rt.biped.setCurrentLayer(controller, 0)
        for item in unique:
            try:
                count = int(rt.numKeys(item))
            except Exception:
                continue
            count = max(0, count)
            most_keys = max(most_keys, count)
            for index in range(1, count + 1):
                times.append(float(rt.getKeyTime(item, index)))
    finally:
        if original_layer is not None:
            rt.biped.setCurrentLayer(controller, original_layer)

    if not times:
        return None

    # 키 시각의 단위를 **키 개수로 판정한다.**
    #
    # pymxs 가 time 값을 어떤 단위로 넘기는지가 경로마다 다르다 — 실측으로,
    # MAXScript 안에서 `getKeyTime v 180 as integer` 는 28640(틱)인데 파이썬으로
    # 넘어온 같은 값은 179(프레임)였다. 틱이라 가정하고 ticksPerFrame(160)으로
    # 나눴더니 179/160 → 올림 2 가 되어 **180프레임 클립이 3프레임으로 잘려**
    # 나갔다(artoke_idle-startle.bvh, 6KB).
    #
    # 한쪽으로 가정하지 않고 잰다: 가장 키가 많은 컨트롤러의 키 수와 견줘,
    # 폭이 키 수보다 훨씬 크면 틱이고 비슷하면 이미 프레임이다. 베이크된 모캡은
    # 프레임마다 키가 찍히므로 이 비교가 성립한다.
    span = max(times) - min(times)
    per_frame = float(rt.ticksPerFrame)
    looks_like_ticks = most_keys > 1 and span > (most_keys - 1) * (per_frame / 2.0)
    scale = per_frame if looks_like_ticks else 1.0
    return math.floor(min(times) / scale), math.ceil(max(times) / scale)


def _bvh_tree(
    joints: Sequence[tuple[str, Optional[str]]],
    offsets: dict[str, tuple[float, float, float]],
) -> BvhJoint:
    built = {
        name: BvhJoint(
            name=name,
            offset=offsets[name],
            channels=list(_ROOT_CHANNELS if parent is None else _JOINT_CHANNELS),
        )
        for name, parent in joints
    }
    roots = []
    for name, parent in joints:
        if parent is None:
            roots.append(built[name])
        else:
            built[parent].children.append(built[name])
    if len(roots) != 1:
        raise ValueError(f"BVH 루트는 하나여야 합니다: {len(roots)}")
    return roots[0]


def _rt():
    from pymxs import runtime as rt  # type: ignore

    return rt


def export_biped_bvh(bip_name: str, out_path: str) -> str:
    """씬 바이패드를 BVH로 써서 maxbridge 관례의 상태 문자열을 돌려준다."""
    try:
        rt = _rt()
        node = rt.getNodeByName(bip_name)
        if node is None:
            return f"ERROR: 바이패드를 찾을 수 없습니다: {bip_name}"
        controller = rt.getTMController(node)
        if rt.classOf(controller) != rt.Vertical_Horizontal_Turn:
            return f"ERROR: 바이패드 루트가 아닙니다: {bip_name}"

        joints = biped_skeleton(rt, controller)
        if not joints:
            return f"ERROR: 바이패드 관절을 찾을 수 없습니다: {bip_name}"
        frame_range = _key_range(rt, controller, joints)
        if frame_range is None:
            return f"ERROR: 바이패드 애니메이션 키가 없습니다: {bip_name}"

        offsets = biped_rest_offsets(rt, controller, joints)
        frames = biped_frames(rt, controller, joints, *frame_range)
        if not frames:
            return f"ERROR: 내보낼 프레임이 없습니다: {bip_name}"

        bvh = BvhFile(
            root=_bvh_tree(joints, offsets),
            frame_time=1.0 / float(rt.frameRate),
            frames=frames,
        )
        with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialize_bvh(bvh))
        return f"OK: {out_path} ({len(frames)} frames, {len(joints)} joints)"
    except Exception as exc:
        return f"ERROR: BVH export failed: {exc}"
