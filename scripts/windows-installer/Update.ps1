param(
    [string]$InstallRoot = $PSScriptRoot,
    [switch]$CheckOnce,
    [switch]$NoPrompt,
    [switch]$WaitForPrevious,
    [string]$Feed = 'https://117.50.48.22/updates/windows/stable/manifest.json',
    [int]$OwnerPid = 0,
    [long]$OwnerStartTicks = 0
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
. (Join-Path $PSScriptRoot 'Install.ps1') -InstallRoot $InstallRoot -NoLaunch -NoShortcuts -NoRegistry
. (Join-Path $PSScriptRoot 'Launch.ps1') -InstallRoot $InstallRoot

function Get-SmaUpdateNow { return [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }
function Test-SmaUpdateInteger($Value, [long]$Minimum, [long]$Maximum) { return ($Value -is [int] -or $Value -is [long]) -and $Value -ge $Minimum -and $Value -le $Maximum }
function Get-SmaUpdateUri([string]$Value) {
    $uri = $null
    if (![Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$uri) -or !$uri.IsWellFormedOriginalString() -or $uri.Scheme -ne 'https' -or !$uri.Host -or $uri.UserInfo -or $uri.Query -or $uri.Fragment) { throw 'Updates require an HTTPS URL without credentials.' }
    return $uri
}
function Get-SmaUpdateBytes([Uri]$Uri, [long]$Limit) {
    if ($Uri.Scheme -ne 'https' -or $Limit -lt 1 -or $Limit -gt 268435456) { throw 'Invalid update download bounds.' }
    $previousTls = [Net.ServicePointManager]::SecurityProtocol
    $response = $null;$stream = $null;$memory = New-Object IO.MemoryStream
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $request = [Net.HttpWebRequest]::Create($Uri)
        $request.AllowAutoRedirect = $false
        $request.UseDefaultCredentials = $false
        $request.Credentials = $null
        $request.PreAuthenticate = $false
        $request.Proxy = $null
        $request.CookieContainer = New-Object Net.CookieContainer
        $request.Timeout = 10000;$request.ReadWriteTimeout = 10000
        $request.KeepAlive = $false
        $request.UserAgent = 'SuperMonoAgent-ProgramUpdater/1'
        $response = $request.GetResponse()
        if ([int]$response.StatusCode -ne 200 -or $response.ContentLength -gt $Limit) { throw 'Unexpected update download response.' }
        $stream = $response.GetResponseStream()
        $buffer = New-Object byte[] 65536
        $deadline = [DateTime]::UtcNow.AddMinutes(2)
        while (($count = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            if ($memory.Length + $count -gt $Limit -or [DateTime]::UtcNow -gt $deadline) { throw 'Update download exceeds its limits.' }
            $memory.Write($buffer, 0, $count)
        }
        return ,$memory.ToArray()
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
        if ($null -ne $response) { $response.Close() }
        $memory.Dispose()
        [Net.ServicePointManager]::SecurityProtocol = $previousTls
    }
}
function Read-SmaFullFeed([byte[]]$Raw, [Uri]$FeedUri) {
    if ($Raw.Length -gt 1048576) { throw 'Update manifest exceeds its size limit.' }
    $envelope = ConvertFrom-SmaJson $Raw 'Update envelope'
    Assert-SmaKeys $envelope @('schema', 'key_id', 'payload', 'signature') 'Update envelope'
    if (!(Test-SmaUpdateInteger $envelope.schema 1 1) -or $envelope.key_id -cne $script:SmaKeyId) { throw 'Untrusted update signature key.' }
    try { $bytes = [Convert]::FromBase64String($envelope.payload);$signature = [Convert]::FromBase64String($envelope.signature) }
    catch { throw 'Invalid update signature encoding.' }
    $key = New-Object Security.Cryptography.RSAParameters
    $key.Modulus = [Convert]::FromBase64String($script:SmaModulus);$key.Exponent = [Convert]::FromBase64String($script:SmaExponent)
    $rsa = [Security.Cryptography.RSA]::Create()
    try { $rsa.ImportParameters($key);if (!$rsa.VerifyData($bytes,$signature,[Security.Cryptography.HashAlgorithmName]::SHA256,[Security.Cryptography.RSASignaturePadding]::Pkcs1)) { throw 'Update signature validation failed.' } }
    finally { $rsa.Dispose() }
    $value = ConvertFrom-SmaJson $bytes 'Signed update'
    Assert-SmaKeys $value @('schema','format','channel','arch','version','sequence','created','expires','url','size','sha256') 'Signed update'
    if (!(Test-SmaUpdateInteger $value.schema 1 1) -or $value.format -cne 'supermono-windows-full-update-v1' -or $value.channel -cne 'stable' -or $value.arch -cne 'x64' -or $value.version -isnot [string] -or $value.version -cnotmatch '^\d{1,6}\.\d{1,6}\.\d{1,6}$') { throw 'Unsupported program update manifest.' }
    $now = Get-SmaUpdateNow
    if (!(Test-SmaUpdateInteger $value.sequence 1 9007199254740991) -or !(Test-SmaUpdateInteger $value.created 0 9007199254740991) -or !(Test-SmaUpdateInteger $value.expires 1 9007199254740991) -or $value.created -gt $now + 300 -or $value.expires -le $now -or $value.expires -le $value.created) { throw 'Update validity or sequence is invalid.' }
    if (!(Test-SmaUpdateInteger $value.size 1 268435456) -or $value.sha256 -isnot [string] -or $value.sha256 -cnotmatch '^[0-9a-f]{64}$' -or $value.url -isnot [string]) { throw 'Invalid update archive metadata.' }
    try { $archive = New-Object Uri($FeedUri, $value.url) } catch { throw 'Invalid update archive URL.' }
    $archive = Get-SmaUpdateUri $archive.AbsoluteUri
    $expected = '/updates/installers/' + $value.version + '/SuperMonoAgent-' + $value.version + '-Setup.zip'
    if ($archive.Host -ine $FeedUri.Host -or $archive.Port -ne $FeedUri.Port -or $archive.AbsolutePath -cne $expected) { throw 'Update archive must use the trusted origin and installer directory.' }
    return @{ Manifest = $value; Uri = $archive; PayloadHash = (Get-SmaHash $bytes) }
}
function Read-SmaInstalledVersion([string]$Root) {
    $pointer = Read-SmaLaunchJson (Join-Path $Root 'current.json')
    if ($pointer.schema -ne 1 -or $pointer.version -isnot [string] -or $pointer.version -cnotmatch '^\d{1,6}\.\d{1,6}\.\d{1,6}$') { throw 'Invalid installed version pointer.' }
    return [version]$pointer.version
}
function Read-SmaUpdateState([string]$Root) {
    $path = Join-Path $Root 'program-updates\state.json'
    if (!(Test-Path -LiteralPath $path)) { return @{ schema=1;highest_sequence=[long]0;observed_hash=$null;declined_version=$null;declined_until=[long]0;status='never';ready_version=$null } }
    $state = Read-SmaLaunchJson $path
    if ($state.schema -ne 1 -or !(Test-SmaUpdateInteger $state.highest_sequence 0 9007199254740991) -or !(Test-SmaUpdateInteger $state.declined_until 0 9007199254740991) -or ($null -ne $state.observed_hash -and $state.observed_hash -cnotmatch '^[0-9a-f]{64}$')) { throw 'Invalid program update state.' }
    return $state
}
function Save-SmaUpdateState([string]$Root, $State) {
    $directory = Join-Path $Root 'program-updates'
    Assert-SmaNoLink $directory
    New-SmaPrivateState $directory
    Write-SmaJson (Join-Path $directory 'state.json') $State
}
function Expand-SmaProgramArchive([string]$Archive, [string]$Destination) {
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $stream = [IO.File]::OpenRead($Archive)
    $zip = New-Object IO.Compression.ZipArchive($stream, [IO.Compression.ZipArchiveMode]::Read)
    try {
        if ($zip.Entries.Count -gt 4096) { throw 'Update ZIP has too many entries.' }
        $seen = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
        $total = [long]0
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName.TrimEnd('/')
            Assert-SmaRelative $name
            if (!$seen.Add($name)) { throw 'Update ZIP has colliding entries.' }
            $top = $name.Split('/')[0]
            if ($top -cnotin @('Install.cmd','Install.ps1','Launch.ps1','Uninstall.ps1','Update.ps1','install-manifest.json','payload')) { throw 'Program updates must contain only public installer files.' }
            $mode = ($entry.ExternalAttributes -shr 16) -band 0xF000
            if ($mode -notin @(0,0x8000,0x4000) -or ($entry.ExternalAttributes -band 1024) -ne 0) { throw 'Update ZIP contains a link or unsupported file type.' }
            if ($entry.Length -lt 0 -or $entry.Length -gt 268435456) { throw 'Update ZIP entry exceeds its size limit.' }
            $total += $entry.Length
            if ($total -gt 1073741824) { throw 'Update ZIP exceeds its unpacked size limit.' }
        }
        [void][IO.Directory]::CreateDirectory($Destination)
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName.TrimEnd('/')
            $target = Join-SmaChild $Destination $name
            if ($entry.FullName.EndsWith('/')) { [void][IO.Directory]::CreateDirectory($target);continue }
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))
            $entryInput = $entry.Open();$output = [IO.File]::Open($target,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
            try {
                $buffer = New-Object byte[] 65536;$written = [long]0
                while (($count=$entryInput.Read($buffer,0,$buffer.Length)) -gt 0) { $written += $count;if ($written -gt $entry.Length) { throw 'Update ZIP length mismatch.' };$output.Write($buffer,0,$count) }
                if ($written -ne $entry.Length) { throw 'Update ZIP length mismatch.' }
            } finally { $output.Dispose();$entryInput.Dispose() }
        }
    } finally { $zip.Dispose();$stream.Dispose() }
}
function Remove-SmaUpdatePackage([string]$CacheDirectory, [string]$Package) {
    $base = [IO.Path]::GetFullPath($CacheDirectory).TrimEnd('\')
    $target = [IO.Path]::GetFullPath($Package)
    $name = [IO.Path]::GetFileName($target)
    if ([IO.Path]::GetDirectoryName($target) -ine $base -or ($name -cnotin @('p','package') -and $name -cnotmatch '^(?:s-[0-9a-f]{8}|package-stage-[0-9a-f]{32})$')) { throw 'Update cleanup is outside its owned cache.' }
    Assert-SmaNoLink $base;Assert-SmaNoLink $target
    if (!(Test-Path -LiteralPath $target)) { return }
    $queue = New-Object 'Collections.Generic.Queue[string]'
    $queue.Enqueue($target)
    while ($queue.Count -gt 0) {
        $directory = $queue.Dequeue()
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Update cleanup refuses links.' }
            if ($item.PSIsContainer) { $queue.Enqueue($item.FullName) }
        }
    }
    Remove-Item -LiteralPath $target -Recurse -Force
}
function Assert-SmaPreparedPackage([string]$Package, [string]$Version) {
    $validated = Read-SmaCatalog $Package
    if ($validated.Catalog.version -cne $Version -or $null -ne $validated.Profile -or $null -ne $validated.Catalog.defaults_private_sha256) { throw 'Program updates must use a matching public installer.' }
}
function New-SmaUpdateStage([string]$Directory) {
    for ($attempt=0;$attempt -lt 16;$attempt++) {
        $stage = Join-Path $Directory ('s-' + [Guid]::NewGuid().ToString('N').Substring(0,8))
        if (Test-Path -LiteralPath $stage) { continue }
        try { [void](New-Item -Path $stage -ItemType Directory -ErrorAction Stop);return $stage }
        catch { if (!(Test-Path -LiteralPath $stage)) { throw } }
    }
    throw 'Unable to reserve a unique update staging directory.'
}
function Get-SmaPreparedUpdate([string]$Root, $Release) {
    $manifest = $Release.Manifest
    $cache = Join-Path $Root 'program-updates\cache'
    Assert-SmaNoLink $cache;New-SmaPrivateState $cache
    $directory = Join-Path $cache $manifest.sha256.Substring(0,16)
    Assert-SmaNoLink $directory
    New-SmaPrivateState $directory
    $archivePath = Join-Path $directory 'setup.zip'
    if (!(Test-Path -LiteralPath $archivePath) -or (Get-Item -LiteralPath $archivePath).Length -ne $manifest.size -or (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant() -cne $manifest.sha256) {
        $bytes = Get-SmaUpdateBytes $Release.Uri $manifest.size
        if ($bytes.Length -ne $manifest.size -or (Get-SmaHash $bytes) -cne $manifest.sha256) { throw 'Program archive download failed integrity validation.' }
        Write-SmaAtomic $archivePath $bytes
    }
    $package = Join-Path $directory 'p'
    if (Test-Path -LiteralPath $package) {
        try { Assert-SmaPreparedPackage $package $manifest.version;return $package }
        catch { Remove-SmaUpdatePackage $directory $package }
    }
    $stage = New-SmaUpdateStage $directory
    try {
        Expand-SmaProgramArchive $archivePath $stage
        Assert-SmaPreparedPackage $stage $manifest.version
        [IO.Directory]::Move($stage, $package)
    } finally { Remove-SmaUpdatePackage $directory $stage }
    return $package
}
function Show-SmaUpdatePrompt([string]$Version) {
    Add-Type -AssemblyName System.Windows.Forms
    $message = ('"\u65b0\u7248\u672c {0} \u5df2\u4e0b\u8f7d\u5e76\u9a8c\u8bc1\u3002\u662f\u5426\u73b0\u5728\u5b89\u88c5\uff1f\n\n\u5f53\u524d\u7a97\u53e3\u548c\u7ec8\u7aef\u7ee7\u7eed\u8fd0\u884c\uff0c\u4e0b\u6b21\u6253\u5f00\u5ba2\u6237\u7aef\u65f6\u751f\u6548\uff1b\u540e\u53f0\u4f1a\u8bdd\u6b63\u5e38\u7ed3\u675f\u540e\u518d\u5207\u6362\u65b0\u540e\u53f0\u3002"' | ConvertFrom-Json) -f $Version
    return [Windows.Forms.MessageBox]::Show($message,'SuperMonoAgent',[Windows.Forms.MessageBoxButtons]::YesNo,[Windows.Forms.MessageBoxIcon]::Information,[Windows.Forms.MessageBoxDefaultButton]::Button2) -eq [Windows.Forms.DialogResult]::Yes
}
function Show-SmaUpdateInstalled([string]$Version) {
    Add-Type -AssemblyName System.Windows.Forms
    $message = ('"\u7248\u672c {0} \u5df2\u5b89\u88c5\u3002\u4e0b\u6b21\u6253\u5f00\u5ba2\u6237\u7aef\u65f6\u751f\u6548\uff0c\u5f53\u524d\u7a97\u53e3\u548c\u540e\u53f0\u7ec8\u7aef\u6ca1\u6709\u88ab\u5173\u95ed\u3002"' | ConvertFrom-Json) -f $Version
    [void][Windows.Forms.MessageBox]::Show($message,'SuperMonoAgent',[Windows.Forms.MessageBoxButtons]::OK,[Windows.Forms.MessageBoxIcon]::Information)
}
function Install-SmaProgramUpdate([string]$Package, [string]$Root, [string]$State) {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arguments = @('-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',(Join-Path $Package 'Install.ps1'),'-InstallRoot',$Root,'-StateDir',$State,'-NoLaunch')
    $info.Arguments = ($arguments | ForEach-Object { ConvertTo-SmaArgument $_ }) -join ' '
    $info.UseShellExecute=$false;$info.CreateNoWindow=$true;$info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true
    $process = [Diagnostics.Process]::Start($info)
    try {
        $stdout=$process.StandardOutput.ReadToEndAsync();$stderr=$process.StandardError.ReadToEndAsync()
        if (!$process.WaitForExit(600000)) { throw 'The installer is still running; no application process was stopped.' }
        [void]$stdout.GetAwaiter().GetResult();[void]$stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) { throw 'The verified update could not be installed; current terminals were not stopped.' }
    } finally { $process.Dispose() }
}
function Invoke-SmaProgramUpdate([string]$Root, [string]$FeedUrl, [switch]$SuppressPrompt, [switch]$RequireClient) {
    $rootPath = Assert-SmaRoot $Root
    if (Test-Path -LiteralPath (Join-Path $rootPath '.updates-disabled')) { return @{ status='disabled' } }
    $current = Read-SmaInstalledVersion $rootPath
    $configuration = Read-SmaLaunchJson (Join-Path $rootPath 'config.json')
    $feedUri = Get-SmaUpdateUri $FeedUrl
    $release = Read-SmaFullFeed (Get-SmaUpdateBytes $feedUri 1048576) $feedUri
    $manifest = $release.Manifest
    $state = Read-SmaUpdateState $rootPath
    if ($manifest.sequence -lt $state.highest_sequence -or ($manifest.sequence -eq $state.highest_sequence -and $null -ne $state.observed_hash -and $state.observed_hash -cne $release.PayloadHash)) { throw 'Program update sequence rollback or conflict rejected.' }
    $state.highest_sequence=[long]$manifest.sequence;$state.observed_hash=$release.PayloadHash
    # Persist authenticated observations even when downloading or unpacking fails.
    $state.status='checking';Save-SmaUpdateState $rootPath $state
    if ([version]$manifest.version -le $current) { $state.status='current';Save-SmaUpdateState $rootPath $state;return @{status='current';version=$manifest.version} }
    $package = Get-SmaPreparedUpdate $rootPath $release
    $state.status='ready';$state.ready_version=$manifest.version;Save-SmaUpdateState $rootPath $state
    if ($SuppressPrompt -or ($RequireClient -and !(Test-SmaMainWindow)) -or ($state.declined_version -ceq $manifest.version -and $state.declined_until -gt (Get-SmaUpdateNow))) { return @{status='downloaded';version=$manifest.version;package=$package} }
    if ([version]$manifest.version -le (Read-SmaInstalledVersion $rootPath)) { return @{status='current';version=$manifest.version} }
    if (!(Show-SmaUpdatePrompt $manifest.version)) {
        $state.declined_version=$manifest.version;$state.declined_until=(Get-SmaUpdateNow)+86400;$state.status='declined';Save-SmaUpdateState $rootPath $state
        return @{status='declined';version=$manifest.version}
    }
    if (Test-Path -LiteralPath (Join-Path $rootPath '.updates-disabled')) { return @{status='disabled'} }
    if ([version]$manifest.version -le (Read-SmaInstalledVersion $rootPath)) { return @{status='current';version=$manifest.version} }
    Install-SmaProgramUpdate $package $rootPath ([IO.Path]::GetFullPath($configuration.state_dir))
    if ((Read-SmaInstalledVersion $rootPath) -ne [version]$manifest.version) { throw 'The installer did not switch the version pointer.' }
    $state.status='installed';$state.declined_version=$null;$state.declined_until=[long]0;Save-SmaUpdateState $rootPath $state
    Show-SmaUpdateInstalled $manifest.version
    return @{status='installed';version=$manifest.version}
}
function Get-SmaUpdateClient([int]$ExpectedPid, [long]$ExpectedTicks) {
    if ($ExpectedPid -gt 0 -and $ExpectedTicks -gt 0) {
        try {
            $process=Get-Process -Id $ExpectedPid -ErrorAction Stop
            if ($process.ProcessName -eq 'SuperMonoAgent' -and $process.StartTime.ToUniversalTime().Ticks -eq $ExpectedTicks -and $process.MainWindowHandle -ne 0) { return $process }
        } catch { }
        return $null
    }
    return Get-SmaMainProcess
}
function Wait-SmaUpdateInterval([int]$ClientPid, [long]$ClientTicks, [string]$Root) {
    $deadline=[DateTime]::UtcNow.AddMinutes(15)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ((Test-Path -LiteralPath (Join-Path $Root '.updates-disabled')) -or $null -eq (Get-SmaUpdateClient $ClientPid $ClientTicks)) { return $false }
        Start-Sleep -Seconds 2
    }
    return $true
}
function New-SmaUpdateMutex([string]$Root) { return New-Object Threading.Mutex($false,(Get-SmaUpdateMutexName $Root)) }
function Invoke-SmaUpdateWorker([string]$Root, [string]$FeedUrl, [switch]$Once, [switch]$SuppressPrompt, [int]$ClientPid=0, [long]$ClientTicks=0, [switch]$WaitPrevious) {
    $rootPath=Assert-SmaRoot $Root
    if (!(Test-Path -LiteralPath (Join-Path $rootPath 'current.json'))) { throw 'Program updater requires an installed application.' }
    [void](Get-SmaUpdateUri $FeedUrl)
    $mutex=New-SmaUpdateMutex $rootPath;$owned=$false
    try {
        try {$owned=$mutex.WaitOne(0)}catch [Threading.AbandonedMutexException]{$owned=$true}
        if (!$owned -and $WaitPrevious) {
            $deadline=[DateTime]::UtcNow.AddSeconds(60)
            while (!$owned -and (!$Once -or [DateTime]::UtcNow -lt $deadline)) {
                if ((Test-Path -LiteralPath (Join-Path $rootPath '.updates-disabled')) -or (!$Once -and $null -eq (Get-SmaUpdateClient $ClientPid $ClientTicks))) { return @{status='client-closed'} }
                Start-Sleep -Milliseconds 200
                try {$owned=$mutex.WaitOne(0)}catch [Threading.AbandonedMutexException]{$owned=$true}
            }
        }
        if (!$owned) { return @{status='already-checking'} }
        if (!$Once) {
            $deadline=[DateTime]::UtcNow.AddSeconds(30)
            while ($null -eq (Get-SmaUpdateClient $ClientPid $ClientTicks)) { if([DateTime]::UtcNow -gt $deadline){return @{status='no-client'}};Start-Sleep -Milliseconds 200 }
        }
        do {
            try { $result=Invoke-SmaProgramUpdate $rootPath $FeedUrl -SuppressPrompt:$SuppressPrompt -RequireClient:(!$Once) }
            catch { $result=@{status='failed';error_type=$_.Exception.GetType().Name} }
            if ($Once) { return $result }
        } while (Wait-SmaUpdateInterval $ClientPid $ClientTicks $rootPath)
        return @{status='client-closed'}
    } finally { if($owned){$mutex.ReleaseMutex()};$mutex.Dispose() }
}
if ($MyInvocation.InvocationName -ne '.') {
    try { Invoke-SmaUpdateWorker $InstallRoot $Feed -Once:$CheckOnce -SuppressPrompt:$NoPrompt -ClientPid $OwnerPid -ClientTicks $OwnerStartTicks -WaitPrevious:$WaitForPrevious | ConvertTo-Json -Compress -Depth 5 }
    catch { Write-Output ('{"status":"failed","error_type":"' + $_.Exception.GetType().Name + '"}');exit 1 }
}
