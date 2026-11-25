"""Unified Unity/Unreal log tailing and one-click AI modding context Markdown generator."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from app.core.config import DEFAULT_MCP_APP, get_bepinex_bridge_roots
from app.engines.generic import scan_installed_mods
from app.engines.unity import UnityEngineAdapter
from app.engines.unreal import UnrealEngineAdapter
from app.services.conflict_guard import inspect_conflict_guard
from app.services.discovery import make_slug
from app.services.game_snapshots import list_game_snapshots
from app.services.game_store import inspect_game_modding_info
from app.services.unreal_ini import resolve_unreal_ini_targets

_ERROR_LINE_RE = re.compile(
    r"(\[(?:error|fatal|critical)[^\]]*\]"
    r"|\b(?:error|fatal|exception|unhandled|crash|traceback|assert(?:ion)?\s+failed)\b"
    r"|Log[A-Za-z]+:\s*(?:Error|Fatal):"
    r"|\b(?:NullReferenceException|MissingMethodException|TypeLoadException|FileNotFoundException)\b)",
    re.IGNORECASE,
)
_STACK_CONT_RE = re.compile(r"^(\s+at\s+|\s+in\s+|--- End of |Caused by:|Stack trace:)", re.IGNORECASE)


def _local_appdata_root() -> Path:
    raw = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(raw)


def discover_game_log_files(
    game_dir: Path,
    *,
    detected_project_name: str = "",
) -> list[Path]:
    """Return existing Unity/Unreal log files sorted with the most recently modified first."""
    candidates: list[Path] = []
    if game_dir.is_dir():
        candidates.extend(UnityEngineAdapter().get_log_paths(game_dir))
        candidates.extend(UnrealEngineAdapter().get_log_paths(game_dir))
        for pat in ("Saved/Logs/*.log", "*/Saved/Logs/*.log", "*.log"):
            try:
                candidates.extend(p for p in game_dir.glob(pat) if p.is_file())
            except OSError:
                pass

    proj_candidates: list[str] = []
    if detected_project_name.strip():
        proj_candidates.append(detected_project_name.strip())
    if game_dir.is_dir():
        for exe in game_dir.glob("*/Binaries/Win64/*.exe"):
            proj_dir = exe.parent.parent.parent.name
            if proj_dir and proj_dir not in ("Engine", "Binaries") and proj_dir not in proj_candidates:
                proj_candidates.append(proj_dir)

    local_root = _local_appdata_root()
    for proj in proj_candidates:
        saved_logs = local_root / proj / "Saved" / "Logs"
        if saved_logs.is_dir():
            try:
                candidates.extend(p for p in saved_logs.glob("*.log") if p.is_file())
            except OSError:
                pass

    deduped: list[Path] = []
    seen: set[str] = set()
    for p in candidates:
        try:
            norm = os.path.normcase(os.path.normpath(str(p.resolve())))
        except OSError:
            norm = os.path.normcase(os.path.normpath(str(p)))
        if norm in seen or not p.is_file():
            continue
        seen.add(norm)
        deduped.append(p)

    deduped.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)
    return deduped


def _read_tail_lines(path: Path, max_bytes: int = 512 * 1024) -> list[str]:
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            data = fh.read()
            lines = data.decode("utf-8", errors="ignore").replace("\r\n", "\n").splitlines()
            return lines[1:] if len(lines) > 1 else lines
        data = fh.read()
    return data.decode("utf-8-sig", errors="ignore").replace("\r\n", "\n").splitlines()


def filter_error_lines(lines: list[str]) -> tuple[list[str], int]:
    """Filter log lines for errors/exceptions while retaining immediate stack-trace continuations."""
    filtered: list[str] = []
    error_hits = 0
    keep_stack = False
    for line in lines:
        if _ERROR_LINE_RE.search(line):
            filtered.append(line)
            error_hits += 1
            keep_stack = True
        elif keep_stack and _STACK_CONT_RE.match(line):
            filtered.append(line)
        else:
            keep_stack = False
    return filtered, error_hits


def read_game_log_tail(
    game_dir: Path,
    *,
    lines: int = 100,
    errors_only: bool = False,
    detected_project_name: str = "",
) -> dict[str, Any]:
    """Tail up to *lines* entries from the most recent Unity or Unreal log file for a game."""
    clamped_lines = max(1, min(int(lines or 100), 1000))
    log_files = discover_game_log_files(game_dir, detected_project_name=detected_project_name)
    if not log_files:
        return {
            "status": "ok",
            "log_path": "",
            "available_logs": [],
            "errors_only": bool(errors_only),
            "requested_lines": clamped_lines,
            "total_lines": 0,
            "error_count": 0,
            "returned_lines": 0,
            "lines": [],
            "text": "",
        }

    primary = log_files[0]
    raw_lines = _read_tail_lines(primary)
    error_lines, error_hits = filter_error_lines(raw_lines)
    source_lines = error_lines if errors_only else raw_lines
    tailed = source_lines[-clamped_lines:]

    available_meta = []
    for p in log_files[:10]:
        try:
            st = p.stat()
            available_meta.append({"path": str(p), "name": p.name, "size_bytes": st.st_size})
        except OSError:
            continue

    return {
        "status": "ok",
        "log_path": str(primary),
        "available_logs": available_meta,
        "errors_only": bool(errors_only),
        "requested_lines": clamped_lines,
        "total_lines": len(raw_lines),
        "error_count": error_hits,
        "returned_lines": len(tailed),
        "lines": tailed,
        "text": "\n".join(tailed),
    }


def build_ai_modding_context(
    game: dict[str, Any],
    *,
    info: dict[str, Any] | None = None,
    conflict_report: dict[str, Any] | None = None,
    snapshots: list[dict[str, Any]] | None = None,
    log_tail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a concise Markdown modding context brief ready to paste into any AI coding session."""
    game_name = str(game.get("name") or game.get("id") or "Unknown Game")
    slug = make_slug(str(game.get("id") or game_name))
    game_dir = Path(str(game.get("game_dir") or "").strip())

    resolved_info = info if isinstance(info, dict) else (
        game.get("cached_modding_info")
        if isinstance(game.get("cached_modding_info"), dict) and game.get("cached_modding_info")
        else (inspect_game_modding_info(game_dir, game_name) if game_dir.is_dir() else {})
    )
    resolved_conflicts = conflict_report or (
        inspect_conflict_guard(game_dir) if game_dir.is_dir() else {"has_conflicts": False, "warnings": [], "summary": "Game folder unavailable."}
    )
    resolved_snaps = snapshots if snapshots is not None else list_game_snapshots(slug)
    proj_name = str(resolved_info.get("detected_project_name") or "")
    resolved_log = log_tail or read_game_log_tail(game_dir, lines=30, errors_only=False, detected_project_name=proj_name)

    installed_mods, _mod_scan_meta = scan_installed_mods(game_dir) if game_dir.is_dir() else ([], {})

    bridge_script = str(game.get("mcp_app") or DEFAULT_MCP_APP or "")
    bridge_roots = [str(r) for r in get_bepinex_bridge_roots()]
    ue_targets = resolve_unreal_ini_targets(game_dir, game_name=game_name) if game_dir.is_dir() else {}

    dump_paths: list[str] = []
    if (game_dir / "BepInEx" / "interop").is_dir():
        dump_paths.append(str(game_dir / "BepInEx" / "interop"))
    if (game_dir / "BepInEx" / "config").is_dir():
        dump_paths.append(str(game_dir / "BepInEx" / "config"))
    for d in game_dir.glob("*_Data/Managed") if game_dir.is_dir() else []:
        if d.is_dir():
            dump_paths.append(str(d))
    for k, p in ue_targets.items():
        if p:
            dump_paths.append(f"{k}: {p}")

    md_lines: list[str] = [
        f"# AI Modding Context Pack — {game_name}",
        "",
        "## Game & Runtime",
        f"- **Game ID / Slug**: `{game.get('id', slug)}` (`{slug}`)",
        f"- **Install Path**: `{game_dir}`",
        f"- **Executable**: `{resolved_info.get('executable') or game.get('exe_name') or 'Unknown'}`",
        f"- **Engine / Category**: {resolved_info.get('mcp_category') or game.get('runtime') or 'Unknown'}",
        f"- **Runtime Details**: Unity `{resolved_info.get('unity_version') or 'N/A'}` | Backend `{resolved_info.get('backend') or resolved_info.get('scripting_backend') or 'N/A'}` | Metadata `v{resolved_info.get('metadata_version') or '?'}`",
        f"- **Modding Readiness**: {resolved_info.get('modding_readiness') or 'Unknown'}",
        "",
        "## MCP Bridge & Tooling Paths",
        f"- **Live Bridge Port**: `{resolved_info.get('mcp_port') or 5142}` (localhost HTTP/stdio when game + bridge plugin are running)",
        f"- **Bridge Script**: `{bridge_script or 'Not configured'}`",
        f"- **Discovered Bridge Roots**: {', '.join(f'`{r}`' for r in bridge_roots) if bridge_roots else 'None detected'}",
        f"- **Recommended / Available Tools**: {', '.join(resolved_info.get('recommended_tools') or []) or 'Standard workspace tools'}",
        "",
        "## Dump & Config Paths",
    ]
    if dump_paths:
        md_lines.extend(f"- `{p}`" for p in dump_paths)
    else:
        md_lines.append("- None detected yet (launch game with loader or run Fingerprint & Rescan).")

    md_lines.extend(["", "## Installed Plugins / Mods"])
    if installed_mods:
        for mod in installed_mods[:25]:
            mod_label = mod.get("file_name") or mod.get("name") or "unknown"
            mod_cat = mod.get("category") or mod.get("kind") or "mod"
            md_lines.append(f"- `{mod_label}` ({mod_cat}) — `{mod.get('path', '')}`")
    else:
        md_lines.append("- None detected.")

    md_lines.extend(["", "## Safety, Conflicts & Snapshots"])
    if resolved_snaps:
        latest = resolved_snaps[0]
        md_lines.append(
            f"- **Latest Snapshot**: `{latest.get('snapshot_id')}` ({latest.get('label') or 'Snapshot'}) — {latest.get('file_count', 0)} file(s)"
        )
    else:
        md_lines.append("- **Latest Snapshot**: None created yet.")

    warnings = resolved_conflicts.get("warnings") or []
    if warnings:
        for w in warnings:
            md_lines.append(f"- **Conflict Warning ({w.get('severity', 'warning')})**: {w.get('title', 'Conflict')} — {w.get('detail', '')}")
    else:
        md_lines.append(f"- **Conflict Check**: {resolved_conflicts.get('summary') or 'No proxy DLL or Vortex/MO2 conflicts detected.'}")

    log_path = resolved_log.get("log_path") or "None found"
    err_count = resolved_log.get("error_count", 0)
    tail_text = (resolved_log.get("text") or "").strip() or "(No log lines available yet.)"
    md_lines.extend([
        "",
        f"## Last 30 Log Lines (`{log_path}` | Errors in log: {err_count})",
        "```text",
        tail_text,
        "```",
        "",
    ])

    markdown = "\n".join(md_lines)
    return {
        "status": "ok",
        "slug": slug,
        "game_name": game_name,
        "log_path": log_path,
        "error_count": err_count,
        "char_count": len(markdown),
        "markdown": markdown,
    }
