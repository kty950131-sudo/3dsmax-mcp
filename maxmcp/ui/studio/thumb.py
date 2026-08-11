"""클립 골격을 그려 썸네일과 프리뷰를 만든다."""

import json
import os
from typing import Any, Sequence

from maxmcp.helpers.bvh import BvhFile, parse_bvh
from maxmcp.ui.studio.library import cache_path
from maxmcp.ui.studio.skeleton import bones, bounds, fk

# 호버할 때 카드가 재생하는 프레임 수. 균등 간격이어야 움직임이 부드럽다.
PLAYBACK_FRAMES = 24
_CACHE_VERSION = 3

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


def build_pose_data(clip_path: str) -> dict[str, Any]:
    """클립을 파싱해 샘플 프레임의 조인트 좌표를 뽑는다 (Qt 불필요)."""
    text = open(clip_path, encoding="utf-8", errors="replace").read()
    bvh = parse_bvh(text)
    indices = _evenly(len(bvh.frames), PLAYBACK_FRAMES)
    poses = [fk(bvh, i) for i in indices]
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
