$ErrorActionPreference = "SilentlyContinue"

$outputRoot = Join-Path "build" "windows-ci"
New-Item -ItemType Directory -Force $outputRoot | Out-Null

$pythonProcesses = @(Get-Process python*)
$pythonProcessIds = @($pythonProcesses | Select-Object -ExpandProperty Id)
$listeners = @(
    Get-NetTCPConnection -State Listen |
        Where-Object {
            $_.OwningProcess -in $pythonProcessIds -or
            ($_.LocalPort -ge 18000 -and $_.LocalPort -le 20000)
        } |
        Select-Object LocalAddress, LocalPort, OwningProcess
)
$diagnostics = [ordered]@{
    captured_at = (Get-Date).ToUniversalTime().ToString("o")
    python = (python --version 2>&1 | Out-String).Trim()
    processes = @(
        $pythonProcesses |
            Select-Object Id, ProcessName, StartTime, CPU, Responding
    )
    listeners = $listeners
}
$outputPath = Join-Path $outputRoot "diagnostics.json"
$diagnostics | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 $outputPath
