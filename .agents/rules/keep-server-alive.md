---
trigger: always_on
description: Keep 127.0.0.1:8765 alive after code edits without spawning subagents or duplicate tasks.
---
Only run if you modified `.py`, `.html`, `.css`, or `.js` files in this turn:
- Check `http://127.0.0.1:8765/api/health` using a single Python `urllib.request` one-liner (2s timeout). Never spawn a browser subagent.
- Only if that check fails, launch `run_server.py` detached via `powershell -Command "Start-Process -WindowStyle Hidden -FilePath '.\.venv\Scripts\python.exe' -ArgumentList 'run_server.py'"` and re-check once after 2s.
