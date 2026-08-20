import json
import math
import pathlib

import pytest

from maxmcp.helpers.quat import (
    euler_to_quat,
    quat_blend,
    quat_normalise,
    quat_slerp,
    quat_to_euler,
)


@pytest.mark.parametrize(
    "angles",
    [(0, 0, 0), (30, 0, 0), (0, 45, 0), (0, 0, -60), (12, -34, 56), (89, 1, -179)],
)
def test_euler_round_trip(angles):
    """Euler -> 사원수 -> Euler 가 제자리로 돌아온다. 부호 규약이 틀리면 여기서 깨진다."""
    back = quat_to_euler(euler_to_quat(*angles))
    for got, want in zip(back, angles):
        assert abs(got - want) < 1e-9


def test_rejects_orders_the_clips_do_not_use():
    """등재된 클립은 전부 ZYX 다. 다른 순서는 조용히 틀리는 대신 거부한다."""
    with pytest.raises(ValueError):
        euler_to_quat(0, 0, 0, order="XYZ")
    with pytest.raises(ValueError):
        quat_to_euler((0.0, 0.0, 0.0, 1.0), order="XYZ")


def test_gimbal_pole_still_round_trips_the_rotation():
    """ZYX 의 특이점(m31 이 ±1, 즉 y=±90°)에서도 회전 자체는 보존된다."""
    q = euler_to_quat(0, 90, 0)
    again = euler_to_quat(*quat_to_euler(q))
    for got, want in zip(again, q):
        assert abs(got - want) < 1e-9


def test_slerp_halfway_between_two_yaws_is_the_midpoint():
    a = euler_to_quat(0, 0, 0)
    b = euler_to_quat(0, 90, 0)
    assert abs(quat_to_euler(quat_slerp(a, b, 0.5))[1] - 45.0) < 1e-9


def test_slerp_endpoints_are_exact():
    a = euler_to_quat(0, 0, 0)
    b = euler_to_quat(0, 90, 0)
    assert quat_slerp(a, b, 0.0) == a
    assert quat_slerp(a, b, 1.0) == b


def test_slerp_identical_quaternions_does_not_divide_by_zero():
    a = euler_to_quat(11, 22, 33)
    assert quat_slerp(a, a, 0.5) == pytest.approx(a)


def test_blend_ignores_zero_weights():
    a = euler_to_quat(0, 0, 0)
    b = euler_to_quat(0, 90, 0)
    assert quat_blend([(a, 1.0), (b, 0.0)]) == pytest.approx(a)


def test_blend_of_equal_weights_matches_slerp_half():
    a = euler_to_quat(0, 0, 0)
    b = euler_to_quat(0, 90, 0)
    assert quat_blend([(a, 0.5), (b, 0.5)]) == pytest.approx(quat_slerp(a, b, 0.5))


def test_blend_weights_need_not_sum_to_one():
    """가중치는 누적 비율로만 쓰이므로 0.25/0.25 도 0.5/0.5 와 같은 결과여야 한다."""
    a = euler_to_quat(0, 0, 0)
    b = euler_to_quat(0, 90, 0)
    assert quat_blend([(a, 0.25), (b, 0.25)]) == pytest.approx(quat_blend([(a, 0.5), (b, 0.5)]))


def test_blend_of_nothing_is_none():
    assert quat_blend([]) is None
    assert quat_blend([(euler_to_quat(0, 0, 0), 0.0)]) is None


def test_blend_takes_the_short_way_round():
    """부호가 반대인 두 사원수는 같은 회전이다 — 긴 쪽으로 돌면 안 된다."""
    a = euler_to_quat(0, 10, 0)
    b = tuple(-v for v in euler_to_quat(0, 30, 0))
    assert abs(quat_to_euler(quat_blend([(a, 0.5), (b, 0.5)]))[1] - 20.0) < 1e-9


def test_blend_of_four_lands_inside_the_spread():
    """블렌드 스페이스에서 가중치가 0이 아닌 클립은 최대 4개(방향 2 x 층 2)다."""
    quats = [(euler_to_quat(0, yaw, 0), 0.25) for yaw in (0, 10, 20, 30)]
    yaw = quat_to_euler(quat_blend(quats))[1]
    assert 0.0 < yaw < 30.0


def test_normalise_of_zero_is_identity():
    """길이 0 은 회전이 아니다. 0 으로 나누는 대신 항등을 준다."""
    assert quat_normalise((0.0, 0.0, 0.0, 0.0)) == (0.0, 0.0, 0.0, 1.0)


def test_euler_to_quat_matches_a_hand_computed_yaw():
    """three 에서 옮긴 식이 맞는지 손으로 계산한 값과 맞춰 본다: Y축 90°."""
    x, y, z, w = euler_to_quat(0, 90, 0)
    assert abs(x) < 1e-15
    assert abs(y - math.sin(math.pi / 4)) < 1e-15
    assert abs(z) < 1e-15
    assert abs(w - math.cos(math.pi / 4)) < 1e-15


# ---- three 와의 교차 검증 ----------------------------------------------------
#
# 이 모듈은 three 의 식을 옮긴 것이고, 갈려도 화면에는 아무 표시가 나지 않는다 —
# 사이트에서 내보낸 클립과 Max 에서 굳힌 클립이 조용히 달라질 뿐이다. 그래서 three
# 의 실제 출력을 픽스처로 박아 두고 숫자로 맞춘다.
#
# 픽스처는 script-market 의 `scripts/emit-zyx-reference.mts` 가 만든다. 손으로
# 고치지 말고 그 스크립트를 다시 돌려라.

_REFERENCE = pathlib.Path(__file__).parent / "fixtures" / "three-zyx-reference.json"


def _reference_cases():
    with _REFERENCE.open(encoding="utf-8") as handle:
        return json.load(handle)["cases"]


def test_reference_fixture_is_present():
    assert _REFERENCE.exists(), (
        f"{_REFERENCE} 가 없다 — script-market 에서 "
        "`npx tsx scripts/emit-zyx-reference.mts <경로>` 로 생성하라."
    )


@pytest.mark.parametrize("case", _reference_cases(), ids=lambda c: str(c["degrees"]))
def test_euler_to_quat_matches_three(case):
    got = euler_to_quat(*case["degrees"])
    for value, want in zip(got, case["quaternion"]):
        assert abs(value - want) < 1e-12


@pytest.mark.parametrize("case", _reference_cases(), ids=lambda c: str(c["degrees"]))
def test_quat_to_euler_matches_three(case):
    got = quat_to_euler(tuple(case["quaternion"]))
    for value, want in zip(got, case["backToDegrees"]):
        assert abs(value - want) < 1e-9
