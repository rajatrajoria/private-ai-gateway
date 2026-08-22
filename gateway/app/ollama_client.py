import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import yaml

from app.config import get_settings

logger = logging.getLogger(__name__)

_REGISTRY_PATH = Path(__file__).parent / "models_registry.yaml"

# CPU inference on a 7B model with a large context window can genuinely take
# several minutes: at the full OLLAMA_NUM_CTX (16384 tokens) and observed
# prompt-processing speeds (~25-30 tok/s on this hardware), just reading the
# prompt can take ~10 minutes before generation even starts. 300s was cutting
# real requests off mid-processing — this leaves real margin above that.
_CHAT_TIMEOUT_SECONDS = 1200.0


def load_registry() -> dict[str, dict[str, str]]:
    with _REGISTRY_PATH.open() as f:
        return yaml.safe_load(f) or {}


def resolve_model_entry(model_tag: str) -> dict[str, str] | None:
    """Looks up a real Ollama tag in the allowlist. Returns None if it's not
    listed — a model must be in models_registry.yaml to be callable at all."""
    return load_registry().get(model_tag)


async def chat(model_tag: str, messages: list[dict[str, str]]) -> dict[str, Any]:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=_CHAT_TIMEOUT_SECONDS) as client:
        response = await client.post(
            f"{settings.ollama_base_url}/api/chat",
            json={
                "model": model_tag,
                "messages": messages,
                "stream": False,
                # Pin thread count to match the ollama container's cgroup CPU
                # quota. Without this, llama.cpp auto-detects the host's full
                # core count and oversubscribes threads against a smaller
                # enforced budget, causing severe throttling contention.
                "options": {
                    "num_thread": settings.ollama_num_thread,
                    "num_ctx": settings.ollama_num_ctx,
                },
            },
        )
        response.raise_for_status()
        return response.json()


async def chat_stream(model_tag: str, messages: list[dict[str, str]]) -> AsyncIterator[dict[str, Any]]:
    """Same request as chat(), but with Ollama's own stream:true — yields one
    parsed JSON object per token/chunk as Ollama emits them (newline-delimited
    JSON, not SSE, on Ollama's side; routes/chat.py is what wraps this into
    actual SSE for the caller). The last chunk has "done": true."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=_CHAT_TIMEOUT_SECONDS) as client:
        async with client.stream(
            "POST",
            f"{settings.ollama_base_url}/api/chat",
            json={
                "model": model_tag,
                "messages": messages,
                "stream": True,
                "options": {
                    "num_thread": settings.ollama_num_thread,
                    "num_ctx": settings.ollama_num_ctx,
                },
            },
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.strip():
                    yield json.loads(line)


async def warm_up_first_model() -> None:
    """Fired once at startup as a background task (see main.py's lifespan) —
    never awaited by it, so a slow or failing warm-up can't delay the gateway
    becoming ready. Sends one throwaway request so the first *real* request
    doesn't pay Ollama's cold-load penalty (loading a 7B model's weights into
    RAM took ~20s+ in testing on this hardware).

    Deliberately warms only the FIRST model listed in the registry, not all
    of them: OLLAMA_MAX_LOADED_MODELS=1 (see docker-compose.yml) means only
    one model can be resident in RAM at a time, so warming a second model
    would immediately evict the first, wasting the first warm-up entirely
    for whichever model actually gets used first in practice."""
    registry = load_registry()
    if not registry:
        return
    first_model = next(iter(registry))
    try:
        logger.info("Warming up model '%s'...", first_model)
        await chat(first_model, [{"role": "user", "content": "Hi"}])
        logger.info("Warm-up complete for '%s'.", first_model)
    except Exception:
        logger.warning(
            "Warm-up request for '%s' failed — not fatal, the first real "
            "request will just pay the cold-load cost instead.",
            first_model,
            exc_info=True,
        )


async def is_ollama_reachable() -> bool:
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{settings.ollama_base_url}/api/tags")
            return response.status_code == 200
    except httpx.HTTPError:
        return False
