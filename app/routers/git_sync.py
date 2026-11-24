"""GitHub update check, pull/sync, local snapshots, backups, portable export, and git status/push."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import zipfile
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.core.audit import append_audit_log, clear_audit_log_cache, read_audit_logs
from app.core.config import (
    ANTI_CHEAT_BINARIES,
    APP_DATA_ROOT,
    APP_SNAPSHOTS_DIR,
    APP_UI_CONFIG_PATH,
    ASSET_AUDITS_DIR,
    AUDIT_APP_LOG_PATH,
    AUDIT_GAME_LOG_PATH,
    AUDIT_LOG_PATH,
    BACKUP_ROOT,
    BRANDING_BANNERS_DIR,
    BRANDING_DIR,
    BRANDING_FAVICONS_DIR,
    BUILT_MODS_DIR,
    DEFAULT_BEPINEX_SHA256,
    DEFAULT_BEPINEX_ZIP,
    DEFAULT_GAME_DIR,
    DEFAULT_MCP_APP,
    DEFAULT_PLUGIN_BUILD_DIR,
    DEFAULT_VENV_PYTHON,
    DEV_MODE_PATH,
    GAME_ICONS_DIR,
    GAMES_RESPONSE_CACHE,
    GAMES_STORE_PATH,
    ICON_EXTRACT_SEMAPHORE,
    LIBRARY_CACHE_PATH,
    MANIFEST_DIR,
    PC_SPECS_CACHE_PATH,
    PLUGIN_DEPLOY_FILES,
    POC_DIR,
    REPO_CONFIG_PATH,
    RESOURCE_ROOT,
    SCAN_EXCLUSIONS_PATH,
    SCAN_PROGRESS,
    STEAM_ASSETS_CACHE,
    STEAM_ASSETS_CACHE_PATH,
    TOOL_VERSIONS_PATH,
    TOOLS_ROOT,
    UI_SETTINGS_PATH,
    UNREAL_INI_BACKUP_DIR,
    WORKSPACE_DIR,
    backup_dir_for_game,
    clear_pc_specs_cache,
    default_managed_games,
    get_app_data_root,
    invalidate_games_cache,
    load_managed_games,
    manifest_path_for_game,
    save_managed_games,
    sha256_file,
    sha256_text,
)


from app.core.security import dpapi_decrypt, dpapi_encrypt

router = APIRouter()

def _load_workspace_module(module_name: str, filename: str):
    import importlib.util

    mod_path = WORKSPACE_DIR / filename
    if not mod_path.is_file():
        raise HTTPException(status_code=404, detail=f"Required module not found: {mod_path}")
    spec = importlib.util.spec_from_file_location(module_name, str(mod_path))
    if spec is None or spec.loader is None:
        raise HTTPException(status_code=500, detail=f"Failed to load module spec: {mod_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sha256_file_local(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@router.get("/api/app/backups")
async def list_app_backups() -> dict[str, Any]:
    """Lists all incremental and baseline app backups."""
    bm = _load_workspace_module("backup_manager", "backup_manager.py")
    idx = bm.load_index()
    versions = list(reversed(idx.get("versions", [])))
    for v in versions:
        summary_path = bm.BACKUPS_DIR / v["folder"] / "SUMMARY.md"
        if summary_path.is_file():
            v["summary_preview"] = summary_path.read_text(encoding="utf-8")[:1500]
    return {
        "status": "ok",
        "backups_dir": str(bm.BACKUPS_DIR),
        "versions": versions,
        "snapshots": _list_app_snapshots(),
    }


class SaveAppBackupRequest(BaseModel):
    reason: str = Field(default="Manual save from App Management")
    force_baseline: bool = Field(default=False)


@router.post("/api/app/backup/save")
async def save_app_backup(req: SaveAppBackupRequest) -> dict[str, Any]:
    """Saves an incremental (or baseline) snapshot of the app."""
    bm = _load_workspace_module("backup_manager", "backup_manager.py")
    manifest = bm.create_backup(reason=req.reason.strip() or "Manual save", force_baseline=req.force_baseline)
    if manifest is None:
        return {
            "status": "no_changes",
            "summary": "No file changes detected since the last saved backup.",
            "manifest": None,
        }
    summary = (
        f"Saved app backup {manifest['version_id']} ({manifest['kind']}): "
        f"+{manifest['stats']['added']} ~{manifest['stats']['modified']} -{manifest['stats']['deleted']}"
    )
    append_audit_log(
        action="app.backup.save",
        status="ok",
        summary=summary,
        details={"version_id": manifest["version_id"], "folder": manifest["folder"]},
    )
    return {"status": "ok", "summary": summary, "manifest": manifest}


class RestoreAppBackupRequest(BaseModel):
    version_id: str


@router.post("/api/app/backup/restore")
async def restore_app_backup(req: RestoreAppBackupRequest) -> dict[str, Any]:
    """Restores the app files to a selected saved backup version."""
    bm = _load_workspace_module("backup_manager", "backup_manager.py")
    bm.restore_version(req.version_id.strip(), bm.PROJECT_ROOT)
    summary = f"Restored app to backup version '{req.version_id}'"
    append_audit_log(
        action="app.backup.restore",
        status="ok",
        summary=summary,
        details={"version_id": req.version_id},
    )
    return {"status": "ok", "summary": summary, "version_id": req.version_id}


@router.post("/api/app/export")
async def export_portable_app() -> dict[str, Any]:
    """Packages the app into a portable Windows .zip distribution ready for download."""
    pw = _load_workspace_module("package_windows", "package_windows.py")
    result = pw.build_package()
    summary = f"Packaged portable Windows app ({result['zip_size_mb']} MB, {result['file_count']} files)"
    append_audit_log(
        action="app.export",
        status="ok",
        summary=summary,
        sha256=result.get("zip_sha256"),
        details=result,
    )
    return {
        "status": "ok",
        "summary": summary,
        "download_url": "/api/app/export/download",
        **result,
    }


@router.get("/api/app/export/download")
async def download_portable_app():
    """Streams the packaged portable Windows .zip archive as a browser download."""
    zip_path = WORKSPACE_DIR / "dist" / "ModToolPoC-Win64.zip"
    if not zip_path.is_file():
        pw = _load_workspace_module("package_windows", "package_windows.py")
        pw.build_package()
    if not zip_path.is_file():
        raise HTTPException(status_code=404, detail="Portable app archive not found.")
    return FileResponse(
        path=zip_path,
        media_type="application/zip",
        filename="ModToolPoC-Win64.zip",
    )


REPO_CONFIG_PATH = get_app_data_root() / "repo_config.json"
APP_SNAPSHOTS_DIR = get_app_data_root() / "app_backups"
APP_SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_REPO_CONFIG: dict[str, str] = {
    "owner": "MrWibbleBottom",
    "repo": "The-Modding-Tool",
    "branch": "main",
}


def _parse_github_owner_repo(owner_or_url: str, repo_name: str = "") -> tuple[str, str]:
    import re

    raw_o = str(owner_or_url or "").strip()
    raw_r = str(repo_name or "").strip()
    for candidate in (raw_o, raw_r):
        if "github.com" in candidate.lower():
            m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s?#]+)", candidate, re.IGNORECASE)
            if m:
                owner = m.group(1).strip()
                repo = m.group(2).strip()
                if repo.lower().endswith(".git"):
                    repo = repo[:-4]
                return owner, repo
    if "/" in raw_o and not raw_r:
        parts = [p.strip() for p in raw_o.split("/") if p.strip()]
        if len(parts) >= 2:
            repo = parts[1][:-4] if parts[1].lower().endswith(".git") else parts[1]
            return parts[0], repo
    if raw_r.lower().endswith(".git"):
        raw_r = raw_r[:-4]
    return raw_o, raw_r


def load_repo_config() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "owner": DEFAULT_REPO_CONFIG["owner"],
        "repo": DEFAULT_REPO_CONFIG["repo"],
        "branch": DEFAULT_REPO_CONFIG["branch"],
        "token": "",
        "installed_sha": "",
        "last_checked": "",
    }
    file_existed = REPO_CONFIG_PATH.is_file()
    changed = not file_existed
    if file_existed:
        try:
            raw = json.loads(REPO_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k in defaults:
                    if raw.get(k) is not None and str(raw.get(k)).strip() != "":
                        val = str(raw[k]).strip()
                        defaults[k] = dpapi_decrypt(val) if k == "token" else val
                    elif k in DEFAULT_REPO_CONFIG:
                        defaults[k] = DEFAULT_REPO_CONFIG[k]
                        changed = True
        except Exception:
            changed = True
    if not defaults["owner"]:
        defaults["owner"] = DEFAULT_REPO_CONFIG["owner"]
        changed = True
    if not defaults["repo"]:
        defaults["repo"] = DEFAULT_REPO_CONFIG["repo"]
        changed = True
    if not defaults["branch"]:
        defaults["branch"] = DEFAULT_REPO_CONFIG["branch"]
        changed = True
    if changed:
        save_repo_config(defaults)
    return defaults


def save_repo_config(cfg: dict[str, Any]) -> None:
    REPO_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    to_save = dict(cfg)
    if to_save.get("token"):
        to_save["token"] = dpapi_encrypt(str(to_save["token"]))
    REPO_CONFIG_PATH.write_text(json.dumps(to_save, indent=2), encoding="utf-8")


def _mask_token(tok: str) -> str:
    if not tok:
        return ""
    if len(tok) <= 6:
        return "******"
    return "******" + tok[-4:]


def _safe_repo_config_response(cfg: dict[str, Any]) -> dict[str, Any]:
    tok = cfg.get("token", "")
    sha = cfg.get("installed_sha", "")
    return {
        "owner": cfg.get("owner", ""),
        "repo": cfg.get("repo", ""),
        "branch": cfg.get("branch", "main"),
        "token_set": bool(tok),
        "token_masked": _mask_token(tok),
        "installed_sha": sha,
        "installed_short_sha": sha[:7] if sha else "",
        "last_checked": cfg.get("last_checked", ""),
    }


def _create_app_snapshot(tag: str = "pre_sync") -> dict[str, Any]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    snap_name = f"{ts}_{tag}"
    snap_dir = APP_SNAPSHOTS_DIR / snap_name
    snap_dir.mkdir(parents=True, exist_ok=True)

    exclude_parts = {".venv", "__pycache__", ".git", "node_modules"}
    file_hashes: dict[str, str] = {}
    copied = 0
    for fpath in POC_DIR.rglob("*"):
        if not fpath.is_file():
            continue
        rel = fpath.relative_to(POC_DIR)
        if any(part in exclude_parts for part in rel.parts):
            continue
        dest = snap_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fpath, dest)
        file_hashes[str(rel).replace("\\", "/")] = sha256_file(fpath)
        copied += 1

    manifest = {
        "snapshot": snap_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tag": tag,
        "file_count": copied,
        "files": file_hashes,
    }
    (snap_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _list_app_snapshots() -> list[dict[str, Any]]:
    if not APP_SNAPSHOTS_DIR.is_dir():
        return []
    snaps: list[dict[str, Any]] = []
    for d in sorted(APP_SNAPSHOTS_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        m_path = d / "manifest.json"
        info: dict[str, Any] = {"name": d.name, "created_at": "", "file_count": 0, "tag": ""}
        if m_path.is_file():
            try:
                m = json.loads(m_path.read_text(encoding="utf-8"))
                info["created_at"] = m.get("created_at", "")
                info["file_count"] = m.get("file_count", 0)
                info["tag"] = m.get("tag", "")
            except Exception:
                pass
        snaps.append(info)
    return snaps


def _github_headers(token: str = "") -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ModToolPoC/1.0",
    }
    if token.strip():
        headers["Authorization"] = f"Bearer {token.strip()}"
    return headers


class RepoConfigRequest(BaseModel):
    owner: str = ""
    repo: str = ""
    branch: str = "main"
    token: str = ""
    clear_token: bool = False


class RollbackSnapshotRequest(BaseModel):
    snapshot: str


@router.get("/api/app/repo-config")
async def get_app_repo_config() -> dict[str, Any]:
    cfg = load_repo_config()
    return {"status": "ok", **_safe_repo_config_response(cfg)}


@router.post("/api/app/repo-config")
async def save_app_repo_config(req: RepoConfigRequest) -> dict[str, Any]:
    cfg = load_repo_config()
    parsed_owner, parsed_repo = _parse_github_owner_repo(req.owner, req.repo)
    cfg["owner"] = parsed_owner or DEFAULT_REPO_CONFIG["owner"]
    cfg["repo"] = parsed_repo or DEFAULT_REPO_CONFIG["repo"]
    cfg["branch"] = req.branch.strip() or DEFAULT_REPO_CONFIG["branch"]
    if req.clear_token:
        cfg["token"] = ""
    elif req.token.strip() and not req.token.strip().startswith("******"):
        cfg["token"] = req.token.strip()
    save_repo_config(cfg)

    append_audit_log(
        action="app.repo_config.save",
        status="ok",
        summary=f"Saved GitHub repo config ({cfg['owner']}/{cfg['repo']}@{cfg['branch']})",
        details={"owner": cfg["owner"], "repo": cfg["repo"], "branch": cfg["branch"], "token_set": bool(cfg["token"])},
    )
    return {"status": "ok", **_safe_repo_config_response(cfg)}


@router.post("/api/app/check-update")
async def check_github_update() -> dict[str, Any]:
    import urllib.error
    import urllib.request

    cfg = load_repo_config()
    owner, repo, branch = cfg["owner"].strip(), cfg["repo"].strip(), cfg["branch"].strip() or "main"
    if not owner or not repo:
        return {
            "status": "unconfigured",
            "message": "GitHub owner and repository name are not configured.",
            "installed_sha": cfg.get("installed_sha", ""),
            "installed_short_sha": (cfg.get("installed_sha") or "")[:7],
            "update_available": False,
        }

    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}"
    req = urllib.request.Request(url, headers=_github_headers(cfg.get("token", "")))
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as he:
        if he.code == 401:
            msg = "GitHub authentication failed (401). Check your Private Repo Token."
        elif he.code == 403:
            msg = "GitHub API rate limit exceeded or access forbidden (403). Add a token or retry later."
        elif he.code == 404:
            msg = f"Repository or branch '{owner}/{repo}@{branch}' not found (404)."
        else:
            msg = f"GitHub API error ({he.code})."
        return {"status": "error", "message": msg, "installed_sha": cfg.get("installed_sha", ""), "update_available": False}
    except Exception as exc:
        return {"status": "offline", "message": f"Unable to reach GitHub ({exc}).", "installed_sha": cfg.get("installed_sha", ""), "update_available": False}

    latest_sha = str(data.get("sha") or "").strip()
    commit_obj = data.get("commit") or {}
    commit_msg = str(commit_obj.get("message") or "").splitlines()[0] if commit_obj.get("message") else ""
    commit_date = str(((commit_obj.get("committer") or {}).get("date")) or ((commit_obj.get("author") or {}).get("date")) or "")
    installed_sha = cfg.get("installed_sha", "")
    cfg["last_checked"] = datetime.now(timezone.utc).isoformat()
    save_repo_config(cfg)

    return {
        "status": "ok",
        "latest_sha": latest_sha,
        "short_sha": latest_sha[:7],
        "commit_message": commit_msg,
        "commit_date": commit_date,
        "installed_sha": installed_sha,
        "installed_short_sha": installed_sha[:7],
        "update_available": bool(latest_sha and latest_sha != installed_sha),
    }


@router.post("/api/app/sync")
async def sync_app_from_github() -> dict[str, Any]:
    import io
    import urllib.error
    import urllib.request

    cfg = load_repo_config()
    owner, repo, branch = cfg["owner"].strip(), cfg["repo"].strip(), cfg["branch"].strip() or "main"
    if not owner or not repo:
        raise HTTPException(status_code=400, detail="GitHub owner and repository must be configured before syncing.")

    # 1. Pre-sync snapshot
    snapshot = _create_app_snapshot(tag="pre_sync")

    # 2. Fetch latest commit SHA (best effort)
    latest_sha = ""
    try:
        c_req = urllib.request.Request(
            f"https://api.github.com/repos/{owner}/{repo}/commits/{branch}",
            headers=_github_headers(cfg.get("token", "")),
        )
        with urllib.request.urlopen(c_req, timeout=10) as c_resp:
            latest_sha = str(json.loads(c_resp.read().decode("utf-8", errors="replace")).get("sha") or "").strip()
    except Exception:
        pass

    # 3. Download zipball
    zip_url = f"https://api.github.com/repos/{owner}/{repo}/zipball/{branch}"
    z_req = urllib.request.Request(zip_url, headers=_github_headers(cfg.get("token", "")))
    try:
        with urllib.request.urlopen(z_req, timeout=30) as z_resp:
            zip_bytes = z_resp.read()
    except urllib.error.HTTPError as he:
        raise HTTPException(status_code=he.code, detail=f"GitHub zipball download failed ({he.code}).")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GitHub zipball download failed: {exc}")

    archive_sha256 = hashlib.sha256(zip_bytes).hexdigest()
    extracted_files: list[str] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        # Detect if repo has a poc-ui/ subfolder
        stripped_names = [Path(m.filename).parts[1:] for m in members if len(Path(m.filename).parts) > 1]
        has_poc_ui = any(parts and parts[0].lower() == "poc-ui" for parts in stripped_names)
        for m in members:
            parts = Path(m.filename).parts
            if len(parts) <= 1:
                continue
            rel_parts = parts[1:]
            if has_poc_ui:
                if rel_parts[0].lower() != "poc-ui" or len(rel_parts) <= 1:
                    continue
                rel_sub = Path(*rel_parts[1:])
            else:
                rel_sub = Path(*rel_parts)
            dest = (POC_DIR / rel_sub).resolve()
            if not str(dest).startswith(str(POC_DIR.resolve())):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(m) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted_files.append(str(rel_sub).replace("\\", "/"))

    if latest_sha:
        cfg["installed_sha"] = latest_sha
    cfg["last_checked"] = datetime.now(timezone.utc).isoformat()
    save_repo_config(cfg)

    append_audit_log(
        action="app.github_sync",
        status="ok",
        summary=f"Synced {len(extracted_files)} file(s) from GitHub {owner}/{repo}@{branch}",
        sha256=archive_sha256,
        details={
            "owner": owner,
            "repo": repo,
            "branch": branch,
            "installed_sha": cfg.get("installed_sha", ""),
            "archive_sha256": archive_sha256,
            "snapshot": snapshot["snapshot"],
            "file_count": len(extracted_files),
        },
    )
    return {
        "status": "ok",
        "installed_sha": cfg.get("installed_sha", ""),
        "installed_short_sha": (cfg.get("installed_sha") or "")[:7],
        "archive_sha256": archive_sha256,
        "snapshot": snapshot["snapshot"],
        "extracted_count": len(extracted_files),
    }


@router.post("/api/app/rollback")
async def rollback_app_snapshot(req: RollbackSnapshotRequest) -> dict[str, Any]:
    snap_name = Path(req.snapshot.strip()).name
    snap_dir = APP_SNAPSHOTS_DIR / snap_name
    if not snap_name or not snap_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Snapshot '{snap_name}' not found.")

    safety_snap = _create_app_snapshot(tag="pre_rollback")
    restored = 0
    for fpath in snap_dir.rglob("*"):
        if not fpath.is_file() or fpath.name == "manifest.json":
            continue
        rel = fpath.relative_to(snap_dir)
        dest = (POC_DIR / rel).resolve()
        if not str(dest).startswith(str(POC_DIR.resolve())):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fpath, dest)
        restored += 1

    append_audit_log(
        action="app.snapshot_rollback",
        status="ok",
        summary=f"Rolled back app to snapshot '{snap_name}' ({restored} file(s))",
        details={"snapshot": snap_name, "safety_snapshot": safety_snap["snapshot"], "restored_count": restored},
    )
    return {
        "status": "ok",
        "snapshot": snap_name,
        "safety_snapshot": safety_snap["snapshot"],
        "restored_count": restored,
    }


def _resolve_git_app_root() -> Path:
    if (WORKSPACE_DIR / ".git").is_dir():
        return WORKSPACE_DIR
    if (POC_DIR / ".git").is_dir():
        return POC_DIR
    return WORKSPACE_DIR


def _sanitize_git_output(text: str, token: str = "", auth_b64: str = "") -> str:
    out = str(text or "")
    if token:
        out = out.replace(token, "***")
    if auth_b64:
        out = out.replace(auth_b64, "***")
    return out.strip()


def _run_git_cmd(
    git_bin: str,
    args: list[str],
    cwd: Path,
    extra_header: str = "",
    timeout: int = 30,
) -> tuple[int, str, str]:
    import subprocess

    cmd = [git_bin]
    if extra_header:
        cmd.extend(["-c", f"http.extraheader={extra_header}"])
    cmd.extend(args)
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


@router.get("/api/app/git-status")
async def get_app_git_status() -> dict[str, Any]:
    git_bin = shutil.which("git")
    app_root = _resolve_git_app_root()
    is_repo = (app_root / ".git").is_dir()
    resp: dict[str, Any] = {
        "status": "ok",
        "git_available": bool(git_bin),
        "app_root": str(app_root),
        "is_repo": is_repo,
        "branch": "",
        "remote_url": "",
        "changed_files": [],
        "latest_local_commit": None,
    }
    if not git_bin or not is_repo:
        return resp

    rc_br, out_br, _ = _run_git_cmd(git_bin, ["rev-parse", "--abbrev-ref", "HEAD"], app_root)
    if rc_br == 0:
        resp["branch"] = out_br.strip()

    rc_rem, out_rem, _ = _run_git_cmd(git_bin, ["remote", "get-url", "origin"], app_root)
    if rc_rem == 0:
        resp["remote_url"] = out_rem.strip()

    rc_st, out_st, _ = _run_git_cmd(git_bin, ["status", "--porcelain"], app_root)
    if rc_st == 0:
        changed: list[str] = []
        for line in out_st.splitlines():
            if len(line) >= 4:
                changed.append(line[3:].strip())
        resp["changed_files"] = changed

    rc_log, out_log, _ = _run_git_cmd(git_bin, ["log", "-1", "--format=%H%x1f%h%x1f%s%x1f%cI"], app_root)
    if rc_log == 0 and out_log.strip():
        parts = out_log.strip().split("\x1f")
        if len(parts) >= 4:
            resp["latest_local_commit"] = {
                "sha": parts[0],
                "short_sha": parts[1],
                "message": parts[2],
                "date": parts[3],
            }
    return resp


class GitPushRequest(BaseModel):
    commit_message: str = ""
    branch: str = "main"


@router.post("/api/app/git-push")
async def push_app_to_github(req: GitPushRequest) -> dict[str, Any]:
    import base64

    git_bin = shutil.which("git")
    if not git_bin:
        raise HTTPException(status_code=500, detail="Git CLI ('git') is not installed or not on PATH.")

    cfg = load_repo_config()
    owner = cfg.get("owner", "").strip()
    repo = cfg.get("repo", "").strip()
    token = cfg.get("token", "").strip()
    branch = req.branch.strip() or cfg.get("branch", "main").strip() or "main"
    if not owner or not repo:
        raise HTTPException(status_code=400, detail="GitHub owner and repository must be configured before pushing.")

    app_root = _resolve_git_app_root()

    # Ensure .gitignore exists with required exclusions
    gitignore_path = app_root / ".gitignore"
    required_ignores = [".venv/", "__pycache__/", "*.pyc", ".env", "*.log"]
    existing_lines: list[str] = []
    if gitignore_path.is_file():
        existing_lines = gitignore_path.read_text(encoding="utf-8", errors="replace").splitlines()
    existing_set = {ln.strip() for ln in existing_lines}
    added_ignores = [pat for pat in required_ignores if pat not in existing_set]
    if added_ignores or not gitignore_path.is_file():
        new_lines = existing_lines + added_ignores
        gitignore_path.write_text("\n".join(new_lines).rstrip() + "\n", encoding="utf-8")

    # Initialize .git if needed
    if not (app_root / ".git").is_dir():
        rc_init, _, err_init = _run_git_cmd(git_bin, ["init"], app_root)
        if rc_init != 0:
            raise HTTPException(status_code=500, detail=f"git init failed: {err_init.strip()}")
        _run_git_cmd(git_bin, ["branch", "-M", branch], app_root)

    # Ensure origin remote points to clean HTTPS URL (no token embedded)
    clean_remote_url = f"https://github.com/{owner}/{repo}.git"
    rc_rem, out_rem, _ = _run_git_cmd(git_bin, ["remote", "get-url", "origin"], app_root)
    if rc_rem != 0:
        _run_git_cmd(git_bin, ["remote", "add", "origin", clean_remote_url], app_root)
    elif out_rem.strip() != clean_remote_url:
        _run_git_cmd(git_bin, ["remote", "set-url", "origin", clean_remote_url], app_root)

    # Stage all changes
    rc_add, _, err_add = _run_git_cmd(git_bin, ["add", "-A"], app_root)
    if rc_add != 0:
        raise HTTPException(status_code=500, detail=f"git add -A failed: {err_add.strip()}")

    # Count changed files & commit if staged changes exist
    rc_st, out_st, _ = _run_git_cmd(git_bin, ["status", "--porcelain"], app_root)
    changed_count = len([ln for ln in out_st.splitlines() if ln.strip()]) if rc_st == 0 else 0

    rc_diff, _, _ = _run_git_cmd(git_bin, ["diff", "--cached", "--quiet"], app_root)
    commit_created = False
    if rc_diff != 0:
        ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        commit_msg = req.commit_message.strip() or f"Update app ({ts_str})"
        rc_cm, out_cm, err_cm = _run_git_cmd(git_bin, ["commit", "-m", commit_msg], app_root)
        if rc_cm != 0:
            raise HTTPException(status_code=500, detail=f"git commit failed: {(err_cm or out_cm).strip()}")
        commit_created = True

    # Push with ephemeral header if token is set
    auth_b64 = ""
    extra_header = ""
    if token:
        auth_b64 = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
        extra_header = f"AUTHORIZATION: basic {auth_b64}"

    rc_push, out_push, err_push = _run_git_cmd(
        git_bin,
        ["push", "-u", "origin", branch],
        app_root,
        extra_header=extra_header,
        timeout=60,
    )
    sanitized_out = _sanitize_git_output(f"{out_push}\n{err_push}", token=token, auth_b64=auth_b64)
    if rc_push != 0:
        return JSONResponse(
            status_code=400,
            content={
                "status": "error",
                "detail": "git push failed",
                "stderr": _sanitize_git_output(err_push or out_push, token=token, auth_b64=auth_b64),
            },
        )

    rc_head, out_head, _ = _run_git_cmd(git_bin, ["rev-parse", "HEAD"], app_root)
    head_sha = out_head.strip() if rc_head == 0 else ""
    if head_sha:
        cfg["installed_sha"] = head_sha
        cfg["branch"] = branch
        save_repo_config(cfg)

    append_audit_log(
        action="GIT_PUSH",
        status="ok",
        summary=f"Pushed {owner}/{repo}@{branch} ({head_sha[:7] or 'HEAD'}, {changed_count} changed file(s))",
        sha256=head_sha,
        details={
            "owner": owner,
            "repo": repo,
            "branch": branch,
            "commit_sha": head_sha,
            "changed_file_count": changed_count,
            "commit_created": commit_created,
        },
    )
    return {
        "status": "ok",
        "commit_sha": head_sha,
        "short_sha": head_sha[:7],
        "branch": branch,
        "changed_file_count": changed_count,
        "commit_created": commit_created,
        "output": sanitized_out or "Push completed.",
    }

