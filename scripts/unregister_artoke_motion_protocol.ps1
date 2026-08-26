[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$protocolKey = "HKCU:\Software\Classes\artoke-motion"

if (-not (Test-Path -LiteralPath $protocolKey)) {
    Write-Output "The artoke-motion protocol is not registered for the current user."
    exit 0
}
$resolved = Get-Item -LiteralPath $protocolKey
if ($resolved.Name -ne "HKEY_CURRENT_USER\Software\Classes\artoke-motion") {
    [Console]::Error.WriteLine("Refusing to remove an unexpected registry key.")
    exit 1
}

if ($DryRun) {
    Write-Output "Would remove the artoke-motion protocol for the current user."
    exit 0
}

Remove-Item -LiteralPath $protocolKey -Recurse -Force
Write-Output "Unregistered the artoke-motion protocol for the current user."
