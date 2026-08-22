from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import get_settings


def _rate_limit_key(request: Request) -> str:
    """Rate-limit per API key, not per IP. Every caller reaches this server
    through the same Cloudflare Tunnel, so IP-based limiting would lump every
    app together as one 'source'. Falling back to remote address only covers
    the unauthenticated case (missing key), which auth.py rejects anyway."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:]
    return get_remote_address(request)


limiter = Limiter(key_func=_rate_limit_key)


def dynamic_chat_limit() -> str:
    """Read the limit from settings at call time (not import time) so it's
    read fresh from .env / env vars, matching how get_settings() is used elsewhere."""
    return f"{get_settings().rate_limit_per_minute}/minute"
