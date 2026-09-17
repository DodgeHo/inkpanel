[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$AdbPath = '',

    [Parameter(Mandatory = $false)]
    [string]$OutputDir = (Join-Path (Get-Location) 'artifacts\device-info')
)

$ErrorActionPreference = 'Stop'

# adb 从哪来（按优先级）：-AdbPath → PATH 上的 adb → ANDROID_HOME/ANDROID_SDK_ROOT
# → 常见安装位置。以前这里写死了一个本机绝对路径，别人 clone 下来必然跑不动。
if (-not $AdbPath -or -not (Test-Path -LiteralPath $AdbPath -PathType Leaf)) {
    $resolved = Get-Command adb -ErrorAction SilentlyContinue
    if ($resolved) {
        $AdbPath = $resolved.Source
    } else {
        $roots = @($env:ANDROID_HOME, $env:ANDROID_SDK_ROOT, $env:H9DASH_SDK) | Where-Object { $_ }
        $cands = @()
        foreach ($r in $roots) { $cands += (Join-Path $r 'platform-tools\adb.exe') }
        $cands += @(
            "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe",
            'C:\Android\Sdk\platform-tools\adb.exe'
        )
        foreach ($c in $cands) {
            if (Test-Path -LiteralPath $c -PathType Leaf) { $AdbPath = $c; break }
        }
    }
}
if (-not $AdbPath -or -not (Test-Path -LiteralPath $AdbPath -PathType Leaf)) {
    throw ("找不到 adb。请任选一种方式指定：" + "`n" +
           "  · 把 platform-tools 加进 PATH" + "`n" +
           "  · set ANDROID_HOME=D:\Android\Sdk" + "`n" +
           "  · .\collect_device_info.ps1 -AdbPath D:\Android\Sdk\platform-tools\adb.exe")
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$OutputDir = (Resolve-Path -LiteralPath $OutputDir).Path

function Invoke-AdbCapture {
    param(
        [string]$Name,
        [string[]]$Arguments,
        [switch]$Shell
    )
    $args = @()
    if ($Shell) { $args += 'shell' }
    $args += $Arguments
    $stdoutPath = Join-Path $OutputDir "$Name.txt"
    $stderrPath = Join-Path $OutputDir "$Name.stderr.txt"
    & $AdbPath @args 1> $stdoutPath 2> $stderrPath
    $code = $LASTEXITCODE
    [PSCustomObject]@{ Name = $Name; ExitCode = $code; Stdout = $stdoutPath; Stderr = $stderrPath }
}

$started = Get-Date
$deviceList = Invoke-AdbCapture -Name 'adb-devices' -Arguments @('devices', '-l')
$hasDevice = ((Get-Content -LiteralPath $deviceList.Stdout -Raw) -split "`r?`n" | Where-Object { $_ -match '\tdevice\b' }).Count -gt 0

$results = [System.Collections.Generic.List[object]]::new()
$results.Add($deviceList)
if ($hasDevice) {
    $commands = @(
        @{ Name = 'getprop'; Args = @('getprop') },
        @{ Name = 'wm-size'; Args = @('wm', 'size') },
        @{ Name = 'wm-density'; Args = @('wm', 'density') },
        @{ Name = 'dumpsys-display'; Args = @('dumpsys', 'display') },
        @{ Name = 'dumpsys-input'; Args = @('dumpsys', 'input') },
        @{ Name = 'pm-list-packages'; Args = @('pm', 'list', 'packages', '-f') },
        @{ Name = 'mount'; Args = @('mount') },
        @{ Name = 'proc-partitions'; Args = @('cat', '/proc/partitions') },
        @{ Name = 'proc-cpuinfo'; Args = @('cat', '/proc/cpuinfo') },
        @{ Name = 'proc-version'; Args = @('cat', '/proc/version') },
        @{ Name = 'df'; Args = @('df') },
        @{ Name = 'get-uid'; Args = @('id') },
        @{ Name = 'which-su'; Args = @('which', 'su') },
        @{ Name = 'which-fastboot'; Args = @('which', 'fastboot') },
        @{ Name = 'recovery-property'; Args = @('getprop', 'ro.bootmode') },
        @{ Name = 'epd-search'; Args = @('sh', '-c', 'find /system /vendor /data -maxdepth 4 -iname "*epd*" -o -iname "*eink*" 2>/dev/null') }
    )
    foreach ($command in $commands) {
        $results.Add((Invoke-AdbCapture -Name $command.Name -Arguments $command.Args -Shell))
    }
} else {
    $notice = @"
No ADB device was detected.

Next manual step on TOPSIR H9:
1. Settings -> Developer options -> USB debugging: ON.
2. Accept the RSA authorization prompt if shown.
3. If available, select USB configuration = File transfer or ADB.
4. Reconnect the cable and run this script again.

This script intentionally does not install, reboot, remount, or write to the device.
"@
    $notice | Set-Content -LiteralPath (Join-Path $OutputDir 'NEXT-STEP.txt') -Encoding UTF8
}

$summary = [PSCustomObject]@{
    CollectedAt = (Get-Date).ToString('o')
    StartedAt = $started.ToString('o')
    AdbPath = $AdbPath
    DeviceDetected = $hasDevice
    OutputDir = $OutputDir
    Commands = $results
    ReadOnly = $true
}
$summary | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $OutputDir 'collection-summary.json') -Encoding UTF8
$summary | Format-List
