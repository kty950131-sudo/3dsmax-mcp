"""Max 안에서만 동작하는 pymxs 호출 모음.

bvh_biped_ui.ms 의 검증된 절차를 파이썬으로 옮긴 것이다. 원본 .ms 는 그대로
남아 있고 이 모듈과 독립적으로 동작한다. 모든 공개 함수는 원본 .ms 관례대로
"OK: ..." 또는 "ERROR: ..." 문자열을 돌려준다.
"""

import os
import tempfile
from typing import Optional, Sequence

from maxmcp.helpers.bvh import DEFAULT_BIPED_PRUNE, has_upright_spine, prepare_for_biped


def _rt():
    from pymxs import runtime

    return runtime


def _tm_controller(rt, node):
    """노드의 트랜스폼 컨트롤러.

    MAXScript 의 ``node.transform.controller`` 를 pymxs 로 직역하면 안 된다 —
    pymxs 의 ``node.transform`` 은 Matrix3 **값**을 돌려주므로 그 위의
    ``.controller`` 는 AttributeError 다 (실측: 임포트가 바이패드만 만들고
    모션 없이 죽었다). ``getTMController`` 가 pymxs 의 올바른 경로다.
    """
    return rt.getTMController(node)


ROOT_POINT_NAME = "Root"


def ensure_root_point(rt, name: str = ROOT_POINT_NAME):
    """원점에 루트 포인트 헬퍼를 만든다. 이미 있으면 그대로 둔다.

    본이 아니라 Point 헬퍼다 — 본은 스킨·익스포트 대상에 섞여 들어가는 지오메트리
    라서, 월드 원점 표시라는 목적에 맞지 않는다. **키도 찍지 않는다**: 클립이
    임포트 단계에서 원점 시작으로 재중심되므로(``recenter_ground``) 정지한 원점
    포인트가 곧 캐릭터의 시작 자리다.

    바이패드에 링크하지 않는다. COM 은 바이패드 컨트롤러가 쥐고 있어 부모를
    붙였을 때 믹서·리타게팅에 무슨 영향이 가는지 확인된 바가 없다.
    """
    existing = rt.getNodeByName(name)
    if existing is not None:
        return existing
    point = rt.Point()
    # 이름부터 붙인다. 아래 치장에서 실패해도 이름 없는 헬퍼가 남으면 다음
    # 임포트가 그것을 못 찾고 하나 더 만든다.
    point.name = name
    try:
        point.size = 20.0
        point.Box = True
        point.wirecolor = rt.color(255, 200, 0)
    except Exception:
        pass
    return point


def scene_bipeds() -> list[str]:
    """씬의 바이패드 루트 노드 이름 목록.

    pymxs 는 일부 씬 객체(바이패드 하위 노드 등)에서 ``.controller`` 접근이
    AttributeError 를 던진다 (실측: Bip_run2 임포트 직후 rt.objects 순회 중).
    객체 하나 때문에 목록 전체가 죽으면 안 되므로 개별로 감싼다.
    """
    rt = _rt()
    names: list[str] = []
    for obj in rt.objects:
        try:
            if rt.classOf(_tm_controller(rt, obj)) == rt.Vertical_Horizontal_Turn:
                names.append(obj.name)
        except Exception:
            continue
    return names


#: 새 바이패드를 만들 때 쓰는 키. `biped.createNew` 에 주는 값과 같아야 한다 —
#: 파일이 이 키로 맞춰져 들어오므로 둘이 어긋나면 만든 크기가 곧바로 덮인다.
DEFAULT_BIPED_HEIGHT = 170.0


def convert_clip(
    src_path: str,
    x_offset: float = 0.0,
    speed: float = 1.0,
    trim: tuple[float, float] = (0.0, 1.0),
    time_map: Optional[Sequence[float]] = None,
    target_height: Optional[float] = None,
) -> tuple[str, bool]:
    """*_biped.bvh 를 만들고 (경로, 직립여부) 를 돌려준다.

    ``target_height`` 를 주면 골격을 그 키로 맞춘 뒤 내보낸다. 필요한 이유는
    ``biped.loadMocapFile`` 이 모션만 얹는 게 아니라 **바이패드를 파일 치수로
    다시 만들기** 때문이다 — 게임에서 뽑은 클립은 미터라(엘렌 1.2, 레미엘
    1.1~2.3, 컷신 30.1) 그대로 넣으면 1.2 짜리 바이패드가 선다.
    """
    text = open(src_path, encoding="utf-8", errors="replace").read()
    converted = prepare_for_biped(
        text,
        prune=DEFAULT_BIPED_PRUNE,
        offset=(x_offset, 0.0, 0.0),
        speed=speed,
        trim_range=trim,
        time_map=time_map,
        target_height=target_height,
    )
    stem, _ = os.path.splitext(src_path)
    out_path = f"{stem}_biped.bvh"
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(converted)
    return out_path, has_upright_spine(text)


def biped_height(rt, controller) -> Optional[float]:
    """바이패드가 지금 서 있는 키(씬 단위). 못 재면 None.

    ``loadMocapFile`` 앞에서 파일을 이 키에 맞추기 위해 잰다. 바이패드 컨트롤러에
    키를 직접 묻는 속성이 없어서, 발 아래에서 머리 위까지 노드 바운딩박스로
    잰다 — 머리 노드는 정수리가 아니라 목 위 상자라 머리 상자의 위쪽을 쓴다.
    """
    try:
        head = rt.biped.getNode(controller, rt.Name("head"))
        foot = rt.biped.getNode(controller, rt.Name("lleg"), link=3)
        if head is None or foot is None:
            return None
        top = float(head.max.z)
        bottom = float(foot.min.z)
        height = top - bottom
        return height if height > 0 else None
    except Exception:
        return None


def _delete_biped(rt, root) -> None:
    """바이패드를 통째로 지운다.

    ``delete <root>`` 는 **루트 노드 하나만** 지운다. 바이패드는 골반·척추·팔다리로
    수십 개 노드가 달린 계층이라, 루트만 지우면 나머지가 부모 없이 씬에 남는다.
    임포트를 반복하면 그 뼈 뭉치가 겹겹이 쌓여 관절이 꼬인 것처럼 보인다.
    """
    nodes = []

    def collect(node) -> None:
        nodes.append(node)
        try:
            for child in node.children:
                collect(child)
        except Exception:
            pass

    collect(root)
    # 자식부터 지운다 — 부모를 먼저 지우면 남은 참조가 끊긴 노드를 가리킨다.
    for node in reversed(nodes):
        try:
            rt.delete(node)
        except Exception:
            pass


def import_clip(
    src_path: str,
    bip_name: str,
    convert: bool,
    x_offset: float,
    speed: float = 1.0,
    trim: tuple[float, float] = (0.0, 1.0),
    time_map: Optional[Sequence[float]] = None,
    mirror: bool = False,
    arm_points: Optional[Sequence[tuple[int, float]]] = None,
) -> str:
    """바이패드를 만들고 클립을 올린다."""
    if not os.path.isfile(src_path):
        return f"ERROR: file not found: {src_path}"

    load_path = src_path
    upright = True
    if convert:
        try:
            # 새 바이패드는 우리가 키를 정한다 — 파일이 미터든 센티미터든 늘
            # 같은 크기로 선다.
            load_path, upright = convert_clip(
                src_path, x_offset, speed, trim, time_map,
                target_height=DEFAULT_BIPED_HEIGHT,
            )
        except Exception as exc:
            return f"ERROR: convert failed: {exc}"

    rt = _rt()
    bip = rt.biped.createNew(DEFAULT_BIPED_HEIGHT, -90, rt.Point3(0, 0, 0))
    if bip is None:
        return "ERROR: biped.createNew failed"
    if bip_name:
        bip.name = bip_name

    controller = _tm_controller(rt, bip)
    old_quiet = rt.setQuietMode(True)
    ok = False
    try:
        ok = rt.biped.loadMocapFile(controller, load_path)
    except Exception:
        ok = False
    finally:
        rt.setQuietMode(old_quiet)

    if not ok:
        # 실패한 바이패드를 씬에 남기지 않는다 (원본 .ms 와 같은 규칙).
        # 루트만 지우면 뼈 수십 개가 남으므로 계층째 지운다.
        _delete_biped(rt, bip)
        return f"ERROR: loadMocapFile rejected {load_path}"

    if mirror:
        try:
            rt.biped.mirror(controller)
        except Exception:
            pass

    # 속도 곡선이 파일에 구워져 함께 들어오듯, 팔 간격도 임포트 한 번으로 걸린다.
    if arm_points:
        _arm_space(rt, controller, arm_points, bip.name)

    # 월드 원점 표시. 클립이 원점 시작으로 재중심되므로 이 자리가 시작점이다.
    # 실패해도 임포트 자체는 성공이므로 조용히 넘긴다.
    try:
        ensure_root_point(rt)
    except Exception:
        pass

    msg = f"OK: {bip.name}"
    if not upright:
        msg += " | 경고: T포즈 골격이 아님(_tpose 파일 권장) — 자세가 틀어질 수 있음"
    return msg


def retarget_clip(
    src_path: str,
    bip_name: str,
    convert: bool,
    speed: float = 1.0,
    trim: tuple[float, float] = (0.0, 1.0),
    time_map: Optional[Sequence[float]] = None,
    mirror: bool = False,
    arm_points: Optional[Sequence[tuple[int, float]]] = None,
) -> str:
    """기존 바이패드에 클립을 올린다 — 새로 만들지 않는다.

    convert 가 켜져 있으면 대상 바이패드의 현재 X 위치를 변환 오프셋으로 구워
    애니메이션을 바꿔도 씬 배치가 유지된다 (BVH 루트 좌표로 순간이동하지 않게).
    """
    if not os.path.isfile(src_path):
        return f"ERROR: file not found: {src_path}"

    rt = _rt()
    bip = rt.getNodeByName(bip_name)
    if bip is None:
        return f"ERROR: 바이패드를 찾을 수 없음: {bip_name}"
    controller = _tm_controller(rt, bip)
    if rt.classOf(controller) != rt.Vertical_Horizontal_Turn:
        return f"ERROR: not a biped root: {bip_name}"
    if controller.figureMode:
        return f"ERROR: 피겨 모드를 먼저 해제하세요: {bip_name}"
    if controller.mixerMode:
        controller.mixerMode = False

    # 규칙: 기존 캐릭터에 얹을 때 뼈대는 그 캐릭터 것을 쓴다.
    #
    # `loadMocapFile` 은 모션만 얹지 않고 **바이패드를 파일 치수로 다시 만든다**.
    # 그래서 파일을 그대로 넣으면 공들여 맞춰 둔 캐릭터의 키가 클립 키로 덮인다
    # (게임 추출 클립은 미터라 1.2 짜리가 된다). 반대로 파일을 그 바이패드의 키에
    # 맞춰 두면, 덮어써도 같은 값이라 골격이 그대로 남는다.
    target_height = biped_height(rt, controller)

    load_path = src_path
    upright = True
    if convert:
        try:
            x_offset = float(bip.transform.position.x)
            load_path, upright = convert_clip(
                src_path, x_offset, speed, trim, time_map,
                target_height=target_height,
            )
        except Exception as exc:
            return f"ERROR: convert failed: {exc}"

    # 스튜디오가 만든 ArmSpace 레이어는 새 클립 위에 겹쳐 팔을 또 벌린다 — 지우고 시작.
    # 사용자가 만든 레이어는 지우지 않는다. 남의 작업이라 조용히 없앨 수 없고,
    # 대신 이름을 모아 두었다가 결과 메시지로 알린다.
    leftover: list[str] = []
    for i in range(int(rt.biped.numLayers(controller)), 0, -1):
        name = str(rt.biped.getLayerName(controller, i))
        if name == "ArmSpace":
            rt.biped.deleteLayer(controller, i)
        else:
            leftover.append(name)

    # 로드 전에 베이스 층(0)으로 내려온다.
    #
    # 바이패드는 **현재 레이어까지만** 합성해 보여 준다. 스튜디오가 팔 간격을
    # 걸면 `_arm_space` 가 현재 레이어를 ArmSpace 에 남기고 끝나므로(그래야
    # 오프셋이 보인다), 그 바이패드에 다시 로드하면 현재 레이어가 위쪽에 걸린
    # 채다. 그러면 방금 실은 클립이 근거를 잃은 옛 오프셋에 덮여 어긋나 보인다
    # — 새 바이패드에서는 레이어가 없어 안 나타나고 기존 바이패드에서만 나던
    # 증상이 이것이다.
    try:
        rt.biped.setCurrentLayer(controller, 0)
    except Exception:
        pass

    # 기존 캐릭터에는 BVH 를 직접 넣지 않는다 — 골격이 다시 만들어져 스킨과
    # 링크가 끊긴다. 임시 바이패드를 거쳐 모션만 옮긴다.
    # 기존 캐릭터의 골격 세팅(피겨)을 로드 전에 .fig 로 붙잡아 둔다.
    #
    # `loadMocapFile` 은 바이패드 구조를 파일에 맞춰 다시 만들므로, 공들여 맞춘
    # 링크 수·스케일이 덮인다. 임시 바이패드를 거쳐 .bip 만 옮기는 방식도 써
    # 봤지만 **구조가 다른 바이패드 사이의 .bip 로드는 링크를 재배분하며 본을
    # 비틀었다**(실사용 보고). CS 의 정석은 이것이다: 모션을 로드한 뒤 저장해 둔
    # .fig 를 다시 입혀 골격 세팅을 캐릭터 것으로 되돌린다.
    fig_path = os.path.join(tempfile.gettempdir(), "bvh_studio_figure.fig")
    fig_saved = False
    was_figure_mode = bool(controller.figureMode)
    try:
        # Autodesk MAXScript는 SaveFigFile도 Figure Mode에서만 유효하다.
        controller.figureMode = True
        fig_saved = bool(rt.biped.saveFigFile(controller, fig_path))
    except Exception:
        fig_saved = False
    finally:
        controller.figureMode = was_figure_mode

    old_quiet = rt.setQuietMode(True)
    ok = False
    fig_restored = False
    try:
        ok = bool(rt.biped.loadMocapFile(controller, load_path))
        if ok and fig_saved:
            # 피겨 복원은 피겨 모드에서 한다 — 켜고, 입히고, 끈다.
            try:
                controller.figureMode = True
                fig_restored = bool(rt.biped.loadFigFile(controller, fig_path))
            finally:
                controller.figureMode = was_figure_mode
    except Exception:
        ok = False
    finally:
        rt.setQuietMode(old_quiet)
        try:
            os.unlink(fig_path)
        except OSError:
            pass

    if not ok:
        # 기존 바이패드는 사용자 소유다 — 실패해도 지우지 않는다
        return f"ERROR: loadMocapFile rejected {load_path}"

    if mirror:
        try:
            rt.biped.mirror(controller)
        except Exception:
            pass

    # 위에서 옛 ArmSpace 를 이미 지웠으므로 여기서 새 곡선만 얹으면 된다.
    if arm_points:
        _arm_space(rt, controller, arm_points, bip.name)

    msg = f"OK: {bip.name} 애니메이션 교체"
    if not fig_saved:
        msg += (
            " | 경고: 피겨(.fig) 저장이 실패해 골격 세팅을 되돌리지 못했습니다"
            " — 스키닝이 덮였을 수 있습니다"
        )
    elif not fig_restored:
        msg += (
            " | 경고: 피겨(.fig) 복원이 실패했습니다"
            " — 피겨 모드에서 수동으로 .fig 를 다시 입혀 주세요"
        )
    if not upright:
        msg += " | 경고: T포즈 골격이 아님(_tpose 파일 권장) — 자세가 틀어질 수 있음"
    if leftover:
        # 지우지 않고 알린다. 이 레이어들의 값은 갈아치운 옛 애니메이션에 대한
        # 오프셋이라 새 클립 위에서는 근거가 없다 — 켜 보면 어긋난다.
        msg += (
            f" | 알림: 레이어 {len(leftover)}개가 남아 있음({', '.join(leftover)})"
            " — 옛 애니메이션 기준 오프셋이라 켜면 어긋납니다"
        )
    return msg


class _animate:
    """``animate on`` 블록의 파이썬 대응."""

    def __init__(self, rt) -> None:
        self._rt = rt

    def __enter__(self) -> None:
        self._rt.animate = True

    def __exit__(self, *exc: object) -> None:
        self._rt.animate = False


def _arm_space(rt, controller, points: Sequence[tuple[int, float]], label: str) -> str:
    """ArmSpace 레이어를 다시 만든다. 컨트롤러를 이미 들고 있는 쪽이 부른다.

    이름으로 다시 찾지 않는 이유: 임포트 직후에는 방금 만든 바이패드를 손에
    쥐고 있는데, 이름으로 되찾으면 같은 이름이 둘일 때 엉뚱한 쪽에 레이어가
    얹힌다.
    """
    for i in range(int(rt.biped.numLayers(controller)), 0, -1):
        if rt.biped.getLayerName(controller, i) == "ArmSpace":
            rt.biped.deleteLayer(controller, i)

    if not points or all(deg == 0 for _, deg in points):
        return f"OK: {label} 팔 간격 없음"

    layer_index = int(rt.biped.numLayers(controller)) + 1
    rt.biped.createLayer(controller, layer_index, "ArmSpace")
    rt.biped.setCurrentLayer(controller, layer_index)

    left = rt.biped.getNode(controller, rt.Name("larm"), link=2)
    right = rt.biped.getNode(controller, rt.Name("rarm"), link=2)

    for frame, degrees in points:
        rt.sliderTime = frame
        with _animate(rt):
            rt.rotate(left, rt.angleaxis(degrees, rt.Point3(0, 0, 1)))
            rt.rotate(right, rt.angleaxis(-degrees, rt.Point3(0, 0, 1)))
            rt.biped.addNewKey(left.controller, frame)
            rt.biped.addNewKey(right.controller, frame)

    # **베이스 레이어로 내려와 끝낸다.**
    #
    # 전에는 ArmSpace 를 현재 레이어로 남겼다 — 바이패드가 현재 레이어까지만
    # 합성해 보여 주므로 그래야 오프셋이 눈에 보이기 때문이다. 그런데 그 상태로
    # 두면 **In Place Mode 가 죽는다**: 맥스 2026 실측으로 레이어 1 에서는
    # `inPlaceMode = true` 가 에러 없이 조용히 무시되고(값이 false 로 남는다)
    # 레이어 0 에서만 켜진다. 사용자가 "산발적으로 버튼이 안 눌린다"고 한 것이
    # 이것이고, 산발적인 이유는 팔 간격을 0 이 아니게 준 임포트에서만 레이어가
    # 생기기 때문이다.
    #
    # 둘을 동시에 만족시킬 수는 없다. 맥스에 레이어를 베이스로 접는 API 가 없다
    # (`collapseAllLayers`·`collapseLayer`·`flattenLayers` 모두 이 버전에 없음).
    # 그래서 상시 쓰는 쪽인 In Place 를 살리고, 팔 간격은 확인할 때만
    # `set_arm_space_visible` 로 잠깐 올려 본다. 레이어와 키는 그대로 남으므로
    # 데이터는 하나도 잃지 않는다.
    rt.biped.setCurrentLayer(controller, 0)
    return f"OK: {label} 팔 간격 키 {len(points)}개 (베이스 레이어로 복귀)"


def set_in_place(bip_name: str, on: bool) -> str:
    """In Place Mode 를 켜고 끈다. 맥스 모션 패널의 그 버튼과 같은 것이다.

    스튜디오가 이걸 직접 갖는 이유는 **그 버튼이 조용히 죽기 때문**이다. 현재
    레이어가 베이스(0)가 아니면 맥스는 `inPlaceMode = true` 를 에러 없이 무시하고
    값을 false 로 남긴다(맥스 2026 실측). 사용자는 버튼이 안 눌린다고만 느낀다.

    그래서 여기서는 순서를 정해 둔다: **베이스 레이어로 내리고 → 켜고 → 정말
    켜졌는지 확인한다.** 확인까지 하는 이유는 맥스가 거절해도 예외를 던지지
    않아서다. 안 켜졌으면 그렇게 말한다.
    """
    rt = _rt()
    node = rt.getNodeByName(bip_name)
    if node is None:
        return f"ERROR: 바이패드를 찾지 못했습니다: {bip_name}"
    controller = _tm_controller(rt, node)
    if controller is None:
        return f"ERROR: 바이패드가 아닙니다: {bip_name}"

    # 켤 때만 내린다. 끄는 것은 어느 레이어에서든 되고, 보고 있던 레이어를
    # 마음대로 바꾸면 팔 간격을 확인하던 사람의 화면이 뒤집힌다.
    if on:
        try:
            rt.biped.setCurrentLayer(controller, 0)
        except Exception:
            pass

    try:
        controller.inPlaceMode = bool(on)
    except Exception as exc:  # noqa: BLE001 - 맥스 예외를 그대로 전달한다
        return f"ERROR: In Place Mode 를 바꾸지 못했습니다: {exc}"

    actual = bool(controller.inPlaceMode)
    if actual != bool(on):
        return (
            "ERROR: 맥스가 In Place Mode 를 거절했습니다. "
            "피겨 모드·풋스텝 모드가 켜져 있거나 베이스 레이어가 아닙니다"
        )
    return "OK: In Place Mode " + ("켬" if actual else "끔")


def set_arm_space_visible(bip_name: str, visible: bool) -> str:
    """팔 간격 레이어를 잠깐 올려 보거나 베이스로 내린다.

    바이패드는 현재 레이어까지만 합성하므로, 팔 간격을 눈으로 확인하려면 그
    레이어를 현재로 세워야 한다. 대신 그동안 In Place Mode 가 잠긴다 — 그래서
    기본은 내려둔 상태이고 이 함수는 확인용 토글이다.
    """
    rt = _rt()
    node = rt.getNodeByName(bip_name)
    if node is None:
        return f"ERROR: 바이패드를 찾지 못했습니다: {bip_name}"
    controller = _tm_controller(rt, node)
    if controller is None:
        return f"ERROR: 바이패드가 아닙니다: {bip_name}"

    if not visible:
        rt.biped.setCurrentLayer(controller, 0)
        return "OK: 베이스 레이어 (In Place Mode 사용 가능)"

    for i in range(int(rt.biped.numLayers(controller)), 0, -1):
        if str(rt.biped.getLayerName(controller, i)) == "ArmSpace":
            rt.biped.setCurrentLayer(controller, i)
            return "OK: 팔 간격 레이어 (확인하는 동안 In Place Mode 가 잠깁니다)"
    return "OK: 팔 간격 레이어가 없습니다"


def apply_arm_space(bip_name: str, points: Sequence[tuple[int, float]]) -> str:
    """씬의 바이패드를 이름으로 찾아 ArmSpace 레이어를 건다.

    points 는 (프레임, 각도) 쌍. 원본 .ms 는 프레임 0 에 키 하나만 찍었고,
    여기서는 같은 API 로 여러 시각에 찍는다.
    """
    rt = _rt()
    bip = rt.getNodeByName(bip_name)
    if bip is None:
        return f"ERROR: 바이패드를 찾을 수 없음: {bip_name}"
    controller = _tm_controller(rt, bip)
    if rt.classOf(controller) != rt.Vertical_Horizontal_Turn:
        return f"ERROR: not a biped root: {bip_name}"
    return _arm_space(rt, controller, points, bip_name)


def send_to_mixer(bip_name: str, clips_dir: str) -> str:
    """현재 모션을 .bip 으로 저장하고 Motion Mixer 에 올린다."""
    rt = _rt()
    bip = rt.getNodeByName(bip_name)
    if bip is None:
        return f"ERROR: 바이패드를 찾을 수 없음: {bip_name}"
    controller = _tm_controller(rt, bip)
    if rt.classOf(controller) != rt.Vertical_Horizontal_Turn:
        return f"ERROR: not a biped root: {bip_name}"
    if controller.figureMode:
        return f"ERROR: 피겨 모드를 먼저 해제하세요: {bip_name}"

    os.makedirs(clips_dir, exist_ok=True)
    bip_file = os.path.join(clips_dir, f"{bip_name}.bip")

    if controller.mixerMode:
        controller.mixerMode = False
    saved = False
    try:
        saved = rt.biped.saveBipFile(controller, bip_file)
    except Exception:
        saved = False
    if not saved:
        return f"ERROR: saveBipFile failed: {bip_file}"

    controller.mixerMode = True
    mixer = controller.mixer
    if mixer.numTrackgroups == 0:
        rt.appendTrackgroup(mixer)
    track = rt.getTrack(rt.getTrackgroup(mixer, 1), 1)
    ok = False
    try:
        ok = rt.appendClip(track, bip_file, False, 0)
    except Exception:
        ok = False
    if not ok:
        return f"ERROR: appendClip failed: {bip_file}"

    rt.theMixer.updateDisplay()
    rt.theMixer.showMixer()
    return f"OK: {bip_name} → Mixer"
