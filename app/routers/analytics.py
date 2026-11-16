"""Analytics dashboard (PRD 14.1 / 14.2): KPIs, breakdowns, attribution, routing, RR balance,
revenue and CSV export. Members see their own bookings; admins see the workspace and (with the
team_analytics entitlement) can filter by team / host."""
import csv
import datetime as dt
import io
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import Ctx, get_ctx, render
from app.models import EventType, Team, User, WorkspaceMember
from app.services import analytics as analytics_svc  # noqa: F401  (registers @periodic analytics_retention)

router = APIRouter()


def _parse_date(v: Optional[str]) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(v) if v else None
    except ValueError:
        return None


def _filters(request: Request, ctx: Ctx, db: Session):
    from app.services.analytics import Filters, local_range_to_utc, to_local

    qp = request.query_params
    tz = ctx.user.default_timezone or "UTC"
    today = to_local(dt.datetime.utcnow(), tz).date()
    end_d = _parse_date(qp.get("end")) or today
    start_d = _parse_date(qp.get("start")) or (end_d - dt.timedelta(days=29))
    if start_d > end_d:
        start_d, end_d = end_d, start_d
    start, end = local_range_to_utc(start_d, end_d, tz)
    is_admin = ctx.can("admin")
    team_ok = is_admin and ctx.feature("team_analytics")
    host_id = qp.get("host") or None
    if not is_admin:
        host_id = ctx.user.id  # members/viewers see only their own data
    elif host_id and not db.scalar(
        select(WorkspaceMember.id).where(WorkspaceMember.workspace_id == ctx.workspace.id, WorkspaceMember.user_id == host_id)
    ):
        host_id = None
    team_id = qp.get("team") if team_ok else None
    if team_id and not db.scalar(select(Team.id).where(Team.id == team_id, Team.workspace_id == ctx.workspace.id)):
        team_id = None
    et_id = qp.get("event_type") or None
    if et_id and not db.scalar(select(EventType.id).where(EventType.id == et_id, EventType.workspace_id == ctx.workspace.id)):
        et_id = None
    f = Filters(
        workspace_id=ctx.workspace.id, start=start, end=end, tz=tz, host_id=host_id, team_id=team_id, event_type_id=et_id,
        extra={"start_date": start_d, "end_date": end_d, "is_admin": is_admin, "team_ok": team_ok},
    )
    return f


@router.get("/app/analytics")
def analytics_page(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    from app.services.analytics import compute

    if not ctx.feature("analytics"):
        return render(request, "app/analytics/index.html", ctx, nav="analytics", locked=True)
    f = _filters(request, ctx, db)
    data = compute(db, f)
    data.pop("rows", None)
    prev = analytics_svc.compare_previous(db, f)
    members = []
    teams = []
    if f.extra["is_admin"]:
        members = db.execute(
            select(User)
            .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
            .where(WorkspaceMember.workspace_id == ctx.workspace.id)
            .order_by(User.name)
        ).scalars().all()
        if f.extra["team_ok"]:
            teams = db.scalars(select(Team).where(Team.workspace_id == ctx.workspace.id).order_by(Team.name)).all()
    et_q = select(EventType).where(EventType.workspace_id == ctx.workspace.id, EventType.deleted_at.is_(None))
    if not f.extra["is_admin"]:
        et_q = et_q.where(EventType.owner_user_id == ctx.user.id)
    event_types = db.scalars(et_q.order_by(EventType.name)).all()
    retention = analytics_svc.retention_days(ctx.workspace)
    return render(
        request, "app/analytics/index.html", ctx, nav="analytics", locked=False, d=data, f=f, prev=prev,
        members=members, teams=teams, event_types=event_types, query=str(request.url.query), retention=retention,
    )


@router.get("/app/analytics/export.csv")
def analytics_export(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    from app.deps import Forbidden
    from app.services.analytics import CSV_HEADER, csv_rows, csv_safe, fetch_bookings

    if not ctx.feature("analytics"):
        raise Forbidden("Your plan does not include 'analytics'. Upgrade in Settings > Billing.")
    f = _filters(request, ctx, db)
    rows = csv_rows(db, fetch_bookings(db, f))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_HEADER)
    for r in rows:
        w.writerow([csv_safe(c) for c in r])
    name = f"bookings_{f.extra['start_date']}_{f.extra['end_date']}.csv"
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/app/analytics/data.json")
def analytics_json(request: Request, ctx: Ctx = Depends(get_ctx), db: Session = Depends(get_db)):
    """Same dataset as the dashboard, as JSON (KPIs, previous period, heatmap, lead time, breakdowns)."""
    from app.deps import Forbidden

    if not ctx.feature("analytics"):
        raise Forbidden("Your plan does not include 'analytics'. Upgrade in Settings > Billing.")
    f = _filters(request, ctx, db)
    data = analytics_svc.compute(db, f)
    data.pop("rows", None)
    data["previous_period"] = analytics_svc.compare_previous(db, f)
    data["range"] = {"start": str(f.extra["start_date"]), "end": str(f.extra["end_date"]), "tz": f.tz}
    data["retention_days"] = analytics_svc.retention_days(ctx.workspace)
    return data
