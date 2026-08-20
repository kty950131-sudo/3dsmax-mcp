"""바이패드 → BVH 내보내기.

사양서(script-market `docs/superpowers/specs/2026-08-20-biped-polished-clips-design.md`)
대로 관절과 키 범위는 그 캐릭터에서 읽고, 피겨 모드와 레이어 상태는 원상 복구한다.
"""

import importlib
import json
import math
import sys
import types
from unittest.mock import MagicMock, call

import pytest

from maxmcp.helpers.bvh import BvhFile, parse_bvh
from maxmcp.ui.studio import biped_export
from maxmcp.ui.studio.skeleton import fk
from maxmcp.ui.studio.biped_export import (
    biped_frames,
    biped_rest_offsets,
    biped_skeleton,
    export_biped_bvh,
)


class FakePoint:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x = x
        self.y = y
        self.z = z


class FakeQuat:
    def __init__(self, x: float, y: float, z: float, w: float) -> None:
        self.x = x
        self.y = y
        self.z = z
        self.w = w


class _Inverse:
    def __init__(self, source: "FakeMatrix") -> None:
        self.source = source


class FakeMatrix:
    """회전행렬과 ``matrix3.rotation``의 부호를 따로 줄 수 있는 최소 대역."""

    def __init__(
        self,
        translation=(0.0, 0.0, 0.0),
        z_degrees: float = 0.0,
        reported_degrees: float | None = None,
    ) -> None:
        radians = math.radians(z_degrees)
        c, s = math.cos(radians), math.sin(radians)
        self.row1 = FakePoint(c, s, 0.0)
        self.row2 = FakePoint(-s, c, 0.0)
        self.row3 = FakePoint(0.0, 0.0, 1.0)
        self.translation = FakePoint(*translation)

        reported = z_degrees if reported_degrees is None else reported_degrees
        half = math.radians(reported) / 2.0
        self.rotation = FakeQuat(0.0, 0.0, math.sin(half), math.cos(half))
        self._local_by_parent: dict[int, FakeMatrix] = {}

    def local_to(self, parent: "FakeMatrix", local: "FakeMatrix") -> None:
        self._local_by_parent[id(parent)] = local

    def __mul__(self, other):
        if isinstance(other, _Inverse):
            return self._local_by_parent.get(id(other.source), self)
        return NotImplemented


class FakeNode:
    def __init__(self, name, parent=None, transform=None):
        self.name = name
        self.handle = name
        self.parent = parent
        self.children = []
        self.transform = transform or FakeMatrix()
        self.controller = MagicMock()
        if parent is not None:
            parent.children.append(self)


class FakeNodeRef:
    """같은 Max 노드를 가리키지만 Python 객체는 다른 pymxs 래퍼를 흉내낸다."""

    def __init__(self, node: FakeNode) -> None:
        self.handle = node.handle


def _rig(
    fingers=0,
    finger_links=0,
    spine_links=3,
    toes=1,
    toe_links=1,
    wrapped_parents=False,
):
    """역할·링크와 실제 부모 관계를 함께 돌려주는 가짜 바이패드."""
    rt = MagicMock()
    made: dict[tuple[str, int], FakeNode] = {}

    caps = {
        "pelvis": 1,
        "spine": spine_links,
        "neck": 1,
        "head": 1,
        "lleg": 3,
        "rleg": 3,
        "larm": 4,
        "rarm": 4,
        "lfingers": fingers * finger_links,
        "rfingers": fingers * finger_links,
        "ltoes": toes * toe_links,
        "rtoes": toes * toe_links,
    }

    def get_node(_ctrl, role, link=1):
        role = str(role)
        key = (role, link)
        if key in made:
            return made[key]
        if link > caps.get(role, 0):
            return None

        side = role[:1]
        if role == "pelvis":
            parent = None
        elif role == "spine":
            parent = get_node(
                _ctrl,
                "pelvis" if link == 1 else "spine",
                1 if link == 1 else link - 1,
            )
        elif role == "neck":
            parent = get_node(_ctrl, "spine", spine_links)
        elif role == "head":
            parent = get_node(_ctrl, "neck", 1)
        elif role.endswith("arm"):
            parent = (
                get_node(_ctrl, "spine", spine_links)
                if link == 1
                else get_node(_ctrl, role, link - 1)
            )
        elif role.endswith("leg"):
            parent = (
                get_node(_ctrl, "pelvis", 1)
                if link == 1
                else get_node(_ctrl, role, link - 1)
            )
        elif role.endswith("fingers"):
            segment = (link - 1) % finger_links if finger_links else 0
            parent = (
                get_node(_ctrl, f"{side}arm", 4)
                if segment == 0
                else get_node(_ctrl, role, link - 1)
            )
        else:
            segment = (link - 1) % toe_links if toe_links else 0
            parent = (
                get_node(_ctrl, f"{side}leg", 3)
                if segment == 0
                else get_node(_ctrl, role, link - 1)
            )

        node = FakeNode(
            f"Bip001 {role}{link}", parent, FakeMatrix((float(link), 0.0, 0.0))
        )
        if parent is not None and wrapped_parents:
            node.parent = FakeNodeRef(parent)
        made[key] = node
        return node

    rt.biped.getNode.side_effect = get_node
    rt.biped.getCurrentLayer.return_value = 2
    rt.Name.side_effect = lambda s: s
    rt.getHandleByAnim.side_effect = lambda node: node.handle
    rt.inverse.side_effect = lambda matrix: _Inverse(matrix)
    rt.sliderTime = 99
    rt.frameRate = 30
    rt.ticksPerFrame = 160
    rt._made = made
    return rt


def test_reads_the_links_the_character_actually_has() -> None:
    rt = _rig(fingers=0, finger_links=0)
    joints = biped_skeleton(rt, MagicMock())
    names = [j for j, _ in joints]

    assert "Hips" in names
    assert "LeftUpLeg" in names and "LeftLowLeg" in names and "LeftFoot" in names
    assert "LeftCollar" in names and "LeftHand" in names
    # 손가락이 없는 캐릭터에는 빈 관절을 보태지 않는다.
    assert not any("Finger" in n for n in names)


def test_finger_presence_changes_the_joint_count() -> None:
    without = biped_skeleton(_rig(fingers=0, finger_links=0), MagicMock())
    with_fingers = biped_skeleton(_rig(fingers=2, finger_links=3), MagicMock())

    # 좌우 각각 2손가락 × 3마디만 늘어난다.
    assert len(with_fingers) - len(without) == 12


def test_digit_branches_use_max_node_identity_not_python_wrapper_identity() -> None:
    joints = dict(
        biped_skeleton(
            _rig(fingers=2, finger_links=1, wrapped_parents=True), MagicMock()
        )
    )

    # 두 번째 손가락 뿌리도 손에서 갈라져야지 첫 번째 손가락의 자식이면 안 된다.
    assert joints["LeftFinger1"] == "LeftHand"
    assert joints["LeftFinger2"] == "LeftHand"


def test_toe_link_count_follows_the_rig() -> None:
    one_link = biped_skeleton(_rig(toes=1, toe_links=1), MagicMock())
    three_links = biped_skeleton(_rig(toes=1, toe_links=3), MagicMock())

    # 좌우 발가락에 두 마디씩 더 생긴다.
    assert len(three_links) - len(one_link) == 4


def test_spine_link_count_follows_the_rig() -> None:
    """첫 링크가 Chest 이므로 세 링크면 Chest3까지 있어야 한다."""
    three = [n for n, _ in biped_skeleton(_rig(spine_links=3), MagicMock())]
    four = [n for n, _ in biped_skeleton(_rig(spine_links=4), MagicMock())]
    assert "Chest3" in three and "Chest4" not in three
    assert "Chest4" in four


def test_every_joint_carries_its_parent() -> None:
    """BVH는 계층이라 부모를 모르면 트리를 세울 수 없다."""
    joints = dict(biped_skeleton(_rig(), MagicMock()))
    assert joints["Hips"] is None
    assert joints["LeftLowLeg"] == "LeftUpLeg"
    assert joints["LeftUpLeg"] == "Hips"
    assert joints["Chest"] == "Hips"
    assert joints["Head"] == "Neck"


def test_rest_offsets_leave_figure_mode_even_when_reading_fails() -> None:
    rt = _rig()
    controller = MagicMock()
    controller.figureMode = False
    joints = biped_skeleton(rt, controller)
    rt.inverse.side_effect = RuntimeError("local transform failed")

    with pytest.raises(RuntimeError, match="local transform failed"):
        biped_rest_offsets(rt, controller, joints)

    assert controller.figureMode is False


def test_rest_offsets_convert_the_measured_max_basis_to_bvh() -> None:
    """척추1 실측 로컬 이동의 길이 축이 BVH에서도 같은 길이로 남아야 한다."""
    rt = _rig()
    controller = MagicMock()
    pelvis = rt.biped.getNode(controller, "pelvis", link=1)
    spine = rt.biped.getNode(controller, "spine", link=1)
    spine.transform.local_to(
        pelvis.transform,
        FakeMatrix(translation=(7.548, 2.498, 0.142)),
    )

    offsets = biped_rest_offsets(rt, controller, [("Hips", None), ("Chest", "Hips")])

    # Max는 Z-up이고 BVH는 Y-up이다. X를 유지해 손의 좌우가 같게 하고 오른손계를
    # 보존하면 Max (x,y,z) -> BVH (x,z,-y)가 된다.
    assert offsets["Chest"] == pytest.approx((7.548, 0.142, -2.498), abs=1e-6)


@pytest.mark.parametrize(
    ("hips", "chest", "chest_local"),
    [
        ((0.12, -2.21, 97.59), (0.11, -4.92, 105.07), (-2.71, 0.01, 7.48)),
        ((0.29, -2.38, 97.68), (0.25, -5.14, 105.14), (-2.76, 0.04, 7.46)),
    ],
)
def test_exported_fk_matches_measured_world_points_in_y_up(
    hips,
    chest,
    chest_local,
) -> None:
    """정답지 F0/F60을 풀었을 때 위치·OFFSET·회전이 한 기저 안에 있어야 한다."""
    rt = _rig()
    controller = MagicMock()
    pelvis = rt.biped.getNode(controller, "pelvis", link=1)
    spine = rt.biped.getNode(controller, "spine", link=1)
    pelvis.transform = FakeMatrix(translation=hips, z_degrees=90.0)
    spine.transform = FakeMatrix(translation=chest)
    spine.transform.local_to(
        pelvis.transform,
        FakeMatrix(translation=chest_local),
    )
    joints = [("Hips", None), ("Chest", "Hips")]

    offsets = biped_rest_offsets(rt, controller, joints)
    frames = biped_frames(rt, controller, joints, 0, 0)
    bvh = BvhFile(
        root=biped_export._bvh_tree(joints, offsets),
        frame_time=1.0 / 30.0,
        frames=frames,
    )
    positions = fk(bvh, 0)

    # Max의 앞쪽 -Y를 BVH +Z로 보낸 결과다. 축이나 부호가 하나라도 바뀌면
    # 루트 위치뿐 아니라 90도 회전 아래의 Chest 위치도 함께 깨진다.
    assert positions["Hips"] == pytest.approx(
        (hips[0], hips[2], -hips[1]), abs=1e-6
    )
    assert positions["Chest"] == pytest.approx(
        (chest[0], chest[2], -chest[1]), abs=1e-6
    )


def test_frames_drop_to_base_layer_and_restore_the_original_layer() -> None:
    rt = _rig()
    controller = MagicMock()
    joints = biped_skeleton(rt, controller)

    rows = biped_frames(rt, controller, joints, 0, 1)

    assert len(rows) == 2
    assert rt.biped.setCurrentLayer.call_args_list == [call(controller, 0), call(controller, 2)]
    assert rt.sliderTime == 99


def test_frame_channel_count_is_root_six_and_other_joints_three() -> None:
    rt = _rig()
    controller = MagicMock()
    joints = biped_skeleton(rt, controller)

    row = biped_frames(rt, controller, joints, 0, 0)[0]

    assert len(row) == 6 + 3 * (len(joints) - 1)


def test_empty_frame_range_has_no_rows() -> None:
    rt = _rig()
    controller = MagicMock()

    assert biped_frames(rt, controller, biped_skeleton(rt, controller), 5, 4) == []


def test_quaternion_convention_is_measured_from_matrix_rows() -> None:
    rt = _rig()
    controller = MagicMock()
    pelvis = rt.biped.getNode(controller, "pelvis", link=1)
    # 행렬은 +90도인데 rotation 프로퍼티만 켤레(-90도)를 돌려주는 버전이다.
    pelvis.transform = FakeMatrix(z_degrees=90.0, reported_degrees=-90.0)

    row = biped_frames(rt, controller, [("Hips", None)], 0, 0)[0]

    # 켤레 판별은 Max 기저에서 끝낸 뒤, Max Z 회전을 BVH Y 회전으로 옮긴다.
    assert row[3:] == pytest.approx([0.0, 90.0, 0.0], abs=1e-6)


def test_child_rotation_uses_parent_local_axes_and_zyx_channel_order() -> None:
    rt = _rig()
    controller = MagicMock()
    pelvis = rt.biped.getNode(controller, "pelvis", link=1)
    spine = rt.biped.getNode(controller, "spine", link=1)
    pelvis.transform = FakeMatrix(z_degrees=30.0)
    spine.transform = FakeMatrix(z_degrees=50.0)
    spine.transform.local_to(pelvis.transform, FakeMatrix(z_degrees=20.0))

    row = biped_frames(rt, controller, [("Hips", None), ("Chest", "Hips")], 0, 0)[0]

    # Max Z는 BVH Y가 된다. 채널 선언은 그대로 Z,Y,X이므로 Y 회전은 가운데다.
    assert row[3:6] == pytest.approx([0.0, 30.0, 0.0], abs=1e-6)
    assert row[6:9] == pytest.approx([0.0, 20.0, 0.0], abs=1e-6)


def _export_rt() -> tuple[MagicMock, MagicMock]:
    rt = _rig()
    controller = MagicMock()
    controller.figureMode = False
    bip = MagicMock()
    bip.name = "Bip001"
    rt.getNodeByName.return_value = bip
    rt.getTMController.return_value = controller
    rt.classOf.return_value = rt.Vertical_Horizontal_Turn
    return rt, controller


def test_export_rejects_a_biped_without_animation_keys(tmp_path, monkeypatch) -> None:
    rt, _controller = _export_rt()
    rt.numKeys.return_value = 0
    out = tmp_path / "empty.bvh"
    monkeypatch.setattr(biped_export, "_rt", lambda: rt)

    msg = export_biped_bvh("Bip001", str(out))

    assert msg.startswith("ERROR:"), msg
    assert not out.exists()


def test_export_builds_a_bvh_file_with_the_declared_channels(tmp_path, monkeypatch) -> None:
    rt, controller = _export_rt()
    rt.numKeys.side_effect = lambda candidate: 1 if candidate is controller else 0
    rt.getKeyTime.return_value = 0
    out = tmp_path / "walk.bvh"
    monkeypatch.setattr(biped_export, "_rt", lambda: rt)

    msg = export_biped_bvh("Bip001", str(out))

    assert msg.startswith("OK:"), msg
    parsed = parse_bvh(out.read_text(encoding="utf-8"))
    assert parsed.root.channels == [
        "Xposition", "Yposition", "Zposition", "Zrotation", "Yrotation", "Xrotation"
    ]
    assert all(
        child.channels == ["Zrotation", "Yrotation", "Xrotation"]
        for child in parsed.root.children
    )
    assert len(parsed.frames) == 1
    assert parsed.frame_time == pytest.approx(1.0 / 30.0)


def test_bridge_export_slot_uses_the_export_result(tmp_path, monkeypatch) -> None:
    compat = types.ModuleType("maxmcp.ui.studio.compat")

    class QObject:
        def __init__(self, _parent=None) -> None:
            pass

    def slot(*_args, **_kwargs):
        return lambda fn: fn

    compat.QtCore = types.SimpleNamespace(QObject=QObject, Slot=slot)
    compat.QtWidgets = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "maxmcp.ui.studio.compat", compat)
    monkeypatch.delitem(sys.modules, "maxmcp.ui.studio.bridge", raising=False)
    StudioBridge = importlib.import_module("maxmcp.ui.studio.bridge").StudioBridge

    monkeypatch.setattr(
        biped_export,
        "export_biped_bvh",
        lambda bip, path: f"OK: {bip} -> {path}",
    )
    bridge = StudioBridge(str(tmp_path))
    payload = json.dumps({"biped": "Bip001", "path": str(tmp_path / "walk.bvh")})

    result = json.loads(bridge.export_biped_bvh(payload))

    assert result["ok"] is True
    assert result["data"]["message"].startswith("OK:")


def test_studio_page_has_an_export_button() -> None:
    """내보내기는 스튜디오 버튼으로 부른다.

    MCP 안전 모드가 `python.Execute` 를 막아서 밖에서 이 함수를 부를 길이 없고,
    애초에 최종 사용 방식도 버튼이다. 저장 위치를 묻는 대화상자를 함께 띄운다 —
    경로를 손으로 치게 하면 오타 하나에 조용히 엉뚱한 데 쓰인다.
    """
    from pathlib import Path

    html = (
        Path(__file__).resolve().parents[1]
        / "maxmcp" / "ui" / "studio" / "web" / "studio_draft.html"
    ).read_text(encoding="utf-8")
    assert 'data-action="export-bvh"' in html
    assert "export_biped_bvh" in html
    assert "choose_bvh_path" in html


def test_export_name_comes_from_the_selected_card() -> None:
    """저장 이름을 고른 카드에서 채운다.

    기본값이 바이패드 이름이면 `Bip001.bvh` 가 제안된다 — 어느 클립을 폴리싱한
    것인지 파일명에 안 남아서, 나중에 폴더를 열면 무엇인지 알 수 없다.
    UI 는 이미 고른 카드를 `state.selected` 로 알고 있으므로 그걸 쓴다.
    """
    from pathlib import Path

    html = (
        Path(__file__).resolve().parents[1]
        / "maxmcp" / "ui" / "studio" / "web" / "studio_draft.html"
    ).read_text(encoding="utf-8")
    export = html[html.index('action === "export-bvh"') : html.index('action === "mixer"')]
    assert "state.selected" in export


def test_key_times_in_frames_are_not_divided_again() -> None:
    """pymxs 가 프레임 단위로 넘긴 키 시각을 틱으로 오인하면 클립이 잘린다.

    실측: MAXScript 안에서 `getKeyTime v 180 as integer` 는 28640(틱)인데 pymxs 로
    넘어온 값은 179(프레임)다. 그걸 ticksPerFrame(160)으로 또 나누면
    179/160 → 올림 2 가 되어 **180프레임 클립이 3프레임으로 잘린다.**
    실제로 그렇게 내보내진 파일이 나왔다(artoke_idle-startle.bvh, 6KB).
    """
    rt = _rig()
    rt.numKeys.side_effect = lambda _c: 180
    rt.getKeyTime.side_effect = lambda _c, i: i - 1        # 0..179 프레임
    rt.getTMController.side_effect = lambda node: node.controller

    start, end = biped_export._key_range(rt, MagicMock(), biped_skeleton(rt, MagicMock()))
    assert (start, end) == (0, 179)


def test_key_times_in_ticks_are_converted() -> None:
    """반대로 틱으로 넘어오는 환경도 견딘다 — 키 수와 견줘 단위를 판정한다."""
    rt = _rig()
    rt.numKeys.side_effect = lambda _c: 180
    rt.getKeyTime.side_effect = lambda _c, i: (i - 1) * 160  # 0..28640 틱
    rt.getTMController.side_effect = lambda node: node.controller

    start, end = biped_export._key_range(rt, MagicMock(), biped_skeleton(rt, MagicMock()))
    assert (start, end) == (0, 179)


# ---- 회전 기저 변환 ----
# 쿼터니언 벡터부만 치환하면 **180도 근처에서 회전이 사라진다.** 그 각도에서는
# w ~= 0 이라 q 와 -q(켤레)가 같은 회전을 뜻하고, 켤레 판별이 무의미해진다.
# 실측으로 LeftUpLeg(맥스 로컬 ZYX -177.48, 2.76, -175.23)가 0,0,0 으로 뭉개져
# 다리가 통째로 뒤집혔다(발이 머리 위로 갔다). 그래서 행렬로 켤레변환한다.


def _matrix_from_zyx(z: float, y: float, x: float):
    """맥스 행벡터 규약으로 Rz*Ry*Rx 를 만든다 — 테스트용 최소 대역."""
    import math as _m

    def rot(axis, deg):
        c, s = _m.cos(_m.radians(deg)), _m.sin(_m.radians(deg))
        if axis == "x":
            return ((1, 0, 0), (0, c, s), (0, -s, c))
        if axis == "y":
            return ((c, 0, -s), (0, 1, 0), (s, 0, c))
        return ((c, s, 0), (-s, c, 0), (0, 0, 1))

    def mul(a, b):
        return tuple(
            tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
            for i in range(3)
        )

    return mul(mul(rot("z", z), rot("y", y)), rot("x", x))


class _RowMatrix:
    """row1..3 과 rotation 을 함께 내주는 가짜 matrix3."""

    def __init__(self, rows) -> None:
        self.row1 = FakePoint(*rows[0])
        self.row2 = FakePoint(*rows[1])
        self.row3 = FakePoint(*rows[2])
        # 행렬에서 사원수를 뽑는다(부호는 켤레 판별이 알아서 고른다)
        import math as _m

        m = rows
        tr = m[0][0] + m[1][1] + m[2][2]
        if tr > 0:
            s = _m.sqrt(tr + 1.0) * 2
            w, x, y, z = 0.25 * s, (m[1][2] - m[2][1]) / s, (m[2][0] - m[0][2]) / s, (m[0][1] - m[1][0]) / s
        elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
            s = _m.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2
            w, x, y, z = (m[1][2] - m[2][1]) / s, 0.25 * s, (m[1][0] + m[0][1]) / s, (m[2][0] + m[0][2]) / s
        elif m[1][1] > m[2][2]:
            s = _m.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2
            w, x, y, z = (m[2][0] - m[0][2]) / s, (m[1][0] + m[0][1]) / s, 0.25 * s, (m[2][1] + m[1][2]) / s
        else:
            s = _m.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2
            w, x, y, z = (m[0][1] - m[1][0]) / s, (m[2][0] + m[0][2]) / s, (m[2][1] + m[1][2]) / s, 0.25 * s
        # 실측: 이 맥스에서 `matrix3.rotation` 은 행렬이 나타내는 회전의 **켤레**다
        # (32표본 전부, 잔차 5e-7). 그 조건이라야 `_uses_inverse_quaternion` 이
        # 실제와 같은 경로를 타므로 버그가 재현된다.
        self.rotation = FakeQuat(-x, -y, -z, w)


def _roundtrip(z: float, y: float, x: float):
    """맥스 로컬 ZYX -> 채널. 채널을 FK 규약으로 다시 합성하면 P R Pt 와 같아야 한다."""
    rows = _matrix_from_zyx(z, y, x)
    return biped_export._rotation_channels(_RowMatrix(rows), False)


def _rebuild_error(z: float, y: float, x: float) -> float:
    """채널 -> 회전행렬 재구성 오차. 0 이면 기저 이동이 무손실이다."""
    from maxmcp.ui.studio.skeleton import _mul, _rot

    rows = _matrix_from_zyx(z, y, x)
    ch = biped_export._rotation_channels(_RowMatrix(rows), False)
    got = _mul(_mul(_rot("Z", ch[0]), _rot("Y", ch[1])), _rot("X", ch[2]))
    p = biped_export._basis()
    want = biped_export._mat_mul(
        biped_export._mat_mul(p, biped_export._transpose(rows)), biped_export._transpose(p)
    )
    return sum(abs(got[i][j] - want[i][j]) for i in range(3) for j in range(3))


def test_rotation_survives_near_180_degrees() -> None:
    """실측 LeftUpLeg. 벡터부만 치환하던 구현은 여기서 0,0,0 을 냈고 다리가 뒤집혔다."""
    channels = _roundtrip(-175.23, 2.76, -177.48)
    assert max(abs(v) for v in channels) > 90.0, channels
    assert _rebuild_error(-175.23, 2.76, -177.48) < 1e-6


def test_basis_change_is_lossless_across_the_sphere() -> None:
    """축 하나만 어긋나도 여기서 깨진다 — 실측 네 값만으로는 못 잡는다."""
    for z in (-179.0, -90.0, -1.0, 0.0, 37.0, 91.0, 178.0):
        for y in (-89.0, -30.0, 0.0, 45.0, 88.0):
            for x in (-177.0, -60.0, 0.0, 60.0, 177.0):
                assert _rebuild_error(z, y, x) < 1e-6, (z, y, x)


def test_rotation_keeps_its_magnitude() -> None:
    """실측 LeftLowArm. 값 21.32 가 다른 축에 부호 반대로 새면 안 된다."""
    channels = _roundtrip(0.0, 0.0, 21.32)
    biggest = max(channels, key=abs)
    assert abs(abs(biggest) - 21.32) < 0.01, channels


def test_identity_stays_identity() -> None:
    assert max(abs(v) for v in _roundtrip(0.0, 0.0, 0.0)) < 1e-6
