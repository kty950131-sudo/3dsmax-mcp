"""Max 안에서만 동작하는 pymxs 호출 모음.

bvh_biped_ui.ms 의 검증된 절차를 파이썬으로 옮긴 것이다. 원본 .ms 는 그대로
남아 있고 이 모듈과 독립적으로 동작한다. 모든 공개 함수는 원본 .ms 관례대로
"OK: ..." 또는 "ERROR: ..." 문자열을 돌려준다.
"""

import os
from typing import Optional, Sequence

from src.helpers.bvh import DEFAULT_BIPED_PRUNE, has_upright_spine, prepare_for_biped


def _rt():
    from pymxs import runtime

    return runtime


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
            if rt.classOf(obj.transform.controller) == rt.Vertical_Horizontal_Turn:
                names.append(obj.name)
        except Exception:
            continue
    return names


def convert_clip(
    src_path: str,
    x_offset: float = 0.0,
    speed: float = 1.0,
    trim: tuple[float, float] = (0.0, 1.0),
    time_map: Optional[Sequence[float]] = None,
) -> tuple[str, bool]:
    """*_biped.bvh 를 만들고 (경로, 직립여부) 를 돌려준다."""
    text = open(src_path, encoding="utf-8", errors="replace").read()
    converted = prepare_for_biped(
        text,
        prune=DEFAULT_BIPED_PRUNE,
        offset=(x_offset, 0.0, 0.0),
        speed=speed,
        trim_range=trim,
        time_map=time_map,
    )
    stem, _ = os.path.splitext(src_path)
    out_path = f"{stem}_biped.bvh"
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(converted)
    return out_path, has_upright_spine(text)


def import_clip(
    src_path: str,
    bip_name: str,
    convert: bool,
    x_offset: float,
    speed: float = 1.0,
    trim: tuple[float, float] = (0.0, 1.0),
    time_map: Optional[Sequence[float]] = None,
    mirror: bool = False,
) -> str:
    """바이패드를 만들고 클립을 올린다."""
    if not os.path.isfile(src_path):
        return f"ERROR: file not found: {src_path}"

    load_path = src_path
    upright = True
    if convert:
        try:
            load_path, upright = convert_clip(src_path, x_offset, speed, trim, time_map)
        except Exception as exc:
            return f"ERROR: convert failed: {exc}"

    rt = _rt()
    bip = rt.biped.createNew(170, -90, rt.Point3(0, 0, 0))
    if bip is None:
        return "ERROR: biped.createNew failed"
    if bip_name:
        bip.name = bip_name

    controller = bip.transform.controller
    old_quiet = rt.setQuietMode(True)
    ok = False
    try:
        ok = rt.biped.loadMocapFile(controller, load_path)
    except Exception:
        ok = False
    finally:
        rt.setQuietMode(old_quiet)

    if not ok:
        # 실패한 바이패드를 씬에 남기지 않는다 (원본 .ms 와 같은 규칙)
        try:
            rt.delete(bip)
        except Exception:
            pass
        return f"ERROR: loadMocapFile rejected {load_path}"

    if mirror:
        try:
            rt.biped.mirror(controller)
        except Exception:
            pass

    msg = f"OK: {bip.name}"
    if not upright:
        msg += " | 경고: T포즈 골격이 아님(_tpose 파일 권장) — 자세가 틀어질 수 있음"
    return msg


class _animate:
    """``animate on`` 블록의 파이썬 대응."""

    def __init__(self, rt) -> None:
        self._rt = rt

    def __enter__(self) -> None:
        self._rt.animate = True

    def __exit__(self, *exc: object) -> None:
        self._rt.animate = False


def apply_arm_space(bip_name: str, points: Sequence[tuple[int, float]]) -> str:
    """ArmSpace 레이어에 시각별 팔 벌림 키를 찍는다.

    points 는 (프레임, 각도) 쌍. 원본 .ms 는 프레임 0 에 키 하나만 찍었고,
    여기서는 같은 API 로 여러 시각에 찍는다.
    """
    rt = _rt()
    bip = rt.getNodeByName(bip_name)
    if bip is None:
        return f"ERROR: 바이패드를 찾을 수 없음: {bip_name}"
    controller = bip.transform.controller
    if rt.classOf(controller) != rt.Vertical_Horizontal_Turn:
        return f"ERROR: not a biped root: {bip_name}"

    for i in range(int(rt.biped.numLayers(controller)), 0, -1):
        if rt.biped.getLayerName(controller, i) == "ArmSpace":
            rt.biped.deleteLayer(controller, i)

    if not points or all(deg == 0 for _, deg in points):
        return f"OK: {bip_name} 팔 간격 없음"

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

    # ArmSpace 를 현재 레이어로 남긴다 — 0 번으로 되돌리면 오프셋이 사라진다
    return f"OK: {bip_name} 팔 간격 키 {len(points)}개"


def send_to_mixer(bip_name: str, clips_dir: str) -> str:
    """현재 모션을 .bip 으로 저장하고 Motion Mixer 에 올린다."""
    rt = _rt()
    bip = rt.getNodeByName(bip_name)
    if bip is None:
        return f"ERROR: 바이패드를 찾을 수 없음: {bip_name}"
    controller = bip.transform.controller
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
