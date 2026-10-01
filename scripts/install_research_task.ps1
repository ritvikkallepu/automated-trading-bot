param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$DataDirectory,
    [int]$Port = 63332,
    [string]$TaskName = 'CoinDCX Research Collector'
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$windowless = Join-Path (Split-Path $pythonPath) 'pythonw.exe'
if (Test-Path -LiteralPath $windowless) { $pythonPath = $windowless }
$data = [System.IO.Path]::GetFullPath($DataDirectory)
$arguments = '-u -m app.research.continuous.runtime --data-dir "{0}" --port {1}' -f $data, $Port
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument $arguments -WorkingDirectory $repo
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Public market research only. Restarts the research worker; never starts trading.' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output "Research collector installed: $TaskName. Dashboard http://127.0.0.1:$Port/#research"
