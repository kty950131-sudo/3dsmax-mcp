"""Max 안에서만 동작하는 손 포즈 적용 (pymxs).

maxbridge.py 관례를 따른다: 공개 함수는 dict 를 돌려주거나 RuntimeError 를
던지고, bridge.py 의 ``reply`` 가 JSON 으로 싼다.

Max 2026 실측으로 확정한 API (이 시그니처가 아니면 Argument count/Type error):

    biped.loadCopyPasteFile <ctrl> <path>            -> 컬렉션이 끝에 추가된다
    biped.getCopyCollection <ctrl> <i>               -> CopyCollection (.name)
    getCopy <collection> <#posture|#pose|#track> <i> -> BipedCopy (전역 함수다.
                                                       biped.getCopy 는 없다)
    biped.pasteBipPosture <ctrl> <copy> <opposite> #pstdefault <hor> <ver> <trn> <byvel>
    biped.pasteBipPose    <ctrl> <copy> <opposite> #pstdefault <hor> <ver> <trn> <byvel>
    biped.pasteBipTrack   <ctrl> <copy> <opposite> <hor> <ver> <trn>   (6개, 타입 인자 없음)

실측으로 확인한 두 가지가 이 파일의 설계를 정한다.

1. **손가락 뼈가 없으면 아무 일도 일어나지 않는다.** 포스처는 손가락 노드에만
   붙으므로 `fingers == 0` 인 바이패드(BVH 임포트본이 대개 그렇다)에서는
   붙여넣기가 성공을 돌려주면서도 화면이 그대로다. 그래서 붙이기 전에 Figure
   모드에서 손가락을 5개 3관절로 만든다. 기존 애니메이션(COM 키·다리 키·프레임별
   값)이 그대로 유지되는 것을 Max 2026 에서 실측했다.
2. **붙여넣기는 그냥은 키를 만들지 않는다.** 정적 포즈만 바뀐다. 키가 필요하면
   ``pymxs.animate(True)`` 컨텍스트 안에서 붙여야 한다(실측: 키 0 → 1).
"""

import os

import pymxs

from maxmcp.ui.studio.hand_poses import opposites_for, pose_cards, shelf_info, source_side
from maxmcp.ui.studio.maxbridge import _rt, _tm_controller

COLLECTION_NAME = "hand_pose"

#: 저장할 때 쓰는 썸네일 종류. **#snapAuto 여야 한다** — #snapNone 은 썸네일을 아예
#: 안 만들고, 우리 카드 목록은 파일 안의 TGA 를 세어 만들기 때문에 그렇게 저장한
#: 포즈는 카드에서 통째로 사라진다. #snapView 는 현재 뷰포트를 그대로 찍어서
#: 대상이 화면 밖이면 빈 그림이 된다(실측: 1669바이트 단색). Max 2026 실측.
SNAPSHOT = "snapAuto"

#: 손 포스처가 붙으려면 있어야 하는 손가락 구조. 표준 Biped 기본값과 같다.
FINGERS = 5
FINGER_LINKS = 3

#: 카드 종류 → (getCopy 에 넘길 이름, 붙여넣기 함수 이름, 인자 개수)
KINDS = {
    "posture": ("posture", "pasteBipPosture", 8),
    "pose": ("pose", "pasteBipPose", 8),
    "track": ("track", "pasteBipTrack", 6),
}

SIDE_LABELS = {"right": "오른쪽", "left": "왼쪽", "both": "양쪽"}


def _root_of(rt, node):
    """노드가 속한 바이패드의 루트(COM). 바이패드가 아니면 None."""
    if node is None:
        return None
    try:
        controller = _tm_controller(rt, node)
    except Exception:
        return None
    if controller is None:
        return None
    if rt.classOf(controller) == rt.Vertical_Horizontal_Turn:
        return node
    root = getattr(controller, "rootNode", None)
    if root is not None:
        try:
            if rt.classOf(_tm_controller(rt, root)) == rt.Vertical_Horizontal_Turn:
                return root
        except Exception:
            return None
    return None


def resolve_target(rt, bip_name: str = ""):
    """적용할 바이패드를 고른다. **씬에서 고른 것이 먼저다.**

    뷰포트에서 팔이든 손가락이든 아무 부위나 눌러 둔 채 카드를 누르면 그
    바이패드에 붙는다. 선택이 바이패드가 아니면 목록에서 고른 이름으로 물러선다.
    """
    for node in rt.selection:
        root = _root_of(rt, node)
        if root is not None:
            return root, "selection"
    if bip_name:
        # 맥스는 같은 이름을 여럿 허용한다. 이름만으로는 어느 것인지 못 정하므로
        # 엉뚱한 바이패드를 고치지 않도록 여기서 멈추고 뷰포트 선택을 요구한다.
        # (실측: 씬에 Bip002 가 셋이라 이름으로 찾은 것이 의도한 바이패드가 아니었다.)
        named = [obj for obj in rt.objects if str(obj.name) == bip_name]
        matches = [obj for obj in named if _root_of(rt, obj) is obj]
        if len(matches) > 1:
            raise RuntimeError(
                f"'{bip_name}' 이름의 바이패드가 {len(matches)}개입니다. "
                "뷰포트에서 대상 바이패드의 아무 부위나 선택한 뒤 다시 누르세요."
            )
        if matches:
            return matches[0], "list"
        raise RuntimeError(f"바이패드를 찾지 못했습니다: {bip_name}")
    raise RuntimeError("바이패드를 뷰포트에서 고르거나 목록에서 선택하세요")


def selected_root_name() -> str:
    """뷰포트에서 고른 바이패드의 **루트 이름**. 바이패드가 아니면 빈 문자열.

    팔이든 손가락이든 아무 부위나 잡아도 그 바이패드를 가리키게 한다. 화면의
    "대상" 칸이 이것을 따라가야, 바이패드를 잡아 둔 채 임포트를 눌렀을 때
    새로 만들지 않고 그 바이패드에 적용된다.
    """
    rt = _rt()
    for node in rt.selection:
        root = _root_of(rt, node)
        if root is not None:
            return str(root.name)
    return ""


def any_biped(rt):
    """씬의 아무 바이패드 루트. 컬렉션 목록을 조회할 때만 쓴다."""
    for obj in rt.objects:
        try:
            if rt.classOf(_tm_controller(rt, obj)) == rt.Vertical_Horizontal_Turn:
                return obj
        except Exception:
            continue
    return None


def fingers_attached(rt, controller) -> bool:
    """손가락 뿌리 마디가 손에 붙어 있는가.

    개수만 맞고 **부모가 끊긴** 상태가 실제로 나왔다: 오른손 Finger0~4 의 parent 가
    None 이라, 포즈를 붙여도 그 손만 꿈쩍하지 않았다("엄지만 움직인다" 로 보였다).
    개수 검사만으로는 이 상태를 못 잡는다.
    """
    step = max(1, int(controller.fingerLinks))
    for side in ("rFingers", "lFingers"):
        for finger in range(int(controller.fingers)):
            node = rt.biped.getNode(controller, rt.Name(side), link=finger * step + 1)
            if node is None or node.parent is None:
                return False
    return True


def ensure_fingers(rt, controller) -> bool:
    """손가락을 5개 3관절로 맞추고, 손에 붙어 있는지까지 확인한다.

    Figure 모드를 잠깐 켰다 끄는 것 외에 부작용은 없다 — COM 키, 다리 키,
    프레임별 값이 그대로인 것을 Max 2026 에서 실측했다. 뼈가 끊겨 있으면
    한 번 지웠다 다시 만들어 손에 붙인다.
    """
    counts_ok = (
        int(controller.fingers) == FINGERS and int(controller.fingerLinks) == FINGER_LINKS
    )
    if counts_ok and fingers_attached(rt, controller):
        return False
    was_figure = bool(controller.figureMode)
    controller.figureMode = True
    try:
        # 개수가 맞는데 끊겨 있으면 0 으로 지웠다 다시 만들어야 손에 붙는다.
        if counts_ok:
            controller.fingers = 0
        controller.fingers = FINGERS
        controller.fingerLinks = FINGER_LINKS
    finally:
        controller.figureMode = was_figure
    return True


def _find_collection(rt, controller, name: str):
    for i in range(1, int(rt.biped.numCopyCollections(controller)) + 1):
        col = rt.biped.getCopyCollection(controller, i)
        if str(col.name) == name:
            return col
    return None


def ensure_collection(rt, controller, shelf: str = "hand"):
    """선반의 컬렉션을 현재 컬렉션으로 만들어 돌려준다.

    없으면 .cpy 를 읽고, 파일도 없으면 빈 컬렉션을 만든다(전신 포즈 선반은
    사용자가 처음 저장하기 전까지 파일이 없다). 이미 있으면 다시 읽지 않는다 —
    loadCopyPasteFile 은 같은 파일도 매번 새 컬렉션으로 추가한다.
    (복사 컬렉션은 바이패드별이 아니라 씬 전체에서 공유된다. 실측.)
    """
    info = shelf_info(shelf)
    name, path = info["collection"], info["path"]
    col = _find_collection(rt, controller, name)
    if col is None:
        if os.path.isfile(path):
            if not rt.biped.loadCopyPasteFile(controller, path):
                raise RuntimeError(f"복사 컬렉션을 읽지 못했습니다: {path}")
            col = _find_collection(rt, controller, name)
            if col is None:
                raise RuntimeError(f"컬렉션 '{name}' 이 파일에 없습니다: {path}")
        else:
            col = rt.biped.createCopyCollection(controller, name)
            if col is None:
                raise RuntimeError(f"컬렉션을 만들지 못했습니다: {name}")
    rt.biped.setCurrentCopyCollection(controller, col)
    return col


def copy_catalog(shelf: str = "hand") -> dict:
    """``{이름: {"kind": posture|pose|track, "index": n}}``.

    파일에서 뽑은 썸네일에 종류를 붙이려고 쓴다. 같은 .cpy 에 포스처·포즈·트랙이
    섞여 있어도 카드가 알맞은 붙여넣기 함수를 고를 수 있다. 씬에 바이패드가
    하나도 없으면 빈 dict 다 — 그때는 카드가 전부 포스처로 뜨고, 바이패드가
    생긴 뒤 새로고침하면 바로잡힌다.
    """
    rt = _rt()
    node = any_biped(rt)
    if node is None:
        return {}
    controller = _tm_controller(rt, node)
    col = ensure_collection(rt, controller, shelf)
    catalog = {}
    for kind, (type_name, _fn, _argc) in KINDS.items():
        try:
            total = int(rt.biped.numCopies(controller, rt.Name(type_name)))
        except Exception:
            continue
        for i in range(1, total + 1):
            name = str(rt.biped.getCopyName(controller, rt.Name(type_name), i))
            catalog[name] = {"kind": kind, "index": i, "source": source_side(name)}
    return catalog


def _paste(rt, controller, copy, kind: str, opposite: bool) -> None:
    """종류에 맞는 붙여넣기. 반환값은 보지 않는다 — MAXScript 에서는 true 가
    오지만 pymxs 를 거치면 같은 호출이 False 로 온다(Max 2026 실측)."""
    _type_name, fn_name, argc = KINDS[kind]
    fn = getattr(rt.biped, fn_name)
    if argc == 8:
        fn(controller, copy, opposite, rt.Name("pstdefault"), False, False, False, False)
    else:
        fn(controller, copy, opposite, False, False, False)


def apply_hand_pose(
    bip_name: str = "",
    index: int = 1,
    side: str = "right",
    kind: str = "posture",
    key: bool = False,
    shelf: str = "hand",
) -> dict:
    """카드 하나를 바이패드에 붙인다.

    side: "right" 는 그대로, "left" 는 opposite 로, "both" 는 둘 다.
          전신 포즈 선반에는 좌우가 없으므로 무시한다.
    key:  True 면 현재 프레임에 키를 남긴다(애니메이션). False 면 포즈만 바뀐다.
    """
    info = shelf_info(shelf)
    if not info["sides"]:
        side = "right"  # 전신 포즈는 미러 없이 그대로 붙인다
    if kind not in KINDS:
        raise RuntimeError(f"모르는 카드 종류입니다: {kind}")
    if side not in ("right", "left", "both"):
        raise RuntimeError(f"side 는 right/left/both 중 하나입니다: {side}")

    rt = _rt()
    root, picked_by = resolve_target(rt, bip_name)
    controller = _tm_controller(rt, root)
    type_name = KINDS[kind][0]

    # 검사는 undo 블록 **밖에서** 끝낸다. pymxs.undo 컨텍스트 안에서 예외를 올리면
    # 파이썬 예외가 아니라 Max 가 "Unknown exception thrown" 으로 죽는다(실측).
    col = ensure_collection(rt, controller, shelf)
    total = int(rt.biped.numCopies(controller, rt.Name(type_name)))
    if not 1 <= index <= total:
        label = {"posture": "포스처", "pose": "포즈", "track": "애니"}[kind]
        raise RuntimeError(f"{label} 카드가 {total}개뿐입니다: {index} 번은 없습니다")
    copy = rt.getCopy(col, rt.Name(type_name), index)
    name = str(rt.biped.getCopyName(controller, rt.Name(type_name), index))
    origin = source_side(name) if info["sides"] else ""
    opposites = opposites_for(origin, side) if info["sides"] else [False]

    # 손가락이 없으면 포스처가 붙을 곳이 없다. 만들어 준다.
    # **undo 블록 밖에서** 한다: Figure 모드 전환은 뼈대를 다시 만드는 큰 작업이라
    # undo 기록 안에서 반복하면 Max 의 파이썬 인터프리터가 통째로 죽는 것을 봤다
    # (그 뒤로는 python.Init 으로도 안 살아나고 Max 를 다시 켜야 했다).
    made_fingers = ensure_fingers(rt, controller) if info["sides"] else False

    with pymxs.undo(True, "hand pose"):
        if key:
            # 붙여넣기는 그냥은 키를 만들지 않는다. animate 컨텍스트가 필요하다(실측).
            with pymxs.animate(True):
                for opposite in opposites:
                    _paste(rt, controller, copy, kind, opposite)
        else:
            for opposite in opposites:
                _paste(rt, controller, copy, kind, opposite)

    rt.redrawViews()
    frame = int(rt.sliderTime.frame)
    layer = int(rt.biped.getCurrentLayer(controller))
    where = SIDE_LABELS[side] if info["sides"] else "전신"
    if origin and side != "both" and origin != side:
        where += " (미러)"
    message = f"{name} → {root.name} ({where}, f{frame}, L{layer}{', 키' if key else ''})"
    if made_fingers:
        message += f" · 손가락 {FINGERS}개 {FINGER_LINKS}관절 생성"
    if picked_by == "selection":
        message += " · 선택한 바이패드"
    return {
        "message": message,
        "biped": str(root.name),
        "frame": frame,
        "layer": layer,
        "made_fingers": made_fingers,
        "picked_by": picked_by,
        "source": origin,
        "mirrored": bool(origin) and side != "both" and origin != side,
    }


# ---- 맥스에서 떠서 선반에 올리기 -------------------------------------------
# Max 2026 실측 시그니처 (인자 개수가 다르면 Argument count error):
#   biped.copyBipPosture <ctrl> <col> <노드배열> <#snapAuto>   (4개)
#   biped.copyBipPose    <ctrl> <col> <#snapAuto>              (3개, 전신이라 노드 목록이 없다)
#   biped.setCopyName    <ctrl> <#posture|#pose> <i> <이름>
#   biped.deleteCopy     <ctrl> <#posture|#pose> <이름>        (인덱스로는 못 지운다)
#   biped.saveCopyPasteFile <ctrl> <경로>                      (현재 컬렉션만 저장한다)

#: 손 포즈에 담을 노드. 손목(#rarm link 4 = R Hand)까지 넣는다 — 손가락만 담으면
#: 손목 각도가 빠져서 같은 포즈로 보이지 않는다. 팔뚝(link 3)은 넣지 않는다:
#: 팔 전체가 따라오면 포즈가 아니라 팔 동작을 덮어쓰게 된다.
HAND_LINK = 4
ARM_TRACK = {"right": "rarm", "left": "larm"}
FINGER_TRACK = {"right": "rFingers", "left": "lFingers"}


def _hand_nodes(rt, controller, side: str) -> list:
    nodes = [rt.biped.getNode(controller, rt.Name(ARM_TRACK[side]), link=HAND_LINK)]
    total = int(controller.fingers) * int(controller.fingerLinks)
    for link in range(1, total + 1):
        node = rt.biped.getNode(controller, rt.Name(FINGER_TRACK[side]), link=link)
        if node is not None:
            nodes.append(node)
    return [n for n in nodes if n is not None]


def _copy_names(rt, controller, type_name: str) -> list:
    total = int(rt.biped.numCopies(controller, rt.Name(type_name)))
    return [str(rt.biped.getCopyName(controller, rt.Name(type_name), i)) for i in range(1, total + 1)]


def _index_of(rt, controller, type_name: str, name: str) -> int:
    """이름으로 번호를 찾는다. **번호는 이름순이라 새 복사본이 끝에 오지 않는다.**

    맥스는 새 복사본을 컬렉션 끝에 붙이지 않고 이름순 자리에 끼워 넣는다
    (실측: 40개짜리 컬렉션에 RArmRFing03 을 뜨니 3번이 되었다). 그래서
    ``numCopies`` 를 새 복사본의 번호로 쓰면 **맨 뒤에 있던 남의 포즈를**
    가리키게 되고, 이름을 바꾸는 순간 그 포즈를 덮어쓴다. 실제로 그렇게
    사용자의 포스처 하나를 잃었다. 번호는 반드시 이름으로 되찾는다.
    """
    for i, existing in enumerate(_copy_names(rt, controller, type_name), start=1):
        if existing == name:
            return i
    raise RuntimeError(f"복사본을 찾지 못했습니다: {name}")


def _unique_name(rt, controller, type_name: str, wanted: str, skip: str) -> str:
    """같은 이름이 이미 있으면 뒤에 번호를 붙인다. 카드는 이름으로 종류·번호를
    찾으므로(copy_catalog) 이름이 겹치면 엉뚱한 포즈가 붙는다."""
    taken = {n for n in _copy_names(rt, controller, type_name) if n != skip}
    if wanted not in taken:
        return wanted
    for n in range(2, 1000):
        candidate = f"{wanted}{n}"
        if candidate not in taken:
            return candidate
    raise RuntimeError(f"이름이 너무 많이 겹칩니다: {wanted}")


def _write_shelf_file(rt, controller, shelf: str):
    """선반 컬렉션을 자기 .cpy 로 굽고 ``(맥스 개수, 카드로 보이는 개수)`` 를 돌려준다.

    저장은 **현재 컬렉션 전체**를 쓴다. 고른 포즈 하나만 골라 담을 수는 없으므로,
    사용자가 맥스의 Copy/Paste 롤아웃에서 따로 뜬 포스처도 함께 파일에 들어간다.

    두 숫자가 갈리는 이유: 카드 목록은 파일에 박힌 TGA 썸네일을 세어 만드는데,
    썸네일 없이(#snapNone) 뜬 복사본은 파일에 들어가고도 카드가 안 생긴다.
    실측으로 41개 중 6개가 그렇게 안 보였다. 숫자를 둘 다 돌려주어 화면이
    "왜 개수가 다른가" 를 스스로 말하게 한다.

    남은 포즈가 없으면 파일을 지운다 — 빈 컬렉션을 저장한 파일은 다음에 읽을 때
    컬렉션이 없다며 실패한다.
    """
    info = shelf_info(shelf)
    path = info["path"]
    count = int(rt.biped.numCopies(controller, rt.Name(info["kind"])))
    if count == 0:
        if os.path.isfile(path):
            os.remove(path)
        return 0, 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not rt.biped.saveCopyPasteFile(controller, path):
        raise RuntimeError(f"포즈 파일을 저장하지 못했습니다: {path}")
    return count, len(pose_cards(path))


def save_copy(shelf: str = "hand", bip_name: str = "", side: str = "right", name: str = "") -> dict:
    """지금 맥스에 서 있는 자세를 떠서 선반에 올린다.

    손 선반은 고른 쪽 손목+손가락만, 전신 선반은 바이패드 전체를 담는다.
    이름을 주지 않으면 맥스가 붙이는 이름을 그대로 쓴다.
    """
    info = shelf_info(shelf)
    rt = _rt()
    root, picked_by = resolve_target(rt, bip_name)
    controller = _tm_controller(rt, root)

    if info["sides"] and side not in ("right", "left"):
        raise RuntimeError("저장할 손을 왼쪽이나 오른쪽으로 고르세요 (양쪽은 저장할 수 없습니다)")

    col = ensure_collection(rt, controller, shelf)
    type_name = info["kind"]
    # 손가락 만들기는 undo 밖에서 끝낸다 — Figure 모드 전환을 undo 안에서 하면
    # 맥스 파이썬이 통째로 죽는 것을 봤다.
    made_fingers = ensure_fingers(rt, controller) if info["sides"] else False

    if info["sides"]:
        nodes = _hand_nodes(rt, controller, side)
        if len(nodes) < 2:
            raise RuntimeError("손 노드를 찾지 못했습니다. 손가락이 있는 바이패드인지 확인하세요.")
        made = rt.biped.copyBipPosture(controller, col, nodes, rt.Name(SNAPSHOT))
    else:
        made = rt.biped.copyBipPose(controller, col, rt.Name(SNAPSHOT))
    if made is None:
        raise RuntimeError("포즈를 뜨지 못했습니다.")
    # 맥스가 붙인 이름이 유일한 단서다. 번호는 이름순이라 믿을 수 없다.
    final = str(made.name)

    wanted = (name or "").strip()
    if wanted:
        # 손 포즈는 **이름 첫 글자가 미러 여부를 정한다**(R/L). 사용자가 지은
        # 이름에 그 글자가 없으면 왼손 포즈가 오른손으로 들어간다.
        if info["sides"] and source_side(wanted) != side:
            wanted = ("R" if side == "right" else "L") + wanted
        wanted = _unique_name(rt, controller, type_name, wanted, final)
        rt.biped.setCopyName(controller, rt.Name(type_name), _index_of(rt, controller, type_name, final), wanted)
        final = wanted
    # 이름을 바꾸면 자리도 다시 정렬된다. 번호는 마지막에 한 번 더 찾는다.
    index = _index_of(rt, controller, type_name, final)

    count, visible = _write_shelf_file(rt, controller, shelf)
    message = f"{info['label']}에 저장: {final} (카드 {visible}개)"
    if count != visible:
        message += f" · 썸네일 없는 {count - visible}개는 카드로 안 보입니다"
    if info["sides"]:
        message += f" · {SIDE_LABELS[side]} 손"
    if made_fingers:
        message += f" · 손가락 {FINGERS}개 {FINGER_LINKS}관절 생성"
    if picked_by == "selection":
        message += " · 선택한 바이패드"
    return {
        "message": message,
        "name": final,
        "index": index,
        "count": count,
        "visible": visible,
        "biped": str(root.name),
        "made_fingers": made_fingers,
    }


def delete_copy(shelf: str = "hand", name: str = "", bip_name: str = "") -> dict:
    """선반에서 포즈 하나를 지우고 파일을 다시 굽는다."""
    info = shelf_info(shelf)
    if not name:
        raise RuntimeError("지울 포즈 이름이 없습니다")
    rt = _rt()
    node = any_biped(rt)
    if node is None:
        raise RuntimeError("씬에 바이패드가 없어 컬렉션을 열 수 없습니다")
    try:
        root, _picked = resolve_target(rt, bip_name)
    except RuntimeError:
        root = node
    controller = _tm_controller(rt, root)
    ensure_collection(rt, controller, shelf)
    rt.biped.deleteCopy(controller, rt.Name(info["kind"]), name)
    count, visible = _write_shelf_file(rt, controller, shelf)
    return {
        "message": f"{name} 삭제 (카드 {visible}개 남음)",
        "name": name,
        "count": count,
        "visible": visible,
    }


# ---- 바이패드 애니메이션 레이어 -------------------------------------------
# 맥스 모션 패널의 Layers 롤아웃과 같은 일을 한다. 스튜디오에서 포즈를 얹고
# 지우는 동안 패널을 오가지 않으려고 여기에 둔다.
#
# Max 2026 실측 시그니처:
#   biped.numLayers <ctrl> / getCurrentLayer / setCurrentLayer <ctrl> <i>
#   biped.createLayer <ctrl> <i> <name> / deleteLayer <ctrl> <i>
#   biped.getLayerName <ctrl> <i> / getLayerActive / setLayerActive <ctrl> <i> <bool>
#   biped.collapseAtLayer <ctrl> <i>
# 레이어 0 은 베이스이고 목록에 항상 있다(만들거나 지울 수 없다).

LAYER_NAME = "layer"


def _layers_of(rt, controller) -> dict:
    count = int(rt.biped.numLayers(controller))
    layers = [{"index": 0, "name": "", "active": True}]
    for i in range(1, count + 1):
        layers.append(
            {
                "index": i,
                "name": str(rt.biped.getLayerName(controller, i)),
                "active": bool(rt.biped.getLayerActive(controller, i)),
            }
        )
    return {"current": int(rt.biped.getCurrentLayer(controller)), "layers": layers}


def layer_state(bip_name: str = "") -> dict:
    """레이어 목록과 현재 레이어. 대상을 못 정하면 빈 상태 —
    화면의 한 칸일 뿐이라 오류로 막지 않는다."""
    rt = _rt()
    try:
        root, _picked = resolve_target(rt, bip_name)
    except RuntimeError:
        return {"biped": "", "current": 0, "layers": []}
    state = _layers_of(rt, _tm_controller(rt, root))
    state["biped"] = str(root.name)
    return state


def layer_op(bip_name: str = "", op: str = "select", index=None) -> dict:
    """레이어 버튼 하나 = op 하나. 끝나면 새 상태를 돌려준다.

    undo 블록으로 감싸지 않는다: Figure 모드 전환을 undo 안에서 반복했다가 맥스
    파이썬이 통째로 죽는 것을 봤고, 레이어 조작은 맥스가 스스로 기록한다.
    지우기는 되돌릴 수 없으므로 화면에서 확인을 받는다.
    """
    rt = _rt()
    root, _picked = resolve_target(rt, bip_name)
    controller = _tm_controller(rt, root)
    count = int(rt.biped.numLayers(controller))
    current = int(rt.biped.getCurrentLayer(controller))
    target = current if index is None else int(index)

    if op == "create":
        at = count + 1  # 언제나 맨 위에 쌓는다 — 목록 순서와 번호가 어긋나지 않는다
        rt.biped.createLayer(controller, at, f"{LAYER_NAME}{at}")
        rt.biped.setCurrentLayer(controller, at)
    elif op == "delete":
        if target < 1:
            raise RuntimeError("베이스 레이어(레이어0)는 지울 수 없습니다")
        if target > count:
            raise RuntimeError(f"없는 레이어입니다: {target}")
        rt.biped.deleteLayer(controller, target)
    elif op == "select":
        if not 0 <= target <= count:
            raise RuntimeError(f"레이어 번호가 범위 밖입니다: {target}")
        rt.biped.setCurrentLayer(controller, target)
    elif op == "toggle":
        if target < 1:
            raise RuntimeError("베이스 레이어(레이어0)는 끌 수 없습니다")
        rt.biped.setLayerActive(controller, target, not bool(rt.biped.getLayerActive(controller, target)))
    elif op == "collapse":
        # 합병: **고른 레이어 하나**만 바로 아래로 내린다. 위에 쌓인 레이어는 그대로다
        # (실측: 3칸에서 2번을 합치면 2칸이 되고 이름은 layer1/layer2 로 남는다).
        if target < 1:
            raise RuntimeError("합병할 레이어를 고르세요 (레이어1 이상). 베이스로는 합칠 수 없습니다")
        if target > count:
            raise RuntimeError(f"없는 레이어입니다: {target}")
        # **인자는 "합칠 레이어" 가 아니라 "받을 아래 레이어" 다.** 맥스는 넘긴 번호
        # 바로 위의 칸을 그 칸으로 내린다. 그대로 target 을 넘기면 한 칸 위가 합쳐지고,
        # 맨 위 칸을 고르면 위가 없어서 "Array index out of range" 로 튕긴다 —
        # 눌러도 칸 수가 그대로이던 것이 이것이다.
        # 실측(Max 2026): 4칸에서 collapseAtLayer 4 는 범위 오류, 3 을 넘기면
        # 맨 위 칸이 그 아래로 합쳐져 3칸이 된다.
        rt.biped.setCurrentLayer(controller, target)
        rt.biped.collapseAtLayer(controller, target - 1)
        left = int(rt.biped.numLayers(controller))
        if left >= count:
            # 조용한 실패를 성공으로 보고하지 않는다.
            raise RuntimeError(
                f"레이어{target} 이 레이어{target - 1} 로 합쳐지지 않았습니다 "
                f"(칸 수 {count} 그대로). 맥스 모션 패널에서 그 레이어가 활성인지 확인하세요."
            )
        # 합쳐 넣은 칸으로 내려선다. 없어진 번호에 서 있으면 다음 조작이 엉뚱한 칸을 짚는다.
        rt.biped.setCurrentLayer(controller, max(0, min(target - 1, left)))
    else:
        raise RuntimeError(f"모르는 레이어 동작입니다: {op}")

    rt.redrawViews()
    state = _layers_of(rt, controller)
    state["biped"] = str(root.name)
    return state
