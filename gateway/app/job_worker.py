import asyncio
import json
import logging

import httpx

from app import job_store
from app.ollama_client import chat as ollama_chat
from app.ollama_client import resolve_model_entry

logger = logging.getLogger(__name__)

# How often to check for a new job when the queue is empty. Short enough that
# a freshly submitted job starts promptly, long enough not to hammer SQLite.
_POLL_INTERVAL_SECONDS = 2.0

# The single job currently being processed, if any — tracked so DELETE
# /v1/jobs/{id} can find and cancel an in-flight job from a completely
# different asyncio context (the HTTP request handler). We deliberately run
# _process_job as its OWN task (not just awaited inline in run_worker_loop)
# so that cancelling it only aborts that one job's Ollama call — the outer
# while-loop task itself is never touched, so it immediately moves on to
# claim the next queued job the moment this one ends, cancelled or not.
_current_job_id: str | None = None
_current_job_task: asyncio.Task | None = None


async def run_worker_loop() -> None:
    """Single-consumer loop: claim the oldest queued job, run it to
    completion, then look for the next one. Deliberately one job at a time —
    Ollama itself only processes one request at a time (OLLAMA_NUM_PARALLEL=1),
    so running more than one job concurrently here would just mean multiple
    of our own requests piling up in Ollama's internal queue instead of ours,
    with no visibility into their position. Keeping exactly one job in flight
    on our side keeps queue position (job_store.queue_position) accurate."""
    global _current_job_id, _current_job_task
    while True:
        job = await job_store.claim_next_job()
        if job is None:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            continue

        _current_job_id = job["id"]
        _current_job_task = asyncio.create_task(_process_job(job))
        try:
            await _current_job_task
        except asyncio.CancelledError:
            # _process_job already recorded the "cancelled" status itself
            # (see below) before re-raising — this is just where that
            # CancelledError surfaces on our side. Swallowing it here, rather
            # than letting it propagate further, is exactly what keeps this
            # while-loop alive to pick up the next job immediately.
            pass
        finally:
            _current_job_id = None
            _current_job_task = None


def cancel_current_job(job_id: str) -> bool:
    """Called from the DELETE /v1/jobs/{id} request handler, not from the
    worker loop itself. Returns True if `job_id` was actually the job
    currently in flight (and its task was just cancelled), False otherwise
    (already finished, or never started) so the caller can react correctly.

    Cancelling the task raises asyncio.CancelledError inside _process_job at
    its next await point — which is the httpx call to Ollama — closing that
    connection. In testing, Ollama's server does notice the dropped
    connection and stops generating for that request rather than continuing
    to burn CPU in the background, which is what actually lets the next
    queued job start immediately instead of waiting behind an orphaned
    generation still occupying Ollama's one processing slot
    (OLLAMA_NUM_PARALLEL=1)."""
    if _current_job_id == job_id and _current_job_task is not None:
        _current_job_task.cancel()
        return True
    return False


async def _process_job(job: dict) -> None:
    if resolve_model_entry(job["model"]) is None:
        await job_store.finish_job(job["id"], "failed", error=f"Unknown model '{job['model']}'")
        return

    # Pure passthrough — messages go to Ollama exactly as the caller sent
    # them, nothing added or rewritten.
    messages = json.loads(job["messages"])

    try:
        result = await ollama_chat(job["model"], messages)
        await job_store.finish_job(job["id"], "done", result={"message": result.get("message", {})})
    except asyncio.CancelledError:
        await job_store.finish_job(job["id"], "cancelled", error="Cancelled by user request.")
        # Re-raise so asyncio's own bookkeeping treats this task as properly
        # cancelled (not as having "completed" while swallowing the signal) —
        # run_worker_loop is the one place that actually catches it.
        raise
    except httpx.HTTPError as exc:
        logger.warning("Job %s failed: %s", job["id"], exc)
        await job_store.finish_job(job["id"], "failed", error=f"Ollama backend error: {exc}")
