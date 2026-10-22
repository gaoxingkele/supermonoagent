param([string]$Serial = '', [string]$Adb = '', [switch]$NoLaunch)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
if (!$Adb) {
    $portableAdb = Join-Path $repoRoot '.omx/tools/android-sdk/platform-tools/adb.exe'
    $Adb = if (Test-Path -LiteralPath $portableAdb) { $portableAdb } else { (Get-Command adb -ErrorAction Stop).Source }
}
function Invoke-Adb([string[]]$Arguments) {
    $result = & $Adb @Arguments
    if ($LASTEXITCODE -ne 0) { throw "ADB operation failed ($LASTEXITCODE)" }
    return $result
}
$devices = @(Invoke-Adb @('devices') | Where-Object { $_ -match '^(\S+)\s+device$' } | ForEach-Object { ($_ -split '\s+')[0] })
if ($Serial) {
    if ($devices -notcontains $Serial) { throw 'Selected Android device is not connected and authorized' }
} elseif ($devices.Count -eq 1) { $Serial = $devices[0] }
else { throw 'Connect exactly one authorized phone or specify -Serial' }
$python = Join-Path $repoRoot '.venv/Scripts/python.exe'
if (!(Test-Path -LiteralPath $python)) { $python = (Get-Command python -ErrorAction Stop).Source }
$bundleJson = & $python (Join-Path $PSScriptRoot 'package-android-ui.py')
if ($LASTEXITCODE -ne 0) { throw 'Mobile UI packaging failed' }
$bundle = $bundleJson | ConvertFrom-Json
$revision = [string]$bundle.revision
if ($revision -notmatch '^[A-Za-z0-9_-]{1,96}$') { throw 'Invalid staged revision' }
$remoteRoot = '/sdcard/Android/data/dev.supermonoagent.mobile/files/ui-updates'
$remoteDirectory = "$remoteRoot/$revision"
# These absolute paths are fixed to this application's external files; no delete or move crosses it.
if ($remoteDirectory -notmatch '^/sdcard/Android/data/dev\.supermonoagent\.mobile/files/ui-updates/[A-Za-z0-9_-]{1,96}$') { throw 'Unsafe update destination' }
Invoke-Adb @('-s', $Serial, 'shell', 'mkdir', '-p', $remoteRoot) | Out-Null
# An existing directory is an error: selected revisions are immutable.
Invoke-Adb @('-s', $Serial, 'shell', 'mkdir', $remoteDirectory) | Out-Null
Invoke-Adb @('-s', $Serial, 'push', (Join-Path $bundle.directory 'mobile'), "$remoteDirectory/mobile") | Out-Null
Invoke-Adb @('-s', $Serial, 'push', (Join-Path $bundle.directory 'manifest.properties'), "$remoteDirectory/manifest.properties") | Out-Null
$pointerPath = Join-Path $bundle.directory 'active.txt'
[IO.File]::WriteAllText($pointerPath, "$revision`n", [Text.Encoding]::ASCII)
$pendingPointer = "$remoteRoot/active-$revision.tmp"
Invoke-Adb @('-s', $Serial, 'push', $pointerPath, $pendingPointer) | Out-Null
Invoke-Adb @('-s', $Serial, 'shell', 'mv', $pendingPointer, "$remoteRoot/active.txt") | Out-Null
if (!$NoLaunch) {
    Invoke-Adb @('-s', $Serial, 'shell', 'am', 'start', '-n', 'dev.supermonoagent.mobile/.MainActivity', '--ez', 'reload_ui', 'true') | Out-Null
}
Write-Host "USB UI update ready: $revision ($($bundle.bytes) bytes). Native APK unchanged."
