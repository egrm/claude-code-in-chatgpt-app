# Manual setup and reference

## Prerequisites in detail

- Python 3.11+ from python.org/Homebrew/your distribution. Debian/Ubuntu may need `python3-venv`/`python3-pip`; setup falls back to `ensurepip` or the system pip if the venv lacks pip.
- Native Claude Code signed in with Claude Max. Check only non-secret fields: `claude auth status --json` should show `loggedIn=true`, `authMethod=claude.ai`, `apiProvider=firstParty`, `subscriptionType=max`. Never copy tokens or credential files.
- Windows: Git for Windows (Git Bash) for Claude's Bash tool. The Windows worker uses `pythonw.exe` (no console); the MCP command uses `python.exe`.

## What setup changes

- `.venv`, `.runtime` (private: task database, loopback capability, logs, config backups, `install.json` with the detected Claude path).
- Host config `~/.codex/config.toml` (or `$CODEX_HOME`): only `[mcp_servers.claude_supervisor]` with `command` = this checkout's venv Python, `args` = `[server.py]`, `startup_timeout_sec = 30`, `tool_timeout_sec = 60`. A backup is written to `.runtime` before any change; other text is preserved byte-for-byte. An entry owned by another folder is refused.
- Launcher (one per user): Windows task `ClaudeCodeSupervisor-<SID>` (no trigger, interactive, limited, no time limit, single instance; legacy `.runtime/service-task.json` still read); macOS `~/Library/LaunchAgents/com.github.egrm.claude-code-supervisor.plist`; Linux `~/.config/systemd/user/claude-code-supervisor.service`, or a detached session when no user systemd is reachable.

To register by hand instead of `configure`, use the app's **Settings > MCP servers**: name `claude_supervisor`, STDIO, command = the venv Python, one argument = `server.py`, no environment or credentials.

## Lifecycle

The MCP facade holds a lease (heartbeat 2 s, TTL 6 s). With no lease the worker waits 10 s for a reconnect (30 s if nobody ever connected), then refuses new work, persists active tasks as `interrupted`, closes Claude connections within 8 s and exits; leftover owned processes are killed (Windows job, POSIX own process group). Resume is explicit with `task_resume`. Pending approvals are cancelled, not carried over.

Upgrading: `setup.py install` stops an older idle worker; if it has active tasks it is left running and setup exits 4. After upgrading, refresh the MCP server in the app so its facade is current.

## Tools

`task_list`, `task_status`, `task_wait` (≤20 s compact poll), `task_events`, `task_request`, `task_answer` (one specific permission/question; approval notes are not delivered), `task_followup` (new instructions; interrupts the current turn), `task_interrupt`, `task_resume`, `task_stop`, `task_result`, `task_start` (defaults Opus 5.5/high, 30 turns, `max_budget_usd` estimate cap 1.0). At most four connected sessions.

## Billing boundaries

Claude Code owns auth. The worker clears API/provider variables, ignores alternate settings, pins exact models, disables fallback and fast mode, and stops a session if overage is reported enabled or in use. `max_budget_usd` is a list-price estimate cap, not a spending guard. Keep usage credits disabled.

## Troubleshooting

Run `python setup.py doctor` first. Common fixes: install native Claude and log in with `--claudeai`; enable the entry in app settings; refresh MCP after setup; "lease required" means the app's facade predates this version (refresh it); a foreign entry/launcher means another checkout is registered — run `setup.py remove` there first. Worker log: `.runtime/daemon.log` (Linux: `journalctl --user -u claude-code-supervisor`).

## Development checks (no inference)

```sh
.venv/bin/python -m pytest -q          # Windows: .venv\Scripts\python.exe -m pytest -q --basetemp .test-tmp
.venv/bin/python mcp_probe.py
```

Opt-in inference checks: `tests/live_mcp_check.py --infer`, `tests/live_sdk_check.py`, `tests/live_mcp_coding_check.py`.
