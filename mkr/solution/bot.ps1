param(
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$scriptPath = Join-Path $PSScriptRoot 'telegram_bot.py'
$runtime = Join-Path $PSScriptRoot '.runtime'
$pidFile = Join-Path $runtime 'bot.pid'
$stdout = Join-Path $runtime 'bot.stdout.log'
$stderr = Join-Path $runtime 'bot.stderr.log'

function Find-BotProcesses {
    $recordedId = 0
    if (Test-Path -LiteralPath $pidFile) {
        [void][int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$recordedId)
    }
    $absolute = [regex]::Escape($scriptPath)
    $relative = '(?:^|[\s"])mkr2[\\/]solution[\\/]telegram_bot\.py(?=[\s"]|$)'
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object {
        $_.CommandLine -match ('(?:^|[\s"])' + $absolute + '(?=[\s"]|$)') -or
        ($recordedId -gt 0 -and ($_.ProcessId -eq $recordedId -or $_.ParentProcessId -eq $recordedId) -and
            $_.CommandLine -match $relative)
    }
}

$running = @(Find-BotProcesses)
if ($Action -eq 'status') {
    if ($running.Count) {
        Write-Output ('Bot is running (PID {0}).' -f ($running.ProcessId -join ', '))
    } else {
        Write-Output 'Bot is stopped.'
    }
    return
}

if ($Action -eq 'stop') {
    if ($running.Count) {
        Stop-Process -Id @($running.ProcessId) -ErrorAction SilentlyContinue
        $deadline = (Get-Date).AddSeconds(5)
        while (@(Find-BotProcesses).Count -and (Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 200
        }
        if (@(Find-BotProcesses).Count) { throw 'The bot process is still running.' }
    }
    Write-Output 'Bot is stopped.'
    return
}

if ($running.Count) {
    Write-Output 'Bot is already running.'
    return
}

$python = Join-Path $root '.venv\Scripts\python.exe'
if (!(Test-Path -LiteralPath $python)) {
    $python = (Get-Command python.exe).Source
}
New-Item -Path $runtime -ItemType Directory -Force | Out-Null
$process = Start-Process -FilePath $python -ArgumentList '-X', 'utf8', '-u', ('"{0}"' -f $scriptPath) `
    -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
Set-Content -LiteralPath $pidFile -Value $process.Id
Start-Sleep -Seconds 2
$process.Refresh()
if ($process.HasExited) {
    Get-Content -LiteralPath $stderr -Encoding UTF8
    throw ('Bot exited with code {0}.' -f $process.ExitCode)
}
Write-Output ('Bot process started (PID {0}).' -f $process.Id)
Get-Content -LiteralPath $stdout -Encoding UTF8
