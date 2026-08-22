import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app import job_store
from app.config import get_settings
from app.job_worker import run_worker_loop
from app.ollama_client import is_ollama_reachable
from app.rate_limit import limiter
from app.routes import chat, jobs, models

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await job_store.init_db()
    # Any job still marked "processing" belongs to a worker that no longer
    # exists (the container restarted) — fail it explicitly rather than
    # leaving a caller polling forever for a job that will never resume.
    # Jobs still "queued" are untouched: nothing was lost for them.
    await job_store.mark_stale_processing_failed()
    worker_task = asyncio.create_task(run_worker_loop())
    yield
    worker_task.cancel()


app = FastAPI(title="Private AI Gateway", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(models.router)
app.include_router(chat.router)
app.include_router(jobs.router)


@app.get("/healthz")
async def healthz() -> dict:
    """Unauthenticated on purpose: this is a plain liveness probe (used by
    Docker's healthcheck and by you to sanity-check the stack), not part of
    the API surface that serves model responses."""
    return {"status": "ok", "ollama_reachable": await is_ollama_reachable()}
