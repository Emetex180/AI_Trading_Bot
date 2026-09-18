# Install the bot as Windows Scheduled Tasks
#
# Registers two tasks that survive a reboot and restart on failure:
#
#   AITradingBot-Scanner  -> python run.py scan    (the live engine)
#   AITradingBot-Web      -> python run.py serve   (Waitress, behind IIS)
#
# They are two tasks rather than one because MetaTrader5's Python API binds the
# whole process to a single terminal. Running the scanner inside the web process
# would let a browser request reach the terminal in the middle of a poll.
#
# Usage (PowerShell, as Administrator):
#
#   .\install-service.ps1 -ProjectPath C:\apps\ai-trading-bot
#   .\install-service.ps1 -ProjectPath C:\apps\ai-trading-bot -Uninstall
#
# Options:
#   -RunAs <account>   the account the tasks run under. Defaults to the account
#                      running this script. It needs "Log on as a batch job"
#                      and read/write access to the project directory.
#   -DelaySeconds <n>  how long the scanner waits after boot before starting.
#                      Default 90, to give the MT5 terminal time to log in.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectPath,

    [string]$RunAs = "$env:USERDOMAIN\$env:USERNAME",

    [int]$DelaySeconds = 90,

    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$scannerTask = 'AITradingBot-Scanner'
$webTask     = 'AITradingBot-Web'

function Remove-BotTask {
    param([string]$Name)
    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "Removed task '$Name'."
    } else {
        Write-Host "Task '$Name' was not registered."
    }
}

if ($Uninstall) {
    Remove-BotTask -Name $webTask
    Remove-BotTask -Name $scannerTask
    Write-Host ''
    Write-Host 'Tasks removed. The application, its database and its .env were not touched.'
    exit 0
}

# --------------------------------------------------------------------------
# Validate the install before registering anything against it
# --------------------------------------------------------------------------
$ProjectPath = (Resolve-Path -LiteralPath $ProjectPath).Path
$python = Join-Path $ProjectPath '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    throw "No virtual environment python at $python. Create one first: py -3.12 -m venv .venv"
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectPath 'run.py'))) {
    throw "$ProjectPath does not look like the project root (no run.py)."
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectPath '.env'))) {
    Write-Warning "No .env in $ProjectPath. The app will start with defaults, " +
                  "which means no FLASK_SECRET_KEY and sessions that reset on every restart."
}

# The venv's python.exe is a launcher; -FilePath with quoted arguments is the
# reliable way to pass a script path through Task Scheduler without the quoting
# getting mangled.
function New-BotTask {
    param(
        [string]$Name,
        [string]$Command,
        [string]$Description,
        [int]$Delay
    )

    $action = New-ScheduledTaskAction `
        -Execute $python `
        -Argument "run.py $Command" `
        -WorkingDirectory $ProjectPath

    # Start at boot, wait for the network and (for the scanner) for the terminal.
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Delay = "PT${Delay}S"

    # Restart on crash, but not forever: three attempts, five minutes apart, on
    # top of the one-hour default. A process that dies instantly should surface
    # as a stopped task rather than an invisible restart loop.
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 5) `
        -ExecutionTimeLimit (New-TimeSpan -Days 0) `
        -MultipleInstances IgnoreNew

    $principal = New-ScheduledTaskPrincipal `
        -UserId $RunAs `
        -LogonType S4U `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description $Description `
        -Force | Out-Null

    Write-Host "Registered '$Name'  ($python run.py $Command)"
}

# The scanner starts after the terminal has had time to log in; the web app does
# not depend on MT5 at all, so it starts immediately.
New-BotTask -Name $scannerTask -Command 'scan' -Delay $DelaySeconds `
    -Description 'Live ICT scanner (alert only unless AUTO_TRADING=true).'

New-BotTask -Name $webTask -Command 'serve' -Delay 0 `
    -Description 'Client platform behind Waitress. Front with IIS + ARR on 443.'

Write-Host ''
Write-Host 'Both tasks registered. Start them now with:'
Write-Host "    Start-ScheduledTask -TaskName '$scannerTask'"
Write-Host "    Start-ScheduledTask -TaskName '$webTask'"
Write-Host ''
Write-Host 'Check on them with:'
Write-Host "    Get-ScheduledTask -TaskName 'AITradingBot-*' | Get-ScheduledTaskInfo"
Write-Host ''
Write-Host "The tasks run as $RunAs. If that account cannot read the project"
Write-Host 'directory, or is not allowed to log on as a batch job, the tasks will'
Write-Host 'register but fail to start -- check the Task Scheduler history.'
