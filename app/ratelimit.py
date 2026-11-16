"""Simple in-memory sliding-window rate limiter (swap for Redis in production)."""
import threading
import time
from collections import defaultdict, deque

from fastapi import Request

from app.config import IS_TEST
from app.errors import RATE_LIMITED, APIError

_lock = threading.Lock()
_hits: dict[str, deque] = defaultdict(deque)
ENABLED = not IS_TEST


def check(key: str, limit: int, window_seconds: int) -> bool:
    if not ENABLED:
        return True
    now = time.monotonic()
    with _lock:
        q = _hits[key]
        while q and now - q[0] > window_seconds:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def enforce(request: Request, bucket: str, limit: int = 30, window_seconds: int = 60, key: str | None = None):
    """Raise APIError(429) if over limit. bucket names the endpoint; key defaults to client IP."""
    k = f"{bucket}:{key or client_ip(request)}"
    if not check(k, limit, window_seconds):
        raise APIError(RATE_LIMITED, "Too many requests. Please slow down.", 429)
