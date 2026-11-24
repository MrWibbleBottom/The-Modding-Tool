"""JSONL audit logging for game and application actions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.core.config import (
    AUDIT_APP_LOG_PATH,
    AUDIT_GAME_LOG_PATH,
    AUDIT_LOG_PATH,
    sha256_text,
)

AUDIT_LOG_CACHE: list[dict[str, Any]] | None = None


def _classify_log_action(action: str) -> str:
    low = (action or "").strip().lower()
    if low.startswith(("app.", "app_ui.", "pc_specs.")):
        return "app"
    return "game"


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
    with AUDIT_LOG_PATH.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
    cat_path = AUDIT_APP_LOG_PATH if category == "app" else AUDIT_GAME_LOG_PATH
    with cat_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
    AUDIT_LOG_CACHE = None
    return entry


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
