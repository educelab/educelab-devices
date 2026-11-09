<#
.SYNOPSIS
Repeats the GenTL stall configuration (track gentl-stall_20261109, Phase 2)
and logs every run, stopping at the first stall.

.DESCRIPTION
Each run is `el-devices genicam --backend gentl --pixel-format Mono8
--exposure min --throughput-limit 80 --frames 100` (or `el-devices spinnaker`
with -Driver spinnaker). Every run's output goes to its own log in -LogDir,
and a summary file lists each run's result. A run counts as stalled when the
report prints a camera's "first failure".

Run it from the venv with educelab-devices installed. If scripts are blocked:
  powershell -ExecutionPolicy Bypass -File .\stall-runs.ps1 -LogDir E:\gentl-stall

.EXAMPLE
.\stall-runs.ps1 -LogDir E:\gentl-stall
Task 2.1: up to 10 runs, in the producer's camera order.

.EXAMPLE
.\stall-runs.ps1 -LogDir E:\gentl-stall -Serial 20305113,20305122,20305114,20298452,20298479
Task 2.3: 20305113 opened first.

.EXAMPLE
.\stall-runs.ps1 -LogDir E:\gentl-stall -Driver spinnaker -Runs 8
Task 2.4: the same configuration through Spinnaker.
#>
param(
    [Parameter(Mandatory = $true)] [string] $LogDir,
    [int] $Runs = 10,
    [string[]] $Serial = @(),
    [ValidateSet("gentl", "spinnaker")] [string] $Driver = "gentl",
    [switch] $KeepGoing
)

# Native stderr must not stop the loop.
$ErrorActionPreference = "Continue"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$settings = @("--pixel-format", "Mono8", "--exposure", "min",
    "--throughput-limit", "80", "--frames", "100")
if ($Driver -eq "gentl") {
    $command = @("genicam", "--backend", "gentl") + $settings
} else {
    $command = @("spinnaker") + $settings
}
foreach ($s in $Serial) { $command += @("--serial", $s) }

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$summary = Join-Path $LogDir "$stamp-$Driver-summary.txt"
$version = (& python -c "import educelab.devices as d; print(d.__version__)") 2>&1
Set-Content -Path $summary -Encoding utf8 -Value (
    "el-devices $($command -join ' ')`n" +
    "educelab-devices $version, $(& python --version 2>&1), $env:COMPUTERNAME")

for ($run = 1; $run -le $Runs; $run++) {
    $log = Join-Path $LogDir ("{0}-{1}-run{2:D2}.log" -f $stamp, $Driver, $run)
    $header = "run $run of ${Runs}, $(Get-Date -Format o): el-devices $($command -join ' ')"
    Write-Host "== $header"
    Set-Content -Path $log -Value $header -Encoding utf8
    & el-devices @command 2>&1 | ForEach-Object {
        $line = "$_"
        Write-Host $line
        Add-Content -Path $log -Value $line -Encoding utf8
    }
    $code = $LASTEXITCODE
    $stalled = Select-String -Path $log -Pattern "first failure" -Quiet
    if ($stalled) { $result = "STALLED" }
    elseif ($code -ne 0) { $result = "exit $code" }
    else { $result = "ok" }
    $line = "run ${run}: $result, $(Get-Date -Format o) ($(Split-Path $log -Leaf))"
    Add-Content -Path $summary -Value $line -Encoding utf8
    Write-Host "== $line"
    if ($stalled -and -not $KeepGoing) {
        Write-Host "Stopping: a camera stalled. Its counters are in $log."
        break
    }
    Start-Sleep -Seconds 5
}
Write-Host "Summary: $summary"
