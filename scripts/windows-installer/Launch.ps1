param([string]$InstallRoot = $PSScriptRoot)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

function Read-SmaLaunchJson([string]$Path) {
    if ((Get-Item -LiteralPath $Path).Length -gt 32768) { throw 'Installed configuration is too large.' }
    try { return ([IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8) | ConvertFrom-Json -ErrorAction Stop) }
    catch { throw 'Installed configuration is invalid.' }
}
function Get-SmaLaunchChild([string]$Root, [string]$Relative) {
    if (!$Relative -or [IO.Path]::IsPathRooted($Relative) -or $Relative.Contains('\') -or $Relative.Contains(':')) { throw 'Invalid installed entry point.' }
    foreach ($part in $Relative.Split('/')) { if (!$part -or $part -in @('.', '..') -or $part.EndsWith('.') -or $part.EndsWith(' ')) { throw 'Invalid installed entry point.' } }
    $base = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $target = [IO.Path]::GetFullPath((Join-Path $base $Relative.Replace('/', '\')))
    if (!$target.StartsWith($base + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Installed entry escapes the version directory.' }
    $current = $target
    while ($current -and $current.Length -ge $base.Length) {
        if ((Test-Path -LiteralPath $current) -and ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Installed application contains a link.' }
        $current = [IO.Path]::GetDirectoryName($current)
    }
    return $target
}
function Get-SmaMainProcess {
    foreach ($process in @(Get-Process -Name SuperMonoAgent -ErrorAction SilentlyContinue)) {
        try {
            if ($process.SessionId -eq [Diagnostics.Process]::GetCurrentProcess().SessionId -and $process.MainWindowHandle -ne 0 -and $process.MainWindowTitle -match '^SuperMono(?:Agent)?\b' -and $process.MainWindowTitle -notmatch '(?i:logs|\u65e5\u5fd7)') { return $process }
        } catch { }
    }
    return $null
}
function Test-SmaMainWindow { return $null -ne (Get-SmaMainProcess) }
function Show-SmaMainWindow {
    $main = Get-SmaMainProcess
    if ($null -eq $main) { return $false }
    try {
        if (-not ('SuperMonoAgent.InstallerWindow' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace SuperMonoAgent {
    public static class InstallerWindow {
        [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr handle);
        [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr handle, int command);
        [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr handle);
    }
}
'@
        }
        $handle = [IntPtr]$main.MainWindowHandle
        if ($handle -eq [IntPtr]::Zero) { return $false }
        if ([SuperMonoAgent.InstallerWindow]::IsIconic($handle)) { [void][SuperMonoAgent.InstallerWindow]::ShowWindowAsync($handle, 9) }
        return [SuperMonoAgent.InstallerWindow]::SetForegroundWindow($handle)
    } catch { return $false }
}
function Test-SmaDesktopRuntime {
    $release = 0
    try { $release = [int](Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full' -Name Release -ErrorAction Stop).Release } catch { }
    if ($release -lt 394802) { return $false }
    # Microsoft Learn: microsoft-edge/webview2/concepts/distribution.
    $client = 'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    foreach ($key in @(('HKCU:\SOFTWARE\' + $client), ('HKLM:\SOFTWARE\WOW6432Node\' + $client), ('HKLM:\SOFTWARE\' + $client))) {
        try {
            $value = (Get-ItemProperty $key -Name pv -ErrorAction Stop).pv
            if ($value -and [version]$value -gt [version]'0.0.0.0') { return $true }
        } catch { }
    }
    return $false
}
function ConvertTo-SmaArgument([string]$Value) {
    $result = New-Object Text.StringBuilder
    [void]$result.Append('"');$slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') { $slashes++;continue }
        if ($character -eq '"') { [void]$result.Append(('\' * ($slashes * 2 + 1)));[void]$result.Append('"') }
        else { [void]$result.Append(('\' * $slashes));[void]$result.Append($character) }
        $slashes = 0
    }
    [void]$result.Append(('\' * ($slashes * 2)));[void]$result.Append('"')
    return $result.ToString()
}
function Get-SmaLaunchPlan([string]$Root) {
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $pointer = Read-SmaLaunchJson (Join-Path $rootPath 'current.json')
    $configuration = Read-SmaLaunchJson (Join-Path $rootPath 'config.json')
    if ($pointer.schema -ne 1 -or $pointer.version -cnotmatch '^\d{1,6}\.\d{1,6}\.\d{1,6}$' -or $configuration.schema -ne 1) { throw 'Unsupported installed configuration.' }
    $versionPath = Get-SmaLaunchChild $rootPath ('versions/' + $pointer.version)
    $executable = Get-SmaLaunchChild $versionPath $pointer.entry
    if (!(Test-Path -LiteralPath $executable -PathType Leaf) -or !$executable.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase)) { throw 'Installed executable is unavailable.' }
    $uri = $null
    if (![Uri]::TryCreate($configuration.relay, [UriKind]::Absolute, [ref]$uri) -or $uri.Scheme -ne 'wss' -or $uri.UserInfo -or $uri.Query -or $uri.Fragment) { throw 'Installed relay configuration is invalid.' }
    $state = [IO.Path]::GetFullPath($configuration.state_dir)
    $tokenFile = [IO.Path]::GetFullPath($configuration.token_file)
    if (!(Test-Path -LiteralPath $tokenFile -PathType Leaf)) { throw 'The existing private Token file is unavailable; it was not replaced.' }
    $desktop = Test-SmaDesktopRuntime
    $arguments = @('workspace')
    if (!$desktop) { $arguments = @('browser', '--workspace') }
    $arguments += @('--relay', $configuration.relay, '--token-file', $tokenFile, '--state-dir', $state)
    return @{ Executable = $executable; Arguments = $arguments; WorkingDirectory = $versionPath; StateDir = $state; Mode = $(if ($desktop) { 'workspace' } else { 'browser' }) }
}
function Get-SmaUpdateMutexName([string]$Root) {
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $value = ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes([IO.Path]::GetFullPath($Root).ToLowerInvariant())))).Replace('-', '') }
    finally { $hash.Dispose() }
    return 'Local\SuperMonoAgent.ProgramUpdate.' + [Security.Principal.WindowsIdentity]::GetCurrent().User.Value + '.' + $value
}
function Start-SmaUpdateProcess([Diagnostics.ProcessStartInfo]$Info) { return [Diagnostics.Process]::Start($Info) }
function Start-SmaUpdateWorker([string]$Root, [int]$OwnerPid = 0, [switch]$OneShot, [switch]$NewClient) {
    if ((Test-Path -LiteralPath (Join-Path $Root '.updates-disabled')) -or !(Test-Path -LiteralPath (Join-Path $Root 'Update.ps1')) -or !(Test-Path -LiteralPath (Join-Path $Root 'Install.ps1')) -or !(Test-Path -LiteralPath (Join-Path $Root 'current.json'))) { return $false }
    if (!$NewClient) { try {
        $existing = [Threading.Mutex]::OpenExisting((Get-SmaUpdateMutexName $Root))
        $existing.Dispose();return $true
    } catch [Threading.WaitHandleCannotBeOpenedException] { }
    catch [UnauthorizedAccessException] { return $true }
    }
    $owner = $null
    if (!$OneShot) {
        if ($OwnerPid -gt 0) { $owner = Get-Process -Id $OwnerPid -ErrorAction SilentlyContinue }
        if ($null -eq $owner) { $owner = Get-SmaMainProcess }
    }
    $arguments = @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $Root 'Update.ps1'), '-InstallRoot', $Root)
    if ($NewClient) { $arguments += '-WaitForPrevious' }
    try {
        if ($OneShot) { $arguments += '-CheckOnce' }
        elseif ($null -ne $owner) { $arguments += @('-OwnerPid', [string]$owner.Id, '-OwnerStartTicks', [string]$owner.StartTime.ToUniversalTime().Ticks) }
        else { $arguments += @('-CheckOnce', '-NoPrompt') }
        $info = New-Object Diagnostics.ProcessStartInfo
        $info.FileName = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $info.Arguments = ($arguments | ForEach-Object { ConvertTo-SmaArgument $_ }) -join ' '
        $info.WorkingDirectory = [IO.Path]::GetFullPath($Root)
        $info.UseShellExecute = $false
        $info.CreateNoWindow = $true
        $info.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
        $process = Start-SmaUpdateProcess $info
        if ($process -is [Diagnostics.Process]) { $process.Dispose() }
        return $true
    } catch { return $false }
}
function Invoke-SmaLaunch([string]$Root) {
    if (Test-SmaMainWindow) { $activated = Show-SmaMainWindow;[void](Start-SmaUpdateWorker $Root);return @{ started = $false; reason = 'existing-main-window'; activated = $activated } }
    $name = 'Local\SuperMonoAgent.InstalledMainLaunch.' + [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $mutex = New-Object Threading.Mutex($false, $name)
    $owned = $false
    try {
        try { $owned = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $owned = $true }
        if (!$owned) { return @{ started = $false; reason = 'main-window-starting' } }
        if (Test-SmaMainWindow) { $activated = Show-SmaMainWindow;[void](Start-SmaUpdateWorker $Root);return @{ started = $false; reason = 'existing-main-window'; activated = $activated } }
        $plan = Get-SmaLaunchPlan $Root
        $oldState = [Environment]::GetEnvironmentVariable('SUPERMONO_STATE_DIR', 'Process')
        try {
            [Environment]::SetEnvironmentVariable('SUPERMONO_STATE_DIR', $plan.StateDir, 'Process')
            $arguments = ($plan.Arguments | ForEach-Object { ConvertTo-SmaArgument $_ }) -join ' '
            $process = Start-Process -FilePath $plan.Executable -ArgumentList $arguments -WorkingDirectory $plan.WorkingDirectory -WindowStyle Hidden -PassThru
        } finally { [Environment]::SetEnvironmentVariable('SUPERMONO_STATE_DIR', $oldState, 'Process') }
        if ($plan.Mode -eq 'workspace') {
            $deadline = [DateTime]::UtcNow.AddSeconds(20)
            while (!$process.HasExited -and !(Test-SmaMainWindow) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 100 }
        }
        if ($process.HasExited -and $process.ExitCode -ne 0) { throw 'The installed application exited unsuccessfully; existing background terminals were not stopped.' }
        $starting = $plan.Mode -eq 'workspace' -and !$process.HasExited -and !(Test-SmaMainWindow)
        $ownerId = 0
        try { $ownerId = [int]$process.Id } catch { }
        [void](Start-SmaUpdateWorker $Root -OwnerPid $ownerId -OneShot:($plan.Mode -eq 'browser') -NewClient)
        return @{ started = (!$starting); starting = $starting; mode = $plan.Mode }
    } finally {
        if ($owned) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}
if ($MyInvocation.InvocationName -ne '.') {
    try { Invoke-SmaLaunch $InstallRoot | ConvertTo-Json -Compress }
    catch { Write-Error ('SuperMonoAgent launch failed: ' + $_.Exception.Message); exit 1 }
}
