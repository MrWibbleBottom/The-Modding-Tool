"""FastAPI router for per-game Mod Vault storage, static audit reports, and endpoint promotion."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from app.core.config import APP_DATA_ROOT as _BASE_APP_DATA_ROOT, load_managed_games
from app.services import endpoints as _endpoints_svc
from app.services import mod_auditor as _mod_auditor
from app.services.discovery import make_slug

router = APIRouter()


class PromoteStoredModRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    category_override: str = Field(default="")


class LinkLocalModRequest(BaseModel):
    local_path: str
    bundle_name: str = Field(default="")
    notes: str = Field(default="")
    source_label: str = Field(default="Local Path")


class EditStoredModValueRequest(BaseModel):
    file_path: str
    symbol: str
    new_value: str


def _effective_app_data_root() -> Path:
    if _mod_auditor.APP_DATA_ROOT != _BASE_APP_DATA_ROOT:
        return Path(_mod_auditor.APP_DATA_ROOT)
    return Path(_endpoints_svc.APP_DATA_ROOT)


def _find_game(slug: str) -> dict[str, Any]:
    clean = make_slug(slug)
    store = load_managed_games()
    for g in store.get("games", []):
        if g.get("id") == slug or make_slug(str(g.get("id") or "")) == clean or make_slug(str(g.get("name") or "")) == clean:
            return g
    raise HTTPException(status_code=404, detail=f"Managed game '{slug}' not found.")


@router.post("/api/games/{slug}/stored-mods/upload")
async def post_upload_stored_mod(
    slug: str,
    file: UploadFile | None = File(default=None),
    files: list[UploadFile] = File(default=[]),
    bundle_name: str = Form(default=""),
    notes: str = Form(default=""),
    source_label: str = Form(default=""),
) -> dict[str, Any]:
    """Stream uploaded archives or loose unpacked files/folders into the game's Mod Vault and audit them."""
    target = _find_game(slug)
    uploads: list[UploadFile] = []
    if file is not None and str(file.filename or "").strip():
        uploads.append(file)
    for item in files:
        if item is not None and str(item.filename or "").strip():
            uploads.append(item)
    if not uploads:
        raise HTTPException(status_code=400, detail="No mod files were provided.")
    try:
        return await _mod_auditor.store_and_audit_streamed_uploads(
            target,
            slug,
            uploads,
            bundle_name=bundle_name,
            notes=notes,
            source_label=source_label,
            app_data_root=_effective_app_data_root(),
        )
    except _mod_auditor.UploadTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/games/{slug}/stored-mods/link-local")
async def post_link_local_mod(slug: str, req: LinkLocalModRequest) -> dict[str, Any]:
    """Audit and register a local unpacked mod folder, `.zip`, or `.pak` directly from disk."""
    target = _find_game(slug)
    clean_slug = make_slug(str(target.get("id") or slug))
    try:
        return _mod_auditor.ingest_local_mod_path(
            target,
            req.local_path,
            bundle_name=req.bundle_name or None,
            slug=clean_slug,
            notes=req.notes,
            source_label=req.source_label,
            app_data_root=_effective_app_data_root(),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/games/{slug}/stored-mods")
async def get_list_stored_mods(slug: str) -> dict[str, Any]:
    """List all reference mods currently stored in the Mod Vault for *slug*."""
    target = _find_game(slug)
    clean_slug = make_slug(str(target.get("id") or slug))
    items = _mod_auditor.list_stored_mods(clean_slug, app_data_root=_effective_app_data_root())
    return {
        "status": "ok",
        "slug": clean_slug,
        "count": len(items),
        "stored_mods": items,
    }


@router.get("/api/games/{slug}/stored-mods/{mod_id}/audit")
async def get_stored_mod_audit_report(slug: str, mod_id: str) -> dict[str, Any]:
    """Return full static audit report and `blueprint_markdown` for a stored mod."""
    target = _find_game(slug)
    clean_slug = make_slug(str(target.get("id") or slug))
    try:
        report = _mod_auditor.get_stored_mod_audit(
            clean_slug,
            mod_id,
            app_data_root=_effective_app_data_root(),
        )
        return {
            "status": "ok",
            "slug": clean_slug,
            "mod_id": mod_id,
            "blueprint_markdown": report.get("blueprint_markdown") or "",
            "audit_report": report,
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/games/{slug}/stored-mods/{mod_id}/promote-endpoints")
async def post_promote_stored_mod_endpoints(
    slug: str,
    mod_id: str,
    req: PromoteStoredModRequest | None = None,
) -> dict[str, Any]:
    """Merge audited hooks and candidate configs into `endpoint_knowledge.json` and the game's catalog."""
    target = _find_game(slug)
    clean_slug = make_slug(str(target.get("id") or slug))
    try:
        return _mod_auditor.promote_stored_mod_endpoints(
            target,
            clean_slug,
            mod_id,
            symbols=req.symbols if req is not None else None,
            category_override=req.category_override if req is not None else "",
            app_data_root=_effective_app_data_root(),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/games/{slug}/stored-mods/{mod_id}/edit-value")
async def post_edit_stored_mod_value(
    slug: str,
    mod_id: str,
    req: EditStoredModValueRequest,
) -> dict[str, Any]:
    """Update an inline value inside an unpacked or linked stored mod's `.ini`/`.cfg`/`.lua` file."""
    target = _find_game(slug)
    clean_slug = make_slug(str(target.get("id") or slug))
    try:
        return _mod_auditor.edit_stored_mod_value(
            clean_slug,
            mod_id,
            rel_file=req.file_path,
            symbol=req.symbol,
            new_value=req.new_value,
            app_data_root=_effective_app_data_root(),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/games/{slug}/stored-mods/{mod_id}/deploy")
async def post_deploy_stored_mod(slug: str, mod_id: str) -> dict[str, Any]:
    """Create a pre-deploy snapshot and copy active stored/unpacked mod files into the game directory."""
    target = _find_game(slug)
    clean_slug = make_slug(str(target.get("id") or slug))
    try:
        return _mod_auditor.deploy_stored_mod(
            target,
            clean_slug,
            mod_id,
            app_data_root=_effective_app_data_root(),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/games/{slug}/stored-mods/{mod_id}")
async def delete_game_stored_mod(slug: str, mod_id: str) -> dict[str, Any]:
    """Delete a stored reference mod from the Mod Vault and `managed_games.json`."""
    target = _find_game(slug)
    clean_slug = make_slug(str(target.get("id") or slug))
    try:
        return _mod_auditor.delete_stored_mod(
            clean_slug,
            mod_id,
            app_data_root=_effective_app_data_root(),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
