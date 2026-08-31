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
#: 맥스에서 손본 클립을 두는 곳. 원본과 같은 이름을 써서 경로만 봐도 짝이 드러난다.
POLISHED_DIR = "polished"

#: 이 파일이 라이브러리 루트에 있으면 그 폴더의 클립은 사이트에 올리면 안 된다.
#: 출처가 있는 자료(추출본·리타게팅본)를 모아 둔 칸을 표시하는 용도다.
NO_UPLOAD_NAME = "DO-NOT-UPLOAD.txt"


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
    # polished/ 에 같은 이름의 손본 클립이 있는가. 목록에 표시하지 않으면 같은
    # 클립을 두 번 손보거나, 이미 고친 것을 원본으로 착각한다.
    polished: bool = False
    # 라이브러리 루트 바로 아래의 어느 칸에서 왔는가 (예: "kimodo-output", "zzz-raw").
    # 루트에 바로 놓인 클립은 빈 문자열이다. 여러 출처를 한 루트로 모으면서
    # 카드만 보고는 출처를 알 수 없게 되어 붙였다 — 라이선스가 출처마다 다르다.
    source: str = ""


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


def _subfolders(folder: str) -> list[str]:
    """루트 바로 아래의 폴더 이름. 정션(바로가기)도 폴더로 본다."""
    try:
        return sorted(
            name
            for name in os.listdir(folder)
            if name != POLISHED_DIR and os.path.isdir(os.path.join(folder, name))
        )
    except OSError:
        return []


def load_shelf(folder: str) -> dict:
    """사이드카 매니페스트의 분류. 사이트 것과 로컬 것을 합친다.

    분류표(categories)는 사이트 것이 정본이다 — 로컬 것은 사이트 동기화를 아직
    안 한 폴더(예: 로컬 전용 클립만 있는 폴더)에서만 쓰인다. 클립별 배정은 로컬이
    이긴다: 사이트에 있는 클립을 로컬에서 다른 선반에 두고 싶을 수 있고, 그건
    로컬 파일을 고친 사람의 의도가 더 최근이다.

    매니페스트는 루트뿐 아니라 **하위 폴더에도** 있을 수 있다. 여러 출처를 한
    루트(정션 모음)로 묶으면서 각 원본 폴더가 자기 매니페스트를 그대로 들고
    오기 때문이다 (실측: artoke-biped 는 artoke-manifest.json, zzz-remiel 은
    local-shelf.json 을 각각 갖고 있다).
    """
    roots = [folder] + [os.path.join(folder, name) for name in _subfolders(folder)]
    categories: list = []
    by_name: dict = {}
    for root in roots:
        site = _read_manifest(os.path.join(root, MANIFEST_NAME))
        local = _read_manifest(os.path.join(root, LOCAL_SHELF_NAME))
        categories = categories or site["categories"] or local["categories"]
        by_name.update(site["by_name"])
        by_name.update(local["by_name"])
    return {"categories": categories, "by_name": by_name}


def folder_policy(folder: str) -> dict:
    """이 라이브러리 루트를 사이트에 올려도 되는가.

    ``DO-NOT-UPLOAD.txt`` 가 루트에 있으면 올리면 안 되는 칸이다. 화면에 크게
    적어 두려고 읽는다 — 폴더 이름만으로는 두 칸이 구별되지 않는다.
    """
    marker = os.path.join(folder, NO_UPLOAD_NAME)
    if not os.path.isfile(marker):
        return {"upload": True, "note": ""}
    try:
        with open(marker, encoding="utf-8") as handle:
            note = handle.read().strip().splitlines()
    except OSError:
        note = []
    return {"upload": False, "note": note[0] if note else "업로드 금지 폴더입니다."}


def _polished_names(directory: str) -> set:
    """그 폴더의 polished/ 안에 있는 손본 클립 이름."""
    polished_dir = os.path.join(directory, POLISHED_DIR)
    if not os.path.isdir(polished_dir):
        return set()
    try:
        return {n for n in os.listdir(polished_dir) if n.lower().endswith(".bvh")}
    except OSError:
        return set()


def scan(folder: str) -> list[Clip]:
    """폴더와 그 하위의 .bvh 와 .fbx 를 모두 훑는다. ``*_biped.bvh`` 는 변환 산출물이라 제외한다.

    **하위 폴더를 따라 들어가고, 정션(바로가기)도 따라간다.** 라이브러리를 한
    루트 아래에 정션으로 모아 두는 구성(ani_ / ani_2)에서는 루트에 .bvh 가 한
    개도 없고 전부 하위에 있다. ``os.walk`` 는 기본값으로 정션을 건너뛰므로
    ``followlinks=True`` 가 필요하다 (실측: 없으면 652개가 0개로 보인다).
    """
    if not os.path.isdir(folder):
        return []
    shelf = load_shelf(folder)
    root_abs = os.path.abspath(folder)
    clips: list[Clip] = []
    seen: set = set()
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=True):
        # polished/ 는 원본의 짝이라 목록에 따로 세우지 않는다. 표시에만 쓴다.
        dirnames[:] = sorted(d for d in dirnames if d != POLISHED_DIR)
        polished = _polished_names(dirpath)
        relative = os.path.relpath(dirpath, root_abs)
        source = "" if relative == "." else relative.split(os.sep)[0]
        lowered = {n.lower() for n in filenames}
        for name in sorted(filenames):
            lower = name.lower()
            if lower.endswith(".bvh"):
                stem = name[: -len(".bvh")]
            elif lower.endswith(".fbx"):
                # FBX 도 카드로 세운다(2026-08-30). 같은 이름의 .bvh 가 곁에 있으면
                # 변환이 끝난 것이라 그 카드 하나로 충분하다 — 둘 다 세우면 같은
                # 클립이 두 장 뜬다. 열 때 fbx_import 가 곁에 .bvh 를 만든다.
                stem = name[: -len(".fbx")]
                if (stem + ".bvh").lower() in lowered:
                    continue
            else:
                continue
            if stem.lower().endswith("_biped"):
                continue
            path = os.path.join(dirpath, name)
            # 정션이 겹치면 같은 파일이 두 경로로 잡힐 수 있다. 실제 경로로 한 번만.
            real = os.path.normcase(os.path.realpath(path))
            if real in seen:
                continue
            seen.add(real)
            # 동기화본은 <prefix><매니페스트 이름> 으로 저장된다 — 접두사를 벗겨 찾는다.
            # 로컬 클립은 접두사가 없으므로 파일명 그대로가 키다.
            synced = name.startswith(DEFAULT_PREFIX)
            key = name[len(DEFAULT_PREFIX):] if synced else name
            category, sub, detail = shelf["by_name"].get(key, (None, None, None))
            clips.append(
                Clip(
                    stem=stem,
                    path=path,
                    tags=extract_tags(stem),
                    category=category,
                    sub=sub,
                    detail=detail,
                    local=not synced,
                    polished=name in polished,
                    source=source,
                )
            )
    clips.sort(key=lambda c: (c.source, c.stem))
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
    # 정션(바로가기) 안의 클립은 지우지 않는다. 경로만 보면 라이브러리 안이지만
    # 실제 파일은 원본 폴더에 있어서, 지우면 라이브러리가 아니라 **원본**이 사라진다.
    # 여러 출처를 정션으로 모아 둔 구성에서는 이것이 되돌릴 수 없는 사고가 된다.
    real_folder = os.path.normcase(os.path.realpath(folder_abs))
    real_target = os.path.normcase(os.path.realpath(target))
    if os.path.commonpath([real_folder, real_target]) != real_folder:
        raise ValueError(
            "바로가기(정션) 안의 클립이라 지우지 않습니다 — 지우면 원본이 사라집니다: "
            f"{os.path.realpath(target)}"
        )
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
