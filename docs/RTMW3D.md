# BVH Studio RTMW3D 로컬 백엔드

BVH Studio의 `영상 추가`는 OpenMMLab RTMW3D-L을 RTX GPU에서 로컬 실행한다.
영상은 외부 서버로 전송하지 않으며 SMPL-X와 PyTorch3D를 사용하지 않는다.

이 런타임은 설정된 저장소 checkout에서만 지원한다. `3dsmax-mcp` wheel만 설치해서는
`.venv-rtmw3d`, `vendor` 아래 extractor 저장소, 모델·checkpoint가 포함·설치·구성되지
않는다. 아래 경로와 `scripts/install-rtmw3d.ps1`을 사용하는 checkout에서 실행한다.

## 현재 설치 위치

- Python 환경: `C:\work\Ai\3dsmax-mcp\.venv-rtmw3d`
- MMPose v1.3.2: `C:\work\Ai\vendor\mmpose`
- 모델: `C:\work\Ai\vendor\models\rtmw3d`
- CUDA 런타임: PyTorch CUDA 12.8

## 생성 파일

- `<영상명>_rtmw3d.json`: 23개 신체 관절, 신뢰도, FPS
- `<영상명>_rtmw3d_tpose.bvh`: Character Studio/Biped용 BVH
- `<영상명>_rtmw3d_trace.json`: 입력·중간 결과·BVH SHA-256

RTMW3D의 프레임별 3D 관절에 시간 평활화를 적용하고, 신뢰도 0.2 미만인
관절은 직전 유효 위치를 유지한다. BVH 변환기는 첫 프레임의 골격 길이를 사용해
Y-up Biped 계층을 만들고 로컬 관절 회전을 계산한다.

## 한계와 검수

- 단일 인물이 화면 대부분을 차지하는 영상을 전제로 한다.
- 카메라 이동은 실제 루트 이동과 혼동될 수 있다.
- 발 고정은 아직 최종 IK 보정이 아니므로 3ds Max에서 미끄러짐을 확인한다.
- 결과는 자동 승인하지 않는다. Biped에서 방향·루트·발 접촉을 확인한 뒤 등록한다.

재설치는 `scripts/install-rtmw3d.ps1`을 실행한다.
