# Claude Supervisor Implementation Plan

Goal: implement the approved external supervised roster within the current desktop app using supported local MCP registration. The standalone behavior and constraints are specified in ../README.md; validation is recorded in verification.md.

Architecture: Python official ClaudeSDKClient, SQLite task/event storage, async stdio MCP facade and a shared authenticated loopback worker. A current-user on-demand scheduler action starts windowless Python outside the facade's Windows cleanup job. Sessions survive MCP reconnects; worker restart recovers durable unfinished tasks as interrupted and permits exact-session resume. No binary patching or voice restart. Existing Claude login owns auth. Defaults: Opus 5.5/high for future coding; explicit Haiku 5.5 integration verification.

Constraints: no credential reading/copying, no API/provider fallback, no overage usage, explicit per-request approval, workspace path confinement for file tools; bounded events and compact polls. Claude is an external roster rather than native agents. Do not continuously inject events.

Review focus: stale permission answers after interrupt; event cursor gaps; unexpected process exit versus silence; environment/config billing overrides; persistence after MCP restart.

1. Write failing behavioral tests for bounded cursor event store, truncation and durable recovery. Ask authorized Opus 5.5/high collaborator to implement event store with tools disabled; inspect streaming metadata, review response and integrate after test failure. Run suite.
2. Write failing tests for registry lifecycle, question/permission roundtrip, stale replies and billing environment confinement. Implement registry with backend seam and SDK adapter. Live SDK covers external behavior doubles cannot establish.
3. Expose list/status/start/followup/events/wait/answer/interrupt/resume/result as MCP tools with concise descriptions and server instructions. Test real MCP transport and tool schemas.
4. Run bounded Haiku end-to-end in disposable fixture: question/approval, read/write and test result, followup/context continuity, interrupt/redirect and saved-session resume. Save compact evidence; no expanded workload.
5. Review integration and security boundaries. Back up current user config without printing contents; register only dedicated claude_supervisor MCP table through supported config. Verify preservation and server startup. Do not end/restart voice. Report reload if current tool catalog cannot refresh.

Execution: inline under existing explicit authorization. No repository metadata exists at workspace root; no commits/worktree are required. Findings and evidence remain independent from other research.
