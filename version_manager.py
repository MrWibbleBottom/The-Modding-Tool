"""
Semantic Versioning (SemVer) Manager for ModToolPoC / Battleground.

Follows standard SemVer best practices:
- Pre-launch development: 0.MINOR.PATCH (e.g. 0.1.0, 0.1.1, 0.2.0)
- Full launch milestone:  1.0.0
- Post-launch releases:   MAJOR.MINOR.PATCH (e.g. 1.0.1, 1.1.0, 2.0.0)
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
VERSION_FILE = PROJECT_ROOT / "version.json"


def format_semver(major: int, minor: int, patch: int, prerelease: str = "") -> str:
    base = f"{major}.{minor}.{patch}"
    return f"{base}-{prerelease}" if prerelease else base


def load_version_data() -> dict[str, Any]:
    if VERSION_FILE.is_file():
        try:
            data = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "major" in data:
                return data
        except Exception:
            pass
    default = {
        "major": 0,
        "minor": 1,
        "patch": 0,
        "prerelease": "",
        "version": "0.1.0",
        "channel": "pre-launch",
        "launched": False,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "history": [
            {
                "version": "0.1.0",
                "bump": "init",
                "note": "Initial semantic version (pre-launch; 1.0.0 reserved for full launch)",
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            }
        ],
    }
    save_version_data(default)
    return default


def save_version_data(data: dict[str, Any]) -> None:
    VERSION_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def bump_version(bump_type: str = "patch", note: str = "Incremental project change") -> dict[str, Any]:
    data = load_version_data()
    major = int(data.get("major", 0))
    minor = int(data.get("minor", 1))
    patch = int(data.get("patch", 0))
    prerelease = str(data.get("prerelease", "") or "")
    launched = bool(data.get("launched", major >= 1))

    b = (bump_type or "patch").strip().lower()
    if b == "launch":
        if major < 1:
            major, minor, patch = 1, 0, 0
        else:
            patch += 1
        prerelease = ""
        launched = True
    elif b == "major":
        major += 1
        minor = 0
        patch = 0
        prerelease = ""
        launched = major >= 1
    elif b == "minor":
        minor += 1
        patch = 0
    elif b == "patch":
        patch += 1
    else:
        raise ValueError(f"Unsupported bump_type '{bump_type}'. Use patch, minor, major, or launch.")

    ver_str = format_semver(major, minor, patch, prerelease)
    now = datetime.now().isoformat(timespec="seconds")

    data["major"] = major
    data["minor"] = minor
    data["patch"] = patch
    data["prerelease"] = prerelease
    data["version"] = ver_str
    data["launched"] = launched
    data["channel"] = "stable" if launched else "pre-launch"
    data["updated_at"] = now

    history = data.get("history")
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "version": ver_str,
            "bump": b,
            "note": note,
            "timestamp": now,
        }
    )
    data["history"] = history[-100:]
    save_version_data(data)
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage SemVer version.json for Battleground.")
    parser.add_argument("--show", action="store_true", help="Print current version info.")
    parser.add_argument("--bump", choices=["patch", "minor", "major", "launch"], help="Advance version.")
    parser.add_argument("--note", type=str, default="Manual version bump", help="Change note.")
    args = parser.parse_args()

    if args.bump:
        res = bump_version(args.bump, args.note)
        print(f"Version advanced to {res['version']} ({res['channel']})")
        return

    print(json.dumps(load_version_data(), indent=2))


if __name__ == "__main__":
    main()
