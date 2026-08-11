param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$WorkspaceRoot = Split-Path -Parent $ProjectRoot
$Environment = Join-Path $ProjectRoot '.venv-rtmw3d'
$Repository = Join-Path $WorkspaceRoot 'vendor\mmpose'
$ModelDirectory = Join-Path $WorkspaceRoot 'vendor\models\rtmw3d'
$Python = Join-Path $Environment 'Scripts\python.exe'

if ($CheckOnly) {
    & (Join-Path $ProjectRoot '.venv\Scripts\python.exe') -c "from maxmcp.rtmw3d.runtime import default_readiness; import json; r=default_readiness(); print(json.dumps({'ready':r.ready,'missing':r.missing_files,'environment':str(r.environment),'repository':str(r.repository)}))"
    exit $LASTEXITCODE
}

if (-not (Test-Path -LiteralPath $Python)) {
    uv venv --python 3.10 $Environment
}
uv pip install --python $Python torch torchvision --index-url https://download.pytorch.org/whl/cu128
uv pip install --python $Python 'numpy<2' 'opencv-python<4.12' mmengine 'mmcv-lite==2.1.0' 'mmdet==3.3.0' json-tricks xtcocotools scipy munkres chumpy

if (-not (Test-Path -LiteralPath $Repository)) {
    git clone --branch v1.3.2 --depth 1 https://github.com/open-mmlab/mmpose.git $Repository
}

$Heads = Join-Path $Repository 'mmpose\models\heads\__init__.py'
$HeadText = Get-Content -Raw -LiteralPath $Heads
if ($HeadText -notmatch 'mmcv-lite: RTMW3D') {
    $HeadText = $HeadText.Replace(
        'from .transformer_heads import EDPoseHead',
        "try:`n    from .transformer_heads import EDPoseHead`nexcept ModuleNotFoundError:  # mmcv-lite: RTMW3D does not use EDPose CUDA ops`n    EDPoseHead = None"
    )
    Set-Content -LiteralPath $Heads -Value $HeadText -Encoding utf8
}
uv pip uninstall --python $Python mmpose 2>$null
uv pip install --python $Python $Repository

New-Item -ItemType Directory -Force -Path $ModelDirectory | Out-Null
$PoseModel = Join-Path $ModelDirectory 'rtmw3d-l_8xb64_cocktail14-384x288-794dbc78_20240626.pth'
if (-not (Test-Path -LiteralPath $PoseModel)) {
    curl.exe -L --fail -o $PoseModel 'https://download.openmmlab.com/mmpose/v1/wholebody_3d_keypoint/rtmw3d/rtmw3d-l_8xb64_cocktail14-384x288-794dbc78_20240626.pth'
}
& $Python -c "import torch; assert torch.cuda.is_available(); print(torch.cuda.get_device_name(0))"
& $PSCommandPath -CheckOnly
