import json

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.auth import require_api_key
from app.ollama_client import chat as ollama_chat
from app.ollama_client import chat_stream as ollama_chat_stream
from app.ollama_client import resolve_model_entry
from app.rate_limit import dynamic_chat_limit, limiter

router = APIRouter()


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False


async def _sse_event_stream(model_tag: str, messages: list[dict]):
    """Server-Sent Events framing around Ollama's own newline-delimited JSON
    stream. Each event's data is a JSON object shaped like the chunks Ollama
    itself emits (`{"message": {"role": "assistant", "content": "..."}, "done": false}`),
    so a client only has to parse `data:` lines, not learn a second schema.
    Ends with a literal `data: [DONE]` sentinel (an OpenAI-style convention,
    not part of Ollama's own protocol) so clients have an unambiguous
    end-of-stream marker distinct from the last real chunk."""
    try:
        async for chunk in ollama_chat_stream(model_tag, messages):
            yield f"data: {json.dumps(chunk)}\n\n"
            if chunk.get("done"):
                break
        yield "data: [DONE]\n\n"
    except httpx.HTTPError as exc:
        # The response has already started (200 + headers went out the
        # moment StreamingResponse began), so a backend error can't become an
        # HTTP 502 at this point — the status code is long since committed.
        # Instead it's reported as one more SSE event the client can check
        # for, the same way OpenAI-compatible streaming APIs do this.
        yield f"data: {json.dumps({'error': f'Ollama backend error: {exc}'})}\n\n"


@router.post("/v1/chat")
@limiter.limit(dynamic_chat_limit)
async def chat_endpoint(
    request: Request,
    body: ChatRequest,
    caller: str = Depends(require_api_key),
):
    if resolve_model_entry(body.model) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown model '{body.model}'. Check GET /v1/models for available names.",
        )

    # Pure passthrough — messages go to Ollama exactly as the caller sent
    # them, nothing added or rewritten.
    messages = [message.model_dump() for message in body.messages]

    if body.stream:
        return StreamingResponse(
            _sse_event_stream(body.model, messages),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        result = await ollama_chat(body.model, messages)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ollama backend error: {exc}",
        ) from exc

    return {
        "model": body.model,
        "message": result.get("message", {}),
    }
