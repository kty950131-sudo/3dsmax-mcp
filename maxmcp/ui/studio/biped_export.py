"""맥스 바이패드 → BVH.

맥스에는 BVH 익스포터가 없다(익스포터 목록에 Biovision 이 없고 Character Studio 도
`saveBipFile`·`saveFigFile` 뿐이다). 폴리싱한 클립을 꺼내려면 직접 써야 한다.

**관절 목록을 코드에 박지 않는다.** 그 캐릭터의 바이패드에서 역할×링크를 훑어
읽는다 — 실측상 `Bip001` 은 손가락 0x0, `Bip002` 는 1x1 이라 고정 목록은 한쪽이
반드시 틀린다. 설계 근거는 script-market 의
`docs/superpowers/specs/2026-08-20-biped-polished-clips-design.md`.
"""

import math
import os
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


# 손가락 다섯 개. 바이패드는 한 역할(`rFingers`)에 전부 평탄화해서 담으므로
# 링크 번호에서 어느 손가락의 몇 번째 마디인지 되짚어야 한다.
_FINGERS = ("Thumb", "Index", "Middle", "Ring", "Pinky")


def _finger_name(tag: str, link: int, per_finger: int) -> str:
    """SOMA 손가락 이름. 예전에는 `LeftFinger1`..`15` 로 평탄하게 썼다.

    그 이름은 어느 규격에도 없어서 리타게팅이 대응을 못 찾았고, 손 포즈를
    캐릭터에 얹으면 **손가락이 통째로 사라졌다**(웹 뷰어에서 실측: 포즈 34장이
    전부 같은 그림). 몸통은 이미 SOMA 계열 이름을 쓰고 있었으므로 손가락도
    맞춘다.

    **바이패드에는 손허리뼈(metacarpal)가 없다.** SOMA 는 손가락마다 네 마디를
    두는데(1=손허리, 2~4=마디) 바이패드는 세 마디뿐이라, 그 셋이 SOMA 의
    2~4 에 붙는다. 엄지는 SOMA 도 세 마디라 1~3 에 그대로 붙는다.
    (`bone-naming.ts` 의 biped↔metahuman 표와 같은 규칙이다.)
    """
    index = (link - 1) // per_finger
    segment = (link - 1) % per_finger + 1
    if index >= len(_FINGERS):
        return f"{tag}Finger{link}"      # 손가락이 다섯을 넘으면 옛 이름으로 둔다
    finger = _FINGERS[index]
    if finger == "Thumb":
        return f"{tag}Hand{finger}{segment}"
    return f"{tag}Hand{finger}{segment + 1}"


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
            if link == 1:
                # **허벅지는 루트에 매단다.** Max 는 허벅지의 부모를 `Bip Spine` 이라고
                # 하지만, 바이패드가 실제로 다리를 모는 것은 척추가 아니라 COM 이다.
                # 척추에 매달면 척추가 돌 때 다리가 따라 돌아 버려서 다리 사슬 전체가
                # 일정한 거리만큼 어긋난다(실측: 100 단위 바이패드에서 10 가까이).
                joints.append(_BipedJoint(name, leg_parent, node))
                names_by_node[_node_key(rt, node)] = name
            else:
                add(name, leg_parent, node)
            leg_parent = name

        hand = f"{tag}Hand"
        previous = hand
        if any(j.name == hand for j in joints):
            per = max(1, int(getattr(controller, "fingerLinks", 3)))
            for link, node in _walk(rt, controller, f"{side}fingers", limit=40):
                name = _finger_name(tag, link, per)
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


def _world_rotation(matrix) -> tuple[tuple[float, ...], ...]:
    """맥스 월드 행렬의 회전을 **열벡터 규약** 3×3 으로 읽는다.

    맥스는 행벡터 규약이라 행렬의 행이 기저의 상(像)이다. 우리 FK 는 열벡터로
    합성하므로 전치해서 넘긴다(`_rotation_channels` 와 같은 이유·같은 방식이다).
    """
    rows = tuple(_normalised(getattr(matrix, f"row{i}")) for i in range(1, 4))
    return _transpose(rows)


def _to_bvh_rotation(r_max):
    """R_bvh = P R_max Pᵀ. 벡터 쪽 `_max_to_bvh_xyz` 와 같은 기저 이동이다."""
    p = _basis()
    return _mat_mul(_mat_mul(p, r_max), _transpose(p))


def biped_rest_basis(
    rt,
    controller,
    joints: Sequence[tuple[str, Optional[str]]],
    frame=None,
):
    """피겨 자세를 ``(OFFSET, 휴식 월드 회전)`` 으로 읽는다.

    **OFFSET 은 부모 로컬이 아니라 월드 차이다.** 예전에는 부모 로컬 이동을 그대로
    썼는데, 바이패드 본의 로컬 축은 뼈를 따라 누워 있어서 결과 파일의 휴식 골격이
    통째로 +X 로 눕는다(실측: `Neck [29.67, 0, 0]`, `LeftUpArm [12.61, 0, 0]`).
    FK 로 풀면 좌표는 맞지만, 그 파일을 다시 바이패드로 읽으면 골격 자체가
    누운 채로 만들어져 자세가 깨진다 — 임포터가 "T포즈 골격이 아님" 이라고
    경고하던 것이 이것이다. 라이브러리의 다른 클립은 척추가 +Y 로 선다
    (`Spine1 [0, 5, 0]`).

    월드 차이로 쓰면 휴식 골격이 곧 **피겨 자세를 월드 축에서 본 모습**이 되어
    같은 형태가 된다. 대신 매 프레임 회전을 "휴식으로부터의 변화" 로 써야 한다
    (`biped_frames` 참고). 월드 좌표 결과는 두 방식이 완전히 같다.
    """
    #: **피겨 모드를 휴식 자세로 쓰지 않는다.** 피겨↔애니 전환에서 척추는 돌아가는데
    #: 다리는 따라오지 않는다(실측: 척추의 부모 상대 방향이 90도 달라지는 동안
    #: 허벅지는 그대로였다). 그 상태를 휴식으로 삼으면 다리 사슬 전체가 일정한
    #: 거리만큼 어긋난 채 구워진다(100 단위 바이패드에서 10 가까이).
    #: 그래서 **내보낼 첫 프레임**을 휴식으로 쓴다. 그 프레임에서는 모든 채널이
    #: 0 이 되고, 나머지 프레임은 그로부터의 변화로 정확히 풀린다.
    was_figure = bool(controller.figureMode)
    controller.figureMode = False
    original = rt.sliderTime
    try:
        if frame is not None:
            rt.sliderTime = frame
        selected = _selected_joints(rt, controller, joints)
        places = {j.name: _xyz(j.node.transform.translation) for j in selected}
        rest = {j.name: _world_rotation(j.node.transform) for j in selected}
        offsets: dict[str, tuple[float, float, float]] = {}
        for joint in selected:
            # 루트 이동은 매 프레임 위치 채널에 들어가므로 OFFSET에도 쓰면 두 번 더해진다.
            if joint.parent is None:
                offsets[joint.name] = (0.0, 0.0, 0.0)
                continue
            here, up = places[joint.name], places[joint.parent]
            offsets[joint.name] = _max_to_bvh_xyz(
                (here[0] - up[0], here[1] - up[1], here[2] - up[2])
            )
        return offsets, rest
    finally:
        rt.sliderTime = original
        # 저장 중 예외가 나도 Figure Mode에 남으면 이후 키 편집 자체가 달라진다.
        controller.figureMode = was_figure


def biped_rest_offsets(
    rt,
    controller,
    joints: Sequence[tuple[str, Optional[str]]],
    frame=None,
) -> dict[str, tuple[float, float, float]]:
    """휴식 자세의 **월드** 뼈 벡터를 BVH OFFSET 으로 읽는다."""
    return biped_rest_basis(rt, controller, joints, frame=frame)[0]


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
    rest=None,
) -> list[list[float]]:
    """start..end를 포함해 루트 6채널, 나머지 3채널 행으로 샘플한다.

    OFFSET 이 월드 뼈 벡터이므로(`biped_rest_basis`), 채널 회전은 **휴식 자세로부터
    얼마나 돌았는가**여야 한다. 관절 j 의 월드 변화를 ``D_j = R_j · R0_jᵀ`` 로 두면

        자식 채널 = D_부모ᵀ · D_자식      (루트 채널 = D_루트)

    이 되고, FK 로 풀면 ``P_j = P_부모 + D_부모 · (P0_j − P0_부모)`` 가 되어 월드
    좌표가 원본과 정확히 일치한다. 예전의 "부모 로컬 회전" 방식과 결과 좌표는 같고,
    달라지는 것은 휴식 골격의 생김새다 — 그쪽이 이 라이브러리의 규약이다.
    """
    if end < start:
        return []

    selected = _selected_joints(rt, controller, joints)
    # 채널은 트리 순서로 쓴다. 목록 순서로 쓰면 분기 지점부터 통째로 밀린다.
    by_name = {joint.name: joint for joint in selected}
    ordered = [by_name[name] for name in _channel_order(joints) if name in by_name]
    if rest is None:
        rest = biped_rest_basis(rt, controller, joints)[1]
    original_layer = _current_layer(rt, controller)
    original_time = rt.sliderTime
    rows: list[list[float]] = []
    try:
        # ArmSpace는 보기용 보정이므로 키 범위와 샘플 모두 베이스 합성값이어야 한다.
        rt.biped.setCurrentLayer(controller, 0)

        for frame in range(start, end + 1):
            rt.sliderTime = frame
            delta = {
                joint.name: _mat_mul(
                    _world_rotation(joint.node.transform), _transpose(rest[joint.name])
                )
                for joint in selected
            }
            row: list[float] = []
            for joint in ordered:
                if joint.parent is None:
                    row.extend(_max_to_bvh_xyz(joint.node.transform.translation))
                    local = delta[joint.name]
                else:
                    local = _mat_mul(_transpose(delta[joint.parent]), delta[joint.name])
                z, y, x = _euler_zyx(_to_bvh_rotation(local))
                row.extend([z, y, x])
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


def _channel_order(joints: Sequence[tuple[str, Optional[str]]]) -> list:
    """채널을 쓸 순서. **트리 깊이우선이지 목록 순서가 아니다.**

    BVH 는 계층 선언 순서대로 채널을 읽는다. 그런데 `_biped_joints` 가 돌려주는
    평탄한 목록은 팔 → 다리 → 손가락 → 발가락 순이라, 손가락이 손의 자식인 트리에서는
    **다리 자리에서부터 순서가 갈린다.** 그대로 쓰면 그 지점 이후의 채널이 통째로
    밀려 엉뚱한 관절에 붙는다 — 실측으로 `LeftUpLeg` 부터 어긋났고(10.36),
    다리·손가락·발가락과 오른쪽 전체가 깨졌다. 척추와 왼팔만 멀쩡해 보였던 이유가
    이것이다(거기까지는 두 순서가 우연히 같다).
    """
    children: dict = {}
    root = None
    for name, parent in joints:
        children.setdefault(parent, []).append(name)
        if parent is None:
            root = name
    order: list = []
    stack = [root]
    while stack:
        name = stack.pop()
        order.append(name)
        stack.extend(reversed(children.get(name, [])))
    return order


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


def export_biped_bvh(bip_name: str, out_path: str, node=None) -> str:
    """씬 바이패드를 BVH로 써서 maxbridge 관례의 상태 문자열을 돌려준다.

    node 를 주면 이름으로 다시 찾지 않는다. 맥스는 같은 이름을 여럿 허용해서
    ``getNodeByName`` 이 엉뚱한 바이패드를 집을 수 있다(포즈 쪽에서 실제로 겪었다).
    """
    try:
        rt = _rt()
        if node is None:
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

        offsets, rest = biped_rest_basis(rt, controller, joints, frame=frame_range[0])
        frames = biped_frames(rt, controller, joints, *frame_range, rest=rest)
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


# ---- 라이브러리에 애니 올리기 ----------------------------------------------
# 포즈 선반의 "＋ 맥스에서 저장" 과 짝이다. 포즈는 .cpy 에, 애니는 클립 폴더에
# .bvh 로 들어간다 — 라이브러리 카드가 곧 .bvh 파일이라 저장하는 순간 카드가 된다.

#: 파일 이름에 쓸 수 없는 글자. 경로 구분자를 포함해 통째로 막는다 —
#: 이름 칸에 "..\어딘가" 를 넣어 라이브러리 밖에 쓰게 두지 않는다.
_BAD_NAME = set(r'<>:"/\|?*')


def _clean_stem(name: str) -> str:
    stem = "".join(ch for ch in (name or "").strip() if ch not in _BAD_NAME).strip(" .")
    if stem.lower().endswith(".bvh"):
        stem = stem[:-4].strip()
    return stem


def save_anim_to_library(
    folder: str, bip_name: str = "", name: str = "", overwrite: bool = False
) -> dict:
    """지금 잡혀 있는 바이패드의 애니를 클립 폴더에 .bvh 로 쓴다.

    대상은 포즈 쪽과 같은 규칙이다: **뷰포트에서 고른 것이 먼저**이고, 아무것도
    안 골랐을 때만 목록의 이름을 쓴다.
    """
    from maxmcp.ui.studio.biped_pose import resolve_target

    if not folder or not os.path.isdir(folder):
        raise RuntimeError(f"클립 폴더가 없습니다: {folder or '(비어 있음)'}")
    stem = _clean_stem(name)
    if not stem:
        raise RuntimeError("파일 이름을 적어 주세요")

    rt = _rt()
    root, picked_by = resolve_target(rt, bip_name)
    out_path = os.path.join(folder, stem + ".bvh")
    if os.path.exists(out_path) and not overwrite:
        # 덮어쓰기는 화면에서 확인을 받는다. 같은 이름이 흔하고, 폴리싱한 클립을
        # 말없이 덮어쓰면 되돌릴 방법이 없다.
        return {"exists": True, "path": out_path, "name": stem}

    message = export_biped_bvh(str(root.name), out_path, node=root)
    if message.startswith("ERROR"):
        raise RuntimeError(message)
    note = f"{stem}.bvh 저장 · {message.split('(', 1)[-1].rstrip(')')}"
    if picked_by == "selection":
        note += " · 선택한 바이패드"
    return {
        "exists": False,
        "saved": True,
        "path": out_path,
        "name": stem,
        "biped": str(root.name),
        "message": note,
    }
