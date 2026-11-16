"""Standard error envelope (PRD 22.4): {"error": {"code", "message", "request_id"}}."""
from fastapi import Request
from fastapi.responses import JSONResponse


class APIError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, details: dict | None = None):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(message)


# Common codes
SLOT_NO_LONGER_AVAILABLE = "SLOT_NO_LONGER_AVAILABLE"
NOT_FOUND = "NOT_FOUND"
FORBIDDEN = "FORBIDDEN"
UNAUTHORIZED = "UNAUTHORIZED"
VALIDATION_ERROR = "VALIDATION_ERROR"
RATE_LIMITED = "RATE_LIMITED"
IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
CALENDAR_UNAVAILABLE = "CALENDAR_UNAVAILABLE"
PAYMENT_REQUIRED = "PAYMENT_REQUIRED"


def error_body(request: Request, code: str, message: str, details: dict | None = None) -> dict:
    body = {"error": {"code": code, "message": message, "request_id": getattr(request.state, "request_id", None)}}
    if details:
        body["error"]["details"] = details
    return body


async def api_error_handler(request: Request, exc: APIError):
    return JSONResponse(error_body(request, exc.code, exc.message, exc.details), status_code=exc.status_code)
