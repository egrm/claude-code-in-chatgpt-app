param([switch]$RegisterMcp, [string]$CodexExe = 'codex')
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv/Scripts/python.exe'
$workerExe = Join-Path $projectRoot '.venv/Scripts/pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    python -m venv (Join-Path $projectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed' }
}
& $pythonExe -m pip install --disable-pip-version-check -q -r (Join-Path $projectRoot 'requirements-dev.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
Push-Location $projectRoot
try {
    & $pythonExe -c 'from runtime import private_runtime; private_runtime()'
    if ($LASTEXITCODE -ne 0) { throw 'Runtime privacy setup failed' }
    $currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $taskName = 'ClaudeCodeSupervisor-' + $currentIdentity.User.Value
    $actionArgs = '"' + (Join-Path $projectRoot 'daemon.py') + '"'
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) {
        if ($existing.Actions.Execute -notin @($pythonExe,$workerExe) -or $existing.Actions.Arguments -ne $actionArgs) {
            throw 'A worker task with this name belongs to another installation; refusing to overwrite it'
        }
        if ($existing.Actions.Execute -ne $workerExe) {
            $action = New-ScheduledTaskAction -Execute $workerExe -Argument $actionArgs -WorkingDirectory $projectRoot
            Set-ScheduledTask -TaskName $taskName -Action $action | Out-Null
        }
    } else {
        $action = New-ScheduledTaskAction -Execute $workerExe -Argument $actionArgs -WorkingDirectory $projectRoot
        $principal = New-ScheduledTaskPrincipal -UserId $currentIdentity.User.Value -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        # On demand only: no timed/logon trigger, no autonomous inference.
        Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Description 'On-demand persistent worker for the local Claude Code supervisor MCP bridge' | Out-Null
    }
    @{name=$taskName;root=$projectRoot} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $projectRoot '.runtime/service-task.json') -Encoding UTF8
    & $pythonExe -c 'from runtime import ensure_worker; ensure_worker(); print("Persistent worker is ready")'
    if ($LASTEXITCODE -ne 0) { throw 'Worker startup failed' }
    if ($RegisterMcp) {
        & $pythonExe (Join-Path $projectRoot 'registration.py') --check-existing
        if ($LASTEXITCODE -ne 0) { throw 'MCP name belongs to another installation' }
        $configHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
        $configFile = Join-Path $configHome 'config.toml'
        if (Test-Path -LiteralPath $configFile) {
            $backupFile = Join-Path $projectRoot ('.runtime/config.toml.backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
            Copy-Item -LiteralPath $configFile -Destination $backupFile
        }
        & $CodexExe mcp add claude_supervisor -- $pythonExe (Join-Path $projectRoot 'server.py')
        if ($LASTEXITCODE -ne 0) { throw 'MCP registration failed' }
        if ($backupFile) {
            & $pythonExe (Join-Path $projectRoot 'registration.py') --preserve-from $backupFile
        } else {
            & $pythonExe (Join-Path $projectRoot 'registration.py')
        }
        if ($LASTEXITCODE -ne 0) { throw 'MCP timeout setup failed' }
    }
} finally { Pop-Location }
