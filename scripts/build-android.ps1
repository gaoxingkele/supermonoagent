param([string]$ToolsRoot = "", [switch]$Bootstrap, [string]$ConnectionDefaults = "", [string]$OutputDir = "")
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
New-Item -ItemType Directory -Force (Join-Path $repoRoot '.omx') | Out-Null
try { $androidBuildLock = [IO.File]::Open((Join-Path $repoRoot '.omx/android-build.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
catch { throw 'Cannot acquire the Android build lock; another build may be running.' }
try {
if (!$ToolsRoot) { $ToolsRoot = Join-Path $repoRoot '.omx/tools' }
$ToolsRoot = [IO.Path]::GetFullPath($ToolsRoot)
if ($Bootstrap) { & "$PSScriptRoot/bootstrap-android.ps1" -ToolsRoot $ToolsRoot }
$java = Get-ChildItem "$ToolsRoot/jdk/*/bin/java.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if (!$java) { throw 'Portable JDK missing; run with -Bootstrap' }
$env:JAVA_HOME = $java.Directory.Parent.FullName
$env:PATH = "$env:JAVA_HOME/bin;$env:PATH"
$sdk = Join-Path $ToolsRoot 'android-sdk'
$buildTools = Join-Path $sdk 'build-tools/35.0.0'
$androidJar = Join-Path $sdk 'platforms/android-35/android.jar'
if (!(Test-Path $androidJar)) { throw 'Android SDK missing; run with -Bootstrap' }
$work = Join-Path $repoRoot '.omx/android-build'
$work = Join-Path $work ([guid]::NewGuid().ToString('N'))
$dist = if ($OutputDir) { [IO.Path]::GetFullPath($OutputDir) } else { Join-Path $repoRoot 'dist/android' }
New-Item -ItemType Directory -Force "$work/classes", "$work/dex", $dist | Out-Null
function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Build tool failed: $Executable ($LASTEXITCODE)" }
}
$manifest = Join-Path $repoRoot 'android/AndroidManifest.xml'
# Every newly built update gets a distinct user-visible version and Android code.
$manifestText = [IO.File]::ReadAllText($manifest)
[xml]$manifestXml = $manifestText
$androidNamespace = 'http://schemas.android.com/apk/res/android'
$nextCode = [int]$manifestXml.manifest.GetAttribute('versionCode', $androidNamespace) + 1
$currentVersion = [version]$manifestXml.manifest.GetAttribute('versionName', $androidNamespace)
$nextVersion = '{0}.{1}.{2}' -f $currentVersion.Major, $currentVersion.Minor, ($currentVersion.Build + 1)
$outputVersion = Split-Path $dist -Leaf
if ($outputVersion -match '^\d+\.\d+\.\d+$' -and $outputVersion -ne $nextVersion) { throw "Output directory version $outputVersion does not match APK version $nextVersion." }
$manifestText = $manifestText -replace 'android:versionCode="\d+"', ('android:versionCode="{0}"' -f $nextCode)
$manifestText = $manifestText -replace 'android:versionName="[^"]+"', ('android:versionName="{0}"' -f $nextVersion)
[IO.File]::WriteAllText($manifest, $manifestText, (New-Object Text.UTF8Encoding($false)))
Write-Host "Building version $nextVersion ($nextCode)"
$stagedAssets = Join-Path $work 'assets'
$mobileAssets = Join-Path $stagedAssets 'mobile'
New-Item -ItemType Directory -Force "$mobileAssets/vendor" | Out-Null
$defaultsPath = if ($ConnectionDefaults) { [IO.Path]::GetFullPath($ConnectionDefaults) } else { Join-Path $repoRoot '.omx/android-connection-defaults.json' }
$defaultsJson = '{}'
# Generic builds never silently embed a developer's local credentials.
if ($ConnectionDefaults) {
    $defaults = Get-Content -LiteralPath $defaultsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (!$defaults.endpoint -or $defaults.endpoint -notmatch '^https?://') { throw 'Connection defaults need an HTTP(S) endpoint' }
    $defaultsJson = [ordered]@{ endpoint = [string]$defaults.endpoint; code = [string]$defaults.code; token = [string]$defaults.token } | ConvertTo-Json -Compress
}
[IO.File]::WriteAllText((Join-Path $stagedAssets 'connection-defaults.json'), $defaultsJson, (New-Object Text.UTF8Encoding($false)))
foreach ($name in @('index.html', 'app.js', 'app.css', 'mobile-core.js')) {
    Copy-Item -LiteralPath "$repoRoot/android/assets/mobile/$name" -Destination $mobileAssets
}
New-Item -ItemType Directory -Force "$mobileAssets/agent-modes" | Out-Null
foreach ($name in @('codex', 'claude-code', 'grok', 'kimi', 'pi-dev', 'cursor-cli')) {
    Copy-Item -LiteralPath "$repoRoot/src/supermonoagent/static/agent-modes/$name.json" -Destination "$mobileAssets/agent-modes"
}
Copy-Item -LiteralPath "$repoRoot/src/supermonoagent/static/update-trust.json" -Destination (Join-Path $stagedAssets 'update-trust.json')
Copy-Item -LiteralPath "$repoRoot/android/assets/update-bridge.js" -Destination (Join-Path $stagedAssets 'update-bridge.js')
Copy-Item -LiteralPath "$repoRoot/android/assets/update-api.json" -Destination (Join-Path $stagedAssets 'update-api.json')
$changesPath = Join-Path $repoRoot 'android/CHANGELOG.txt'
if (Test-Path -LiteralPath $changesPath) { Copy-Item -LiteralPath $changesPath -Destination (Join-Path $stagedAssets 'CHANGELOG.txt') }
foreach ($name in @('xterm.js', 'xterm.css', 'xterm-fit.js', 'xterm-unicode11.js', 'xterm-webgl.js', 'terminal-client.js', 'file-client.js', 'file-upload.js')) {
    Copy-Item -LiteralPath "$repoRoot/src/supermonoagent/static/$name" -Destination "$mobileAssets/vendor"
}
# Hash the exact packaged bytes, keeping shared vendor files in their existing source location.
$assetHashes = @(Get-ChildItem -LiteralPath $stagedAssets -Recurse -File | Sort-Object FullName | ForEach-Object {
    [ordered]@{ path = $_.FullName.Substring($stagedAssets.Length + 1).Replace('\', '/'); sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLower() }
})
Invoke-Checked "$buildTools/aapt2.exe" @('compile', '--dir', "$repoRoot/android/res", '-o', "$work/resources.zip")
Invoke-Checked "$buildTools/aapt2.exe" @('link', '-o', "$work/unsigned.apk", '-I', $androidJar, '--manifest', $manifest, '-A', $stagedAssets, "$work/resources.zip")
$sources = @(Get-ChildItem "$repoRoot/android/src" -Filter '*.java' -Recurse | ForEach-Object { $_.FullName })
Invoke-Checked "$env:JAVA_HOME/bin/javac.exe" (@('-encoding', 'UTF-8', '-source', '8', '-target', '8', '-Xlint:-options', '-classpath', $androidJar, '-d', "$work/classes") + $sources)
$classes = @(Get-ChildItem "$work/classes" -Filter '*.class' -Recurse | ForEach-Object { $_.FullName })
Invoke-Checked "$buildTools/d8.bat" (@('--lib', $androidJar, '--min-api', '26', '--output', "$work/dex") + $classes)
# Append DEX without rewriting AAPT2 asset local headers. jar uf rewrites deflated
# assets with data descriptors, which some Android AssetManagers cannot read.
$python = Join-Path $repoRoot '.venv/Scripts/python.exe'
if (!(Test-Path -LiteralPath $python)) { $python = (Get-Command python -ErrorAction Stop).Source }
Invoke-Checked $python @("$PSScriptRoot/append-apk-dex.py", "$work/unsigned.apk", "$work/dex/classes.dex")
Invoke-Checked "$buildTools/zipalign.exe" @('-f', '-p', '4', "$work/unsigned.apk", "$work/aligned.apk")
$keystore = Join-Path $ToolsRoot 'android-debug.keystore'
if (!(Test-Path $keystore)) {
    Invoke-Checked "$env:JAVA_HOME/bin/keytool.exe" @('-genkeypair', '-keystore', $keystore, '-storepass', 'android', '-keypass', 'android', '-alias', 'androiddebugkey', '-dname', 'CN=Android Debug,O=Android,C=US', '-keyalg', 'RSA', '-keysize', '2048', '-validity', '10000')
}
$apk = Join-Path $dist 'SuperMonoAgent.apk'
$previous = Join-Path $repoRoot '.omx/android-v1'
if ((Test-Path -LiteralPath $apk) -and !(Test-Path -LiteralPath "$previous/SuperMonoAgent.apk")) {
    New-Item -ItemType Directory -Force $previous | Out-Null
    Copy-Item -LiteralPath $apk -Destination "$previous/SuperMonoAgent.apk"
}
Invoke-Checked "$buildTools/apksigner.bat" @('sign', '--ks', $keystore, '--ks-pass', 'pass:android', '--key-pass', 'pass:android', '--ks-key-alias', 'androiddebugkey', '--out', $apk, "$work/aligned.apk")
$verification = & "$buildTools/apksigner.bat" verify --verbose --print-certs $apk
if ($LASTEXITCODE -ne 0) { throw 'APK signature verification failed' }
$verification | Tee-Object -FilePath "$dist/apksigner-verification.txt"
$sha = (Get-FileHash $apk -Algorithm SHA256).Hash.ToLower()
"$sha  SuperMonoAgent.apk" | Set-Content "$dist/SuperMonoAgent.apk.sha256" -Encoding ascii
$assetHashes | ConvertTo-Json | Set-Content "$dist/packaged-assets.json" -Encoding utf8
& "$env:JAVA_HOME/bin/java.exe" --version | Out-File "$dist/toolchain.txt"
Get-Content "$sdk/build-tools/35.0.0/source.properties", "$sdk/platforms/android-35/source.properties", "$sdk/platform-tools/source.properties", "$sdk/cmdline-tools/latest/source.properties" | Out-File "$dist/toolchain.txt" -Append
Get-Content "$ToolsRoot/jdk-download.json" | Out-File "$dist/toolchain.txt" -Append
Get-FileHash "$ToolsRoot/android-cli.zip" -Algorithm SHA256 | Format-List | Out-File "$dist/toolchain.txt" -Append
Write-Host "APK: $apk ($((Get-Item $apk).Length) bytes), SHA256: $sha"
} finally { $androidBuildLock.Dispose() }
