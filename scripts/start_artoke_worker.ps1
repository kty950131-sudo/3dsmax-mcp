param(
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [Parameter(Mandatory = $true)][string]$RepoRoot
)

$resolvedPython = (Resolve-Path -LiteralPath $PythonPath).Path
$resolvedRepo = (Resolve-Path -LiteralPath $RepoRoot).Path
Push-Location -LiteralPath $resolvedRepo
try {
    & $resolvedPython -m src.worker run
} finally {
    Pop-Location
}
