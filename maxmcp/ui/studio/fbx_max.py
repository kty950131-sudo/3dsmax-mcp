"""Max 안에서 FBX 를 BVH 로 바꾼다 — Blender 가 못 읽는 애니를 살린다.

Blender 의 FBX 임포터는 **Max Biped 에서 구워 내보낸 FBX 의 애니를 잃는다**
(실측 2026-08-30: Female Start Walking 의 fcurve 609개가 전부 상수, 반면 Max 로
같은 파일을 임포트하면 38/39 노드가 회전한다). 스튜디오는 Max 안에서 도니, 변환도
Max 에서 한다 — importFile 로 들여와 노드 트리를 샘플하고 BVH 로 쓴다.

좌표 변환은 biped_export 의 헬퍼를 그대로 쓴다. 그 헬퍼들은 Max Z-up 을 BVH Y-up
으로 옮기므로(`_max_to_bvh_xyz`, `_to_bvh_rotation`) 결과가 곧바로 똑바로 선다 —
별도 축 보정이 필요 없다.

이 모듈은 pymxs 가 있어야(=Max 안) 돈다. 밖에서는 fbx_import 가 Blender 로 물러난다.
"""

from __future__ import annotations

import os
from typing import Optional

from maxmcp.helpers.bvh import BvhFile, merge_into_parent, parse_bvh, serialize_bvh
from maxmcp.ui.studio.biped_export import (
    _bvh_tree,
    _euler_zyx,
    _mat_mul,
    _max_to_bvh_xyz,
    _to_bvh_rotation,
    _transpose,
    _world_rotation,
    _xyz,
)

# Max Biped 본 이름 -> 공백 없는 Character Studio 이름. scripts/fbx_to_bvh.py 와
# 같은 표를 쓴다 — 한 곳만 고치고 다른 곳을 잊는 일이 없게 여기서 가져온다.
try:
    import importlib.util as _ilu

    _spec = _ilu.spec_from_file_location(
        "_fbx_to_bvh",
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))), "scripts", "fbx_to_bvh.py"),
    )
    _mod = _ilu.module_from_spec(_spec)
    # bpy import 는 실패하므로 RENAME 만 뽑아 온다: 파일을 읽어 그 dict 만 평가한다.
    RENAME: dict = {}
    with open(_spec.origin, encoding="utf-8") as _h:
        _src = _h.read()
    _start = _src.index("RENAME = {")
    _end = _src.index("}", _start) + 1
    RENAME = eval(_src[_start + len("RENAME = "):_end])  # noqa: S307 - 우리 저장소의 리터럴
except Exception:  # pragma: no cover - 방어
    RENAME = {}


def available() -> bool:
    try:
        import pymxs  # noqa: F401
        return True
    except Exception:
        return False


def _fresh_nodes(rt, before_handles):
    return [n for n in rt.objects if n.handle not in before_handles]


def _pick_root(fresh):
    """Bip001 트리의 꼭대기. Max 에서 내보낸 Biped FBX 는 골반이 루트다."""
    named = {n.name: n for n in fresh}
    for candidate in ("Bip001 Pelvis", "Bip001"):
        if candidate in named:
            return named[candidate]
    # 이름을 못 맞추면, RENAME 에 있는 본 중 조상이 가장 적은 것.
    known = [n for n in fresh if n.name in RENAME]
    if not known:
        return None
    def depth(node):
        d, p = 0, node.parent
        while p is not None:
            d += 1
            p = p.parent
        return d
    return min(known, key=depth)


def _build_joints(root, keep: dict):
    """(이름, 부모이름) 목록을 CS 이름으로. keep 에 없는 본은 건너뛴다.

    부모가 keep 에서 빠졌으면 그 위로 올라가 가장 가까운 남은 조상에 잇는다 —
    사슬 중간이 비어도 트리가 끊기지 않게.
    """
    joints = []
    nodes = {}

    def nearest_kept_ancestor(node):
        p = node.parent
        while p is not None:
            if p.name in keep:
                return keep[p.name]
            p = p.parent
        return None

    def walk(node):
        if node.name in keep:
            cs = keep[node.name]
            joints.append((cs, nearest_kept_ancestor(node)))
            nodes[cs] = node
        for child in node.children:
            walk(child)

    walk(root)
    return joints, nodes


def convert(fbx_path: str, dst: str) -> dict:
    """FBX 를 Max 로 들여와 샘플해 BVH 로 쓴다. 들여온 노드는 지운다."""
    import pymxs

    rt = pymxs.runtime
    keep = dict(RENAME)

    range_before = rt.animationRange
    before = {n.handle for n in rt.objects}
    ok = False
    try:
        ok = rt.importFile(fbx_path, rt.Name("noPrompt"), using=rt.FBXIMP)
    except Exception as exc:
        return {"error": f"importFile 실패: {exc}"}
    fresh = _fresh_nodes(rt, before)
    try:
        if not ok or not fresh:
            return {"error": "FBX 임포트가 노드를 만들지 않았습니다"}
        root = _pick_root(fresh)
        if root is None:
            return {"error": "Bip001 체인을 찾지 못했습니다"}

        joints, nodes = _build_joints(root, keep)
        if len(joints) < 3:
            return {"error": "쓸 만한 본이 부족합니다"}

        ordered = [name for name, _ in joints]
        start = int(rt.animationRange.start.frame)
        end = int(rt.animationRange.end.frame)

        original_time = rt.sliderTime
        # rest(휴식) 기준을 첫 프레임에서 잡는다.
        rt.sliderTime = start
        rest_rot = {name: _world_rotation(nodes[name].transform) for name in ordered}
        rest_pos = {name: _xyz(nodes[name].transform.translation) for name in ordered}

        parent_of = dict(joints)
        offsets = {}
        for name in ordered:
            parent = parent_of[name]
            if parent is None:
                offsets[name] = (0.0, 0.0, 0.0)
            else:
                here, up = rest_pos[name], rest_pos[parent]
                offsets[name] = _max_to_bvh_xyz((here[0] - up[0], here[1] - up[1], here[2] - up[2]))

        rows = []
        for frame in range(start, end + 1):
            rt.sliderTime = frame
            delta = {
                name: _mat_mul(_world_rotation(nodes[name].transform), _transpose(rest_rot[name]))
                for name in ordered
            }
            row = []
            for name in ordered:
                parent = parent_of[name]
                if parent is None:
                    row.extend(_max_to_bvh_xyz(nodes[name].transform.translation))
                    local = delta[name]
                else:
                    local = _mat_mul(_transpose(delta[parent]), delta[name])
                z, y, x = _euler_zyx(_to_bvh_rotation(local))
                row.extend([z, y, x])
            rows.append(row)
        rt.sliderTime = original_time

        tree = BvhFile(root=_bvh_tree(joints, offsets), frame_time=1.0 / float(rt.frameRate), frames=rows)
        # 골반이 루트면 Character Studio 가 아는 Hips 로 이름만 바꾼다.
        if tree.root.name == "Pelvis":
            tree.root.name = "Hips"
        elif any(node.name == "Pelvis" for node in _iter(tree.root)):
            tree = merge_into_parent(tree, "Pelvis")

        with open(dst, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialize_bvh(tree))
        return {"frames": end - start + 1, "bones": len(ordered)}
    finally:
        try:
            rt.delete(fresh)
        except Exception:
            for node in fresh:
                try:
                    rt.delete(node)
                except Exception:
                    pass
        rt.animationRange = range_before


def _iter(joint):
    yield joint
    for child in joint.children:
        yield from _iter(child)
