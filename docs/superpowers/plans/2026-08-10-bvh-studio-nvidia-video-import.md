# BVH Studio NVIDIA Video Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** BVH Studio에서 로컬 영상을 추가하고 NVIDIA Maxine 34관절 결과를 Biped 호환 T-pose BVH로 변환해 라이브러리·SQLite·3ds Max 검수 흐름에 연결한다.

**Architecture:** `bvh_studio2.ms`는 기존 런처로 유지하고 Qt WebChannel bridge에 파일 선택·비동기 작업 슬롯을 추가한다. Maxine extractor는 별도 subprocess로 실행해 3ds Max UI를 막지 않으며, 독립적인 `nvidia_body34.py`가 보존된 JSON을 검증하고 BVH로 변환한다.

**Tech Stack:** 3ds Max 2026, MAXScript, Python 3.11(Max 내장), PySide6/QtWebEngine, NVIDIA Maxine AR SDK, standard library, pytest, SQLite

## Global Constraints

- 출력 파일명은 `<영상이름>_nvidia_tpose.bvh`다.
- 입력 확장자는 `.mp4`, `.mov`, `.mkv`, `.avi`, `.webm`만 허용한다.
- 영상 추론은 3ds Max 메인 스레드에서 실행하지 않는다.
- 중간 형식은 `artoke.nvidia-body34.v1` JSON이다.
- 검수되지 않은 결과를 Artoke 홈페이지에 자동 게시하지 않는다.
- NVIDIA eye/ear/finger-tip 관절은 Biped BVH에서 제외한다.
- 완료 artifact는 원본 영상, body34 JSON, BVH, trace와 SHA-256을 보존한다.
- 기존 주황색 BVH Studio 정체성을 유지하고 영상 작업 상태에만 `#76b900`을 사용한다.

---

### Task 1: Maxine 설치 진단과 공식 설치 런북

**Files:**
- Create: `src/nvidia/maxine.py`
- Create: `scripts/install-maxine-body-pose.ps1`
- Create: `native/maxine_body34/CMakeLists.txt`
- Create: `native/maxine_body34/main.cpp`
- Create: `tests/test_maxine.py`
- Modify: `docs/MAXINE_BODY_POSE.md`

**Interfaces:**
- Produces: `MaxineReadiness`, `check_maxine(root: Path) -> MaxineReadiness`, `build_bodytrack_command(video: Path, output: Path, sdk_root: Path) -> list[str]`, native `maxine_body34.exe`
- Consumes: NVIDIA AR SDK feature names `nvarbodyposeestimation,nvarbodydetection`

- [ ] **Step 1: Write failing readiness and command tests**

```python
def test_check_maxine_lists_missing_runtime(tmp_path):
    report = check_maxine(tmp_path)
    assert not report.ready
    assert "nvarbodyposeestimation" in report.missing_features

def test_bodytrack_command_keeps_paths_as_arguments(tmp_path):
    command = build_bodytrack_command(
        tmp_path / "clip one.mp4", tmp_path / "body.json", tmp_path / "sdk"
    )
    assert command[-4:] == [
        "--input", str(tmp_path / "clip one.mp4"),
        "--output", str(tmp_path / "body.json"),
    ]
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_maxine.py -v`  
Expected: collection fails because `src.nvidia.maxine` does not exist.

- [ ] **Step 3: Implement readiness types and exact command builder**

```python
@dataclass(frozen=True)
class MaxineReadiness:
    ready: bool
    sdk_root: Path
    missing_files: tuple[str, ...]
    missing_features: tuple[str, ...]

def build_bodytrack_command(video: Path, output: Path, sdk_root: Path) -> list[str]:
    return [
        str(sdk_root / "artoke" / "maxine_body34.exe"),
        "--input", str(video), "--output", str(output),
    ]
```

The official NVIDIA `BodyTrack.exe` accepts offline input and rendered output video but
does not expose a 34-joint JSON output option. `native/maxine_body34` therefore calls the
documented `NvAR_Parameter_Output(KeyPoints3D)`, `JointAngles`, and
`KeyPointsConfidence` APIs for every decoded frame and writes
`artoke.nvidia-body34.v1` JSON. It links only against the officially installed AR SDK and
does not copy NVIDIA model binaries into the repository.

- [ ] **Step 4: Add PowerShell installer with NGC credential gate**

The script must verify GPU compute capability, require an existing `NGC_API_KEY` environment value without printing it, download the official AR SDK package through the NVIDIA-provided NGC command, and run:

```powershell
./install_feature.ps1 -gpu 120 -features nvarbodyposeestimation,nvarbodydetection
```

It exits code 2 with a Korean instruction when `NGC_API_KEY` is absent and never substitutes unofficial models.

- [ ] **Step 5: Run tests and readiness smoke check**

Run: `python -m pytest tests/test_maxine.py -v`  
Expected: PASS.  
Run: `pwsh -File scripts/install-maxine-body-pose.ps1 -CheckOnly`  
Expected on current PC before SDK installation: structured `blocked` result naming SDK/features, not a traceback.

- [ ] **Step 6: Commit**

```bash
git add src/nvidia/maxine.py scripts/install-maxine-body-pose.ps1 native/maxine_body34 tests/test_maxine.py docs/MAXINE_BODY_POSE.md
git commit -m "feat: add Maxine body pose readiness and installer"
```

### Task 2: NVIDIA body34 JSON validation and Biped BVH conversion

**Files:**
- Create: `src/nvidia/body34.py`
- Create: `tests/fixtures/nvidia_body34_identity.json`
- Create: `tests/test_nvidia_body34.py`
- Modify: `src/helpers/bvh.py`

**Interfaces:**
- Produces: `Body34Motion`, `load_body34(path: Path) -> Body34Motion`, `body34_to_bvh(motion: Body34Motion) -> str`, `convert_body34_file(input_path: Path, output_path: Path) -> int`
- Consumes: schema `artoke.nvidia-body34.v1`, rotations in XYZW, positions in meters, Y-up right-handed coordinates

- [ ] **Step 1: Write failing schema and identity-motion tests**

```python
def test_rejects_wrong_schema(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"schema":"wrong","frames":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        load_body34(path)

def test_identity_motion_exports_biped_hierarchy(fixture_path):
    text = body34_to_bvh(load_body34(fixture_path))
    assert text.startswith("HIERARCHY\nROOT Hips")
    assert "JOINT Chest" in text
    assert "JOINT LeftUpArm" in text
    assert "Frames: 2" in text
    assert "Frame Time: 0.016666667" in text
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_nvidia_body34.py -v`  
Expected: import failure for `src.nvidia.body34`.

- [ ] **Step 3: Implement strict immutable data model and validation**

Validate schema, positive FPS, monotonic frame indexes, identical joint sets, finite values, normalized quaternions, and confidence in `[0,1]`. A missing joint is accepted only when explicitly present with confidence `0.0`; carry forward the previous valid rotation or identity for frame zero.

- [ ] **Step 4: Implement fixed Character Studio hierarchy and rotation conversion**

Use exactly these animated nodes:

```text
Hips
├─ Chest ─ Neck
│          ├─ LeftCollar ─ LeftUpArm ─ LeftLowArm ─ LeftHand
│          └─ RightCollar ─ RightUpArm ─ RightLowArm ─ RightHand
├─ LeftUpLeg ─ LeftLowLeg ─ LeftFoot ─ LeftToe
└─ RightUpLeg ─ RightLowLeg ─ RightFoot ─ RightToe
```

Derive static offsets from `reference_pose`, convert local XYZW quaternion to BVH Z/X/Y Euler degrees with continuous angle unwrapping, and convert root translation meters to centimeters.

- [ ] **Step 5: Reparse generated BVH with existing parser**

```python
parsed = parse_bvh(body34_to_bvh(load_body34(fixture_path)))
assert len(parsed.frames) == 2
assert parsed.frame_time == pytest.approx(1 / 60)
```

- [ ] **Step 6: Run focused and existing BVH regression tests**

Run: `python -m pytest tests/test_nvidia_body34.py tests/test_bvh_helpers.py tests/test_bvh_studio.py -v`  
Expected: PASS with no skipped tests.

- [ ] **Step 7: Commit**

```bash
git add src/nvidia/body34.py src/helpers/bvh.py tests/fixtures/nvidia_body34_identity.json tests/test_nvidia_body34.py
git commit -m "feat: convert NVIDIA body34 motion to Biped BVH"
```

### Task 3: Non-blocking video job controller

**Files:**
- Create: `src/ui/studio/video_jobs.py`
- Create: `tests/test_video_jobs.py`
- Modify: `src/ui/studio/bridge.py`

**Interfaces:**
- Produces: `VideoJobController.start(payload: dict) -> dict`, `.status(job_id: str) -> dict`, `.cancel(job_id: str) -> dict`
- Consumes: `check_maxine`, `build_bodytrack_command`, `convert_body34_file`

- [ ] **Step 1: Write failing state-transition tests**

```python
def test_job_blocks_when_maxine_is_missing(tmp_path):
    controller = VideoJobController(runner=FakeRunner(), readiness=blocked_report(tmp_path))
    job = controller.start({"video": str(tmp_path / "clip.mp4"), "library": str(tmp_path)})
    assert job["status"] == "blocked"
    assert job["stage"] == "sdk_check"

def test_second_running_job_is_rejected(controller, video_payload):
    controller.start(video_payload)
    with pytest.raises(RuntimeError, match="실행 중"):
        controller.start(video_payload)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_video_jobs.py -v`  
Expected: import failure for `video_jobs`.

- [ ] **Step 3: Implement finite states and background worker**

Allowed transitions are:

```text
queued → sdk_check → extracting → converting → validating → complete
                  ↘ blocked      ↘ failed        ↘ cancelled
```

Use one daemon worker thread, one subprocess at a time, a lock around current job state, and `subprocess.Popen` so cancel terminates only the owned child process.

- [ ] **Step 4: Add WebChannel slots**

```python
@QtCore.Slot(result=str)
def choose_video(self) -> str: ...

@QtCore.Slot(str, result=str)
def start_video_job(self, payload_json: str) -> str: ...

@QtCore.Slot(str, result=str)
def video_job_status(self, job_id: str) -> str: ...

@QtCore.Slot(str, result=str)
def cancel_video_job(self, job_id: str) -> str: ...
```

The file picker uses the exact filter `Video (*.mp4 *.mov *.mkv *.avi *.webm)` and returns an empty selection as `{cancelled: true}`.

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_video_jobs.py tests/test_bvh_studio.py -v`  
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/ui/studio/video_jobs.py src/ui/studio/bridge.py tests/test_video_jobs.py
git commit -m "feat: add non-blocking BVH Studio video jobs"
```

### Task 4: BVH Studio 영상 추가 UI

**Files:**
- Modify: `src/ui/studio/web/studio_draft.html`
- Modify: `src/ui/studio/launch.py`
- Modify: `tests/test_bvh_studio.py`

**Interfaces:**
- Consumes: bridge slots from Task 3
- Produces: `[data-action="add-video"]`, `.video-job-card`, job polling and result auto-selection

- [ ] **Step 1: Write failing DOM contract tests**

```python
def test_studio_page_exposes_video_import_controls():
    html = STUDIO_PAGE.read_text(encoding="utf-8")
    assert 'data-action="add-video"' in html
    assert 'class="video-job-card"' in html
    assert "start_video_job" in html
    assert "video_job_status" in html
```

- [ ] **Step 2: Run the test and verify RED**

Run: `python -m pytest tests/test_bvh_studio.py::test_studio_page_exposes_video_import_controls -v`  
Expected: FAIL because controls are absent.

- [ ] **Step 3: Add accessible toolbar control and collapsed job card**

Add `영상 추가` beside `Artoke에서 가져오기`. Preserve the orange import CTA. Use `#76b900` only for the video status marker/progress fill, 2px radii, 44px minimum target height, `aria-live="polite"` on status, and keyboard-accessible buttons.

- [ ] **Step 4: Implement bridge calls and polling**

On selection: call `choose_video`, then `start_video_job`. Poll `video_job_status` every 500ms only while status is nonterminal. On `complete`, refresh clips and select the returned BVH path. On `blocked`, show the missing SDK/features and installer document path.

- [ ] **Step 5: Run HTML contract and complete test suite**

Run: `python -m pytest tests/test_bvh_studio.py -v`  
Expected: PASS.  
Run: `python -m pytest -q`  
Expected: all repository tests PASS with no new skips.

- [ ] **Step 6: Commit**

```bash
git add src/ui/studio/web/studio_draft.html src/ui/studio/launch.py tests/test_bvh_studio.py
git commit -m "feat: add video import workspace to BVH Studio"
```

### Task 5: SQLite provenance and end-to-end 3ds Max verification

**Files:**
- Modify: `src/ui/studio/video_jobs.py`
- Modify: `src/ui/studio/bridge.py`
- Modify: `tests/test_video_jobs.py`
- Modify: `C:/work/Ai/motiongen-engine/src/zzz_motion/pipeline.py`
- Modify: `C:/work/Ai/motiongen-engine/tests/test_pipeline.py`
- Modify: `C:/work/Ai/me/me/wiki/dev-tasks/2026-08-07-video-to-bvh-3dsmax-validation-pipeline.md`

**Interfaces:**
- Consumes: `register_artifact(..., clip_id=...)`, completed NVIDIA job result, existing Biped review flow
- Produces: tool name `NVIDIA Maxine Body34→BVH`, execution trace with hashes, actual Biped review evidence

- [ ] **Step 1: Write failing provenance test**

```python
def test_nvidia_artifact_is_bound_to_accepted_clip(db_path, completed_job):
    artifact_id = register_nvidia_result(db_path, 1, 3, completed_job)
    row = connect_database(db_path).execute(
        "SELECT source_id, clip_id, tool_name, status FROM artifacts WHERE id=?",
        (artifact_id,),
    ).fetchone()
    assert tuple(row) == (1, 3, "NVIDIA Maxine Body34→BVH", "complete")
```

- [ ] **Step 2: Run test and verify RED**

Run: `C:/work/Ai/motiongen-engine/.venv/Scripts/python.exe -m pytest C:/work/Ai/motiongen-engine/tests/test_pipeline.py -v`  
Expected: failure because `register_nvidia_result` is absent.

- [ ] **Step 3: Implement artifact registration and trace hashing**

Register only after video, body34 JSON, BVH and trace exist. Trace fields include backend, SDK version, source/clip IDs, FPS, frame count, average/min confidence, SHA-256 for every retained file, command arguments without credentials, and status.

- [ ] **Step 4: Run both repository suites**

Run: `python -m pytest C:/work/Ai/3dsmax-mcp/tests -q`  
Expected: PASS.  
Run: `C:/work/Ai/motiongen-engine/.venv/Scripts/python.exe -m pytest C:/work/Ai/motiongen-engine/tests -q`  
Expected: PASS.

- [ ] **Step 5: Verify in interactive 3ds Max 2026**

Launch `maxscript/bvh_studio2.ms`, confirm `영상 추가`, select the prepared dodge clip, ensure the UI remains responsive during extraction, load the completed BVH into `ZZZ_Dodge_Nvidia`, and retain:

```text
<output>/dodge_nvidia_tpose.bvh
<output>/dodge_body34.json
<output>/reviews/dodge_review.max
<output>/reviews/dodge_viewport.png
<output>/reviews/dodge_max_review.json
```

Do not mark accepted until frame count, first-frame orientation, root motion and visible foot sliding are reviewed.

- [ ] **Step 6: Regenerate Obsidian report and commit code/docs**

Run the existing `write-obsidian` command for source 1. Update the dev-task note with measured paths and limitations. Commit only files owned by this task.

```bash
git add src/ui/studio/video_jobs.py src/ui/studio/bridge.py tests/test_video_jobs.py
git commit -m "feat: persist NVIDIA BVH provenance and review handoff"
```
