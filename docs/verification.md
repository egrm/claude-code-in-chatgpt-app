# Verification — 2026-10-18

- 25 automated tests passed at the standalone installation. Tests cover event retention/cursors/truncation/persistence, subscription-route guards, credential/search path boundaries, task lifecycle, permission/question round trips, stale answers, silence versus failure, disconnect cleanup, capacity during resume, authenticated RPC, compact polling, Windows child process containment/visibility, and configuration preservation.
- Official SDK 0.2.168 around native Claude Code 2.1.302: Haiku 5.5 asked the expected clarification, paused before a write until approval, returned tool results, and completed. A second pending write was interrupted and remained absent; stale approval was refused. Disconnect/reconnect resumed the exact same session and retained the prior question/answer context.
- Actual MCP stdio client negotiated protocol 2025-11-25 and discovered all 12 advertised tools. A task survived closing the first MCP client and reconnecting a second, then completed with the expected fixed response. A subsequent reconnect-only check used no inference.
- Actual coding task through MCP: Haiku read a disposable arithmetic fixture, received approval only for its exact edit and `python check.py`, fixed it, and exposed the shell tool result. The coordinator independently ran the fixture test: exit code 0, stdout `FIXTURE_TEST_PASS`.
- Subscription evidence reported Max claude.ai / firstParty, actual requested model, `isUsingOverage=false`, `overageStatus=rejected`, `overageDisabledReason=org_level_disabled`. No API-key/provider fallback was configured. CLI list-price estimates are not charges.
- Opus 5.5/high supplied the event-store implementation as a bounded tool-free coding collaborator. Its actual model metadata and streaming arrival were verified; its output was reviewed and passed behavioral tests. Collaboration transcripts remain local and are excluded from Git.
- Windows regression: identified the dedicated idle worker's console with title `claude`. Switched only its verified launcher to pythonw and added hidden subprocess policy. Replacement worker had no attached console; synchronous and asyncio child-console tests passed. No unrelated windows or active voice session were closed.
- Host registration: `claude_supervisor`, project-local Python interpreter plus `server.py`, 30-second startup timeout and 60-second tool timeout. Existing config backed up privately; every unrelated parsed setting matched the original afterward. Original configuration text was preserved when repairing native CLI normalization of an unrelated empty args array.
- A fresh read-only code review found three issues (recursive search access, dead connection slots, resume admission). Each received a failing regression test and fix; follow-up review found no important remaining issue in those changes.

## Limits still requiring an app-level check

The current voice call was never restarted or ended. The bridge is registered and independently tested through real MCP, but the running coordinator's tool catalog may require refreshing that MCP server. Automatic wake-up is not implemented: keep compact `task_wait` polling active while supervising. No native-agent roster integration or private reasoning access is claimed.

Keep account usage credits disabled. Authentication preflight establishes the subscription route, while overage settings are observed from SDK rate-limit events rather than independently attested before the first model request.

Raw event logs, authentication metadata, runtime transport capabilities, fixture code and configuration backups remain outside publication.
