$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$launcher = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "start_artoke_worker.ps1")).Path
$pythonCommand = Get-Command pythonw.exe -ErrorAction Stop
$pythonPath = (Resolve-Path -LiteralPath $pythonCommand.Source).Path
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$name = "ARTOKE Motion Worker"
$command = 'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -PythonPath "{1}" -RepoRoot "{2}"' -f $launcher, $pythonPath, $repoRoot

New-Item -Path $runKey -Force | Out-Null
New-ItemProperty -Path $runKey -Name $name -Value $command -PropertyType String -Force | Out-Null
Write-Output "Registered ARTOKE Motion Worker for the current Windows user."
