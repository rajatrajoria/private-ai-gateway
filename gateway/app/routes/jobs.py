import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app import job_store
from app.auth import require_api_key
from app.job_worker import cancel_current_job
from app.ollama_client import resolve_model_entry
from app.rate_limit import dynamic_chat_limit, limiter

router = APIRouter()


class JobMessage(BaseModel):
    role: str
    content: str


class CreateJobRequest(BaseModel):
    model: str
    messages: list[JobMessage]


@router.post("/v1/jobs", status_code=status.HTTP_202_ACCEPTED)
@limiter.limit(dynamic_chat_limit)
async def create_job(
    request: Request,
    body: CreateJobRequest,
    caller: str = Depends(require_api_key),
) -> dict:
    """Submits a request for background processing instead of waiting for it
    synchronously. Use this instead of /v1/chat for anything that might take
    more than a minute or two on real data — a synchronous HTTP call that
    long is unreliable for most clients and is hard-killed by Cloudflare's
    free-tier 100-second proxy timeout regardless of any client-side
    patience. Poll GET /v1/jobs/{job_id} for the result. `model` must be a
    real Ollama tag listed in models_registry.yaml (e.g.
    "qwen2.5:7b-instruct-q4_K_M") — not an alias."""
    entry = resolve_model_entry(body.model)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown model '{body.model}'. Check GET /v1/models for available names.",
        )

    daily_limit = entry.get("daily_limit")
    if daily_limit is not None:
        recent_count = await job_store.count_recent_jobs(caller, body.model, hours=24)
        if recent_count >= daily_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Daily limit of {daily_limit} '{body.model}' submissions reached for this API key. Try again later.",
            )

    job_id = await job_store.create_job(
        caller=caller,
        model=body.model,
        messages=[message.model_dump() for message in body.messages],
    )
    return {"job_id": job_id, "status": "queued"}


@router.get("/v1/jobs/{job_id}")
async def get_job_status(job_id: str, caller: str = Depends(require_api_key)) -> dict:
    job = await job_store.get_job(job_id)

    # Treat "exists but belongs to a different API key" the same as "doesn't
    # exist" — don't let one app confirm another app's job IDs are valid.
    if job is None or job["caller"] != caller:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    response: dict = {
        "job_id": job["id"],
        # Requests address models directly by their real Ollama tag (e.g.
        # "qwen2.5:7b-instruct-q4_K_M"), so job["model"] already IS that real
        # tag from the moment of submission — no separate resolution step.
        "model": job["model"],
        "status": job["status"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
    }
    if job["status"] == "queued":
        response["queue_position"] = await job_store.queue_position(job_id)
    elif job["status"] == "done":
        response["result"] = json.loads(job["result"])
    elif job["status"] in ("failed", "cancelled"):
        response["error"] = job["error"]
    return response


@router.delete("/v1/jobs/{job_id}")
async def cancel_job(job_id: str, caller: str = Depends(require_api_key)) -> dict:
    """Cancels a job. A 'queued' job is simply removed from the queue — it
    never started, so there's nothing to interrupt. A 'processing' job is
    actually interrupted: the in-flight Ollama request is aborted (see
    job_worker.cancel_current_job for how and why that's safe), not just
    marked cancelled while Ollama keeps computing in the background. Either
    way the worker is free to pick up the next queued job immediately —
    cancelling never blocks on or waits for anything.

    A job that has already reached 'done'/'failed'/'cancelled' can't be
    cancelled — too late, it's not running anymore."""
    job = await job_store.get_job(job_id)
    if job is None or job["caller"] != caller:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    if job["status"] == "queued":
        # Atomic compare-and-set — handles the race where the worker claims
        # this exact job (queued -> processing) in between our status check
        # above and this call. If that happens, cancel_if_queued reports
        # False and we fall through to the processing-cancel path below.
        if await job_store.cancel_if_queued(job_id):
            return {"job_id": job_id, "status": "cancelled"}
        job = await job_store.get_job(job_id)

    if job["status"] == "processing":
        if cancel_current_job(job_id):
            return {"job_id": job_id, "status": "cancelling"}
        # Lost a race against the job finishing on its own just now.
        job = await job_store.get_job(job_id)
        if job["status"] == "processing":
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Job is processing but is not tracked as the active job; this should not happen.",
            )

    if job["status"] in ("done", "failed", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job already {job['status']}, cannot cancel.",
        )
    return {"job_id": job_id, "status": job["status"]}
