"""Thread-safe in-memory background job runner with 30-minute completed-job pruning."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_JOB_TTL_SECONDS = 30 * 60
_TERMINAL_STATUSES = frozenset({"done", "completed", "error", "failed"})
_UNSET = object()


def _public_view(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "kind": job.get("kind", ""),
        "status": job["status"],
        "percent": job["percent"],
        "stage": job["stage"],
        "log_lines": list(job.get("log_lines", [])),
        "result": job.get("result"),
        "error": job.get("error"),
        "created_at": job.get("created_at"),
        "finished_at": job.get("finished_at"),
    }


def prune_expired_jobs(*, max_age_seconds: float = _JOB_TTL_SECONDS, now_monotonic: float | None = None) -> int:
    """Remove completed/failed jobs older than *max_age_seconds*; return number pruned."""
    now_m = time.monotonic() if now_monotonic is None else now_monotonic
    with _LOCK:
        expired = [
            jid
            for jid, job in _JOBS.items()
            if job.get("status") in _TERMINAL_STATUSES
            and job.get("_finished_mono") is not None
            and (now_m - float(job["_finished_mono"])) >= max_age_seconds
        ]
        for jid in expired:
            _JOBS.pop(jid, None)
        return len(expired)


def create_job(kind: str = "", stage: str = "Queued...") -> dict[str, Any]:
    """Create and register a running background job."""
    prune_expired_jobs()
    job_id = uuid.uuid4().hex[:16]
    job = {
        "job_id": job_id,
        "kind": str(kind or "").strip(),
        "status": "running",
        "percent": 0,
        "stage": str(stage or "Starting..."),
        "log_lines": [],
        "result": None,
        "error": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None,
        "_finished_mono": None,
    }
    with _LOCK:
        _JOBS[job_id] = job
        return _public_view(job)


def update_job(
    job_id: str,
    *,
    status: str | None = None,
    percent: int | float | None = None,
    stage: str | None = None,
    log_line: str | None = None,
    result: Any = _UNSET,
    error: str | None = None,
) -> dict[str, Any] | None:
    """Update progress, logs, status, or result for *job_id*."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        if percent is not None:
            job["percent"] = max(0, min(100, int(round(float(percent)))))
        if stage is not None:
            job["stage"] = str(stage)
        if log_line is not None and str(log_line).strip():
            lines = job.setdefault("log_lines", [])
            lines.append(str(log_line).rstrip())
            if len(lines) > 250:
                del lines[:-250]
        if result is not _UNSET:
            job["result"] = result
        if error is not None:
            job["error"] = str(error)
        if status is not None:
            norm_status = str(status).strip().lower()
            job["status"] = norm_status
            if norm_status in _TERMINAL_STATUSES and job.get("_finished_mono") is None:
                job["finished_at"] = datetime.now(timezone.utc).isoformat()
                job["_finished_mono"] = time.monotonic()
                if norm_status in ("done", "completed") and percent is None:
                    job["percent"] = 100
        return _public_view(job)


def get_job(job_id: str) -> dict[str, Any] | None:
    """Return current job snapshot for *job_id* (or None if missing/expired)."""
    prune_expired_jobs()
    with _LOCK:
        job = _JOBS.get(str(job_id or "").strip())
        return _public_view(job) if job is not None else None


def start_background_job(
    kind: str,
    worker: Callable[[Callable[..., dict[str, Any] | None]], Any],
    *,
    initial_stage: str = "Starting...",
) -> dict[str, Any]:
    """Run synchronous or coroutine *worker(report)* on a background thread and return job info."""
    job = create_job(kind=kind, stage=initial_stage)
    job_id = job["job_id"]

    def _report(**kwargs: Any) -> dict[str, Any] | None:
        return update_job(job_id, **kwargs)

    def _runner() -> None:
        try:
            outcome = worker(_report)
            if asyncio.iscoroutine(outcome):
                outcome = asyncio.run(outcome)
            update_job(
                job_id,
                status="done",
                percent=100,
                stage=outcome.get("summary", "Completed") if isinstance(outcome, dict) else "Completed",
                result=outcome,
            )
        except Exception as exc:
            detail = getattr(exc, "detail", None) or str(exc)
            update_job(
                job_id,
                status="error",
                stage=f"Failed: {detail}",
                error=str(detail),
                log_line=f"[ERROR] {detail}",
            )

    t = threading.Thread(target=_runner, name=f"job-{kind or job_id}", daemon=True)
    t.start()
    return job
