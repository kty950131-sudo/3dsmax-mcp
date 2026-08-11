"""곡선 제어점을 프레임 단위 time_map 으로 변환한다.

제어점 사이는 선형 보간을 쓴다. 제어점이 단조면 선형 보간 결과도 단조라
``warp`` 의 단조 요구가 구조적으로 보장된다.
"""

from typing import Sequence


def build_time_map(
    points: Sequence[tuple[float, float]], src_frames: int
) -> list[float]:
    """(출력_비율, 원본_비율) 제어점에서 time_map 을 만든다.

    출력 비율 1.0 이 원본 길이와 같은 재생 시간이다. 마지막 제어점의 출력
    비율이 2.0 이면 결과는 원본의 두 배 길이(= 절반 속도)가 된다.
    """
    if len(points) < 2:
        raise ValueError("need at least two control points")
    if src_frames < 1:
        raise ValueError(f"src_frames must be positive, got {src_frames}")
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        if bx < ax or by < ay:
            raise ValueError(f"control points must be non-decreasing: {(ax, ay)} -> {(bx, by)}")
    if points[-1][0] <= points[0][0]:
        raise ValueError(f"curve must advance in output time: first x={points[0][0]}, last x={points[-1][0]}")

    last_frame = float(src_frames - 1)
    out_frames = max(1, int(round(points[-1][0] * last_frame)) + 1)

    result: list[float] = []
    for i in range(out_frames):
        out_ratio = (i / last_frame) if last_frame else 0.0
        result.append(_sample(points, out_ratio) * last_frame)
    return result


def _sample(points: Sequence[tuple[float, float]], x: float) -> float:
    """제어점 위에서 x 에 해당하는 원본 비율을 선형 보간으로 구한다."""
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        if ax <= x <= bx:
            if bx == ax:
                return by
            return ay + (by - ay) * (x - ax) / (bx - ax)
    # Unreachable: for any x in (points[0][0], points[-1][0]), one of the loop conditions
    # must match due to monotone non-decreasing points. This line is a safety fallback only.
    return points[-1][1]
