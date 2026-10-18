# Compatibility wrapper: the installer is now `python setup.py install` (all platforms).
# MCP registration is part of install; -RegisterMcp is accepted for older instructions and
# -CodexExe is no longer needed (no Codex CLI dependency). Use -NoConfigure to skip it.
param([switch]$RegisterMcp, [string]$CodexExe = '', [switch]$NoConfigure)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
if ($CodexExe) { Write-Warning '-CodexExe is ignored: setup.py edits only the owned MCP table directly' }
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPython) {
    $python = @($venvPython)
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = @('python')
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $python = @('py', '-3')
} else {
    throw 'Python 3.11+ not found; install it from python.org and reopen PowerShell'
}
$arguments = @((Join-Path $projectRoot 'setup.py'), 'install', '--dev')
if ($NoConfigure) { $arguments += '--no-configure' }
$command = $python[0]
$prefix = @($python | Select-Object -Skip 1)
& $command @prefix @arguments
if ($LASTEXITCODE -ne 0) { throw "setup.py install failed (exit $LASTEXITCODE)" }
