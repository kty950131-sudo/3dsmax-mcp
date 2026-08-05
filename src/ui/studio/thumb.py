"""클립 골격을 그려 썸네일과 프리뷰를 만든다."""

import json
import os
from typing import Any

from src.helpers.bvh import parse_bvh
from src.ui.studio.library import cache_path
from src.ui.studio.skeleton import bones, bounds, fk

SAMPLE_FRAMES = 12
_CACHE_VERSION = 1


def sample_indices(total: int, count: int = SAMPLE_FRAMES) -> list[int]:
    """클립 전체에서 균등 간격으로 프레임 인덱스를 고른다."""
    if total <= 0:
        raise ValueError(f"total must be positive, got {total}")
    if total <= count:
        return list(range(total))
    last = total - 1
    return [round(i * last / (count - 1)) for i in range(count)]


def build_pose_data(clip_path: str) -> dict[str, Any]:
    """클립을 파싱해 샘플 프레임의 조인트 좌표를 뽑는다 (Qt 불필요)."""
    text = open(clip_path, encoding="utf-8", errors="replace").read()
    bvh = parse_bvh(text)
    indices = sample_indices(len(bvh.frames))
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
