"""클립 골격을 그려 썸네일과 프리뷰를 만든다."""

import json
import os
from typing import Any, Optional, Sequence

from maxmcp.helpers.bvh import (
    BvhFile,
    BvhJoint,
    _column_map,
    parse_bvh,
    rig_height,
    strip_static_root,
)
from maxmcp.ui.studio.library import cache_path
from maxmcp.ui.studio.skeleton import bones, bounds, fk, travel_joint

# 호버할 때 카드가 재생하는 프레임 수. 균등 간격이어야 움직임이 부드럽다.
PLAYBACK_FRAMES = 24
_CACHE_VERSION = 6  # 6: fk 위치 채널 이중 합산 수정 — 캐시된 포즈가 전부
#    깨진 fk 로 계산된 것이라 버전을 올려 다시 굽게 한다(역관절 프리뷰의 원인)

Vec3 = tuple[float, float, float]


def _evenly(total: int, count: int) -> list[int]:
    """0..total-1 에서 균등 간격으로 count 개를 고른다."""
    if total <= 0:
        raise ValueError(f"total must be positive, got {total}")
    if total <= count:
        return list(range(total))
    last = total - 1
    return [round(i * last / (count - 1)) for i in range(count)]


def pose_vector(pose: dict[str, Vec3]) -> tuple[float, ...]:
    """루트 상대 좌표를 조인트 이름 정렬 순으로 편 벡터.

    ``fk`` 는 루트를 가장 먼저 넣으므로 첫 항목을 루트로 본다. 루트를 빼야
    제자리 동작과 크게 이동하는 동작이 같은 기준으로 비교된다 — 안 빼면 이동량이
    포즈 차이를 덮어버린다.
    """
    if not pose:
        return ()
    root = next(iter(pose.values()))
    return tuple(
        pose[name][axis] - root[axis] for name in sorted(pose) for axis in range(3)
    )


def pose_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """두 포즈 벡터의 제곱 거리. 순위 비교에만 쓰므로 제곱근을 씌우지 않는다."""
    return sum((x - y) ** 2 for x, y in zip(a, b))


def poster_index(poses: Sequence[dict[str, Vec3]]) -> int:
    """정지 상태에서 보여줄 대표 프레임의 위치.

    평균 포즈에서 가장 먼 프레임을 고른다. 0번 프레임은 대개 준비 자세라 클립이
    무슨 동작인지 설명하지 못한다.

    재생 프레임 자체는 **균등 간격이어야 한다.** 포즈 차이가 큰 순서로 고르면
    정지 한 장으로는 좋지만 이어 붙이면 극단 사이를 튀어 경련처럼 보인다.
    대표성과 부드러움은 다른 요구라, 재생은 균등 간격으로 두고 대표 한 장만
    여기서 고른다.
    """
    if not poses:
        return 0
    vectors = [pose_vector(pose) for pose in poses]
    width = len(vectors[0])
    if width == 0:
        return 0
    mean = [sum(vec[i] for vec in vectors) / len(vectors) for i in range(width)]
    return max(range(len(vectors)), key=lambda i: pose_distance(vectors[i], mean))


def lock_in_place(
    poses: Sequence[dict[str, Vec3]], anchor: str | None
) -> list[dict[str, Vec3]]:
    """가로 이동을 걷어내 캐릭터를 제자리에 붙든다. **표시 좌표에만** 적용한다.

    이동하는 클립을 그대로 그리면 바운즈가 이동 경로 전체를 덮어서, 8방향 달리기처럼
    키의 대여섯 배를 가는 클립은 캐릭터가 점처럼 작아지고 카드 안을 미끄러져 간다.
    앵커의 가로 좌표를 매 프레임 빼면 크기가 몸에 맞고 동작 자체가 보인다.

    걷어내는 것은 **가로(X·Z)뿐**이다. Y 를 같이 빼면 도약과 웅크림이 사라져 점프
    클립과 걷기 클립이 구분되지 않는다.

    원본 BVH 는 건드리지 않는다 — Max 로 임포트할 때는 이동이 그대로 따라간다.
    """
    if not anchor or not poses or anchor not in poses[0]:
        return list(poses)
    locked: list[dict[str, Vec3]] = []
    for pose in poses:
        ax, _, az = pose[anchor]
        locked.append({name: (p[0] - ax, p[1], p[2] - az) for name, p in pose.items()})
    return locked


#: 사람 키. 뼈 단위가 파일마다 달라(엘렌 1.2, Kimodo 176.4) 이동량을 그대로는
#: 비교할 수 없다. 신장으로 나눠 이 값을 곱하면 두 파일의 숫자를 나란히 볼 수 있다.
HUMAN_METRES = 1.7


def travel_readout(bvh: BvhFile) -> Optional[dict[str, float]]:
    """가로 이동 거리와 속도. 키를 못 재는 파일은 None.

    세로는 빼고 잰다 — 제자리 점프는 이동이 아니다.

    미터를 지어내지 않고 None 을 주는 이유: 뼈 길이가 0 인 파일은 무엇으로도
    환산할 근거가 없다. 화면은 그때 이동 정보를 아예 안 적는다.
    """
    height = rig_height(bvh)
    if height <= 0 or len(bvh.frames) < 2:
        return None
    name = travel_joint(bvh)
    if name is None:
        return None

    columns = _column_map(bvh.root)

    def find(joint: BvhJoint) -> Optional[BvhJoint]:
        if joint.name == name:
            return joint
        for child in joint.children:
            hit = find(child)
            if hit is not None:
                return hit
        return None

    joint = find(bvh.root)
    if joint is None:
        return None
    start, _ = columns[id(joint)]
    axis = {}
    for i, channel in enumerate(joint.channels):
        low = channel.lower()
        if low in ("xposition", "zposition"):
            axis[low] = start + i
    if len(axis) < 2:
        return None

    first, last = bvh.frames[0], bvh.frames[-1]
    dx = last[axis["xposition"]] - first[axis["xposition"]]
    dz = last[axis["zposition"]] - first[axis["zposition"]]
    metres = ((dx * dx + dz * dz) ** 0.5 / height) * HUMAN_METRES
    seconds = (len(bvh.frames) - 1) * bvh.frame_time
    return {
        "metres": metres,
        "seconds": seconds,
        "metres_per_second": metres / seconds if seconds > 0 else 0.0,
    }


def build_pose_data(clip_path: str) -> dict[str, Any]:
    """클립을 파싱해 샘플 프레임의 조인트 좌표를 뽑는다 (Qt 불필요)."""
    text = open(clip_path, encoding="utf-8", errors="replace").read()
    # 정지한 래퍼 루트를 걷어낸다. kimodo/SOMA 출력의 `Root` 는 전 프레임 원점에
    # 있고 이동은 자식 `Hips` 가 들고 있어서, 그대로 그리면 원점에 박힌 Root 와
    # 멀어지는 몸 사이에 고무줄 같은 뼈가 하나 생긴다.
    bvh = strip_static_root(parse_bvh(text))
    indices = _evenly(len(bvh.frames), PLAYBACK_FRAMES)
    poses = lock_in_place([fk(bvh, i) for i in indices], travel_joint(bvh))
    every = [p for pose in poses for p in pose.values()]
    return {
        "version": _CACHE_VERSION,
        "mtime": os.path.getmtime(clip_path),
        "bones": bones(bvh.root),
        "poses": [{k: list(v) for k, v in pose.items()} for pose in poses],
        "poster": poster_index(poses),
        "bounds": list(bounds(every, 0.0)),
        "frames": len(bvh.frames),
        "frame_time": bvh.frame_time,
        "travel": travel_readout(bvh),
    }


def load_pose_data(clip_path: str, cache_dir: str) -> dict[str, Any]:
    """캐시가 유효하면 재사용하고, 아니면 다시 계산해 저장한다."""
    path = cache_path(clip_path, cache_dir)
    try:
        with open(path, encoding="utf-8") as handle:
            cached = json.load(handle)
        if (
            cached.get("version") == _CACHE_VERSION
            and cached.get("mtime") == os.path.getmtime(clip_path)
        ):
            return cached
    except (OSError, ValueError):
        pass

    data = build_pose_data(clip_path)
    os.makedirs(cache_dir, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
    except OSError:
        pass  # 캐시 실패는 치명적이지 않다
    return data
