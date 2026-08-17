"""클립 폴더 스캔과 캐시 경로 계산.

라이브러리 폴더에는 아무것도 쓰지 않는다. github_sync 의 동기화 대상이라
캐시를 그 안에 두면 오염된다.
"""

import hashlib
import json
import os
from typing import NamedTuple, Optional

from maxmcp.helpers.github_sync import DEFAULT_PREFIX

# artoke_sync 가 동기화 때 남기는 매니페스트 사본. 사이트의 분류(categories)와
# 모션별 category/sub/detail 이 들어 있어, 라이브러리가 사이트와 같은 선반으로
# 그룹핑할 수 있다. 없으면(동기화 전) 전부 미분류로 뜬다.
MANIFEST_NAME = "artoke-manifest.json"

# 사이트에 없는 로컬 클립의 분류. 같은 형태지만 손으로(또는 변환 스크립트로)
# 만드는 파일이고, 동기화가 건드리지 않는다 — 사이트에 올리지 않기로 한 클립을
# 스튜디오에서만 선반에 얹으려면 이게 필요하다. 사이트에는 아무것도 보내지 않는다.
LOCAL_SHELF_NAME = "local-shelf.json"


class Clip(NamedTuple):
    stem: str
    path: str
    tags: tuple[str, ...]
    category: Optional[str] = None
    sub: Optional[str] = None
    detail: Optional[str] = None
    # 동기화가 받아온 것이 아니라 이 PC 에만 있는 클립. 동기화 접두사가 없으면
    # 로컬이다 — 사이트에서 내려온 클립은 예외 없이 접두사를 달고 저장된다.
    # 카드에 표시하려고 들고 다닌다: 사이트에 없는 클립은 지우면 되돌릴 곳이 없다.
    local: bool = False


def extract_tags(stem: str) -> tuple[str, ...]:
    """파일명에서 태그를 뽑는다. 숫자만인 토막은 태그로 만들지 않는다."""
    parts = [p for p in stem.split("_") if p and not p.isdigit()]
    return tuple(parts) if parts else (stem,)


def _read_manifest(path: str) -> dict:
    """매니페스트 하나를 읽는다. 없거나 깨졌으면 빈 구조 — 스캔은 계속된다."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"categories": [], "by_name": {}}
    by_name = {}
    for motion in data.get("motions", []):
        if isinstance(motion, dict) and motion.get("name") and motion.get("category"):
            by_name[motion["name"]] = (
                motion["category"],
                motion.get("sub"),
                motion.get("detail"),
            )
    return {"categories": data.get("categories", []), "by_name": by_name}


def load_shelf(folder: str) -> dict:
    """사이드카 매니페스트의 분류. 사이트 것과 로컬 것을 합친다.

    분류표(categories)는 사이트 것이 정본이다 — 로컬 것은 사이트 동기화를 아직
    안 한 폴더(예: 로컬 전용 클립만 있는 폴더)에서만 쓰인다. 클립별 배정은 로컬이
    이긴다: 사이트에 있는 클립을 로컬에서 다른 선반에 두고 싶을 수 있고, 그건
    로컬 파일을 고친 사람의 의도가 더 최근이다.
    """
    site = _read_manifest(os.path.join(folder, MANIFEST_NAME))
    local = _read_manifest(os.path.join(folder, LOCAL_SHELF_NAME))
    return {
        "categories": site["categories"] or local["categories"],
        "by_name": {**site["by_name"], **local["by_name"]},
    }


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
        # 동기화본은 <prefix><매니페스트 이름> 으로 저장된다 — 접두사를 벗겨 찾는다.
        # 로컬 클립은 접두사가 없으므로 파일명 그대로가 키다.
        synced = name.startswith(DEFAULT_PREFIX)
        key = name[len(DEFAULT_PREFIX):] if synced else name
        category, sub, detail = shelf["by_name"].get(key, (None, None, None))
        clips.append(
            Clip(
                stem=stem,
                path=os.path.join(folder, name),
                tags=extract_tags(stem),
                category=category,
                sub=sub,
                detail=detail,
                local=not synced,
            )
        )
    return clips


def delete_clip(folder: str, path: str) -> dict:
    """클립을 라이브러리 폴더에서 지운다. 변환 산출물(``*_biped.bvh``)도 같이.

    사이트에는 어떤 요청도 보내지 않는다 — 로컬 폴더만 정리한다. artoke
    동기화본을 지우면 다음 동기화 때 다시 받아진다 (원본은 사이트에 그대로).
    """
    folder_abs = os.path.abspath(folder)
    target = os.path.abspath(path)
    if os.path.commonpath([folder_abs, target]) != folder_abs:
        raise ValueError(f"라이브러리 폴더 밖은 지우지 않습니다: {path}")
    if not target.lower().endswith(".bvh"):
        raise ValueError(f".bvh 만 지울 수 있습니다: {path}")
    if not os.path.isfile(target):
        raise FileNotFoundError(f"이미 없습니다: {path}")

    removed = [os.path.basename(target)]
    os.remove(target)
    sibling = target[: -len(".bvh")] + "_biped.bvh"
    if os.path.isfile(sibling):
        os.remove(sibling)
        removed.append(os.path.basename(sibling))
    return {"removed": removed}


def cache_path(clip_path: str, cache_dir: str) -> str:
    """클립 절대 경로 해시로 캐시 파일 경로를 만든다."""
    digest = hashlib.sha1(
        os.path.abspath(clip_path).lower().encode("utf-8")
    ).hexdigest()[:16]
    return os.path.join(cache_dir, f"{digest}.json")
