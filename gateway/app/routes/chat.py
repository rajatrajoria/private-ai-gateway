import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.auth import require_api_key
from app.ollama_client import chat as ollama_chat
from app.ollama_client import resolve_model_entry, with_system_prompt
from app.rate_limit import dynamic_chat_limit, limiter

router = APIRouter()


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False  # reserved for future SSE support; not implemented yet


@router.post("/v1/chat")
@limiter.limit(dynamic_chat_limit)
async def chat_endpoint(
    request: Request,
    body: ChatRequest,
    caller: str = Depends(require_api_key),
) -> dict:
    if body.stream:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="stream=true is not implemented yet; use stream=false",
        )

    entry = resolve_model_entry(body.model)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown model '{body.model}'. Check GET /v1/models for available names.",
        )

    messages = with_system_prompt(entry, [message.model_dump() for message in body.messages])

    try:
        result = await ollama_chat(entry["ollama_tag"], messages)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ollama backend error: {exc}",
        ) from exc

    return {
        "model": body.model,
        "message": result.get("message", {}),
    }
