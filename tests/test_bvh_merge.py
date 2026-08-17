"""관절 합치기 — 동작이 보존되는지를 월드 좌표로 검증한다.

채널 값만 비교하면 합쳤다는 사실만 확인할 뿐 "같은 동작인가"는 답하지 못한다.
정방향 운동학으로 모든 관절의 월드 위치를 프레임마다 구해 합치기 전후를 맞춰 본다.
"""

import pytest

from maxmcp.helpers.bvh import (
    _axis_values,
    _column_map,
    merge_into_parent,
    parse_bvh,
    serialize_bvh,
)
from maxmcp.helpers.quat import euler_to_quat, quat_mul, quat_rotate

# 부모가 회전하고, 합칠 관절도 회전하고, 그 관절의 rest 오프셋이 0 이 아니다.
# 셋이 다 있어야 "부모 회전에 딸려 도는 오프셋" 항이 결과에 나타난다.
THREE_JOINT = """HIERARCHY
ROOT Hips
{
\tOFFSET 0.0 0.0 0.0
\tCHANNELS 6 Xposition Yposition Zposition Zrotation Yrotation Xrotation
\tJOINT Pelvis
\t{
\t\tOFFSET 1.0 2.0 3.0
\t\tCHANNELS 3 Zrotation Yrotation Xrotation
\t\tJOINT LeftUpLeg
\t\t{
\t\t\tOFFSET 0.0 -10.0 0.0
\t\t\tCHANNELS 3 Zrotation Yrotation Xrotation
\t\t\tEnd Site
\t\t\t{
\t\t\t\tOFFSET 0.0 -8.0 0.0
\t\t\t}
\t\t}
\t}
}
MOTION
Frames: 3
Frame Time: 0.033333
0.0 90.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0
5.0 92.0 1.0 30.0 20.0 10.0 15.0 25.0 35.0 40.0 5.0 20.0
-3.0 88.0 2.0 -45.0 60.0 -20.0 -10.0 5.0 50.0 12.0 -30.0 8.0
"""


def world_positions(bvh) -> list[dict]:
    """프레임별 {관절 이름: 월드 위치}. BVH 의 정의 그대로 누적한다."""
    columns = _column_map(bvh.root)
    frames = []
    for row in bvh.frames:
        found: dict[str, tuple[float, float, float]] = {}

        def visit(joint, parent_pos, parent_quat):
            start, _ = columns[id(joint)]
            tx, ty, tz = _axis_values(joint, row, start, "position")
            rx, ry, rz = _axis_values(joint, row, start, "rotation")
            local = (
                joint.offset[0] + tx,
                joint.offset[1] + ty,
                joint.offset[2] + tz,
            )
            carried = quat_rotate(parent_quat, local)
            here = (
                parent_pos[0] + carried[0],
                parent_pos[1] + carried[1],
                parent_pos[2] + carried[2],
            )
            found[joint.name] = here
            here_quat = quat_mul(parent_quat, euler_to_quat(rx, ry, rz))
            for child in joint.children:
                visit(child, here, here_quat)

        visit(bvh.root, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))
        frames.append(found)
    return frames


def assert_motion_preserved(before, after, merged, parent, tolerance=1e-9) -> None:
    """합치기가 동작을 바꾸지 않았음을 월드 좌표로 확인한다.

    ``parent`` 는 비교에서 뺀다 — 합치고 나면 부모가 합쳐진 관절의 자리에 서는
    것이 정의다. 대신 그 자리가 정확히 옛 ``merged`` 자리인지를 따로 본다.
    """
    a, b = world_positions(before), world_positions(after)
    assert len(a) == len(b)
    shared = (set(a[0]) & set(b[0])) - {parent}
    assert shared, "비교할 공통 관절이 없다"
    worst = 0.0
    for frame_a, frame_b in zip(a, b):
        for joint in shared:
            worst = max(
                worst,
                max(abs(p - q) for p, q in zip(frame_a[joint], frame_b[joint])),
            )
    assert worst < tolerance, f"월드 위치가 어긋났다: 최대 {worst}"

    moved = max(
        max(abs(p - q) for p, q in zip(frame_a[merged], frame_b[parent]))
        for frame_a, frame_b in zip(a, b)
    )
    assert moved < tolerance, f"{parent} 가 {merged} 자리에 안 섰다: 최대 {moved}"


def test_merge_preserves_every_joint_position() -> None:
    before = parse_bvh(THREE_JOINT)
    after = merge_into_parent(before, "Pelvis")
    assert_motion_preserved(before, after, "Pelvis", "Hips")


def test_merge_removes_the_joint_and_adopts_its_children() -> None:
    after = merge_into_parent(parse_bvh(THREE_JOINT), "Pelvis")
    assert [c.name for c in after.root.children] == ["LeftUpLeg"]
    # 열도 같이 줄어야 한다 — 안 그러면 남은 관절이 엉뚱한 열을 읽는다
    assert len(after.frames[0]) == 9


def test_merge_needs_the_offset_carry_term() -> None:
    """부모가 회전하는 프레임에서 오프셋 항이 실제로 결과를 바꾼다.

    이 항을 빼먹으면 가만히 선 클립에서는 안 보이고 도는 클립에서만 틀린다.
    그 차이가 무시할 수준이 아니라는 것을 숫자로 남긴다.
    """
    before = parse_bvh(THREE_JOINT)
    after = merge_into_parent(before, "Pelvis")
    # 회전이 0 인 첫 프레임은 단순 덧셈이라 오프셋이 그대로 실린다
    assert after.frames[0][:3] == pytest.approx([1.0, 92.0, 3.0])
    # 회전이 걸린 프레임은 오프셋이 돌아서 실리므로 단순 덧셈과 달라야 한다
    naive = [5.0 + 1.0, 92.0 + 2.0, 1.0 + 3.0]
    assert after.frames[1][:3] != pytest.approx(naive, abs=1e-6)


def test_merging_a_missing_joint_changes_nothing() -> None:
    before = parse_bvh(THREE_JOINT)
    assert merge_into_parent(before, "NotHere") is before


def test_merging_the_root_is_refused() -> None:
    # 조용히 통과시키면 루트가 사라진 파일을 만들어 낸다.
    with pytest.raises(ValueError, match="루트"):
        merge_into_parent(parse_bvh(THREE_JOINT), "Hips")


def test_merge_survives_a_round_trip_through_text() -> None:
    before = parse_bvh(THREE_JOINT)
    after = parse_bvh(serialize_bvh(merge_into_parent(before, "Pelvis")))
    assert_motion_preserved(before, after, "Pelvis", "Hips", tolerance=1e-4)  # 직렬화가 소수 6 자리로 자른다


def test_merge_holds_when_the_joint_swings_far() -> None:
    """골반이 크게 도는 구간. 작은 각도만 보면 합성 오류가 오차에 묻힌다.

    수치는 실제 Max Biped 클립에서 재 온 것이다 — 골반 오프셋
    ``(-0.468, -3.360, 2.988)`` 에 회전 폭 96 도. 클립 자체를 픽스처로 두지 않는
    것은 그것이 게임 자산이기 때문이고, 합성을 검증하는 데 필요한 것은 파일이
    아니라 이 숫자들이다.
    """
    frames = []
    for step in range(13):
        t = step / 12
        frames.append(
            " ".join(
                str(v)
                for v in (
                    3 * t, 95 + 2 * t, -4 * t,          # Hips 이동
                    140 * t - 70, 55 * t, -30 * t,       # Hips 회전
                    96 * t - 48, 20 * t, -35 * t,        # Pelvis 회전 (폭 96도)
                    45 * t, -25 * t, 60 * t,             # LeftUpLeg 회전
                )
            )
        )
    text = (
        THREE_JOINT.split("MOTION")[0].replace(
            "OFFSET 1.0 2.0 3.0", "OFFSET -0.467969 -3.360026 2.987886"
        )
        + "MOTION\nFrames: 13\nFrame Time: 0.033333\n"
        + "\n".join(frames)
        + "\n"
    )
    before = parse_bvh(text)
    after = merge_into_parent(before, "Pelvis")
    assert "Pelvis" not in set(_names(after.root))
    assert_motion_preserved(before, after, "Pelvis", "Hips")


def _names(joint):
    yield joint.name
    for child in joint.children:
        yield from _names(child)
