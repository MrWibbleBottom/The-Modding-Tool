#!/usr/bin/env python3
"""Self-healing supervisor for the local FastAPI + Uvicorn server on 127.0.0.1:8765."""

from pathlib import Path
import socket
import subprocess
import sys
import time

HOST = "127.0.0.1"
PORT = 8765
ROOT_DIR = Path(__file__).resolve().parent
POC_DIR = ROOT_DIR / "poc-ui"


def is_port_open(host: str = HOST, port: int = PORT, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main() -> int:
    if is_port_open(HOST, PORT):
        print(f"Server is already running on http://{HOST}:{PORT}")
        return 0

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "controller:app",
        "--app-dir",
        str(POC_DIR),
        "--host",
        HOST,
        "--port",
        str(PORT),
        "--reload",
        "--reload-dir",
        str(POC_DIR),
    ]

    proc = None
    try:
        while True:
            print(f"[supervisor] Starting Uvicorn on http://{HOST}:{PORT} ...")
            proc = subprocess.Popen(cmd, cwd=str(ROOT_DIR))
            rc = proc.wait()
            print(f"[supervisor] Uvicorn exited with code {rc}. Restarting in 1.5s...")
            time.sleep(1.5)
    except KeyboardInterrupt:
        print("\n[supervisor] Stopping server...")
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
