---
trigger: always_on
description: Keep 127.0.0.1:8765 alive after code edits without spawning subagents or duplicate tasks.
---
Only run if you modified `.py`, `.html`, `.css`, or `.js` files in this turn:
- Check `http://127.0.0.1:8765/api/health` using a single Python `urllib.request` one-liner (2s timeout; use `BypassSandbox: true` on Windows if sandbox loopback is blocked). Never spawn a browser subagent.
- Only if that check fails, start `.\.venv\Scripts\python.exe run_server.py` via `run_command` with `IsDaemon: true`, `BypassSandbox: true`, and `WaitMsBeforeAsync: 500` (do not use `Start-Process` in a non-daemon command, because Windows Job Object cleanup terminates child processes on command exit), then re-check `/api/health` once after 2s.
