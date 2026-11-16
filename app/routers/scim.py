"""SCIM 2.0 user provisioning (RFC 7643/7644, minimal core User schema) - PRD 17.2.

Auth: `Authorization: Bearer <token>` where the token was generated in Admin > SSO & SCIM
(stored only as hash_secret(token) in SSOConfig.scim_token_hash). The token identifies the
workspace; every operation is scoped to that workspace's memberships.

Resource id = the user's id. `active: false` deactivates the workspace membership (the user
can no longer sign in to this workspace); DELETE removes the membership.

Groups map to Teams (id = team id, displayName = team name, members = team members, who must be
workspace members). List endpoints support RFC 7644 filters (eq ne co sw ew gt ge lt le pr,
and/or/not, parentheses) and startIndex/count pagination.
"""
import re
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import audit
from app.config import settings
from app.db import get_db
from app.entitlements import has_feature
from app.models import EventType, SSOConfig, Team, TeamMember, User, Workspace, WorkspaceMember
from app.ratelimit import client_ip, enforce
from app.security import constant_time_eq, hash_secret

router = APIRouter(prefix="/scim/v2")

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SCIM_MEDIA = "application/scim+json"


class ScimError(Exception):
    def __init__(self, status: int, detail: str, scim_type: Optional[str] = None):
        self.status, self.detail, self.scim_type = status, detail, scim_type


def scim_response(body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type=SCIM_MEDIA)


def error_response(e: ScimError) -> JSONResponse:
    body = {"schemas": [ERROR_SCHEMA], "status": str(e.status), "detail": e.detail}
    if e.scim_type:
        body["scimType"] = e.scim_type
    headers = {"WWW-Authenticate": 'Bearer realm="scim"'} if e.status == 401 else None
    return JSONResponse(body, status_code=e.status, media_type=SCIM_MEDIA, headers=headers)


def _auth(request: Request, db: Session) -> Workspace:
    enforce(request, "scim", limit=300, window_seconds=60)
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise ScimError(401, "Bearer token required")
    token = header[7:].strip()
    if not token:
        raise ScimError(401, "Bearer token required")
    h = hash_secret(token)
    cfg = db.scalar(select(SSOConfig).where(SSOConfig.scim_token_hash == h))
    if not cfg or not cfg.scim_token_hash or not constant_time_eq(cfg.scim_token_hash, h):
        raise ScimError(401, "Invalid SCIM token")
    ws = db.get(Workspace, cfg.workspace_id)
    if not ws or not has_feature(ws, "scim"):
        raise ScimError(403, "SCIM provisioning is not enabled for this workspace's plan")
    return ws


def _user_resource(u: User, m: WorkspaceMember) -> dict:
    parts = (u.name or "").split(" ", 1)
    return {
        "schemas": [USER_SCHEMA],
        "id": u.id,
        "userName": u.email,
        "name": {"formatted": u.name or "", "givenName": parts[0] if parts else "",
                 "familyName": parts[1] if len(parts) > 1 else ""},
        "displayName": u.name or u.email,
        "emails": [{"value": u.email, "type": "work", "primary": True}],
        "active": m.status == "active",
        "roles": [{"value": m.role}],
        "meta": {
            "resourceType": "User",
            "created": u.created_at.isoformat() + "Z",
            "lastModified": max(u.updated_at, m.updated_at).isoformat() + "Z",
            "location": f"{settings.base_url.rstrip('/')}/scim/v2/Users/{u.id}",
        },
    }


def _member(db: Session, ws: Workspace, user_id: str) -> tuple[User, WorkspaceMember]:
    row = db.execute(
        select(User, WorkspaceMember)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .where(WorkspaceMember.workspace_id == ws.id, User.id == user_id)
    ).first()
    if not row:
        raise ScimError(404, f"User {user_id} not found")
    return row[0], row[1]


def _name_from(body: dict) -> str:
    name = body.get("name") or {}
    full = body.get("displayName") or name.get("formatted") or " ".join(
        p for p in (name.get("givenName"), name.get("familyName")) if p
    )
    return (full or "").strip()


def _email_from(body: dict) -> str:
    email = body.get("userName") or ""
    if "@" not in email:
        emails = body.get("emails") or []
        prim = next((e for e in emails if e.get("primary")), emails[0] if emails else {})
        email = prim.get("value") or email
    return email.strip().lower()


def _set_active(db: Session, ws: Workspace, u: User, m: WorkspaceMember, active: bool, ip: str):
    if not active and u.id == ws.owner_user_id:
        raise ScimError(400, "The workspace owner cannot be deactivated via SCIM", "mutability")
    new = "active" if active else "deactivated"
    if m.status != new:
        m.status = new
        audit(db, "scim.user_" + ("reactivated" if active else "deactivated"), workspace_id=ws.id,
              actor_id="scim", entity_type="user", entity_id=u.id, ip=ip)


def _wrap(fn):
    """Run a handler converting ScimError into SCIM error responses."""
    import functools
    import inspect

    @functools.wraps(fn)
    async def inner(*args, **kwargs):
        try:
            res = fn(*args, **kwargs)
            if inspect.isawaitable(res):
                res = await res
            return res
        except ScimError as e:
            db = kwargs.get("db")
            if db is not None:
                db.rollback()
            return error_response(e)

    return inner


@router.get("/ServiceProviderConfig")
def service_provider_config():
    return scim_response({
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "documentationUri": f"{settings.base_url.rstrip('/')}/app/admin/sso",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 200},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [{
            "type": "oauthbearertoken", "name": "OAuth Bearer Token",
            "description": "Workspace SCIM token generated in Admin > SSO & SCIM", "primary": True,
        }],
        "meta": {"resourceType": "ServiceProviderConfig"},
    })


@router.get("/ResourceTypes")
def resource_types():
    def rt(name, endpoint, schema):
        return {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"], "id": name, "name": name,
                "endpoint": endpoint, "schema": schema, "meta": {"resourceType": "ResourceType"}}

    res = [rt("User", "/Users", USER_SCHEMA), rt("Group", "/Groups", GROUP_SCHEMA)]
    return scim_response({"schemas": [LIST_SCHEMA], "totalResults": len(res), "itemsPerPage": len(res),
                          "startIndex": 1, "Resources": res})


@router.get("/Schemas")
def schemas():
    def attr(name, typ="string", multi=False, sub=None, **kw):
        a = {"name": name, "type": typ, "multiValued": multi, "required": kw.get("required", False),
             "mutability": kw.get("mutability", "readWrite"), "returned": "default", "caseExact": False,
             "uniqueness": kw.get("uniqueness", "none")}
        if sub:
            a["subAttributes"] = sub
        return a

    user = {"id": USER_SCHEMA, "name": "User", "attributes": [
        attr("userName", required=True, uniqueness="server"), attr("displayName"),
        attr("name", "complex", sub=[attr("formatted"), attr("givenName"), attr("familyName")]),
        attr("emails", "complex", True, sub=[attr("value"), attr("type"), attr("primary", "boolean")]),
        attr("active", "boolean")]}
    group = {"id": GROUP_SCHEMA, "name": "Group", "attributes": [
        attr("displayName", required=True),
        attr("members", "complex", True, sub=[attr("value", mutability="immutable"), attr("display", mutability="readOnly")])]}
    res = [dict(x, schemas=["urn:ietf:params:scim:schemas:core:2.0:Schema"], meta={"resourceType": "Schema"})
           for x in (user, group)]
    return scim_response({"schemas": [LIST_SCHEMA], "totalResults": 2, "itemsPerPage": 2, "startIndex": 1,
                          "Resources": res})


# --------------------------------------------------------------------------- filters (RFC 7644 3.4.2.2)
_TOKEN_RE = re.compile(r'\s*(?:(\()|(\))|("(?:[^"\\]|\\.)*")|([^\s()]+))')
_OPS = {"eq", "ne", "co", "sw", "ew", "gt", "ge", "lt", "le", "pr"}


def _tokenize(text: str) -> list:
    out, pos = [], 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m or m.end() == pos:
            raise ScimError(400, "Unparseable filter", "invalidFilter")
        pos = m.end()
        if m.group(1):
            out.append(("(", None))
        elif m.group(2):
            out.append((")", None))
        elif m.group(3):
            import json as _json

            out.append(("val", _json.loads(m.group(3))))
        else:
            out.append(("word", m.group(4)))
    return out


def parse_filter(text: str):
    """Parse a SCIM filter into a nested tuple AST: ("and"|"or", a, b) | ("not", a) | (op, attr, value)."""
    toks = _tokenize(text)
    i = 0

    def peek():
        return toks[i] if i < len(toks) else (None, None)

    def take():
        nonlocal i
        t = peek()
        i += 1
        return t

    def expr():
        left = term()
        while peek()[0] == "word" and peek()[1].lower() == "or":
            take()
            left = ("or", left, term())
        return left

    def term():
        left = factor()
        while peek()[0] == "word" and peek()[1].lower() == "and":
            take()
            left = ("and", left, factor())
        return left

    def factor():
        kind, val = take()
        if kind == "(":
            e = expr()
            if take()[0] != ")":
                raise ScimError(400, "Missing ')' in filter", "invalidFilter")
            return e
        if kind == "word" and val.lower() == "not":
            return ("not", factor())
        if kind != "word":
            raise ScimError(400, "Expected attribute name in filter", "invalidFilter")
        attr_name = val
        k2, op = take()
        if k2 != "word" or op.lower() not in _OPS:
            raise ScimError(400, f"Unsupported filter operator near '{attr_name}'", "invalidFilter")
        op = op.lower()
        if op == "pr":
            return ("pr", attr_name, None)
        k3, value = take()
        if k3 == "val":
            pass
        elif k3 == "word":
            low = value.lower()
            value = True if low == "true" else False if low == "false" else None if low == "null" else value
        else:
            raise ScimError(400, "Expected value in filter", "invalidFilter")
        return (op, attr_name, value)

    ast = expr()
    if i != len(toks):
        raise ScimError(400, "Unexpected trailing tokens in filter", "invalidFilter")
    return ast


def _norm_attr(name: str) -> str:
    name = name.strip()
    for prefix in (USER_SCHEMA + ":", GROUP_SCHEMA + ":"):
        if name.lower().startswith(prefix.lower()):
            name = name[len(prefix):]
    return name.lower()


def _cmp(op: str, actual, expected) -> bool:
    if op == "pr":
        return actual not in (None, "", [])
    if isinstance(actual, bool) or isinstance(expected, bool):
        a, e = bool(actual) if actual is not None else None, expected
        if op == "eq":
            return a == e
        if op == "ne":
            return a != e
        return False
    a = "" if actual is None else str(actual).lower()
    e = "" if expected is None else str(expected).lower()
    return {
        "eq": a == e, "ne": a != e, "co": e in a, "sw": a.startswith(e), "ew": a.endswith(e),
        "gt": a > e, "ge": a >= e, "lt": a < e, "le": a <= e,
    }[op]


def eval_filter(ast, attrs: dict) -> bool:
    kind = ast[0]
    if kind == "and":
        return eval_filter(ast[1], attrs) and eval_filter(ast[2], attrs)
    if kind == "or":
        return eval_filter(ast[1], attrs) or eval_filter(ast[2], attrs)
    if kind == "not":
        return not eval_filter(ast[1], attrs)
    op, name, value = ast
    actual = attrs.get(_norm_attr(name))
    if isinstance(actual, (list, tuple, set)):
        if op == "pr":
            return bool(actual)
        if op == "ne":
            return all(_cmp("ne", v, value) for v in actual)
        return any(_cmp(op, v, value) for v in actual)
    return _cmp(op, actual, value)


def _paging(request: Request) -> tuple[int, int]:
    try:
        start = max(1, int(request.query_params.get("startIndex", 1)))
        count = min(200, max(0, int(request.query_params.get("count", 100))))
    except ValueError:
        raise ScimError(400, "startIndex and count must be integers", "invalidValue")
    return start, count


def _list_response(items: list, start: int, count: int) -> JSONResponse:
    page = items[start - 1: start - 1 + count]
    return scim_response({"schemas": [LIST_SCHEMA], "totalResults": len(items), "startIndex": start,
                          "itemsPerPage": len(page), "Resources": page})


def _user_attrs(u: User, m: WorkspaceMember) -> dict:
    parts = (u.name or "").split(" ", 1)
    return {"id": u.id, "username": u.email, "emails": [u.email], "emails.value": [u.email],
            "displayname": u.name or u.email, "name.formatted": u.name or "", "name.givenname": parts[0] if parts else "",
            "name.familyname": parts[1] if len(parts) > 1 else "", "active": m.status == "active",
            "roles": [m.role], "roles.value": [m.role], "externalid": None,
            "meta.created": u.created_at.isoformat() + "Z", "meta.lastmodified": u.updated_at.isoformat() + "Z"}


FILTER_RE = re.compile(r'^\s*(userName|emails(?:\.value)?)\s+eq\s+"([^"]*)"\s*$', re.I)


@router.get("/Users")
@_wrap
def list_users(request: Request, db: Session = Depends(get_db)):
    ws = _auth(request, db)
    q = (
        select(User, WorkspaceMember)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .where(WorkspaceMember.workspace_id == ws.id, WorkspaceMember.status != "invited")
    )
    flt = (request.query_params.get("filter") or "").strip()
    ast = parse_filter(flt) if flt else None
    start, count = _paging(request)
    rows = db.execute(q.order_by(User.created_at, User.id)).all()
    if ast is not None:
        rows = [(u, mem) for u, mem in rows if eval_filter(ast, _user_attrs(u, mem))]
    return _list_response([_user_resource(u, mem) for u, mem in rows], start, count)


@router.post("/Users")
@_wrap
async def create_user(request: Request, db: Session = Depends(get_db)):
    from app.services.bootstrap import add_user_to_workspace

    ws = _auth(request, db)
    try:
        body = await request.json()
    except Exception:
        raise ScimError(400, "Invalid JSON body", "invalidSyntax")
    email = _email_from(body or {})
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ScimError(400, "userName must be an email address", "invalidValue")
    domains = (ws.settings_json or {}).get("approved_domains") or []
    if domains and has_feature(ws, "domain_control") and email.split("@", 1)[1] not in domains:
        raise ScimError(400, "Email domain is not approved for this workspace", "invalidValue")
    existing = db.scalar(
        select(WorkspaceMember).join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == ws.id, User.email == email)
    )
    if existing and existing.status != "invited":
        raise ScimError(409, "User already exists in this workspace", "uniqueness")
    name = _name_from(body) or email.split("@")[0]
    user = add_user_to_workspace(db, ws, email, name, role="member")
    m = db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == ws.id, WorkspaceMember.user_id == user.id))
    # pending email invites for this address are superseded by provisioning
    for inv in db.scalars(select(WorkspaceMember).where(
        WorkspaceMember.workspace_id == ws.id, WorkspaceMember.status == "invited",
        WorkspaceMember.id != m.id, func.lower(WorkspaceMember.invite_email) == email,
    )).all():
        db.delete(inv)
    m.status = "active" if body.get("active", True) else "deactivated"
    if not user.name:
        user.name = name
    audit(db, "scim.user_provisioned", workspace_id=ws.id, actor_id="scim", entity_type="user", entity_id=user.id,
          metadata={"email": email}, ip=client_ip(request))
    db.commit()
    resp = scim_response(_user_resource(user, m), 201)
    resp.headers["Location"] = f"{settings.base_url.rstrip('/')}/scim/v2/Users/{user.id}"
    return resp


@router.get("/Users/{user_id}")
@_wrap
def get_user(user_id: str, request: Request, db: Session = Depends(get_db)):
    ws = _auth(request, db)
    u, m = _member(db, ws, user_id)
    return scim_response(_user_resource(u, m))


def _as_bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() == "true"
    return bool(v)


@router.patch("/Users/{user_id}")
@_wrap
async def patch_user(user_id: str, request: Request, db: Session = Depends(get_db)):
    ws = _auth(request, db)
    u, m = _member(db, ws, user_id)
    try:
        body = await request.json()
    except Exception:
        raise ScimError(400, "Invalid JSON body", "invalidSyntax")
    ops = (body or {}).get("Operations") or []
    if not ops:
        raise ScimError(400, "PatchOp requires Operations", "invalidValue")
    ip = client_ip(request)
    for op in ops:
        kind = str(op.get("op", "")).lower()
        if kind not in ("replace", "add"):
            raise ScimError(400, f"Unsupported op '{op.get('op')}'", "invalidValue")
        path = (op.get("path") or "").strip()
        value = op.get("value")
        items = {path: value} if path else (value if isinstance(value, dict) else {})
        for key, val in items.items():
            k = key.lower()
            if k == "active":
                _set_active(db, ws, u, m, _as_bool(val), ip)
            elif k in ("displayname", "name.formatted"):
                u.name = str(val)[:200]
            elif k == "name" and isinstance(val, dict):
                u.name = _name_from({"name": val}) or u.name
            elif k in ("name.givenname", "name.familyname"):
                parts = (u.name or "").split(" ", 1) + [""]
                given, family = parts[0], parts[1]
                if k == "name.givenname":
                    given = str(val)
                else:
                    family = str(val)
                u.name = f"{given} {family}".strip()
            # other attributes (emails, userName changes, etc.) are ignored
    db.commit()
    return scim_response(_user_resource(u, m))


@router.put("/Users/{user_id}")
@_wrap
async def put_user(user_id: str, request: Request, db: Session = Depends(get_db)):
    ws = _auth(request, db)
    u, m = _member(db, ws, user_id)
    try:
        body = await request.json()
    except Exception:
        raise ScimError(400, "Invalid JSON body", "invalidSyntax")
    name = _name_from(body or {})
    if name:
        u.name = name[:200]
    if "active" in (body or {}):
        _set_active(db, ws, u, m, _as_bool(body["active"]), client_ip(request))
    db.commit()
    return scim_response(_user_resource(u, m))


@router.delete("/Users/{user_id}")
@_wrap
def delete_user(user_id: str, request: Request, db: Session = Depends(get_db)):
    ws = _auth(request, db)
    u, m = _member(db, ws, user_id)
    if u.id == ws.owner_user_id:
        raise ScimError(400, "The workspace owner cannot be deprovisioned via SCIM", "mutability")
    db.delete(m)
    audit(db, "scim.user_deprovisioned", workspace_id=ws.id, actor_id="scim", entity_type="user", entity_id=u.id,
          metadata={"email": u.email}, ip=client_ip(request))
    db.commit()
    return Response(status_code=204)


# =========================================================================== Groups (-> Teams)
def _group_resource(db: Session, t: Team) -> dict:
    rows = db.execute(
        select(TeamMember, User).join(User, User.id == TeamMember.user_id).where(TeamMember.team_id == t.id)
        .order_by(TeamMember.created_at)
    ).all()
    return {
        "schemas": [GROUP_SCHEMA],
        "id": t.id,
        "displayName": t.name,
        "members": [{"value": u.id, "display": u.name or u.email, "$ref": f"{settings.base_url.rstrip('/')}/scim/v2/Users/{u.id}",
                     "type": "User"} for _, u in rows],
        "meta": {"resourceType": "Group", "created": t.created_at.isoformat() + "Z",
                 "lastModified": t.updated_at.isoformat() + "Z",
                 "location": f"{settings.base_url.rstrip('/')}/scim/v2/Groups/{t.id}"},
    }


def _group_attrs(db: Session, t: Team) -> dict:
    ids = db.scalars(select(TeamMember.user_id).where(TeamMember.team_id == t.id)).all()
    return {"id": t.id, "displayname": t.name, "members": list(ids), "members.value": list(ids), "externalid": None}


def _team(db: Session, ws: Workspace, gid: str) -> Team:
    t = db.get(Team, gid)
    if not t or t.workspace_id != ws.id:
        raise ScimError(404, f"Group {gid} not found")
    return t


def _member_ids(value) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out = []
    for it in items:
        v = it.get("value") if isinstance(it, dict) else it
        if v:
            out.append(str(v))
    return out


def _check_users(db: Session, ws: Workspace, user_ids: list[str]) -> None:
    for uid in user_ids:
        if not db.scalar(select(WorkspaceMember.id).where(WorkspaceMember.workspace_id == ws.id,
                                                          WorkspaceMember.user_id == uid,
                                                          WorkspaceMember.status != "invited")):
            raise ScimError(400, f"Member {uid} is not a provisioned user of this workspace", "invalidValue")


def _add_members(db: Session, ws: Workspace, t: Team, user_ids: list[str], ip: str) -> None:
    _check_users(db, ws, user_ids)
    existing = set(db.scalars(select(TeamMember.user_id).where(TeamMember.team_id == t.id)).all())
    for uid in user_ids:
        if uid not in existing:
            db.add(TeamMember(team_id=t.id, user_id=uid, role="member", priority=1, active=True))
            existing.add(uid)
            audit(db, "scim.group_member_added", workspace_id=ws.id, actor_id="scim", entity_type="team",
                  entity_id=t.id, metadata={"user_id": uid}, ip=ip)


def _remove_members(db: Session, ws: Workspace, t: Team, user_ids: Optional[list[str]], ip: str) -> None:
    q = select(TeamMember).where(TeamMember.team_id == t.id)
    if user_ids is not None:
        q = q.where(TeamMember.user_id.in_(user_ids or [""]))
    for tm in db.scalars(q).all():
        audit(db, "scim.group_member_removed", workspace_id=ws.id, actor_id="scim", entity_type="team",
              entity_id=t.id, metadata={"user_id": tm.user_id}, ip=ip)
        db.delete(tm)


def _unique_team_slug(db: Session, name: str) -> str:
    from app.services.bootstrap import slugify

    base = slugify(name) or "team"
    slug, n = base, 2
    while db.scalar(select(Team.id).where(Team.slug == slug)):
        slug, n = f"{base}-{n}", n + 1
    return slug


_MEMBER_PATH_RE = re.compile(r'^members\[\s*value\s+eq\s+"([^"]+)"\s*\]$', re.I)


@router.get("/Groups")
@_wrap
def list_groups(request: Request, db: Session = Depends(get_db)):
    ws = _auth(request, db)
    flt = (request.query_params.get("filter") or "").strip()
    ast = parse_filter(flt) if flt else None
    start, count = _paging(request)
    teams = db.scalars(select(Team).where(Team.workspace_id == ws.id).order_by(Team.created_at, Team.id)).all()
    if ast is not None:
        teams = [t for t in teams if eval_filter(ast, _group_attrs(db, t))]
    excluded = (request.query_params.get("excludedAttributes") or "").lower()
    items = []
    for t in teams:
        r = _group_resource(db, t)
        if "members" in excluded:
            r.pop("members", None)
        items.append(r)
    return _list_response(items, start, count)


@router.post("/Groups")
@_wrap
async def create_group(request: Request, db: Session = Depends(get_db)):
    ws = _auth(request, db)
    try:
        body = await request.json()
    except Exception:
        raise ScimError(400, "Invalid JSON body", "invalidSyntax")
    name = str((body or {}).get("displayName") or "").strip()
    if not name:
        raise ScimError(400, "displayName is required", "invalidValue")
    if db.scalar(select(Team.id).where(Team.workspace_id == ws.id, func.lower(Team.name) == name.lower())):
        raise ScimError(409, "A group with this displayName already exists", "uniqueness")
    t = Team(workspace_id=ws.id, name=name[:200], slug=_unique_team_slug(db, name), description="Provisioned via SCIM")
    db.add(t)
    db.flush()
    ip = client_ip(request)
    _add_members(db, ws, t, _member_ids(body.get("members")), ip)
    audit(db, "scim.group_created", workspace_id=ws.id, actor_id="scim", entity_type="team", entity_id=t.id,
          metadata={"name": t.name}, ip=ip)
    db.commit()
    resp = scim_response(_group_resource(db, t), 201)
    resp.headers["Location"] = f"{settings.base_url.rstrip('/')}/scim/v2/Groups/{t.id}"
    return resp


@router.get("/Groups/{group_id}")
@_wrap
def get_group(group_id: str, request: Request, db: Session = Depends(get_db)):
    ws = _auth(request, db)
    return scim_response(_group_resource(db, _team(db, ws, group_id)))


@router.patch("/Groups/{group_id}")
@_wrap
async def patch_group(group_id: str, request: Request, db: Session = Depends(get_db)):
    ws = _auth(request, db)
    t = _team(db, ws, group_id)
    try:
        body = await request.json()
    except Exception:
        raise ScimError(400, "Invalid JSON body", "invalidSyntax")
    ops = (body or {}).get("Operations") or []
    if not ops:
        raise ScimError(400, "PatchOp requires Operations", "invalidValue")
    ip = client_ip(request)
    for op in ops:
        kind = str(op.get("op", "")).lower()
        path = (op.get("path") or "").strip()
        value = op.get("value")
        if kind not in ("add", "remove", "replace"):
            raise ScimError(400, f"Unsupported op '{op.get('op')}'", "invalidValue")
        m = _MEMBER_PATH_RE.match(path)
        if kind == "remove":
            if m:
                _remove_members(db, ws, t, [m.group(1)], ip)
            elif path.lower() == "members":
                ids = _member_ids(value)
                _remove_members(db, ws, t, ids if value is not None else None, ip)
            else:
                raise ScimError(400, f"Cannot remove '{path}'", "noTarget")
            continue
        if path.lower() == "members":
            if kind == "replace":
                ids = _member_ids(value)
                _check_users(db, ws, ids)
                keep = set(ids)
                current = set(db.scalars(select(TeamMember.user_id).where(TeamMember.team_id == t.id)).all())
                _remove_members(db, ws, t, sorted(current - keep), ip)
                db.flush()
            _add_members(db, ws, t, _member_ids(value), ip)
        elif path.lower() == "displayname" or (not path and isinstance(value, dict)):
            new_name = value if path else (value or {}).get("displayName")
            if not path and isinstance(value, dict) and "members" in value:
                _add_members(db, ws, t, _member_ids(value.get("members")), ip)
            if new_name:
                t.name = str(new_name)[:200]
                audit(db, "scim.group_renamed", workspace_id=ws.id, actor_id="scim", entity_type="team",
                      entity_id=t.id, metadata={"name": t.name}, ip=ip)
        elif path.lower() in ("externalid", "id"):
            continue
        else:
            raise ScimError(400, f"Unsupported path '{path}'", "invalidPath")
        db.flush()
    db.commit()
    if request.query_params.get("excludedAttributes") == "members":
        return Response(status_code=204)
    return scim_response(_group_resource(db, t))


@router.put("/Groups/{group_id}")
@_wrap
async def put_group(group_id: str, request: Request, db: Session = Depends(get_db)):
    ws = _auth(request, db)
    t = _team(db, ws, group_id)
    try:
        body = await request.json()
    except Exception:
        raise ScimError(400, "Invalid JSON body", "invalidSyntax")
    ip = client_ip(request)
    if (body or {}).get("displayName"):
        t.name = str(body["displayName"])[:200]
    if "members" in (body or {}):
        ids = _member_ids(body.get("members"))
        _check_users(db, ws, ids)
        current = set(db.scalars(select(TeamMember.user_id).where(TeamMember.team_id == t.id)).all())
        _remove_members(db, ws, t, sorted(current - set(ids)), ip)
        db.flush()
        _add_members(db, ws, t, ids, ip)
    db.commit()
    return scim_response(_group_resource(db, t))


@router.delete("/Groups/{group_id}")
@_wrap
def delete_group(group_id: str, request: Request, db: Session = Depends(get_db)):
    ws = _auth(request, db)
    t = _team(db, ws, group_id)
    n = db.scalar(select(func.count(EventType.id)).where(EventType.team_id == t.id, EventType.deleted_at.is_(None))) or 0
    if n:
        raise ScimError(409, f"The team still owns {n} event type(s); move or delete them in the app first")
    ip = client_ip(request)
    _remove_members(db, ws, t, None, ip)
    audit(db, "scim.group_deleted", workspace_id=ws.id, actor_id="scim", entity_type="team", entity_id=t.id,
          metadata={"name": t.name}, ip=ip)
    db.flush()
    db.delete(t)
    db.commit()
    return Response(status_code=204)
