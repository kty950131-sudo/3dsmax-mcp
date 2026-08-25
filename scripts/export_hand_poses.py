"""손 포스처를 1프레임 BVH 로 굽는다. **3ds Max 안에서** 돈다.

    Max 리스너(F11)에서:
        python.ExecuteFile @"C:\\work\\Ai\\3dsmax-mcp\\scripts\\export_hand_poses.py"

결과와 로그: `C:\\work\\Ai\\pose\\biped\\hand\\`

## 첫 판이 왜 헛돌았나 (셋 다 실측으로 확인)

**하나, 손가락이 0개였다.** 씬 바이패드에 손가락 링크가 없어서 관절 23개짜리
BVH 가 나왔다. 손 포스처는 손가락만 바꾸므로 **34장이 바이트 단위로 똑같았다.**
`ensure_fingers` 를 직접 부르고 개수를 확인한다.

**둘, 1프레임이 아니라 180프레임이었다.** `export_biped_bvh` 는 기존 키 구간을
찾아 그 전체를 쓴다. 자세 한 장이 필요하므로 `biped_frames` 를 한 프레임만
불러 직접 쓴다.

**셋, 바이패드에 클립이 올라가 있었다.** 애니메이션이 있으면 포스처를 붙여도
그 프레임의 키가 자세를 도로 덮는다. 굽기 전에 `clearAllAnimation` 으로 비운다.

## 장면을 지키는 방법

시작할 때 `holdMaxFile`, 끝날 때 `fetchMaxFile` 을 부른다. 애니메이션을 비우고
손가락을 만드는 일이 모두 그 안에서 일어나므로, 끝나면 **사장님 장면이 손대기
전 그대로** 돌아온다. 되돌리기 루프(`max undo`)는 쓰지 않는다 — 그걸로 작업
194개를 잃은 적이 있다(2026-08-22).
"""
from __future__ import annotations

import os
import re
import sys
import traceback

ROOT = r"C:\work\Ai\3dsmax-mcp"
OUT_DIR = r"C:\work\Ai\pose\biped\hand"
LOG_PATH = os.path.join(OUT_DIR, "_export.log")
SHELF = "hand"
# 양손을 함께 찍는다. 원본 포스처는 오른손 기준이고, `apply_hand_pose` 가
# "both" 를 받으면 왼손에는 거울로 붙인다. 한 파일에 두 손이 다 들어간다.
SIDE = "both"
# 기준으로 삼을 포스처. 34장을 재서 마디 사잇각이 가장 작은 것을 골랐다
# (rfing06 이 평균 8.0도로 가장 곧다. 다음이 rfing02 의 15.6도).
NEUTRAL_POSE = "RFing06"

_lines: list[str] = []


def log(message: str) -> None:
    _lines.append(str(message))
    try:
        print(message)
    except Exception:
        pass


def flush() -> None:
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(LOG_PATH, "w", encoding="utf-8") as handle:
            handle.write("\n".join(_lines) + "\n")
    except Exception:
        traceback.print_exc()


def slug(name: str) -> str:
    out = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    return out or "pose"


def _figure_basis(rt, controller, joints):
    """`biped_rest_basis` 와 같은 계산이되 피겨 모드를 끄지 않는다.

    그 함수는 일부러 애니 모드로 내려가는데(척추·다리 사슬 때문에), 손 포즈는
    곧게 편 손을 기준으로 삼아야 해서 여기서는 그러면 안 된다.
    """
    from maxmcp.ui.studio.biped_export import (
        _selected_joints, _world_rotation, _xyz, _max_to_bvh_xyz,
    )
    selected = _selected_joints(rt, controller, joints)
    places = {j.name: _xyz(j.node.transform.translation) for j in selected}
    rest = {j.name: _world_rotation(j.node.transform) for j in selected}
    offsets = {}
    for joint in selected:
        if joint.parent is None:
            offsets[joint.name] = (0.0, 0.0, 0.0)
            continue
        here, up = places[joint.name], places[joint.parent]
        offsets[joint.name] = _max_to_bvh_xyz(
            (here[0] - up[0], here[1] - up[1], here[2] - up[2])
        )
    return offsets, rest


def run() -> None:
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)

    from pymxs import runtime as rt

    # Max 의 파이썬은 한 번 읽은 모듈을 계속 들고 있다. 소스를 고치고 이 파일을
    # 다시 돌려도 **옛 코드가 그대로 돈다** — 손가락 이름을 SOMA 규격으로 고쳤는데
    # 결과가 안 바뀌어서 한 판을 헛돌았다. 쓰기 전에 다시 읽는다.
    import importlib
    for name in (
        "maxmcp.helpers.bvh",
        "maxmcp.ui.studio.biped_export",
        "maxmcp.ui.studio.biped_pose",
    ):
        module = sys.modules.get(name)
        if module is not None:
            importlib.reload(module)
    log("모듈 다시 읽음")

    from maxmcp.ui.studio.biped_pose import (
        apply_hand_pose, copy_catalog, any_biped, ensure_fingers,
    )
    from maxmcp.ui.studio.biped_export import (
        biped_skeleton, biped_rest_basis, biped_frames,
    )
    from maxmcp.helpers.bvh import BvhFile, serialize_bvh
    from maxmcp.ui.studio.biped_export import _bvh_tree

    node = any_biped(rt)
    if node is None:
        log("★ 씬에 바이패드가 없습니다.")
        return
    controller = rt.getTMController(node)
    log(f"바이패드 {node.name} · 손가락 {int(controller.fingers)}개")

    catalog = copy_catalog(SHELF)
    postures = {n: v for n, v in catalog.items() if v.get("kind") == "posture"} or catalog
    log(f"컬렉션 {len(catalog)}개 · 굽는 것 {len(postures)}개")
    if not postures:
        log("★ 컬렉션이 비어 있습니다.")
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    rt.holdMaxFile()
    written: list[str] = []
    failed: list[str] = []
    try:
        # `clearAllAnimation` 은 부르지 않는다. 처음엔 클립이 자세를 덮을까 봐
        # 넣었는데, 그 뒤 손가락이 만들어지지 않았다(생성 True 인데 0개 x 0마디).
        # BVH Studio 도 그것을 안 부른다. 포스처를 키로 남기면(key=True) 그
        # 프레임에 고정되므로 애니메이션을 비울 이유가 없다.
        log(f"피겨 모드 지금 {bool(controller.figureMode)}")
        try:
            made = ensure_fingers(rt, controller)
        except Exception as exc:
            made = f"예외 {type(exc).__name__}: {exc}"
        count, links = int(controller.fingers), int(controller.fingerLinks)
        log(f"손가락 생성 {made} → 지금 {count}개 x {links}마디")
        if count == 0 or links == 0:
            # 여기서 멈춘다. 손가락이 없으면 34장이 전부 같은 파일이 된다
            # (실제로 그렇게 나왔다). 헛수고를 만들지 않는다.
            log("★ 손가락이 안 만들어졌습니다. 굽지 않고 멈춥니다.")
            log("   피겨 모드를 직접 켜고 Structure 롤아웃에서 Fingers 5 / Links 3 을"
                " 준 뒤 다시 돌리면 됩니다.")
            return

        frame = int(rt.currentTime)

        # 휴식 자세는 **포스처를 붙이기 전에 한 번만** 잡는다.
        #
        # 처음엔 장마다 같은 프레임으로 휴식과 자세를 함께 읽었는데, 그러면
        # `biped_frames` 가 "휴식으로부터의 변화" 를 쓰므로 회전이 전부 0 이
        # 된다. 자세가 채널이 아니라 골격(OFFSET)에 구워져서, 파일은 서로
        # 달라도 뷰어에서는 34장이 같은 손이 나왔다.
        #
        # 손대지 않은 손을 휴식으로 삼으면 각 포스처의 채널이 그로부터의
        # 차이를 제대로 들고 간다.
        base_joints = biped_skeleton(rt, controller)

        # 기준은 **편 손**으로 잡되, 모드는 애니 모드 그대로 둔다.
        #
        # 두 번 틀렸다.
        #
        # 처음에는 그냥 그 프레임을 읽었다. 그런데 Max 의 손이 이미 쥐어져
        # 있어서 34장이 "주먹에서 얼마나 폈나" 로 기록됐고, 웹에서 손가락이
        # 반대로 꺾여 보였다(쉬는 골격 검지 42도·76도).
        #
        # 다음에는 피겨 자세로 바꿨다. **볼트가 하지 말라고 적어 둔 것이었고
        # 적힌 그대로 깨졌다** — 쉬는 골격이 평균 46%, 위팔은 63.5% 어긋났다.
        # 어긋나는 것은 다리 사슬만이 아니다.
        #
        # 그래서 모드는 건드리지 않고, 기준을 잡기 전에 **가장 곧게 편
        # 포스처를 한 번 붙인다.** 몸은 애니 모드 그대로라 골격이 안 변하고,
        # 손만 펴진 상태가 기준이 된다.
        neutral = NEUTRAL_POSE if NEUTRAL_POSE in postures else None
        if neutral:
            apply_hand_pose(
                bip_name=node.name, index=int(postures[neutral]["index"]),
                side=SIDE, kind=str(postures[neutral].get("kind") or "posture"),
                key=True, shelf=SHELF,
            )
            log(f"기준 자세로 {neutral} 를 붙였습니다 (가장 곧게 편 손)")
        else:
            log(f"★ 기준 포스처 '{NEUTRAL_POSE}' 를 못 찾았습니다. 지금 손 모양이 기준이 됩니다.")

        base_joints = biped_skeleton(rt, controller)
        offsets, rest = biped_rest_basis(rt, controller, base_joints, frame=frame)
        log(f"기준 고정 (관절 {len(base_joints)}개) · {SIDE} 손")

        for name, info in postures.items():
            try:
                apply_hand_pose(
                    bip_name=node.name,
                    index=int(info["index"]),
                    side=SIDE,
                    kind=str(info.get("kind") or "posture"),
                    key=True,          # 키를 남겨야 그 프레임에 자세가 고정된다
                    shelf=SHELF,
                )
                joints = base_joints
                rows = biped_frames(rt, controller, joints, frame, frame, rest=rest)
                if not rows:
                    failed.append(f"{name}: 프레임이 비었습니다")
                    continue
                bvh = BvhFile(
                    root=_bvh_tree(joints, offsets),
                    frame_time=1.0 / float(rt.frameRate),
                    frames=rows,
                )
                path = os.path.join(OUT_DIR, f"{slug(name)}.bvh")
                with open(path, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(serialize_bvh(bvh))
                written.append(f"{name} ({len(joints)}관절)")
            except Exception as exc:
                failed.append(f"{name}: {type(exc).__name__} {exc}")
    finally:
        try:
            rt.fetchMaxFile(quiet=True)
            log("장면 복원 완료 (fetchMaxFile)")
        except Exception as exc:
            log(f"★ 장면 복원 실패: {type(exc).__name__} {exc}")

    log(f"\n구운 것 {len(written)}개")
    for line in written[:3]:
        log(f"    {line}")
    if failed:
        log(f"실패 {len(failed)}개:")
        for line in failed[:12]:
            log(f"    {line}")


try:
    run()
except Exception:
    log("★ 예외로 중단:\n" + traceback.format_exc())
finally:
    flush()
    try:
        print(f"로그: {LOG_PATH}")
    except Exception:
        pass
