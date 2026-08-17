"""8방향 로코모션 세트를 임의 각도로 굳히기 — Max 쪽 구현.

사이트(`/viewer` 블렌드 패널)는 살아 있는 three 믹서를 표본화해 굳힌다. Max 는
브라우저를 띄우지 않고 로컬 폴더에서 일하므로 BVH 채널을 직접 섞는다.

가중치 정의는 사이트의 `src/lib/blend-space.ts` 와 **같아야 한다.** 갈리면 같은
다이얼이 두 곳에서 서로 다른 방향을 낸다. `tests/test_blend.py` 의 픽스처 표가
저쪽 테스트와 같은 숫자이고, 그것이 유일한 교차 저장소 계약이다.

포즈 보간은 three 가 액션 순서대로 slerp 를 누적하는 방식을 `quat.quat_blend` 가
따라가지만, 부동소수점 누적 차이로 1e-6 수준에서 갈린다. 비트 단위 동일은 보장하지
않는다.

굳힌 결과는 임시 BVH 로 쓰고 **경로만** 돌려준다. `import_clip` / `retarget_clip`
이 이미 파일 경로를 받으므로, 그 두 슬롯에 손대지 않고도 바이패드 생성·트림·미러·
팔 간격·배치 간격이 그대로 따라온다.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from typing import Optional, Sequence

from maxmcp.helpers.bvh import BvhFile, BvhJoint, parse_bvh, serialize_bvh
from maxmcp.helpers.quat import euler_to_quat, quat_blend, quat_to_euler

COMPASS = [
    ("f", 0.0),
    ("fr", 45.0),
    ("r", 90.0),
    ("br", 135.0),
    ("b", 180.0),
    ("bl", 225.0),
    ("l", 270.0),
    ("fl", 315.0),
]
_SECTOR = 45.0
_DIRS = {d for d, _ in COMPASS}

# 느린 층이 슬라이더 왼쪽에 오도록 하는 우선순위. 사이트의 TIER_ORDER 와 같다.
TIER_ORDER = ["walk", "jog", "run", "sprint"]

# 사이트의 STAGE_HEIGHT_M 과 같은 값. 클립 높이를 실제 미터로 바꾸는 유일한 기준이다.
STAGE_HEIGHT_M = 1.8
MANIFEST_NAME = "artoke-manifest.json"
PHASE_NAME = "phase.json"


# ---- 가중치 -----------------------------------------------------------------


def direction_weights(angle_deg: float) -> list[tuple[str, float]]:
    """임의 각도를 인접 두 방향의 선형 가중치로. 합은 항상 1 이다."""
    angle = (angle_deg % 360.0 + 360.0) % 360.0
    index = int(angle // _SECTOR) % len(COMPASS)
    nxt = (index + 1) % len(COMPASS)
    t = (angle - index * _SECTOR) / _SECTOR
    return [(COMPASS[index][0], 1.0 - t), (COMPASS[nxt][0], t)]


def blend_weights(
    angle_deg: float, speed_t: float, tiers: Sequence[str]
) -> dict[str, float]:
    """방향 x 속도의 이중선형 가중치. 키는 ``<tier>-<dir>``.

    speed_t 0 = tiers[0](느린 층), 1 = 마지막 층. 층이 하나면 speed_t 는 무시한다 —
    걷기층이 등재되기 전에도 방향 블렌드는 동작해야 한다.
    """
    dirs = direction_weights(angle_deg)
    if len(tiers) <= 1:
        return {f"{tiers[0]}-{d}": w for d, w in dirs}

    clamped = min(1.0, max(0.0, speed_t))
    span = clamped * (len(tiers) - 1)
    # 층이 셋 이상이면 speed_t 가 가리키는 인접 두 층 사이만 섞는다.
    low = min(len(tiers) - 2, int(span))
    t = span - low
    weights: dict[str, float] = {}
    for d, w in dirs:
        weights[f"{tiers[low]}-{d}"] = w * (1.0 - t)
        weights[f"{tiers[low + 1]}-{d}"] = w * t
    return weights


def discover_tiers(entries: Sequence[dict]) -> list[str]:
    """8방향이 전부 갖춰진 locomotion 층만. 빠진 방향이 있으면 세트가 아니다.

    사이트의 `discoverBlendTiers` 와 같은 규칙이다: 카테고리가 locomotion 이고
    슬러그가 ``<층>-<방향>`` 꼴이어야 한다. 매니페스트의 ``sub`` 로 묶지 않는 이유는
    저쪽이 슬러그 접두사로 묶기 때문이고, 규칙이 두 벌이면 갈린다.
    """
    by_tier: dict[str, set[str]] = {}
    for entry in entries:
        if entry.get("category") != "locomotion":
            continue
        name = str(entry.get("name", ""))
        if not name.endswith(".bvh"):
            continue
        tier, _, direction = name[: -len(".bvh")].rpartition("-")
        if not tier or not tier.isalpha() or direction not in _DIRS:
            continue
        by_tier.setdefault(tier, set()).add(direction)
    full = [tier for tier, dirs in by_tier.items() if len(dirs) == len(COMPASS)]
    return sorted(
        full,
        key=lambda tier: TIER_ORDER.index(tier) if tier in TIER_ORDER else len(TIER_ORDER),
    )


def weighted(weights: dict[str, float], values: dict[str, float]) -> Optional[float]:
    """사이트의 weightedAverage 와 같다. 값이 없는 슬러그는 빠지고, 없으면 None."""
    total = 0.0
    accumulated = 0.0
    for slug, weight in weights.items():
        if weight <= 0 or slug not in values:
            continue
        total += weight
        accumulated += weight * values[slug]
    return accumulated / total if total > 0 else None


# ---- 채널 블렌딩 -------------------------------------------------------------


def _channel_joints(joint: BvhJoint) -> list[BvhJoint]:
    """채널을 가진 관절을 모션 행의 열 순서대로. End Site 는 채널이 없어 빠진다."""
    found = [joint] if joint.channels else []
    for child in joint.children:
        found.extend(_channel_joints(child))
    return found


def _channel_columns(bvh: BvhFile) -> list[tuple[BvhJoint, dict[str, int]]]:
    """관절별 (채널이름 -> 열 번호). `bvh._column_map` 과 같은 순회 순서다."""
    columns: list[tuple[BvhJoint, dict[str, int]]] = []
    cursor = 0
    for joint in _channel_joints(bvh.root):
        mapping: dict[str, int] = {}
        for channel in joint.channels:
            mapping[channel] = cursor
            cursor += 1
        columns.append((joint, mapping))
    return columns


def cycle_window(bvh: BvhFile, phase_entry: dict) -> BvhFile:
    """소스에서 한 보행 사이클만 잘라낸다. 위상 정보가 없으면 그대로 돌려준다.

    이걸 빠뜨리면 `blend_channels` 가 출력 프레임을 **6초 테이크 전체**에 걸쳐
    매핑한다 — 다섯 걸음이 한 걸음 시간에 압축돼 들어가고, 프레임간 회전 변화량이
    소스의 3도에서 18도로 뛴다(실측). 결과는 다리가 뭉개진 클립이다.

    사이트 쪽은 `AnimationUtils.subclip` 으로 같은 일을 먼저 한다
    (`blend-preview.tsx`) — 여기서도 같은 경계로 잘라야 두 출력이 같은 물건이 된다.

    시작점은 첫 왼발 접지다: 모든 소스가 같은 위상에서 시작해야 섞을 때 발이 맞는다.
    """
    frames = phase_entry.get("cycleFrames", 0)
    if frames <= 0:
        return bvh
    start = (phase_entry.get("leftContacts") or [0])[0]
    end = min(len(bvh.frames), start + frames)
    if end - start < 2:
        return bvh
    return BvhFile(root=bvh.root, frame_time=bvh.frame_time, frames=bvh.frames[start:end])


def _sample(frames: list[list[float]], frame: int, total: int) -> list[float]:
    """소스를 ``total`` 프레임으로 늘이거나 줄여 ``frame`` 번째 행을 뽑는다.

    가장 가까운 행을 고른다 — 행 사이를 선형 보간하면 회전 채널을 Euler 공간에서
    섞는 셈이 되고, 그것이 이 모듈이 존재하는 이유와 정면으로 어긋난다. 사이클 길이가
    소스와 몇 프레임 차이라 가장 가까운 행으로 충분하다. 층 간 주기 차이가 크게
    벌어지면(걷기 40 vs 스프린트 16) 티가 날 수 있고, 그때는 사원수 리샘플링이 답이다.
    """
    if len(frames) == 1 or total <= 1:
        return frames[0]
    position = frame / (total - 1) * (len(frames) - 1)
    return frames[min(len(frames) - 1, int(round(position)))]


def blend_channels(pairs: Sequence[tuple[BvhFile, float]], frames: int) -> BvhFile:
    """가중치대로 섞은 새 BvhFile. 헤더(관절 트리)는 첫 소스의 것을 쓴다.

    회전은 사원수에서, 위치는 선형으로 섞는다. 위치를 사원수로 올릴 이유는 없고,
    회전을 선형으로 두면 45도 간격에서 무너진다.
    """
    active = [(bvh, w) for bvh, w in pairs if w > 0]
    if not active:
        raise ValueError("가중치가 0 이 아닌 소스가 없다")

    base = active[0][0]
    layout = _channel_columns(base)
    width = len(base.frames[0])
    out_frames: list[list[float]] = []

    for frame in range(frames):
        rows = [(_sample(bvh.frames, frame, frames), w) for bvh, w in active]
        total_weight = sum(w for _, w in rows)
        values = [0.0] * width

        for _joint, columns in layout:
            for axis in ("Xposition", "Yposition", "Zposition"):
                column = columns.get(axis)
                if column is None:
                    continue
                values[column] = sum(row[column] * w for row, w in rows) / total_weight

            if "Zrotation" not in columns:
                continue
            blended = quat_blend(
                [
                    (
                        euler_to_quat(
                            row[columns["Xrotation"]],
                            row[columns["Yrotation"]],
                            row[columns["Zrotation"]],
                        ),
                        w,
                    )
                    for row, w in rows
                ]
            )
            if blended is None:
                continue
            x_deg, y_deg, z_deg = quat_to_euler(blended)
            values[columns["Xrotation"]] = x_deg
            values[columns["Yrotation"]] = y_deg
            values[columns["Zrotation"]] = z_deg

        out_frames.append(values)

    return BvhFile(root=base.root, frame_time=base.frame_time, frames=out_frames)


# ---- 이동 합성 --------------------------------------------------------------


def synthesise_travel(
    bvh: BvhFile,
    travel_joint_name: str,
    angle_deg: float,
    metres_per_second: float,
    source_units_per_metre: float,
) -> BvhFile:
    """수평 이동을 다이얼 방향의 직선으로 갈아 끼운다. Y 는 손대지 않는다.

    소스의 이동을 섞는 대신 합성하는 이유는 다이얼이 30도면 결과가 정확히 30도로
    가야 하기 때문이다 — 소스에 섞인 드리프트가 결과를 끌고 가지 않는다. 상하
    흔들림과 점프는 애니메이션의 일부라 수직 성분은 그대로 둔다.
    """
    columns = None
    for joint, mapping in _channel_columns(bvh):
        if joint.name == travel_joint_name and "Xposition" in mapping:
            columns = mapping
            break
    if columns is None:
        return bvh

    radians = math.radians(angle_deg)
    # 사이트의 방향 화살표와 같은 규약: 이 리그의 오른쪽은 −X 다.
    per_second_x = -math.sin(radians) * metres_per_second * source_units_per_metre
    per_second_z = math.cos(radians) * metres_per_second * source_units_per_metre
    origin_x = bvh.frames[0][columns["Xposition"]]
    origin_z = bvh.frames[0][columns["Zposition"]]

    frames = [list(row) for row in bvh.frames]
    for index, row in enumerate(frames):
        seconds = index * bvh.frame_time
        row[columns["Xposition"]] = origin_x + per_second_x * seconds
        row[columns["Zposition"]] = origin_z + per_second_z * seconds
    return BvhFile(root=bvh.root, frame_time=bvh.frame_time, frames=frames)


# ---- 전체 절차 --------------------------------------------------------------


def _travel_joint_of(bvh: BvhFile) -> Optional[str]:
    """이동을 들고 있는 관절. 없으면 None.

    `skeleton.travel_joint` 가 이미 위치 채널의 분산으로 이것을 찾으므로 그것을 쓴다 —
    이름을 하드코딩하면 리그가 바뀔 때 조용히 틀린다. helpers 가 ui 를 부르는 것은
    층이 뒤집힌 것이지만, 이 한 함수를 옮기는 것은 동작하는 코드를 건드리는 일이고
    복제하는 것은 더 나쁘다. 순환은 없다 (skeleton 은 helpers.bvh 만 부른다).

    분산이 0 이면(정지 클립) 고를 근거가 없다. 그때는 위치 채널을 가진 관절 중
    **가장 깊은** 것을 쓴다 — SOMA 는 Root 와 Hips 가 둘 다 위치 채널을 갖고 이동은
    Hips 가 들고 있으므로, 첫 번째를 고르면 정지 노드에 이동을 쓰게 된다.
    """
    from maxmcp.ui.studio.skeleton import travel_joint

    found = travel_joint(bvh)
    if found:
        return found
    candidates = [
        joint.name for joint, columns in _channel_columns(bvh) if "Xposition" in columns
    ]
    return candidates[-1] if candidates else None


def bake_blend_file(folder: str, angle_deg: float, speed_t: float) -> dict:
    """폴더의 8방향 세트를 임의 각도로 굳혀 임시 BVH 로 쓰고 정보를 돌려준다.

    임포트는 하지 않는다 — 경로만 넘기면 기존 `import_clip` / `retarget_clip` 이
    나머지를 그대로 처리한다.
    """
    manifest_path = os.path.join(folder, MANIFEST_NAME)
    if not os.path.exists(manifest_path):
        raise ValueError(
            f"{MANIFEST_NAME} 가 없다 — 먼저 사이트에서 동기화해야 8방향 세트를 찾을 수 있다."
        )
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    entries = manifest["motions"] if isinstance(manifest, dict) else manifest

    tiers = discover_tiers(entries)
    if not tiers:
        raise ValueError("8방향이 모두 갖춰진 locomotion 층이 없다 — 세트를 동기화하라.")

    phase_path = os.path.join(folder, PHASE_NAME)
    if not os.path.exists(phase_path):
        raise ValueError(
            f"{PHASE_NAME} 가 없다 — 발접지 위상 없이 섞으면 발이 엇갈린 클립이 나온다."
        )
    with open(phase_path, encoding="utf-8") as handle:
        phase = json.load(handle)["clips"]

    weights = blend_weights(angle_deg, speed_t, tiers)
    active = {slug: w for slug, w in weights.items() if w > 0}

    def entry(slug: str) -> dict:
        return phase.get(f"{slug}.bvh", {})

    def cycle_seconds_of(slug: str) -> Optional[float]:
        found = entry(slug)
        frames = found.get("cycleFrames", 0)
        fps = found.get("fps", 0)
        return frames / fps if frames > 0 and fps > 0 else None

    # 목표 사이클: 지금 실제로 섞이는 클립들의 가중 평균 주기. 사이트의
    # weightedCadence 와 같은 계산이다 — 언리얼 sync group 이 하는 일이고, 그래야
    # 걷기(40프레임)와 달리기(22프레임)를 섞을 때 한쪽이 끌려가지 않는다.
    cycles = {
        slug: seconds
        for slug in active
        if (seconds := cycle_seconds_of(slug)) is not None
    }
    cycle_seconds = weighted(active, cycles)
    if cycle_seconds is None or cycle_seconds <= 0:
        raise ValueError("위상 정보가 없어 사이클 길이를 정할 수 없다.")

    pairs = []
    for slug, weight in active.items():
        path = os.path.join(folder, f"{slug}.bvh")
        if not os.path.exists(path):
            raise ValueError(f"{slug}.bvh 가 폴더에 없다 — 매니페스트와 파일이 어긋났다.")
        with open(path, encoding="utf-8") as handle:
            source = parse_bvh(handle.read())
        # 섞기 전에 한 사이클로 자른다. 자르지 않으면 6초 테이크 전체가 사이클
        # 길이로 압축된다 — 자세한 이유는 `cycle_window` 주석.
        pairs.append((cycle_window(source, entry(slug)), weight))

    first = next(iter(active))
    fps = entry(first).get("fps", 30)
    frames = max(2, round(cycle_seconds * fps))
    blended = blend_channels(pairs, frames=frames)
    # 프레임 간격은 목표 사이클을 프레임 수로 나눈 것이다. 소스 간격을 그대로 두면
    # 파일이 주장하는 길이와 실제 사이클이 어긋난다.
    blended = BvhFile(
        root=blended.root, frame_time=cycle_seconds / frames, frames=blended.frames
    )

    # 실제 지면 속도는 소스 속도 x timeScale 이다. 리타이밍을 무시하면 걷기↔달리기를
    # 건너갈 때 합성 이동이 발을 미끄러뜨린다 (사이트의 effectiveSpeeds 와 같다).
    speeds = {}
    for slug in active:
        found = entry(slug)
        own_cycle = cycle_seconds_of(slug)
        if "metresPerSecond" in found and own_cycle:
            speeds[slug] = found["metresPerSecond"] * own_cycle / cycle_seconds
    speed = weighted(active, speeds)

    rig_height = entry(first).get("rigHeight")
    travel_name = _travel_joint_of(blended)
    if speed is not None and rig_height and travel_name:
        blended = synthesise_travel(
            blended, travel_name, angle_deg, speed, rig_height / STAGE_HEIGHT_M
        )

    bearing = int(round((angle_deg % 360 + 360) % 360))
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=f"_blend-{'-'.join(tiers)}-{bearing:03d}deg.bvh",
        delete=False,
        encoding="utf-8",
    )
    with handle:
        handle.write(serialize_bvh(blended))
    return {
        "path": handle.name,
        "tiers": tiers,
        "metresPerSecond": speed,
        "frames": frames,
        "frameTime": blended.frame_time,
    }
