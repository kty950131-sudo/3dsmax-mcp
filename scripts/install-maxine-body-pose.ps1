[CmdletBinding()]
param(
    [string]$SdkRoot = "C:\Program Files\NVIDIA Corporation\NVIDIA AR SDK",
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$requiredFeatures = @("nvarbodyposeestimation", "nvarbodydetection")
$missing = [System.Collections.Generic.List[string]]::new()

if (-not (Test-Path -LiteralPath (Join-Path $SdkRoot "include\nvAR.h"))) {
    $missing.Add("include/nvAR.h")
}
if (-not (Test-Path -LiteralPath (Join-Path $SdkRoot "models"))) {
    $missing.Add("models")
}
foreach ($feature in $requiredFeatures) {
    if (-not (Test-Path -LiteralPath (Join-Path $SdkRoot "features\$feature\bin"))) {
        $missing.Add("features/$feature/bin")
    }
}

$gpu = & nvidia-smi --query-gpu=name,driver_version,compute_cap --format=csv,noheader 2>$null
$payload = [ordered]@{
    status = if ($missing.Count -eq 0) { "ready" } else { "blocked" }
    sdk_root = $SdkRoot
    gpu = ($gpu -join "; ")
    missing = @($missing)
    ngc_key_present = -not [string]::IsNullOrWhiteSpace($env:NGC_API_KEY)
}

if ($CheckOnly) {
    $payload | ConvertTo-Json -Depth 4
    if ($missing.Count -eq 0) { exit 0 } else { exit 2 }
}

if (-not (Test-Path -LiteralPath (Join-Path $SdkRoot "include\nvAR.h"))) {
    Write-Error @"
NVIDIA AR SDK Core가 없습니다. NVIDIA 계정으로 공식 NGC Collection에서 Windows SDK Core를 내려받아 설치하세요:
https://catalog.ngc.nvidia.com/orgs/nvidia/teams/maxine/collections/maxine_ar_sdk_collection
라이선스 동의가 필요한 다운로드이므로 이 스크립트는 비공식 주소로 우회하지 않습니다.
"@
    exit 2
}

if ([string]::IsNullOrWhiteSpace($env:NGC_API_KEY)) {
    Write-Error @"
NGC_API_KEY가 없습니다. https://org.ngc.nvidia.com/setup/api-key 에서 키를 만든 뒤
현재 PowerShell 세션에 `$env:NGC_API_KEY를 설정하고 다시 실행하세요.
키는 파일이나 로그에 저장하지 않습니다.
"@
    exit 2
}

$featureInstaller = Join-Path $SdkRoot "features\install_feature.ps1"
if (-not (Test-Path -LiteralPath $featureInstaller)) {
    Write-Error "공식 feature installer를 찾을 수 없습니다: $featureInstaller"
    exit 2
}

$previousCliKey = $env:NGC_CLI_API_KEY
try {
    $env:NGC_CLI_API_KEY = $env:NGC_API_KEY
    & $featureInstaller -gpu 120 -features ($requiredFeatures -join ",")
    if ($LASTEXITCODE -ne 0) {
        throw "Maxine feature 설치 실패 (exit $LASTEXITCODE)"
    }
}
finally {
    $env:NGC_CLI_API_KEY = $previousCliKey
}

& $PSCommandPath -SdkRoot $SdkRoot -CheckOnly
