"""
Incremental Project Backup Manager & File Watcher for Battleground.

- Creates a full baseline backup (v001_YYYYMMDD_HHMMSS_baseline) in ./backups/
- Watches the project tree for file additions, modifications, and deletions
- On each change (debounced), creates a new incremental backup folder
  (v002_YYYYMMDD_HHMMSS_incremental, ...) containing:
    - files/         : only the added or modified files for this version
    - manifest.json  : complete file-to-version map + hashes so any version
                       can be restored completely
    - SUMMARY.md     : human-readable summary of changed/added/deleted files
                       and unified text diffs
    - restore.ps1    : self-contained PowerShell script to restore this version
    - env_info.json  : Python/dotnet/runtime metadata needed to resume
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
BACKUPS_DIR = PROJECT_ROOT / "backups"
STATE_FILE = BACKUPS_DIR / "backup_index.json"

EXCLUDED_DIR_NAMES = {
    "backups",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".git",
    "bin",
    "obj",
    "node_modules",
    ".vs",
    "dist",
}

EXCLUDED_FILE_NAMES = {
    "Thumbs.db",
    "desktop.ini",
    "version.json",
}

TEXT_EXTENSIONS = {
    ".py", ".html", ".htm", ".css", ".js", ".ts", ".json", ".md", ".txt",
    ".csv", ".ps1", ".bat", ".cmd", ".sh", ".cs", ".csproj", ".sln",
    ".toml", ".yaml", ".yml", ".ini", ".cfg", ".xml", ".gitignore",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def is_text_file(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTENSIONS or path.name in {"LICENSE", "README"}


def scan_project_files() -> dict[str, dict[str, Any]]:
    """Return {rel_posix_path: {size, mtime_ns, sha256}} for all tracked project files."""
    files: dict[str, dict[str, Any]] = {}
    for root, dirs, filenames in os.walk(PROJECT_ROOT):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIR_NAMES)
        root_path = Path(root)
        for fname in sorted(filenames):
            if fname in EXCLUDED_FILE_NAMES:
                continue
            fpath = root_path / fname
            if not fpath.is_file():
                continue
            rel = fpath.relative_to(PROJECT_ROOT).as_posix()
            try:
                st = fpath.stat()
                files[rel] = {
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                    "sha256": sha256_file(fpath),
                }
            except OSError:
                continue
    return files


def load_index() -> dict[str, Any]:
    if STATE_FILE.is_file():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"versions": []}


def save_index(index: dict[str, Any]) -> None:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(index, indent=2), encoding="utf-8")


def collect_env_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "python_executable": sys.executable,
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
        "project_root": str(PROJECT_ROOT),
        "resume_commands": {
            "setup_venv": "python -m venv bepinex-mcp\\.venv && .\\bepinex-mcp\\.venv\\Scripts\\pip install -r poc-ui\\requirements.txt",
            "start_server": ".\\bepinex-mcp\\.venv\\Scripts\\python.exe -m uvicorn controller:app --app-dir poc-ui --host 127.0.0.1 --port 8765",
            "start_watcher": ".\\bepinex-mcp\\.venv\\Scripts\\python.exe backup_manager.py --watch",
        },
    }
    try:
        res = subprocess.run(["dotnet", "--version"], capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            info["dotnet_version"] = res.stdout.strip()
    except Exception:
        pass

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        mg = Path(local_appdata) / "ModToolPoC" / "managed_games.json"
        if mg.is_file():
            info["managed_games_source"] = str(mg)
    return info


def build_diff_preview(old_file: Path | None, new_file: Path | None, rel_path: str) -> str:
    if old_file and new_file and is_text_file(new_file):
        try:
            old_lines = old_file.read_text(encoding="utf-8", errors="replace").splitlines()
            new_lines = new_file.read_text(encoding="utf-8", errors="replace").splitlines()
            diff = list(
                difflib.unified_diff(
                    old_lines,
                    new_lines,
                    fromfile=f"a/{rel_path}",
                    tofile=f"b/{rel_path}",
                    lineterm="",
                    n=2,
                )
            )
            if not diff:
                return ""
            max_lines = 40
            snippet = "\n".join(diff[:max_lines])
            if len(diff) > max_lines:
                snippet += f"\n... ({len(diff) - max_lines} more diff lines)"
            return f"```diff\n{snippet}\n```"
        except Exception:
            return ""
    return ""


def write_restore_script(version_dir: Path, version_id: str) -> None:
    script = f"""param(
    [string]$TargetDir = "{str(PROJECT_ROOT)}"
)

$ErrorActionPreference = "Stop"
$PythonExe = "{sys.executable}"
if (-not (Test-Path $PythonExe)) {{
    $PythonExe = "python"
}}

& $PythonExe "{str(PROJECT_ROOT / 'backup_manager.py')}" --restore {version_id} --target "$TargetDir"
"""
    (version_dir / "restore.ps1").write_text(script, encoding="utf-8")


def snapshot_appdata(version_dir: Path) -> None:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return
    mg = Path(local_appdata) / "ModToolPoC" / "managed_games.json"
    if mg.is_file():
        dest = version_dir / "_appdata" / "managed_games.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(mg, dest)


def create_backup(reason: str = "manual", force_baseline: bool = False) -> dict[str, Any] | None:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    index = load_index()
    versions: list[dict[str, Any]] = index.get("versions", [])

    current_files = scan_project_files()
    prev_version = versions[-1] if versions else None
    prev_manifest_files: dict[str, dict[str, Any]] = {}
    if prev_version and not force_baseline:
        prev_manifest_path = BACKUPS_DIR / prev_version["folder"] / "manifest.json"
        if prev_manifest_path.is_file():
            prev_manifest = json.loads(prev_manifest_path.read_text(encoding="utf-8"))
            prev_manifest_files = prev_manifest.get("files", {})

    is_baseline = force_baseline or (prev_version is None)
    added: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []

    if is_baseline:
        added = sorted(current_files.keys())
    else:
        for rel, meta in current_files.items():
            if rel not in prev_manifest_files:
                added.append(rel)
            elif prev_manifest_files[rel].get("sha256") != meta["sha256"]:
                modified.append(rel)
        for rel in prev_manifest_files:
            if rel not in current_files:
                deleted.append(rel)
        added.sort()
        modified.sort()
        deleted.sort()

    if not is_baseline and not added and not modified and not deleted:
        return None

    next_num = len(versions) + 1
    version_id = f"v{next_num:03d}"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    kind = "baseline" if is_baseline else "incremental"
    folder_name = f"{version_id}_{stamp}_{kind}"
    version_dir = BACKUPS_DIR / folder_name
    files_dir = version_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    changed_set = set(added) | set(modified)
    new_manifest_files: dict[str, dict[str, Any]] = {}

    diff_sections: list[str] = []
    for rel, meta in sorted(current_files.items()):
        if is_baseline or rel in changed_set:
            src = PROJECT_ROOT / rel
            dst = files_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            new_manifest_files[rel] = {
                "sha256": meta["sha256"],
                "size": meta["size"],
                "stored_in": folder_name,
            }
            if not is_baseline and rel in modified:
                old_holder = prev_manifest_files[rel]["stored_in"]
                old_file = BACKUPS_DIR / old_holder / "files" / rel
                diff_md = build_diff_preview(old_file if old_file.is_file() else None, dst, rel)
                if diff_md:
                    diff_sections.append(f"### Modified: `{rel}`\n\n{diff_md}")
        else:
            prev_entry = prev_manifest_files[rel]
            new_manifest_files[rel] = {
                "sha256": prev_entry["sha256"],
                "size": prev_entry["size"],
                "stored_in": prev_entry["stored_in"],
            }

    env_info = collect_env_info()
    (version_dir / "env_info.json").write_text(json.dumps(env_info, indent=2), encoding="utf-8")
    snapshot_appdata(version_dir)
    write_restore_script(version_dir, version_id)

    # Advance semantic version on each incremental change (or record current version on baseline)
    app_version_str = "0.1.0"
    try:
        import version_manager
        if is_baseline:
            app_version_str = version_manager.load_version_data().get("version", "0.1.0")
        else:
            changed_preview = ", ".join((added + modified + deleted)[:5])
            bumped = version_manager.bump_version(
                "patch",
                note=f"{reason} (+{len(added)} ~{len(modified)} -{len(deleted)}: {changed_preview})",
            )
            app_version_str = bumped.get("version", "0.1.0")
    except Exception:
        pass

    manifest = {
        "version_id": version_id,
        "app_version": app_version_str,
        "folder": folder_name,
        "kind": kind,
        "parent_version": prev_version["version_id"] if (prev_version and not is_baseline) else None,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "reason": reason,
        "stats": {
            "total_files": len(new_manifest_files),
            "added": len(added),
            "modified": len(modified),
            "deleted": len(deleted),
            "stored_in_this_folder": len(changed_set),
        },
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "files": new_manifest_files,
    }
    (version_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    summary_lines = [
        f"# Backup {version_id} — App v{app_version_str} ({kind.title()})",
        "",
        f"- **App Version**: `v{app_version_str}`",
        f"- **Created**: {manifest['created_at']}",
        f"- **Folder**: `{folder_name}`",
        f"- **Parent Version**: `{manifest['parent_version'] or 'None (Baseline)'}`",
        f"- **Trigger**: {reason}",
        f"- **Tracked Files at This Version**: {len(new_manifest_files)}",
        f"- **Changes**: +{len(added)} added, ~{len(modified)} modified, -{len(deleted)} deleted",
        "",
        "## Resume / Restore Instructions",
        "",
        "To restore the project to this exact version:",
        "```powershell",
        f"python backup_manager.py --restore {version_id}",
        "```",
        f"Or run `restore.ps1` inside `{folder_name}`.",
        "",
        "To run the app after restoring:",
        "```powershell",
        env_info["resume_commands"]["start_server"],
        "```",
        "",
    ]
    if added:
        summary_lines.append("## Added Files")
        summary_lines.extend(f"- `{p}`" for p in added)
        summary_lines.append("")
    if modified:
        summary_lines.append("## Modified Files")
        summary_lines.extend(f"- `{p}`" for p in modified)
        summary_lines.append("")
    if deleted:
        summary_lines.append("## Deleted Files")
        summary_lines.extend(f"- `{p}`" for p in deleted)
        summary_lines.append("")
    if diff_sections:
        summary_lines.append("## Change Diffs")
        summary_lines.append("")
        summary_lines.extend(diff_sections)

    (version_dir / "SUMMARY.md").write_text("\n".join(summary_lines), encoding="utf-8")

    versions.append(
        {
            "version_id": version_id,
            "app_version": app_version_str,
            "folder": folder_name,
            "kind": kind,
            "parent_version": manifest["parent_version"],
            "created_at": manifest["created_at"],
            "reason": reason,
            "stats": manifest["stats"],
        }
    )
    index["versions"] = versions
    save_index(index)
    print(
        f"[backup_manager] Created {version_id} ({kind}) -> {version_dir} "
        f"[+{len(added)} ~{len(modified)} -{len(deleted)}]",
        flush=True,
    )
    return manifest


def restore_version(version_id: str, target_dir: Path) -> None:
    index = load_index()
    versions = index.get("versions", [])
    match = next((v for v in versions if v["version_id"] == version_id or v["folder"] == version_id), None)
    if not match:
        raise SystemExit(f"Backup version '{version_id}' not found in {STATE_FILE}")

    manifest_path = BACKUPS_DIR / match["folder"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    target_files: dict[str, dict[str, Any]] = manifest.get("files", {})

    target_dir = target_dir.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    # Remove tracked files in target_dir that do not exist in target_files (when restoring in-place)
    for root, dirs, filenames in os.walk(target_dir):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIR_NAMES]
        root_path = Path(root)
        for fname in filenames:
            if fname in EXCLUDED_FILE_NAMES:
                continue
            fpath = root_path / fname
            rel = fpath.relative_to(target_dir).as_posix()
            if rel not in target_files:
                fpath.unlink()

    restored_count = 0
    for rel, meta in sorted(target_files.items()):
        holder_folder = meta["stored_in"]
        src = BACKUPS_DIR / holder_folder / "files" / rel
        if not src.is_file():
            raise FileNotFoundError(f"Missing backup object: {src}")
        dst = target_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        restored_count += 1

    appdata_mg = BACKUPS_DIR / match["folder"] / "_appdata" / "managed_games.json"
    if appdata_mg.is_file():
        if target_dir == PROJECT_ROOT.resolve() and os.environ.get("LOCALAPPDATA"):
            dest_mg = Path(os.environ["LOCALAPPDATA"]) / "ModToolPoC" / "managed_games.json"
        else:
            dest_mg = target_dir / "_appdata" / "managed_games.json"
        dest_mg.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(appdata_mg, dest_mg)

    print(f"[backup_manager] Restored {match['version_id']} ({restored_count} files) -> {target_dir}")


def quick_fingerprint() -> dict[str, tuple[int, int]]:
    snap: dict[str, tuple[int, int]] = {}
    for root, dirs, filenames in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIR_NAMES]
        root_path = Path(root)
        for fname in filenames:
            if fname in EXCLUDED_FILE_NAMES:
                continue
            fpath = root_path / fname
            try:
                st = fpath.stat()
                snap[fpath.relative_to(PROJECT_ROOT).as_posix()] = (st.st_size, st.st_mtime_ns)
            except OSError:
                continue
    return snap


def watch_loop(poll_sec: float = 1.5, debounce_sec: float = 2.0) -> None:
    if not load_index().get("versions"):
        create_backup(reason="initial baseline (auto-watcher startup)", force_baseline=True)

    print(f"[backup_manager] Watching {PROJECT_ROOT} for changes (poll={poll_sec}s, debounce={debounce_sec}s)...", flush=True)
    last_snap = quick_fingerprint()
    pending_since: float | None = None

    while True:
        time.sleep(poll_sec)
        curr_snap = quick_fingerprint()
        if curr_snap != last_snap:
            last_snap = curr_snap
            pending_since = time.monotonic()
        elif pending_since is not None and (time.monotonic() - pending_since) >= debounce_sec:
            pending_since = None
            create_backup(reason="automatic incremental backup on file change")
            last_snap = quick_fingerprint()


def main() -> None:
    parser = argparse.ArgumentParser(description="Incremental backup manager for Battleground.")
    parser.add_argument("--baseline", action="store_true", help="Create a new full baseline backup.")
    parser.add_argument("--snapshot", action="store_true", help="Create an incremental backup if changes exist.")
    parser.add_argument("--reason", type=str, default="manual snapshot", help="Reason/summary note for the backup.")
    parser.add_argument("--watch", action="store_true", help="Watch project directory and auto-create incremental backups.")
    parser.add_argument("--restore", type=str, help="Restore project to a specific version ID (e.g. v001).")
    parser.add_argument("--target", type=str, default=str(PROJECT_ROOT), help="Target directory for --restore.")
    parser.add_argument("--list", action="store_true", help="List all recorded backup versions.")
    args = parser.parse_args()

    if args.list:
        idx = load_index()
        print(json.dumps(idx, indent=2))
        return
    if args.restore:
        restore_version(args.restore, Path(args.target))
        return
    if args.baseline:
        create_backup(reason=args.reason, force_baseline=True)
        return
    if args.snapshot:
        res = create_backup(reason=args.reason, force_baseline=False)
        if res is None:
            print("[backup_manager] No changes detected since last backup.")
        return
    if args.watch:
        watch_loop()
        return

    parser.print_help()


if __name__ == "__main__":
    main()
