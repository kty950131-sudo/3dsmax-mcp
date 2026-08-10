Write-Output "Enter the ARTOKE worker token. Input is hidden by Windows."
cmdkey.exe /generic:ARTOKE/MotionWorkerToken /user:ARTOKE /pass
if ($LASTEXITCODE -ne 0) {
    throw "Windows Credential Manager did not store the ARTOKE token."
}
Write-Output "Stored ARTOKE worker token in Windows Credential Manager."
