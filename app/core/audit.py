"""JSONL audit logging for game and application actions."""

from __future__ import annotations

import json
import re
import traceback
from datetime import datetime, timezone
from typing import Any

from app.core.config import (
    AUDIT_APP_LOG_PATH,
    AUDIT_GAME_LOG_PATH,
    AUDIT_LOG_PATH,
    sha256_text,
)

AUDIT_LOG_CACHE: list[dict[str, Any]] | None = None

_SENSITIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{6,}\b"), "***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{6,}\b"), "***"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{6,}\b"), "***"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic|token)\s+)\S+"), r"\1***"),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9\-._~+/]+=*"), r"\1***"),
    (re.compile(r"(?i)(http\.extraheader=)\S+"), r"\1***"),
    (re.compile(r"(https?://)[^:/@\s]+:[^/@\s]+@"), r"\1***@"),
    (
        re.compile(
            r"""(?i)(["']?(?:token|api[_-]?key|secret|password|passwd|session[_-]?token|x-modtool-session|x-session-token|access[_-]?token|refresh[_-]?token)["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*')"""
        ),
        r'\1"***"',
    ),
    (
        re.compile(
            r"""(?i)\b(token|api[_-]?key|secret|password|passwd|session[_-]?token|x-modtool-session|x-session-token|access[_-]?token|refresh[_-]?token)(\s*[:=]\s*)([^\s,;'"}\]]+)"""
        ),
        r"\1\2***",
    ),
)

_SENSITIVE_KEYS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "password",
        "passwd",
        "secret",
        "client_secret",
        "private_key",
        "api_key",
        "apikey",
        "x-api-key",
        "authorization",
        "x-modtool-session",
        "x-session-token",
        "session_token",
        "cookie",
        "set-cookie",
    }
)


def _classify_log_action(action: str) -> str:
    low = (action or "").strip().lower()
    if low.startswith(("app.", "app_ui.", "pc_specs.", "action_failed", "client.")):
        return "app"
    return "game"


def sanitize_audit_text(text: Any, max_len: int = 2000) -> str:
    """Redact credentials/tokens from audit text and cap its length."""
    cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    for pattern, replacement in _SENSITIVE_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = cleaned.strip()
    if max_len > 0 and len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + "... [truncated]"
    return cleaned


def sanitize_audit_value(value: Any, *, max_len: int = 2000, depth: int = 0) -> Any:
    """Recursively sanitize nested audit details values."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return sanitize_audit_text(value, max_len=max_len)
    if depth >= 6:
        return sanitize_audit_text(value, max_len=max_len)
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for idx, (k, v) in enumerate(value.items()):
            if idx >= 50:
                sanitized["_truncated_keys"] = len(value) - 50
                break
            key_str = sanitize_audit_text(k, max_len=200)
            if key_str.lower() in _SENSITIVE_KEYS:
                sanitized[key_str] = "***" if v else ""
            else:
                sanitized[key_str] = sanitize_audit_value(v, max_len=max_len, depth=depth + 1)
        return sanitized
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        out = [sanitize_audit_value(item, max_len=max_len, depth=depth + 1) for item in items[:50]]
        if len(items) > 50:
            out.append(f"... [{len(items) - 50} more items]")
        return out
    return sanitize_audit_text(value, max_len=max_len)


def clear_audit_log_cache() -> None:
    global AUDIT_LOG_CACHE
    AUDIT_LOG_CACHE = None


def append_audit_log(
    action: str,
    status: str,
    summary: str,
    sha256: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    global AUDIT_LOG_CACHE
    category = _classify_log_action(action)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "category": category,
        "action": action,
        "status": status,
        "summary": summary,
        "sha256": sha256 or sha256_text(f"{action}:{status}:{summary}"),
        "details": details or {},
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG_PATH.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
    cat_path = AUDIT_APP_LOG_PATH if category == "app" else AUDIT_GAME_LOG_PATH
    cat_path.parent.mkdir(parents=True, exist_ok=True)
    with cat_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
    AUDIT_LOG_CACHE = None
    return entry


def log_action_error(
    source: str,
    action: str,
    endpoint: str,
    status_code: int | None,
    message: Any,
    detail: Any = None,
) -> dict[str, Any]:
    """Redact secrets and append a structured ``ACTION_FAILED`` entry to ``audit.jsonl``."""
    clean_source = sanitize_audit_text(source or "unknown", max_len=200) or "unknown"
    clean_action = sanitize_audit_text(action or "unknown", max_len=200) or "unknown"
    clean_endpoint = sanitize_audit_text(endpoint or "", max_len=300)
    clean_status = int(status_code) if status_code is not None else None

    if isinstance(message, BaseException):
        raw_msg = str(message) or type(message).__name__
        exc_obj: BaseException | None = message
    else:
        raw_msg = str(message or "")
        exc_obj = detail if isinstance(detail, BaseException) else None

    clean_message = sanitize_audit_text(raw_msg, max_len=1500) or "Action failed"

    if isinstance(detail, BaseException):
        clean_detail: Any = sanitize_audit_text(str(detail) or type(detail).__name__, max_len=2000)
    elif detail is not None:
        clean_detail = sanitize_audit_value(detail, max_len=2000)
    else:
        clean_detail = None

    status_part = f" ({clean_status})" if clean_status is not None else ""
    endpoint_part = f" {clean_endpoint}" if clean_endpoint else ""
    clean_summary = sanitize_audit_text(
        f"[{clean_source}] {clean_action}{endpoint_part}{status_part}: {clean_message}",
        max_len=500,
    )

    merged_details: dict[str, Any] = {
        "source": clean_source,
        "action": clean_action,
        "endpoint": clean_endpoint,
        "status_code": clean_status,
        "message": clean_message,
    }
    if clean_detail is not None and clean_detail != "":
        merged_details["detail"] = clean_detail
    if exc_obj is not None and exc_obj.__traceback__ is not None:
        tb_text = "".join(traceback.format_exception(type(exc_obj), exc_obj, exc_obj.__traceback__))
        merged_details["traceback"] = sanitize_audit_text(tb_text[-2000:], max_len=1500)

    return append_audit_log(
        action="ACTION_FAILED",
        status="error",
        summary=clean_summary,
        details=merged_details,
    )


def read_audit_logs(limit: int = 50, category: str = "all") -> list[dict[str, Any]]:
    global AUDIT_LOG_CACHE
    if AUDIT_LOG_CACHE is None:
        if not AUDIT_LOG_PATH.is_file():
            AUDIT_LOG_CACHE = []
        else:
            lines = AUDIT_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
            entries: list[dict[str, Any]] = []
            for raw in lines[-400:]:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                    if "category" not in item:
                        item["category"] = _classify_log_action(item.get("action", ""))
                    entries.append(item)
                except json.JSONDecodeError:
                    continue
            AUDIT_LOG_CACHE = list(reversed(entries))

    cat = (category or "all").strip().lower()
    if cat in ("game", "app"):
        return [e for e in AUDIT_LOG_CACHE if e.get("category") == cat][:limit]
    return AUDIT_LOG_CACHE[:limit]
