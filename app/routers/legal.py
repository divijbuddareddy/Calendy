"""Published legal pages required by the release gate (PRD 24.3 / MVP definition of done):
privacy notice, terms of service and data retention policy. Each page carries a version
(`settings.privacy_version` etc.); invitee booking consent records the privacy version it accepted."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.deps import render

router = APIRouter()


def _common() -> dict:
    return {
        "company": settings.company_name,
        "contact": settings.privacy_contact_email,
        "versions": {"privacy": settings.privacy_version, "terms": settings.terms_version,
                     "retention": settings.retention_version},
    }


@router.get("/privacy", include_in_schema=False)
def privacy(request: Request):
    return render(request, "legal/privacy.html", **_common())


@router.get("/terms", include_in_schema=False)
def terms(request: Request):
    return render(request, "legal/terms.html", **_common())


@router.get("/data-retention", include_in_schema=False)
def data_retention(request: Request):
    from app.services.analytics import DEFAULT_RETENTION_DAYS as booking_days

    try:
        from app.services.notetaker import DEFAULT_RETENTION_DAYS as recap_days
    except Exception:  # pragma: no cover
        recap_days = 90
    return render(request, "legal/retention.html", booking_days=booking_days, recap_days=recap_days,
                  account_purge_days=settings_purge_days(), **_common())


def settings_purge_days() -> int:
    from app.services.retention import ACCOUNT_PURGE_DAYS

    return ACCOUNT_PURGE_DAYS


@router.get("/legal/versions.json", include_in_schema=False)
def versions():
    """Machine-readable current policy versions (used by booking consent capture)."""
    return JSONResponse(_common()["versions"])
