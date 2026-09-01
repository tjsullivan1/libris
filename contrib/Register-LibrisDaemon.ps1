#Requires -Version 5.1
<#
.SYNOPSIS
    Register `libris serve` to start at logon, so the browser extension always
    has something to talk to.

.DESCRIPTION
    The extension's failure message when the daemon is down is "start it with
    libris serve", which means leaving the page you were about to clip. This
    registers a scheduled task instead.

    The task runs with the S4U logon type: no password is stored, and no console
    window appears at logon. The daemon binds loopback only, so it needs no
    network access of the kind S4U withholds.

.PARAMETER TaskName
    The scheduled task to create. Re-running with the same name replaces it.

.PARAMETER Port
    The port to serve on. Must match the extension's server URL.

.PARAMETER LibrisPath
    The libris executable. Resolved from PATH when not given.

.PARAMETER Remove
    Unregister the task instead of creating it.

.EXAMPLE
    .\Register-LibrisDaemon.ps1

.EXAMPLE
    .\Register-LibrisDaemon.ps1 -Port 9000

.EXAMPLE
    .\Register-LibrisDaemon.ps1 -Remove
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = 'Libris daemon',
    [int]$Port = 8787,
    [string]$LibrisPath,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

if ($Remove) {
    if ($PSCmdlet.ShouldProcess($TaskName, 'Unregister scheduled task')) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed the '$TaskName' task. Libris will no longer start at logon."
    }
    return
}

if (-not $LibrisPath) {
    $found = Get-Command libris -ErrorAction SilentlyContinue
    if (-not $found) {
        throw "libris is not on PATH. Install it with 'uv tool install libris[server]', or pass -LibrisPath."
    }
    $LibrisPath = $found.Source
}

if (-not (Test-Path -LiteralPath $LibrisPath)) {
    throw "No libris executable at $LibrisPath."
}

$action = New-ScheduledTaskAction -Execute $LibrisPath -Argument "serve --port $Port" -WorkingDirectory $HOME
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

if ($PSCmdlet.ShouldProcess($TaskName, "Register 'libris serve --port $Port' at logon")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description 'Serves the Libris Shelf to the browser extension on loopback.' `
        -Force | Out-Null

    Start-ScheduledTask -TaskName $TaskName

    # Verified by asking the daemon rather than by trusting the task started.
    # `libris serve` refuses to run without a configured Shelf, so a task that
    # exits immediately every morning is the quiet failure worth catching here.
    $health = $null
    foreach ($attempt in 1..10) {
        Start-Sleep -Milliseconds 500
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2
            break
        }
        catch {
            $health = $null
        }
    }

    if (-not $health) {
        Write-Warning "Registered '$TaskName', but nothing answered on port $Port."
        Write-Warning "Run 'libris serve' by hand to see why. A Shelf must be configured first:"
        Write-Warning "  libris config --vault <path>"
        return
    }

    Write-Host "Registered '$TaskName'. Libris $($health.version) is serving http://127.0.0.1:$Port"

    if (-not $health.vault_configured) {
        Write-Warning 'No Shelf is configured. Run: libris config --vault <path>'
    }
    else {
        Write-Host "Shelf: $($health.vault_path)"
    }

    Write-Host ''
    Write-Host 'Paste this token into the extension options page:'
    & $LibrisPath serve --show-token
}
