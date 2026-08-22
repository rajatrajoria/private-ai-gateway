import hmac

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings

bearer_scheme = HTTPBearer(auto_error=False)


def require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str:
    """FastAPI dependency: validates the Bearer token and returns the caller's
    app name (from API_KEYS=name:key,...) so routes can log/rate-limit per caller.

    Uses hmac.compare_digest instead of `==` for every comparison: a plain string
    equality check returns as soon as the first differing character is found, so
    an attacker measuring response times can recover the key one character at a
    time. compare_digest always takes the same time regardless of where strings
    diverge, which closes that side channel.
    """
    settings = get_settings()
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    provided = credentials.credentials
    for known_key, app_name in settings.api_keys_by_secret.items():
        if hmac.compare_digest(known_key, provided):
            return app_name

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key",
        headers={"WWW-Authenticate": "Bearer"},
    )
