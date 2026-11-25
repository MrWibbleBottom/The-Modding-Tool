"""Tool/loader installation, status reporting, and checksum-verified upgrade helpers."""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException

from app.core.audit import append_audit_log
from app.core.config import (
    DEFAULT_BEPINEX_SHA256,
    DEFAULT_BEPINEX_ZIP,
    DEFAULT_GAME_DIR,
    DEFAULT_PLUGIN_BUILD_DIR,
    TOOL_VERSIONS_PATH,
    TOOLS_ROOT,
    WORKSPACE_DIR,
    atomic_write_json,
    get_app_data_root,
    get_bepinex_bridge_roots,
    load_json_sanitized,
    load_managed_games,
    save_managed_games,
    sha256_file,
    sha256_text,
)
from app.core.constants import PLUGIN_DEPLOY_FILES, TOOL_RELEASE_CATALOG
from app.services.conflict_guard import inspect_conflict_guard
from app.services.game_snapshots import create_game_snapshot, list_game_snapshots
from app.services.game_store import enrich_game_status, inspect_game_modding_info


async def _download_and_extract_zip(
    url: str,
    target_dir: Path,
    archive_name: str,
    *,
    strip_nested_bepinex_root: bool = False,
) -> tuple[Path, str, int]:
    """Download a zip archive over HTTPS, compute its SHA-256, and extract it into *target_dir*."""
    import httpx

    target_dir.mkdir(parents=True, exist_ok=True)
    archive_path = target_dir / archive_name
    async with httpx.AsyncClient(follow_redirects=True, timeout=90.0) as client:
        resp = await client.get(url, headers={"User-Agent": "TheModdingTool/1.0"})
        resp.raise_for_status()
        archive_path.write_bytes(resp.content)

    file_hash = sha256_file(archive_path)
    extracted_count = 0
    if archive_name.lower().endswith(".zip") and zipfile.is_zipfile(archive_path):
        extract_dest = target_dir
        with zipfile.ZipFile(archive_path, "r") as zf:
            names = [n for n in zf.namelist() if n and not n.endswith("/")]
            if (
                strip_nested_bepinex_root
                and target_dir.name.lower() == "bepinex"
                and names
                and all(n.lower().startswith("bepinex/") for n in names)
            ):
                extract_dest = target_dir.parent
            zf.extractall(extract_dest)
            extracted_count = len(zf.namelist())
    return (archive_path, file_hash, extracted_count)


def _score_github_asset(
    asset: dict[str, Any],
    asset_match: dict[str, Any] | None,
    *,
    is_prerelease: bool = False,
) -> int | None:
    name = str(asset.get("name") or "").strip()
    dl_url = str(asset.get("browser_download_url") or "").strip()
    if not name or not dl_url:
        return None
    name_low = name.lower()
    rules = asset_match or {}
    include_tokens = [str(t).strip().lower() for t in (rules.get("include") or []) if str(t).strip()]
    prefer_tokens = [str(t).strip().lower() for t in (rules.get("prefer") or []) if str(t).strip()]
    exclude_tokens = [str(t).strip().lower() for t in (rules.get("exclude") or []) if str(t).strip()]

    for tok in include_tokens:
        if tok not in name_low:
            return None
    for tok in exclude_tokens:
        if tok in name_low:
            return None

    score = 100
    if name_low.endswith(".zip"):
        score += 10
    if not is_prerelease:
        score += 15
    for idx, tok in enumerate(prefer_tokens):
        if tok in name_low:
            score += 20 + max(0, len(prefer_tokens) - idx)
    return score


def resolve_github_release_asset_info(
    repos: list[str] | tuple[str, ...] | str,
    asset_match: dict[str, Any] | None = None,
    fallback_url: str | None = None,
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Query GitHub Releases API across ordered *repos* and score matching assets; fall back only if rate-limited/offline."""
    import httpx

    repo_list = [repos] if isinstance(repos, str) else [str(r).strip() for r in (repos or []) if str(r or "").strip()]
    rules = dict(asset_match or {})
    if "include" not in rules:
        rules["include"] = [".zip"]

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "TheModdingTool/1.0",
    }
    saw_unavailable = False
    last_error = ""

    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        for repo in repo_list:
            candidate_releases: list[dict[str, Any]] = []
            try:
                resp = client.get(f"https://api.github.com/repos/{repo}/releases/latest", headers=headers)
                if resp.status_code in (403, 429) or resp.status_code >= 500:
                    saw_unavailable = True
                    last_error = f"GitHub API HTTP {resp.status_code} for {repo}"
                    continue
                if resp.status_code == 200:
                    latest_rel = resp.json()
                    if isinstance(latest_rel, dict):
                        candidate_releases.append(latest_rel)

                # Fallback to /releases list if /latest is 404 or had no assets
                if resp.status_code == 404 or not (candidate_releases and candidate_releases[0].get("assets")):
                    resp_all = client.get(f"https://api.github.com/repos/{repo}/releases?per_page=10", headers=headers)
                    if resp_all.status_code in (403, 429) or resp_all.status_code >= 500:
                        saw_unavailable = True
                        last_error = f"GitHub API HTTP {resp_all.status_code} for {repo}/releases"
                        continue
                    if resp_all.status_code == 200:
                        rel_list = resp_all.json()
                        if isinstance(rel_list, list):
                            candidate_releases.extend(r for r in rel_list if isinstance(r, dict) and not r.get("draft"))
                    elif resp_all.status_code == 404:
                        last_error = f"No GitHub releases found for {repo}"
                        continue
                    else:
                        last_error = f"GitHub API HTTP {resp_all.status_code} for {repo}/releases"
                        continue
            except Exception as exc:
                saw_unavailable = True
                last_error = f"{type(exc).__name__}: {exc}"
                continue

            best_tuple: tuple[int, dict[str, Any], dict[str, Any]] | None = None
            for rel in candidate_releases:
                is_pre = bool(rel.get("prerelease"))
                for asset in rel.get("assets") or []:
                    if not isinstance(asset, dict):
                        continue
                    sc = _score_github_asset(asset, rules, is_prerelease=is_pre)
                    if sc is None:
                        continue
                    if best_tuple is None or sc > best_tuple[0]:
                        best_tuple = (sc, asset, rel)

            if best_tuple is not None:
                _, chosen_asset, chosen_rel = best_tuple
                dl_url = str(chosen_asset.get("browser_download_url") or "")
                digest_raw = str(chosen_asset.get("digest") or "")
                sha_digest = digest_raw.split(":", 1)[1].strip().lower() if digest_raw.startswith("sha256:") else ""
                return {
                    "url": dl_url,
                    "repo": repo,
                    "tag": str(chosen_rel.get("tag_name") or "latest"),
                    "asset_name": str(chosen_asset.get("name") or "asset.zip"),
                    "sha256": sha_digest,
                    "used_fallback": False,
                }
            last_error = f"No matching asset in {repo} for {rules.get('include')}"

    if saw_unavailable and fallback_url:
        return {
            "url": fallback_url,
            "repo": repo_list[0] if repo_list else "",
            "tag": "fallback",
            "asset_name": Path(fallback_url).name or "download.zip",
            "sha256": "",
            "used_fallback": True,
        }
    raise RuntimeError(last_error or f"Could not resolve matching GitHub release asset across {repo_list}.")


def resolve_github_release_asset(
    repos: list[str] | tuple[str, ...] | str,
    asset_match: dict[str, Any] | None = None,
    fallback_url: str | None = None,
) -> str:
    """Resolve live `browser_download_url` across ordered GitHub repos, falling back to `fallback_url` only if offline/rate-limited."""
    info = resolve_github_release_asset_info(repos, asset_match, fallback_url=fallback_url)
    return str(info["url"])


async def _install_github_release_tool(
    repo: str | list[str] | tuple[str, ...],
    target_dir: Path,
    asset_keyword_or_match: str | dict[str, Any] = ".zip",
    *,
    fallback_url: str | None = None,
    strip_nested_bepinex_root: bool = False,
) -> tuple[str, str, int]:
    """Resolve matching GitHub release asset across repo(s), download, and extract into *target_dir*."""
    target_dir.mkdir(parents=True, exist_ok=True)
    asset_match = (
        dict(asset_keyword_or_match)
        if isinstance(asset_keyword_or_match, dict)
        else {"include": [asset_keyword_or_match, ".zip"] if asset_keyword_or_match != ".zip" else [".zip"]}
    )
    info = resolve_github_release_asset_info(repo, asset_match, fallback_url=fallback_url)
    dl_url = info["url"]
    aname = info["asset_name"] or f"{Path(dl_url).name or 'asset.zip'}"
    tag_name = info["tag"]
    _, file_hash, count = await _download_and_extract_zip(
        dl_url,
        target_dir,
        aname,
        strip_nested_bepinex_root=strip_nested_bepinex_root,
    )
    (target_dir / "release_info.json").write_text(
        json.dumps(
            {
                "repo": info["repo"],
                "tag": tag_name,
                "asset": aname,
                "download_url": dl_url,
                "used_fallback": info["used_fallback"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return (f"{aname} ({tag_name})", file_hash, count)


async def install_modding_tool_impl(
    tool_id_raw: str,
    game_id: str,
    game_dir_str: str,
    *,
    force: bool = False,
    pc_specs_fn: Callable[[bool], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Install or deploy a modding tool/app for the active game or tools directory."""
    tool_id = tool_id_raw.strip().lower()
    game_dir = Path(game_dir_str.strip()) if game_dir_str.strip() else Path(DEFAULT_GAME_DIR)
    dir_exists = game_dir.is_dir()

    guard_result: dict[str, Any] = {"has_conflicts": False, "warnings": []}
    loader_tools = {"bepinex_il2cpp", "bepinex_mono", "bepinex_mcp_il2cpp", "unity_explorer_il2cpp", "ue4ss", "reframework"}
    if dir_exists and tool_id in loader_tools:
        guard_result = inspect_conflict_guard(game_dir, intended_action=tool_id)
        blocking_warnings = [
            w for w in guard_result.get("warnings", [])
            if w.get("severity") == "high"
            or w.get("code") in ("mod_manager_detected", "proxy_dll_winhttp", "proxy_dll_dwmapi")
        ]
        if blocking_warnings and not force:
            return {
                "status": "conflict_warning",
                "requires_confirmation": True,
                "tool_id": tool_id,
                "game_id": game_id,
                "warnings": guard_result["warnings"],
                "conflict_guard": guard_result,
                "message": guard_result["summary"],
            }

    pre_snapshot_id = ""
    if dir_exists and tool_id in loader_tools:
        try:
            snap = create_game_snapshot(
                game_id or game_dir.name,
                game_dir,
                label=f"Pre-install {tool_id}",
                audit=True,
            )
            pre_snapshot_id = snap.get("snapshot_id", "")
        except ValueError:
            pre_snapshot_id = ""

    try:
        if tool_id == "bepinex_il2cpp":
            zip_path = Path(DEFAULT_BEPINEX_ZIP)
            if not zip_path.is_file():
                raise HTTPException(status_code=400, detail=f"Local BepInEx 6 IL2CPP zip not found at {zip_path}")
            dest_dir = game_dir if dir_exists else (TOOLS_ROOT / "BepInEx6_IL2CPP")
            dest_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(dest_dir)
                count = len(zf.namelist())
            h = sha256_file(zip_path)
            msg = f"Extracted BepInEx 6 IL2CPP ({count} files) into {dest_dir}"

        elif tool_id == "bepinex_mcp_il2cpp":
            build_dir = Path(DEFAULT_PLUGIN_BUILD_DIR)
            if not build_dir.is_dir():
                raise HTTPException(status_code=400, detail=f"Plugin build directory not found: {build_dir}")
            dest_dir = (
                (game_dir / "BepInEx" / "plugins" / "BepInExMCP.IL2CPP")
                if dir_exists
                else (TOOLS_ROOT / "BepInExMCP.IL2CPP")
            )
            dest_dir.mkdir(parents=True, exist_ok=True)
            copied = 0
            for fname in PLUGIN_DEPLOY_FILES:
                src = build_dir / fname
                if src.is_file():
                    shutil.copy2(src, dest_dir / fname)
                    copied += 1
            main_dll = dest_dir / "BepInExMCP.IL2CPP.dll"
            h = sha256_file(main_dll) if main_dll.is_file() else sha256_text(str(dest_dir))
            msg = f"Deployed BepInExMCP.IL2CPP + Roslyn ({copied} files) to {dest_dir}"

        elif tool_id in TOOL_RELEASE_CATALOG:
            cat_meta = TOOL_RELEASE_CATALOG[tool_id]
            strip_bep = False
            if tool_id == "unity_explorer_il2cpp":
                dest_dir = (
                    (game_dir / "BepInEx")
                    if (dir_exists and (game_dir / "GameAssembly.dll").is_file())
                    else (TOOLS_ROOT / "UnityExplorer_IL2CPP")
                )
                strip_bep = True
            elif tool_id == "bepinex_mono":
                is_mono_game = dir_exists and not (game_dir / "GameAssembly.dll").is_file() and bool(list(game_dir.glob("*_Data")))
                dest_dir = game_dir if is_mono_game else (TOOLS_ROOT / "BepInEx5_Mono")
            else:
                dir_names = {
                    "il2cpp_dumper": "Il2CppDumper",
                    "dnspy_ex": "dnSpyEx",
                    "ue4ss": "UE4SS",
                    "fmodel": "FModel",
                    "ghidra_mcp": "GhidraMCP",
                    "x64dbg": "x64dbg",
                    "gdre_tools": "GDRETools",
                    "mod_engine_2": "ModEngine2",
                    "reframework": "REFramework",
                    "xedit_creation": "xEdit",
                }
                dest_dir = TOOLS_ROOT / dir_names[tool_id]

            label, h, count = await _install_github_release_tool(
                cat_meta["github_repos"],
                dest_dir,
                cat_meta.get("asset_match") or {"include": [".zip"]},
                fallback_url=cat_meta.get("fallback_url"),
                strip_nested_bepinex_root=strip_bep,
            )
            msg = f"Installed {cat_meta['name']} [{label}] ({count} files) to {dest_dir}"

        else:
            raise HTTPException(status_code=400, detail=f"Unknown modding tool_id: '{tool_id}'")

    except HTTPException:
        raise
    except Exception as exc:
        append_audit_log(
            action=f"tools.install.{tool_id}",
            status="error",
            summary=f"Failed to install modding tool '{tool_id}': {exc}",
            details={"tool_id": tool_id, "game_id": game_id, "game_dir": str(game_dir)},
        )
        raise HTTPException(status_code=500, detail=f"Failed to install '{tool_id}': {exc}") from exc

    append_audit_log(
        action=f"tools.install.{tool_id}",
        status="ok",
        summary=msg,
        sha256=h,
        details={"tool_id": tool_id, "install_dir": str(dest_dir), "game_id": game_id},
    )

    store = load_managed_games()
    target = next((g for g in store.get("games", []) if g.get("id") == game_id), None)
    if target and dir_exists:
        scraped_info = inspect_game_modding_info(game_dir, str(target.get("name", "")), pc_specs_fn=pc_specs_fn)
        scraped_info["metadata_scraped"] = True
        target["cached_modding_info"] = scraped_info
        target["metadata_scraped"] = True
        save_managed_games(store)

    enriched_target = enrich_game_status(target, pc_specs_fn=pc_specs_fn) if target else None
    setup_checklist = build_setup_verification_checklist(
        game_dir,
        str(target.get("name", game_dir.name)) if target else game_dir.name,
        slug=game_id or game_dir.name,
        info=(enriched_target or {}).get("modding_info") if enriched_target else None,
    )
    return {
        "status": "ok",
        "tool_id": tool_id,
        "install_dir": str(dest_dir),
        "sha256": h,
        "message": msg,
        "snapshot_id": pre_snapshot_id,
        "conflict_warnings": guard_result.get("warnings", []),
        "setup_checklist": setup_checklist,
        "game": enriched_target,
    }


async def install_recommended_tools_impl(
    game_id: str,
    game_dir_str: str,
    *,
    tool_ids: list[str] | None = None,
    force: bool = False,
    pc_specs_fn: Callable[[bool], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Install all recommended (or specified) modding tools sequentially, continuing past single-tool errors."""
    game_dir = Path(game_dir_str.strip()) if game_dir_str.strip() else Path(DEFAULT_GAME_DIR)
    store = load_managed_games()
    target = next((g for g in store.get("games", []) if g.get("id") == game_id), None)
    game_name = str((target or {}).get("name") or game_dir.name or game_id)

    name_map: dict[str, str] = {
        tid: str(meta.get("name") or tid) for tid, meta in TOOL_RELEASE_CATALOG.items()
    }
    ordered_ids: list[str] = [str(t).strip().lower() for t in (tool_ids or []) if str(t).strip()]
    if not ordered_ids:
        info = inspect_game_modding_info(game_dir, game_name, pc_specs_fn=pc_specs_fn) if game_dir.is_dir() else {}
        for app_entry in info.get("modding_apps", []):
            aid = str(app_entry.get("id") or "").strip().lower()
            if aid:
                name_map[aid] = str(app_entry.get("name") or aid)
            if app_entry.get("recommended") and aid:
                ordered_ids.append(aid)

    installed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    latest_game: dict[str, Any] | None = None
    latest_checklist: dict[str, Any] | None = None

    for tid in ordered_ids:
        tname = name_map.get(tid, tid)
        try:
            res = await install_modding_tool_impl(
                tid,
                game_id,
                game_dir_str,
                force=force,
                pc_specs_fn=pc_specs_fn,
            )
            if res.get("status") == "conflict_warning":
                skipped.append({
                    "tool_id": tid,
                    "name": tname,
                    "status": "skipped",
                    "reason": res.get("message") or "Skipped due to conflict guard warning.",
                    "conflict_warnings": res.get("warnings", []),
                })
            else:
                installed.append({
                    "tool_id": tid,
                    "name": tname,
                    "status": "installed",
                    "message": res.get("message") or f"Installed {tname}",
                    "install_dir": res.get("install_dir", ""),
                    "sha256": res.get("sha256", ""),
                })
                if res.get("game") is not None:
                    latest_game = res["game"]
                if res.get("setup_checklist") is not None:
                    latest_checklist = res["setup_checklist"]
        except HTTPException as exc:
            failed.append({
                "tool_id": tid,
                "name": tname,
                "status": "failed",
                "status_code": exc.status_code,
                "reason": str(exc.detail),
            })
        except Exception as exc:
            failed.append({
                "tool_id": tid,
                "name": tname,
                "status": "failed",
                "reason": str(exc),
            })

    if latest_game is None and target is not None:
        latest_game = enrich_game_status(target, pc_specs_fn=pc_specs_fn)
    if latest_checklist is None:
        latest_checklist = build_setup_verification_checklist(
            game_dir,
            game_name,
            slug=game_id or game_dir.name,
            info=(latest_game or {}).get("modding_info") if latest_game else None,
        )

    if not failed:
        batch_status = "ok"
    elif installed:
        batch_status = "partial"
    else:
        batch_status = "error"

    summary = f"Installed {len(installed)}/{len(ordered_ids)} tool(s)"
    if skipped:
        summary += f", {len(skipped)} skipped"
    if failed:
        summary += f", {len(failed)} failed"

    append_audit_log(
        action="tools.install_recommended",
        status="ok" if not failed else "warning",
        summary=summary,
        details={"game_id": game_id, "installed": [x["tool_id"] for x in installed], "failed": failed, "skipped": skipped},
    )
    return {
        "status": batch_status,
        "summary": summary,
        "installed": installed,
        "skipped": skipped,
        "failed": failed,
        "installed_count": len(installed),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "game": latest_game,
        "setup_checklist": latest_checklist,
    }


install_recommended_tools = install_recommended_tools_impl


def build_setup_verification_checklist(
    game_dir: Path,
    game_name: str,
    *,
    slug: str = "",
    info: dict[str, Any] | None = None,
    snapshots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate step-by-step guided setup readiness checks until required tools report Ready."""
    dir_exists = game_dir.is_dir()
    resolved_info = info if isinstance(info, dict) else (
        inspect_game_modding_info(game_dir, game_name) if dir_exists else {}
    )
    resolved_slug = slug or game_dir.name or "game"
    resolved_snaps = snapshots if snapshots is not None else list_game_snapshots(resolved_slug)
    guard = inspect_conflict_guard(game_dir) if dir_exists else {"has_conflicts": False, "warnings": [], "summary": ""}

    cat = str(resolved_info.get("mcp_category") or "")
    has_ga = bool(dir_exists and (game_dir / "GameAssembly.dll").is_file())
    backend_low = str(resolved_info.get("scripting_backend") or "").lower()
    is_unity = ("unity" in cat.lower()) or has_ga or ("il2cpp" in backend_low) or ("mono" in backend_low)
    is_unreal = bool(resolved_info.get("is_unreal_engine") or "unreal" in cat.lower())

    bep_ready = bool(resolved_info.get("doorstop_installed") and resolved_info.get("bepinex_installed"))
    bridge_ready = bool(resolved_info.get("mcp_bridge_installed"))
    ue_ini_ready = bool((resolved_info.get("unreal_settings") or {}).get("ini_found"))
    ue4ss_ready = bool(
        dir_exists and (list(game_dir.glob("*/Binaries/Win64/UE4SS.dll")) or list(game_dir.glob("*/Binaries/Win64/dwmapi.dll")))
    )

    steps = [
        {
            "id": "game_folder",
            "title": "1. Game Directory & Executable",
            "status": "ready" if dir_exists else "missing",
            "detail": f"Located at {game_dir}" if dir_exists else f"Folder not found: {game_dir}",
            "required": True,
        },
        {
            "id": "baseline_snapshot",
            "title": "2. Clean Baseline Snapshot",
            "status": "ready" if resolved_snaps else "pending",
            "detail": (
                f"{len(resolved_snaps)} snapshot(s) saved (earliest: {resolved_snaps[-1]['snapshot_id']})"
                if resolved_snaps
                else "Auto-created on tool install or via Create Snapshot"
            ),
            "required": True,
        },
        {
            "id": "mod_loader",
            "title": "3. Mod Loader / Runtime Hook",
            "status": (
                ("ready" if bep_ready else "pending")
                if is_unity
                else (("ready" if (ue4ss_ready or ue_ini_ready) else "pending") if is_unreal else ("ready" if dir_exists else "pending"))
            ),
            "detail": (
                ("Doorstop + BepInEx detected" if bep_ready else "Install BepInEx 6 (IL2CPP) or BepInEx 5 (Mono)")
                if is_unity
                else (
                    ("RE-UE4SS / Unreal INI workspace verified" if (ue4ss_ready or ue_ini_ready) else "Install RE-UE4SS or rescan Unreal config")
                    if is_unreal
                    else "Standard folder / native config modding"
                )
            ),
            "required": True,
        },
        {
            "id": "mcp_bridge",
            "title": "4. Live Bridge / Config Surface",
            "status": (
                ("ready" if bridge_ready else "pending")
                if is_unity
                else ("ready" if (not is_unreal or ue_ini_ready) else "pending")
            ),
            "detail": (
                ("BepInExMCP plugin deployed" if bridge_ready else "Deploy BepInExMCP live bridge plugin")
                if is_unity
                else ("Config / INI surface accessible" if (not is_unreal or ue_ini_ready) else "Launch game once or select Unreal INI profile")
            ),
            "required": is_unity,
        },
        {
            "id": "conflict_check",
            "title": "5. Mod Manager / Proxy Conflict Check",
            "status": "warning" if guard.get("has_conflicts") else "ready",
            "detail": guard.get("summary") or "No Vortex/MO2 or proxy DLL conflicts detected",
            "required": False,
        },
    ]

    all_ready = all(s["status"] == "ready" for s in steps if s.get("required"))
    return {
        "status": "ok",
        "all_ready": all_ready,
        "readiness_label": "Ready" if all_ready else "Setup Needed",
        "steps": steps,
    }


def _load_tool_versions() -> dict[str, Any]:
    if TOOL_VERSIONS_PATH.is_file():
        try:
            data = load_json_sanitized(TOOL_VERSIONS_PATH, persist=True)
            return data if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}


def _save_tool_versions(data: dict[str, Any]) -> None:
    atomic_write_json(TOOL_VERSIONS_PATH, data)


def get_modding_tools_status_impl() -> dict[str, Any]:
    """Return local versions and SHA-256 checksums for deployed modding tools."""
    saved = _load_tool_versions()
    bep_zip = Path(DEFAULT_BEPINEX_ZIP)
    bep_sha = sha256_file(bep_zip) if bep_zip.is_file() else None

    plugin_dll = Path(DEFAULT_PLUGIN_BUILD_DIR) / "BepInExMCP.IL2CPP.dll"
    plugin_sha = sha256_file(plugin_dll) if plugin_dll.is_file() else None

    ue4ss_dir = get_app_data_root() / "tools" / "ue4ss"
    melon_dir = get_app_data_root() / "tools" / "melonloader"

    return {
        "status": "ok",
        "tools": [
            {
                "id": "bepinex6_il2cpp",
                "name": "BepInEx 6 Unity.IL2CPP (x64)",
                "present": bep_zip.is_file(),
                "version": saved.get("bepinex6_il2cpp", {}).get("version", "6.0.0-be.793" if bep_zip.is_file() else "Not Cached"),
                "path": str(bep_zip),
                "sha256": bep_sha,
                "expected_sha256": saved.get("bepinex6_il2cpp", {}).get("expected_sha256", DEFAULT_BEPINEX_SHA256),
                "checksum_match": (bep_sha == saved.get("bepinex6_il2cpp", {}).get("expected_sha256", DEFAULT_BEPINEX_SHA256)) if bep_sha else False,
            },
            {
                "id": "bepinex_mcp_plugin",
                "name": "BepInExMCP.IL2CPP Bridge Plugin",
                "present": plugin_dll.is_file(),
                "version": saved.get("bepinex_mcp_plugin", {}).get("version", "Local Release Build" if plugin_dll.is_file() else "Not Built"),
                "path": str(plugin_dll),
                "sha256": plugin_sha,
                "checksum_match": plugin_dll.is_file(),
            },
            {
                "id": "ue4ss",
                "name": "RE-UE4SS (Unreal Engine 4/5)",
                "present": ue4ss_dir.is_dir() and any(ue4ss_dir.iterdir()),
                "version": saved.get("ue4ss", {}).get("version", "Not Downloaded"),
                "path": str(ue4ss_dir),
                "sha256": saved.get("ue4ss", {}).get("sha256"),
                "checksum_match": bool(saved.get("ue4ss", {}).get("sha256")),
            },
            {
                "id": "melonloader",
                "name": "MelonLoader (Unity Mono/IL2CPP)",
                "present": melon_dir.is_dir() and any(melon_dir.iterdir()),
                "version": saved.get("melonloader", {}).get("version", "Not Downloaded"),
                "path": str(melon_dir),
                "sha256": saved.get("melonloader", {}).get("sha256"),
                "checksum_match": bool(saved.get("melonloader", {}).get("sha256")),
            },
        ],
    }


def upgrade_modding_tool_impl(
    tool_id: str,
    *,
    require_https: bool = True,
    require_checksum: bool = True,
    expected_sha256: str = "",
    custom_url: str = "",
    keep_backup: bool = True,
) -> dict[str, Any]:
    """Upgrade a modding deployment tool with HTTPS, SHA-256 checksum, and backup options."""
    tool_id = tool_id.strip()
    saved = _load_tool_versions()
    expected = expected_sha256.strip().lower()

    if tool_id == "bepinex_mcp_plugin":
        csproj = next(
            (
                root / "plugins" / "BepInExMCP.IL2CPP" / "BepInExMCP.IL2CPP.csproj"
                for root in get_bepinex_bridge_roots()
                if (root / "plugins" / "BepInExMCP.IL2CPP" / "BepInExMCP.IL2CPP.csproj").is_file()
            ),
            WORKSPACE_DIR / "bridges" / "bepinex-mcp" / "plugins" / "BepInExMCP.IL2CPP" / "BepInExMCP.IL2CPP.csproj",
        )
        if not csproj.is_file():
            raise HTTPException(status_code=404, detail=f"Plugin project not found: {csproj}")
        plugin_dll = Path(DEFAULT_PLUGIN_BUILD_DIR) / "BepInExMCP.IL2CPP.dll"
        if keep_backup and plugin_dll.is_file():
            bak = plugin_dll.with_suffix(f".dll.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            shutil.copy2(plugin_dll, bak)
        proc = subprocess.run(
            ["dotnet", "build", str(csproj), "-c", "Release", "--nologo"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0 or not plugin_dll.is_file():
            raise HTTPException(status_code=500, detail=f"dotnet build failed:\n{proc.stdout}\n{proc.stderr}")
        new_sha = sha256_file(plugin_dll)
        if require_checksum and expected and new_sha != expected:
            raise HTTPException(status_code=400, detail=f"Checksum mismatch: expected {expected}, got {new_sha}")
        saved["bepinex_mcp_plugin"] = {
            "version": f"Release ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
            "sha256": new_sha,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_tool_versions(saved)
        summary = f"Rebuilt BepInExMCP.IL2CPP Release plugin (SHA-256: {new_sha[:16]}...)"
        append_audit_log(action="app.tools.upgrade", status="ok", summary=summary, sha256=new_sha, details={"tool_id": tool_id})
        return {"status": "ok", "summary": summary, "sha256": new_sha, "build_output": proc.stdout[-1500:]}

    repo_map = {
        "bepinex6_il2cpp": ("BepInEx/BepInEx", "BepInEx-Unity.IL2CPP-win-x64"),
        "ue4ss": ("UE4SS-RE/RE-UE4SS", "UE4SS"),
        "melonloader": ("LavaGang/MelonLoader", "MelonLoader.x64"),
    }
    if tool_id not in repo_map:
        raise HTTPException(status_code=400, detail=f"Unknown tool_id: {tool_id}")

    repo, asset_prefix = repo_map[tool_id]
    download_url = custom_url.strip()
    release_tag = "custom"
    github_digest = ""

    if not download_url:
        api_url = f"https://api.github.com/repos/{repo}/releases/latest"
        gh_req = urllib.request.Request(api_url, headers={"User-Agent": "TheModdingTool/1.0"})
        try:
            with urllib.request.urlopen(gh_req, timeout=20) as resp:
                rel_data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to query GitHub release for {repo}: {exc}")
        release_tag = rel_data.get("tag_name") or "latest"
        for asset in rel_data.get("assets", []):
            name = asset.get("name", "")
            if asset_prefix.lower() in name.lower() and name.lower().endswith(".zip"):
                download_url = asset.get("browser_download_url", "")
                digest = asset.get("digest") or ""
                if digest.startswith("sha256:"):
                    github_digest = digest.split(":", 1)[1].strip().lower()
                break
        if not download_url:
            raise HTTPException(status_code=404, detail=f"No matching .zip asset ({asset_prefix}) found in {repo} {release_tag}.")

    if require_https and not download_url.lower().startswith("https://"):
        raise HTTPException(status_code=400, detail=f"Blocked insecure non-HTTPS download URL: {download_url}")

    effective_expected = expected or github_digest
    if require_checksum and not effective_expected:
        raise HTTPException(
            status_code=400,
            detail="Checksum verification is required, but no Expected SHA-256 was provided and the GitHub release asset did not publish a SHA-256 digest.",
        )

    dl_dir = get_app_data_root() / "tools" / tool_id
    dl_dir.mkdir(parents=True, exist_ok=True)
    tmp_zip = dl_dir / f"{tool_id}_{release_tag}.zip.tmp"
    dl_req = urllib.request.Request(download_url, headers={"User-Agent": "TheModdingTool/1.0"})
    try:
        with urllib.request.urlopen(dl_req, timeout=60) as resp, tmp_zip.open("wb") as out_f:
            shutil.copyfileobj(resp, out_f)
    except Exception as exc:
        if tmp_zip.exists():
            tmp_zip.unlink()
        raise HTTPException(status_code=502, detail=f"Download failed: {exc}")

    actual_sha = sha256_file(tmp_zip)
    if require_checksum and effective_expected and actual_sha != effective_expected:
        tmp_zip.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"Security Check Failed: SHA-256 mismatch! Expected {effective_expected}, got {actual_sha}. Download discarded.",
        )

    if tool_id == "bepinex6_il2cpp":
        assets_dir = WORKSPACE_DIR / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        target_zip = assets_dir / "BepInEx-Unity.IL2CPP-win-x64-6.0.0-be.793+dbc82f0.zip"
        if keep_backup and target_zip.is_file():
            shutil.copy2(target_zip, target_zip.with_suffix(f".zip.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}"))
        shutil.move(str(tmp_zip), str(target_zip))
        final_path = target_zip
    else:
        final_path = dl_dir / f"{tool_id}_{release_tag}.zip"
        if keep_backup and final_path.is_file():
            shutil.copy2(final_path, final_path.with_suffix(f".zip.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}"))
        shutil.move(str(tmp_zip), str(final_path))
        with zipfile.ZipFile(final_path, "r") as zf:
            zf.extractall(dl_dir / "extracted")

    saved[tool_id] = {
        "version": release_tag,
        "sha256": actual_sha,
        "expected_sha256": effective_expected or actual_sha,
        "source_url": download_url,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_tool_versions(saved)
    summary = f"Upgraded {tool_id} to {release_tag} (SHA-256 verified: {actual_sha[:16]}...)"
    append_audit_log(action="app.tools.upgrade", status="ok", summary=summary, sha256=actual_sha, details=saved[tool_id])
    return {"status": "ok", "summary": summary, "version": release_tag, "sha256": actual_sha, "path": str(final_path)}
