param([Parameter(Mandatory=$true)][string]$Plan, [switch]$Prepare)
$ErrorActionPreference = 'Stop'
$config = Get-Content -LiteralPath $Plan -Raw -Encoding UTF8 | ConvertFrom-Json
$target = [IO.Path]::GetFullPath($config.target)
$stage = [IO.Path]::GetFullPath($config.stage)
$backup = [IO.Path]::GetFullPath($config.backup)
$parent = [IO.Path]::GetDirectoryName($target)
if (!$parent -or [IO.Path]::GetDirectoryName($stage) -ne $parent -or
    [IO.Path]::GetDirectoryName($backup) -ne $parent -or
    [IO.Path]::GetFileName($stage) -notmatch '^\.WormStudio-update-[a-f0-9-]+$' -or
    !$backup.StartsWith($target+'.previous-', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Invalid update directories'
}
if ($Prepare) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
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
try {
    $process = Get-Process -Id $config.parentPid -ErrorAction SilentlyContinue
    if ($process -and !$process.WaitForExit(120000)) { throw 'Application did not exit; update cancelled' }
    # Keep the complete old application; never merge old and new program files.
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ($true) {
        try { Move-Item -LiteralPath $target -Destination $backup; break }
        catch { if ([DateTime]::UtcNow -ge $deadline) { throw }; Start-Sleep -Milliseconds 500 }
    }
    try {
        Move-Item -LiteralPath $stage -Destination $target
        Start-Process -FilePath (Join-Path $target 'WormStudio.exe') -WorkingDirectory $target -WindowStyle Hidden -ErrorAction Stop
    } catch {
        if (Test-Path -LiteralPath $target) { Move-Item -LiteralPath $target -Destination $stage }
        Move-Item -LiteralPath $backup -Destination $target
        Start-Process -FilePath (Join-Path $target 'WormStudio.exe') -WorkingDirectory $target -WindowStyle Hidden
        throw
    }
    'Updated successfully. Previous version: '+$backup | Set-Content -LiteralPath $config.log -Encoding UTF8
} catch {
    $_.Exception.ToString() | Set-Content -LiteralPath $config.log -Encoding UTF8
    exit 1
}
