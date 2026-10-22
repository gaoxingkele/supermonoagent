param([string]$Python = "", [string]$OutputDir = "dist/windows")
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
if (!$Python) { $Python = Join-Path $repoRoot '.venv/Scripts/python.exe' }
if (!(Test-Path $Python)) { throw 'Create .venv and install .[desktop] plus pyinstaller before building.' }
$dist = [IO.Path]::GetFullPath((Join-Path $repoRoot $OutputDir))
$work = Join-Path $repoRoot '.omx/pyinstaller'
New-Item -ItemType Directory -Force $dist, $work | Out-Null
$buildArgs = @('-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--console',
    '--name', 'SuperMonoAgent', '--distpath', $dist, '--workpath', "$work/build", '--specpath', $work,
    '--paths', "$repoRoot/src", '--add-data', "$repoRoot/src/supermonoagent/static:supermonoagent/static",
    '--hidden-import', 'supermonoagent.daemon', '--hidden-import', 'supermonoagent.relay',
    '--hidden-import', 'supermonoagent.frontend',
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
Copy-Item "$repoRoot/docs/windows-clients.md" "$dist/README.md" -Force
$exe = Join-Path $dist 'SuperMonoAgent/SuperMonoAgent.exe'
$sha = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
"$sha  SuperMonoAgent/SuperMonoAgent.exe" | Set-Content "$dist/SuperMonoAgent.exe.sha256" -Encoding ascii
$sources = @(Get-ChildItem "$repoRoot/src/supermonoagent" -File -Recurse | Where-Object { $_.Extension -in '.py', '.js', '.css' } | ForEach-Object {
    @{ path = $_.FullName.Substring($repoRoot.Length + 1).Replace('\', '/'); sha256 = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower() }
})
@{ built_at = [DateTime]::UtcNow.ToString('o'); exe_sha256 = $sha; sources = $sources } | ConvertTo-Json -Depth 4 | Set-Content "$dist/build-manifest.json" -Encoding UTF8
Write-Host "Windows clients ready: $dist"
