# NVIDIA Maxine Body Pose 설치

BVH Studio의 `영상 추가` 기능은 NVIDIA AR SDK 3D Body Pose를 로컬 RTX GPU에서
실행한다. SMPL-X와 PyTorch3D는 사용하지 않는다.

## 현재 PC 요구사항

- Windows 10/11 64-bit
- NVIDIA Tensor Core GPU
- 드라이버 570.65 이상
- Visual Studio 2022 Desktop development with C++
- CMake 3.21 이상
- NVIDIA AR SDK Core와 두 feature
  - `nvarbodyposeestimation`
  - `nvarbodydetection`

RTX 5060 Laptop GPU의 compute capability는 12.0이므로 설치 스크립트는 NVIDIA가
정한 feature GPU 값 `120`을 사용한다.

## 설치

1. [NVIDIA AR SDK Collection](https://catalog.ngc.nvidia.com/orgs/nvidia/teams/maxine/collections/maxine_ar_sdk_collection)에 로그인한다.
2. Windows AR SDK Core 라이선스에 동의하고 기본 위치에 설치한다.
3. [NGC API 키](https://org.ngc.nvidia.com/setup/api-key)를 만든다.
4. 키를 현재 PowerShell 프로세스에만 설정한다.

```powershell
$env:NGC_API_KEY = '<발급받은 키>'
pwsh -File C:\work\Ai\3dsmax-mcp\scripts\install-maxine-body-pose.ps1
```

스크립트는 키를 파일·명령줄·로그에 기록하지 않고, NVIDIA가 SDK와 함께 제공한
`install_feature.ps1`에 프로세스 환경변수로만 전달한다.

## 상태 확인

```powershell
pwsh -File C:\work\Ai\3dsmax-mcp\scripts\install-maxine-body-pose.ps1 -CheckOnly
```

`blocked`이면 `missing`에 나온 SDK/feature만 설치한다. 공식 SDK Core는 라이선스
동의가 필요한 NGC 다운로드이므로 자동 우회 다운로드를 하지 않는다.

## 공식 자료

- https://docs.nvidia.com/maxine/ar/latest/WindowsARSDK/Windows.html
- https://docs.nvidia.com/maxine/ar/1.0.0/WindowsARSDK/InstalltheARSDK.html
- https://docs.nvidia.com/maxine/ar/latest/API/Architecture/properties.html
- https://github.com/NVIDIA-Maxine/AR-SDK-Samples
