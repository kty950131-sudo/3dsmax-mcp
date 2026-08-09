# BVH Studio 2D Tracking Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** RTMW3D 추론 결과를 영상 위에서 관절별로 수정하고, 원본을 보존한 편집 JSON과 Biped 호환 BVH를 다시 생성한다.

**Architecture:** 추론기가 BVH용 3D 좌표와 영상 픽셀용 2D 좌표를 함께 기록한다. 순수 Python 편집 엔진이 수정·복사·감쇠 전파·원자적 저장·BVH 생성을 담당하고, Qt 브리지는 프레임 JPEG와 엔진 명령만 노출한다. HTML은 상태를 임시 보관하지 않고 브리지 응답을 정본으로 사용한다.

**Tech Stack:** Python 3.10, OpenCV, PySide6/QWebChannel, HTML Canvas/JavaScript, pytest, 3ds Max 2026.

## Global Constraints

- 원본 `<stem>_rtmw3d.json`과 기존 `<stem>_rtmw3d_tpose.bvh`는 덮어쓰지 않는다.
- 편집 출력명은 `<stem>_rtmw3d_edited.json`, `<stem>_rtmw3d_edited_tpose.bvh`, `<stem>_rtmw3d_edited_trace.json`으로 고정한다.
- BODY23 관절 이름과 순서는 `src.rtmw3d.motion.BODY23_NAMES`를 정본으로 사용한다.
- 화면 CSS 좌표가 아니라 원본 영상 픽셀 좌표를 저장한다.
- 2D 수정 시 기존 깊이 Z와 confidence를 보존한다.
- 다중 인물, optical flow, IK, 손가락·얼굴 편집, 3D 깊이 드래그는 구현하지 않는다.
- 기존 `maxscript/bvh_biped_ui.ms`는 수정하지 않는다.

---

## File Map

- `scripts/run-rtmw3d.py`: 추론 결과에 영상 크기와 smoothing 이전 2D 픽셀 좌표를 추가한다.
- `src/rtmw3d/motion.py`: 선택적 `image_size`·`image_keypoints`를 검증하고 읽는다.
- `src/ui/studio/tracking_editor.py`: 편집 세션과 저장/BVH 재생성의 단일 책임 모듈.
- `src/ui/studio/frame_reader.py`: OpenCV 랜덤 프레임 읽기와 12장 LRU JPEG 캐시.
- `src/ui/studio/bridge.py`: QWebChannel 슬롯과 세션 수명 관리.
- `src/ui/studio/web/studio_draft.html`: Figma 확정 디자인의 트래킹 화면과 Canvas 상호작용.
- `tests/test_rtmw3d_runner.py`: 추론 출력 계약.
- `tests/test_rtmw3d_motion.py`: 확장 스키마 검증.
- `tests/test_tracking_editor.py`: 수정·복사·전파·저장·BVH 생성.
- `tests/test_frame_reader.py`: 프레임 디코딩과 캐시.
- `tests/test_bridge.py`: 새 슬롯 JSON 계약.
- `tests/test_bvh_studio.py`: HTML 이벤트·필수 요소 회귀 계약.

---

### Task 1: RTMW3D 2D/3D 좌표 계약

**Files:**
- Modify: `scripts/run-rtmw3d.py`
- Modify: `src/rtmw3d/motion.py`
- Create: `tests/test_rtmw3d_runner.py`
- Modify: `tests/test_rtmw3d_motion.py`

**Interfaces:**
- Produces: `Rtmw3dMotion.image_size: tuple[int, int] | None`
- Produces: `Rtmw3dFrame.image_keypoints: tuple[tuple[float, float], ...] | None`
- JSON: top-level `image_size: {"width": int, "height": int}` and per-frame `image_keypoints: {joint: [x, y]}`.

- [ ] **Step 1: Write runner contract tests**

Add a pure helper to the runner and test without importing MMPose:

```python
def test_build_frame_keeps_image_pixels_separate_from_3d():
    raw = np.array([[10.0, 20.0, 0.5]] * 23)
    frame = build_frame_record(0, raw, np.ones(23), raw)
    assert frame["image_keypoints"]["nose"] == [10.0, 20.0]
    assert frame["keypoints"]["nose"] == [10.0, -20.0, -0.5]
```

- [ ] **Step 2: Run RED gate**

Run: `.venv/Scripts/python.exe -m pytest tests/test_rtmw3d_runner.py tests/test_rtmw3d_motion.py -q`

Expected: FAIL because `build_frame_record`, `image_size`, and `image_keypoints` do not exist.

- [ ] **Step 3: Implement the output helper**

Add to `scripts/run-rtmw3d.py`:

```python
def build_frame_record(index, raw_points, raw_scores, smoothed_points):
    body = np.stack((smoothed_points[:, 0], -smoothed_points[:, 1], -smoothed_points[:, 2]), axis=1)
    return {
        "index": index,
        "keypoints": {name: body[i].astype(float).tolist() for i, name in enumerate(BODY23_NAMES)},
        "image_keypoints": {name: raw_points[i, :2].astype(float).tolist() for i, name in enumerate(BODY23_NAMES)},
        "scores": {name: float(max(0.0, min(1.0, raw_scores[i]))) for i, name in enumerate(BODY23_NAMES)},
    }
```

Record the first frame width/height at top level. Do not change the existing 3D smoothing behavior.

- [ ] **Step 4: Extend the immutable motion types and parser**

Use:

```python
ImagePoint = tuple[float, float]

@dataclass(frozen=True)
class Rtmw3dFrame:
    index: int
    keypoints: tuple[Vector, ...]
    scores: tuple[float, ...]
    image_keypoints: tuple[ImagePoint, ...] | None = None

@dataclass(frozen=True)
class Rtmw3dMotion:
    source_video: str
    fps: float
    frames: tuple[Rtmw3dFrame, ...]
    image_size: tuple[int, int] | None = None
```

Reject incomplete image data: if one frame has `image_keypoints`, every frame must have exactly BODY23; require positive `image_size` with it. Accept legacy files where both fields are absent.

- [ ] **Step 5: Run GREEN gate and commit**

Run the same target and expect PASS. Commit:

```bash
git add scripts/run-rtmw3d.py src/rtmw3d/motion.py tests/test_rtmw3d_runner.py tests/test_rtmw3d_motion.py
git commit -m "feat: retain RTMW3D image-space keypoints"
```

---

### Task 2: Pure Tracking Edit Engine

**Files:**
- Create: `src/ui/studio/tracking_editor.py`
- Create: `tests/test_tracking_editor.py`

**Interfaces:**
- Consumes: `load_rtmw3d(path) -> Rtmw3dMotion`, `convert_rtmw3d_file(source, output) -> int`.
- Produces: `TrackingSession.open(path: Path) -> TrackingSession`.
- Produces: `frame(index: int) -> dict`, `set_point(frame_index, joint, x, y) -> dict`, `copy_to_next(frame_index, joint) -> dict`, `propagate(frame_index, end_frame, joint) -> dict`, `reset_point(frame_index, joint) -> dict`, `save(library: Path) -> dict`.

- [ ] **Step 1: Write edit behavior tests**

Create fixtures with three frames and assert:

```python
session.set_point(0, "left_wrist", 120.0, 80.0)
assert session.frame(0)["image_keypoints"]["left_wrist"] == [120.0, 80.0]
assert session.frame(0)["keypoints"]["left_wrist"][2] == original_z
assert original_path.read_bytes() == original_bytes
```

Add tests for invalid joint, out-of-range frame, non-finite coordinate, reset, next-frame copy, and manual-edit collision.

- [ ] **Step 2: Write exact propagation test**

For a correction offset `(30, -12)` at frame 0 propagated through frame 3, expect offsets `(30,-12)`, `(20,-8)`, `(10,-4)`, `(0,0)`. A `manual` edit at frame 2 must stop propagation before frame 2 and return `stopped_at: 2`.

- [ ] **Step 3: Run RED gate**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tracking_editor.py -q`

Expected: import failure for `src.ui.studio.tracking_editor`.

- [ ] **Step 4: Implement immutable source plus edit overlay**

Internally store only edits:

```python
@dataclass(frozen=True)
class PointEdit:
    x: float
    y: float
    kind: Literal["manual", "copied", "propagated"]

class TrackingSession:
    _source_path: Path
    _motion: Rtmw3dMotion
    _edits: dict[tuple[int, str], PointEdit]
```

`frame()` composes source data plus edits. Legacy JSON without `image_keypoints` uses `[x, -y]` from 3D as a read-only fallback and rejects `save()` with a message requiring re-extraction.

- [ ] **Step 5: Implement save and BVH regeneration**

Write JSON to a sibling temporary file, `Path.replace()` it to the final edited JSON, call the existing converter into a temporary BVH, then replace the final BVH. Write trace last with source/output paths, edit count, SHA-256 hashes, and timestamp. On converter failure, remove only temporary files.

- [ ] **Step 6: Run GREEN gate and commit**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tracking_editor.py tests/test_rtmw3d_motion.py -q`

Commit:

```bash
git add src/ui/studio/tracking_editor.py tests/test_tracking_editor.py
git commit -m "feat: add non-destructive tracking edit engine"
```

---

### Task 3: Random Frame Reader and QWebChannel Bridge

**Files:**
- Create: `src/ui/studio/frame_reader.py`
- Modify: `src/ui/studio/bridge.py`
- Create: `tests/test_frame_reader.py`
- Modify: `tests/test_bridge.py`

**Interfaces:**
- Produces: `VideoFrameReader(path: Path, cache_size: int = 12)`.
- Produces: `read(index: int) -> {index, width, height, jpeg_data_url}` and `close() -> None`.
- Bridge slots: `open_tracking(path)`, `tracking_frame(index)`, `tracking_set_point(payload_json)`, `tracking_copy_next(payload_json)`, `tracking_propagate(payload_json)`, `tracking_reset_point(payload_json)`, `tracking_save(library)`.

- [ ] **Step 1: Write frame reader tests**

Generate a three-frame 64×48 AVI with OpenCV. Assert exact dimensions, `data:image/jpeg;base64,` prefix, out-of-range rejection, cache hit without a second decode, and `close()` idempotency.

- [ ] **Step 2: Write bridge contract tests**

Inject fake session/reader factories into `StudioBridge`. Verify every slot returns the existing `{ok,data|error}` JSON envelope and that opening a second session closes the first reader.

- [ ] **Step 3: Run RED gate**

Run: `.venv/Scripts/python.exe -m pytest tests/test_frame_reader.py tests/test_bridge.py -q`

Expected: missing module/slots.

- [ ] **Step 4: Implement reader with bounded LRU**

Use `OrderedDict[int, dict]`; seek using `cv2.CAP_PROP_POS_FRAMES`, encode with `cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])`, and evict oldest entries over 12. Protect capture/cache with one `threading.Lock`.

- [ ] **Step 5: Implement bridge slots**

Keep `_tracking_session` and `_frame_reader` on `StudioBridge`. `open_tracking` obtains `source_video` from the session and returns frame count, FPS, image size, joint names, confidence summaries, and dirty state. Validate JSON payload keys before calling the engine.

- [ ] **Step 6: Run GREEN gate and commit**

Run the same target and expect PASS. Commit:

```bash
git add src/ui/studio/frame_reader.py src/ui/studio/bridge.py tests/test_frame_reader.py tests/test_bridge.py
git commit -m "feat: expose tracking sessions to BVH Studio"
```

---

### Task 4: Figma-Matched Tracking Editor UI

**Files:**
- Modify: `src/ui/studio/web/studio_draft.html`
- Modify: `tests/test_bvh_studio.py`

**Interfaces:**
- Consumes all Task 3 bridge slot names exactly.
- Produces JS functions: `openTracking(path)`, `renderTrackingFrame()`, `pickTrackingPoint(event)`, `saveTrackingPoint(event)`, `propagateTrackingPoint()`, `saveTrackingBvh()`.

- [ ] **Step 1: Write HTML contract tests**

Assert the document contains semantic elements and bridge calls:

```python
for token in [
    'data-view="tracking"', 'data-canvas="video"', 'data-canvas="pose3d"',
    'open_tracking', 'tracking_frame', 'tracking_set_point',
    'tracking_copy_next', 'tracking_propagate', 'tracking_save'
]:
    assert token in html
```

Also assert `Noto Sans KR`, `#48a9c5`, `#f5b642`, and a dirty-close handler are present.

- [ ] **Step 2: Run RED gate**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bvh_studio.py -q`

Expected: missing tracking view tokens.

- [ ] **Step 3: Add the fixed editor layout**

Implement the approved Figma structure: 68px top bar, 64px tool rail, two equal viewports, 256px inspector, bottom timeline. Preserve the existing library DOM and switch views by toggling `hidden`; do not delete current features.

- [ ] **Step 4: Implement coordinate-safe Canvas drawing**

Use contain-fit math:

```javascript
const scale = Math.min(canvasWidth / imageWidth, canvasHeight / imageHeight);
const offsetX = (canvasWidth - imageWidth * scale) / 2;
const offsetY = (canvasHeight - imageHeight * scale) / 2;
const screenToImage = (x, y) => [(x - offsetX) / scale, (y - offsetY) / scale];
```

Draw JPEG, BODY23 edges, confidence-colored joints, selected ring, and previous-position ghost in one canvas. Hit radius is 12 CSS pixels after device-pixel-ratio conversion.

- [ ] **Step 5: Implement frame/timeline actions**

Arrow keys step one frame; Space toggles playback; pointer drag updates locally and sends one save on pointer-up. Buttons call copy, propagation, reset, and final save. Playback stops while dragging. Manual edits and low-confidence frames get distinct timeline markers.

- [ ] **Step 6: Implement motion and accessibility**

Use 160–240ms `cubic-bezier(0.16, 1, 0.3, 1)` transitions and honor `prefers-reduced-motion`. Give every icon button `aria-label`, announce save/error status via the existing live region, and keep keyboard focus visible.

- [ ] **Step 7: Run GREEN gate and commit**

Run: `.venv/Scripts/python.exe -m pytest tests/test_bvh_studio.py tests/test_bridge.py -q`

Commit:

```bash
git add src/ui/studio/web/studio_draft.html tests/test_bvh_studio.py
git commit -m "feat: add BVH Studio tracking correction UI"
```

---

### Task 5: End-to-End Video Correction and 3ds Max Handoff

**Files:**
- Modify: `tests/test_video_jobs.py`
- Create: `tests/test_tracking_workflow.py`
- Modify: `docs/RTMW3D.md`
- Modify: `maxscript/bvh_studio2.ms` only if launcher instructions need text changes.

**Interfaces:**
- Consumes Task 1–4 outputs without new production abstractions.
- Produces retained smoke artifacts in a temporary test directory only.

- [ ] **Step 1: Write workflow test**

Create a two-frame synthetic video and matching RTMW3D JSON. Open a session, change `left_wrist`, propagate it, save, parse the edited BVH, and assert:

```python
assert source.read_bytes() == original
assert edited_json.is_file()
assert edited_bvh.is_file()
assert trace["edit_count"] == 2
assert parse_bvh(edited_bvh.read_text()).frames == 2
```

- [ ] **Step 2: Run RED gate**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tracking_workflow.py -q`

Expected: integration behavior incomplete until all task outputs are connected.

- [ ] **Step 3: Connect completed video jobs to the editor**

When `pollVideoJob()` receives `complete`, keep the generated BVH in the library and offer `트래킹 수정` using `job.rtmw3d_path`. Do not auto-open the editor; the user chooses whether to review.

- [ ] **Step 4: Document the workflow and limitations**

In `docs/RTMW3D.md`, document output names, original preservation, keyboard/pointer controls, legacy JSON re-extraction requirement, and explicit exclusions (multi-person, optical flow, IK, depth drag).

- [ ] **Step 5: Run full verification**

Run:

```powershell
Push-Location C:\work\Ai\3dsmax-mcp
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m compileall src scripts
git diff --check
Pop-Location
```

Expected: all tests pass, compileall exit 0, diff-check exit 0.

- [ ] **Step 6: Verify inside 3ds Max 2026**

Run `maxscript/bvh_studio2.ms`, load a short video, open `트래킹 수정`, move one wrist, save, import `<stem>_rtmw3d_edited_tpose.bvh`, and verify the Biped loads without creating an extra dummy root. Do not modify or save the user's existing scene during smoke verification; use a new empty scene or stop at the import confirmation if a clean scene is unavailable.

- [ ] **Step 7: Commit integration**

```bash
git add tests/test_video_jobs.py tests/test_tracking_workflow.py docs/RTMW3D.md maxscript/bvh_studio2.ms
git commit -m "test: verify video correction to Biped BVH workflow"
```
