from pathlib import Path
from typing import Any

import httpx
import yaml

from app.config import get_settings

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


def resolve_model_entry(logical_name: str) -> dict[str, str] | None:
    return load_registry().get(logical_name)


def with_system_prompt(entry: dict[str, str], messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Prepends the registry entry's default system_prompt unless the caller
    already supplied their own system message. Shared by the synchronous
    /v1/chat route and the background job worker so the two paths can never
    drift apart in behavior."""
    system_prompt = entry.get("system_prompt")
    has_system_message = any(m["role"] == "system" for m in messages)
    if system_prompt and not has_system_message:
        return [{"role": "system", "content": system_prompt}] + messages
    return messages


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


async def is_ollama_reachable() -> bool:
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{settings.ollama_base_url}/api/tags")
            return response.status_code == 200
    except httpx.HTTPError:
        return False
