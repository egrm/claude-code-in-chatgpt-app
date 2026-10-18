# Implementation checkpoint

This branch is unfinished and is not an installation release.

Implemented so far: stdlib setup entry point, venv/dependency automation, native Claude detection, safe MCP configuration editing, platform launcher adapters, doctor/remove commands and new regression tests. These still require review fixes and final integration validation.

In progress: client leases and bounded shutdown of the worker and active Claude tasks after the last client disconnects; stale approval cancellation and explicit resume; service ownership fixes; complete installer health checks; short cross-platform README and advanced reference.

Validation completed before this checkpoint: existing 25-test baseline; platform primitives on Windows and real Ubuntu WSL; registration preservation on both; mocked service adapter tests. This does not establish completed installer or app-close behavior. New lease tests are intentionally red until implementation lands. macOS is implemented in adapters but not tested on a Mac.

No desktop app or active voice session was restarted. The running development worker remains the prior implementation until a safe upgrade after coding. No Claude inference was run by the new installer tests.
