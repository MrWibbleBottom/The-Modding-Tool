"""Primary FastAPI application entry point for The Modding Tool."""

from __future__ import annotations

import json
import mimetypes
from contextlib import asynccontextmanager

# Ensure Windows registry quirks never serve ES modules as text/plain
mimetypes.init()
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.audit import append_audit_log, log_action_error, sanitize_audit_text, sanitize_audit_value
from app.core.config import RESOURCE_ROOT, STATIC_DIR, TEMPLATES_DIR, WORKSPACE_DIR
from app.core.security import SessionTokenMiddleware, get_session_token
from app.routers import asset_audit, branding, git_sync, library, mcp_bridge, system


class SafeMimeStaticFiles(StaticFiles):
    """StaticFiles subclass that enforces JS/CSS MIME types regardless of Windows registry."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        low = path.lower()
        if low.endswith((".js", ".mjs")):
            response.headers["content-type"] = "application/javascript; charset=utf-8"
            response.headers["cache-control"] = "no-cache"
        elif low.endswith(".css"):
            response.headers["content-type"] = "text/css; charset=utf-8"
            response.headers["cache-control"] = "no-cache"
        return response


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        system.register_protocol_handler()
    except Exception:
        pass
    append_audit_log(
        action="server.startup",
        status="ok",
        summary="The Modding Tool FastAPI controller initialized on 127.0.0.1:8765",
    )
    try:
        yield
    finally:
        await mcp_bridge.mcp_manager.close()


def _default_error_hint(status_code: int) -> str:
    if status_code in (401, 403):
        return "Refresh the page or verify your session token and credentials."
    if status_code == 404:
        return "Verify the requested resource or API endpoint exists."
    if status_code == 422:
        return "Check the submitted fields and fix any validation errors."
    if status_code == 400:
        return "Check the request parameters and try again."
    if status_code in (502, 503, 504):
        return "An upstream service or local tool failed or timed out."
    if status_code >= 500:
        return "Check the Audit Log or server diagnostics for details."
    return "Review the error details and try again."


def _safe_log_api_error(
    request: Request,
    message: BaseException | str,
    *,
    status_code: int,
    detail: object = None,
) -> None:
    if not request.url.path.startswith("/api/"):
        return
    try:
        request.state._audit_error_logged = True
        log_action_error(
            source="backend",
            action=f"{request.method} {request.url.path}",
            endpoint=request.url.path,
            status_code=status_code,
            message=message,
            detail=detail,
        )
    except Exception:
        pass


def _build_api_error_payload(
    *,
    status_code: int,
    message: str,
    hint: str | None = None,
    detail: object = None,
    extra: dict | None = None,
) -> dict:
    clean_message = sanitize_audit_text(message or f"HTTP {status_code}", max_len=500) or f"HTTP {status_code}"
    clean_hint = sanitize_audit_text(hint or _default_error_hint(status_code), max_len=300)
    if detail is None:
        clean_detail: object = clean_message
    elif isinstance(detail, str):
        clean_detail = sanitize_audit_text(detail, max_len=1500) or clean_message
    else:
        clean_detail = sanitize_audit_value(detail, max_len=1500)

    payload: dict = dict(extra) if isinstance(extra, dict) else {}
    payload["ok"] = False
    payload["message"] = clean_message
    payload["hint"] = clean_hint
    payload["detail"] = clean_detail
    return payload


class ApiErrorCaptureMiddleware(BaseHTTPMiddleware):
    """Log and normalize any direct ``/api/*`` error response (``status_code >= 400``)."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if (
            not request.url.path.startswith("/api/")
            or response.status_code < 400
            or getattr(request.state, "_audit_error_logged", False)
        ):
            return response

        chunks: list[bytes] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        raw_body = b"".join(chunks)

        parsed = None
        content_type = (response.headers.get("content-type") or "").lower()
        if raw_body and ("json" in content_type or raw_body.lstrip().startswith((b"{", b"["))):
            try:
                parsed = json.loads(raw_body.decode("utf-8", errors="replace"))
            except Exception:
                parsed = None

        if isinstance(parsed, dict):
            raw_detail = (
                parsed.get("detail")
                or parsed.get("stderr")
                or parsed.get("error")
                or parsed.get("message")
                or f"HTTP {response.status_code}"
            )
            raw_msg = (
                parsed.get("message")
                or (raw_detail if isinstance(raw_detail, str) else None)
                or parsed.get("hint")
                or f"Request failed with HTTP {response.status_code}"
            )
            payload = _build_api_error_payload(
                status_code=response.status_code,
                message=str(raw_msg),
                hint=parsed.get("hint"),
                detail=raw_detail,
                extra=parsed,
            )
        else:
            raw_text = raw_body.decode("utf-8", errors="replace").strip()
            msg = raw_text or f"Request failed with HTTP {response.status_code}"
            payload = _build_api_error_payload(
                status_code=response.status_code,
                message=msg,
                detail=raw_text or f"HTTP {response.status_code}",
            )

        _safe_log_api_error(
            request,
            payload["message"],
            status_code=response.status_code,
            detail=payload["detail"],
        )

        headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower() not in ("content-length", "content-type")
        }
        return JSONResponse(status_code=response.status_code, content=payload, headers=headers)


app = FastAPI(title="The Modding Tool", lifespan=lifespan)
app.add_middleware(SessionTokenMiddleware)
app.add_middleware(ApiErrorCaptureMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.exception_handler(StarletteHTTPException)
async def api_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api/"):
        raw_detail = exc.detail if exc.detail else f"HTTP {exc.status_code}"
        msg = raw_detail if isinstance(raw_detail, str) else f"HTTP {exc.status_code}"
        if exc.status_code >= 400:
            _safe_log_api_error(
                request,
                msg,
                status_code=exc.status_code,
                detail=raw_detail,
            )
        payload = _build_api_error_payload(
            status_code=exc.status_code,
            message=msg,
            detail=raw_detail,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=payload,
            headers=getattr(exc, "headers", None),
        )
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def api_validation_exception_handler(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/api/"):
        errors = exc.errors()
        _safe_log_api_error(
            request,
            "Request validation failed",
            status_code=422,
            detail={"errors": errors},
        )
        payload = _build_api_error_payload(
            status_code=422,
            message="Request validation failed",
            detail=errors,
        )
        return JSONResponse(status_code=422, content=payload)
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(Exception)
async def api_unhandled_exception_handler(request: Request, exc: Exception):
    if request.url.path.startswith("/api/"):
        safe_msg = sanitize_audit_text(str(exc), max_len=300) or "Internal server error"
        _safe_log_api_error(request, exc, status_code=500, detail=exc)
        payload = _build_api_error_payload(
            status_code=500,
            message=safe_msg,
            detail=safe_msg,
        )
        return JSONResponse(status_code=500, content=payload)
    return HTMLResponse(content="Internal Server Error", status_code=500)

STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", SafeMimeStaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(system.router)
app.include_router(library.router)
app.include_router(asset_audit.router)
app.include_router(mcp_bridge.router)
app.include_router(git_sync.router)
app.include_router(branding.router)


def get_asset_version() -> str:
    """Compute lightweight cache-busting token from version.json + latest mtime in app/static/."""
    ver = "0.1.0"
    for candidate in (WORKSPACE_DIR / "version.json", RESOURCE_ROOT / "version.json"):
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("version"):
                    ver = str(data["version"]).strip()
                    break
            except Exception:
                pass
    latest_mtime = 0
    if STATIC_DIR.is_dir():
        for item in STATIC_DIR.rglob("*"):
            if item.is_file():
                try:
                    mtime = int(item.stat().st_mtime)
                    if mtime > latest_mtime:
                        latest_mtime = mtime
                except OSError:
                    pass
    return f"{ver}-{latest_mtime}"


@app.get("/")
async def serve_index() -> HTMLResponse:
    template_path = TEMPLATES_DIR / "index.html"
    asset_version = get_asset_version()
    html = template_path.read_text(encoding="utf-8")
    html = html.replace("__SESSION_TOKEN__", get_session_token())
    html = html.replace("{{ asset_version }}", asset_version).replace("__ASSET_VERSION__", asset_version)
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})


@app.get("/offline")
async def serve_offline() -> HTMLResponse:
    template_path = TEMPLATES_DIR / "offline.html"
    html = template_path.read_text(encoding="utf-8")
    return HTMLResponse(content=html, headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
async def serve_service_worker() -> Response:
    sw_path = STATIC_DIR / "sw.js"
    js_text = sw_path.read_text(encoding="utf-8")
    return Response(
        content=js_text,
        media_type="application/javascript; charset=utf-8",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache",
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)
