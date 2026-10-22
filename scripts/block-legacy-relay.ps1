param(
    [Parameter(Mandatory=$true)][string]$ProgramPath,
    [string]$EvidenceFile = ""
)
$ErrorActionPreference = 'Stop'
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (!$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Administrator approval is required for this scoped firewall rule.' }
$program = (Resolve-Path -LiteralPath $ProgramPath).Path
$name = 'SuperMonoAgent-Block-Legacy-117-8817'
$existing = Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue
if ($existing) {
    $app = $existing | Get-NetFirewallApplicationFilter
    $address = $existing | Get-NetFirewallAddressFilter
    $port = $existing | Get-NetFirewallPortFilter
    if ($app.Program -ne $program -or $address.RemoteAddress -ne '117.50.48.22' -or $port.RemotePort -ne '8817' -or $existing.Action -ne 'Block') { throw 'An unexpected rule already uses this name; no changes were made.' }
} else {
    New-NetFirewallRule -Name $name -DisplayName 'SuperMonoAgent: block legacy plaintext relay' `
        -Direction Outbound -Action Block -Enabled True -Profile Any -Protocol TCP `
        -Program $program -RemoteAddress '117.50.48.22' -RemotePort 8817 | Out-Null
}
$report = @{ rule=$name; program=$program; remote_address='117.50.48.22'; remote_port=8817; blocked=$true; loopback_unchanged=$true }
if ($EvidenceFile) { [IO.File]::WriteAllText([IO.Path]::GetFullPath($EvidenceFile), ($report | ConvertTo-Json), [Text.UTF8Encoding]::new($false)) }
$report | ConvertTo-Json -Compress
