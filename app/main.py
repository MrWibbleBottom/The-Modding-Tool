"""Primary FastAPI application entry point for ModToolPoC."""

from __future__ import annotations

import mimetypes
from contextlib import asynccontextmanager

# Ensure Windows registry quirks never serve ES modules as text/plain
mimetypes.init()
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.core.audit import append_audit_log
from app.core.config import POC_DIR, STATIC_DIR, TEMPLATES_DIR
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
    append_audit_log(
        action="server.startup",
        status="ok",
        summary="PoC FastAPI controller initialized on 127.0.0.1:8765",
    )
    try:
        yield
    finally:
        await mcp_bridge.mcp_manager.close()


app = FastAPI(title="Battlemarked IL2CPP Modding PoC", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(SessionTokenMiddleware)

STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", SafeMimeStaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(system.router)
app.include_router(library.router)
app.include_router(asset_audit.router)
app.include_router(mcp_bridge.router)
app.include_router(git_sync.router)
app.include_router(branding.router)


@app.get("/")
async def serve_index() -> HTMLResponse:
    template_path = TEMPLATES_DIR / "index.html"
    if not template_path.is_file():
        template_path = POC_DIR / "index.html"
    html = template_path.read_text(encoding="utf-8")
    html = html.replace("__SESSION_TOKEN__", get_session_token())
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)
