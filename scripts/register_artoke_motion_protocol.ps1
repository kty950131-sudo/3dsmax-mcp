[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PythonPath,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

foreach ($character in $PythonPath.ToCharArray()) {
    if ([char]::IsControl($character) -or $character -eq '"' -or $character -eq "'") {
        [Console]::Error.WriteLine("PythonPath contains characters that cannot be quoted safely.")
        exit 1
    }
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    [Console]::Error.WriteLine("PythonPath does not point to an existing file.")
    exit 1
}
$interpreter = (Resolve-Path -LiteralPath $PythonPath).Path
$command = '"{0}" -m maxmcp.local_ingest ingest "%1"' -f $interpreter

$protocolKey = "HKCU:\Software\Classes\artoke-motion"
$iconKey = "$protocolKey\DefaultIcon"
$commandKey = "$protocolKey\shell\open\command"

if ($DryRun) {
    Write-Output $command
    exit 0
}

New-Item -Path $protocolKey -Force | Out-Null
Set-ItemProperty -Path $protocolKey -Name "(default)" -Value "URL:ARTOKE Motion Companion"
New-ItemProperty -Path $protocolKey -Name "URL Protocol" -Value "" -PropertyType String -Force | Out-Null
New-Item -Path $iconKey -Force | Out-Null
Set-ItemProperty -Path $iconKey -Name "(default)" -Value ('"{0}",0' -f $interpreter)
New-Item -Path $commandKey -Force | Out-Null
Set-ItemProperty -Path $commandKey -Name "(default)" -Value $command
Write-Output "Registered the artoke-motion protocol for the current user."
