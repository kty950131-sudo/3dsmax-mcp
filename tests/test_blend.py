import json
import math
import os

import pytest

from maxmcp.helpers.blend import (
    bake_blend_file,
    blend_channels,
    blend_weights,
    cycle_window,
    direction_weights,
    discover_tiers,
    synthesise_travel,
)
from maxmcp.helpers.bvh import parse_bvh, serialize_bvh

# ---- 사이트와 공유하는 가중치 픽스처 ----------------------------------------
#
# script-market 의 `src/lib/blend-space.test.ts` 와 같은 표다. 두 저장소가 이 숫자에서
# 갈리면 같은 다이얼이 서로 다른 방향을 내므로, 여기 값을 고치려면 저쪽도 같이 고쳐야
# 한다. 각도는 이진수로 정확히 표현되는 것만 골랐다 — 그래서 허용오차 없이 == 로 본다.

DIRECTION_CASES = [
    (0, ("f", 1.0), ("fr", 0.0)),
    (22.5, ("f", 0.5), ("fr", 0.5)),
    (45, ("fr", 1.0), ("r", 0.0)),
    (180, ("b", 1.0), ("bl", 0.0)),
    (315, ("fl", 1.0), ("f", 0.0)),
    (337.5, ("fl", 0.5), ("f", 0.5)),
    (-22.5, ("fl", 0.5), ("f", 0.5)),
    (382.5, ("f", 0.5), ("fr", 0.5)),
]


@pytest.mark.parametrize("angle,first,second", DIRECTION_CASES)
def test_direction_weights(angle, first, second):
    assert direction_weights(angle) == [first, second]


def test_direction_weights_always_sum_to_one():
    for angle in range(-360, 721, 7):
        assert sum(w for _, w in direction_weights(angle)) == pytest.approx(1.0)


BLEND_CASES = [
    (["run"], 22.5, 0.7, {"run-f": 0.5, "run-fr": 0.5}),
    (["walk", "run"], 0, 0, {"walk-f": 1.0, "run-f": 0.0, "walk-fr": 0.0, "run-fr": 0.0}),
    (["walk", "run"], 0, 1, {"walk-f": 0.0, "run-f": 1.0, "walk-fr": 0.0, "run-fr": 0.0}),
    (
        ["walk", "run"],
        22.5,
        0.5,
        {"walk-f": 0.25, "run-f": 0.25, "walk-fr": 0.25, "run-fr": 0.25},
    ),
    (["walk", "run"], 0, 2, {"walk-f": 0.0, "run-f": 1.0, "walk-fr": 0.0, "run-fr": 0.0}),
    (["walk", "run"], 0, -1, {"walk-f": 1.0, "run-f": 0.0, "walk-fr": 0.0, "run-fr": 0.0}),
    (
        ["walk", "run", "sprint"],
        0,
        0.25,
        {"walk-f": 0.5, "run-f": 0.5, "walk-fr": 0.0, "run-fr": 0.0},
    ),
    (
        ["walk", "run", "sprint"],
        0,
        0.5,
        {"run-f": 1.0, "sprint-f": 0.0, "run-fr": 0.0, "sprint-fr": 0.0},
    ),
    (
        ["walk", "run", "sprint"],
        0,
        1,
        {"run-f": 0.0, "sprint-f": 1.0, "run-fr": 0.0, "sprint-fr": 0.0},
    ),
]


@pytest.mark.parametrize("tiers,angle,speed_t,expected", BLEND_CASES)
def test_blend_weights(tiers, angle, speed_t, expected):
    assert blend_weights(angle, speed_t, tiers) == expected


def test_three_tiers_leave_the_far_tier_out_entirely():
    """인접 두 층만 섞는다 — speed_t 0.5 에서 walk 키는 아예 없다."""
    assert "walk-f" not in blend_weights(0, 0.5, ["walk", "run", "sprint"])


def test_discover_tiers_needs_all_eight_directions():
    entries = [
        {"name": f"walk-{d}.bvh", "category": "locomotion"}
        for d in ["f", "fr", "r", "br", "b", "bl", "l", "fl"]
    ]
    entries.append({"name": "run-f.bvh", "category": "locomotion"})
    entries.append({"name": "punch-f.bvh", "category": "attack"})
    assert discover_tiers(entries) == ["walk"]


def test_discover_tiers_orders_slow_to_fast():
    entries = [
        {"name": f"{tier}-{d}.bvh", "category": "locomotion"}
        for tier in ["sprint", "walk", "run"]
        for d in ["f", "fr", "r", "br", "b", "bl", "l", "fl"]
    ]
    assert discover_tiers(entries) == ["walk", "run", "sprint"]


def test_discover_tiers_ignores_non_locomotion_even_with_matching_slugs():
    entries = [
        {"name": f"walk-{d}.bvh", "category": "attack"}
        for d in ["f", "fr", "r", "br", "b", "bl", "l", "fl"]
    ]
    assert discover_tiers(entries) == []


def test_discover_tiers_handles_an_empty_manifest():
    assert discover_tiers([]) == []


# ---- 채널 블렌딩 -------------------------------------------------------------


def minimal_bvh(rows: list[list[float]], frame_time: float = 0.0333333) -> str:
    """Root(6채널) + Hips(3채널) 짜리 최소 BVH. 채널 순서는 등재 클립과 같다."""
    head = (
        "HIERARCHY\n"
        "ROOT Root\n"
        "{\n"
        "  OFFSET 0.00 0.00 0.00\n"
        "  CHANNELS 6 Xposition Yposition Zposition Zrotation Yrotation Xrotation\n"
        "  JOINT Hips\n"
        "  {\n"
        "    OFFSET 0.00 100.00 0.00\n"
        "    CHANNELS 3 Zrotation Yrotation Xrotation\n"
        "    End Site\n"
        "    {\n"
        "      OFFSET 0.00 10.00 0.00\n"
        "    }\n"
        "  }\n"
        "}\n"
    )
    motion = f"MOTION\nFrames: {len(rows)}\nFrame Time: {frame_time}\n"
    motion += "".join(" ".join(f"{v:.6f}" for v in row) + "\n" for row in rows)
    return head + motion


ZERO_ROW = [0.0] * 9


def test_blend_channels_round_trips_shape():
    a = parse_bvh(minimal_bvh([ZERO_ROW, ZERO_ROW]))
    reparsed = parse_bvh(serialize_bvh(blend_channels([(a, 1.0)], frames=2)))
    assert len(reparsed.frames) == 2
    assert len(reparsed.frames[0]) == 9


def test_full_weight_on_one_clip_reproduces_it():
    rows = [[1.0, 2.0, 3.0, 10.0, 20.0, 30.0, 5.0, 6.0, 7.0]] * 3
    out = blend_channels([(parse_bvh(minimal_bvh(rows)), 1.0)], frames=3)
    for got, want in zip(out.frames[0], rows[0]):
        assert abs(got - want) < 1e-6


def test_blending_two_yaws_lands_between_them():
    left = parse_bvh(minimal_bvh([ZERO_ROW] * 2))
    right = parse_bvh(minimal_bvh([[0.0, 0.0, 0.0, 0.0, 90.0, 0.0, 0.0, 0.0, 0.0]] * 2))
    out = blend_channels([(left, 0.5), (right, 0.5)], frames=2)
    # Root 의 Yrotation 은 5번째 열 (Xpos Ypos Zpos Zrot Yrot Xrot)
    assert abs(out.frames[0][4] - 45.0) < 1e-6


def test_zero_weight_sources_are_ignored():
    left = parse_bvh(minimal_bvh([ZERO_ROW] * 2))
    right = parse_bvh(minimal_bvh([[0.0, 0.0, 0.0, 0.0, 90.0, 0.0, 0.0, 0.0, 0.0]] * 2))
    out = blend_channels([(left, 1.0), (right, 0.0)], frames=2)
    assert abs(out.frames[0][4]) < 1e-6


def test_no_active_source_is_an_error_not_an_empty_clip():
    a = parse_bvh(minimal_bvh([ZERO_ROW] * 2))
    with pytest.raises(ValueError):
        blend_channels([(a, 0.0)], frames=2)


def test_positions_are_averaged_linearly_not_slerped():
    a = parse_bvh(minimal_bvh([ZERO_ROW] * 2))
    b = parse_bvh(minimal_bvh([[10.0] + [0.0] * 8] * 2))
    out = blend_channels([(a, 0.5), (b, 0.5)], frames=2)
    assert abs(out.frames[0][0] - 5.0) < 1e-6


def test_clips_are_resampled_to_the_requested_frame_count():
    a = parse_bvh(minimal_bvh([ZERO_ROW] * 10))
    assert len(blend_channels([(a, 1.0)], frames=4).frames) == 4


def test_resampling_follows_the_source_curve():
    """소스가 프레임마다 다르면 리샘플이 순서를 지켜야 한다."""
    rows = [[0.0, 0.0, 0.0, 0.0, float(i * 10), 0.0, 0.0, 0.0, 0.0] for i in range(10)]
    out = blend_channels([(parse_bvh(minimal_bvh(rows)), 1.0)], frames=4)
    yaws = [row[4] for row in out.frames]
    assert yaws == sorted(yaws)
    assert abs(yaws[0]) < 1e-6
    assert abs(yaws[-1] - 90.0) < 1e-6


# ---- 이동 합성 --------------------------------------------------------------


def test_synthesise_travel_walks_the_dialled_bearing():
    bvh = parse_bvh(minimal_bvh([ZERO_ROW] * 5))
    out = synthesise_travel(bvh, "Root", 30.0, 1.5, 100.0 / 1.8)
    dx = out.frames[4][0] - out.frames[0][0]
    dz = out.frames[4][2] - out.frames[0][2]
    # 사이트와 같은 규약: 이 리그의 오른쪽은 −X 라 방위각은 atan2(−dx, dz) 다.
    assert abs(math.degrees(math.atan2(-dx, dz)) - 30.0) < 1e-9
    seconds = 4 * bvh.frame_time
    assert abs(math.hypot(dx, dz) - 1.5 * seconds * (100.0 / 1.8)) < 1e-9


def test_synthesise_travel_leaves_the_vertical_alone():
    rows = [[0.0, float(i), 0.0] + [0.0] * 6 for i in range(4)]
    out = synthesise_travel(parse_bvh(minimal_bvh(rows)), "Root", 90.0, 2.0, 55.0)
    assert [row[1] for row in out.frames] == [0.0, 1.0, 2.0, 3.0]


def test_synthesise_travel_starts_from_the_clips_own_origin():
    rows = [[5.0, 0.0, 7.0] + [0.0] * 6] * 3
    out = synthesise_travel(parse_bvh(minimal_bvh(rows)), "Root", 0.0, 1.0, 55.0)
    assert out.frames[0][0] == 5.0
    assert out.frames[0][2] == 7.0


def test_synthesise_travel_forward_is_plus_z_and_right_is_minus_x():
    bvh = parse_bvh(minimal_bvh([ZERO_ROW] * 2, frame_time=1.0))
    forward = synthesise_travel(bvh, "Root", 0.0, 1.0, 1.0)
    assert abs(forward.frames[1][0] - 0.0) < 1e-12
    assert abs(forward.frames[1][2] - 1.0) < 1e-12

    right = synthesise_travel(bvh, "Root", 90.0, 1.0, 1.0)
    assert abs(right.frames[1][0] + 1.0) < 1e-12
    assert abs(right.frames[1][2] - 0.0) < 1e-12


def test_synthesise_travel_does_not_mutate_its_input():
    bvh = parse_bvh(minimal_bvh([ZERO_ROW] * 3, frame_time=1.0))
    synthesise_travel(bvh, "Root", 90.0, 2.0, 55.0)
    assert bvh.frames[2][0] == 0.0


def test_synthesise_travel_returns_the_clip_when_the_joint_has_no_position():
    bvh = parse_bvh(minimal_bvh([ZERO_ROW] * 3))
    out = synthesise_travel(bvh, "Hips", 90.0, 2.0, 55.0)
    assert out.frames == bvh.frames


# ---- bake_blend_file 의 실패 경로 --------------------------------------------


def test_cycle_window_cuts_one_cycle_from_the_first_left_contact():
    rows = [[0.0, 0.0, 0.0, 0.0, float(i), 0.0, 0.0, 0.0, 0.0] for i in range(40)]
    out = cycle_window(
        parse_bvh(minimal_bvh(rows)),
        {"cycleFrames": 12, "leftContacts": [5, 17, 29]},
    )
    assert len(out.frames) == 12
    assert out.frames[0][4] == 5.0
    assert out.frames[-1][4] == 16.0


def test_cycle_window_passes_through_without_phase():
    rows = [[0.0] * 9 for _ in range(8)]
    bvh = parse_bvh(minimal_bvh(rows))
    assert len(cycle_window(bvh, {}).frames) == 8
    assert len(cycle_window(bvh, {"cycleFrames": 0}).frames) == 8


def test_cycle_window_refuses_a_window_that_ran_off_the_end():
    """접지가 테이크 끝에 있으면 두 프레임도 못 자른다 — 자르지 않는 쪽이 낫다."""
    rows = [[0.0] * 9 for _ in range(10)]
    bvh = parse_bvh(minimal_bvh(rows))
    assert len(cycle_window(bvh, {"cycleFrames": 12, "leftContacts": [9]}).frames) == 10


def test_bake_only_covers_one_cycle_not_the_whole_take(tmp_path):
    """굳힌 클립은 한 보행 사이클이어야 한다.

    이걸 놓치면 `_sample` 이 출력 프레임을 6초 테이크 전체에 매핑해서 다섯 걸음이
    한 걸음 시간에 압축된다. 실측으로 프레임간 회전 변화량이 소스의 3도에서 18도로
    뛰었고, 방위각·프레임 수·파싱은 전부 정상이라 그것만 보면 지나친다.

    소스의 Yrotation 을 프레임 번호로 두면 굳힌 클립이 훑은 구간이 값으로 그대로
    드러난다: 한 사이클(15프레임)만 봤으면 최대가 15 근처, 전체(60프레임)를 봤으면
    60 근처다.
    """
    dirs = ["f", "fr", "r", "br", "b", "bl", "l", "fl"]
    rows = [[0.0, 0.0, 0.0, 0.0, float(i), 0.0, 0.0, 0.0, 0.0] for i in range(60)]
    for d in dirs:
        (tmp_path / f"walk-{d}.bvh").write_text(minimal_bvh(rows), encoding="utf-8")
    (tmp_path / "artoke-manifest.json").write_text(
        json.dumps(
            {"motions": [{"name": f"walk-{d}.bvh", "category": "locomotion"} for d in dirs]}
        ),
        encoding="utf-8",
    )
    (tmp_path / "phase.json").write_text(
        json.dumps(
            {
                "version": 1,
                "clips": {
                    f"walk-{d}.bvh": {
                        "leftContacts": [0, 15, 30, 45],
                        "rightContacts": [7],
                        "cycleFrames": 15,
                        "fps": 30,
                        "metresPerSecond": 1.3,
                        "rigHeight": 100.0,
                    }
                    for d in dirs
                },
            }
        ),
        encoding="utf-8",
    )

    out = bake_blend_file(str(tmp_path), 0.0, 0.0)
    written = parse_bvh(open(out["path"], encoding="utf-8").read())
    os.unlink(out["path"])

    yaws = [row[4] for row in written.frames]
    assert out["frames"] == 15
    assert max(yaws) < 15.5, f"한 사이클을 넘어 훑었다 — 최대 Yrotation {max(yaws)}"
    # 프레임당 1도씩 오르는 소스이므로 굳힌 클립도 그래야 한다
    steps = [b - a for a, b in zip(yaws, yaws[1:])]
    assert max(steps) < 1.6, f"프레임이 건너뛰었다 — 최대 간격 {max(steps)}"


def test_bake_blend_file_says_why_without_a_manifest(tmp_path):
    with pytest.raises(ValueError, match="artoke-manifest"):
        bake_blend_file(str(tmp_path), 0.0, 0.0)


def test_bake_blend_file_says_why_without_a_set(tmp_path):
    (tmp_path / "artoke-manifest.json").write_text(
        json.dumps({"motions": [{"name": "run-f.bvh", "category": "locomotion"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="8방향"):
        bake_blend_file(str(tmp_path), 0.0, 0.0)


def test_bake_blend_file_says_why_without_phase(tmp_path):
    dirs = ["f", "fr", "r", "br", "b", "bl", "l", "fl"]
    (tmp_path / "artoke-manifest.json").write_text(
        json.dumps(
            {"motions": [{"name": f"walk-{d}.bvh", "category": "locomotion"} for d in dirs]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="phase.json"):
        bake_blend_file(str(tmp_path), 0.0, 0.0)


def test_bake_blend_file_writes_a_reparsable_clip(tmp_path):
    """세트와 위상이 갖춰지면 파싱 가능한 BVH 를 쓰고 방위각이 다이얼과 맞는다."""
    dirs = ["f", "fr", "r", "br", "b", "bl", "l", "fl"]
    rows = [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0] for _ in range(30)]
    for d in dirs:
        (tmp_path / f"walk-{d}.bvh").write_text(minimal_bvh(rows), encoding="utf-8")
    (tmp_path / "artoke-manifest.json").write_text(
        json.dumps(
            {"motions": [{"name": f"walk-{d}.bvh", "category": "locomotion"} for d in dirs]}
        ),
        encoding="utf-8",
    )
    (tmp_path / "phase.json").write_text(
        json.dumps(
            {
                "version": 1,
                "clips": {
                    f"walk-{d}.bvh": {
                        "leftContacts": [0, 15],
                        "rightContacts": [7],
                        "cycleFrames": 15,
                        "fps": 30,
                        "metresPerSecond": 1.3,
                        "rigHeight": 100.0,
                    }
                    for d in dirs
                },
            }
        ),
        encoding="utf-8",
    )

    out = bake_blend_file(str(tmp_path), 30.0, 0.0)
    assert out["tiers"] == ["walk"]
    assert out["metresPerSecond"] == pytest.approx(1.3)
    assert out["frames"] == 15

    written = parse_bvh(open(out["path"], encoding="utf-8").read())
    assert len(written.frames) == 15
    dx = written.frames[-1][0] - written.frames[0][0]
    dz = written.frames[-1][2] - written.frames[0][2]
    assert abs(math.degrees(math.atan2(-dx, dz)) - 30.0) < 1e-6
    assert math.hypot(dx, dz) > 0
