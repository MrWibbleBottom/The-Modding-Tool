"""
Semantic Versioning (SemVer) Manager for The Modding Tool.

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
CHANGELOG_FILE = PROJECT_ROOT / "CHANGELOG.md"


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


def append_changelog_entry(
    prompt: str,
    feedback: str,
    changes: list[str] | str,
    *,
    version: str | None = None,
    timestamp: str | None = None,
    changelog_path: Path | None = None,
) -> Path:
    """Prepend a dated, versioned entry recording Prompt, AG Feedback, and Version Changes to CHANGELOG.md."""
    target = changelog_path or CHANGELOG_FILE
    ver = (version or load_version_data().get("version") or "0.1.0").strip().lstrip("v")
    stamp = (timestamp or datetime.now().strftime("%Y-%m-%d %H:%M")).strip()

    if isinstance(changes, str):
        change_lines = [ln.strip() for ln in changes.splitlines() if ln.strip()]
    else:
        change_lines = [str(item).strip() for item in changes if str(item).strip()]
    if not change_lines:
        change_lines = ["No code or file changes recorded."]

    bullets = "\n".join(
        line if line.startswith("- ") else f"- {line}"
        for line in change_lines
    )
    entry = (
        f"## {stamp} — v{ver}\n\n"
        f"- **Prompt**: {str(prompt or '').strip()}\n"
        f"- **AG Feedback**: {str(feedback or '').strip()}\n"
        f"- **Version Changes**:\n"
        + "\n".join(f"  {b}" for b in bullets.splitlines())
        + "\n"
    )

    header = (
        "# Changelog\n\n"
        "Chronological record of user prompts, AG feedback, and version changes "
        "(filed newest-first with date stamp followed by version number).\n\n"
    )
    if target.is_file():
        existing = target.read_text(encoding="utf-8")
        marker = "\n## "
        idx = existing.find(marker)
        if idx != -1:
            body = existing[: idx + 1] + entry + "\n" + existing[idx + 1 :]
        elif existing.startswith("## "):
            body = header + entry + "\n" + existing
        else:
            body = existing.rstrip() + "\n\n" + entry
    else:
        body = header + entry

    target.write_text(body, encoding="utf-8", newline="\n")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage SemVer version.json and CHANGELOG.md for The Modding Tool.")
    parser.add_argument("--show", action="store_true", help="Print current version info.")
    parser.add_argument("--bump", choices=["patch", "minor", "major", "launch"], help="Advance version.")
    parser.add_argument("--note", type=str, default="Manual version bump", help="Change note.")
    parser.add_argument("--changelog", action="store_true", help="Append an entry to CHANGELOG.md.")
    parser.add_argument("--prompt", type=str, default="", help="User prompt text for CHANGELOG.md.")
    parser.add_argument("--feedback", type=str, default="", help="AG feedback summary for CHANGELOG.md.")
    parser.add_argument("--changes", nargs="*", default=[], help="Version change items for CHANGELOG.md.")
    args = parser.parse_args()

    if args.bump:
        res = bump_version(args.bump, args.note)
        print(f"Version advanced to {res['version']} ({res['channel']})")
        if args.changelog:
            append_changelog_entry(
                prompt=args.prompt or args.note,
                feedback=args.feedback or f"Version bumped to v{res['version']} ({res['channel']}).",
                changes=args.changes or [args.note],
                version=res["version"],
            )
        return

    if args.changelog:
        out = append_changelog_entry(
            prompt=args.prompt or args.note,
            feedback=args.feedback or "Logged entry.",
            changes=args.changes or [args.note],
        )
        print(f"Updated {out}")
        return

    print(json.dumps(load_version_data(), indent=2))


if __name__ == "__main__":
    main()
