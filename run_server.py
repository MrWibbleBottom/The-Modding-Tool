#!/usr/bin/env python3
"""Self-healing supervisor for the local FastAPI + Uvicorn server on 127.0.0.1:8765."""

import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser

HOST = "127.0.0.1"
PORT = 8765
URL = f"http://{HOST}:{PORT}"
HEALTH_URL = f"{URL}/api/health"
ROOT_DIR = Path(__file__).resolve().parent
APP_DIR = ROOT_DIR / "app"
VENV_PYTHON = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
SHUTDOWN_SIGNAL_FILE = Path(tempfile.gettempdir()) / "themoddingtool_shutdown.flag"


def is_port_open(host: str = HOST, port: int = PORT, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_and_open_browser(health_url: str = HEALTH_URL, target_url: str = URL, timeout_sec: float = 20.0) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            with opener.open(health_url, timeout=1.5) as resp:
                if resp.status == 200:
                    webbrowser.open(target_url)
                    return
        except Exception:
            pass
        time.sleep(0.3)


def _terminate_process(proc: subprocess.Popen[bytes] | subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def main() -> int:
    parser = argparse.ArgumentParser(description="Start The Modding Tool supervised server.")
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open http://127.0.0.1:8765 in the default browser once /api/health responds 200 OK.",
    )
    args = parser.parse_args()

    if is_port_open(HOST, PORT):
        print(f"Server is already running on {URL}", flush=True)
        if args.open_browser:
            webbrowser.open(URL)
        return 0

    try:
        SHUTDOWN_SIGNAL_FILE.unlink(missing_ok=True)
    except OSError:
        pass

    if args.open_browser:
        threading.Thread(target=wait_and_open_browser, daemon=True).start()

    python_exe = str(VENV_PYTHON) if VENV_PYTHON.is_file() else sys.executable
    cmd = [
        python_exe,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        HOST,
        "--port",
        str(PORT),
        "--reload",
        "--reload-dir",
        str(APP_DIR),
    ]

    env = os.environ.copy()
    env["MODTOOL_SHUTDOWN_SIGNAL_FILE"] = str(SHUTDOWN_SIGNAL_FILE)

    popen_kwargs: dict[str, object] = {"cwd": str(ROOT_DIR), "env": env}
    if sys.platform == "win32" and Path(sys.executable).name.lower() == "pythonw.exe":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    proc = None
    try:
        while True:
            print(f"[supervisor] Starting Uvicorn on {URL} ...", flush=True)
            proc = subprocess.Popen(cmd, **popen_kwargs)
            while proc.poll() is None:
                if SHUTDOWN_SIGNAL_FILE.is_file():
                    print("[supervisor] Shutdown signal received. Stopping Uvicorn...", flush=True)
                    _terminate_process(proc)
                    try:
                        SHUTDOWN_SIGNAL_FILE.unlink(missing_ok=True)
                    except OSError:
                        pass
                    return 0
                time.sleep(0.25)

            if SHUTDOWN_SIGNAL_FILE.is_file():
                try:
                    SHUTDOWN_SIGNAL_FILE.unlink(missing_ok=True)
                except OSError:
                    pass
                print("[supervisor] Shutdown signal received. Exiting cleanly.", flush=True)
                return 0

            rc = proc.returncode
            print(f"[supervisor] Uvicorn exited with code {rc}. Restarting in 1.5s...", flush=True)
            time.sleep(1.5)
    except KeyboardInterrupt:
        print("\n[supervisor] Stopping server...", flush=True)
        if proc:
            _terminate_process(proc)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
