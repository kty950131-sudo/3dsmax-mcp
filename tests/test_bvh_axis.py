"""`zup_to_yup` — Z-up 골격을 Y-up 으로 돌린다.

Max Biped 를 Blender 로 내보내면 뼈가 Z 축을 따라 눕는다(척추가 위가 아니라 옆).
Character Studio 는 Y-up 을 전제하므로 그대로 실으면 자세가 틀어진다. 이 함수가
휴식 골격의 오프셋·프레임 회전·루트 위치를 한 번에 Y-up 으로 옮긴다.
"""

import math

from maxmcp.helpers.bvh import (
    BvhFile,
    BvhJoint,
    has_upright_spine,
    serialize_bvh,
    zup_to_yup,
)


def _joint(name, offset, children=None):
    return BvhJoint(
        name=name,
        offset=offset,
        channels=["Xposition", "Yposition", "Zposition", "Zrotation", "Yrotation", "Xrotation"],
        children=children or [],
    )


def _zup_tree() -> BvhFile:
    # 척추가 +Z 로 올라가고 다리가 -Z 로 내려가는 골격 (Blender 가 내보내는 모양).
    # has_upright_spine 이 이름으로 척추를 찾으므로 "Chest" 로 둔다.
    head = _joint("Head", (0.0, 0.0, 2.0))
    chest = _joint("Chest", (0.0, 0.0, 8.0), [head])
    leg = _joint("LeftUpLeg", (2.0, 0.0, -6.0))
    root = _joint("Hips", (0.0, 0.0, 0.0), [chest, leg])
    frame = [0.0] * 24  # 루트 6 + Chest 6 + Head 6 + Leg 6
    return BvhFile(root=root, frame_time=0.033, frames=[list(frame), list(frame)])


def test_spine_points_up_after_conversion() -> None:
    before = _zup_tree()
    assert has_upright_spine(serialize_bvh(before)) is False

    after = zup_to_yup(before)

    assert has_upright_spine(serialize_bvh(after)) is True


def test_offsets_move_z_up_to_y_up() -> None:
    after = zup_to_yup(_zup_tree())
    chest = after.root.children[0]
    # (x, y, z_up) -> (x, z_up, -y): 위로 8 이던 것이 Y 로 간다
    assert chest.offset[1] == 8.0
    assert abs(chest.offset[2]) < 1e-9


def test_root_translation_channels_are_rotated() -> None:
    tree = _zup_tree()
    # 루트가 Z 로 10 (위로) 이동하는 프레임
    tree.frames[1][0] = 1.0   # Xposition
    tree.frames[1][1] = 0.0   # Yposition
    tree.frames[1][2] = 10.0  # Zposition (up)

    after = zup_to_yup(tree)

    # X 는 그대로, 위(Z) 는 Y 로
    assert after.frames[1][0] == 1.0
    assert after.frames[1][1] == 10.0
    assert abs(after.frames[1][2]) < 1e-9


def test_a_pure_z_rotation_becomes_a_y_rotation() -> None:
    """Z-up 세계에서 수직축(Z) 을 도는 것은 Y-up 세계에서 수직축(Y) 을 도는 것이다."""
    tree = _zup_tree()
    # 루트를 Z 축 기준 30도 (채널 순서 Z,Y,X)
    tree.frames[1][3] = 30.0
    tree.frames[1][4] = 0.0
    tree.frames[1][5] = 0.0

    after = zup_to_yup(tree)

    z, y, x = after.frames[1][3], after.frames[1][4], after.frames[1][5]
    assert abs(y - 30.0) < 1e-3 or abs(y + 30.0) < 1e-3
    assert abs(z) < 1e-3 and abs(x) < 1e-3


def test_already_upright_is_left_alone() -> None:
    """Y-up 골격을 또 돌리면 안 된다. 두 번 부르면 원래대로 돌아온다는 뜻이 아니라,
    Y-major 면 그대로 둔다."""
    up = _joint("Chest", (0.0, 8.0, 0.0))
    root = _joint("Hips", (0.0, 0.0, 0.0), [up])
    tree = BvhFile(root=root, frame_time=0.033, frames=[[0.0] * 12])
    assert has_upright_spine(serialize_bvh(tree)) is True

    after = zup_to_yup(tree)

    assert after.root.children[0].offset == (0.0, 8.0, 0.0)
