"""Application entrypoint: modular monolith (PRD 19.3)."""
import asyncio
import importlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.config import IS_TEST, settings
from app.db import init_db
from app.deps import Forbidden, LoginRequired, render
from app.errors import APIError, api_error_handler, error_body
from app.middleware import RequestContextMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")

# Order matters: the public catch-all router (/{username}/...) MUST be last.
ROUTER_MODULES = [
    # auth / host app
    "app.routers.auth",
    "app.routers.dashboard",
    "app.routers.event_types",
    "app.routers.availability",
    "app.routers.scheduled",
    "app.routers.settings",
    # integrations / automation / platform
    "app.routers.integrations",
    "app.routers.workflows",
    "app.routers.payments",
    "app.routers.webhooks_inbound",
    "app.routers.api_ext",  # extension/mobile endpoints under /api/v1/ext (before api_v1)
    "app.routers.api_v1",
    "app.routers.embed",
    "app.routers.dev",
    # growth / team / enterprise / AI
    "app.routers.teams",
    "app.routers.routing",
    "app.routers.contacts",
    "app.routers.analytics",
    "app.routers.admin",
    "app.routers.assistant",
    "app.routers.notetaker",
    "app.routers.polls",
    "app.routers.scim",
    # platform: legal pages (release gate), PWA/mobile, ops
    "app.routers.legal",
    "app.routers.pwa",
    "app.routers.ops",
    # public booking (JSON API then HTML catch-all LAST)
    "app.routers.public_api",
    "app.routers.public",
]


# Routers that must stay last (catch-all public pages).
_TAIL = ("app.routers.public_api", "app.routers.public")
# Background-task modules that register @periodic jobs but have no router.
SERVICE_MODULES = ["app.services.retention"]


def _discover_extra_routers() -> list[str]:
    """Router modules added under app/routers/ but not listed above are included automatically
    (before the public catch-all), so new feature modules can't be silently unreachable."""
    import pkgutil

    import app.routers as pkg

    listed = set(ROUTER_MODULES)
    return sorted(f"app.routers.{m.name}" for m in pkgutil.iter_modules(pkg.__path__)
                  if not m.name.startswith("_") and f"app.routers.{m.name}" not in listed)


def _startup_hooks() -> None:
    """Optional startup hooks from other modules; a missing module must never stop the app booting."""
    from app.db import engine

    try:
        from app.services.booking import ensure_postgres_constraints
    except ImportError:
        ensure_postgres_constraints = None
    if ensure_postgres_constraints:
        try:
            ensure_postgres_constraints(engine)
        except Exception:
            log.exception("ensure_postgres_constraints failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _startup_hooks()
    stop = asyncio.Event()
    task = None
    if settings.worker_enabled and not IS_TEST:
        from app.worker import worker_loop

        task = asyncio.create_task(worker_loop(stop))
    yield
    stop.set()
    if task:
        await task


def create_app() -> FastAPI:
    if settings.env == "prod" and settings.secret_key.startswith("dev-secret"):
        raise RuntimeError("SECRET_KEY must be set to a strong random value in production (ENV=prod).")
    app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    try:
        from app.services.observability import init_observability
    except ImportError:
        init_observability = None
    if init_observability:
        try:
            init_observability(app)
        except Exception:
            log.exception("init_observability failed")
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="aal_session",
        same_site="lax",
        https_only=settings.base_url.startswith("https"),
        max_age=settings.session_max_age_minutes * 60,
    )
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/app/api") or "application/json" in request.headers.get("accept", ""):
            return JSONResponse(error_body(request, "UNAUTHORIZED", "Login required"), status_code=401)
        nxt = request.url.path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse(f"/login?next={nxt}", status_code=303)

    @app.exception_handler(Forbidden)
    async def _forbidden(request: Request, exc: Forbidden):
        if request.url.path.startswith("/app/api") or "application/json" in request.headers.get("accept", ""):
            return JSONResponse(error_body(request, "FORBIDDEN", str(exc) or "Forbidden"), status_code=403)
        return render(request, "error.html", status_code=403, title="Access denied", message=str(exc) or "Forbidden")

    app.add_exception_handler(APIError, api_error_handler)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return JSONResponse(
            error_body(request, "VALIDATION_ERROR", "Invalid request", {"errors": exc.errors()}), status_code=422
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/") or "application/json" in request.headers.get("accept", ""):
            code = {404: "NOT_FOUND", 403: "FORBIDDEN", 401: "UNAUTHORIZED"}.get(exc.status_code, "HTTP_ERROR")
            return JSONResponse(error_body(request, code, str(exc.detail)), status_code=exc.status_code)
        title = {404: "Page not found", 403: "Access denied"}.get(exc.status_code, "Something went wrong")
        return render(request, "error.html", status_code=exc.status_code, title=title, message=str(exc.detail))

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        """Liveness + database reachability (used by Render's health check and uptime monitors)."""
        from sqlalchemy import text

        from app.db import engine

        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            log.exception("healthz: database unreachable")
            return JSONResponse({"ok": False, "db": "unreachable"}, status_code=503)
        return {"ok": True, "db": "ok", "env": settings.env}

    @app.get("/", include_in_schema=False)
    def root(request: Request):
        if request.session.get("user_id"):
            return RedirectResponse("/app", status_code=303)
        return render(request, "landing.html")

    head = [m for m in ROUTER_MODULES if m not in _TAIL]
    for mod in head + _discover_extra_routers() + [m for m in ROUTER_MODULES if m in _TAIL]:
        try:
            m = importlib.import_module(mod)
        except Exception:
            if IS_TEST or mod in ROUTER_MODULES:
                raise
            log.exception("optional router %s failed to import; skipped", mod)
            continue
        if getattr(m, "router", None) is not None:
            app.include_router(m.router)
    for mod in SERVICE_MODULES:
        importlib.import_module(mod)
    return app


app = create_app()
