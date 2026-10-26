param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\SuperMonoAgent'),
    [string]$StateDir = (Join-Path $env:USERPROFILE '.supermonoagent'),
    [switch]$NoLaunch,
    [switch]$NoShortcuts,
    [switch]$NoRegistry
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
$script:SmaStateExplicit = $PSBoundParameters.ContainsKey('StateDir')
$script:SmaUtf8 = New-Object Text.UTF8Encoding($false, $true)
$script:SmaKeyId = 'c728238ab3ebabab'
$script:SmaModulus = 'oDjM8PlquUwaRQfMNx5TWQVB5vY7nzEiC8GMS27SBVZsIpiQrjRJVFCkAnWQIEz+uNvHZ+TK4h2i+Q9tKxZyI0DicjS1G7Nrw0exSNmiyi5QO+hQfxl4U+g0/PII6Zffdnmi0mC1c5Mni01uVPmdi1Dj62NgyLyK5Uw4wUt/n1Tq5dXUTN592FGCtq9du5g5UvXLD8R8QS2ep2CHUO4DavnsbBHP+3iI0uKmOP9xwHZOxNy6tRa4jmeMJs1XBzBD8dGIlW4QJ2oNV3WF/Afm2SA8hDTxMkLqFa3i2Gh6WuYt88Ss6hWTzuoYhjZVovyLKjX1sNI69DvV4pCGhCC8LD99i61577+vzkA+HE0nyA48gkti4Fk1Rpgy1WQxpRoxgG2PFMSJSYtrcDSq9d9ONVu1lfTtIh0OvOJIPKb80OGbKsMLq2gj8/oWDE8OXHxtlTLX2qVvMlBqB4omYrsREzJ243fzrshiBJ4xPQrNY01M0SjNIgL9wYqjazoFnQ8p'
$script:SmaExponent = 'AQAB'

function Read-SmaBytes([string]$Path, [int]$Limit) {
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        if ($stream.Length -gt $Limit) { throw 'Installer metadata exceeds its size limit.' }
        $bytes = New-Object byte[] ([int]$stream.Length)
        $offset = 0
        while ($offset -lt $bytes.Length) {
            $count = $stream.Read($bytes, $offset, $bytes.Length - $offset)
            if (!$count) { throw 'Installer metadata changed while reading.' }
            $offset += $count
        }
        if ($stream.ReadByte() -ne -1) { throw 'Installer metadata changed while reading.' }
        return ,$bytes
    } finally { $stream.Dispose() }
}
function ConvertFrom-SmaJson([byte[]]$Bytes, [string]$Kind) {
    try { return ($script:SmaUtf8.GetString($Bytes) | ConvertFrom-Json -ErrorAction Stop) }
    catch { throw ($Kind + ' JSON is invalid.') }
}
function ConvertFrom-SmaStrictJson([byte[]]$Bytes, [string]$Kind) {
    $value = ConvertFrom-SmaJson $Bytes $Kind
    if ($value -isnot [pscustomobject]) { throw ($Kind + ' must be an object.') }
    $text = $script:SmaUtf8.GetString($Bytes)
    $objects = New-Object 'Collections.Generic.Stack[object]'
    $count = 0
    for ($index = 0; $index -lt $text.Length;) {
        $character = $text[$index]
        if ($character -eq '{') {
            $keys = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
            $objects.Push($keys)
            if ($objects.Count -gt 8) { throw ($Kind + ' is too deeply nested.') }
        } elseif ($character -eq '}') { [void]$objects.Pop() }
        elseif ($character -eq '"') {
            $start = $index; $index++
            while ($index -lt $text.Length) {
                if ($text[$index] -eq '\') { $index += 2;continue }
                if ($text[$index] -eq '"') { break }
                $index++
            }
            $index++; $next = $index
            while ($next -lt $text.Length -and [char]::IsWhiteSpace($text[$next])) { $next++ }
            if ($next -lt $text.Length -and $text[$next] -eq ':') {
                $count++
                if (!$objects.Count -or $count -gt 128) { throw ($Kind + ' has too many fields.') }
                $key = ConvertFrom-Json -InputObject $text.Substring($start, $index - $start) -ErrorAction Stop
                if (!$objects.Peek().Add([string]$key)) { throw ($Kind + ' contains duplicate fields.') }
            }
            continue
        }
        $index++
    }
    return $value
}
function Assert-SmaKeys($Value, [string[]]$Keys, [string]$Kind) {
    if ($null -eq $Value -or $Value -isnot [pscustomobject]) { throw ($Kind + ' must be an object.') }
    $actual = @($Value.PSObject.Properties.Name)
    if ($actual.Count -ne $Keys.Count) { throw ($Kind + ' has unexpected fields.') }
    foreach ($name in $Keys) { if ($actual -cnotcontains $name) { throw ($Kind + ' has missing fields.') } }
}
function Get-SmaHash([byte[]]$Bytes) {
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hash.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose() }
}
function Assert-SmaRelative([string]$Name) {
    if (!$Name -or $Name.Length -gt 240 -or $Name.StartsWith('/') -or $Name.Contains('\') -or [IO.Path]::IsPathRooted($Name)) { throw 'Unsafe catalog path.' }
    foreach ($part in $Name.Split('/')) {
        if (!$part -or $part -in @('.', '..') -or $part.EndsWith('.') -or $part.EndsWith(' ') -or
            $part.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0 -or $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)') { throw 'Unsafe catalog path.' }
    }
    if ($Name -eq '.install-manifest.json') { throw 'Reserved catalog path.' }
}
function Join-SmaChild([string]$Root, [string]$Name) {
    Assert-SmaRelative $Name
    $base = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $result = [IO.Path]::GetFullPath((Join-Path $base $Name.Replace('/', '\')))
    if (!$result.StartsWith($base + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Path escapes its directory.' }
    return $result
}
function Assert-SmaNoLink([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            if (((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Installer paths must not contain links or reparse points.' }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if ($parent -eq $current) { break }
        $current = $parent
    }
}
function Assert-SmaRoot([string]$Path) {
    $result = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    if (!$result -or $result -eq [IO.Path]::GetPathRoot($result).TrimEnd('\', '/')) { throw 'A drive root cannot be used as an application directory.' }
    Assert-SmaNoLink $result
    return $result
}
function Assert-SmaRelay([string]$Value) {
    $uri = $null
    if (![Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$uri) -or !$uri.IsWellFormedOriginalString() -or $uri.Scheme -ne 'wss' -or !$uri.Host -or $uri.UserInfo -or $uri.Query -or $uri.Fragment) { throw 'A secure relay URL without credentials is required.' }
}
function Test-SmaToken([string]$Value) {
    if (!$Value -or $Value.Length -gt 4096) { return $false }
    foreach ($character in $Value.ToCharArray()) { if ([char]::IsWhiteSpace($character) -or [char]::IsControl($character)) { return $false } }
    return $true
}
function Assert-SmaFileExchangeProfile($Value) {
    $keys = @('schema','broker_url','enrollment_key')
    if ($null -ne $Value -and $null -ne $Value.PSObject.Properties['broker_token']) { $keys += 'broker_token' }
    Assert-SmaKeys $Value $keys 'File exchange profile'
    if (($Value.schema -isnot [int] -and $Value.schema -isnot [long]) -or $Value.schema -ne 1 -or
        $Value.broker_url -isnot [string] -or $Value.broker_url.Length -gt 2048 -or
        $Value.broker_url -cmatch '[^\x21-\x7e]' -or $Value.broker_url.Contains('%') -or $Value.broker_url.Contains('\')) { throw 'File exchange profile is invalid.' }
    Assert-SmaRelay $Value.broker_url
    $uri = New-Object Uri($Value.broker_url)
    if ($uri.AbsolutePath -cne '/file/ws') { throw 'File exchange profile is invalid.' }
    if ($Value.enrollment_key -isnot [string] -or $Value.enrollment_key.Length -lt 32 -or $Value.enrollment_key.Length -gt 8192 -or
        $Value.enrollment_key -cmatch '[^\x21-\x7e]') { throw 'File exchange profile is invalid.' }
    if ($null -ne $Value.PSObject.Properties['broker_token']) {
        if ($Value.broker_token -isnot [string] -or !(Test-SmaToken $Value.broker_token) -or $Value.broker_token -cmatch '[^\x21-\x7e]') { throw 'File exchange profile is invalid.' }
    }
}
function Assert-SmaFile([string]$Path, $Record) {
    Assert-SmaKeys $Record @('size', 'sha256') 'File record'
    if (($Record.size -isnot [int] -and $Record.size -isnot [long]) -or $Record.size -lt 0 -or $Record.size -gt 268435456 -or $Record.sha256 -isnot [string] -or $Record.sha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'Invalid catalog file metadata.' }
    Assert-SmaNoLink $Path
    $file = Get-Item -LiteralPath $Path -Force
    if ($file.PSIsContainer -or $file.Length -ne $Record.size -or (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Record.sha256) { throw 'Package file integrity validation failed.' }
}
function Assert-SmaRuntime([string]$Root, $Files, [switch]$Installed) {
    if ($Files -isnot [pscustomobject] -or @($Files.PSObject.Properties).Count -lt 1 -or @($Files.PSObject.Properties).Count -gt 2048) { throw 'Invalid runtime catalog.' }
    Assert-SmaNoLink $Root
    $expected = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $total = [long]0
    foreach ($property in $Files.PSObject.Properties) {
        Assert-SmaRelative $property.Name
        if (!$expected.Add($property.Name)) { throw 'Case-colliding catalog paths.' }
        Assert-SmaFile (Join-SmaChild $Root $property.Name) $property.Value
        $total += [long]$property.Value.size
        if ($total -gt 1073741824) { throw 'Runtime exceeds its size limit.' }
    }
    $pending = New-Object 'Collections.Generic.Stack[string]'
    $pending.Push([IO.Path]::GetFullPath($Root))
    while ($pending.Count) {
        foreach ($item in Get-ChildItem -LiteralPath $pending.Pop() -Force) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Package contains a link or reparse point.' }
            if ($item.PSIsContainer) { $pending.Push($item.FullName);continue }
            $name = $item.FullName.Substring(([IO.Path]::GetFullPath($Root).TrimEnd('\')).Length + 1).Replace('\', '/')
            if ($Installed -and $name -eq '.install-manifest.json') { continue }
            if (!$expected.Contains($name)) { throw 'Package contains an unexpected runtime file.' }
        }
    }
}
function Read-SmaCatalog([string]$PackageRoot) {
    Assert-SmaNoLink $PackageRoot
    foreach ($item in Get-ChildItem -LiteralPath $PackageRoot -Force) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Package contains a link or reparse point.' }
    }
    $raw = Read-SmaBytes (Join-Path $PackageRoot 'install-manifest.json') 1048576
    $envelope = ConvertFrom-SmaJson $raw 'Signature envelope'
    Assert-SmaKeys $envelope @('schema', 'key_id', 'payload', 'signature') 'Signature envelope'
    if ($envelope.schema -ne 1 -or $envelope.key_id -cne $script:SmaKeyId) { throw 'Untrusted installer signature key.' }
    try { $payloadBytes = [Convert]::FromBase64String($envelope.payload); $signature = [Convert]::FromBase64String($envelope.signature) }
    catch { throw 'Invalid installer signature encoding.' }
    $parameters = New-Object Security.Cryptography.RSAParameters
    $parameters.Modulus = [Convert]::FromBase64String($script:SmaModulus)
    $parameters.Exponent = [Convert]::FromBase64String($script:SmaExponent)
    $rsa = [Security.Cryptography.RSA]::Create()
    try {
        $rsa.ImportParameters($parameters)
        if (!$rsa.VerifyData($payloadBytes, $signature, [Security.Cryptography.HashAlgorithmName]::SHA256, [Security.Cryptography.RSASignaturePadding]::Pkcs1)) { throw 'Installer signature validation failed.' }
    } finally { $rsa.Dispose() }
    $catalog = ConvertFrom-SmaJson $payloadBytes 'Signed catalog'
    Assert-SmaKeys $catalog @('schema', 'format', 'version', 'arch', 'entry', 'relay', 'files', 'bootstrap_files', 'defaults_private_sha256') 'Signed catalog'
    if ($catalog.schema -ne 1 -or $catalog.format -cne 'supermono-windows-install-v1' -or $catalog.arch -cne 'x64' -or $catalog.version -isnot [string] -or $catalog.version -cnotmatch '^\d{1,6}\.\d{1,6}\.\d{1,6}$') { throw 'Unsupported installer catalog.' }
    if (![Environment]::Is64BitOperatingSystem) { throw 'This package requires 64-bit Windows.' }
    Assert-SmaRelay $catalog.relay
    Assert-SmaRelative $catalog.entry
    if (!$catalog.entry.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase) -or $null -eq $catalog.files.PSObject.Properties[$catalog.entry]) { throw 'The entry point is not in the signed runtime.' }
    $bootstrapNames = @('Install.cmd', 'Install.ps1', 'Launch.ps1', 'Uninstall.ps1')
    if ($null -ne $catalog.bootstrap_files.PSObject.Properties['Update.ps1']) { $bootstrapNames += 'Update.ps1' }
    Assert-SmaKeys $catalog.bootstrap_files $bootstrapNames 'Bootstrap catalog'
    foreach ($name in $bootstrapNames) { Assert-SmaFile (Join-Path $PackageRoot $name) $catalog.bootstrap_files.PSObject.Properties[$name].Value }
    Assert-SmaRuntime (Join-Path $PackageRoot 'payload') $catalog.files
    $profile = $null
    $profilePath = Join-Path $PackageRoot 'defaults.private.json'
    if ($null -ne $catalog.defaults_private_sha256) {
        if ($catalog.defaults_private_sha256 -isnot [string] -or $catalog.defaults_private_sha256 -cnotmatch '^[0-9a-f]{64}$') { throw 'Invalid private defaults digest.' }
        $profileBytes = Read-SmaBytes $profilePath 65536
        if ((Get-SmaHash $profileBytes) -cne $catalog.defaults_private_sha256) { throw 'Private defaults integrity validation failed.' }
        $profile = ConvertFrom-SmaStrictJson $profileBytes 'Private defaults'
        $privateKeys = @('relay', 'token', 'room_mode')
        if ($null -ne $profile.PSObject.Properties['file_exchange']) { $privateKeys += 'file_exchange' }
        Assert-SmaKeys $profile $privateKeys 'Private defaults'
        if ($profile.relay -cne $catalog.relay -or $profile.room_mode -cne 'computer-name' -or $profile.token -isnot [string] -or !(Test-SmaToken $profile.token)) { throw 'Private defaults are invalid.' }
        if ($null -ne $profile.PSObject.Properties['file_exchange']) { Assert-SmaFileExchangeProfile $profile.file_exchange }
    } elseif (Test-Path -LiteralPath $profilePath) { throw 'Unsigned private defaults are not allowed.' }
    $allowed = @($bootstrapNames) + @('install-manifest.json', 'payload')
    if ($null -ne $profile) { $allowed += 'defaults.private.json' }
    foreach ($item in Get-ChildItem -LiteralPath $PackageRoot -Force) { if ($allowed -cnotcontains $item.Name) { throw 'Package contains an unexpected top-level item.' } }
    return @{ Catalog = $catalog; EnvelopeBytes = $raw; Profile = $profile }
}
function Write-SmaAtomic([string]$Path, [byte[]]$Bytes) {
    Assert-SmaNoLink $Path
    $temporary = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [IO.File]::WriteAllBytes($temporary, $Bytes)
        if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temporary, $Path, [NullString]::Value) }
        else { [IO.File]::Move($temporary, $Path) }
    } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
}
function Write-SmaJson([string]$Path, $Value) { Write-SmaAtomic $Path $script:SmaUtf8.GetBytes(($Value | ConvertTo-Json -Depth 8)) }
function Write-SmaPrivateNew([string]$Path, [string]$Text) {
    $security = New-Object Security.AccessControl.FileSecurity
    $security.SetAccessRuleProtection($true, $false)
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $security.SetOwner($sid)
    foreach ($identity in @($sid, (New-Object Security.Principal.SecurityIdentifier('S-1-5-18')))) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, [Security.AccessControl.FileSystemRights]::FullControl, [Security.AccessControl.AccessControlType]::Allow)
        $security.AddAccessRule($rule)
    }
    $stream = New-Object IO.FileStream($Path, [IO.FileMode]::CreateNew, [Security.AccessControl.FileSystemRights]::Write, [IO.FileShare]::None, 4096, [IO.FileOptions]::None, $security)
    try { $bytes = $script:SmaUtf8.GetBytes($Text); $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) }
    finally { $stream.Dispose() }
}
function New-SmaPrivateState([string]$Path) {
    if (Test-Path -LiteralPath $Path) { return }
    $security = New-Object Security.AccessControl.DirectorySecurity
    $security.SetAccessRuleProtection($true, $false)
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $security.SetOwner($sid)
    $security.SetOwner($sid)
    $inherit = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
    foreach ($identity in @($sid, (New-Object Security.Principal.SecurityIdentifier('S-1-5-18')))) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, [Security.AccessControl.FileSystemRights]::FullControl, $inherit, [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow)
        $security.AddAccessRule($rule)
    }
    [void][IO.Directory]::CreateDirectory($Path, $security)
}
function Assert-SmaPrivateAccess([string]$Path, [switch]$Directory) {
    Assert-SmaNoLink $Path
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer -ne [bool]$Directory) { throw 'File exchange state has an invalid type.' }
    $acl = Get-Acl -LiteralPath $Path
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if (!$acl.AreAccessRulesProtected -or $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -cne $sid) { throw 'File exchange state permissions are unsafe; existing data was not replaced.' }
    $own = $false; $system = $false
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        $identity = $rule.IdentityReference.Value
        if ($identity -ceq $sid) { $own = $true }
        elseif ($identity -ceq 'S-1-5-18') { $system = $true }
        else { throw 'File exchange state permissions are unsafe; existing data was not replaced.' }
    }
    if (!$own -or !$system) { throw 'File exchange state permissions are unsafe; existing data was not replaced.' }
}
function Get-SmaFileExchangePlan([string]$State, $Profile) {
    if ($null -eq $Profile -or $null -eq $Profile.PSObject.Properties['file_exchange']) { return $null }
    $parent = Join-Path $State 'file-exchange'
    $path = Join-Path $parent 'profile.json'
    Assert-SmaNoLink $parent;Assert-SmaNoLink $path
    if (Test-Path -LiteralPath $parent) { Assert-SmaPrivateAccess $parent -Directory }
    if (Test-Path -LiteralPath $path) {
        Assert-SmaPrivateAccess $path
        $existing = ConvertFrom-SmaStrictJson (Read-SmaBytes $path 32768) 'Existing file exchange profile'
        Assert-SmaFileExchangeProfile $existing
        return @{ Existing = $true; Path = $path; Parent = $parent; Profile = $existing }
    }
    return @{ Existing = $false; Path = $path; Parent = $parent; Profile = $Profile.file_exchange }
}
function Initialize-SmaFileExchange($Plan) {
    if ($null -eq $Plan -or $Plan.Existing) { return }
    Assert-SmaNoLink $Plan.Parent
    New-SmaPrivateState $Plan.Parent
    Assert-SmaPrivateAccess $Plan.Parent -Directory
    $temporary = Join-Path $Plan.Parent ('.profile-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        $json = $Plan.Profile | ConvertTo-Json -Compress -Depth 4
        Write-SmaPrivateNew $temporary $json
        Assert-SmaNoLink $Plan.Path
        try { [IO.File]::Move($temporary, $Plan.Path) }
        catch [IO.IOException] {
            if (!(Test-Path -LiteralPath $Plan.Path)) { throw }
            Assert-SmaPrivateAccess $Plan.Path
            Assert-SmaFileExchangeProfile (ConvertFrom-SmaStrictJson (Read-SmaBytes $Plan.Path 32768) 'Existing file exchange profile')
        }
    } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
}
function Read-SmaToken([string]$Path) {
    try { $value = $script:SmaUtf8.GetString((Read-SmaBytes $Path 32768)).Trim([char]0xFEFF).Trim() }
    catch { throw 'Existing Token file is unreadable or invalid; it was not replaced.' }
    if (!(Test-SmaToken $value)) { throw 'Existing Token file is invalid; it was not replaced.' }
    return $value
}
function Initialize-SmaToken([string]$State, $Profile) {
    $pointer = Join-Path $State 'token-source.json'
    $tokenPath = Join-Path $State 'token.txt'
    if (Test-Path -LiteralPath $pointer) {
        $existing = ConvertFrom-SmaJson (Read-SmaBytes $pointer 16384) 'Existing Token pointer'
        if ($existing.path -isnot [string] -or !$existing.path) { throw 'Existing Token pointer is invalid; it was not replaced.' }
        $selected = $existing.path
        if (![IO.Path]::IsPathRooted($selected)) { $selected = Join-Path $State $selected }
        [void](Read-SmaToken $selected)
        return [IO.Path]::GetFullPath($selected)
    }
    if (Test-Path -LiteralPath $tokenPath) { [void](Read-SmaToken $tokenPath) }
    else {
        $legacy = Join-Path $State 'token'
        if (Test-Path -LiteralPath $legacy) { $value = Read-SmaToken $legacy }
        elseif ($null -ne $Profile) { $value = $Profile.token }
        else {
            $secure = Read-Host 'Enter the relay Token (input is hidden)' -AsSecureString
            $address = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
            try { $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($address) }
            finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($address); $secure.Dispose() }
            if (!(Test-SmaToken $value)) { throw 'A valid Token is required; no existing configuration was replaced.' }
        }
        New-SmaPrivateState $State
        Write-SmaPrivateNew $tokenPath $value
        $value = $null
    }
    if (!(Test-Path -LiteralPath $pointer)) { Write-SmaPrivateNew $pointer (@{ path = $tokenPath } | ConvertTo-Json -Compress) }
    return $tokenPath
}
function Remove-SmaStage([string]$Path, [string]$Root) {
    $full = [IO.Path]::GetFullPath($Path)
    if (!$full.StartsWith($Root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -or (Split-Path $full -Leaf) -notmatch '^\.install-stage-[0-9a-f]{32}$') { throw 'Refusing cleanup outside the staging directory.' }
    if (Test-Path -LiteralPath $full) { Assert-SmaNoLink $full; Remove-Item -LiteralPath $full -Recurse -Force }
}
function New-SmaShortcut([string]$Root, [string]$Executable, [string]$Destination) {
    $shell = New-Object -ComObject WScript.Shell
    $path = Join-Path $Destination 'SuperMonoAgent.lnk'
    $link = $shell.CreateShortcut($path)
    if ((Test-Path -LiteralPath $path) -and $link.Description -ne 'SuperMonoAgent installed application') { throw 'An unrelated desktop shortcut already uses SuperMonoAgent.lnk.' }
    $link.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $link.Arguments = '-NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + (Join-Path $Root 'Launch.ps1') + '" -InstallRoot "' + $Root + '"'
    $link.WorkingDirectory = $Root
    $link.IconLocation = $Executable + ',0'
    $link.Description = 'SuperMonoAgent installed application'
    $link.WindowStyle = 7
    $link.Save()
}
function Invoke-SmaInstall {
    $root = Assert-SmaRoot $InstallRoot
    $state = Assert-SmaRoot $StateDir
    $package = [IO.Path]::GetFullPath($PSScriptRoot)
    $validated = Read-SmaCatalog $package
    $catalog = $validated.Catalog
    $currentPath = Join-Path $root 'current.json'
    $configuration = Join-Path $root 'config.json'
    $oldConfiguration = $null
    if (Test-Path -LiteralPath $configuration) {
        Assert-SmaNoLink $configuration
        $oldConfiguration = ConvertFrom-SmaJson (Read-SmaBytes $configuration 32768) 'Existing application configuration'
        Assert-SmaKeys $oldConfiguration @('schema', 'relay', 'state_dir', 'token_file') 'Existing application configuration'
        if ($oldConfiguration.schema -ne 1) { throw 'Existing application configuration was not replaced.' }
        Assert-SmaRelay $oldConfiguration.relay
        $existingState = Assert-SmaRoot $oldConfiguration.state_dir
        if ($script:SmaStateExplicit -and $state -ne $existingState) { throw 'Existing installation uses another StateDir; it was not replaced.' }
        $state = $existingState
        [void](Read-SmaToken $oldConfiguration.token_file)
    }
    $oldCurrent = $null
    if (Test-Path -LiteralPath $currentPath) {
        Assert-SmaNoLink $currentPath
        $oldCurrent = ConvertFrom-SmaJson (Read-SmaBytes $currentPath 16384) 'Existing current pointer'
        if ($oldCurrent.schema -ne 1 -or $oldCurrent.version -cnotmatch '^\d{1,6}\.\d{1,6}\.\d{1,6}$') { throw 'Existing current pointer is invalid; it was not replaced.' }
    }
    $fileExchangePlan = Get-SmaFileExchangePlan $state $validated.Profile
    [void][IO.Directory]::CreateDirectory($root)
    $stage = Join-Path $root ('.install-stage-' + [Guid]::NewGuid().ToString('N'))
    $versionPath = Join-Path (Join-Path $root 'versions') $catalog.version
    Assert-SmaNoLink $versionPath
    try {
        [void][IO.Directory]::CreateDirectory($stage)
        foreach ($property in $catalog.files.PSObject.Properties) {
            $target = Join-SmaChild $stage $property.Name
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))
            [IO.File]::Copy((Join-SmaChild (Join-Path $package 'payload') $property.Name), $target, $false)
        }
        Assert-SmaRuntime $stage $catalog.files
        if ($null -ne $oldConfiguration) { $tokenPath = $oldConfiguration.token_file }
        else { $tokenPath = Initialize-SmaToken $state $validated.Profile }
        Initialize-SmaFileExchange $fileExchangePlan
        if (Test-Path -LiteralPath $versionPath) { Assert-SmaRuntime $versionPath $catalog.files -Installed }
        else {
            [void][IO.Directory]::CreateDirectory((Join-Path $root 'versions'))
            [IO.Directory]::Move($stage, $versionPath)
        }
        $savedManifest = Join-Path $versionPath '.install-manifest.json'
        if (!(Test-Path -LiteralPath $savedManifest)) { [IO.File]::WriteAllBytes($savedManifest, $validated.EnvelopeBytes) }
        $supportScripts = @('Install.ps1', 'Launch.ps1', 'Uninstall.ps1')
        if ($null -ne $catalog.bootstrap_files.PSObject.Properties['Update.ps1']) { $supportScripts += 'Update.ps1' }
        foreach ($name in $supportScripts) {
            $bytes = Read-SmaBytes (Join-Path $package $name) 262144
            if ((Get-SmaHash $bytes) -cne $catalog.bootstrap_files.PSObject.Properties[$name].Value.sha256) { throw 'Bootstrap changed during installation.' }
            Write-SmaAtomic (Join-Path $root $name) $bytes
        }
        if (!(Test-Path -LiteralPath $configuration)) { Write-SmaJson $configuration @{ schema = 1; relay = $catalog.relay; state_dir = $state; token_file = $tokenPath } }
        if ($null -ne $oldCurrent -and $oldCurrent.version -ne $catalog.version) { Write-SmaJson (Join-Path $root 'previous.json') $oldCurrent }
        $pointer = @{ schema = 1; version = $catalog.version; entry = $catalog.entry; catalog_sha256 = (Get-SmaHash (Read-SmaBytes $savedManifest 1048576)); installed_at = [DateTime]::UtcNow.ToString('o') }
        Write-SmaJson $currentPath $pointer
        $disabled = Join-Path $root '.updates-disabled'
        if (Test-Path -LiteralPath $disabled -PathType Leaf) { Assert-SmaNoLink $disabled;Remove-Item -LiteralPath $disabled -Force }
        $executable = Join-SmaChild $versionPath $catalog.entry
        if (!$NoShortcuts) { New-SmaShortcut $root $executable ([Environment]::GetFolderPath('Desktop')) }
        if (!$NoRegistry) {
            $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\SuperMonoAgent'
            if (Test-Path $key) {
                $prior = Get-ItemProperty $key
                if ($prior.DisplayName -ne 'SuperMonoAgent') { throw 'An unrelated uninstall registration was not replaced.' }
            }
            [void](New-Item -Path $key -Force)
            $properties = @{ DisplayName = 'SuperMonoAgent'; DisplayVersion = $catalog.version; Publisher = 'SuperMonoAgent'; InstallLocation = $root; DisplayIcon = $executable; UninstallString = ('"' + (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') + '" -NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $root 'Uninstall.ps1') + '" -InstallRoot "' + $root + '"') }
            foreach ($name in $properties.Keys) { [void](New-ItemProperty -Path $key -Name $name -Value $properties[$name] -PropertyType String -Force) }
            foreach ($name in @('NoModify', 'NoRepair')) { [void](New-ItemProperty -Path $key -Name $name -Value 1 -PropertyType DWord -Force) }
        }
        if (!$NoLaunch) { & (Join-Path $root 'Launch.ps1') -InstallRoot $root | Out-Null }
        return @{ installed = $true; version = $catalog.version; install_root = $root; state_dir = $state; launch_requested = (!$NoLaunch) }
    } finally { Remove-SmaStage $stage $root }
}
if ($MyInvocation.InvocationName -ne '.') {
    try { Invoke-SmaInstall | ConvertTo-Json -Compress -Depth 5 }
    catch { Write-Error ('SuperMonoAgent installation failed: ' + $_.Exception.Message); exit 1 }
}
