import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Lives on a mounted volume (see docker-compose.yml) so job records survive
# `docker compose restart gateway` — an in-memory dict would silently lose
# every queued/in-flight job on any container restart, leaving callers
# polling for job IDs that no longer exist anywhere.
_DB_PATH = Path("/data/jobs.db")

# sqlite3 is a blocking C extension; every call here runs through
# asyncio.to_thread so it never blocks the event loop that's also serving
# HTTP requests and the background worker loop.


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _init_db_sync() -> None:
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            caller TEXT NOT NULL,
            model TEXT NOT NULL,
            messages TEXT NOT NULL,
            status TEXT NOT NULL,
            result TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


async def init_db() -> None:
    await asyncio.to_thread(_init_db_sync)


def _mark_stale_processing_failed_sync() -> None:
    """A job stuck in 'processing' means the worker died mid-job (container
    restart, crash) — it will never resume, so callers polling for it should
    get a clear failure instead of waiting forever. Jobs still 'queued' are
    untouched: nothing was lost for them, the worker just picks them up again."""
    conn = _connect()
    conn.execute(
        "UPDATE jobs SET status = 'failed', error = ?, finished_at = ? WHERE status = 'processing'",
        ("Interrupted by a server restart before this job finished.", _now()),
    )
    conn.commit()
    conn.close()


async def mark_stale_processing_failed() -> None:
    await asyncio.to_thread(_mark_stale_processing_failed_sync)


def _create_job_sync(job_id: str, caller: str, model: str, messages: list[dict]) -> None:
    conn = _connect()
    conn.execute(
        "INSERT INTO jobs (id, caller, model, messages, status, created_at) VALUES (?, ?, ?, ?, 'queued', ?)",
        (job_id, caller, model, json.dumps(messages), _now()),
    )
    conn.commit()
    conn.close()


async def create_job(caller: str, model: str, messages: list[dict]) -> str:
    job_id = str(uuid.uuid4())
    await asyncio.to_thread(_create_job_sync, job_id, caller, model, messages)
    return job_id


def _count_recent_jobs_sync(caller: str, model: str, since_iso: str) -> int:
    conn = _connect()
    count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE caller = ? AND model = ? AND created_at >= ?",
        (caller, model, since_iso),
    ).fetchone()[0]
    conn.close()
    return count


async def count_recent_jobs(caller: str, model: str, hours: float) -> int:
    """How many jobs this caller has submitted for this model in the last
    `hours` — a rolling window, not a calendar-day count, so it can't be
    gamed by bursting right before/after midnight."""
    since_iso = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return await asyncio.to_thread(_count_recent_jobs_sync, caller, model, since_iso)


def _get_job_sync(job_id: str) -> dict | None:
    conn = _connect()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


async def get_job(job_id: str) -> dict | None:
    return await asyncio.to_thread(_get_job_sync, job_id)


def _queue_position_sync(job_id: str) -> int | None:
    conn = _connect()
    row = conn.execute("SELECT created_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        conn.close()
        return None
    count = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'queued' AND created_at < ?",
        (row["created_at"],),
    ).fetchone()[0]
    conn.close()
    return count


async def queue_position(job_id: str) -> int | None:
    """How many other queued jobs are ahead of this one — 0 means next up."""
    return await asyncio.to_thread(_queue_position_sync, job_id)


def _claim_next_job_sync() -> dict | None:
    """Picks the single oldest queued job and marks it processing. Only ever
    called from the one worker loop (see job_worker.py), so there's no
    multi-worker race to guard against here."""
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    if row is None:
        conn.close()
        return None
    conn.execute(
        "UPDATE jobs SET status = 'processing', started_at = ? WHERE id = ?",
        (_now(), row["id"]),
    )
    conn.commit()
    job = dict(row)
    conn.close()
    return job


async def claim_next_job() -> dict | None:
    return await asyncio.to_thread(_claim_next_job_sync)


def _finish_job_sync(job_id: str, status: str, result: dict | None, error: str | None) -> None:
    conn = _connect()
    conn.execute(
        "UPDATE jobs SET status = ?, result = ?, error = ?, finished_at = ? WHERE id = ?",
        (status, json.dumps(result) if result is not None else None, error, _now(), job_id),
    )
    conn.commit()
    conn.close()


async def finish_job(job_id: str, status: str, result: dict | None = None, error: str | None = None) -> None:
    await asyncio.to_thread(_finish_job_sync, job_id, status, result, error)


def _cancel_if_queued_sync(job_id: str) -> bool:
    """Atomic compare-and-set: only cancels if it's still 'queued' at the
    exact moment this runs. Guards the race where the worker claims the job
    (queued -> processing) between the API handler's initial status check and
    this call — without the WHERE clause, we could stomp a status the worker
    just set, silently losing a job that was actually already running."""
    conn = _connect()
    cursor = conn.execute(
        "UPDATE jobs SET status = 'cancelled', error = ?, finished_at = ? WHERE id = ? AND status = 'queued'",
        ("Cancelled by user request.", _now(), job_id),
    )
    conn.commit()
    cancelled = cursor.rowcount > 0
    conn.close()
    return cancelled


async def cancel_if_queued(job_id: str) -> bool:
    """Returns True if the job was queued and is now cancelled; False if it
    had already left the queued state (already processing/done/etc)."""
    return await asyncio.to_thread(_cancel_if_queued_sync, job_id)
