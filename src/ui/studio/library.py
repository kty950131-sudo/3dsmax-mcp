"""클립 폴더 스캔과 캐시 경로 계산.

라이브러리 폴더에는 아무것도 쓰지 않는다. github_sync 의 동기화 대상이라
캐시를 그 안에 두면 오염된다.
"""

import hashlib
import os
from typing import NamedTuple


class Clip(NamedTuple):
    stem: str
    path: str
    tags: tuple[str, ...]


def extract_tags(stem: str) -> tuple[str, ...]:
    """파일명에서 태그를 뽑는다. 숫자만인 토막은 태그로 만들지 않는다."""
    parts = [p for p in stem.split("_") if p and not p.isdigit()]
    return tuple(parts) if parts else (stem,)


def scan(folder: str) -> list[Clip]:
    """폴더의 .bvh 를 스캔한다. ``*_biped.bvh`` 는 변환 산출물이라 제외한다."""
    if not os.path.isdir(folder):
        return []
    clips: list[Clip] = []
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith(".bvh"):
            continue
        stem = name[: -len(".bvh")]
        if stem.lower().endswith("_biped"):
            continue
        clips.append(
            Clip(stem=stem, path=os.path.join(folder, name), tags=extract_tags(stem))
        )
    return clips


def cache_path(clip_path: str, cache_dir: str) -> str:
    """클립 절대 경로 해시로 캐시 파일 경로를 만든다."""
    digest = hashlib.sha1(
        os.path.abspath(clip_path).lower().encode("utf-8")
    ).hexdigest()[:16]
    return os.path.join(cache_dir, f"{digest}.json")
