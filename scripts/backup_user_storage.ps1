[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [ValidatePattern('^[A-Za-z]:\\?$')]
    [string]$SourceDrive = 'G:',

    [Parameter(Mandatory = $false)]
    [string]$DestinationRoot = (Join-Path (Get-Location) 'artifacts\\backups')
)

$ErrorActionPreference = 'Stop'

$source = if ($SourceDrive.EndsWith('\')) { $SourceDrive } else { "$SourceDrive\" }
if (-not (Test-Path -LiteralPath $source -PathType Container)) {
    throw "源盘不存在或不可访问: $source"
}

$volume = Get-Volume -DriveLetter $SourceDrive.TrimEnd(':', '\')
if (-not $volume) {
    throw "无法读取卷信息: $SourceDrive"
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$destinationRootFull = [IO.Path]::GetFullPath($DestinationRoot)
New-Item -ItemType Directory -Force -Path $destinationRootFull | Out-Null
$destination = Join-Path $destinationRootFull $stamp
New-Item -ItemType Directory -Force -Path $destination | Out-Null

$logPath = Join-Path $destination 'robocopy.log'
$copyArgs = @(
    $source,
    $destination,
    '/E',
    '/COPY:DAT',
    '/DCOPY:DAT',
    '/R:1',
    '/W:1',
    '/XJ',
    '/FFT',
    '/NP',
    "/LOG:$logPath"
)

& robocopy @copyArgs | Out-Null
$robocopyCode = $LASTEXITCODE
if ($robocopyCode -ge 8) {
    throw "Robocopy 失败，退出码 $robocopyCode。详见 $logPath"
}

$files = @(Get-ChildItem -LiteralPath $destination -Recurse -Force -File -ErrorAction Stop |
    Where-Object { $_.Name -notin @('robocopy.log', 'sha256-manifest.json', 'backup-summary.json') })
$manifest = foreach ($file in $files) {
    $relative = [IO.Path]::GetRelativePath($destination, $file.FullName)
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    [PSCustomObject]@{
        Path = $relative
        Length = $file.Length
        LastWriteTimeUtc = $file.LastWriteTimeUtc.ToString('o')
        SHA256 = $hash
    }
}

$manifestPath = Join-Path $destination 'sha256-manifest.json'
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

$summary = [PSCustomObject]@{
    CreatedAt = (Get-Date).ToString('o')
    SourceDrive = $source
    VolumeLabel = $volume.FileSystemLabel
    FileSystem = $volume.FileSystem
    Size = $volume.Size
    SizeRemaining = $volume.SizeRemaining
    Destination = $destination
    FileCount = $files.Count
    TotalBytes = ($files | Measure-Object -Property Length -Sum).Sum
    RobocopyExitCode = $robocopyCode
    Manifest = $manifestPath
    Scope = 'User storage copy only; not a boot/recovery/system/vendor image'
}
$summaryPath = Join-Path $destination 'backup-summary.json'
$summary | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $summaryPath -Encoding UTF8
$summary | Format-List
