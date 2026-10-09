#Requires -Version 5.1
<#
.SYNOPSIS
    Run `libris sync` every half hour, so the remote Library stays current with
    no one at the keyboard.

.DESCRIPTION
    Registers a scheduled task that pushes what changed on the Shelf to Cosmos
    (ADR 0002, ADR 0015). Every run appends what it said, and how it ended, to a
    log file, so a run that failed while nobody watched can still be read.

    The task runs only while you are logged on, as you. Sync signs in to Azure
    through your `az login`, and that sign-in is encrypted for your logon:
    the S4U logon type the daemon's task uses has no password to unlock it.

    It runs `librisw`, the console-less build of `libris` installed beside it,
    so no window appears each time and the task's result is sync's own exit
    code. `conhost --headless` hid the window too, but reported every run as a
    success.

.PARAMETER TaskName
    The scheduled task to create. Re-running with the same name replaces it.

.PARAMETER Minutes
    How often to sync.

.PARAMETER LogPath
    The file each run appends to.

.PARAMETER LibrisPath
    The librisw executable. Found beside the libris on PATH when not given.

.PARAMETER Remove
    Unregister the task instead of creating it.

.EXAMPLE
    .\Register-LibrisSync.ps1

.EXAMPLE
    .\Register-LibrisSync.ps1 -Minutes 60

.EXAMPLE
    .\Register-LibrisSync.ps1 -Remove
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = 'Libris sync',
    [ValidateRange(5, 1440)]
    [int]$Minutes = 30,
    [string]$LogPath = (Join-Path $env:LOCALAPPDATA 'libris\sync.log'),
    [string]$LibrisPath,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

if ($Remove) {
    if ($PSCmdlet.ShouldProcess($TaskName, 'Unregister scheduled task')) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed the '$TaskName' task. The remote Library now changes only when you run libris sync."
    }
    return
}

if (-not $LibrisPath) {
    $found = Get-Command libris -ErrorAction SilentlyContinue
    if (-not $found) {
        throw "libris is not on PATH. Install it with 'uv tool install libris[sync]', or pass -LibrisPath."
    }
    $LibrisPath = Join-Path (Split-Path -Parent $found.Source) 'librisw.exe'
}

if (-not (Test-Path -LiteralPath $LibrisPath)) {
    throw "No librisw executable at $LibrisPath. It ships with libris 0.18 and later: uv tool install 'libris[sync]' --force"
}

$action = New-ScheduledTaskAction -Execute $LibrisPath -Argument "sync --log `"$LogPath`"" -WorkingDirectory $HOME
# First fires one interval from now and repeats with no end date. Not now: the
# explicit start below is the first run, and a trigger firing at registration
# would race it, leaving this script waiting on a run it never saw start
# (#195 review).
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes($Minutes) -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

if ($PSCmdlet.ShouldProcess($TaskName, "Register 'libris sync' every $Minutes minutes")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description "Pushes what changed on the Libris Shelf to the remote Library. Log: $LogPath" `
        -Force | Out-Null

    # Verified by waiting for the first run and reading how it ended, rather
    # than by trusting the task started: a sync that cannot sign in fails every
    # half hour in silence otherwise. Start-ScheduledTask does not wait, and the
    # task can still read Ready or Queued before the run begins, so this waits
    # for a run that started after this point to leave both (#195 review).
    $before = (Get-ScheduledTaskInfo -TaskName $TaskName).LastRunTime
    Start-ScheduledTask -TaskName $TaskName
    $deadline = (Get-Date).AddMinutes(5)
    do {
        Start-Sleep -Seconds 2
        $state = (Get-ScheduledTask -TaskName $TaskName).State
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        $finished = $info.LastRunTime -gt $before -and $state -notin @('Running', 'Queued')
    } while (-not $finished -and (Get-Date) -lt $deadline)

    $result = $info.LastTaskResult
    Write-Host "Registered '$TaskName', running every $Minutes minutes while you are logged on."
    Write-Host "Log: $LogPath"
    Write-Host ''
    if (-not $finished) {
        Write-Warning "The first sync has not finished (task state: $state). Check the log when it does."
    }
    elseif ($result -ne 0) {
        Write-Warning "The first sync failed (exit $result). The end of the log says why:"
    }
    else {
        Write-Host 'The first sync succeeded:'
    }
    if (Test-Path -LiteralPath $LogPath) {
        Get-Content -LiteralPath $LogPath -Tail 15
    }
    elseif ($finished) {
        Write-Warning 'The task left no log, so libris never started. Check -LibrisPath.'
    }
}
