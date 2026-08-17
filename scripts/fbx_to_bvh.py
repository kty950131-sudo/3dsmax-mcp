"""3ds Max Biped FBX 묶음을 BVH 로 일괄 변환한다 (Blender 헤드리스).

    blender --background --python scripts/fbx_to_bvh.py -- --src <FBX폴더> --out <BVH폴더>

FBX 그대로 내보내면 스튜디오에서 못 읽는다. 이유가 셋이라 셋 다 여기서 처리한다.

1. **본 이름에 공백이 있다.** Max Biped 는 ``Bip001 L Thigh`` 처럼 띄어쓴 이름을
   쓰는데, BVH 파서는 계층부를 ``split()`` 으로 토큰화해 ``JOINT`` 다음 토큰
   하나를 이름으로 읽는다(``maxmcp/helpers/bvh.py``). 그대로 두면 이름이
   ``Bip001`` 로 잘리고 남은 토큰이 구조를 망가뜨린다. 그래서 ``RENAME`` 으로
   공백 없는 표준 이름으로 바꾼다 — 겸사겸사 Character Studio 가 아는 어휘라
   ``biped.loadMocapFile`` 쪽 승산도 올라간다.
2. **씬 프레임 범위가 실제 길이가 아니다.** 이 묶음은 씬이 1..250 인데 액션은
   1..46 이다. 씬 범위로 뽑으면 뒤 200 프레임이 정지 화면으로 붙는다. 액션의
   ``frame_range`` 를 쓴다.
3. **본이 200 개 가까이 된다.** 손가락 30 개, 무기 프롭, ``Skn_*`` 보정본, 얼굴,
   머리카락까지 들어 있다. 동작 타이밍을 보는 데는 쓰이지 않고 파일만 50 배로
   불린다. 기본값은 몸통 21 개만 남기고, ``--keep-fingers`` 로 손가락을 되살릴 수
   있다.

두 번째 아마추어(``Bip002``)가 들어 있는 파일이 있다 — 상대역이다. 활성 아마추어
하나만 내보내므로 자동으로 빠진다.
"""

import argparse
import os
import sys

import bpy

# Max Biped -> 공백 없는 표준 이름. 여기 없는 본은 내보내기 전에 지운다.
RENAME = {
    "Bip001": "Hips",
    "Bip001 Pelvis": "Pelvis",
    "Bip001 Spine": "Chest",
    "Bip001 Spine1": "Chest2",
    "Bip001 Spine2": "Chest3",
    "Bip001 Neck": "Neck",
    "Bip001 Head": "Head",
    "Bip001 L Clavicle": "LeftCollar",
    "Bip001 L UpperArm": "LeftUpArm",
    "Bip001 L Forearm": "LeftLowArm",
    "Bip001 L Hand": "LeftHand",
    "Bip001 L Thigh": "LeftUpLeg",
    "Bip001 L Calf": "LeftLowLeg",
    "Bip001 L Foot": "LeftFoot",
    "Bip001 L Toe0": "LeftToe",
    "Bip001 R Clavicle": "RightCollar",
    "Bip001 R UpperArm": "RightUpArm",
    "Bip001 R Forearm": "RightLowArm",
    "Bip001 R Hand": "RightHand",
    "Bip001 R Thigh": "RightUpLeg",
    "Bip001 R Calf": "RightLowLeg",
    "Bip001 R Foot": "RightFoot",
    "Bip001 R Toe0": "RightToe",
}

FINGERS = {
    f"Bip001 {side} Finger{digit}{joint}": f"{'Left' if side == 'L' else 'Right'}Finger{digit}{joint}"
    for side in ("L", "R")
    for digit in range(5)
    for joint in ("", "1", "2")
}


def slugify(stem: str, drop_prefix: str) -> str:
    """``Ellen_Attack_Branch_01_End`` -> ``attack-branch-01-end``.

    접두사(캐릭터 이름)는 폴더 전체가 같은 캐릭터라 파일마다 반복할 이유가 없다.
    그 밖에는 대소문자와 구분자만 정리한다 — ``_End``/``_Short``/``_Near``/``_Back``
    은 실제로 다른 클립이라 줄이면 서로 구별이 안 된다.
    """
    if drop_prefix and stem.lower().startswith(drop_prefix.lower()):
        stem = stem[len(drop_prefix):]
    return stem.strip("_").replace("_", "-").lower()


def pick_armature():
    """Bip001 체인을 들고 있는 아마추어. 없으면 None (모션이 아닌 파일)."""
    for obj in bpy.data.objects:
        if obj.type == "ARMATURE" and any(b.name in RENAME for b in obj.data.bones):
            return obj
    return None


def action_range(arm) -> tuple[int, int]:
    """액션의 실제 길이. 액션이 없으면 씬 범위로 물러선다."""
    anim = arm.animation_data
    if anim and anim.action:
        start, end = anim.action.frame_range
        return int(round(start)), int(round(end))
    scene = bpy.context.scene
    return scene.frame_start, scene.frame_end


def strip_and_rename(arm, keep: dict) -> int:
    """``keep`` 에 없는 본을 지우고 나머지를 새 이름으로 바꾼다. 남은 본 수를 돌려준다.

    Blender 는 본 이름을 바꾸면 애니메이션 커브 경로도 같이 옮겨 주므로 순서는
    지우기 -> 이름 바꾸기 어느 쪽이어도 되지만, 먼저 지워야 이름 충돌이 없다.
    """
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    for bone in [b for b in arm.data.edit_bones if b.name not in keep]:
        arm.data.edit_bones.remove(bone)
    bpy.ops.object.mode_set(mode="OBJECT")
    for bone in arm.data.bones:
        bone.name = keep[bone.name]
    return len(arm.data.bones)


def convert(src: str, dst: str, keep: dict) -> dict:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=src)

    arm = pick_armature()
    if arm is None:
        return {"skipped": "Bip001 체인이 없습니다"}

    start, end = action_range(arm)
    bones = strip_and_rename(arm, keep)

    bpy.ops.object.select_all(action="DESELECT")
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.export_anim.bvh(
        filepath=dst,
        frame_start=start,
        frame_end=end,
        rotate_mode="ZYX",  # maxmcp/helpers/quat.py 가 ZYX 만 안다
        root_transform_only=False,
    )
    return {"frames": end - start + 1, "bones": bones, "fps": bpy.context.scene.render.fps}


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--drop-prefix", default="Ellen_")
    ap.add_argument("--keep-fingers", action="store_true")
    args = ap.parse_args(argv)

    keep = dict(RENAME)
    if args.keep_fingers:
        keep.update(FINGERS)

    os.makedirs(args.out, exist_ok=True)
    names = sorted(n for n in os.listdir(args.src) if n.lower().endswith(".fbx"))
    for index, name in enumerate(names, 1):
        stem = name[: -len(".fbx")]
        slug = slugify(stem, args.drop_prefix)
        src = os.path.join(args.src, name)
        dst = os.path.join(args.out, slug + ".bvh")
        try:
            info = convert(src, dst, keep)
        except Exception as exc:  # 한 파일이 깨져도 나머지는 계속 변환한다
            print(f"RESULT\t{index}/{len(names)}\t{stem}\tFAIL\t{exc}")
            continue
        if "skipped" in info:
            print(f"RESULT\t{index}/{len(names)}\t{stem}\tSKIP\t{info['skipped']}")
            continue
        print(
            f"RESULT\t{index}/{len(names)}\t{stem}\tOK\t{slug}.bvh"
            f"\tframes={info['frames']}\tbones={info['bones']}\tfps={info['fps']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
