import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app import job_store
from app.auth import require_api_key
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
    more than a minute or two (e.g. the "insights" model on a real payload) —
    a synchronous HTTP call that long is unreliable for most clients and is
    hard-killed by Cloudflare's free-tier 100-second proxy timeout regardless
    of any client-side patience. Poll GET /v1/jobs/{job_id} for the result."""
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
    elif job["status"] == "failed":
        response["error"] = job["error"]
    return response
