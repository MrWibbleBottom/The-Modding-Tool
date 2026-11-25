"""Windows DPAPI token encryption helpers and per-process session-token middleware."""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import os
import secrets
import sys
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

SESSION_HEADER = "X-ModTool-Session"
_DPAPI_PREFIX = "dpapi:"
_SESSION_TOKEN = os.environ.get("MODTOOL_SESSION_TOKEN") or secrets.token_urlsafe(32)


def get_session_token() -> str:
    return _SESSION_TOKEN


def ensure_within(target: Path | str, base_dir: Path | str) -> Path:
    """Return resolved *target* if it lies inside *base_dir*; raise ValueError on traversal."""
    resolved = Path(target).resolve()
    base = Path(base_dir).resolve()
    if not resolved.is_relative_to(base):
        raise ValueError(f"Path traversal blocked: '{target}' resolves outside '{base}'")
    return resolved


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def dpapi_encrypt(plaintext: str) -> str:
    """Encrypt a secret string using Windows DPAPI (CurrentUser scope).

    Returns a ``dpapi:<base64>`` string on Windows, or the input unchanged on
    non-Windows platforms / if DPAPI is unavailable.
    """
    if not plaintext or plaintext.startswith(_DPAPI_PREFIX):
        return plaintext
    if sys.platform != "win32":
        return plaintext
    try:
        raw = plaintext.encode("utf-8")
        buf = (ctypes.c_byte * len(raw)).from_buffer_copy(raw)
        blob_in = _DATA_BLOB(len(raw), buf)
        blob_out = _DATA_BLOB()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        if not crypt32.CryptProtectData(
            ctypes.byref(blob_in),
            "TheModdingTool",
            None,
            None,
            None,
            0x01,  # CRYPTPROTECT_UI_FORBIDDEN
            ctypes.byref(blob_out),
        ):
            return plaintext
        try:
            encrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            return _DPAPI_PREFIX + base64.b64encode(encrypted).decode("ascii")
        finally:
            kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return plaintext


def dpapi_decrypt(ciphertext: str) -> str:
    """Decrypt a ``dpapi:<base64>`` string encrypted with ``dpapi_encrypt``."""
    if not ciphertext:
        return ""
    if not ciphertext.startswith(_DPAPI_PREFIX):
        return ciphertext
    if sys.platform != "win32":
        return ""
    try:
        raw = base64.b64decode(ciphertext[len(_DPAPI_PREFIX) :])
        buf = (ctypes.c_byte * len(raw)).from_buffer_copy(raw)
        blob_in = _DATA_BLOB(len(raw), buf)
        blob_out = _DATA_BLOB()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        if not crypt32.CryptUnprotectData(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            0x01,  # CRYPTPROTECT_UI_FORBIDDEN
            ctypes.byref(blob_out),
        ):
            return ""
        try:
            decrypted = ctypes.string_at(blob_out.pbData, blob_out.cbData)
            return decrypted.decode("utf-8")
        finally:
            kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return ""


class SessionTokenMiddleware(BaseHTTPMiddleware):
    """Require ``X-ModTool-Session`` on mutating ``/api/*`` requests."""

    SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

    async def dispatch(self, request: Request, call_next):
        if request.method not in self.SAFE_METHODS and request.url.path.startswith("/api/"):
            token = request.headers.get(SESSION_HEADER, "")
            if not token or not secrets.compare_digest(token, _SESSION_TOKEN):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Invalid or missing session token"},
                )
        return await call_next(request)
