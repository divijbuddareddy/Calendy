"""Request id, structured access logging, CSRF and security headers."""
import json
import logging
import re
import secrets
import time
from urllib.parse import parse_qs

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from app.config import IS_TEST

log = logging.getLogger("access")

# Paths that don't use cookie auth (API keys / signatures / public JSON) are CSRF-exempt.
CSRF_EXEMPT_PREFIXES = (
    "/api/v1/",  # API-key authenticated or public JSON (rate limited, no cookie auth)
    "/webhooks/",  # provider webhooks (signature verified)
    "/scim/",  # SCIM bearer token
    "/sso/",  # SAML ACS: cross-site IdP POST, authenticated by the signed assertion + InResponseTo
    "/oauth/token",  # OAuth client authentication (no cookies)
    "/oauth/revoke",
)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or "req_" + secrets.token_hex(8)
        request.state.request_id = rid
        start = time.perf_counter()

        if request.method not in SAFE_METHODS and not IS_TEST and not request.url.path.startswith(CSRF_EXEMPT_PREFIXES):
            session = request.scope.get("session") or {}
            expected = session.get("csrf")
            supplied = request.headers.get("x-csrf-token")
            if not supplied:
                supplied = request.query_params.get("csrf_token")
            if not supplied:
                ctype = request.headers.get("content-type", "")
                body = await request.body()  # cached & replayed to the endpoint by Starlette
                if "application/x-www-form-urlencoded" in ctype:
                    supplied = (parse_qs(body.decode("utf-8", "ignore")).get("csrf_token") or [None])[0]
                elif "multipart/form-data" in ctype:
                    m = re.search(rb'name="csrf_token"\r\n\r\n([^\r\n]+)', body)
                    supplied = m.group(1).decode() if m else None
            if not expected or not supplied or not secrets.compare_digest(str(expected), str(supplied)):
                if request.url.path.startswith("/app/api") or "json" in request.headers.get("accept", ""):
                    return JSONResponse(
                        {"error": {"code": "CSRF_FAILED", "message": "Invalid CSRF token", "request_id": rid}},
                        status_code=403,
                    )
                return PlainTextResponse("CSRF token missing or invalid. Reload the page and try again.", 403)

        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        # Remember an explicit ?lang= choice for invitee pages (see app/i18n.get_locale).
        lang = request.query_params.get("lang")
        if lang and request.method == "GET":
            from app.i18n import LANG_COOKIE, normalize_locale

            loc = normalize_locale(lang)
            if loc and request.cookies.get(LANG_COOKIE) != loc:
                response.set_cookie(LANG_COOKIE, loc, max_age=180 * 24 * 3600, samesite="lax", httponly=True)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # Booking pages may be framed by embeds (frame-ancestors set per-route); app pages may not.
        if not request.url.path.startswith(("/embed", "/e/")) and "frame-ancestors" not in response.headers.get(
            "Content-Security-Policy", ""
        ):
            if request.query_params.get("embed") != "1":
                response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        dur = (time.perf_counter() - start) * 1000
        log.info(
            json.dumps(
                {
                    "request_id": rid,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "ms": round(dur, 1),
                }
            )
        )
        return response
