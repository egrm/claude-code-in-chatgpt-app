# Claude Code Supervisor

A Windows local MCP bridge that lets a ChatGPT/Codex coordinator supervise persistent Claude Code tasks while retaining its existing conversation and voice interface. Claude tasks live in an **external roster**, separate from native agent tools and the sidebar.

## What it provides

- Persistent official `ClaudeSDKClient` sessions around the installed native `claude.exe`.
- Task states: `running`, `waiting_for_permission`, `waiting_for_answer`, `completed`, `failed`, `interrupted`.
- Compact list/status/wait calls, bounded SQLite event history with cursors and gap detection, and full pending-request inspection on demand.
- Explicit scoped permissions, question answers, interrupt/drain/redirect, final results, stop and exact-session resume.
- An authenticated loopback worker shared by MCP clients. Its transport capability and database stay in a private local runtime directory. Subscription credentials are never read, copied or proxied by this project.

## Setup

Requires Windows, Python 3.14, Claude Code native installation at `%USERPROFILE%\.local\bin\claude.exe`, an existing Claude Max claude.ai login, and the Codex/ChatGPT desktop local MCP host. The executable path resolves from the current user's home directory.

```powershell
Set-Location C:\stardust\Code\claude-code-supervisor
.\setup.ps1 -RegisterMcp -CodexExe 'C:\path\to\codex.exe'
```

Setup creates a project virtual environment, installs pinned dependencies, registers a current-user **on-demand** Task Scheduler entry, starts the worker, backs up the existing Codex configuration privately under `.runtime`, and adds the `claude_supervisor` stdio MCP server. There is no timed/logon trigger and no autonomous inference. The scheduler launches windowless Python outside the MCP client's kill-on-close Windows job; the worker uses its own child cleanup job and a process-scoped policy that hides SDK child windows.

If the app does not discover the added server automatically, open Settings > MCP servers and refresh/restart the server when convenient. Do not end an active voice conversation merely to install this bridge. `/mcp` shows connection status. No application binaries are modified.

## Coordinator workflow

1. `task_list()` displays the external roster.
2. `task_start(prompt, workspace, ...)` returns immediately with a task ID. Defaults: exact `claude-opus-5-5`, `effort="high"`, 30 model turns and a $1 list-price estimate cap. For cheap verification, specify `claude-haiku-5-5`, `effort="low"`, `tools=[]`, a small turn limit and estimate cap.
3. Use `task_status(task_id)` or `task_wait(task_id, after=cursor, timeout=15)` for compact progress. Waits last at most 20 seconds. Continue bounded polling while responsible for a task; there is **no automatic coordinator/chat wake-up**.
4. For a pending request, call `task_request(task_id, request_id)` to inspect the exact proposed tool input. Answer only within the user's authorization using `task_answer(..., allow=true/false, message=...)`. Question answers map exact question text to option labels or free text: `answers={"Format?":"Short"}`. Stale IDs are rejected. Approvals apply once; no broad permission updates are persisted.
5. Request details only when needed with `task_events(task_id, after=cursor, limit=10)`. Limit is at most 20 events; each stored payload is capped at 16000 characters and each task retains at most 1000 events. A gap means events expired; pending requests and terminal state remain in the task record.
6. `task_followup` interrupts and drains an active turn before redirecting in the same session. `task_interrupt` cancels pending approvals. `task_stop` disconnects the process. `task_resume` reconnects by the saved Claude session ID. It does not replay an old approval. `task_result` returns bounded final output/error and billing observations. Independently inspect code and actual test results in the task workspace.

Four Claude sessions may be connected at once. Stop completed sessions to free slots. A worker crash/restart marks unfinished tasks interrupted; resume is explicit. Session transcripts and authentication remain under Claude Code's normal ownership. No transcript picker or unrelated conversation reader is provided.

## Subscription-only controls and limits

The worker removes inherited Anthropic/API/provider routing variables, disables alternate settings sources and plugins/MCP discovery, requires `auth status` to report claude.ai/firstParty/Max, pins exact model IDs, clears fallback chains, and disables fast mode. Claude Code owns login/refresh. `--bare` is deliberately avoided because it disables subscription auth.

**Keep usage credits/overage disabled in your Claude account.** Tests confirmed actual `isUsingOverage=false`, `overageStatus=rejected`, `overageDisabledReason=org_level_disabled`. The SDK reports that evidence through rate-limit events; the worker stops if overage is reported active/enabled or included usage is rejected. This project does not enable credits, buy usage or switch to API keys/providers. Auth status alone cannot independently attest account-level billing settings before the first inference request; do not treat the list-price estimate cap as a guarantee against account-level overage if someone later enables it. CLI estimated dollars are not a subscriber invoice.

File-tool paths are resolved and checked against the task workspace; credential/configuration paths are denied. Mutating tools and shell commands require explicit scoped coordinator approval. A shell command approved by the coordinator runs as the Windows user: the workspace boundary is **not an OS sandbox for shell commands**. Inspect the exact command before approving. Do not approve commands that retrieve credentials or read outside the authorized scope.

Only emitted assistant output and tool lifecycle events are retained. Thinking blocks are deliberately discarded. Activity age is observational; silence does not become a false stalled/failed state. Voice coexistence and app catalog refresh require an actual app-level check; protocol tests do not establish native-agent parity.

## Verify

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp .test-tmp
# No inference:
.\.venv\Scripts\python.exe tests\live_mcp_check.py
# Explicit subscription inference in disposable fixtures:
.\.venv\Scripts\python.exe tests\live_mcp_check.py --infer
.\.venv\Scripts\python.exe tests\live_sdk_check.py
.\.venv\Scripts\python.exe tests\live_mcp_coding_check.py
```

Run async tests with ordinary local Windows access. In the Codex sandbox used during development, even a bare asyncio sleep hung because of Windows event-loop/loopback restrictions; the same tests passed outside that sandbox. Live checks never run as part of the normal pytest suite.

## Troubleshooting and removal

- Check `.runtime/daemon.log` for worker startup errors. It is private and excluded from Git. A local port or scheduled task failure is explicit rather than silently falling back to a different backend.
- If the worker is missing, MCP startup requests its on-demand scheduled task. Running `setup.ps1` again safely reuses an identical task; it refuses to overwrite a different installation's task.
- To shut down only this installation's worker: `.\.venv\Scripts\python.exe server.py --shutdown-worker`. Unfinished tasks become interrupted on next startup.
- To unregister MCP: `codex mcp remove claude_supervisor`. To remove the on-demand launcher, use Task Scheduler to remove the `ClaudeCodeSupervisor-<your-user-SID>` entry whose action points to this folder. No other scheduled tasks are used.
- Configuration backups, event logs, databases, transport capabilities, fixtures and Opus collaboration transcripts are local artifacts excluded from publication.

Opus 5.5 at high effort contributed the SQLite event store. The coordinator reviewed it and verified the implementation with behavioral tests. The remaining lifecycle, routing, SDK and MCP integration code was implemented and verified by the coordinator.

Verified installation results are recorded in [docs/verification.md](docs/verification.md). They establish protocol behavior; this installation still needs its coordinator's refreshed app tool catalog for an app-level voice check.
