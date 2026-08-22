import asyncio
import json
import logging

import httpx

from app import job_store
from app.ollama_client import chat as ollama_chat
from app.ollama_client import resolve_model_entry, with_system_prompt

logger = logging.getLogger(__name__)

# How often to check for a new job when the queue is empty. Short enough that
# a freshly submitted job starts promptly, long enough not to hammer SQLite.
_POLL_INTERVAL_SECONDS = 2.0


async def run_worker_loop() -> None:
    """Single-consumer loop: claim the oldest queued job, run it to
    completion, then look for the next one. Deliberately one job at a time —
    Ollama itself only processes one request at a time (OLLAMA_NUM_PARALLEL=1),
    so running more than one job concurrently here would just mean multiple
    of our own requests piling up in Ollama's internal queue instead of ours,
    with no visibility into their position. Keeping exactly one job in flight
    on our side keeps queue position (job_store.queue_position) accurate."""
    while True:
        job = await job_store.claim_next_job()
        if job is None:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            continue
        await _process_job(job)


async def _process_job(job: dict) -> None:
    entry = resolve_model_entry(job["model"])
    if entry is None:
        await job_store.finish_job(job["id"], "failed", error=f"Unknown model '{job['model']}'")
        return

    messages = with_system_prompt(entry, json.loads(job["messages"]))
    await job_store.set_ollama_tag(job["id"], entry["ollama_tag"])

    try:
        result = await ollama_chat(entry["ollama_tag"], messages)
        await job_store.finish_job(job["id"], "done", result={"message": result.get("message", {})})
    except httpx.HTTPError as exc:
        logger.warning("Job %s failed: %s", job["id"], exc)
        await job_store.finish_job(job["id"], "failed", error=f"Ollama backend error: {exc}")
