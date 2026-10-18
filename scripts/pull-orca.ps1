$ErrorActionPreference = "Stop"
$git = "D:\Program Files\Git\cmd\git.exe"
$dest = Join-Path $PSScriptRoot "..\study\orca"
if (Test-Path (Join-Path $dest ".git")) {
    Set-Location $dest
    & $git sparse-checkout disable
    & $git pull --ff-only
} else {
    & $git clone --depth 1 https://github.com/stablyai/orca.git $dest
}
Write-Output "full study checkout ready: $dest"
