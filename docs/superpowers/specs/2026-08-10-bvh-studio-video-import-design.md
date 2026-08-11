# BVH Studio 영상 추가·NVIDIA 변환 설계

작성일: 2026-08-10
대상 런처: `maxscript/bvh_studio2.ms`
기존 설계: `docs/superpowers/specs/2026-08-06-bvh-studio-design.md`

## 목적

BVH Studio에서 로컬 영상을 선택해 NVIDIA Maxine 3D Body Pose 변환 작업을 만들고,
완료된 Biped 호환 T-pose BVH를 기존 모션 라이브러리에 자동 등록한다. 변환된 BVH는
기존 Artoke 동기화·프리뷰·Biped 임포트·SQLite/Obsidian 검수 흐름을 그대로 사용한다.

## 확정 범위

- 상단 툴바에 `영상 추가` 버튼을 추가한다.
- 파일 선택기는 `mp4`, `mov`, `mkv`, `avi`, `webm`을 허용한다.
- 선택한 영상은 즉시 추론하지 않고 변환 작업으로 등록한다.
- 작업 패널은 입력 영상, 백엔드, 진행 단계, 진행률, 오류, 출력 BVH 경로를 표시한다.
- NVIDIA Maxine 34관절 출력은 중간 JSON으로 보존한다.
- 변환 완료 시 `<영상이름>_nvidia_tpose.bvh`를 현재 라이브러리 폴더에 기록한다.
- 완료된 클립을 새로고침하고 자동 선택한다.
- SQLite 승인 구간을 처리할 때는 source/clip ID를 작업에 함께 보존한다.
- Artoke 업로드는 자동으로 수행하지 않는다. 사용자가 검수·승인한 BVH만 기존 게시
  절차로 홈페이지에 올린다.

## 제외 범위

- Vercel/Artoke 서버에서 NVIDIA 추론 실행
- SMPL-X 메시 복원
- 손가락 전체 리타게팅
- 다중 인물 선택 UI
- 영상 편집기 또는 구간 컷 편집기
- 검수되지 않은 결과의 자동 홈페이지 게시

## 접근 방식 비교

### A. 로컬 NVIDIA 변환 후 라이브러리 등록 — 채택

RTX 5060에서 Maxine을 실행하고 BVH Studio는 작업 생성·상태 표시·결과 등록을 담당한다.
기존 Artoke는 완성 BVH의 배포 경로로 유지한다. GPU가 없는 홈페이지 서버와 분리되며,
실패 산출물이 공개되는 것을 막는다.

### B. 홈페이지에서 영상 업로드 후 서버 변환

사용 흐름은 단순하지만 현재 Vercel 환경에는 NVIDIA GPU와 Maxine 런타임이 없다.
별도 GPU 서버·업로드 저장소·과금·인증이 필요하므로 이번 범위에서 제외한다.

### C. BVH Studio 내부에서 동기 추론

구현은 짧지만 추론 중 3ds Max UI가 멈추며 작업 취소·복구가 어렵다. 사용하지 않는다.

## 아키텍처

```text
bvh_studio2.ms
  → launch.py / studio_draft.html
  → StudioBridge.choose_video()
  → StudioBridge.start_video_job(payload)
  → local subprocess: NVIDIA Maxine extractor
  → 34-joint JSON + execution trace
  → nvidia_to_bvh.py
  → *_nvidia_tpose.bvh
  → bvh-library scan / preview / Biped import
  → SQLite artifact + Biped review
  → 승인 후 Artoke manifest 배포
```

`bvh_studio2.ms`는 현재처럼 런처 역할만 유지한다. 파일 선택은 Qt 네이티브 대화상자를
사용하고, 영상 처리·BVH 변환은 Python 모듈로 분리한다. 3ds Max 메인 스레드에서는
subprocess를 기다리지 않는다.

## UI 설계

기존 BVH Studio의 어두운 표면과 주황색 기본 강조색을 유지한다. NVIDIA 레퍼런스의
공학적이고 각진 언어만 차용해 영상 작업 영역은 2px radius, hairline border,
NVIDIA green `#76b900` 상태선으로 구분한다. NVIDIA 로고나 전용 폰트는 복제하지 않는다.

### 상단 툴바

순서:

```text
[라이브러리 경로] [새로고침] [Artoke에서 가져오기] [영상 추가] [검색]
```

- `영상 추가`는 44px 이상의 클릭 높이를 확보한다.
- 기본 상태는 outline, 영상 선택/진행 중에는 green 상태점만 사용한다.
- 같은 화면의 기존 주황색 `바이패드 생성 + 임포트`가 여전히 유일한 주 CTA다.

### 영상 작업 패널

`영상 추가` 후 우측 패널 상단에 접히는 작업 카드 하나를 표시한다.

- 파일명과 전체 경로
- 상태: `대기`, `SDK 확인`, `관절 추출`, `BVH 변환`, `완료`, `실패`, `취소`
- 결정적 단계 기반 진행률
- `취소`, `폴더 열기`, `결과 선택` 보조 버튼
- 오류는 한 줄 요약과 펼쳐보는 trace 경로

동시에 한 작업만 실행한다. 새 영상을 고르면 실행 중 작업을 덮어쓰지 않고 차단한다.

## 데이터 계약

NVIDIA 중간 JSON 최소 형식:

```json
{
  "schema": "artoke.nvidia-body34.v1",
  "source_video": "C:/path/clip.mp4",
  "fps": 60.0,
  "reference_pose": [{"name": "pelvis", "position": [0, 0, 0]}],
  "frames": [
    {
      "index": 0,
      "root_translation": [0, 0, 0],
      "joints": {
        "pelvis": {"rotation_xyzw": [0, 0, 0, 1], "confidence": 0.98}
      }
    }
  ]
}
```

모든 프레임은 동일한 34관절 집합을 갖는다. 누락 관절은 삭제하지 않고 confidence 0과
마지막 유효 회전을 보존한다. 좌표계, 단위, quaternion 순서는 schema에 고정한다.

## Biped 매핑

- pelvis → Hips
- torso → Chest
- neck → Neck
- shoulder → Collar/UpArm 체인의 기준
- elbow → LowArm
- wrist → Hand
- hip → UpLeg
- knee → LowLeg
- ankle → Foot
- big toe → Toe

눈·귀·손가락 끝은 Biped BVH에서 제외한다. 한 개 torso만 제공되므로 Chest2/Chest3를
추측해 만들지 않는다. 첫 유효 프레임의 reference pose에서 고정 OFFSET을 만들고,
모션 프레임에는 root position과 Z/X/Y rotation 채널을 기록한다.

## 오류 및 품질 게이트

- Maxine SDK/모델 없음: 영상 선택은 허용하고 작업을 `blocked`로 기록한다.
- NGC 인증 없음: 설치 안내 경로만 표시하며 자동 로그인하지 않는다.
- 평균 confidence가 기준 미만이면 BVH를 생성해도 `needs_review`로 기록한다.
- 프레임 수·FPS·관절 집합 불일치 시 완료 artifact를 등록하지 않는다.
- BVH는 `HIERARCHY`, `MOTION`, frame count, frame time을 재파싱해 검증한다.
- `biped.loadMocapFile`이 false이면 생성한 Biped를 삭제하고 검수를 `rejected` 후보로 남긴다.
- 원본 영상, 34관절 JSON, BVH, trace의 경로와 SHA-256을 보존한다.

## 테스트

### Max 없이 실행되는 테스트

- 영상 확장자 필터와 취소 처리
- 작업 상태 전이 및 동시 실행 차단
- body34 JSON schema 검증
- 34관절→Biped 매핑
- identity quaternion의 T-pose BVH
- 프레임 수·frame time·root translation 보존
- 낮은 confidence와 누락 관절 보간
- BVH 재파싱 및 `_biped` 변환 회귀
- SQLite artifact의 source/clip provenance

### 3ds Max 검증

- `영상 추가` 버튼과 작업 카드 표시
- 파일 선택 후 Max UI 비동기 응답 유지
- 생성 BVH를 Biped에 로드
- 첫 프레임 골격 방향, 프레임 수, 루트 이동 확인
- viewport/MAX/trace를 보존하고 SQLite·Obsidian 기록

## 완료 기준

1. BVH Studio에서 영상 파일을 선택할 수 있다.
2. Maxine이 없으면 명확한 blocked 작업으로 남고 Max가 멈추지 않는다.
3. 유효한 body34 JSON 입력은 SMPL-X·PyTorch3D 없이 Biped용 T-pose BVH가 된다.
4. 결과 BVH가 라이브러리에 나타나고 기존 프리뷰·임포트 경로를 탄다.
5. 실제 dodge 영상의 NVIDIA 출력으로 Biped 로드와 검수 증거가 SQLite/Obsidian에 남는다.
