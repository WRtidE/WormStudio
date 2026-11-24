param([Parameter(Mandatory=$true)][string]$Plan, [switch]$Prepare, [switch]$Launch)
$ErrorActionPreference = 'Stop'
$config = Get-Content -LiteralPath $Plan -Raw -Encoding UTF8 | ConvertFrom-Json
$target = [IO.Path]::GetFullPath($config.target)
$stage = [IO.Path]::GetFullPath($config.stage)
$backup = [IO.Path]::GetFullPath($config.backup)
$parent = [IO.Path]::GetDirectoryName($target)
$cache = Join-Path $parent ('.'+[IO.Path]::GetFileName($target)+'.updates')
$stageName = [IO.Path]::GetFileName($stage)
if (!$parent -or [IO.Path]::GetDirectoryName($stage) -ne $cache -or
    $stageName -notmatch '^stage-[a-f0-9-]+$' -or
    $backup -ne (Join-Path $cache ($stageName -replace '^stage-', 'previous-'))) {
    throw 'Invalid update directories'
}
if ($Prepare) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $cacheDir = [IO.Directory]::CreateDirectory($cache)
    if ($cacheDir.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Invalid update cache directory' }
    $cacheDir.Attributes = $cacheDir.Attributes -bor [IO.FileAttributes]::Hidden
    if (Test-Path -LiteralPath $stage) { throw 'Update stage already exists' }
    [IO.Directory]::CreateDirectory($stage) | Out-Null
    try {
        $zip = [IO.Compression.ZipFile]::OpenRead($config.archive)
        try {
            foreach ($entry in $zip.Entries) {
                $name = $entry.FullName.Replace('\','/')
                if (!$name.StartsWith('WormStudio/') -or $name.Contains(':') -or
                    ($name.Split('/') -contains '..') -or (($entry.ExternalAttributes -shr 16) -band 0xF000) -eq 0xA000) {
                    throw 'Unsafe update archive entry'
                }
                $relative = $name.Substring(11)
                if (!$relative) { continue }
                $destination = [IO.Path]::GetFullPath((Join-Path $stage $relative))
                if (!$destination.StartsWith($stage+[IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
                    throw 'Archive entry escapes update directory'
                }
                if ($name.EndsWith('/')) { [IO.Directory]::CreateDirectory($destination) | Out-Null }
                else {
                    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination)) | Out-Null
                    [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $destination, $false)
                }
            }
        } finally { $zip.Dispose() }
        $package = Get-Content -LiteralPath (Join-Path $stage 'resources/app/package.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($package.name -ne 'WormStudio' -or $package.version -ne $config.version -or
            !(Test-Path -LiteralPath (Join-Path $stage 'WormStudio.exe')) -or
            !(Test-Path -LiteralPath (Join-Path $stage 'resources/app/desktop.cjs'))) { throw 'Wrong application or version in update' }
    } catch {
        Remove-Item -LiteralPath $stage -Recurse -Force
        throw
    }
    exit 0
}
if ($Launch) {
    $arguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
                   ('"{0}"' -f $PSCommandPath), '-Plan', ('"{0}"' -f $Plan))
    Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $arguments -WindowStyle Hidden -WorkingDirectory $PSHOME
    exit 0
}
try {
    'Waiting for WormStudio to exit.' | Set-Content -LiteralPath $config.log -Encoding UTF8
    $process = Get-Process -Id $config.parentPid -ErrorAction SilentlyContinue
    if ($process -and !$process.WaitForExit(120000)) { throw 'Application did not exit; update cancelled' }
    # Keep the complete old application; never merge old and new program files.
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ($true) {
        try { [IO.Directory]::Move($target, $backup); break }
        catch { if ([DateTime]::UtcNow -ge $deadline) { throw }; Start-Sleep -Milliseconds 500 }
    }
    try {
        [IO.Directory]::Move($stage, $target)
        Start-Process -FilePath (Join-Path $target 'WormStudio.exe') -WorkingDirectory $target -WindowStyle Hidden -ErrorAction Stop
    } catch {
        if (Test-Path -LiteralPath $target) { [IO.Directory]::Move($target, $stage) }
        [IO.Directory]::Move($backup, $target)
        Start-Process -FilePath (Join-Path $target 'WormStudio.exe') -WorkingDirectory $target -WindowStyle Hidden
        throw
    }
    'Updated successfully. Previous version: '+$backup | Set-Content -LiteralPath $config.log -Encoding UTF8
} catch {
    $_.Exception.ToString() | Set-Content -LiteralPath $config.log -Encoding UTF8
    # Once the original is back in place, this job's extracted files can go.
    # Keep all recovery files if the directory swap itself could not be undone.
    if ([IO.Directory]::Exists($target) -and ![IO.Directory]::Exists($backup) -and [IO.Directory]::Exists($stage)) {
        [IO.Directory]::Delete($stage, $true)
    }
    exit 1
}

# Once the new executable has started, keep the rollback copy for this update.
# Only retire the specific backup recorded by the previous successful update.
$record = Join-Path $cache 'last-backup.txt'
try {
    $oldName = if ([IO.File]::Exists($record)) { [IO.File]::ReadAllText($record) }
    $newName = [IO.Path]::GetFileName($backup)
    [IO.File]::WriteAllText($record, $newName)
    if ($oldName -match '^previous-[a-f0-9-]+$' -and $oldName -ne $newName) {
        $oldBackup = Join-Path $cache $oldName
        if ([IO.Directory]::Exists($oldBackup)) { [IO.Directory]::Delete($oldBackup, $true) }
    }
} catch {
    ('Backup retention: '+$_.Exception.Message) | Add-Content -LiteralPath $config.log -Encoding UTF8
}
