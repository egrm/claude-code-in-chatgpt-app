# Claude Code in ChatGPT App

Use your own Claude Code (Claude Max) as a supervised coding agent from a ChatGPT/Codex desktop chat via local MCP tools (`claude_supervisor`). ChatGPT coordinates; Claude works in a folder you authorize. This is an external task roster, not a model-picker entry or native subagent; there is no automatic chat wake-up.

## Requirements

- Desktop app with local MCP servers ([quickstart](https://learn.chatgpt.com/docs/quickstart), [MCP](https://learn.chatgpt.com/docs/extend/mcp)).
- Python 3.11+ (3.12+ recommended), Git; on Windows also Git for Windows (Git Bash).
- Native Claude Code ([install](https://code.claude.com/docs/en/setup)) at `~/.local/bin/claude` (`claude.exe` on Windows) or a native binary on PATH; npm/`.cmd` shims are not used. Signed in with **Claude Max**: `claude auth login --claudeai`.

## Install

```sh
git clone https://github.com/egrm/claude-code-in-chatgpt-app.git
cd claude-code-in-chatgpt-app
python setup.py install        # macOS/Linux: python3 setup.py install
```

Then use the app's MCP Restart/refresh (no restart is done for you), type `/mcp`, and ask: *"Use the claude_supervisor tools to call task_list. Do not start an agent yet."*

| Command | Purpose |
| --- | --- |
| `setup.py install` | venv + pinned deps, Claude/auth check, owned launcher, owned MCP entry, real MCP discovery (no inference) |
| `setup.py configure` | register only the owned `[mcp_servers.claude_supervisor]` entry (backup, atomic, other settings preserved) |
| `setup.py doctor` | diagnose prerequisites, 4 non-secret auth fields, entry, launcher, worker, MCP discovery; no inference |
| `setup.py remove` | remove only this checkout's entry and launcher (refuses while tasks are active unless `--force`); keeps files |

`setup.ps1` remains as a Windows wrapper for `setup.py install`.

## Behaviour

- **Max only.** Clean environment, `claude.ai`/`firstParty`/`max` required; no API key, provider, model fallback or paid-credit route; fast mode off. Keep usage credits disabled in Claude settings. Defaults: Opus 5.5 / high effort.
- **On demand.** No boot/login autostart. The worker runs only while an MCP client holds a lease. Shutdown (drain) begins about 10 s after the last client disconnects cleanly, or at most 16 s after its last heartbeat; new work is refused at once, active tasks are saved as `interrupted`, then a bounded cleanup closes the owned Claude processes (worst case roughly 14 s more on POSIX). Continue later with `task_resume`.
- **Approvals vs instructions.** `task_answer` resolves one specific pending permission or question. A note sent with an approval does not reach Claude; use `task_followup` to give new instructions.

## Platform status

| Platform | Launcher | Status |
| --- | --- | --- |
| Windows | current-user on-demand Task Scheduler task | tested: automated suite plus real install, repeat install, doctor, remove and last-client exit |
| Linux | `systemd --user` unit without `[Install]`, else detached session | tested on Ubuntu (real systemd user unit, MCP discovery, last-client exit); no Linux desktop app or auth/inference run |
| macOS | user LaunchAgent without RunAtLoad/KeepAlive | implemented, mocked tests only; not run on a Mac |

Details: [manual setup and reference](docs/manual-setup.md), [verification](docs/verification.md), [design](docs/installer-plan.md).
