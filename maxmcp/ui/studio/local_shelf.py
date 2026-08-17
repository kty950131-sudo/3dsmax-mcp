"""사이트에 올리지 않는 로컬 클립을 파일명 규칙으로 선반에 배정한다.

사이트에 있는 클립은 ``artoke-manifest.json`` 이 분류를 들고 온다. 사이트에
올리지 않기로 한 클립에는 그게 없어서 스튜디오에서 전부 "미분류" 한 칸에 쌓인다.
파일명이 규칙적이면 거기서 뽑아내는 편이 76 개를 손으로 배정하는 것보다 정확하다 —
손으로 하면 틀려도 아무도 모르지만, 표는 테스트가 읽는다.

분류 슬러그는 사이트의 분류표(``src/lib/viewer-models.ts``)를 따른다. 여기서 새
선반을 만들지 않는다 — 없는 슬러그를 쓰면 스튜디오 그리드가 그 클립을 조용히
미분류로 떨어뜨린다.
"""

import fnmatch
import json
import os
from typing import Iterable, NamedTuple, Optional


class Rule(NamedTuple):
    """``pattern`` 에 맞으면 이 선반. 목록 순서대로 보고 처음 맞는 것이 이긴다."""

    pattern: str
    category: str
    sub: Optional[str] = None
    detail: Optional[str] = None


# 액션 게임 모션 패키지 하나의 이름 규칙. ``<역할>_<종류>_<번호>_<변형>`` 꼴이고,
# 변형(``_End`` 후딜, ``_Short`` 축약, ``_Near`` 근거리, ``_Back`` 후방)은 같은
# 역할의 다른 클립이라 같은 선반에 둔다.
#
# 순서가 중요하다. ``*-end`` 는 더 넓은 규칙보다 먼저 와야 하고 (``dash-cut-01-end``
# 는 종료지 지속이 아니다), ``idle-afk`` 는 ``idle`` 보다 먼저 와야 한다.
COMBAT_PACKAGE_RULES: tuple[Rule, ...] = (
    # 기본 연계 — Normal_01/02/03 이 1타/2타/3타다
    Rule("attack-normal-01-*", "attack", "combo", "hit1"),
    Rule("attack-normal-02-*", "attack", "combo", "hit2"),
    Rule("attack-normal-03-*", "attack", "combo", "hit3"),
    # 분기 연계
    Rule("attack-branch-01*", "attack", "branch", "b1"),
    Rule("attack-branch-02*", "attack", "branch", "b2"),
    Rule("attack-branch-03*", "attack", "branch", "b3"),
    Rule("attack-branch-04*", "attack", "branch", "b4"),
    # 돌진 — 종료를 먼저 걸러야 cut/slash 가 종료를 삼키지 않는다
    Rule("attack-dash-start-*", "attack", "dash", "start"),
    Rule("attack-dash-end-*", "attack", "dash", "end"),
    Rule("attack-dash-*-end", "attack", "dash", "end"),
    Rule("attack-dash-loop-*", "attack", "dash", "loop"),
    Rule("attack-dash-*", "attack", "dash", "loop"),
    # 반격 — ParryAid 는 패링, Counter 는 회피 반격
    Rule("attack-parryaid-*", "attack", "counter", "parry"),
    Rule("attack-counter*", "attack", "counter", "evade"),
    # 지원 — AssaultAid 는 교대 지원, Rush 는 협공
    Rule("attack-assaultaid*", "attack", "assist", "switch"),
    Rule("attack-rush*", "attack", "assist", "team"),
    # 회피·피격·죽음
    Rule("evade-*", "evade", "step"),
    Rule("hit-h-*", "hit", "flinch", "heavy"),
    Rule("hit-l-*", "hit", "flinch", "light"),
    Rule("hitfly-*", "hit", "launch", "up"),
    Rule("death*", "death", "fall", "front"),
    # 대기 — afk 가 idle 보다 먼저
    Rule("idle-afk*", "idle", "reaction"),
    Rule("idle*", "idle", "base"),
    # 이동
    Rule("run*", "locomotion", "run"),
    Rule("walk*", "locomotion", "walk"),
    Rule("turnback*", "locomotion", "turn"),
    # 교대
    Rule("switchin-attack*", "switch", "in", "assist"),
    Rule("switchin-*", "switch", "in", "normal"),
    Rule("switchout-*", "switch", "out", "normal"),
    # 연출
    Rule("levelswitch*", "cutscene", "transition", "level"),
    Rule("queststart*", "cutscene", "enter", "quest"),
)


def assign(slug: str, rules: Iterable[Rule] = COMBAT_PACKAGE_RULES) -> Optional[Rule]:
    """슬러그에 맞는 첫 규칙. 맞는 게 없으면 None — 미분류로 남긴다.

    억지로 아무 선반에나 넣지 않는다. 규칙이 커버하지 못하는 클립은 미분류로
    보이는 편이 낫다 — 잘못된 선반에 들어가면 아무도 못 찾는다.
    """
    for rule in rules:
        if fnmatch.fnmatchcase(slug, rule.pattern):
            return rule
    return None


def build(
    folder: str,
    categories: list,
    rules: Iterable[Rule] = COMBAT_PACKAGE_RULES,
) -> dict:
    """폴더의 .bvh 를 훑어 ``local-shelf.json`` 내용을 만든다.

    ``categories`` 는 사이트 매니페스트에서 그대로 복사해 넣는다. 여기서 새로
    쓰지 않는 것은 스튜디오와 사이트의 선반 이름이 갈리지 않게 하기 위해서다.
    """
    rules = tuple(rules)
    motions, unmatched = [], []
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith(".bvh"):
            continue
        rule = assign(name[: -len(".bvh")], rules)
        if rule is None:
            unmatched.append(name)
            continue
        entry = {"name": name, "category": rule.category}
        if rule.sub:
            entry["sub"] = rule.sub
        if rule.detail:
            entry["detail"] = rule.detail
        motions.append(entry)
    return {"categories": categories, "motions": motions, "unmatched": unmatched}


def write(folder: str, categories: list, rules: Iterable[Rule] = COMBAT_PACKAGE_RULES) -> dict:
    """``local-shelf.json`` 을 폴더에 쓴다. 미배정 목록을 돌려준다."""
    shelf = build(folder, categories, rules)
    unmatched = shelf.pop("unmatched")
    path = os.path.join(folder, "local-shelf.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(shelf, handle, ensure_ascii=False, indent=2)
    return {"path": path, "shelved": len(shelf["motions"]), "unmatched": unmatched}
