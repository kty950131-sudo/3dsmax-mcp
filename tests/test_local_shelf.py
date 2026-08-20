"""로컬 클립 선반 배정.

실제 모션 패키지의 파일 이름 전부를 픽스처로 쓴다. 규칙 하나를 잘못 놓으면
클립이 엉뚱한 선반으로 조용히 들어가는데, 76 개를 눈으로 확인하는 사람은 없다.
"""

import json

import pytest

from maxmcp.ui.studio.local_shelf import COMBAT_PACKAGE_RULES, assign, build, write

# 변환된 BVH 슬러그 전부. 캐릭터 접두사를 떼고 소문자 하이픈으로 바꾼 이름이다.
PACKAGE = """
attack-assaultaid attack-assaultaid-back attack-assaultaid-end
attack-assaultaid-near attack-assaultaid-near-back
attack-branch-01 attack-branch-01-end attack-branch-01-short
attack-branch-02 attack-branch-02-end attack-branch-02-short
attack-branch-03 attack-branch-03-end attack-branch-04 attack-branch-04-end
attack-counter attack-counter-end
attack-dash-cut-01 attack-dash-cut-01-end attack-dash-end-01
attack-dash-loop-01 attack-dash-slash-01 attack-dash-start-01 attack-dash-start-02
attack-normal-01-01 attack-normal-01-01-end attack-normal-01-02 attack-normal-01-02-end
attack-normal-02-01 attack-normal-02-01-end attack-normal-02-02
attack-normal-03-01 attack-normal-03-01-back attack-normal-03-01-back-near
attack-normal-03-01-end attack-normal-03-01-near
attack-normal-03-02 attack-normal-03-02-back attack-normal-03-02-end
attack-normal-03-02-near attack-normal-03-02-near-back
attack-parryaid-h attack-parryaid-h-end attack-parryaid-l attack-parryaid-l-end
attack-parryaid-start attack-rush attack-rush-end
death evade-back evade-front
hit-h-back hit-h-front hit-l-back hit-l-front hitfly-back hitfly-front
idle idle-afk levelswitch queststart run run-end skin
switchin-attack switchin-attack-end switchin-attack-ex switchin-attack-ex-end
switchin-attack-ex-start switchin-normal switchout-normal
turnback walk walk-end walk-start walk-start-end
""".split()


# 두 번째 패키지(다른 캐릭터). 같은 게임이라 이름 규칙은 같은데 기술 구성이 달라
# 이 패키지에만 있는 계열이 있다 — 버스트, 번호 붙은 스킬, 공중 상태 이동, 이동
# 대시. 한 패키지만 픽스처로 두면 규칙을 고치다 다른 쪽을 조용히 깬다.
PACKAGE_2 = """
attack-assaultaid-end attack-assaultaid attack-burst-01-end attack-burst-01
attack-burst-ex-01-end attack-burst-ex-01 attack-burst-ex-02-end
attack-burst-ex-02 attack-burst-ex-max01 attack-burst-ex-max02-end
attack-burst-ex-max02 attack-changestate-02 attack-changestate
attack-counter-end attack-counter attack-exspecial-end attack-exspecial
attack-normal-01-end attack-normal-01 attack-normal-02-end attack-normal-02
attack-normal-03-end attack-normal-03 attack-normal-04-end attack-normal-04
attack-parryaid-h-end attack-parryaid-h attack-parryaid-l-end
attack-parryaid-l attack-parryaid-start attack-rush-end attack-rush
attack-special-end attack-special dash-end dash-evade dash-loop-02 dash-loop
dash-start death evade-back-02 evade-back evade-front-02 evade-front
evade-to-run-01 finish-airstate-front finish-ex-airstate-end
finish-ex-airstate-start finish-ex-airstate hit-h-back hit-h-front hit-l-back
hit-l-front hit-shake hitfly-back hitfly-front idle-afk
idle-airstate-back-loop idle-airstate-front-loop idle-loop move-b-airback-loop
move-b-airfront-loop move-f-airback-loop move-f-airfront-loop
move-l-airback-loop move-l-airfront-loop move-r-airback-loop
move-r-airfront-loop run-end run-loop-01 run-loop-02 run-transform-01
run-transform-02 skill01-airstate-back skill01-airstate-front
skill02-airstate-back skill02-airstate-front skill03-airstate-back
skill03-airstate-front skill04-airstate-front skill05-airstate-back
start-airstate-back switchin-attack-end switchin-attack-ex-extra-end
switchin-attack-ex-extra switchin-attack-ex-start switchin-attack-ex
switchin-attack switchin-normal switchout-airstate-back switchout-normal
walk-end walk-loop walk-start-end walk-start walk-to-run-01
""".split()


def shelf_of(slug: str) -> tuple:
    rule = assign(slug)
    return None if rule is None else (rule.category, rule.sub, rule.detail)


@pytest.mark.parametrize(
    "slug,expected",
    [
        # 연계는 Normal_01/02/03 이 1타/2타/3타다. 후딜(-end)과 변형(-near/-back)은
        # 같은 타수의 다른 클립이라 같은 선반에 남아야 한다.
        ("attack-normal-01-01", ("attack", "combo", "hit1")),
        ("attack-normal-03-02-near-back", ("attack", "combo", "hit3")),
        # 돌진은 시작/지속/종료가 갈려야 의미가 있다. cut 은 종료 여부로 갈린다.
        ("attack-dash-start-01", ("attack", "dash", "start")),
        ("attack-dash-loop-01", ("attack", "dash", "loop")),
        ("attack-dash-end-01", ("attack", "dash", "end")),
        ("attack-dash-cut-01", ("attack", "dash", "loop")),
        ("attack-dash-cut-01-end", ("attack", "dash", "end")),
        ("attack-dash-slash-01", ("attack", "dash", "loop")),
        # 반격 두 갈래
        ("attack-parryaid-start", ("attack", "counter", "parry")),
        ("attack-counter-end", ("attack", "counter", "evade")),
        # 지원 두 갈래
        ("attack-assaultaid-near-back", ("attack", "assist", "switch")),
        ("attack-rush", ("attack", "assist", "team")),
        # 피격 — hitfly 가 hit-h 규칙에 걸리면 날아감이 경직으로 분류된다
        ("hit-h-front", ("hit", "flinch", "heavy")),
        ("hit-l-back", ("hit", "flinch", "light")),
        ("hitfly-front", ("hit", "launch", "up")),
        # 대기 — afk 가 idle 보다 먼저 걸려야 한다
        ("idle", ("idle", "base", None)),
        ("idle-afk", ("idle", "reaction", None)),
        # 교대 — 지원 등장과 일반 등장
        ("switchin-attack-ex-start", ("switch", "in", "assist")),
        ("switchin-normal", ("switch", "in", "normal")),
        ("switchout-normal", ("switch", "out", "normal")),
        # 이동·연출
        ("walk-start-end", ("locomotion", "walk", None)),
        ("run-end", ("locomotion", "run", None)),
        ("turnback", ("locomotion", "turn", None)),
        ("levelswitch", ("cutscene", "transition", "level")),
        ("queststart", ("cutscene", "enter", "quest")),
    ],
)
def test_assigns_the_expected_shelf(slug, expected) -> None:
    assert shelf_of(slug) == expected


def test_every_motion_in_the_package_lands_somewhere() -> None:
    # skin 은 모션이 아니라 메시다 — 유일하게 미배정으로 남아야 한다.
    unmatched = [slug for slug in PACKAGE if assign(slug) is None]
    assert unmatched == ["skin"]


def test_every_motion_in_the_second_package_lands_somewhere() -> None:
    # 이쪽은 메시 파일이 없다 — 96 개 전부 선반에 올라가야 한다.
    unmatched = [slug for slug in PACKAGE_2 if assign(slug) is None]
    assert unmatched == []


@pytest.mark.parametrize(
    "slug,expected",
    [
        # 두 번째 인덱스가 없는 연계. 4 타까지 있다.
        ("attack-normal-01", ("attack", "combo", "hit1")),
        ("attack-normal-04-end", ("attack", "combo", "hit4")),
        # 버스트는 최종기다. EX 와 Max 는 같은 기술의 상위 단계라 갈라만 둔다.
        ("attack-burst-01", ("skill", "ultimate", "base")),
        ("attack-burst-ex-01-end", ("skill", "ultimate", "ex")),
        ("attack-burst-ex-max02", ("skill", "ultimate", "max")),
        # 특수기와 강화특수기
        ("attack-special", ("attack", "charge", "base")),
        ("attack-exspecial-end", ("attack", "charge", "ex")),
        # 이동 대시 — 공격 대시와 다른 계열이고, 회피 파생만 따로 간다.
        ("dash-start", ("locomotion", "sprint", "start")),
        ("dash-loop-02", ("locomotion", "sprint", "loop")),
        ("dash-evade", ("evade", "step", "dash")),
        # 공중 상태
        ("move-b-airback-loop", ("locomotion", "jump", "air")),
        ("start-airstate-back", ("locomotion", "jump", "start")),
        # 번호 붙은 스킬과 마무리 일격
        ("skill03-airstate-front", ("skill", "cast", None)),
        ("finish-ex-airstate-start", ("skill", "ultimate", "finish")),
        ("attack-changestate-02", ("skill", "cast", "state")),
        ("hit-shake", ("hit", "flinch", "shake")),
    ],
)
def test_assigns_the_expected_shelf_in_the_second_package(slug, expected) -> None:
    assert shelf_of(slug) == expected


def test_unknown_names_stay_unshelved() -> None:
    # 억지로 아무 선반에나 넣으면 잘못된 칸에 들어가 아무도 못 찾는다.
    assert assign("something-nobody-planned-for") is None


def test_rules_use_only_slugs_the_site_defines() -> None:
    # 사이트에 없는 슬러그를 쓰면 그리드가 그 클립을 조용히 미분류로 떨어뜨린다.
    categories = {
        "idle": {"base", "reaction"},
        "locomotion": {"walk", "jog", "run", "sprint", "turn", "jump", "climb"},
        "evade": {"roll", "step", "counter"},
        "attack": {"combo", "branch", "dash", "charge", "counter", "assist"},
        "skill": {"cast", "buff", "ultimate"},
        "switch": {"in", "out"},
        "hit": {"flinch", "launch", "block"},
        "death": {"fall", "revive"},
        "cutscene": {"enter", "transition"},
        "emote": {"greet", "taunt", "dance"},
        "demo": {"sample"},
    }
    for rule in COMBAT_PACKAGE_RULES:
        assert rule.category in categories, rule
        assert rule.sub in categories[rule.category], rule


def test_write_produces_a_shelf_the_library_can_read(tmp_path) -> None:
    for slug in ("attack-branch-01", "run", "skin"):
        (tmp_path / f"{slug}.bvh").write_text("HIERARCHY\n", encoding="utf-8")
    result = write(str(tmp_path), categories=[{"slug": "attack", "label": "공격", "subs": []}])

    assert result["unmatched"] == ["skin.bvh"]
    assert result["shelved"] == 2
    written = json.loads((tmp_path / "local-shelf.json").read_text(encoding="utf-8"))
    entry = next(m for m in written["motions"] if m["name"] == "attack-branch-01.bvh")
    assert entry == {"name": "attack-branch-01.bvh", "category": "attack", "sub": "branch", "detail": "b1"}

    # library.load_shelf 가 실제로 이 파일을 읽어야 의미가 있다
    from maxmcp.ui.studio.library import load_shelf

    assert load_shelf(str(tmp_path))["by_name"]["run.bvh"] == ("locomotion", "run", None)


def test_build_omits_absent_levels(tmp_path) -> None:
    # 세부가 없는 규칙이 detail: null 을 쓰면 그리드가 "세부 없음" 선반을 만든다.
    (tmp_path / "run.bvh").write_text("HIERARCHY\n", encoding="utf-8")
    shelf = build(str(tmp_path), categories=[])
    assert shelf["motions"] == [{"name": "run.bvh", "category": "locomotion", "sub": "run"}]
