"""BVH 채널 블렌딩을 위한 ZYX Euler <-> 사원수 변환.

`bvh.py` 는 채널 값을 바로 보간한다. 시간 워프(한 클립, 작은 간격)에는 맞지만
45도 떨어진 여덟 방향을 섞는 데는 틀린다 — Euler 삼중항을 평균하면 소스가 한 번도
취하지 않는 포즈를 지나가고 짐벌 근처에서는 접힌다. 블렌딩은 사원수에서 한다.

식은 three.js 의 ``Quaternion.setFromEuler`` 와 ``Euler.setFromRotationMatrix`` 의
ZYX 분기를 그대로 옮긴 것이다. 직접 유도하면 부호 규약이 어긋나 사이트 쪽 결과와
갈리는데, 그 갈림은 화면에 보이지 않고 파일에만 남는다.

등재된 클립은 모든 관절이 ``Zrotation Yrotation Xrotation`` 하나뿐이라 ZYX 만
구현한다. 다른 순서는 조용히 틀리는 대신 거부한다.

표준 라이브러리만 쓴다 — 3ds Max 내장 파이썬에 바이너리 의존을 더하지 않는다.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

Quat = tuple[float, float, float, float]

ORDER = "ZYX"
# three.Quaternion.slerp 이 쓰는 값과 같다.
_EPS = 2.220446049250313e-16
# three.Euler.setFromRotationMatrix 의 특이점 판정과 같은 문턱값.
_POLE = 0.9999999


def _require_zyx(order: str) -> None:
    if order != ORDER:
        raise ValueError(
            f"회전 순서 {order!r} 는 지원하지 않는다 — 등재된 클립은 모두 {ORDER} 다. "
            "다른 순서를 섞으려면 그 분기를 three 에서 옮겨 와야 한다."
        )


def euler_to_quat(
    x_deg: float, y_deg: float, z_deg: float, order: str = ORDER
) -> Quat:
    """X·Y·Z 축 회전각(도)을 사원수로.

    각도는 축 기준이고 순서 문자열은 합성 방식만 정한다 — three 의 ``Euler`` 와
    같은 규약이므로 BVH 의 ``Zrotation Yrotation Xrotation`` 행을 그대로 넘기지
    말고 축별로 풀어서 넘겨야 한다.
    """
    _require_zyx(order)
    half_x = math.radians(x_deg) / 2
    half_y = math.radians(y_deg) / 2
    half_z = math.radians(z_deg) / 2
    c1, c2, c3 = math.cos(half_x), math.cos(half_y), math.cos(half_z)
    s1, s2, s3 = math.sin(half_x), math.sin(half_y), math.sin(half_z)
    return (
        s1 * c2 * c3 - c1 * s2 * s3,
        c1 * s2 * c3 + s1 * c2 * s3,
        c1 * c2 * s3 - s1 * s2 * c3,
        c1 * c2 * c3 + s1 * s2 * s3,
    )


def quat_to_euler(q: Quat, order: str = ORDER) -> tuple[float, float, float]:
    """사원수를 X·Y·Z 축 회전각(도)으로. `euler_to_quat` 의 역이다."""
    _require_zyx(order)
    x, y, z, w = q
    x2, y2, z2 = x + x, y + y, z + z
    xx, xy, xz = x * x2, x * y2, x * z2
    yy, yz, zz = y * y2, y * z2, z * z2
    wx, wy, wz = w * x2, w * y2, w * z2

    # three.Matrix4.makeRotationFromQuaternion 의 성분 이름 그대로.
    m11 = 1 - (yy + zz)
    m12 = xy - wz
    m21 = xy + wz
    m22 = 1 - (xx + zz)
    m31 = xz - wy
    m32 = yz + wx
    m33 = 1 - (xx + yy)

    euler_y = math.asin(-max(-1.0, min(1.0, m31)))
    if abs(m31) < _POLE:
        euler_x = math.atan2(m32, m33)
        euler_z = math.atan2(m21, m11)
    else:
        # 짐벌 극: X 와 Z 가 같은 축을 돌아 분해가 하나로 정해지지 않는다. three 와
        # 같이 X 를 0 으로 두고 남는 회전을 전부 Z 에 몬다.
        euler_x = 0.0
        euler_z = math.atan2(-m12, m22)
    return (math.degrees(euler_x), math.degrees(euler_y), math.degrees(euler_z))


def quat_normalise(q: Quat) -> Quat:
    """단위 사원수로. 길이 0 은 회전이 아니므로 항등을 준다."""
    length = math.sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3])
    if length == 0:
        return (0.0, 0.0, 0.0, 1.0)
    return (q[0] / length, q[1] / length, q[2] / length, q[3] / length)


def quat_slerp(a: Quat, b: Quat, t: float) -> Quat:
    """three.Quaternion.slerp 를 그대로 옮긴 것."""
    if t == 0.0:
        return a
    if t == 1.0:
        return b

    cos_half = a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]
    # 부호가 반대인 두 사원수는 같은 회전이다. 뒤집지 않으면 긴 쪽으로 돈다.
    if cos_half < 0:
        b = (-b[0], -b[1], -b[2], -b[3])
        cos_half = -cos_half
    if cos_half >= 1.0:
        return a

    sqr_sin_half = 1.0 - cos_half * cos_half
    if sqr_sin_half <= _EPS:
        # 거의 같은 방향 — 선형 보간 후 정규화. sin 으로 나누면 0 이 된다.
        s = 1.0 - t
        return quat_normalise(
            (
                s * a[0] + t * b[0],
                s * a[1] + t * b[1],
                s * a[2] + t * b[2],
                s * a[3] + t * b[3],
            )
        )

    sin_half = math.sqrt(sqr_sin_half)
    half_theta = math.atan2(sin_half, cos_half)
    ratio_a = math.sin((1.0 - t) * half_theta) / sin_half
    ratio_b = math.sin(t * half_theta) / sin_half
    return (
        a[0] * ratio_a + b[0] * ratio_b,
        a[1] * ratio_a + b[1] * ratio_b,
        a[2] * ratio_a + b[2] * ratio_b,
        a[3] * ratio_a + b[3] * ratio_b,
    )


def quat_blend(pairs: Sequence[tuple[Quat, float]]) -> Optional[Quat]:
    """가중 사원수 블렌드. 가중치가 하나도 없으면 None.

    three 의 ``PropertyMixer`` 와 같은 누적 순서를 쓴다: 누적값에 다음 사원수를
    ``w / 누적가중치`` 로 slerp 해 넣는다. 사이트와 같은 결과를 내려면 순서까지 같아야
    한다 — 다른 순서로 누적하면 부동소수점 차이가 아니라 다른 값이 나온다.

    가중치는 비율로만 쓰이므로 합이 1 일 필요는 없다.
    """
    result: Optional[Quat] = None
    total = 0.0
    for q, weight in pairs:
        if weight <= 0:
            continue
        total += weight
        if result is None:
            result = quat_normalise(q)
            continue
        result = quat_slerp(result, q, weight / total)
    return result
