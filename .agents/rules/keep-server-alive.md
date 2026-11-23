---
trigger: always_on
description: Ensure the local FastAPI web service on 127.0.0.1:8765 remains running after any code edit.
---
# Keep Local Web Service Available
Before finishing any turn where files in this workspace were modified or inspected:
1. Check `http://127.0.0.1:8765/api/health` (e.g. via Python `urllib.request` or `curl`).
2. If the port is not responding, start the server in the background using `run_server.py` (or `start_app.bat`) and re-verify that `/api/health` returns `200 OK` before responding to the user.
