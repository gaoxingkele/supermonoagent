param([string]$Python = "", [string]$OutputDir = "")
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
New-Item -ItemType Directory -Force (Join-Path $repoRoot '.omx') | Out-Null
try { $windowsBuildLock = [IO.File]::Open((Join-Path $repoRoot '.omx/windows-build.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
catch { throw 'Cannot acquire the Windows build lock; another build may be running.' }
try {
if (!$Python) { $Python = Join-Path $repoRoot '.venv/Scripts/python.exe' }
if (!(Test-Path $Python)) { throw 'Create .venv and install .[desktop] plus pyinstaller before building.' }
[xml]$releaseManifest = Get-Content -LiteralPath "$repoRoot/android/AndroidManifest.xml" -Raw
$releaseVersion = $releaseManifest.manifest.GetAttribute('versionName', 'http://schemas.android.com/apk/res/android')
if ($releaseVersion -notmatch '^\d+\.\d+\.\d+$') { throw 'Android release version must be major.minor.patch.' }
if (!$OutputDir) { $OutputDir = "dist/windows/$releaseVersion" }
$dist = [IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDir))
$outputVersion = Split-Path $dist -Leaf
if ($outputVersion -match '^\d+\.\d+\.\d+$' -and $outputVersion -ne $releaseVersion) { throw "Output directory version $outputVersion does not match Windows version $releaseVersion." }
$work = Join-Path $repoRoot '.omx/pyinstaller'
$appIcon = Join-Path $repoRoot 'src/supermonoagent/static/app-icon.ico'
if (!(Test-Path $appIcon)) { throw 'The shared mobile/desktop app icon is missing.' }
New-Item -ItemType Directory -Force $dist, $work | Out-Null
$versionParts = $releaseVersion.Split('.')
$versionTuple = ($versionParts + '0') -join ', '
$versionFile = Join-Path $work 'windows-version.txt'
$versionResource = @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($versionTuple), prodvers=($versionTuple), mask=0x3f,
                   flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'SuperMonoAgent'),
    StringStruct('FileDescription', 'SuperMonoAgent'),
    StringStruct('FileVersion', '$releaseVersion'),
    StringStruct('InternalName', 'SuperMonoAgent'),
    StringStruct('OriginalFilename', 'SuperMonoAgent.exe'),
    StringStruct('ProductName', 'SuperMonoAgent'),
    StringStruct('ProductVersion', '$releaseVersion')
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"@
[IO.File]::WriteAllText($versionFile, $versionResource, [Text.Encoding]::ASCII)
$buildArgs = @('-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--console',
    '--name', 'SuperMonoAgent', '--icon', $appIcon, '--version-file', $versionFile, '--distpath', $dist, '--workpath', "$work/build", '--specpath', $work,
    '--paths', "$repoRoot/src", '--add-data', "$repoRoot/src/supermonoagent/static:supermonoagent/static",
    '--hidden-import', 'supermonoagent.daemon', '--hidden-import', 'supermonoagent.relay',
    '--hidden-import', 'supermonoagent.frontend',
    '--hidden-import', 'supermonoagent.notification_bridge',
    '--hidden-import', 'supermonoagent.relay_bridge', '--hidden-import', 'supermonoagent.console_snapshot', '--hidden-import', 'supermonoagent.daemon_logs',
    '--hidden-import', 'supermonoagent.desktop', '--hidden-import', 'supermonoagent.microsoft',
    '--hidden-import', 'supermonoagent.native_attach', '--hidden-import', 'webview.platforms.winforms',
    '--hidden-import', 'webview.platforms.edgechromium', '--hidden-import', 'clr',
    '--copy-metadata', 'pythonnet', '--exclude-module', 'playwright', '--exclude-module', 'pytest',
    '--exclude-module', 'tkinter', '--exclude-module', 'PyQt5', '--exclude-module', 'PyQt6',
    '--exclude-module', 'PySide2', '--exclude-module', 'PySide6', '--exclude-module', 'gi',
    "$repoRoot/src/supermonoagent/launcher.py")
$winptyRoot = & $Python -c "import pathlib, winpty; print(pathlib.Path(winpty.__file__).parent)"
if ($LASTEXITCODE -ne 0) { throw 'pywinpty is missing from the build environment' }
foreach ($helper in @('OpenConsole.exe', 'winpty-agent.exe')) {
    $helperPath = Join-Path $winptyRoot $helper
    if (Test-Path $helperPath) { $buildArgs += @('--add-binary', ($helperPath + ':winpty')) }
}
& $Python @buildArgs
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
$entries = [ordered]@{'01-Browser.cmd'='browser --workspace'; '02-Orca-Workspace.cmd'='workspace'; '03-Intelligent-Terminal.cmd'='microsoft'}
foreach ($entry in $entries.GetEnumerator()) {
    if ($entry.Value -eq 'microsoft') {
        $lines = @('@echo off', 'setlocal', ('"%~dp0SuperMonoAgent\SuperMonoAgent.exe" ' + $entry.Value + ' %*'), 'if errorlevel 1 pause')
    } else {
        $lines = @('@echo off', 'setlocal', ('start "" "%SystemRoot%\System32\wscript.exe" "%~dp0launch-hidden.vbs" "%~dp0SuperMonoAgent\SuperMonoAgent.exe" ' + $entry.Value + ' %*'))
    }
    [IO.File]::WriteAllLines((Join-Path $dist $entry.Key), $lines, [Text.Encoding]::ASCII)
    if ($entry.Value -ne 'microsoft') {
        $shortcut = @('Option Explicit', 'Dim shell, folder, command',
            'Set shell = CreateObject("WScript.Shell")',
            'folder = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)',
            ('command = Chr(34) & shell.ExpandEnvironmentStrings("%SystemRoot%\System32\wscript.exe") & Chr(34) & " " & Chr(34) & folder & "\launch-hidden.vbs" & Chr(34) & " " & Chr(34) & folder & "\SuperMonoAgent\SuperMonoAgent.exe" & Chr(34) & " ' + $entry.Value + '"'),
            'shell.Run command, 0, False')
        [IO.File]::WriteAllLines((Join-Path $dist $entry.Key.Replace('.cmd','.vbs')), $shortcut, [Text.Encoding]::ASCII)
    }
}
Copy-Item "$repoRoot/scripts/launch-hidden.vbs" "$dist/launch-hidden.vbs" -Force
Copy-Item "$repoRoot/scripts/create-desktop-shortcuts.vbs" "$dist/create-desktop-shortcuts.vbs" -Force
Copy-Item "$repoRoot/docs/windows-clients.md" "$dist/README.md" -Force
Copy-Item "$repoRoot/docs/windows-installation-plan.md" "$dist/windows-installation-plan.md" -Force
Copy-Item "$repoRoot/docs/windows-oneclick-install.md" "$dist/windows-oneclick-install.md" -Force
Copy-Item "$repoRoot/docs/clipboard-files.md" "$dist/clipboard-files.md" -Force
$exe = Join-Path $dist 'SuperMonoAgent/SuperMonoAgent.exe'
$sha = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
"$sha  SuperMonoAgent/SuperMonoAgent.exe" | Set-Content "$dist/SuperMonoAgent.exe.sha256" -Encoding ascii
$sources = @(Get-ChildItem "$repoRoot/src/supermonoagent" -File -Recurse | Where-Object { $_.Extension -in '.py', '.js', '.css', '.json', '.ico', '.png' } | ForEach-Object {
    @{ path = $_.FullName.Substring($repoRoot.Length + 1).Replace('\', '/'); sha256 = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower() }
})
[IO.File]::WriteAllText((Join-Path $dist 'version.txt'), $releaseVersion + "`n", [Text.Encoding]::ASCII)
@{ version = $releaseVersion; built_at = [DateTime]::UtcNow.ToString('o'); exe_sha256 = $sha; sources = $sources } | ConvertTo-Json -Depth 4 | Set-Content "$dist/build-manifest.json" -Encoding UTF8
Write-Host "Windows clients ready: $dist"
} finally { $windowsBuildLock.Dispose() }
