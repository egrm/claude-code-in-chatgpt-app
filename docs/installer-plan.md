# Cross-platform installer plan

Goal: after cloning, one command (`python setup.py install` / `python3 setup.py install`) prepares the venv, checks native Claude Code and Max auth, registers the owned MCP entry, registers an owned on-demand worker launcher, starts it and prints activation steps. `setup.ps1` remains as a compatible Windows wrapper.

## Modules

- `setup.py`: stdlib entry; refuses Python < 3.11 before importing anything else (pins: rpds-py and starlette require >= 3.11; `tomllib`).
- `installer.py` (stdlib): `install | configure | doctor | remove`. Steps needing dependencies (Windows ACLs, worker start, MCP discovery) run as subprocesses of the venv interpreter.
- `platform_support.py` (stdlib): venv paths, native Claude discovery (`~/.local/bin/claude(.exe)`, setup-recorded path, PATH; refuses `.cmd`/script/npm shims by executable header magic), `msvcrt`/`fcntl` lock, private atomic writes, service record (`service.json`, legacy Windows `service-task.json`).
- `services.py` (stdlib): three small launcher adapters, ownership-checked before any change.
  - Windows: unchanged Task Scheduler definition (same cmdlets as the previous setup.ps1, run through Windows PowerShell with env-var inputs). An owned `pythonw.exe` task is never modified; a verified legacy `python.exe` action is updated. Started by `schtasks /Run` as before, outside the MCP host's kill-on-close job.
  - macOS: `~/Library/LaunchAgents` user agent without `RunAtLoad`/`KeepAlive`; `launchctl kickstart` on demand.
  - Linux: `systemd --user` unit without `[Install]` (cannot be enabled); `systemctl --user start` on demand. Without a reachable user manager: `start_new_session` detached worker.
- `registration.py` (stdlib): exact `[mcp_servers.claude_supervisor]` table editing on bytes (BOM/newlines/other text preserved), `tomllib` semantic verification, private backup, atomic replace with re-read conflict check. Refuses other installations' entries and non-header (inline/dotted) forms. Removal only of the owned table. No Codex CLI dependency; tomlkit not needed.
- `daemon.py`: Windows job object unchanged; POSIX worker as process-group leader terminates its own group on exit.

## Safety

- No inference in doctor/setup. Auth display limited to `loggedIn`, `authMethod`, `apiProvider`, `subscriptionType`; Max/firstParty only.
- Remove: refuses while tasks are active unless `--force`; authenticated shutdown of this installation's worker only; deletes only the owned MCP table, owned launcher and its record. No recursive project or account-data deletion.
- No app restarts; activation instructions only.

## Validation

Windows: automated regression plus coordinator real smoke. macOS/Linux: implemented, mocked tests only.
