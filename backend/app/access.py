"""Access policy for a shared installation (this is not tenant isolation)."""

import base64
import binascii
import secrets

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import get_settings
from app.usage import consume, retry_after

CHALLENGE = {"WWW-Authenticate": 'Basic realm="VeriGraph", charset="UTF-8"'}


def authenticate(header: str) -> str | None:
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        username, password = base64.b64decode(value, validate=True).decode("utf-8").split(":", 1)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    expected = get_settings().api_users.get(username)
    if expected and secrets.compare_digest(password.encode(), expected.get_secret_value().encode()):
        return username
    return None


class AccessMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        settings = get_settings()
        request.state.username = None
        if request.method == "OPTIONS" or request.url.path == "/health":
            return await call_next(request)
        if settings.require_auth and not settings.api_users:
            return JSONResponse({"detail": "Authentication is not configured"}, status_code=503)
        protected = bool(settings.api_users) or settings.require_auth or settings.demo_read_only
        if not protected:
            return await call_next(request)
        header = request.headers.get("authorization", "")
        username = authenticate(header) if header else None
        read = request.method in {"GET", "HEAD"}
        if (header and username is None) or (username is None and not (settings.demo_read_only and read)):
            return JSONResponse({"detail": "Sign in to access VeriGraph"}, status_code=401, headers=CHALLENGE)
        request.state.username = username
        if not read:
            # Basic auth can be sent automatically by browsers; reject cross-origin writes.
            origin = request.headers.get("origin")
            allowed = {o.strip().rstrip("/") for o in settings.cors_origins.split(",")}
            if origin and origin.rstrip("/") not in allowed:
                return JSONResponse({"detail": "Origin is not allowed"}, status_code=403)
            if not await run_in_threadpool(consume, f"api:{username}", settings.api_daily_write_limit):
                return JSONResponse({"detail": "Daily write quota reached; resets at midnight UTC"},
                                    status_code=429, headers={"Retry-After": str(retry_after())})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response
