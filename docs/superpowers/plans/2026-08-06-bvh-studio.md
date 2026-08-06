# BVH Studio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `bvh-library` 의 모션 클립을 Mixamo 처럼 썸네일로 훑어보고, 속도 곡선과 트림 핸들로 다듬어, 버튼 하나로 3ds Max 바이패드에 올리는 PySide 툴을 만든다.

**Architecture:** 브라우징·프리뷰·곡선 편집은 순수 파이썬 + Qt 로 처리하고 Max 를 전혀 건드리지 않는다. Max 는 임포트 시점부터만 개입한다. 기존 `src/helpers/bvh.py` 에는 함수를 추가만 하고 기존 시그니처는 바꾸지 않아, 원본 `maxscript/bvh_biped_ui.ms` 가 계속 동작한다.

**Tech Stack:** Python 3.12, PySide2(Max 2024) / PySide6(Max 2026), pymxs, pytest 8.3.3

**Spec:** `docs/superpowers/specs/2026-08-06-bvh-studio-design.md`

## Global Constraints

- `maxscript/bvh_biped_ui.ms` 는 한 줄도 수정하지 않는다. 새 툴과 공존한다.
- `src/helpers/bvh.py` 의 기존 공개 함수 시그니처를 바꾸지 않는다. 추가만 한다.
- `qtmax` 에 의존하지 않는다. Max 2024 에는 없다 (`pymxs`, `PySide2`, `shiboken2` 뿐).
- numpy 를 새 의존성으로 추가하지 않는다. FK·보간은 순수 파이썬으로 한다.
- 라이브러리 폴더(`bvh-library`)에는 아무것도 쓰지 않는다. `github_sync.py` 의 동기화 대상이다.
- 모든 함수 시그니처에 타입 주석을 단다 (PEP 8).
- 테스트 실행: 저장소 루트에서 `python -m pytest tests/test_bvh_helpers.py tests/test_bvh_studio.py -q`
  - 시스템 Python 3.12.10 + pytest 8.3.3 을 쓴다. **`.venv` 에는 pytest 가 없다.**
  - 착수 전 기준선: `python -m pytest tests/test_bvh_helpers.py -q` → 17 passed
  - **`pytest tests/` 로 전체 스위트를 돌리지 않는다.** 33개 파일이
    `ModuleNotFoundError: No module named 'mcp'` 로 수집 실패한다. MCP 의존성은
    저장소 `.venv` 에만 있고 그 venv 에는 pytest 가 없다. 이 작업 이전부터
    그런 상태이며 이번 작업과 무관하다. 우리가 만드는 두 파일만 돌린다.
- 작업 브랜치를 먼저 만든다. `master` 에 직접 커밋하지 않는다.

```bash
git -C C:/work/Ai/3dsmax-mcp checkout -b feat/bvh-studio
```

### UI 경로 변경 (2026-08-06, Task 7 착수 전 결정)

UI 를 **Qt 위젯이 아니라 HTML/CSS 로 그리고 `QWebEngineView` 에 띄운다.** 디자인을
피그마로 잡기로 했고, QSS 로는 둥근 모서리·그림자·전환 애니메이션 재현에 한계가
있어서다. Task 1~6 (순수 로직) 은 전부 그대로 쓴다. 바뀌는 것은 **뷰 계층뿐**이다.

- **BVH Studio 는 Max 2026 전용이다.** Max 2024 의 PySide2 에는 QtWebEngine 이 없다
  (확인함: `3ds Max 2024/Python/Lib/site-packages/PySide2` 에 `QtWeb*.pyd` 없음).
  2024 사용자는 기존 `maxscript/bvh_biped_ui.ms` 롤아웃을 계속 쓴다 — **원본 무수정
  제약이 이제 2024 지원의 유일한 근거이므로 더더욱 지킨다.**
- `compat.py` (Task 6) 의 PySide2 분기는 **그대로 둔다.** 순수 로직 모듈은 2024 에서도
  import 되어야 하고, 이미 커밋·리뷰가 끝난 코드를 흔들지 않는다.
- 그리기는 파이썬이 아니라 JS 가 한다. 파이썬은 **좌표와 데이터만** 넘긴다.
  `build_pose_data` 가 이미 JSON 직렬화 가능한 dict 를 돌려주므로 그대로 건네면 된다.
- `QWebEngineView` 가 호스트 앱 안에서 뜨지 않을 위험이 실재한다 (아래 Task 9 참조).
  **UI 를 다 만들기 전에 Task 9 에서 먼저 증명한다.**

---

### Task 1: 오일러 언랩과 프레임 재샘플링

`retime()` 은 `frame_time` 만 나누므로 구간별로 다른 속도를 표현할 수 없다. 프레임을 실제로 재샘플하는 `warp()` 를 추가한다.

**Files:**
- Modify: `src/helpers/bvh.py` (`retime` 정의 뒤, 375행 부근)
- Test: `tests/test_bvh_helpers.py` (파일 끝에 추가)

**Interfaces:**
- Consumes: `BvhFile`, `BvhJoint` (기존 dataclass)
- Produces:
  - `unwrap_angles(values: list[float]) -> list[float]`
  - `_flat_channels(root: BvhJoint) -> list[str]`
  - `warp(bvh: BvhFile, time_map: Sequence[float]) -> BvhFile`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_bvh_helpers.py` 끝에 추가. 맨 위 import 문에 `unwrap_angles`, `warp` 를 추가한다.

```python
def test_unwrap_angles_crosses_180() -> None:
    # 179 -> -179 는 -358 도 이동이 아니라 +2 도 이동이다
    assert unwrap_angles([179.0, -179.0]) == pytest.approx([179.0, 181.0])


def test_unwrap_angles_passes_through_smooth_run() -> None:
    assert unwrap_angles([0.0, 10.0, 20.0]) == pytest.approx([0.0, 10.0, 20.0])


def test_unwrap_angles_empty() -> None:
    assert unwrap_angles([]) == []


def test_warp_identity_returns_original_frames() -> None:
    bvh = parse_bvh(KIMODO_STYLE)
    out = warp(bvh, [0.0, 1.0])
    assert out.frames == bvh.frames
    assert out.frame_time == bvh.frame_time


def test_warp_halves_frame_count() -> None:
    bvh = parse_bvh(KIMODO_STYLE)
    out = warp(bvh, [0.0])
    assert len(out.frames) == 1
    assert out.frames[0] == bvh.frames[0]


def test_warp_interpolates_midpoint() -> None:
    bvh = parse_bvh(KIMODO_STYLE)
    out = warp(bvh, [0.0, 0.5, 1.0])
    assert len(out.frames) == 3
    # 6번 컬럼(Hips Xposition)은 1.0 -> 1.1 이므로 중간은 1.05
    assert out.frames[1][6] == pytest.approx(1.05)


def test_warp_rejects_decreasing_time_map() -> None:
    bvh = parse_bvh(KIMODO_STYLE)
    with pytest.raises(ValueError, match="non-decreasing"):
        warp(bvh, [1.0, 0.0])


def test_warp_rejects_empty_time_map() -> None:
    bvh = parse_bvh(KIMODO_STYLE)
    with pytest.raises(ValueError, match="empty"):
        warp(bvh, [])


def test_warp_clamps_out_of_range() -> None:
    bvh = parse_bvh(KIMODO_STYLE)
    out = warp(bvh, [-5.0, 99.0])
    assert out.frames[0] == bvh.frames[0]
    assert out.frames[1] == bvh.frames[-1]
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_bvh_helpers.py -q`
Expected: FAIL — `ImportError: cannot import name 'unwrap_angles'`

- [ ] **Step 3: 최소 구현을 쓴다**

`src/helpers/bvh.py` 의 `retime` 정의 바로 뒤에 추가한다. 파일 상단 import 에 `Sequence` 를 더한다: `from typing import Optional, Sequence`

```python
def unwrap_angles(values: list[float]) -> list[float]:
    """오일러 각 수열을 연속으로 편다.

    ±180° 경계를 넘는 지점에서 360° 를 더하거나 빼, 인접 프레임 차이가
    항상 최단 경로가 되게 한다. 보간 전에 반드시 거쳐야 한다.
    """
    if not values:
        return []
    out = [values[0]]
    offset = 0.0
    for prev, cur in zip(values, values[1:]):
        delta = cur - prev
        if delta > 180.0:
            offset -= 360.0
        elif delta < -180.0:
            offset += 360.0
        out.append(cur + offset)
    return out


def _flat_channels(root: BvhJoint) -> list[str]:
    """모션 행의 컬럼 순서대로 채널 이름을 나열한다 (_column_map 과 같은 순회)."""
    names: list[str] = []

    def visit(joint: BvhJoint) -> None:
        names.extend(joint.channels)
        for child in joint.children:
            visit(child)

    visit(root)
    return names


def warp(bvh: BvhFile, time_map: Sequence[float]) -> BvhFile:
    """time_map[i] = 출력 프레임 i 가 가져올 원본 프레임 위치(소수 허용).

    ``retime`` 은 frame_time 만 바꾸므로 균일 속도만 표현할 수 있다. 구간별로
    다른 속도(타임워프)는 이 함수로 프레임을 재샘플해야 한다. frame_time 은
    유지되고 프레임 수만 달라진다.

    회전 채널은 언랩 후 보간하므로 출력값이 ±180° 를 벗어날 수 있다. 이는
    의도된 동작이다 — 연속적인 각도 수열이 소비자 입장에서 더 안전하다.
    """
    if not time_map:
        raise ValueError("time_map must not be empty")
    if not bvh.frames:
        raise ValueError("bvh has no frames")
    for prev, cur in zip(time_map, time_map[1:]):
        if cur < prev:
            raise ValueError(f"time_map must be non-decreasing: {prev} -> {cur}")

    n = len(bvh.frames)
    if len(time_map) == n and all(t == i for i, t in enumerate(time_map)):
        return bvh  # 항등 사상 — 원본을 손대지 않는다

    channels = _flat_channels(bvh.root)
    columns = [list(col) for col in zip(*bvh.frames)]
    prepared = [
        unwrap_angles(col) if name.lower().endswith("rotation") else col
        for name, col in zip(channels, columns)
    ]

    frames: list[list[float]] = []
    for t in time_map:
        clamped = min(max(t, 0.0), float(n - 1))
        lo = int(clamped)
        hi = min(lo + 1, n - 1)
        frac = clamped - lo
        frames.append([col[lo] + (col[hi] - col[lo]) * frac for col in prepared])
    return BvhFile(root=bvh.root, frame_time=bvh.frame_time, frames=frames)
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_bvh_helpers.py -q`
Expected: PASS — 26 passed (기존 17 + 신규 9)

- [ ] **Step 5: 커밋**

```bash
git add tests/test_bvh_helpers.py src/helpers/bvh.py
git commit -m "feat: add euler unwrap and frame resampling to bvh helpers"
```

---

### Task 2: prepare_for_biped 에 time_map 경로 추가

기존 호출자(`bvh_biped_ui.ms`)가 그대로 동작해야 하므로 선택 인자로만 더한다.

**Files:**
- Modify: `src/helpers/bvh.py:400-415` (`prepare_for_biped`)
- Test: `tests/test_bvh_helpers.py`

**Interfaces:**
- Consumes: `warp` (Task 1)
- Produces: `prepare_for_biped(text, prune=(), offset=(0,0,0), speed=1.0, trim_range=(0.0,1.0), time_map=None) -> str`
  - `time_map` 의 인덱스는 **트림된 뒤** 클립 기준이다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_prepare_without_time_map_is_unchanged() -> None:
    # 회귀 방어: 기존 호출자(bvh_biped_ui.ms)의 출력이 바뀌면 안 된다
    baseline = prepare_for_biped(KIMODO_STYLE, prune=("LeftEye",), speed=2.0)
    with_none = prepare_for_biped(
        KIMODO_STYLE, prune=("LeftEye",), speed=2.0, time_map=None
    )
    assert with_none == baseline


def test_prepare_with_time_map_resamples() -> None:
    out = prepare_for_biped(KIMODO_STYLE, time_map=[0.0, 0.5, 1.0])
    assert "Frames: 3" in out


def test_prepare_time_map_ignores_speed() -> None:
    # time_map 이 있으면 speed 는 적용되지 않는다 (frame_time 유지)
    out = prepare_for_biped(KIMODO_STYLE, speed=4.0, time_map=[0.0, 1.0])
    assert "Frame Time: 0.033333" in out
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_bvh_helpers.py -q`
Expected: FAIL — `TypeError: prepare_for_biped() got an unexpected keyword argument 'time_map'`

- [ ] **Step 3: 구현**

`src/helpers/bvh.py:400` 의 `prepare_for_biped` 를 통째로 교체한다.

```python
def prepare_for_biped(
    text: str,
    prune: tuple[str, ...] = (),
    offset: tuple[float, float, float] = (0.0, 0.0, 0.0),
    speed: float = 1.0,
    trim_range: tuple[float, float] = (0.0, 1.0),
    time_map: Optional[Sequence[float]] = None,
) -> str:
    """Rewrite BVH text so 3ds Max biped.loadMocapFile accepts it.

    ``time_map`` 을 주면 균일 ``speed`` 대신 비균일 타임워프를 적용한다.
    인덱스는 트림된 뒤 클립 기준이다. 주지 않으면 기존 경로 그대로다.
    """
    bvh = parse_bvh(text)
    bvh = strip_static_root(bvh)
    bvh = prune_joints(bvh, prune)
    bvh = rename_for_biped(bvh)
    bvh = offset_root(bvh, offset)
    bvh = trim(bvh, trim_range[0], trim_range[1])
    if time_map is None:
        bvh = retime(bvh, speed)
    else:
        bvh = warp(bvh, time_map)
    return serialize_bvh(bvh)
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_bvh_helpers.py -q`
Expected: PASS — 29 passed

- [ ] **Step 5: 커밋**

```bash
git add tests/test_bvh_helpers.py src/helpers/bvh.py
git commit -m "feat: accept optional time_map in prepare_for_biped"
```

---

### Task 3: 제어점 → time_map 생성

곡선 위젯이 만든 제어점에서 프레임 단위 time_map 을 만든다. Qt 없이 테스트 가능하도록 순수 모듈로 분리한다.

**Files:**
- Create: `src/ui/__init__.py` (빈 파일)
- Create: `src/ui/studio/__init__.py` (빈 파일)
- Create: `src/ui/studio/timemap.py`
- Test: `tests/test_bvh_studio.py` (신규)

**Interfaces:**
- Produces: `build_time_map(points: Sequence[tuple[float, float]], src_frames: int) -> list[float]`
  - `points` 는 `(출력_비율, 원본_비율)` 쌍. 둘 다 0.0~1.0.
  - 반환 길이는 출력 프레임 수이며, 마지막 점의 출력 비율로 결정된다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_bvh_studio.py` 를 새로 만든다.

```python
import pytest

from src.ui.studio.timemap import build_time_map


def test_flat_curve_is_identity() -> None:
    # 출력과 원본이 1:1 이면 항등 사상
    assert build_time_map([(0.0, 0.0), (1.0, 1.0)], 5) == pytest.approx(
        [0.0, 1.0, 2.0, 3.0, 4.0]
    )


def test_half_speed_doubles_output_frames() -> None:
    # 출력 2배 길이 동안 원본 전체를 소비 = 절반 속도
    out = build_time_map([(0.0, 0.0), (2.0, 1.0)], 5)
    assert len(out) == 9
    assert out[0] == pytest.approx(0.0)
    assert out[-1] == pytest.approx(4.0)


def test_result_is_always_non_decreasing() -> None:
    out = build_time_map([(0.0, 0.0), (0.5, 0.1), (1.0, 1.0)], 20)
    assert all(b >= a for a, b in zip(out, out[1:]))


def test_rejects_decreasing_control_points() -> None:
    with pytest.raises(ValueError, match="non-decreasing"):
        build_time_map([(0.0, 0.0), (1.0, 0.5), (2.0, 0.2)], 5)


def test_rejects_fewer_than_two_points() -> None:
    with pytest.raises(ValueError, match="at least two"):
        build_time_map([(0.0, 0.0)], 5)


def test_rejects_empty_source() -> None:
    with pytest.raises(ValueError, match="src_frames"):
        build_time_map([(0.0, 0.0), (1.0, 1.0)], 0)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_bvh_studio.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.ui'`

- [ ] **Step 3: 구현**

`src/ui/__init__.py` 와 `src/ui/studio/__init__.py` 는 빈 파일로 만든다.

`src/ui/studio/timemap.py`:

```python
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
    return points[-1][1]
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_bvh_studio.py -q`
Expected: PASS — 6 passed

- [ ] **Step 5: 커밋**

```bash
git add src/ui tests/test_bvh_studio.py
git commit -m "feat: build frame time maps from curve control points"
```

---

### Task 4: BVH 순운동학과 투영

썸네일을 그리려면 채널값에서 조인트 월드 좌표를 구해야 한다. numpy 없이 순수 파이썬으로 한다.

**Files:**
- Create: `src/ui/studio/skeleton.py`
- Test: `tests/test_bvh_studio.py` (추가)

**Interfaces:**
- Consumes: `BvhFile`, `BvhJoint`, `_flat_channels` (Task 1)
- Produces:
  - `fk(bvh: BvhFile, frame: int) -> dict[str, tuple[float, float, float]]`
  - `bones(root: BvhJoint) -> list[tuple[str, str]]`
  - `project(pos: tuple[float, float, float], azimuth_deg: float) -> tuple[float, float]`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_bvh_studio.py` 에 추가.

```python
from src.helpers.bvh import parse_bvh
from src.ui.studio.skeleton import bones, fk, project

TWO_JOINT = """HIERARCHY
ROOT Hips
{
  OFFSET 0.0 0.0 0.0
  CHANNELS 6 Xposition Yposition Zposition Zrotation Yrotation Xrotation
  JOINT Head
  {
    OFFSET 0.0 10.0 0.0
    CHANNELS 3 Zrotation Yrotation Xrotation
    End Site
    {
      OFFSET 0.0 5.0 0.0
    }
  }
}
MOTION
Frames: 2
Frame Time: 0.033333
0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0
0.0 100.0 0.0 0.0 0.0 0.0 0.0 0.0 90.0
"""


def test_fk_rest_pose_stacks_offsets() -> None:
    bvh = parse_bvh(TWO_JOINT)
    pos = fk(bvh, 0)
    assert pos["Hips"] == pytest.approx((0.0, 0.0, 0.0))
    assert pos["Head"] == pytest.approx((0.0, 10.0, 0.0))


def test_fk_applies_root_translation() -> None:
    bvh = parse_bvh(TWO_JOINT)
    pos = fk(bvh, 1)
    assert pos["Hips"] == pytest.approx((0.0, 100.0, 0.0))


def test_bones_lists_parent_child_pairs() -> None:
    bvh = parse_bvh(TWO_JOINT)
    assert bones(bvh.root) == [("Hips", "Head")]


def test_project_front_view_keeps_x_and_y() -> None:
    assert project((3.0, 7.0, 0.0), 0.0) == pytest.approx((3.0, 7.0))


def test_project_side_view_uses_z() -> None:
    x, y = project((3.0, 7.0, 5.0), 90.0)
    assert x == pytest.approx(5.0)
    assert y == pytest.approx(7.0)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_bvh_studio.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.ui.studio.skeleton'`

- [ ] **Step 3: 구현**

`src/ui/studio/skeleton.py`:

```python
"""BVH 채널값 → 조인트 월드 좌표 (순수 파이썬, numpy 미사용)."""

import math
from typing import Sequence

from src.helpers.bvh import BvhFile, BvhJoint

Vec3 = tuple[float, float, float]
Mat3 = tuple[Vec3, Vec3, Vec3]

_IDENTITY: Mat3 = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def _rot(axis: str, deg: float) -> Mat3:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    if axis == "X":
        return ((1.0, 0.0, 0.0), (0.0, c, -s), (0.0, s, c))
    if axis == "Y":
        return ((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c))
    return ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))


def _mul(a: Mat3, b: Mat3) -> Mat3:
    return tuple(
        tuple(sum(a[r][k] * b[k][c] for k in range(3)) for c in range(3))
        for r in range(3)
    )  # type: ignore[return-value]


def _apply(m: Mat3, v: Vec3) -> Vec3:
    return tuple(sum(m[r][c] * v[c] for c in range(3)) for r in range(3))  # type: ignore[return-value]


def fk(bvh: BvhFile, frame: int) -> dict[str, Vec3]:
    """frame 번째 프레임의 조인트별 월드 좌표를 구한다."""
    if not 0 <= frame < len(bvh.frames):
        raise ValueError(f"frame out of range: {frame}")
    row = bvh.frames[frame]
    out: dict[str, Vec3] = {}
    col = 0

    def visit(joint: BvhJoint, parent_pos: Vec3, parent_rot: Mat3) -> None:
        nonlocal col
        trans: Vec3 = (0.0, 0.0, 0.0)
        rot = _IDENTITY
        for name in joint.channels:
            value = row[col]
            axis = name[0].upper()
            if name.lower().endswith("position"):
                idx = {"X": 0, "Y": 1, "Z": 2}[axis]
                trans = tuple(  # type: ignore[assignment]
                    value if i == idx else trans[i] for i in range(3)
                )
            else:
                rot = _mul(rot, _rot(axis, value))
            col += 1

        local = tuple(joint.offset[i] + trans[i] for i in range(3))
        world = _apply(parent_rot, local)  # type: ignore[arg-type]
        pos = tuple(parent_pos[i] + world[i] for i in range(3))
        out[joint.name] = pos  # type: ignore[assignment]
        world_rot = _mul(parent_rot, rot)
        for child in joint.children:
            visit(child, pos, world_rot)  # type: ignore[arg-type]

    # 컬럼 순서는 _column_map 과 같은 전위 순회이므로 col 을 따라가면 된다
    visit(bvh.root, (0.0, 0.0, 0.0), _IDENTITY)
    return out


def bones(root: BvhJoint) -> list[tuple[str, str]]:
    """(부모 이름, 자식 이름) 쌍 목록. 그릴 뼈대다."""
    pairs: list[tuple[str, str]] = []

    def visit(joint: BvhJoint) -> None:
        for child in joint.children:
            pairs.append((joint.name, child.name))
            visit(child)

    visit(root)
    return pairs


def project(pos: Vec3, azimuth_deg: float) -> tuple[float, float]:
    """Y 축 둘레로 azimuth 만큼 돌린 뒤 정직교 투영한다. Y 가 화면 위."""
    rad = math.radians(azimuth_deg)
    x = pos[0] * math.cos(rad) + pos[2] * math.sin(rad)
    return (x, pos[1])


def bounds(positions: Sequence[Vec3], azimuth_deg: float) -> tuple[float, float, float, float]:
    """투영된 좌표의 (min_x, min_y, max_x, max_y). 썸네일 정규화용."""
    pts = [project(p, azimuth_deg) for p in positions]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_bvh_studio.py -q`
Expected: PASS — 11 passed

- [ ] **Step 5: 커밋**

```bash
git add src/ui/studio/skeleton.py tests/test_bvh_studio.py
git commit -m "feat: add bvh forward kinematics and orthographic projection"
```

---

### Task 5: 클립 라이브러리 스캔·태그·캐시

**Files:**
- Create: `src/ui/studio/library.py`
- Test: `tests/test_bvh_studio.py` (추가)

**Interfaces:**
- Produces:
  - `Clip` — `NamedTuple(stem: str, path: str, tags: tuple[str, ...])`
  - `scan(folder: str) -> list[Clip]`
  - `extract_tags(stem: str) -> tuple[str, ...]`
  - `cache_path(clip_path: str, cache_dir: str) -> str`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
from src.ui.studio.library import Clip, cache_path, extract_tags, scan


def test_extract_tags_splits_on_underscore() -> None:
    assert extract_tags("artoke_spin-kick") == ("artoke", "spin-kick")


def test_extract_tags_drops_numeric_suffix() -> None:
    assert extract_tags("attack-combo_00") == ("attack-combo",)


def test_extract_tags_single_token() -> None:
    assert extract_tags("run2") == ("run2",)


def test_scan_excludes_biped_conversions(tmp_path) -> None:
    (tmp_path / "run2.bvh").write_text("x", encoding="utf-8")
    (tmp_path / "run2_biped.bvh").write_text("x", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    clips = scan(str(tmp_path))
    assert [c.stem for c in clips] == ["run2"]


def test_scan_sorts_by_stem(tmp_path) -> None:
    for name in ("zebra.bvh", "alpha.bvh"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    assert [c.stem for c in scan(str(tmp_path))] == ["alpha", "zebra"]


def test_scan_missing_folder_returns_empty() -> None:
    assert scan("Z:/definitely/not/here") == []


def test_cache_path_is_outside_library(tmp_path) -> None:
    clip = str(tmp_path / "run2.bvh")
    out = cache_path(clip, str(tmp_path / "cache"))
    assert out.endswith(".json")
    assert "cache" in out


def test_cache_path_differs_per_clip(tmp_path) -> None:
    a = cache_path(str(tmp_path / "a.bvh"), "C:/cache")
    b = cache_path(str(tmp_path / "b.bvh"), "C:/cache")
    assert a != b
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_bvh_studio.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.ui.studio.library'`

- [ ] **Step 3: 구현**

`src/ui/studio/library.py`:

```python
"""클립 폴더 스캔과 캐시 경로 계산.

라이브러리 폴더에는 아무것도 쓰지 않는다. github_sync 의 동기화 대상이라
캐시를 그 안에 두면 오염된다.
"""

import hashlib
import os
from typing import NamedTuple


class Clip(NamedTuple):
    stem: str
    path: str
    tags: tuple[str, ...]


def extract_tags(stem: str) -> tuple[str, ...]:
    """파일명에서 태그를 뽑는다. 숫자만인 토막은 태그로 만들지 않는다."""
    parts = [p for p in stem.split("_") if p and not p.isdigit()]
    return tuple(parts) if parts else (stem,)


def scan(folder: str) -> list[Clip]:
    """폴더의 .bvh 를 스캔한다. ``*_biped.bvh`` 는 변환 산출물이라 제외한다."""
    if not os.path.isdir(folder):
        return []
    clips: list[Clip] = []
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith(".bvh"):
            continue
        stem = name[: -len(".bvh")]
        if stem.endswith("_biped"):
            continue
        clips.append(
            Clip(stem=stem, path=os.path.join(folder, name), tags=extract_tags(stem))
        )
    return clips


def cache_path(clip_path: str, cache_dir: str) -> str:
    """클립 절대 경로 해시로 캐시 파일 경로를 만든다."""
    digest = hashlib.sha1(
        os.path.abspath(clip_path).lower().encode("utf-8")
    ).hexdigest()[:16]
    return os.path.join(cache_dir, f"{digest}.json")
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_bvh_studio.py -q`
Expected: PASS — 19 passed

- [ ] **Step 5: 커밋**

```bash
git add src/ui/studio/library.py tests/test_bvh_studio.py
git commit -m "feat: scan bvh clip library with tags and external cache paths"
```

---

### Task 6: Qt 호환 계층

Max 2024 는 PySide2 에 `qtmax` 가 없고, 2026 은 PySide6 에 `qtmax` 가 있다. 이 차이를 한 파일에 가둔다.

**Files:**
- Create: `src/ui/studio/compat.py`
- Test: `tests/test_bvh_studio.py` (추가)

**Interfaces:**
- Produces:
  - `QtCore`, `QtGui`, `QtWidgets` — 재수출된 모듈
  - `BINDING: str` — `"PySide6"` 또는 `"PySide2"`
  - `max_main_window() -> object | None`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

PySide 는 Max 밖에는 없으므로, 테스트는 **모듈이 없을 때 명확히 실패하는지**만 확인한다.

```python
def test_compat_raises_clear_error_without_pyside() -> None:
    import importlib

    try:
        import PySide6  # noqa: F401
    except ImportError:
        try:
            import PySide2  # noqa: F401
        except ImportError:
            with pytest.raises(ImportError, match="3ds Max"):
                importlib.import_module("src.ui.studio.compat")
            return
    pytest.skip("PySide 가 있는 환경 — Max 내부에서 확인한다")
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_bvh_studio.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.ui.studio.compat'`

- [ ] **Step 3: 구현**

`src/ui/studio/compat.py`:

```python
"""PySide2(Max 2024) / PySide6(Max 2026) 차이를 흡수한다.

qtmax 는 Max 2026 에만 있다. 2024 의 site-packages 에는 pymxs, PySide2,
shiboken2 뿐이므로 부모 윈도우 탐색에 qtmax 를 전제할 수 없다.
"""

from typing import Optional

try:
    from PySide6 import QtCore, QtGui, QtWidgets

    BINDING = "PySide6"
except ImportError:  # pragma: no cover - 바인딩에 따라 갈린다
    try:
        from PySide2 import QtCore, QtGui, QtWidgets

        BINDING = "PySide2"
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "PySide2/PySide6 를 찾을 수 없습니다. 이 모듈은 3ds Max 안에서 실행해야 합니다."
        ) from exc

__all__ = ["QtCore", "QtGui", "QtWidgets", "BINDING", "max_main_window"]


def max_main_window() -> Optional[object]:
    """Max 메인 윈도우를 찾는다. 못 찾으면 None (부모 없이 띄운다)."""
    try:
        import qtmax  # Max 2026

        return qtmax.GetQMaxMainWindow()
    except Exception:
        pass

    app = QtWidgets.QApplication.instance()
    if app is None:
        return None
    for widget in app.topLevelWidgets():
        if widget.parent() is not None:
            continue
        if widget.metaObject().className() == "QmaxApplicationWindow":
            return widget
    for widget in app.topLevelWidgets():
        if widget.parent() is None and widget.isWindow() and widget.inherits("QMainWindow"):
            return widget
    return None
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_bvh_studio.py -q`
Expected: PASS — 20 passed (PySide 가 없는 시스템 Python 에서는 ImportError 경로가 검증된다)

- [ ] **Step 5: 커밋**

```bash
git add src/ui/studio/compat.py tests/test_bvh_studio.py
git commit -m "feat: add pyside2/6 compat layer without qtmax dependency"
```

---

### Task 7: 포즈 데이터와 캐시

**그리기는 이 태스크에 없다.** 좌표만 만들어 캐시하고, 화면에 그리는 일은 JS 가
한다 (Task 10). 그래서 이 모듈은 Qt 를 전혀 import 하지 않는다 — 시스템 파이썬에서
전부 테스트된다.

**Files:**
- Create: `src/ui/studio/thumb.py`
- Test: `tests/test_bvh_studio.py` (추가)

**Interfaces:**
- Consumes: `fk`, `bones`, `project`, `bounds` (Task 4), `cache_path` (Task 5)
- Produces:
  - `SAMPLE_FRAMES: int = 12`
  - `sample_indices(total: int, count: int = SAMPLE_FRAMES) -> list[int]`
  - `build_pose_data(clip_path: str) -> dict` — 캐시에 저장되는 순수 데이터
  - `load_pose_data(clip_path: str, cache_dir: str) -> dict` — 캐시 유효하면 재사용

`build_pose_data` 의 반환값은 이미 JSON 직렬화 가능하다 (캐시가 `json.dump` 로
저장한다). 이 dict 를 그대로 `QWebChannel` 너머 JS 에 건네 canvas 로 그린다.
파이썬 쪽에 `QPixmap` 렌더러를 두지 않는다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
from src.ui.studio.thumb import sample_indices


def test_sample_indices_spreads_evenly() -> None:
    assert sample_indices(12, 12) == list(range(12))


def test_sample_indices_downsamples() -> None:
    out = sample_indices(100, 12)
    assert len(out) == 12
    assert out[0] == 0
    assert out[-1] == 99
    assert all(b > a for a, b in zip(out, out[1:]))


def test_sample_indices_short_clip() -> None:
    assert sample_indices(3, 12) == [0, 1, 2]


def test_sample_indices_single_frame() -> None:
    assert sample_indices(1, 12) == [0]
```

- [ ] **Step 2: 실패를 확인한다**

Run: `python -m pytest tests/test_bvh_studio.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.ui.studio.thumb'`

- [ ] **Step 3: 구현**

`src/ui/studio/thumb.py`. 이 모듈은 Qt 를 import 하지 않는다 — 전부 순수 파이썬이다.

```python
"""클립 골격을 그려 썸네일과 프리뷰를 만든다."""

import json
import os
from typing import Any

from src.helpers.bvh import parse_bvh
from src.ui.studio.library import cache_path
from src.ui.studio.skeleton import bones, bounds, fk

SAMPLE_FRAMES = 12
_CACHE_VERSION = 1


def sample_indices(total: int, count: int = SAMPLE_FRAMES) -> list[int]:
    """클립 전체에서 균등 간격으로 프레임 인덱스를 고른다."""
    if total <= 0:
        raise ValueError(f"total must be positive, got {total}")
    if total <= count:
        return list(range(total))
    last = total - 1
    return [round(i * last / (count - 1)) for i in range(count)]


def build_pose_data(clip_path: str) -> dict[str, Any]:
    """클립을 파싱해 샘플 프레임의 조인트 좌표를 뽑는다 (Qt 불필요)."""
    text = open(clip_path, encoding="utf-8", errors="replace").read()
    bvh = parse_bvh(text)
    indices = sample_indices(len(bvh.frames))
    poses = [fk(bvh, i) for i in indices]
    every = [p for pose in poses for p in pose.values()]
    return {
        "version": _CACHE_VERSION,
        "mtime": os.path.getmtime(clip_path),
        "bones": bones(bvh.root),
        "poses": [{k: list(v) for k, v in pose.items()} for pose in poses],
        "bounds": list(bounds(every, 0.0)),
        "frames": len(bvh.frames),
        "frame_time": bvh.frame_time,
    }


def load_pose_data(clip_path: str, cache_dir: str) -> dict[str, Any]:
    """캐시가 유효하면 재사용하고, 아니면 다시 계산해 저장한다."""
    path = cache_path(clip_path, cache_dir)
    try:
        with open(path, encoding="utf-8") as handle:
            cached = json.load(handle)
        if (
            cached.get("version") == _CACHE_VERSION
            and cached.get("mtime") == os.path.getmtime(clip_path)
        ):
            return cached
    except (OSError, ValueError):
        pass

    data = build_pose_data(clip_path)
    os.makedirs(cache_dir, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
    except OSError:
        pass  # 캐시 실패는 치명적이지 않다
    return data
```

- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_bvh_studio.py -q`
Expected: PASS — 24 passed

- [ ] **Step 5: 커밋**

```bash
git add src/ui/studio/thumb.py tests/test_bvh_studio.py
git commit -m "feat: cache sampled skeleton poses with an external json cache"
```

---

### Task 8: Max 브리지 (pymxs 포팅)

`bvh_biped_ui.ms` 의 임포트·ArmSpace·Mixer 로직을 파이썬으로 옮긴다. 원본 `.ms` 는 그대로 둔다.

**Files:**
- Create: `src/ui/studio/maxbridge.py`
- Reference: `maxscript/bvh_biped_ui.ms:112-207` (읽기만)

**Interfaces:**
- Consumes: `prepare_for_biped` (Task 2)
- Produces:
  - `import_clip(src_path, bip_name, convert, x_offset, speed=1.0, trim=(0.0,1.0), time_map=None, mirror=False) -> str`
  - `scene_bipeds() -> list[str]`
  - `apply_arm_space(bip_name: str, points: Sequence[tuple[int, float]]) -> str`
  - `send_to_mixer(bip_name: str, clips_dir: str) -> str`
  - 모두 `"OK: ..."` 또는 `"ERROR: ..."` 문자열을 돌려준다 (원본 `.ms` 관례 유지)

- [ ] **Step 1: 원본 로직을 확인한다**

Read: `maxscript/bvh_biped_ui.ms:112-207`. 다음 세 가지를 반드시 보존한다.

1. `loadMocapFile` 실패 시 생성한 바이패드를 `delete` 한다.
2. `saveBipFile` 전에 `mixerMode` 를 끄고, 피겨 모드면 거부한다.
3. `ArmSpace` 레이어는 적용 후 **현재 레이어로 남긴다**. 바이패드는 현재 레이어까지 합성하므로 0 번으로 되돌리면 오프셋이 사라진다.

- [ ] **Step 2: 구현**

`src/ui/studio/maxbridge.py`:

```python
"""Max 안에서만 동작하는 pymxs 호출 모음.

bvh_biped_ui.ms 의 검증된 절차를 파이썬으로 옮긴 것이다. 원본 .ms 는 그대로
남아 있고 이 모듈과 독립적으로 동작한다.
"""

import os
from typing import Optional, Sequence

from src.helpers.bvh import DEFAULT_BIPED_PRUNE, has_upright_spine, prepare_for_biped


def _rt():
    from pymxs import runtime

    return runtime


def scene_bipeds() -> list[str]:
    """씬의 바이패드 루트 노드 이름 목록."""
    rt = _rt()
    return [
        obj.name
        for obj in rt.objects
        if rt.classOf(obj.controller) == rt.Vertical_Horizontal_Turn
    ]


def convert_clip(
    src_path: str,
    x_offset: float = 0.0,
    speed: float = 1.0,
    trim: tuple[float, float] = (0.0, 1.0),
    time_map: Optional[Sequence[float]] = None,
) -> tuple[str, bool]:
    """*_biped.bvh 를 만들고 (경로, 직립여부) 를 돌려준다."""
    text = open(src_path, encoding="utf-8", errors="replace").read()
    converted = prepare_for_biped(
        text,
        prune=DEFAULT_BIPED_PRUNE,
        offset=(x_offset, 0.0, 0.0),
        speed=speed,
        trim_range=trim,
        time_map=time_map,
    )
    stem, _ = os.path.splitext(src_path)
    out_path = f"{stem}_biped.bvh"
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(converted)
    return out_path, has_upright_spine(text)


def import_clip(
    src_path: str,
    bip_name: str,
    convert: bool,
    x_offset: float,
    speed: float = 1.0,
    trim: tuple[float, float] = (0.0, 1.0),
    time_map: Optional[Sequence[float]] = None,
    mirror: bool = False,
) -> str:
    """바이패드를 만들고 클립을 올린다."""
    if not os.path.isfile(src_path):
        return f"ERROR: file not found: {src_path}"

    load_path = src_path
    upright = True
    if convert:
        try:
            load_path, upright = convert_clip(src_path, x_offset, speed, trim, time_map)
        except Exception as exc:
            return f"ERROR: convert failed: {exc}"

    rt = _rt()
    bip = rt.biped.createNew(170, -90, rt.Point3(0, 0, 0))
    if bip is None:
        return "ERROR: biped.createNew failed"
    if bip_name:
        bip.name = bip_name

    controller = bip.transform.controller
    old_quiet = rt.setQuietMode(True)
    ok = False
    try:
        ok = rt.biped.loadMocapFile(controller, load_path)
    except Exception:
        ok = False
    finally:
        rt.setQuietMode(old_quiet)

    if not ok:
        try:
            rt.delete(bip)
        except Exception:
            pass
        return f"ERROR: loadMocapFile rejected {load_path}"

    if mirror:
        try:
            rt.biped.mirror(controller)
        except Exception:
            pass

    msg = f"OK: {bip.name}"
    if not upright:
        msg += " | 경고: T포즈 골격이 아님(_tpose 파일 권장) — 자세가 틀어질 수 있음"
    return msg


def apply_arm_space(bip_name: str, points: Sequence[tuple[int, float]]) -> str:
    """ArmSpace 레이어에 시각별 팔 벌림 키를 찍는다.

    points 는 (프레임, 각도) 쌍이다. 원본 .ms 는 프레임 0 에 키 하나만
    찍었고, 여기서는 같은 API 로 여러 시각에 찍는다.
    """
    rt = _rt()
    bip = rt.getNodeByName(bip_name)
    if bip is None:
        return f"ERROR: 바이패드를 찾을 수 없음: {bip_name}"
    controller = bip.transform.controller
    if rt.classOf(controller) != rt.Vertical_Horizontal_Turn:
        return f"ERROR: not a biped root: {bip_name}"

    for i in range(int(rt.biped.numLayers(controller)), 0, -1):
        if rt.biped.getLayerName(controller, i) == "ArmSpace":
            rt.biped.deleteLayer(controller, i)

    if not points or all(deg == 0 for _, deg in points):
        return f"OK: {bip_name} 팔 간격 없음"

    layer_index = int(rt.biped.numLayers(controller)) + 1
    rt.biped.createLayer(controller, layer_index, "ArmSpace")
    rt.biped.setCurrentLayer(controller, layer_index)

    left = rt.biped.getNode(controller, rt.Name("larm"), link=2)
    right = rt.biped.getNode(controller, rt.Name("rarm"), link=2)

    for frame, degrees in points:
        rt.sliderTime = frame
        with _animate(rt):
            rt.rotate(left, rt.angleaxis(degrees, rt.Point3(0, 0, 1)))
            rt.rotate(right, rt.angleaxis(-degrees, rt.Point3(0, 0, 1)))
            rt.biped.addNewKey(left.controller, frame)
            rt.biped.addNewKey(right.controller, frame)

    # ArmSpace 를 현재 레이어로 남긴다 — 0 번으로 되돌리면 오프셋이 사라진다
    return f"OK: {bip_name} 팔 간격 키 {len(points)}개"


class _animate:
    """``animate on`` 블록의 파이썬 대응."""

    def __init__(self, rt) -> None:
        self._rt = rt

    def __enter__(self) -> None:
        self._rt.animate = True

    def __exit__(self, *exc: object) -> None:
        self._rt.animate = False


def send_to_mixer(bip_name: str, clips_dir: str) -> str:
    """현재 모션을 .bip 으로 저장하고 Motion Mixer 에 올린다."""
    rt = _rt()
    bip = rt.getNodeByName(bip_name)
    if bip is None:
        return f"ERROR: 바이패드를 찾을 수 없음: {bip_name}"
    controller = bip.transform.controller
    if rt.classOf(controller) != rt.Vertical_Horizontal_Turn:
        return f"ERROR: not a biped root: {bip_name}"
    if controller.figureMode:
        return f"ERROR: 피겨 모드를 먼저 해제하세요: {bip_name}"

    os.makedirs(clips_dir, exist_ok=True)
    bip_file = os.path.join(clips_dir, f"{bip_name}.bip")

    if controller.mixerMode:
        controller.mixerMode = False
    saved = False
    try:
        saved = rt.biped.saveBipFile(controller, bip_file)
    except Exception:
        saved = False
    if not saved:
        return f"ERROR: saveBipFile failed: {bip_file}"

    controller.mixerMode = True
    mixer = controller.mixer
    if mixer.numTrackgroups == 0:
        rt.appendTrackgroup(mixer)
    track = rt.getTrack(rt.getTrackgroup(mixer, 1), 1)
    ok = False
    try:
        ok = rt.appendClip(track, bip_file, False, 0)
    except Exception:
        ok = False
    if not ok:
        return f"ERROR: appendClip failed: {bip_file}"

    rt.theMixer.updateDisplay()
    rt.theMixer.showMixer()
    return f"OK: {bip_name} → Mixer"
```

- [ ] **Step 3: 문법을 확인한다**

Max 밖에서는 `pymxs` 가 없어 실행할 수 없다. 문법 오류만 확인한다.

Run: `python -c "import ast,sys; ast.parse(open('src/ui/studio/maxbridge.py',encoding='utf-8').read()); print('syntax ok')"`
Expected: `syntax ok`

- [ ] **Step 4: 커밋**

```bash
git add src/ui/studio/maxbridge.py
git commit -m "feat: port biped import and mixer logic to pymxs"
```

---

### Task 9: QWebEngineView 호스트와 채널 브리지 — **게이트**

UI 를 다 만들기 전에 "Max 안에서 웹뷰가 뜨고 JS↔파이썬이 오간다" 를 먼저 증명한다.

**위험 요소 (이 태스크가 존재하는 이유)** — `QtWebEngine` 은 `QApplication` 이 만들어지기
**전에** `Qt.AA_ShareOpenGLContexts` 가 켜져 있어야 한다. Max 안에서는 QApplication 이
이미 살아 있으므로 나중에 켤 수 없다. 조건이 안 맞으면 웹뷰가 흰 화면이거나, 렌더링이
깨지거나, 최악은 Max 가 죽는다. **작은 스모크 페이지로 먼저 확인하고, 통과해야 Task 10
으로 간다.**

#### 실측 결과 (2026-08-06, Max 2026 빌드 28000 / PySide6 6.5.3)

게이트를 실제로 돌려서 알아낸 것들이다. **추측이 아니라 측정값이다.**

1. **`AA_ShareOpenGLContexts` 는 이미 켜져 있다** (`share_gl_contexts: true`).
   걱정했던 그 문제는 Max 2026 에서 해당 없음.

2. **런타임은 있는데 Qt 가 못 찾는다.** Max 2026 은 `QtWebEngineProcess.exe` 와
   `Qt6WebEngine*.dll` 을 **설치 루트**에, 리소스를 `resources\`, 로케일을
   `qt\translations\qtwebengine_locales\` 에 둔다. 그런데 Qt 의
   `LibraryExecutablesPath` 는 `bin\` 을 가리킨다. 그래서 기본 탐색이 실패하고
   **창은 뜨지만 `loadFinished` 가 영영 오지 않는다** (38초간 확인).
   → `webhost.configure_webengine_paths()` 가 `QTWEBENGINEPROCESS_PATH`,
   `QTWEBENGINE_RESOURCES_PATH`, `QTWEBENGINE_LOCALES_PATH` 를 잡아준다.
   이걸 넣으면 **페이지가 로드되고 실제로 그려진다** (캡처 색 21종, 빈 화면 아님).

3. **`QWebChannel.registerObject` 는 소유권을 가져가지 않는다.** 브리지를 지역
   변수로만 들고 있으면 파이썬이 수거하며 C++ QObject 까지 파괴한다. 증상이
   고약하다 — 채널은 `connected` 이고 JS 쪽 `bridge` 객체도 truthy 인데 **슬롯
   호출 콜백이 영영 안 불린다.** 에러도 안 난다.
   → `WebHost` 가 `bridge.setParent(self)` 로 붙든다.

4. **한 Max 세션에서 웹뷰를 반복 생성/파괴하면 Max 가 죽는다.** 세 번째 시도에서
   프로세스가 종료됐다 (PID 4612 -> 35060).
   → **웹뷰는 세션당 한 번만 만든다.** 다시 열 때는 새로 만들지 말고 기존 창을
   `show()` 하고 `load_page()` 로 페이지만 갈아끼운다. Task 11 의 `launch()` 도
   이 규칙을 따라야 한다 — 닫고 다시 만들면 안 된다.

5. `qrc:///qtwebchannel/qwebchannel.js` 를 `<script src>` 로 부르는 대신 리소스를
   읽어 `QWebEngineScript` 로 주입하는 방식은 **동작한다**
   (`qwebchannel_present: true`).

#### 게이트 통과 (2026-08-06)

새 Max 2026 세션에서 게이트를 한 번 돌려 **`verdict: PASS`** 를 받았다.

```
load_finished_ok : true
image            : distinct_colors 21, dominant_share 0.882, looks_blank false
page             : page_ran / qwebchannel_present / channel_connected
                   / ping_ok / canvas_drawn = 전부 true, qt_version "6.5.3"
```

`qt_version` 이 채워졌다는 것은 **파이썬 슬롯의 반환값이 JS 까지 실제로 돌아왔다**는
뜻이다 — 3번 소유권 수정이 효과가 있었음을 확인한다. 캡처(`.smoke/view.png`)에서
한글 렌더와 canvas 드로잉도 정상이다. Max 는 살아남았다.

**결론: 웹 UI 경로로 진행해도 된다.** Task 10 을 시작할 수 있다.

**Files:**
- Create: `src/ui/studio/bridge.py`
- Create: `src/ui/studio/webhost.py`
- Create: `src/ui/studio/web/smoke.html`
- Create: `maxscript/bvh_studio_smoke.ms`

**Interfaces:**
- Consumes: `compat` (Task 6), `library.scan` (Task 5), `thumb.load_pose_data` (Task 7)
- Produces:
  - `StudioBridge(QtCore.QObject)` — JS 에 노출되는 슬롯 모음. 반환은 **JSON 문자열**.
    - `ping(text: str) -> str` — 스모크용 왕복 확인
    - `list_clips(folder: str) -> str`
    - `pose_data(clip_path: str) -> str`
  - `WebHost(QtWidgets.QWidget)` — `QWebEngineView` + `QWebChannel` 배선
    - `load_page(filename: str) -> None`

- [ ] **Step 1: 브리지**

`src/ui/studio/bridge.py`. `QtCore.QObject` 를 상속하고 `@QtCore.Slot(str, result=str)`
로 노출한다.

- **모든 슬롯은 예외를 잡아** `{"ok": false, "error": "..."}` 로 돌려준다. 파이썬 예외가
  채널 너머로 새면 JS 쪽 콜백이 조용히 안 불리고 페이지가 멈춘 것처럼 보인다.
- 성공은 `{"ok": true, "data": ...}` 로 통일한다.
- 포즈 계산은 슬롯 안에서 동기로 한다. 첫 호출만 느리고 이후는 캐시(Task 7)다.

- [ ] **Step 2: 호스트**

`src/ui/studio/webhost.py`.

- `QWebEngineView` 와 `QWebChannel` 을 만들고 `channel.registerObject("bridge", bridge)`
- 페이지는 `QtCore.QUrl.fromLocalFile(<절대경로>)` 로 연다
- JS 쪽 `qwebchannel.js` 는 **Qt 내장 리소스** `qrc:///qtwebchannel/qwebchannel.js` 에서
  가져온다. 파일로 복사하지 않는다
- `QWebEngineView` import 실패를 잡아 **사람이 읽을 수 있는 메시지**로 바꾼다:
  Max 2024(PySide2)에는 QtWebEngine 이 없으므로 여기서 걸린다

- [ ] **Step 3: 스모크 페이지**

`src/ui/studio/web/smoke.html`. 한 파일에 전부 넣는다 (인라인 CSS/JS).

1. `qwebchannel.js` 를 불러 `bridge` 에 붙는다
2. `bridge.ping("hello")` 를 부르고 응답을 화면에 찍는다
3. `<canvas>` 에 선 하나와 원 하나를 그린다 — GPU 렌더 경로가 사는지 본다

- [ ] **Step 4: 런처**

`maxscript/bvh_studio_smoke.ms`. Task 10 의 `bvh_studio.ms` 와 같은 형태로,
`src.ui.studio.webhost` 를 import 해 스모크 페이지를 띄운다.

- [ ] **Step 5: Max 2026 에서 확인한다 — 여기가 게이트다**

3ds Max 2026 을 띄우고 `maxscript/bvh_studio_smoke.ms` 를 실행한다.

1. 창이 Max 위에 뜬다
2. **흰 화면이 아니다** (내용이 실제로 그려진다)
3. `ping` 왕복 결과 문자열이 보인다
4. canvas 의 선과 원이 보인다
5. 창을 닫고 다시 실행해도 Max 가 죽지 않는다

**하나라도 실패하면 Task 10 을 시작하지 말고 멈춘다.** 폴백 경로를 먼저 검토한다 —
`QWebEngineView` 대신 로컬 HTTP 서버(`http.server`)로 페이지를 서빙하고 외부 브라우저로
열며, 통신은 `QtWebSockets`(PySide6 에 포함)로 한다. 창이 Max 밖으로 나가는 대신
HTML/CSS 자유도는 동일하고, **Task 10 의 HTML/JS 는 그대로 재사용된다.**

- [ ] **Step 6: 커밋**

```bash
git add src/ui/studio/bridge.py src/ui/studio/webhost.py src/ui/studio/web/smoke.html maxscript/bvh_studio_smoke.ms
git commit -m "feat: add qwebchannel bridge and webengine host with smoke page"
```

---

### Task 10: 웹 UI — 그리드, 타임라인, 곡선

화면은 전부 HTML/CSS/JS 다. 파이썬은 데이터만 넘긴다 (Task 9 의 브리지).

**Files:**
- Create: `src/ui/studio/web/index.html`
- Create: `src/ui/studio/web/style.css`
- Create: `src/ui/studio/web/app.js`
- Create: `src/ui/studio/web/draw.js`

**Interfaces:**
- Consumes: `bridge` (Task 9, JS 전역), `build_time_map` 의 제어점 규약 (Task 3)
- Produces: 브라우저에서 동작하는 UI. 파이썬 쪽 새 심볼 없음.

#### 디자인 교체 지점 — DOM 계약

`style.css` 는 **자리표시자다.** 피그마 디자인이 확정되면 이 파일만 교체한다.
`app.js` 는 아래 선택자로만 DOM 을 찾으므로, **디자인 작업은 이 이름들을 유지해야
한다.** 새 요소를 더하는 것은 자유다.

| 선택자 | 역할 |
|---|---|
| `.clip-grid` | 클립 카드 컨테이너 |
| `.clip-card[data-stem]` | 카드 하나. `data-stem` 이 클립 식별자 |
| `.clip-card canvas` | 카드 썸네일 |
| `.clip-card.is-broken` | 파싱 실패 클립 (목록에서 빼지 않는다) |
| `.preview canvas` | 큰 프리뷰 |
| `[data-action="azimuth"]` | 프리뷰 방위각 전환 |
| `.timeline` | 트림 막대 |
| `.trim-handle[data-side="start"\|"end"]` | 트림 핸들 |
| `.curve[data-kind="speed"\|"armspace"]` | 곡선 편집기 (같은 컴포넌트를 둘로 쓴다) |
| `[data-field="name"\|"spacing"\|"convert"\|"mirror"\|"biped"\|"search"\|"folder"]` | 입력 |
| `[data-action="import"\|"refresh"\|"sync"\|"mixer"]` | 버튼 |
| `.status` | 하단 상태 한 줄. 모달을 쓰지 않는다 |

- [ ] **Step 1: 그리드**

카드마다 `<canvas>` 를 두고 `bridge.pose_data` 로 받은 좌표를 `draw.js` 로 그린다.

- **`requestAnimationFrame` 루프는 문서 전체에 하나만 둔다.** 그 루프가 호버 중인
  카드 하나의 프레임 인덱스만 돌린다. 카드마다 루프나 타이머를 두면 클립이 늘수록 죽는다
- 파싱에 실패한 클립은 **목록에서 빼지 말고** `.is-broken` 을 붙이고 사유를 `title` 에
  넣는다. 클립 하나가 목록 전체를 죽이면 안 된다
- canvas 는 `devicePixelRatio` 를 곱해 백킹 스토어를 잡는다. 안 하면 흐릿하게 나온다
- **하이브리드 썸네일**: 브리지가 그 클립의 렌더된 PNG 경로를 주면 canvas 대신 `<img>`
  를 쓴다 (Task 13). 없으면 canvas 로 그린다. **PNG 가 없다고 빈칸을 만들지 않는다** —
  라이브러리는 Max 없이도 즉시 열려야 한다

- [ ] **Step 2: 타임라인**

`.timeline` 에 막대와 좌우 핸들을 그린다. `trim_start < trim_end` 를 항상 유지하고
최소 간격을 한 프레임으로 강제한다. 포인터 이벤트는 `setPointerCapture` 로 잡아
커서가 막대를 벗어나도 드래그가 끊기지 않게 한다.

- [ ] **Step 3: 곡선**

가로축 출력 시간, 세로축 원본 시간. 제어점 단조성은 **드래그 단계에서 이웃 값으로
클램프**해 강제한다 — `build_time_map` 이 `ValueError` 를 던지기 전에 UI 에서 막는다.

같은 컴포넌트를 속도 곡선과 팔 간격 곡선 양쪽에 쓴다. 세로축 라벨과 범위만 인자로
받는다 (`data-kind` 로 구분).

- [ ] **Step 4: 브라우저에서 먼저 확인한다**

Max 없이 크롬에서 `index.html` 을 직접 열어 확인한다. `bridge` 가 없으면 내장된
더미 데이터로 동작하게 해 둔다 — **디자인 반복을 Max 재시작 없이 돌리기 위한 것이다.**

Run: `python -c "import pathlib;[pathlib.Path(p).read_text(encoding='utf-8') for p in ['src/ui/studio/web/index.html','src/ui/studio/web/app.js','src/ui/studio/web/draw.js','src/ui/studio/web/style.css']];print('files ok')"`
Expected: `files ok`

- [ ] **Step 5: 커밋**

```bash
git add src/ui/studio/web/
git commit -m "feat: add web ui for clip grid, timeline and curve editor"
```

---

### Task 11: 조립과 Max 실검증

**Files:**
- Create: `src/ui/studio/window.py`
- Create: `src/ui/studio/launch.py`
- Create: `maxscript/bvh_studio.ms`

**Interfaces:**
- Consumes: Task 3~10 전부
- Produces: `StudioWindow(QtWidgets.QWidget)`, `launch() -> StudioWindow`

- [ ] **Step 1: 창 조립**

`src/ui/studio/window.py`. Task 9 의 `WebHost` 를 감싸고 `index.html` 을 띄운다.
배치는 HTML 이 정하므로 파이썬은 창 제목·크기·부모만 다룬다.

브리지에 나머지 슬롯을 채운다 (Task 9 에서 세 개만 만들었다):

- `import_clip(payload_json: str) -> str` — 이름, 변환 여부, 배치 간격, 미러, 트림,
  속도 곡선 제어점을 한 dict 로 받는다
- `scene_bipeds() -> str`, `apply_arm_space(payload_json: str) -> str`,
  `send_to_mixer(payload_json: str) -> str`
- `sync_from_github(folder: str) -> str`

규칙은 원본과 같게 유지한다:

- 폴더는 원본과 같은 INI 를 읽되 **키를 분리한다** (`bvh_studio.ini`). 원본 설정을 덮어쓰지 않는다
- 캐시 폴더는 `os.path.join(<userScripts>, "bvh_studio_cache")`
- 배치 간격은 세션 임포트 횟수 × 간격값. 원본 `importCount` 동작과 같다
- 곡선이 평평하면 `time_map` 대신 `speed` 를 넘겨 무손실 경로를 탄다

- [ ] **Step 2: 진입점**

`src/ui/studio/launch.py`:

```python
"""3ds Max 안에서 BVH Studio 창을 띄운다."""

from typing import Optional

_WINDOW: Optional[object] = None


def launch() -> object:
    global _WINDOW
    from src.ui.studio.compat import max_main_window
    from src.ui.studio.window import StudioWindow

    if _WINDOW is not None:
        try:
            _WINDOW.close()
        except Exception:
            pass

    _WINDOW = StudioWindow(parent=max_main_window())
    _WINDOW.show()
    return _WINDOW


if __name__ == "__main__":
    launch()
```

- [ ] **Step 3: MAXScript 런처**

`maxscript/bvh_studio.ms`:

```maxscript
-- bvh_studio.ms — BVH Studio (Mixamo형 클립 브라우저) 실행
--
-- 원본 bvh_biped_ui.ms 와 공존한다. 로직은 전부 src/ui/studio/ 아래 파이썬에 있다.

(
	local thisFile = getThisScriptFilename()
	local repoRoot = if thisFile != undefined then
		(pathConfig.removePathLeaf (pathConfig.removePathLeaf thisFile))
	else
		@"C:\work\Ai\3dsmax-mcp"
	local repo = substituteString repoRoot "\\" "/"

	local py = "import sys\n"
	py += "sys.path.insert(0, r'" + repo + "')\n"
	py += "import importlib\n"
	py += "import src.ui.studio.launch as _launch\n"
	py += "importlib.reload(_launch)\n"
	py += "_launch.launch()\n"
	python.Execute py
)
```

- [ ] **Step 4: Max 안에서 검증한다**

3ds Max 2026 을 띄우고 `maxscript/bvh_studio.ms` 를 실행한다. 확인 항목:

1. 창이 Max 메인 윈도우 위에 뜬다 (뒤로 숨지 않는다)
2. `C:\work\Ai\bvh-library` 의 클립 28개 중 `_biped` 가 아닌 것만 그리드에 나온다
3. 썸네일이 움직인다. 두 번째 실행 시 즉시 뜬다 (캐시)
4. 속도 곡선을 꺾은 뒤 임포트하면 프레임 수가 곡선대로 달라진다
5. 트림 핸들을 좁히면 임포트 길이가 줄어든다
6. 팔 간격 곡선의 제어점 시각마다 팔 벌림이 실제로 변한다
7. Motion Mixer 전송이 동작한다
8. 라이브러리 폴더에 캐시 파일이 생기지 않았다

```bash
ls C:/work/Ai/bvh-library     # .cache 폴더가 없어야 한다
```

- [ ] **Step 5: Max 2024 폴백을 확인한다**

2024 의 PySide2 에는 QtWebEngine 이 없으므로 **스튜디오는 2024 에서 동작하지 않는 것이
정상이다.** 확인할 것은 두 가지다.

1. 2024 에서 `bvh_studio.ms` 를 실행하면 **사람이 읽을 수 있는 메시지**로 거절한다
   (스택 트레이스가 아니라 "Max 2026 이 필요합니다" 류). Task 9 Step 2 의 import 가드다
2. 2024 에서 **원본 `bvh_biped_ui.ms` 는 그대로 동작한다** — 2024 사용자의 유일한 경로다

- [ ] **Step 6: 원본이 그대로인지 확인한다**

```bash
git -C C:/work/Ai/3dsmax-mcp diff --stat master -- maxscript/bvh_biped_ui.ms
```
Expected: 출력 없음 (원본 무수정)

Run: `python -m pytest tests/ -q`
Expected: 전체 통과

- [ ] **Step 7: 커밋**

```bash
git add src/ui/studio/window.py src/ui/studio/launch.py maxscript/bvh_studio.ms
git commit -m "feat: assemble bvh studio window and maxscript launcher"
```

---

### Task 12: 재생 프레임과 대표 프레임

> **정정 (2026-08-06)** — 이 태스크는 처음에 "포즈 차이가 가장 큰 프레임 12장"을
> 고르도록 썼고 그렇게 구현했다(최원점 표집). **틀렸다.** 카드 썸네일은 정지 그림이
> 아니라 호버하면 **재생되는** 것이라, 극점만 고르면 프레임 사이 시간 간격이
> 1~166프레임까지 들쭉날쭉해진다(실측 변동계수 0.72~1.90). 재생하면 멈췄다가
> 순간이동하는 것처럼 보인다.
>
> 대표성과 부드러움은 다른 요구다. **재생은 균등 간격 24장**으로 하고(변동계수
> 0.02~0.05, 원본 타이밍 보존), **대표 한 장만 따로 고른다**. 부수적으로 FK 후보
> 200개를 돌 필요가 없어져 클립당 1187ms -> 158ms 로 빨라졌다.
>
> `key_pose_indices` 는 제거했다. 유일한 호출자가 `build_pose_data` 였다.

**추가 배경 (2026-08-06)** — 지금 `sample_indices` 는 균등 간격으로 프레임을 고른다.
걷기처럼 주기적인 동작에서는 12장이 거의 같은 포즈로 나온다. 포즈 변화가 큰 프레임
(극점)을 골라야 썸네일 12장이 실제로 동작을 설명한다.

Max 가 필요 없다. FK 는 이미 있다 (Task 4).

**Files:**
- Modify: `src/ui/studio/thumb.py`
- Test: `tests/test_bvh_studio.py` (추가)

**Interfaces:**
- Consumes: `fk` (Task 4)
- Produces:
  - `pose_vector(pose: dict[str, tuple[float, float, float]]) -> tuple[float, ...]`
    — 루트 상대 좌표를 조인트 이름 정렬 순으로 편 벡터. 이동 성분을 빼야 제자리
      동작과 이동 동작이 같은 기준으로 비교된다
  - `pose_distance(a: Sequence[float], b: Sequence[float]) -> float` — 제곱 거리
  - `key_pose_indices(bvh: BvhFile, count: int = SAMPLE_FRAMES, max_candidates: int = 200) -> list[int]`
    — 최원점 표집(greedy farthest-point). 오름차순 정렬해서 돌려준다

**알고리즘** — 후보 프레임을 `max_candidates` 개로 균등 축소한 뒤(긴 클립에서 FK 비용을
묶는다), 0번 프레임에서 시작해 "이미 고른 것들과의 최소 거리가 최대인 프레임" 을 반복해
추가한다. 마지막에 인덱스를 오름차순 정렬해 재생 순서를 유지한다.

- `build_pose_data` 가 `sample_indices` 대신 이걸 쓴다
- **`_CACHE_VERSION` 을 2 로 올린다.** 안 올리면 예전 캐시가 그대로 살아 새 선택이 안 보인다
- `sample_indices` 는 지운다. 유일한 호출자가 `build_pose_data` 였고, 남기면 죽은 코드다

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_pose_vector_is_root_relative() -> None:
    a = {"Hips": (0.0, 0.0, 0.0), "Head": (0.0, 10.0, 0.0)}
    b = {"Hips": (100.0, 0.0, 0.0), "Head": (100.0, 10.0, 0.0)}
    assert pose_vector(a) == pytest.approx(pose_vector(b))


def test_pose_distance_zero_for_same_pose() -> None:
    v = pose_vector({"Hips": (0.0, 0.0, 0.0), "Head": (0.0, 10.0, 0.0)})
    assert pose_distance(v, v) == pytest.approx(0.0)


def test_key_pose_indices_are_sorted_and_unique() -> None:
    bvh = parse_bvh(TWO_JOINT)
    out = key_pose_indices(bvh, count=2)
    assert out == sorted(out)
    assert len(set(out)) == len(out)


def test_key_pose_indices_short_clip_returns_all() -> None:
    bvh = parse_bvh(TWO_JOINT)          # 2 프레임
    assert key_pose_indices(bvh, count=12) == [0, 1]


def test_key_pose_picks_the_extreme_frame() -> None:
    # 3 프레임 중 1번은 0번과 거의 같고 2번만 크게 다르다 -> 2번이 뽑혀야 한다
    bvh = parse_bvh(THREE_FRAME_ONE_EXTREME)
    assert key_pose_indices(bvh, count=2) == [0, 2]
```

- [ ] **Step 2: 실패를 확인한다** — `ImportError: cannot import name 'key_pose_indices'`
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과를 확인한다**

Run: `python -m pytest tests/test_bvh_helpers.py tests/test_bvh_studio.py -q`

- [ ] **Step 5: 커밋**

```bash
git add src/ui/studio/thumb.py tests/test_bvh_studio.py
git commit -m "feat: pick key poses by farthest-point sampling"
```

---

### Task 13: Max 뷰포트 썸네일 배치 (하이브리드)

**결정 (2026-08-06)** — 썸네일을 **하이브리드**로 간다. 캔버스 계산 썸네일이 기본값이고,
Max 배치가 만든 PNG 가 캐시에 있으면 그걸 우선 보여준다. 라이브러리는 Max 없이도 즉시
열리고, 배치를 한 번 돌리면 썸네일이 업그레이드된다.

**찍는 대상은 기본 바이패드다.** 캐릭터 메시 스키닝은 클립마다 리타게팅이 필요해 이번
범위에서 뺐다.

**Files:**
- Create: `src/ui/studio/maxthumb.py`
- Reference: `src/ui/studio/maxbridge.py` (Task 8), `library.cache_path` (Task 5)

**Interfaces:**
- Produces:
  - `thumb_png_path(clip_path: str, cache_dir: str, index: int) -> str`
  - `has_rendered_thumbs(clip_path: str, cache_dir: str) -> bool`
  - `render_clip_thumbs(clip_path: str, cache_dir: str, frames: Sequence[int]) -> list[str]`
  - `batch_render(folder: str, cache_dir: str) -> dict` — `{"ok": [...], "failed": {...}}`

- [ ] **Step 1: 안전장치를 먼저 만든다**

배치는 바이패드를 만들고 지우며 **씬을 휘젓는다.** 작업 중인 씬에서 돌면 안 된다.

- 시작 전 `rt.getSaveRequired()` 가 참이면 **거부한다** (강제 플래그를 받았을 때만 진행)
- 배치는 `rt.resetMaxFile(rt.Name("noPrompt"))` 로 빈 씬에서 시작한다
- 클립마다 끝나면 만든 바이패드를 지운다. 실패해도 지운다 (`try/finally`)

- [ ] **Step 2: 캡처**

`rt.viewport.getViewportDib()` 로 활성 뷰포트를 받아 PNG 로 저장한다.

- **알려진 함정: 뷰포트가 다른 창에 가려지면 캡처가 실패하거나 검게 나온다.** 배치 시작
  시 Max 를 전면으로 올리고, 캡처 결과가 전부 같은 색이면 실패로 처리해 `failed` 에 넣는다
- 캡처 전에 격자를 끄고(`rt.viewport.setGridVisibility`), 바이패드가 화면에 꽉 차게
  `rt.actionMan.executeAction(0, "310")` (Zoom Extents Selected) 를 쓴다
- 프레임 목록은 `key_pose_indices` (Task 12) 가 고른 것을 그대로 받는다

- [ ] **Step 3: 캐시 규약**

PNG 는 포즈 JSON 과 같은 캐시 폴더에 둔다. 라이브러리 폴더에는 **아무것도 쓰지 않는다.**

- 이름: `<clip 해시>_<index>.png` — 해시는 `library.cache_path` 와 같은 방식
- 원본 클립의 `mtime` 이 PNG 보다 새로우면 무효로 보고 다시 찍는다

- [ ] **Step 4: 문법 확인**

Max 밖에서는 `pymxs` 가 없어 실행할 수 없다. 문법만 본다.

Run: `python -c "import ast;ast.parse(open('src/ui/studio/maxthumb.py',encoding='utf-8').read());print('syntax ok')"`

- [ ] **Step 5: Max 안에서 검증한다**

1. 작업 중인 씬이 열려 있으면 배치가 거부한다
2. 빈 씬에서 28개 클립이 전부 처리된다
3. 캐시 폴더에 PNG 가 생기고 **라이브러리 폴더는 그대로다**
4. 검게 나온 PNG 가 없다 (가림 실패 검출이 동작한다)
5. 배치 후 UI 를 열면 계산 썸네일 대신 PNG 가 보인다

- [ ] **Step 6: 커밋**

```bash
git add src/ui/studio/maxthumb.py
git commit -m "feat: batch-render biped viewport thumbnails into the pose cache"
```

---

## 자체 검토 결과

**스펙 커버리지** — 스펙 각 절이 어느 태스크에 대응하는지:

| 스펙 | 태스크 |
|---|---|
| 4절 파일 구성 | Task 3(패키지), 7, 9, 10, 11 |
| 6절 속도 곡선 | Task 1(warp·언랩), 2(prepare 연결), 3(time_map 생성), 10(곡선 UI) |
| 7절 팔 간격 곡선 | Task 8(`apply_arm_space`), 10(곡선 컴포넌트 재사용) |
| 8절 썸네일·캐시 | Task 4(FK·투영), 5(캐시 경로), 7(포즈 데이터·캐시 IO), 10(canvas 렌더) |
| 9절 화면 구성 | Task 10(HTML 배치), 11(창 조립) |
| 10절 에러 처리 | Task 8(바이패드 삭제·피겨 모드), 9(브리지 예외→JSON), 10(파싱 실패 카드·상태줄) |
| 11절 테스트 | Task 1~7 의 pytest, Task 9 Step 5 의 스모크 게이트, Task 11 Step 4 의 Max 검증 |

**보완한 것** — 스펙 4절 파일 목록에 없던 `timemap.py` 를 추가했다. 곡선 위젯은 자동
테스트가 어려운데 time_map 생성은 곡선 기능의 핵심이라, 테스트 가능한 순수 모듈로
떼어냈다.

**UI 경로 변경 (2026-08-06)** — Task 7 착수 전에 뷰 계층을 Qt 위젯에서 HTML/CSS +
`QWebEngineView` 로 바꿨다. 피그마로 디자인을 잡기로 했고 QSS 로는 재현 한계가 있어서다.
Task 1~6 은 손대지 않았다. `render_pose` 하나를 뺐고, 옛 Task 9(위젯 3종)를 웹 호스트로,
옛 Task 10 을 웹 UI(신규 10)와 조립(신규 11)으로 나눴다.

**남은 한계**

- **웹 UI 에 자동 회귀 방어가 없다.** JS 테스트 관례가 이 저장소에 없다. Task 10 Step 4
  의 브라우저 확인과 Task 11 Step 4 의 Max 검증이 전부다.
- **스튜디오는 Max 2026 전용이다.** 2024 의 PySide2 에 QtWebEngine 이 없다. 2024 는
  원본 `bvh_biped_ui.ms` 로 남는다 — 그래서 원본 무수정 제약이 기능 요구사항이 됐다.
- **`QWebEngineView` 가 Max 안에서 뜨는지 아직 증명되지 않았다.** `AA_ShareOpenGLContexts`
  를 QApplication 생성 후에는 켤 수 없다. Task 9 Step 5 가 이걸 거르는 게이트고,
  실패 시 폴백(로컬 HTTP + 외부 브라우저 + `QtWebSockets`)이 같은 절에 적혀 있다.
