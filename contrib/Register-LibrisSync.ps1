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

    It runs under `conhost --headless`, so no console window appears each time.

.PARAMETER TaskName
    The scheduled task to create. Re-running with the same name replaces it.

.PARAMETER Minutes
    How often to sync.

.PARAMETER LogPath
    The file each run appends to.

.PARAMETER LibrisPath
    The libris executable. Resolved from PATH when not given.

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
    $LibrisPath = $found.Source
}

if (-not (Test-Path -LiteralPath $LibrisPath)) {
    throw "No libris executable at $LibrisPath."
}

$arguments = "--headless `"$LibrisPath`" sync --log `"$LogPath`""
$action = New-ScheduledTaskAction -Execute 'conhost.exe' -Argument $arguments -WorkingDirectory $HOME
# Starts now and repeats with no end date.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes $Minutes)
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

    Start-ScheduledTask -TaskName $TaskName

    # Verified by waiting for the first run and reading how it ended, rather
    # than by trusting the task started: a sync that cannot sign in fails every
    # half hour in silence otherwise.
    $deadline = (Get-Date).AddMinutes(5)
    do {
        Start-Sleep -Seconds 2
        $state = (Get-ScheduledTask -TaskName $TaskName).State
    } while ($state -eq 'Running' -and (Get-Date) -lt $deadline)

    $result = (Get-ScheduledTaskInfo -TaskName $TaskName).LastTaskResult
    Write-Host "Registered '$TaskName', running every $Minutes minutes while you are logged on."
    Write-Host "Log: $LogPath"
    Write-Host ''
    if ($state -eq 'Running') {
        Write-Warning 'The first sync is still running. Check the log when it finishes.'
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
    elseif ($state -ne 'Running') {
        Write-Warning 'The task left no log, so libris never started. Check -LibrisPath.'
    }
}
