# Cross-platform installer plan

Goal: after cloning, one command (`python setup.py install` / `python3 setup.py install`) prepares the venv, checks native Claude Code and Max auth, registers the owned MCP entry, registers an owned on-demand worker launcher, starts it and prints activation steps. `setup.ps1` remains as a compatible Windows wrapper.

## Modules

- `setup.py`: stdlib entry; refuses Python < 3.11 before importing anything else (pins: rpds-py and starlette require >= 3.11; `tomllib`).
- `installer.py` (stdlib): `install | configure | doctor | remove`. Steps needing dependencies (Windows ACLs, worker start, MCP discovery) run as subprocesses of the venv interpreter.
- `platform_support.py` (stdlib): venv paths, native Claude discovery (`~/.local/bin/claude(.exe)`, setup-recorded path, PATH; refuses `.cmd`/script/npm shims by executable header magic), `msvcrt`/`fcntl` lock, private atomic writes, service record (`service.json`, legacy Windows `service-task.json`).
- `services.py` (stdlib): three small launcher adapters, ownership-checked before any change.
  - Windows: unchanged Task Scheduler definition (same cmdlets as the previous setup.ps1, run through Windows PowerShell with env-var inputs). A correct owned `pythonw.exe` task is left unchanged; a verified legacy `python.exe` action is updated; triggers/restart/time-limit drift is repaired only while the task is idle (refused while running). Started by `schtasks /Run` as before, outside the MCP host's kill-on-close job.
  - macOS: `~/Library/LaunchAgents` user agent without `RunAtLoad`/`KeepAlive`; `launchctl kickstart` on demand.
  - Linux: `systemd --user` unit without `[Install]` (cannot be enabled); `systemctl --user start` on demand. Without a reachable user manager: `start_new_session` detached worker.
- `registration.py` (stdlib): exact `[mcp_servers.claude_supervisor]` table editing on bytes (BOM/newlines/other text preserved), `tomllib` semantic verification, private backup, atomic replace with re-read conflict check. Refuses other installations' entries and non-header (inline/dotted) forms. Removal only of the owned table. No Codex CLI dependency; tomlkit not needed.
- `daemon.py`: Windows job object unchanged; POSIX worker as process-group leader terminates its own group on exit.

## Worker lifecycle (`lifecycle.py`)

- On demand only: no boot/login autostart. The facade starts the worker through the owned launcher and holds a lease (heartbeat 2 s, TTL 6 s). Work RPCs require a live lease; requests never extend it.
- No lease for 10 s (grace; startup grace 30 s) → drain: new operations/answers/tool actions refused, active tasks persisted `interrupted` (explicit `task_resume` later), Claude connections closed concurrently within 8 s, then a hard-exit timer ends the process (Windows kill-on-close job; POSIX per-member SIGTERM then own-group SIGKILL). Worst case after abrupt client loss: 16 s plus bounded cleanup.
- `health` reports protocol 2 and `running|draining`; `ensure_worker` waits for a draining predecessor's endpoint and `worker.lock` before launching. Admin `admin_tasks`/`shutdown` need the capability but no lease. Setup stops only an idle older worker.

## Safety

- No inference in doctor/setup. Auth display limited to `loggedIn`, `authMethod`, `apiProvider`, `subscriptionType`; Max/firstParty only.
- Remove: refuses while tasks are active unless `--force`; authenticated shutdown of this installation's worker only; deletes only the owned MCP table, owned launcher and its record. No recursive project or account-data deletion.
- No app restarts; activation instructions only.

## Validation

Windows: automated suite plus real install/repeat/doctor/remove/last-client exit. Linux: Ubuntu suite plus real `systemd --user` install, discovery and last-client cleanup (no desktop app/inference). macOS: mocked tests only.
