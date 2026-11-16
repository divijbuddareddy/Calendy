"""Routing forms (PRD 10.5).

Host app (feature "routing_forms"; viewers read-only):
  GET  /app/routing                                 list + create
  POST /app/routing                                 create form
  GET  /app/routing/{id}                            builder (settings, questions, rules, fallback)
  POST /app/routing/{id}/settings | /delete
  POST /app/routing/{id}/questions | /questions/{fid}/delete | /questions/{fid}/move
  POST /app/routing/{id}/rules | /rules/{rid}/delete | /rules/{rid}/move | /rules-json | /fallback
  GET/POST /app/routing/{id}/preview                test mode: evaluates WITHOUT saving a submission
  POST /app/routing/{id}/prefill                    explicit field -> booking prefill mapping
  GET  /app/routing/{id}/submissions (+ .csv)
Public:
  GET/POST /r/{workspace_slug}/{form_slug}          rate limited + honeypot; saves RoutingSubmission,
                                                    publishes routing.submitted, records PageView, redirects.
"""
from __future__ import annotations

import csv
import io
import json
import re
import secrets
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, object_session

from app import ratelimit
from app.audit import audit
from app.config import settings
from app.db import get_db
from app.deps import Ctx, Forbidden, flash, get_ctx, redirect, render
from app.entitlements import has_feature
from app.events import publish
from app.models import EventType, PageView, RoutingForm, RoutingSubmission, Team, User, Workspace, WorkspaceMember
from app.services.bootstrap import slugify
from app.services import crm as crm_svc
from app.services.routing_engine import (
    CRM_DEST_TYPES,
    DEST_TYPES,
    FIELD_TYPE_LABELS,
    FIELD_TYPES,
    OP_LABELS,
    OPS,
    describe_condition,
    evaluate,
    safe_external_url,
    validate_destination,
)

router = APIRouter()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DEST_LABELS = {"event_type": "Event type booking page", "message": "Custom message", "url": "External URL",
               "team": "Team page", "host": "Specific host's page",
               "salesforce_lookup": "CRM record owner (Salesforce / HubSpot lookup)"}
PREFILL_BASIC = {"name": "Invitee name", "email": "Invitee email", "phone": "Invitee phone", "company": "Company"}
DEFAULT_FALLBACK = {"type": "message", "value": "Thanks! We'll be in touch shortly."}


# ================================================================ helpers
def _ctx(ctx: Ctx = Depends(get_ctx)) -> Ctx:
    if not ctx.feature("routing_forms"):
        raise Forbidden("Routing forms are available on the Teams plan. Upgrade in Settings > Billing.")
    return ctx


def _get_form(db: Session, ctx: Ctx, form_id: str) -> RoutingForm:
    f = db.get(RoutingForm, form_id)
    if not f or f.workspace_id != ctx.workspace.id:
        raise Forbidden("Routing form not found.")
    return f


def _editable(db: Session, ctx: Ctx, form_id: str) -> RoutingForm:
    f = _get_form(db, ctx, form_id)
    if ctx.role == "viewer" or (not ctx.can("admin") and f.owner_user_id != ctx.user.id):
        raise Forbidden("You can view this routing form but not edit it.")
    return f


def _back(form_id: str, anchor: str = ""):
    return redirect(f"/app/routing/{form_id}" + (f"#{anchor}" if anchor else ""))


def _dest_from_form(form, prefix: str = "dest") -> dict:
    t = form.get(f"{prefix}_type") or ""
    val = (form.get(f"{prefix}_{t}") or "").strip() if t in DEST_TYPES else ""
    if t in CRM_DEST_TYPES:
        val = val.lstrip("/").lower()
    return {"type": t, "value": val}


def _choices(db: Session, ws_id: str) -> dict:
    events = db.scalars(select(EventType).where(
        EventType.workspace_id == ws_id, EventType.deleted_at.is_(None), EventType.status == "active",
        EventType.is_managed_template.is_(False), EventType.is_one_off.is_(False))
        .order_by(EventType.name)).all()
    teams = db.scalars(select(Team).where(Team.workspace_id == ws_id).order_by(Team.name)).all()
    hosts = db.scalars(select(User).join(WorkspaceMember, WorkspaceMember.user_id == User.id).where(
        WorkspaceMember.workspace_id == ws_id, WorkspaceMember.status == "active").order_by(User.name)).all()
    return {"events": events, "teams": teams, "hosts": hosts}


def describe_destination(db: Session, ws_id: str, dest: dict) -> str:
    t, v = (dest or {}).get("type"), (dest or {}).get("value")
    if t == "event_type":
        et = db.get(EventType, v) if v else None
        return f"Book: {et.name}" if et and et.workspace_id == ws_id else "Book: (missing event type)"
    if t == "team":
        tm = db.get(Team, v) if v else None
        return f"Team page: {tm.name}" if tm and tm.workspace_id == ws_id else "Team page: (missing)"
    if t == "host":
        u = db.get(User, v) if v else None
        return f"Host page: {u.name}" if u else "Host page: (missing)"
    if t == "url":
        return f"Redirect: {v}"
    if t == "message":
        return f"Message: “{(v or '')[:60]}”"
    if t in CRM_DEST_TYPES:
        return "CRM owner lookup" + (f" → /{v}" if v else " → owner's page") + " (else fallback)"
    return "—"


def _clean(v) -> str:
    if isinstance(v, list):
        v = ", ".join(map(str, v))
    elif isinstance(v, bool):
        v = "Yes" if v else "No"
    return str(v if v is not None else "").strip()


def _question_keys(et: EventType) -> list[tuple[object, str]]:
    return [(q, q.prefill_key or f"a{idx}")
            for idx, q in enumerate(sorted(et.questions, key=lambda x: x.sort_order or 0), start=1)]


def _mapped_prefill(mapping: dict, et: Optional[EventType], answers: dict) -> dict:
    """Explicit mapping: {field_id: name|email|phone|company|q:<question id>|label:<question label>}."""
    params: dict[str, str] = {}
    limits = {"name": 200, "email": 320, "phone": 50, "company": 200}
    qkeys = _question_keys(et) if et else []
    for fid, target in (mapping or {}).items():
        v = _clean(answers.get(fid))
        if not v or not target:
            continue
        if target in limits:
            if target == "email" and not EMAIL_RE.match(v):
                continue
            params[target] = v[:limits[target]]
        elif target.startswith("q:"):
            for q, key in qkeys:
                if q.id == target[2:]:
                    params[key] = v[:500]
        elif target.startswith("label:"):
            want = target[6:].strip().lower()
            for q, key in qkeys:
                if (q.label or "").strip().lower() == want:
                    params[key] = v[:500]
    return params


def _prefill_params(form: RoutingForm, et: EventType, answers: dict, mapping: Optional[dict] = None) -> dict:
    """Map routing answers onto booking-page prefill params (name/email/phone/company + a1..aN).
    Uses the form's explicit field mapping when configured; otherwise falls back to type/label matching."""
    if mapping is None:
        sess = object_session(form)
        mapping = crm_svc.prefill_map(sess, form.id) if sess is not None else {}
    if mapping:
        return _mapped_prefill(mapping, et, answers)
    params: dict[str, str] = {}
    fields = form.form_schema_json or []

    def first(pred):
        for f in fields:
            v = answers.get(f["id"])
            if pred(f) and isinstance(v, str) and v.strip():
                return v.strip()
        return None

    email = first(lambda f: f.get("type") == "email")
    if email and EMAIL_RE.match(email):
        params["email"] = email[:320]
    name = first(lambda f: f.get("type") == "text" and "name" in (f.get("label") or "").lower()
                 and "company" not in (f.get("label") or "").lower())
    if name:
        params["name"] = name[:200]
    phone = first(lambda f: f.get("type") == "phone")
    if phone:
        params["phone"] = phone[:50]
    company = first(lambda f: "company" in (f.get("label") or "").lower())
    if company:
        params["company"] = company[:200]
    by_label = {(f.get("label") or "").strip().lower(): answers.get(f["id"]) for f in fields}
    for idx, q in enumerate(sorted(et.questions, key=lambda x: x.sort_order or 0), start=1):
        v = by_label.get((q.label or "").strip().lower())
        if v in (None, "", []):
            continue
        if isinstance(v, list):
            v = ", ".join(map(str, v))
        params[q.prefill_key or f"a{idx}"] = str(v)[:500]
    return params


def _crm_owner_target(db: Session, form: RoutingForm, dest: dict, answers: dict, mapping: dict,
                      submission_id: Optional[str], utm: Optional[dict]) -> Optional[dict]:
    """Resolve a CRM-lookup destination to the record owner's booking page, or None (-> fallback)."""
    email = crm_svc.submission_email(form, answers, mapping)
    hit = crm_svc.lookup_owner(db, form.workspace_id, email)
    if not hit or not hit.get("owner_email"):
        return None
    owner = db.scalar(select(User).join(WorkspaceMember, WorkspaceMember.user_id == User.id).where(
        WorkspaceMember.workspace_id == form.workspace_id, WorkspaceMember.status == "active",
        func.lower(User.email) == hit["owner_email"].lower(), User.deleted_at.is_(None)))
    if not owner or not owner.slug:
        return None
    slug = ((dest or {}).get("value") or "").strip().lower()
    et = None
    if slug:
        et = db.scalar(select(EventType).where(
            EventType.workspace_id == form.workspace_id, EventType.owner_user_id == owner.id,
            EventType.team_id.is_(None), EventType.slug == slug, EventType.deleted_at.is_(None),
            EventType.status == "active"))
    if et:
        params = _prefill_params(form, et, answers, mapping)
        params.update({k: str(val)[:200] for k, val in (utm or {}).items()})
        if submission_id:
            params["rsid"] = submission_id
        url = f"/{owner.slug}/{et.slug}" + ("?" + urlencode(params) if params else "")
    else:
        url = f"/{owner.slug}"
    return {"kind": "redirect", "url": url, "crm_owner": owner.email, "crm_record": hit}


def resolve_destination(db: Session, form: RoutingForm, dest: dict, answers: dict,
                        submission_id: Optional[str] = None, utm: Optional[dict] = None, _depth: int = 0) -> dict:
    """Return {"kind": "redirect"|"message", "url"?, "message"?}. Only safe, workspace-owned targets."""
    t, v = (dest or {}).get("type"), (dest or {}).get("value")
    ws_id = form.workspace_id
    mapping = crm_svc.prefill_map(db, form.id)
    if t in CRM_DEST_TYPES:
        hit = _crm_owner_target(db, form, dest, answers, mapping, submission_id, utm)
        if hit:
            return hit
        fb = form.fallback_json or DEFAULT_FALLBACK
        if _depth == 0 and fb.get("type") not in CRM_DEST_TYPES:
            res = resolve_destination(db, form, fb, answers, submission_id, utm, _depth=1)
            res["crm_fallback"] = True
            return res
        return {"kind": "message", "message": DEFAULT_FALLBACK["value"], "crm_fallback": True}
    if t == "event_type":
        et = db.get(EventType, v) if v else None
        if et and et.workspace_id == ws_id and not et.deleted_at:
            if et.team_id:
                team = db.get(Team, et.team_id)
                path = f"/team/{team.slug}/{et.slug}"
            else:
                owner = db.get(User, et.owner_user_id)
                path = f"/{owner.slug}/{et.slug}"
            params = _prefill_params(form, et, answers, mapping)
            params.update({k: str(val)[:200] for k, val in (utm or {}).items()})
            if submission_id:
                params["rsid"] = submission_id
            return {"kind": "redirect", "url": path + ("?" + urlencode(params) if params else "")}
    elif t == "team":
        team = db.get(Team, v) if v else None
        if team and team.workspace_id == ws_id:
            return {"kind": "redirect", "url": f"/team/{team.slug}"}
    elif t == "host":
        wm = db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == ws_id, WorkspaceMember.user_id == v))
        u = db.get(User, v) if wm else None
        if u and u.slug:
            return {"kind": "redirect", "url": f"/{u.slug}"}
    elif t == "url":
        url = safe_external_url(v or "")
        if url:
            return {"kind": "redirect", "url": url}
    elif t == "message":
        return {"kind": "message", "message": v or ""}
    return {"kind": "message", "message": "Thanks! Your response has been received."}


def _collect_answers(form: RoutingForm, data) -> tuple[dict, list[str]]:
    answers, errors = {}, []
    for f in form.form_schema_json or []:
        fid, ftype, label = f["id"], f.get("type"), f.get("label") or f["id"]
        if ftype == "checkbox":
            val = [str(x)[:200] for x in data.getlist(fid)][:50]
        elif ftype == "consent":
            val = data.get(fid) in ("1", "on", "true", "yes")
        else:
            val = (data.get(fid) or "").strip()[:1000]
        if f.get("required") and val in ("", [], False, None):
            errors.append(f"“{label}” is required.")
        elif ftype == "email" and val and not EMAIL_RE.match(val):
            errors.append(f"“{label}” must be a valid email address.")
        elif ftype == "number" and val:
            try:
                float(val)
            except ValueError:
                errors.append(f"“{label}” must be a number.")
        elif ftype in ("select", "radio") and val and val not in (f.get("options") or []):
            errors.append(f"Choose a valid option for “{label}”.")
        answers[fid] = val
    return answers, errors


def _answers_display(form: RoutingForm, answers: dict) -> list[tuple[str, str]]:
    out = []
    labels = {f["id"]: f.get("label") or f["id"] for f in form.form_schema_json or []}
    for fid, v in (answers or {}).items():
        if isinstance(v, list):
            v = ", ".join(map(str, v))
        elif isinstance(v, bool):
            v = "Yes" if v else "No"
        out.append((labels.get(fid, fid), str(v)))
    return out


def _rule_name(form: RoutingForm, rule_id: Optional[str]) -> str:
    if not rule_id:
        return "Fallback"
    for i, r in enumerate(form.rules_json or [], start=1):
        if r.get("id") == rule_id:
            return r.get("name") or f"Rule {i}"
    return rule_id


def _validate_rules(rules, field_ids: set) -> Optional[str]:
    if not isinstance(rules, list):
        return "Rules must be a JSON list."

    def check_group(g, depth=0) -> Optional[str]:
        if not isinstance(g, dict) or not isinstance(g.get("conditions"), list) or not g["conditions"]:
            return "Each rule/group needs a non-empty 'conditions' list."
        if (g.get("logic") or "and").lower() not in ("and", "or"):
            return "logic must be 'and' or 'or'."
        if depth > 3:
            return "Groups can be nested at most 3 levels."
        for c in g["conditions"]:
            if isinstance(c, dict) and "conditions" in c:
                e = check_group(c, depth + 1)
                if e:
                    return e
            elif not isinstance(c, dict) or c.get("op") not in OPS or c.get("field") not in field_ids:
                return f"Invalid condition: {json.dumps(c)[:120]}"
        return None

    for r in rules:
        e = check_group(r) or validate_destination((r or {}).get("destination"))
        if e:
            return e
    return None


# ================================================================ list / create
@router.get("/app/routing")
def routing_list(request: Request, ctx: Ctx = Depends(_ctx), db: Session = Depends(get_db)):
    forms = db.scalars(select(RoutingForm).where(RoutingForm.workspace_id == ctx.workspace.id)
                       .order_by(RoutingForm.created_at.desc())).all()
    subs = dict(db.execute(select(RoutingSubmission.form_id, func.count(RoutingSubmission.id))
                           .where(RoutingSubmission.workspace_id == ctx.workspace.id)
                           .group_by(RoutingSubmission.form_id)).all())
    conv = dict(db.execute(select(RoutingSubmission.form_id, func.count(RoutingSubmission.id))
                           .where(RoutingSubmission.workspace_id == ctx.workspace.id,
                                  RoutingSubmission.booking_id.is_not(None))
                           .group_by(RoutingSubmission.form_id)).all())
    return render(request, "app/routing/list.html", ctx, nav="routing", forms=forms, subs=subs, conv=conv)


@router.post("/app/routing")
async def routing_create(request: Request, ctx: Ctx = Depends(_ctx), db: Session = Depends(get_db)):
    if ctx.role == "viewer":
        raise Forbidden("Viewers can't create routing forms.")
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        flash(request, "Form name is required.", "error")
        return redirect("/app/routing")
    base = slugify(form.get("slug") or name)
    slug, n = base, 1
    while db.scalar(select(RoutingForm.id).where(RoutingForm.workspace_id == ctx.workspace.id, RoutingForm.slug == slug)):
        n += 1
        slug = f"{base}-{n}"
    rf = RoutingForm(
        workspace_id=ctx.workspace.id, owner_user_id=ctx.user.id, name=name[:200], slug=slug,
        form_schema_json=[
            {"id": "f1", "type": "text", "label": "Your name", "required": True, "options": []},
            {"id": "f2", "type": "email", "label": "Work email", "required": True, "options": []},
        ],
        rules_json=[], fallback_json=dict(DEFAULT_FALLBACK), status="active",
    )
    db.add(rf)
    db.flush()
    audit(db, "routing_form.created", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="routing_form",
          entity_id=rf.id, metadata={"name": name})
    db.commit()
    flash(request, "Routing form created with name and email questions. Add qualifying questions and rules.")
    return redirect(f"/app/routing/{rf.id}")


# ================================================================ builder
@router.get("/app/routing/{form_id}")
def routing_builder(request: Request, form_id: str, ctx: Ctx = Depends(_ctx), db: Session = Depends(get_db)):
    rf = _get_form(db, ctx, form_id)
    fields = {f["id"]: f.get("label") or f["id"] for f in rf.form_schema_json or []}
    rules = [{"rule": r, "text": f" {(r.get('logic') or 'and').upper()} ".join(
        describe_condition(c, fields) for c in r.get("conditions") or []),
        "dest": describe_destination(db, ctx.workspace.id, r.get("destination"))} for r in rf.rules_json or []]
    can_edit = ctx.role != "viewer" and (ctx.can("admin") or rf.owner_user_id == ctx.user.id)
    return render(request, "app/routing/builder.html", ctx, nav="routing", rf=rf, rules=rules, fields=fields,
                  field_types=FIELD_TYPES, field_type_labels=FIELD_TYPE_LABELS, ops=OPS, op_labels=OP_LABELS,
                  dest_labels=DEST_LABELS, fallback_text=describe_destination(db, ctx.workspace.id, rf.fallback_json),
                  public_url=f"{settings.base_url}/r/{ctx.workspace.slug}/{rf.slug}", can_edit=can_edit,
                  rules_json=json.dumps(rf.rules_json or [], indent=2), prefill=crm_svc.prefill_map(db, rf.id),
                  prefill_targets=_prefill_targets(db, ctx.workspace.id),
                  crm_conn=crm_svc.get_connection(db, ctx.workspace.id), **_choices(db, ctx.workspace.id))


def _prefill_targets(db: Session, ws_id: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """Grouped <optgroup> choices for the prefill mapping UI."""
    groups = [("Invitee details", list(PREFILL_BASIC.items()))]
    events = _choices(db, ws_id)["events"]
    labels: dict[str, str] = {}
    for et in events:
        opts = []
        for q in sorted(et.questions, key=lambda x: x.sort_order or 0):
            opts.append((f"q:{q.id}", f"{et.name} › {q.label}"))
            labels.setdefault((q.label or "").strip().lower(), q.label)
        if opts:
            groups.append((f"Question on “{et.name}”", opts))
    if labels:
        groups.append(("Any event question with label", [(f"label:{v}", v) for v in sorted(labels.values(), key=str.lower)]))
    return groups


@router.post("/app/routing/{form_id}/prefill")
async def save_prefill(request: Request, form_id: str, ctx: Ctx = Depends(_ctx), db: Session = Depends(get_db)):
    from app.models_crm import RoutingPrefillMap

    rf = _editable(db, ctx, form_id)
    form = await request.form()
    valid = {k for _, opts in _prefill_targets(db, ctx.workspace.id) for k, _ in opts}
    mapping, used_basic = {}, set()
    for f in rf.form_schema_json or []:
        target = (form.get(f"map_{f['id']}") or "").strip()
        if not target:
            continue
        if target not in valid:
            flash(request, f"Invalid mapping for “{f.get('label')}”.", "error")
            return _back(rf.id, "prefill")
        if target in PREFILL_BASIC:
            if target in used_basic:
                flash(request, f"Only one question can fill the invitee {target}.", "error")
                return _back(rf.id, "prefill")
            used_basic.add(target)
        mapping[f["id"]] = target
    row = db.scalar(select(RoutingPrefillMap).where(RoutingPrefillMap.form_id == rf.id))
    if not row:
        row = RoutingPrefillMap(form_id=rf.id, workspace_id=ctx.workspace.id, mapping_json={})
        db.add(row)
    row.mapping_json = mapping
    audit(db, "routing_form.prefill_mapped", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          entity_type="routing_form", entity_id=rf.id, metadata={"fields": len(mapping)})
    db.commit()
    flash(request, "Prefill mapping saved." if mapping else "Prefill mapping cleared; label matching is used.")
    return _back(rf.id, "prefill")


@router.post("/app/routing/{form_id}/settings")
async def routing_settings(request: Request, form_id: str, ctx: Ctx = Depends(_ctx), db: Session = Depends(get_db)):
    rf = _editable(db, ctx, form_id)
    form = await request.form()
    name = (form.get("name") or "").strip()
    slug = slugify(form.get("slug") or rf.slug)
    if not name:
        flash(request, "Name is required.", "error")
        return _back(rf.id)
    if slug != rf.slug and db.scalar(select(RoutingForm.id).where(RoutingForm.workspace_id == ctx.workspace.id,
                                                                  RoutingForm.slug == slug)):
        flash(request, "Another form already uses that link.", "error")
        return _back(rf.id)
    rf.name, rf.slug = name[:200], slug
    rf.status = "paused" if form.get("status") == "paused" else "active"
    audit(db, "routing_form.updated", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="routing_form",
          entity_id=rf.id, metadata={"status": rf.status})
    db.commit()
    flash(request, "Saved.")
    return _back(rf.id)


@router.post("/app/routing/{form_id}/delete")
def routing_delete(request: Request, form_id: str, ctx: Ctx = Depends(_ctx), db: Session = Depends(get_db)):
    rf = _editable(db, ctx, form_id)
    audit(db, "routing_form.deleted", workspace_id=ctx.workspace.id, actor_id=ctx.user.id, entity_type="routing_form",
          entity_id=rf.id, metadata={"name": rf.name})
    for s in db.scalars(select(RoutingSubmission).where(RoutingSubmission.form_id == rf.id)):
        db.delete(s)
    db.delete(rf)
    db.commit()
    flash(request, "Routing form deleted.")
    return redirect("/app/routing")


@router.post("/app/routing/{form_id}/questions")
async def add_question(request: Request, form_id: str, ctx: Ctx = Depends(_ctx), db: Session = Depends(get_db)):
    rf = _editable(db, ctx, form_id)
    form = await request.form()
    label = (form.get("label") or "").strip()
    ftype = form.get("type") if form.get("type") in FIELD_TYPES else "text"
    options = [o.strip()[:200] for o in re.split(r"[\n,]", form.get("options") or "") if o.strip()]
    if not label:
        flash(request, "Question label is required.", "error")
        return _back(rf.id, "questions")
    if ftype in ("select", "radio", "checkbox") and not options:
        flash(request, "Dropdown, radio and checkbox questions need options.", "error")
        return _back(rf.id, "questions")
    schema = list(rf.form_schema_json or [])
    nums = [int(f["id"][1:]) for f in schema if re.match(r"^f\d+$", f.get("id", ""))]
    fid = f"f{max(nums or [0]) + 1}"
    schema.append({"id": fid, "type": ftype, "label": label[:300], "required": bool(form.get("required")),
                   "options": options[:50], "help": (form.get("help") or "").strip()[:300]})
    rf.form_schema_json = schema
    db.commit()
    flash(request, "Question added.")
    return _back(rf.id, "questions")


@router.post("/app/routing/{form_id}/questions/{fid}/{action}")
def question_action(request: Request, form_id: str, fid: str, action: str, ctx: Ctx = Depends(_ctx),
                    db: Session = Depends(get_db)):
    rf = _editable(db, ctx, form_id)
    schema = list(rf.form_schema_json or [])
    idx = next((i for i, f in enumerate(schema) if f["id"] == fid), None)
    if idx is None:
        return _back(rf.id, "questions")
    if action == "delete":
        used = fid in json.dumps(rf.rules_json or [])
        if used:
            flash(request, "That question is used by a routing rule. Remove the rule first.", "error")
            return _back(rf.id, "questions")
        schema.pop(idx)
    elif action in ("up", "down"):
        j = idx - 1 if action == "up" else idx + 1
        if 0 <= j < len(schema):
            schema[idx], schema[j] = schema[j], schema[idx]
    rf.form_schema_json = schema
    db.commit()
    return _back(rf.id, "questions")


@router.post("/app/routing/{form_id}/rules")
async def add_rule(request: Request, form_id: str, ctx: Ctx = Depends(_ctx), db: Session = Depends(get_db)):
    rf = _editable(db, ctx, form_id)
    form = await request.form()
    field_ids = {f["id"] for f in rf.form_schema_json or []}
    conds = []
    for i in range(1, 6):
        fld, op = form.get(f"c{i}_field"), form.get(f"c{i}_op")
        if not fld:
            continue
        if fld not in field_ids or op not in OPS:
            flash(request, f"Condition {i} is invalid.", "error")
            return _back(rf.id, "rules")
        conds.append({"field": fld, "op": op, "value": (form.get(f"c{i}_value") or "").strip()[:300]})
    if not conds:
        flash(request, "Add at least one condition.", "error")
        return _back(rf.id, "rules")
    dest = _dest_from_form(form)
    err = validate_destination(dest)
    if err:
        flash(request, err, "error")
        return _back(rf.id, "rules")
    rules = list(rf.rules_json or [])
    rules.append({"id": "r_" + secrets.token_hex(4), "name": (form.get("name") or "").strip()[:100] or f"Rule {len(rules) + 1}",
                  "logic": "or" if form.get("logic") == "or" else "and", "conditions": conds, "destination": dest})
    rf.rules_json = rules
    audit(db, "routing_form.rules_changed", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          entity_type="routing_form", entity_id=rf.id, metadata={"rules": len(rules)})
    db.commit()
    flash(request, "Rule added.")
    return _back(rf.id, "rules")


@router.post("/app/routing/{form_id}/rules-json")
async def rules_json(request: Request, form_id: str, ctx: Ctx = Depends(_ctx), db: Session = Depends(get_db)):
    rf = _editable(db, ctx, form_id)
    form = await request.form()
    try:
        rules = json.loads(form.get("rules_json") or "[]")
    except json.JSONDecodeError as e:
        flash(request, f"Invalid JSON: {e}", "error")
        return _back(rf.id, "advanced")
    err = _validate_rules(rules, {f["id"] for f in rf.form_schema_json or []})
    if err:
        flash(request, err, "error")
        return _back(rf.id, "advanced")
    for r in rules:
        r.setdefault("id", "r_" + secrets.token_hex(4))
    rf.rules_json = rules
    audit(db, "routing_form.rules_changed", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          entity_type="routing_form", entity_id=rf.id, metadata={"rules": len(rules), "via": "json"})
    db.commit()
    flash(request, "Rules saved.")
    return _back(rf.id, "rules")


@router.post("/app/routing/{form_id}/rules/{rid}/{action}")
def rule_action(request: Request, form_id: str, rid: str, action: str, ctx: Ctx = Depends(_ctx),
                db: Session = Depends(get_db)):
    rf = _editable(db, ctx, form_id)
    rules = list(rf.rules_json or [])
    idx = next((i for i, r in enumerate(rules) if r.get("id") == rid), None)
    if idx is not None:
        if action == "delete":
            rules.pop(idx)
        elif action in ("up", "down"):
            j = idx - 1 if action == "up" else idx + 1
            if 0 <= j < len(rules):
                rules[idx], rules[j] = rules[j], rules[idx]
        rf.rules_json = rules
        db.commit()
    return _back(rf.id, "rules")


@router.post("/app/routing/{form_id}/fallback")
async def set_fallback(request: Request, form_id: str, ctx: Ctx = Depends(_ctx), db: Session = Depends(get_db)):
    rf = _editable(db, ctx, form_id)
    form = await request.form()
    dest = _dest_from_form(form, "fb")
    err = validate_destination(dest)
    if not err and dest.get("type") in CRM_DEST_TYPES:
        err = "The fallback can't itself be a CRM lookup (it's used when the lookup finds no owner)."
    if err:
        flash(request, "Fallback not saved: " + err, "error")
        return _back(rf.id, "fallback")
    rf.fallback_json = dest
    db.commit()
    flash(request, "Fallback destination saved.")
    return _back(rf.id, "fallback")


# ================================================================ preview / test mode
@router.get("/app/routing/{form_id}/preview")
def preview_page(request: Request, form_id: str, ctx: Ctx = Depends(_ctx), db: Session = Depends(get_db)):
    rf = _get_form(db, ctx, form_id)
    return render(request, "app/routing/preview.html", ctx, nav="routing", rf=rf, result=None, values={}, errors=[])


@router.post("/app/routing/{form_id}/preview")
async def preview_eval(request: Request, form_id: str, ctx: Ctx = Depends(_ctx), db: Session = Depends(get_db)):
    """Evaluate rules for test answers. Never creates a RoutingSubmission or publishes events."""
    rf = _get_form(db, ctx, form_id)
    data = await request.form()
    answers, errors = _collect_answers(rf, data)
    rule_id, dest = evaluate(rf.rules_json or [], rf.fallback_json or DEFAULT_FALLBACK, answers)
    resolved = resolve_destination(db, rf, dest, answers, submission_id="preview")
    result = {"rule_id": rule_id, "rule_name": _rule_name(rf, rule_id), "destination": dest,
              "dest_text": describe_destination(db, ctx.workspace.id, dest), "resolved": resolved}
    db.rollback()
    if "application/json" in request.headers.get("accept", ""):
        return {"matched_rule_id": rule_id, "destination": dest, "resolved": resolved, "errors": errors}
    return render(request, "app/routing/preview.html", ctx, nav="routing", rf=rf, result=result, values=answers,
                  errors=errors)


# ================================================================ submissions
def _submissions(db: Session, rf: RoutingForm, limit: int = 1000) -> list[RoutingSubmission]:
    return list(db.scalars(select(RoutingSubmission).where(RoutingSubmission.form_id == rf.id,
                                                           RoutingSubmission.workspace_id == rf.workspace_id)
                           .order_by(RoutingSubmission.created_at.desc()).limit(limit)).all())


@router.get("/app/routing/{form_id}/submissions")
def submissions_page(request: Request, form_id: str, ctx: Ctx = Depends(_ctx), db: Session = Depends(get_db)):
    rf = _get_form(db, ctx, form_id)
    subs = _submissions(db, rf, 500)
    rows = [{"s": s, "answers": _answers_display(rf, s.answers_json), "rule": _rule_name(rf, s.matched_rule_id),
             "dest": describe_destination(db, ctx.workspace.id, s.destination_json)} for s in subs]
    converted = sum(1 for s in subs if s.booking_id)
    return render(request, "app/routing/submissions.html", ctx, nav="routing", rf=rf, rows=rows, converted=converted)


def _csv_cell(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


@router.get("/app/routing/{form_id}/submissions.csv")
def submissions_csv(request: Request, form_id: str, ctx: Ctx = Depends(_ctx), db: Session = Depends(get_db)):
    rf = _get_form(db, ctx, form_id)
    fields = rf.form_schema_json or []
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["submission_id", "submitted_at_utc"] + [f.get("label") or f["id"] for f in fields]
               + ["matched_rule", "destination", "booking_id", "converted"])
    for s in _submissions(db, rf, 100000):
        vals = []
        for f in fields:
            v = (s.answers_json or {}).get(f["id"])
            vals.append(", ".join(map(str, v)) if isinstance(v, list) else v)
        w.writerow([_csv_cell(x) for x in [s.id, s.created_at.isoformat(timespec="seconds")] + vals + [
            _rule_name(rf, s.matched_rule_id), describe_destination(db, ctx.workspace.id, s.destination_json),
            s.booking_id or "", "yes" if s.booking_id else "no"]])
    audit(db, "routing_form.exported", workspace_id=ctx.workspace.id, actor_id=ctx.user.id,
          entity_type="routing_form", entity_id=rf.id)
    db.commit()
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{rf.slug}-submissions.csv"'})


# ================================================================ public form
def _public_form(db: Session, ws_slug: str, form_slug: str) -> tuple[Optional[Workspace], Optional[RoutingForm]]:
    ws = db.scalar(select(Workspace).where(Workspace.slug == ws_slug))
    if not ws or not has_feature(ws, "routing_forms"):
        return None, None
    rf = db.scalar(select(RoutingForm).where(RoutingForm.workspace_id == ws.id, RoutingForm.slug == form_slug))
    if not rf or rf.status != "active":
        return ws, None
    return ws, rf


def _utm(request: Request) -> dict:
    return {k: v[:200] for k, v in request.query_params.items() if k.startswith("utm_") and v}


@router.get("/r/{ws_slug}/{form_slug}")
def public_form(request: Request, ws_slug: str, form_slug: str, db: Session = Depends(get_db)):
    ws, rf = _public_form(db, ws_slug, form_slug)
    if not rf:
        return render(request, "routing_public/message.html", status_code=404, title="Form not available",
                      message="This form doesn't exist or isn't accepting responses right now.", ws=ws)
    utm = _utm(request)
    db.add(PageView(workspace_id=ws.id, event_type_id=None, page="routing", utm_json={"form_id": rf.id, **utm}))
    db.commit()
    return render(request, "routing_public/form.html", ws=ws, rf=rf, values={}, errors=[], utm=utm)


@router.post("/r/{ws_slug}/{form_slug}")
async def public_submit(request: Request, ws_slug: str, form_slug: str, db: Session = Depends(get_db)):
    ratelimit.enforce(request, "routing_submit", limit=20, window_seconds=60)
    ws, rf = _public_form(db, ws_slug, form_slug)
    if not rf:
        return render(request, "routing_public/message.html", status_code=404, title="Form not available",
                      message="This form doesn't exist or isn't accepting responses right now.", ws=ws)
    data = await request.form()
    utm = {k: str(v)[:200] for k, v in data.items() if k.startswith("utm_") and v}
    if (data.get("website") or "").strip():  # honeypot: silently accept, store nothing
        return render(request, "routing_public/message.html", title="Thank you",
                      message="Thanks! Your response has been received.", ws=ws)
    answers, errors = _collect_answers(rf, data)
    if errors:
        return render(request, "routing_public/form.html", status_code=400, ws=ws, rf=rf, values=answers,
                      errors=errors, utm=utm)
    rule_id, dest = evaluate(rf.rules_json or [], rf.fallback_json or DEFAULT_FALLBACK, answers)
    sub = RoutingSubmission(workspace_id=ws.id, form_id=rf.id, answers_json=answers, matched_rule_id=rule_id,
                            destination_json=dest)
    db.add(sub)
    db.flush()
    publish(db, "routing.submitted", {"submission_id": sub.id, "form_id": rf.id}, workspace_id=ws.id)
    resolved = resolve_destination(db, rf, dest, answers, submission_id=sub.id, utm=utm)
    if dest.get("type") in CRM_DEST_TYPES:
        sub.destination_json = {**dest, "crm_owner": resolved.get("crm_owner"),
                                "crm_fallback": bool(resolved.get("crm_fallback"))}
    db.commit()
    if resolved["kind"] == "redirect":
        return redirect(resolved["url"])
    return render(request, "routing_public/message.html", title=rf.name, message=resolved["message"], ws=ws)
