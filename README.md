# Claude Code in ChatGPT App

> **Work in progress:** This branch is an implementation checkpoint. The simplified cross-platform installer and close-with-app lifecycle are not yet release-verified. See [remaining work](docs/WORK-IN-PROGRESS.md). `main` remains the previous verified Windows implementation.

Use Claude Code from a ChatGPT/Codex desktop conversation as a persistent, supervised coding agent. ChatGPT coordinates; Claude works in the local folder you choose. You can keep using the existing chat and its voice interface.

This project adds an **external Claude task roster through local MCP tools**. It does not add Claude to the model picker or create native subagent cards. The coordinator checks progress with compact polling; **there is no automatic chat wake-up** when Claude finishes or asks a question.

## Before you start

You need:

- **Windows x64 and a desktop app with local MCP support.** Use the local computer/Codex host, with **Settings > MCP servers** available. This installation is not a ChatGPT-web connector or a cloud-only setup. Account, app rollout and workspace policy can affect availability; see [OpenAI's local MCP instructions](https://learn.chatgpt.com/docs/extend/mcp).
- **Python 3.14 x64**, with `python` available in PowerShell. The verified environment used Python 3.14.4 and PowerShell 7.6.5. Use PowerShell 7 for these commands; other Python/PowerShell versions were not validated. Download Python from [python.org](https://www.python.org/downloads/windows/) and PowerShell from [Microsoft](https://learn.microsoft.com/powershell/scripting/install/installing-powershell-on-windows).
- **Git for Windows**, including Git Bash, from [git-scm.com](https://git-scm.com/downloads/win). Git clones this private repository; Git Bash is needed for the bridge's `Bash` coding/test tool. The bridge does not currently expose Claude's alternative PowerShell tool. Use the standard Git for Windows installation so Claude can discover Bash.
- **Native Claude Code** at `%USERPROFILE%\.local\bin\claude.exe`, signed into your own Claude account. The verified CLI version was **2.1.302**. Use the [official Windows installation instructions](https://code.claude.com/docs/en/setup); an npm `.cmd` shim or a WSL-only installation is not the executable this bridge uses.
- **Claude Max**, with access to the model you request. **This release deliberately accepts Max only**: its guard rejects other subscription types even if the wider Claude Agent SDK supports them. Haiku 5.5 and Opus 5.5 are the only model IDs exposed here; their inclusion is not a guarantee of your account's availability.
- Permission to run a **current-user, on-demand Windows scheduled task** and local loopback communication. Managed computers may require administrator approval for these facilities. There is no timed or logon trigger.
- Access to this **private GitHub repository**. A “repository not found” clone error may mean your GitHub account needs access or Git authentication.

No Anthropic API key, copied subscription token, Node.js installation or terminal UI scraping is required by this bridge. The optional Codex CLI registration route below requires `codex`; the main app-settings route does not.

## 1. Install and verify Claude Code

After installing the native CLI using Anthropic's instructions, open **PowerShell 7** and run:

```powershell
$Claude = Join-Path $env:USERPROFILE '.local\bin\claude.exe'
Test-Path -LiteralPath $Claude
& $Claude --version
& $Claude auth login --claudeai
```

`Test-Path` should return `True`. Complete the Claude Code browser sign-in using your **Claude Max subscription**, not the Anthropic Console/API billing route. If already signed in correctly, you can skip the login command.

Check only the relevant non-secret status fields:

```powershell
& $Claude auth status --json |
    ConvertFrom-Json |
    Select-Object loggedIn, authMethod, apiProvider, subscriptionType
```

The expected values are `True`, `claude.ai`, `firstParty`, `max`. If they differ, fix the sign-in before starting a task. The bridge performs its own clean-environment authentication check as well. Do not copy credential files, run token-export commands, or paste credentials into ChatGPT or this repository.

**In Claude account Settings > Usage, keep usage credits/extra usage disabled.** Included subscription limits still apply. Do not enable paid credits to work around a rate limit. The [subscription billing details and limitations](#subscription-usage-and-billing-boundaries) below explain what the bridge can verify.

## 2. Clone into a permanent local folder

Choose a folder you intend to keep. The example uses your Windows home directory, so no machine-specific username is required:

```powershell
python --version
git --version
$CodeFolder = Join-Path $env:USERPROFILE 'Code'
New-Item -ItemType Directory -Path $CodeFolder -Force | Out-Null
Set-Location $CodeFolder
git clone https://github.com/egrm/claude-code-in-chatgpt-app.git
$Project = Join-Path $CodeFolder 'claude-code-supervisor'
Set-Location $Project
```

If that clone folder already exists, inspect it and use the existing checkout rather than overwriting it. Keep the project out of temporary folders. Moving it later requires updating the MCP entry and removing/recreating the narrowly identified worker launcher.

## 3. Install the project and hidden worker

From the cloned project directory:

```powershell
.\setup.ps1
```

This is a **project-supplied installer**, not a built-in ChatGPT command. It:

1. Creates `.venv` using the `python` command and installs `requirements-dev.txt`, including the pinned SDK/MCP dependencies and test tools. Virtual-environment activation is unnecessary.
2. Creates a private `.runtime` directory for this installation's task database, local transport capability and logs. It does not copy Claude subscription credentials.
3. Registers an on-demand current-user launcher named `ClaudeCodeSupervisor-<your-Windows-user-SID>`. Its action runs this folder's `.venv\Scripts\pythonw.exe` with `daemon.py`.
4. Starts the hidden worker and prints **`Persistent worker is ready`**.

There should be no blank Python/Claude console window. The scheduled task has no timed/logon trigger and starts no model work by itself. Reconnecting an MCP client keeps the independently owned worker and its Claude tasks alive. After a worker crash or Windows restart, reconnecting starts the worker again; unfinished task records become `interrupted` and need explicit resume. The launcher uses the signed-in user's interactive Windows session; it is not an unattended service for a logged-out user.

If PowerShell blocks the script, check `Get-ExecutionPolicy -List` and your organization's policy. Where permitted, a session-only setting such as `Set-ExecutionPolicy -Scope Process RemoteSigned` can allow local scripts. Do not weaken a managed policy or change the machine-wide policy to install this project.

## 4. Connect it to ChatGPT desktop

### Recommended: use the app's MCP settings

First print the two absolute paths you will enter:

```powershell
Join-Path $Project '.venv\Scripts\python.exe'
Join-Path $Project 'server.py'
```

In the desktop app, open **Settings > MCP servers > Add server** and enter:

| Field | Value |
| --- | --- |
| Name | `claude_supervisor` |
| Transport | `STDIO` |
| Command | The first printed path: this project's `.venv\Scripts\python.exe` |
| Arguments | One argument: the second printed path, this project's `server.py` |
| Environment/credentials | Leave empty; no API key or OAuth token is supplied here |

The **MCP command uses `python.exe`** for its stdin/stdout protocol. The separate hidden **worker uses `pythonw.exe`**. Do not swap them. Enter the command and argument as separate fields, not one shell command; preserve a path containing spaces as one argument using the settings editor's argument handling.

Save the server. For the more generous startup timeout used by this project, its host configuration should look like this, with **your actual absolute paths**:

```toml
[mcp_servers.claude_supervisor]
command = 'C:\Users\YourName\Code\claude-code-supervisor\.venv\Scripts\python.exe'
args = ['C:\Users\YourName\Code\claude-code-supervisor\server.py']
startup_timeout_sec = 30
tool_timeout_sec = 60
```

These are **built-in host MCP settings**, not Claude settings. They live in `%USERPROFILE%\.codex\config.toml`, or the configured `CODEX_HOME` location. If you edit the file directly, back it up privately, modify only this table, and avoid adding a duplicate table. Other MCP servers/settings must remain intact. Do not add this server's local loopback transport capability to the host configuration; the supplied facade handles that internally.

### Alternative: register using the Codex CLI

Skip this alternative if you used app settings. With `codex` available in PowerShell, run from the project directory:

```powershell
Get-Command codex
.\setup.ps1 -RegisterMcp
codex mcp get claude_supervisor --json
```

If `codex` is not on PATH but you have its executable, supply its actual path:

```powershell
$CodexExe = 'C:\path\to\codex.exe'
.\setup.ps1 -RegisterMcp -CodexExe $CodexExe
& $CodexExe mcp get claude_supervisor --json
```

`-RegisterMcp` is **this project's setup option**. It calls the host's supported `codex mcp add` command, privately backs up existing configuration under `.runtime`, preserves unrelated settings and sets this entry's 30/60-second timeouts. It refuses to overwrite a `claude_supervisor` entry belonging to a different installation.

## 5. Activate and confirm the connection

Use the app's offered **Restart/refresh** control after saving MCP settings, then open the chat where you want to coordinate Claude. Type `/mcp` to inspect connected servers and confirm `claude_supervisor` is enabled and connected.

Ask ChatGPT:

> Use the claude_supervisor tools to call task_list and show my external Claude roster. Do not start an agent yet.

An empty roster is normal on a new installation. If the running chat still cannot discover the tools, save work and fully quit/reopen the desktop app when convenient, then reopen the chat. An app restart was required to activate this installation's running tool catalog. **Do not restart the app in the middle of a voice call you want to keep.**

Do not look for a new model-picker entry or native Claude subagent card: the tools and their external task IDs are the integration. A UI/client that lacks local MCP support cannot use this local registration just by opening ChatGPT web.

## 6. Run your first inexpensive task from chat

Create an existing, disposable workspace folder and print its path:

```powershell
$Demo = Join-Path $env:USERPROFILE 'claude-supervisor-demo'
New-Item -ItemType Directory -Path $Demo -Force | Out-Null
$Demo
```

Paste the following into ChatGPT, replacing `C:\Users\YourName\claude-supervisor-demo` with that printed path:

> Use claude_supervisor.task_start with prompt “Reply exactly SUPERVISOR_OK.”, workspace “C:\Users\YourName\claude-supervisor-demo”, model `claude-haiku-5-5`, effort `low`, tools `[]`, max_turns `1`, and max_budget_usd `0.03`. Keep the returned task_id. Poll task_wait with its latest cursor until it completes or fails, then show task_result, the actual model and available subscription/overage status. Finally call task_stop for that task. Do not substitute another model or billing route.

This is an actual Claude subscription inference request. An expected result is `SUPERVISOR_OK` with actual model `claude-haiku-5-5`. It has no coding tools, so the model cannot read or modify the demo folder. If the account cannot use that model or is rate-limited, handle the error rather than silently switching.

For normal coding, ask the coordinator to start a task in the **specific existing project folder you authorize**. The defaults are **Opus 5.5 / high effort**, 30 model turns and a `max_budget_usd` value of `1.0`. State the goal, files/actions in scope, test command and what requires your decision. The estimate cap may end a run early; it is not a paid-credit allowance.

## During normal use

Speak or type instructions such as “show Claude's status,” “inspect its last five events,” “answer that format question using our earlier decision,” or “interrupt Claude and focus on the failing test.” The coordinator uses these project-defined tools:

| Task | Tool and behavior |
| --- | --- |
| Find tasks | `task_list` lists the external roster; native agents are separate. |
| Check progress | `task_status` returns compact lifecycle, activity age and pending request IDs. |
| Keep supervising | `task_wait(task_id, after=cursor, timeout=15)` waits at most 20 seconds and returns compact status. Repeat while responsible for the task; it does not schedule future wake-ups. |
| Inspect activity | `task_events(task_id, after=cursor, limit=10)` retrieves details on demand. Maximum 20 events per call, 1000 retained per task, 16000 characters per stored payload. Follow `next_cursor`; `gap=true` means older events expired. |
| Inspect a pending request | `task_request(task_id, request_id)` returns the exact proposed tool input or clarification. Status alone deliberately omits large inputs. |
| Approve/deny once | `task_answer(task_id, request_id, allow=true/false, message=...)` resolves that exact permission request within your authorization. No persistent broad grant is created. |
| Answer a question | `task_answer(..., answers={"Exact question text":"Selected label or free text"})`. Supply each question's exact text. |
| Redirect | `task_followup(task_id, text=...)` interrupts and drains an active turn before sending the new instruction in the same session. |
| Interrupt | `task_interrupt` cancels the current turn and pending approvals, retaining the saved session. Old approval IDs become invalid. |
| Reconnect | `task_resume(task_id, prompt=...)` uses the exact saved session. It does not resurrect a previous approval. |
| Inspect output | `task_result` returns bounded final output/error and billing observations. Review the actual diff and test results independently. |
| Free a connection | `task_stop` disconnects that Claude process but retains task/session records for resume. |

States are `running`, `waiting_for_permission`, `waiting_for_answer`, `completed`, `failed`, `interrupted`. Four Claude sessions may be connected at once; stop completed sessions to free slots. Silence alone never establishes that a task is stuck. Pending approvals can wait indefinitely, so ask the coordinator to keep compact polling active; an idle/ended coordinator turn will not wake automatically.

Tool names, defaults, storage and the worker are supplied by **this repository**. Local MCP registration, `/mcp`, host permission controls and Claude's Agent SDK are supported platform facilities. The bridge does not provide native-agent parity or private reasoning access; thinking blocks are discarded.

## Subscription usage and billing boundaries

Claude Code owns authentication and token refresh. The worker clears inherited API/provider routing variables, ignores alternate settings sources, requires `claude.ai` / `firstParty` / `max`, pins exact model IDs, clears model fallback chains and disables fast mode. It does not generate API keys, export/copy account tokens, enable usage credits or fall back to API-key/provider billing. Included plan limits still apply.

**Keep account-level usage credits/overage disabled before running tasks.** Authentication status does not independently attest that setting before the first inference request. The worker observes SDK rate-limit events and stops if overage is reported enabled/in use, or included usage is rejected. Successful verification reported `isUsingOverage=false`, `overageStatus=rejected`, `overageDisabledReason=org_level_disabled`; check your own account rather than assuming its setting matches.

Despite its name, `max_budget_usd` is a **CLI list-price estimate cap**. Reported estimated dollars are not proof of an API charge and this parameter is not a complete account-level spending guard if someone later enables overage. There is no paid fallback configured to work around exhausted included limits.

File tools resolve paths within the authorized workspace and protect credential/configuration paths. Recursive directory `Grep` is refused; use `Glob` then `Read`/`Grep` specific files. Mutations and shell commands require scoped coordinator approval. An approved shell command runs as your Windows user: **this is not an OS sandbox for shell commands**. Do not approve commands that obtain credentials or operate outside your authorized scope.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| No `Settings > MCP servers` / no tools | Confirm a supported desktop/local host and workspace policy. This is not web/cloud-only registration. Check the server name, separate absolute command/argument paths, enabled state and `/mcp`. Refresh, or reopen the app after the voice call when appropriate. |
| `python` not found / `.venv` setup fails | Install Python 3.14 x64, reopen PowerShell 7, and verify `python --version`. A Microsoft Store alias is not a substitute for a working Python installation. |
| Clone says repository not found | Obtain access to the private repository and authenticate Git. This is separate from Claude login. |
| Authentication rejected | Use native `claude.exe`, sign in with `auth login --claudeai`, and verify Max/firstParty. This release rejects Pro/Team/Enterprise and Console/API routes. Never fix it by copying tokens. |
| Rate limit, model unavailable or estimate cap | Inspect `task_result` and status. Wait for included allowance to reset or explicitly choose a supported available model for a new task. Do not enable credits/API fallback. Resuming retains the task's original limits. |
| Bash/test commands fail | Install standard Git for Windows/Git Bash. This bridge's permitted shell tool is Bash, not Claude's alternative PowerShell tool. |
| Waiting for permission/answer | Call `task_request`, inspect the proposal, then answer its current ID. An interrupted or already answered ID cannot be reused. |
| Blank Python/Claude window | A current installation uses `pythonw.exe` for the worker and hides SDK children. First stop owned tasks and follow the worker restart procedure below. Rerun setup for the same folder; it updates a verified older `python.exe` launcher. Do not kill every Python/Claude process. |
| Worker fault / connection refused | Inspect `.runtime\daemon.log` privately. Check that Windows Task Scheduler and loopback access are permitted. Rerun `.\setup.ps1` from the same checkout; startup should print `Persistent worker is ready`. Do not share the entire `.runtime` directory. |
| Another installation / moved folder | Only one named launcher and `claude_supervisor` host entry are supported per Windows user. Setup refuses to overwrite another folder's entries. Remove the identified old registration/launcher using the steps below, then set up the chosen permanent folder. Do not rename or delete unrelated tasks. |
| Four sessions already connected | Use `task_list`, then `task_stop` on completed or unwanted sessions before starting/resuming another. |
| Restricted development sandbox hangs | The Windows asyncio/loopback tests were run with ordinary local access. A restricted sandbox used during development could hang even a bare asyncio sleep; do not assume the task is a model stall. |

## Stop, restart or uninstall safely

For one Claude task, ask the coordinator to call `task_stop` with its exact task ID. A saved task can later use `task_resume` with a new instruction.

To restart the whole worker, first stop any Claude tasks you do not want interrupted. From **this project's directory**:

```powershell
.\.venv\Scripts\python.exe server.py --shutdown-worker
```

This sends an authenticated shutdown request only to this installation's worker. The next connected MCP request can start it again, so disable the MCP entry temporarily if you need it to remain stopped. For maintenance, disable the entry, request shutdown, wait for its scheduled task to stop, rerun `.\setup.ps1` as needed, then enable/refresh the entry. Unfinished tasks become interrupted on worker restart; resume is explicit.

To uninstall:

1. Stop the external Claude tasks you own, then disable/remove **`claude_supervisor`** in app MCP settings. If you used the CLI, its equivalent is `codex mcp remove claude_supervisor` (or `& $CodexExe mcp remove claude_supervisor` with your actual executable path).
2. Run the worker shutdown command above. Do not terminate all Python or Claude processes.
3. Identify the exact launcher from this installation's metadata:

   ```powershell
   $Project = (Get-Location).Path
   $Launcher = Get-Content -LiteralPath (Join-Path $Project '.runtime\service-task.json') -Raw | ConvertFrom-Json
   Get-ScheduledTask -TaskName $Launcher.name | Select-Object TaskName, State, Actions
   ```

   In Windows **Task Scheduler**, remove only that named task **after checking its action points to this folder's `.venv\Scripts\pythonw.exe` and `daemon.py`**. Its name alone is not enough if the project moved or another installation exists.
4. Keep any private configuration backup you need, then remove the chosen project folder manually if desired. There is no recursive deletion command in this guide. Do not delete Claude Code's separate account/configuration/transcript folders as part of removing this bridge.

`.runtime` contains sensitive local transport material, task data and possibly configuration backups; `.venv`, fixtures, event logs and collaboration transcripts are all excluded from Git. Do not upload or commit those directories.

## Verification and development

From the project directory, these checks make **no model inference requests**:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp .test-tmp
.\.venv\Scripts\python.exe tests\live_mcp_check.py
```

The second command exercises real MCP discovery/reconnection and may start the local worker, but does not start a Claude inference task.

The following are separate **opt-in subscription inference tests** in disposable fixtures; do not run them just to install the bridge:

```powershell
.\.venv\Scripts\python.exe tests\live_mcp_check.py --infer
.\.venv\Scripts\python.exe tests\live_sdk_check.py
.\.venv\Scripts\python.exe tests\live_mcp_coding_check.py
```

The verified installation passed **25 automated tests**, real SDK question/approval/interrupt/exact-session-resume checks, MCP reconnect checks and a coding fixture whose test independently exited 0. The desktop coordinator subsequently discovered the actual MCP tools and used both Haiku 5.5 and Opus 5.5/high after an app restart. Opus also contributed the reviewed/tested event store. See [verification details](docs/verification.md).

This walkthrough was checked against the supplied scripts and tool signatures; a fresh-machine installation was **not** performed for the documentation update. It does not promise automatic wake-ups, native subagent UI, broader subscription support or continuous supervision after the coordinator stops polling.
