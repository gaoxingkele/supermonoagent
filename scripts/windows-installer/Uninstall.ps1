param(
    [string]$InstallRoot = $PSScriptRoot,
    [switch]$NoShortcuts,
    [switch]$NoRegistry
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

function Test-SmaVersionInUse([string]$Path) {
    $prefix = [IO.Path]::GetFullPath($Path).TrimEnd('\') + '\'
    foreach ($process in @(Get-Process -Name SuperMonoAgent -ErrorAction SilentlyContinue)) {
        try {
            $executable = $process.Path
            if (!$executable) { return $true }
            if ([IO.Path]::GetFullPath($executable).StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return $true }
        } catch { return $true }
    }
    return $false
}
function Assert-SmaUninstallTree([string]$Path, [string]$Root) {
    $full = [IO.Path]::GetFullPath($Path)
    $prefix = [IO.Path]::GetFullPath((Join-Path $Root 'versions')).TrimEnd('\') + '\'
    if (!$full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or (Split-Path $full -Leaf) -cnotmatch '^\d{1,6}\.\d{1,6}\.\d{1,6}$') { throw 'Refusing to remove an unmanaged directory.' }
    $pending = New-Object 'Collections.Generic.Stack[string]'
    $pending.Push($full)
    while ($pending.Count) {
        $directory = $pending.Pop()
        if (((Get-Item -LiteralPath $directory -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Uninstall will not traverse links.' }
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Uninstall will not traverse links.' }
            if ($item.PSIsContainer) { $pending.Push($item.FullName) }
        }
    }
}
function Invoke-SmaUninstall([string]$Root) {
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    if (!$rootPath -or $rootPath -eq [IO.Path]::GetPathRoot($rootPath).TrimEnd('\', '/')) { throw 'Refusing to uninstall a drive root.' }
    foreach ($path in @($rootPath, (Join-Path $rootPath 'versions'))) {
        if ((Test-Path -LiteralPath $path) -and ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Uninstall root must not be a link.' }
    }
    $currentPath = Join-Path $rootPath 'current.json'
    if (!(Test-Path -LiteralPath $currentPath) -or (Get-Item -LiteralPath $currentPath).Length -gt 16384) { throw 'Managed installation metadata is missing.' }
    try { $current = [IO.File]::ReadAllText($currentPath, [Text.Encoding]::UTF8) | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'Managed installation metadata is invalid.' }
    if ($current.schema -ne 1 -or $current.version -cnotmatch '^\d{1,6}\.\d{1,6}\.\d{1,6}$' -or $current.catalog_sha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'Managed installation metadata is invalid.' }
    [IO.File]::WriteAllText((Join-Path $rootPath '.updates-disabled'), 'uninstall requested')
    $removed = @();$preserved = @()
    $versions = Join-Path $rootPath 'versions'
    if (Test-Path -LiteralPath $versions) {
        foreach ($directory in Get-ChildItem -LiteralPath $versions -Directory -Force) {
            if ($directory.Name -cnotmatch '^\d{1,6}\.\d{1,6}\.\d{1,6}$' -or !(Test-Path -LiteralPath (Join-Path $directory.FullName '.install-manifest.json'))) { continue }
            if (Test-SmaVersionInUse $directory.FullName) { $preserved += $directory.Name;continue }
            try { Assert-SmaUninstallTree $directory.FullName $rootPath;Remove-Item -LiteralPath $directory.FullName -Recurse -Force;$removed += $directory.Name }
            catch { $preserved += $directory.Name }
        }
    }
    if (!$NoShortcuts) {
        $shortcut = Join-Path ([Environment]::GetFolderPath('Desktop')) 'SuperMonoAgent.lnk'
        if (Test-Path -LiteralPath $shortcut) {
            $shell = New-Object -ComObject WScript.Shell
            $link = $shell.CreateShortcut($shortcut)
            if ($link.Description -eq 'SuperMonoAgent installed application' -and $link.Arguments.Contains((Join-Path $rootPath 'Launch.ps1'))) { Remove-Item -LiteralPath $shortcut -Force }
        }
    }
    if (!$NoRegistry) {
        $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\SuperMonoAgent'
        if (Test-Path $key) {
            $prior = Get-ItemProperty $key
            if ($prior.DisplayName -eq 'SuperMonoAgent' -and $prior.InstallLocation -eq $rootPath) { Remove-Item -Path $key -Recurse -Force }
        }
    }
    if (!$preserved.Count) {
        foreach ($name in @('current.json', 'previous.json', 'config.json', 'Launch.ps1', 'Install.ps1', 'Update.ps1')) {
            $path = Join-Path $rootPath $name
            if (Test-Path -LiteralPath $path -PathType Leaf) { Remove-Item -LiteralPath $path -Force }
        }
    }
    return @{ removed_versions = $removed; preserved_versions = $preserved; state_preserved = $true; support_script = (Join-Path $rootPath 'Uninstall.ps1') }
}
if ($MyInvocation.InvocationName -ne '.') {
    try { Invoke-SmaUninstall $InstallRoot | ConvertTo-Json -Compress -Depth 5 }
    catch { Write-Error ('SuperMonoAgent uninstall failed: ' + $_.Exception.Message); exit 1 }
}
