# Verification — 2026-10-18

## Cross-platform installer and lifecycle (this release)

- Windows full suite: 102 passed, 3 skipped (POSIX-only), including real worker/facade process fixtures: abrupt client loss, startup without a client, graceful facade close, cancellation-resistant close (worker and owned child end within bound, task persisted `interrupted`).
- Ubuntu full suite (independent run): 102 passed, 3 skipped, including external SIGTERM cleanup, draining reconnect/lock wait and registry shutdown races.
- Windows real isolated install: fresh install; repeated install left config bytes and Task Scheduler XML exactly unchanged; `doctor` discovered 12 tools over real MCP; worker exited after the last client and its task returned to Ready; `remove` restored config exactly, deleted the task and kept task history.
- Ubuntu real `systemd --user` fixture with spaces in the path: registered, started protocol-2 worker, MCP discovery 12 tools, exit and cleanup after the last client. Fresh bootstrap without ensurepip recovered via the system pip `--python` fallback; with native Claude absent it exited 2 with guidance, leaving config unchanged and registering no service.
- Not performed: Linux desktop app, Linux Claude auth or inference, any macOS run (launchd adapter is mock-tested only). No app close/restart of the user's main installation.

## Earlier Windows release (still applicable)

- Official SDK 0.2.168 with native Claude Code 2.1.302: question/approval/interrupt/exact-session resume; Max claude.ai/firstParty, `isUsingOverage=false`. MCP stdio protocol 2025-11-25 discovered all 12 tools; coding fixture test exited 0. After an app restart the coordinator used Haiku 5.5 and Opus 5.5/high.
- Raw logs, auth metadata, transport capabilities and config backups remain outside publication.
