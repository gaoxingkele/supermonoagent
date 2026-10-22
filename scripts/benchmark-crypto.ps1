param([string]$Serial = '', [switch]$DesktopOnly)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$work = Join-Path $repoRoot ('.omx/artifacts/crypto-benchmark/run-' + [DateTime]::UtcNow.ToString('yyyyMMdd-HHmmssfff'))
$javac = Get-ChildItem "$repoRoot/.omx/tools/jdk/*/bin/javac.exe" | Select-Object -First 1
if (!$javac) { throw 'Portable Android JDK is missing' }
New-Item -ItemType Directory -Force "$work/classes", "$work/dex" | Out-Null
function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Benchmark tool failed: $Executable ($LASTEXITCODE)" }
}
Invoke-Checked $javac.FullName @('-encoding', 'UTF-8', '-source', '8', '-target', '8', '-Xlint:-options', '-d', "$work/classes", "$PSScriptRoot/CryptoBenchmark.java")
$java = Join-Path $javac.Directory.FullName 'java.exe'
$result = & $java -cp "$work/classes" CryptoBenchmark
if ($LASTEXITCODE -ne 0) { throw 'Desktop crypto benchmark failed' }
$result | Set-Content "$work/windows.json" -Encoding utf8
Write-Host "Windows primitive benchmark: $work/windows.json"
if ($DesktopOnly) { return }
$adb = "$repoRoot/.omx/tools/android-sdk/platform-tools/adb.exe"
$devices = @(& $adb devices | Where-Object { $_ -match '^(\S+)\s+device$' } | ForEach-Object { ($_ -split '\s+')[0] })
if (!$Serial) {
    if ($devices.Count -ne 1) { throw 'Connect exactly one authorized phone or specify -Serial' }
    $Serial = $devices[0]
}
if ($devices -notcontains $Serial) { throw 'Selected phone is not connected and authorized' }
$env:JAVA_HOME = $javac.Directory.Parent.FullName
$sdk = "$repoRoot/.omx/tools/android-sdk"
Invoke-Checked "$sdk/build-tools/35.0.0/d8.bat" @('--min-api', '26', '--lib', "$sdk/platforms/android-35/android.jar", '--output', "$work/dex", "$work/classes/CryptoBenchmark.class")
Invoke-Checked $adb @('-s', $Serial, 'push', "$work/dex/classes.dex", '/data/local/tmp/sma-crypto-benchmark.dex')
$result = & $adb -s $Serial shell dalvikvm -cp /data/local/tmp/sma-crypto-benchmark.dex CryptoBenchmark
if ($LASTEXITCODE -ne 0) { throw 'Android crypto benchmark failed' }
$result | Set-Content "$work/android.json" -Encoding utf8
Write-Host "Android primitive benchmark: $work/android.json"
