"""클립 폴더 스캔과 캐시 경로 계산.

라이브러리 폴더에는 아무것도 쓰지 않는다. github_sync 의 동기화 대상이라
캐시를 그 안에 두면 오염된다.
"""

import hashlib
import json
import os
from typing import NamedTuple, Optional

from src.helpers.github_sync import DEFAULT_PREFIX

# artoke_sync 가 동기화 때 남기는 매니페스트 사본. 사이트의 분류(categories)와
# 모션별 category/sub 가 들어 있어, 라이브러리가 사이트와 같은 선반으로
# 그룹핑할 수 있다. 없으면(동기화 전) 전부 미분류로 뜬다.
MANIFEST_NAME = "artoke-manifest.json"


class Clip(NamedTuple):
    stem: str
    path: str
    tags: tuple[str, ...]
    category: Optional[str] = None
    sub: Optional[str] = None


def extract_tags(stem: str) -> tuple[str, ...]:
    """파일명에서 태그를 뽑는다. 숫자만인 토막은 태그로 만들지 않는다."""
    parts = [p for p in stem.split("_") if p and not p.isdigit()]
    return tuple(parts) if parts else (stem,)


def load_shelf(folder: str) -> dict:
    """사이드카 매니페스트의 분류. 없거나 깨졌으면 빈 구조 — 스캔은 계속된다."""
    try:
        with open(os.path.join(folder, MANIFEST_NAME), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"categories": [], "by_name": {}}
    by_name = {}
    for motion in data.get("motions", []):
        if isinstance(motion, dict) and motion.get("name") and motion.get("category"):
            by_name[motion["name"]] = (motion["category"], motion.get("sub"))
    return {"categories": data.get("categories", []), "by_name": by_name}


def scan(folder: str) -> list[Clip]:
    """폴더의 .bvh 를 스캔한다. ``*_biped.bvh`` 는 변환 산출물이라 제외한다."""
    if not os.path.isdir(folder):
        return []
    shelf = load_shelf(folder)
    clips: list[Clip] = []
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith(".bvh"):
            continue
        stem = name[: -len(".bvh")]
        if stem.lower().endswith("_biped"):
            continue
        # 동기화본은 <prefix><매니페스트 이름> 으로 저장된다 — 접두사를 벗겨 찾는다
        key = name[len(DEFAULT_PREFIX):] if name.startswith(DEFAULT_PREFIX) else name
        category, sub = shelf["by_name"].get(key, (None, None))
        clips.append(
            Clip(
                stem=stem,
                path=os.path.join(folder, name),
                tags=extract_tags(stem),
                category=category,
                sub=sub,
            )
        )
    return clips


def cache_path(clip_path: str, cache_dir: str) -> str:
    """클립 절대 경로 해시로 캐시 파일 경로를 만든다."""
    digest = hashlib.sha1(
        os.path.abspath(clip_path).lower().encode("utf-8")
    ).hexdigest()[:16]
    return os.path.join(cache_dir, f"{digest}.json")
