---
trigger: always_on
description: Keep 127.0.0.1:8765 alive after code edits without spawning subagents or duplicate tasks.
---
# Keep Local Web Service Available
Only run this check if you **modified or created** `.py`, `.html`, `.css`, or `.js` files in this turn (skip if you only read files or an edit was canceled):
1. Run a single shell one-liner using Python `urllib.request` to check `http://127.0.0.1:8765/api/health` (timeout 2s). **Never** launch a browser subagent (`UI Browser Verifier`) for this check.
2. **Only if step 1 fails**, start the server as a detached Windows process so it does not block or pile up in Background Tasks:
   `powershell -Command "Start-Process -WindowStyle Hidden -FilePath '.\.venv\Scripts\python.exe' -ArgumentList 'run_server.py'"`
3. Wait 2 seconds and re-run the `urllib.request` check once to confirm HTTP `200`.