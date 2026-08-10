$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$name = "ARTOKE Motion Worker"
Remove-ItemProperty -Path $runKey -Name $name -ErrorAction SilentlyContinue
Write-Output "Unregistered ARTOKE Motion Worker startup entry. Local files were not removed."
