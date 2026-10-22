param([string]$ToolsRoot = "")
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
if (!$ToolsRoot) { $ToolsRoot = Join-Path $repoRoot '.omx/tools' }
$ToolsRoot = [IO.Path]::GetFullPath($ToolsRoot)
New-Item -ItemType Directory -Force $ToolsRoot | Out-Null
$jdkRoot = Join-Path $ToolsRoot 'jdk'
if (!(Get-ChildItem "$jdkRoot/*/bin/java.exe" -ErrorAction SilentlyContinue)) {
    $metadata = Invoke-RestMethod 'https://api.adoptium.net/v3/assets/latest/21/hotspot?architecture=x64&image_type=jdk&os=windows&vendor=eclipse'
    $package = $metadata[0].binary.package
    $package | ConvertTo-Json | Set-Content (Join-Path $ToolsRoot 'jdk-download.json')
    $archive = Join-Path $ToolsRoot 'jdk.zip'
    Invoke-WebRequest -UseBasicParsing $package.link -OutFile $archive
    if ((Get-FileHash $archive -Algorithm SHA256).Hash.ToLower() -ne $package.checksum) { throw 'JDK checksum mismatch' }
    Expand-Archive $archive $jdkRoot -Force
}
$env:JAVA_HOME = (Get-ChildItem "$jdkRoot/*/bin/java.exe" | Select-Object -First 1).Directory.Parent.FullName
$env:PATH = "$env:JAVA_HOME/bin;$env:PATH"
$sdk = Join-Path $ToolsRoot 'android-sdk'
$manager = Join-Path $sdk 'cmdline-tools/latest/bin/sdkmanager.bat'
if (!(Test-Path $manager)) {
    $archive = Join-Path $ToolsRoot 'android-cli.zip'
    $hash = '90ae805d20434428bffcb699c290860f19bb5f66a67e6b330067e3de801fb04a'
    if (!(Test-Path $archive)) {
        Invoke-WebRequest -UseBasicParsing 'https://dl.google.com/android/repository/commandlinetools-win-15859902_latest.zip' -OutFile $archive
    }
    if ((Get-FileHash $archive -Algorithm SHA256).Hash.ToLower() -ne $hash) { throw 'Android command-line tools checksum mismatch' }
    $extracted = Join-Path $ToolsRoot 'android-cli'
    Expand-Archive $archive $extracted -Force
    New-Item -ItemType Directory -Force (Split-Path $manager -Parent | Split-Path -Parent) | Out-Null
    Copy-Item "$extracted/cmdline-tools/*" (Join-Path $sdk 'cmdline-tools/latest') -Recurse -Force
}
# Accept only license prompts for the packages required by this portable build.
1..20 | ForEach-Object { 'y' } | & $manager "--sdk_root=$sdk" 'platforms;android-35' 'build-tools;35.0.0' 'platform-tools'
if ($LASTEXITCODE -ne 0) { throw 'SDK package installation failed' }
Write-Host "Portable Android SDK ready: $sdk"
