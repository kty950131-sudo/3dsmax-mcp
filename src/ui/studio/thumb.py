"""클립 골격을 그려 썸네일과 프리뷰를 만든다."""

import json
import os
from typing import Any, Sequence

from src.helpers.bvh import BvhFile, parse_bvh
from src.ui.studio.library import cache_path
from src.ui.studio.skeleton import bones, bounds, fk

SAMPLE_FRAMES = 12
_CACHE_VERSION = 2

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


def key_pose_indices(
    bvh: BvhFile, count: int = SAMPLE_FRAMES, max_candidates: int = 200
) -> list[int]:
    """포즈 변화가 큰 프레임을 고른다 (최원점 표집).

    균등 간격은 걷기처럼 주기적인 동작에서 거의 같은 포즈만 뽑는다. 이미 고른
    것들과 가장 먼 포즈를 반복해 추가하면 동작의 극점이 뽑힌다.

    긴 클립에서 FK 비용이 폭발하지 않도록 후보를 ``max_candidates`` 개로 먼저
    균등 축소한다. 반환값은 재생 순서를 유지하도록 오름차순 정렬한다.
    """
    total = len(bvh.frames)
    if total <= 0:
        raise ValueError(f"frame count must be positive, got {total}")
    if total <= count:
        return list(range(total))

    candidates = _evenly(total, min(max_candidates, total))
    vectors = [pose_vector(fk(bvh, i)) for i in candidates]

    chosen = [0]
    best = [pose_distance(vectors[0], vec) for vec in vectors]
    while len(chosen) < count:
        nxt = max(range(len(vectors)), key=lambda i: best[i])
        if best[nxt] <= 0.0:
            break  # 남은 후보가 이미 고른 것과 같은 포즈다 — 중복을 채우지 않는다
        chosen.append(nxt)
        for i, vec in enumerate(vectors):
            distance = pose_distance(vectors[nxt], vec)
            if distance < best[i]:
                best[i] = distance
    return sorted(candidates[i] for i in chosen)


def build_pose_data(clip_path: str) -> dict[str, Any]:
    """클립을 파싱해 샘플 프레임의 조인트 좌표를 뽑는다 (Qt 불필요)."""
    text = open(clip_path, encoding="utf-8", errors="replace").read()
    bvh = parse_bvh(text)
    indices = key_pose_indices(bvh)
    poses = [fk(bvh, i) for i in indices]
    every = [p for pose in poses for p in pose.values()]
    return {
        "version": _CACHE_VERSION,
        "mtime": os.path.getmtime(clip_path),
        "bones": bones(bvh.root),
        "poses": [{k: list(v) for k, v in pose.items()} for pose in poses],
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
