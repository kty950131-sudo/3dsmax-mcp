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
    # 기본 연계 — Normal_01/02/03 이 1타/2타/3타다. 번호 뒤 두 번째 인덱스는
    # 패키지마다 있기도 없기도 하다(``attack-normal-01-01`` / ``attack-normal-01``)
    # 그래서 접미사를 요구하지 않는다. 4타까지 있는 캐릭터도 있다.
    Rule("attack-normal-01*", "attack", "combo", "hit1"),
    Rule("attack-normal-02*", "attack", "combo", "hit2"),
    Rule("attack-normal-03*", "attack", "combo", "hit3"),
    Rule("attack-normal-04*", "attack", "combo", "hit4"),
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
    # 버스트 — EX 와 Max 는 같은 기술의 상위 단계다. 이름은 attack 으로 시작하지만
    # 일반 연계가 아니라 자원을 모아 쓰는 최종기라 궁극기 선반에 둔다.
    Rule("attack-burst-ex-max*", "skill", "ultimate", "max"),
    Rule("attack-burst-ex-*", "skill", "ultimate", "ex"),
    Rule("attack-burst-*", "skill", "ultimate", "base"),
    # 특수기 — EX 는 강화판이다. 에너지를 써서 나가는 공격이라 charge 다.
    Rule("attack-exspecial*", "attack", "charge", "ex"),
    Rule("attack-special*", "attack", "charge", "base"),
    # 상태 전환 자체는 타격이 아니라 기술 발동이다.
    Rule("attack-changestate*", "skill", "cast", "state"),
    # 회피·피격·죽음
    Rule("evade-*", "evade", "step"),
    # 대시 회피는 이동 대시와 이름만 같은 계열이라 먼저 갈라낸다.
    Rule("dash-evade*", "evade", "step", "dash"),
    Rule("hit-h-*", "hit", "flinch", "heavy"),
    Rule("hit-l-*", "hit", "flinch", "light"),
    Rule("hit-shake*", "hit", "flinch", "shake"),
    Rule("hitfly-*", "hit", "launch", "up"),
    Rule("death*", "death", "fall", "front"),
    # 대기 — afk 가 idle 보다 먼저
    Rule("idle-afk*", "idle", "reaction"),
    Rule("idle*", "idle", "base"),
    # 이동
    Rule("run*", "locomotion", "run"),
    Rule("walk*", "locomotion", "walk"),
    Rule("turnback*", "locomotion", "turn"),
    # 이동 대시 — 공격 대시(``attack-dash-*``)와 다른 이동기다. 가장 빠른 이동이라
    # 질주 선반에 둔다.
    Rule("dash-start*", "locomotion", "sprint", "start"),
    Rule("dash-end*", "locomotion", "sprint", "end"),
    Rule("dash-loop*", "locomotion", "sprint", "loop"),
    # 공중 상태 이동·진입 — 발이 땅에 없는 이동이라 지상 이동과 섞지 않는다.
    Rule("move-*-air*-loop", "locomotion", "jump", "air"),
    Rule("start-airstate*", "locomotion", "jump", "start"),
    # 스킬 — 번호가 붙은 기술 슬롯. 마무리 일격은 최종기 쪽이다.
    Rule("skill[0-9]*", "skill", "cast"),
    Rule("finish-*", "skill", "ultimate", "finish"),
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


def main(argv: Optional[list] = None) -> int:
    """분류표를 사이트 매니페스트에서 베껴 와 로컬 선반을 다시 쓴다.

        python -m maxmcp.ui.studio.local_shelf --folder <BVH폴더> \
            --categories-from <artoke-manifest.json 또는 사이트 manifest.json>

    사이트 분류가 바뀌면 다시 돌린다. 분류표를 손으로 옮겨 적지 않는 것이 요점이다.
    """
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--folder", required=True)
    ap.add_argument("--categories-from", required=True)
    args = ap.parse_args(argv)

    with open(args.categories_from, encoding="utf-8") as handle:
        categories = json.load(handle).get("categories", [])
    if not categories:
        print(f"분류표가 비어 있습니다: {args.categories_from}")
        return 1

    result = write(args.folder, categories)
    print(f"{result['shelved']} 개 배정 -> {result['path']} (분류 {len(categories)} 종)")
    for name in result["unmatched"]:
        print(f"  미배정: {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
