"""Enterprise (PRD 17.2): SAML SSO, domain claims, security policies, SCIM Groups/filters,
API-key data deletion and audit search."""
import base64
import datetime as dt
import re
import zlib
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from sqlalchemy import select

from app.audit import audit
from app.models import (
    Booking,
    EventType,
    Invitee,
    SSOConfig,
    Team,
    TeamMember,
    User,
    Workspace,
    WorkspaceMember,
    utcnow,
)
from app.models_enterprise import DomainClaim, SamlAuthnRequest
from app.security import hash_secret
from app.services import sso as sso_svc
from app.services.bootstrap import add_user_to_workspace

IDP = "https://idp.example.com/entity"


# --------------------------------------------------------------------------- helpers
def _keypair(cn="idp"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=365)).sign(key, hashes.SHA256()))
    return (key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()),
            cert.public_bytes(serialization.Encoding.PEM))


KEY, CERT = _keypair()
KEY2, CERT2 = _keypair("evil")


def _iso(t):
    return t.replace(microsecond=0).isoformat() + "Z"


def make_response(ws, in_response_to, email="jane@acme.com", key=KEY, cert=CERT, audience=None, recipient=None,
                  issuer=IDP, not_before=None, not_on_or_after=None, assertion_id="_assert1", sign="assertion",
                  status="urn:oasis:names:tc:SAML:2.0:status:Success", name="Jane Doe"):
    from signxml import XMLSigner

    sp = sso_svc.sp_info(ws)
    now = utcnow()
    nb = not_before or now - dt.timedelta(minutes=1)
    noa = not_on_or_after or now + dt.timedelta(minutes=5)
    xml = f'''<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_resp1" Version="2.0" IssueInstant="{_iso(now)}" Destination="{sp['acs_url']}" InResponseTo="{in_response_to}">
<saml:Issuer>{issuer}</saml:Issuer>
<samlp:Status><samlp:StatusCode Value="{status}"/></samlp:Status>
<saml:Assertion ID="{assertion_id}" Version="2.0" IssueInstant="{_iso(now)}">
<saml:Issuer>{issuer}</saml:Issuer>
<saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">{email}</saml:NameID>
<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer"><saml:SubjectConfirmationData InResponseTo="{in_response_to}" NotOnOrAfter="{_iso(noa)}" Recipient="{recipient or sp['acs_url']}"/></saml:SubjectConfirmation></saml:Subject>
<saml:Conditions NotBefore="{_iso(nb)}" NotOnOrAfter="{_iso(noa)}"><saml:AudienceRestriction><saml:Audience>{audience or sp['entity_id']}</saml:Audience></saml:AudienceRestriction></saml:Conditions>
<saml:AuthnStatement AuthnInstant="{_iso(now)}"/>
<saml:AttributeStatement><saml:Attribute Name="displayName"><saml:AttributeValue>{name}</saml:AttributeValue></saml:Attribute></saml:AttributeStatement>
</saml:Assertion></samlp:Response>'''
    root = etree.fromstring(xml.encode())
    signer = XMLSigner(c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#")
    if sign == "assertion":
        a = root.find("{urn:oasis:names:tc:SAML:2.0:assertion}Assertion")
        signed = signer.sign(a, key=key, cert=cert, reference_uri=assertion_id)
        root.replace(a, signed)
    elif sign == "response":
        root = signer.sign(root, key=key, cert=cert, reference_uri="_resp1")
    return base64.b64encode(etree.tostring(root)).decode()


def _setup_sso(db, ws, verify=True, enforce=False):
    w = db.get(Workspace, ws.id)
    w.plan_id = "enterprise"
    db.add(SSOConfig(workspace_id=ws.id, enabled=True, idp_entity_id=IDP, idp_sso_url="https://idp.example.com/sso",
                     idp_x509_cert=CERT.decode(), enforce_sso=enforce))
    c = DomainClaim(workspace_id=ws.id, domain="acme.com", token="tok", verified_at=utcnow() if verify else None)
    db.add(c)
    db.commit()


def _start_login(client, ws, nxt="/app/contacts"):
    r = client.get(f"/sso/{ws.slug}/login?next={nxt}", follow_redirects=False)
    assert r.status_code == 302
    loc = urlparse(r.headers["location"])
    assert loc.netloc == "idp.example.com"
    qs = parse_qs(loc.query)
    xml = sso_svc.decode_authn_request(qs["SAMLRequest"][0])
    req_id = re.search(r'ID="([^"]+)"', xml).group(1)
    assert qs["RelayState"][0] == req_id
    assert f'AssertionConsumerServiceURL="{sso_svc.sp_info(ws)["acs_url"]}"' in xml
    return req_id


# --------------------------------------------------------------------------- SAML
def test_saml_login_jit_provisions_and_signs_in(client, db, host):
    user, ws = host
    _setup_sso(db, ws)
    req_id = _start_login(client, ws)
    r = client.post(f"/sso/{ws.slug}/acs", data={"SAMLResponse": make_response(ws, req_id)}, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/app/contacts"
    db.rollback(); db.expire_all()
    jane = db.scalar(select(User).where(User.email == "jane@acme.com"))
    assert jane and jane.name == "Jane Doe"
    m = db.scalar(select(WorkspaceMember).where(WorkspaceMember.user_id == jane.id, WorkspaceMember.workspace_id == ws.id))
    assert m.status == "active" and m.role == "member"
    assert client.get("/app", follow_redirects=False).status_code == 200  # session established
    # replay of the same response is rejected
    r2 = client.post(f"/sso/{ws.slug}/acs", data={"SAMLResponse": make_response(ws, req_id)})
    assert r2.status_code == 403 and "already used" in r2.text


def test_saml_response_signed_at_response_level(client, db, host):
    user, ws = host
    _setup_sso(db, ws)
    req_id = _start_login(client, ws)
    r = client.post(f"/sso/{ws.slug}/acs", data={"SAMLResponse": make_response(ws, req_id, sign="response")},
                    follow_redirects=False)
    assert r.status_code == 303, r.text


@pytest.mark.parametrize("kwargs,needle", [
    ({"key": KEY2, "cert": CERT2}, "signature"),
    ({"sign": None}, "signature"),
    ({"audience": "https://other.example/sp"}, "Audience"),
    ({"recipient": "https://evil.example/acs"}, "SubjectConfirmation"),
    ({"issuer": "https://evil-idp"}, "Issuer"),
    ({"not_on_or_after": dt.datetime(2020, 1, 1)}, "expired"),
    ({"not_before": utcnow() + dt.timedelta(hours=1)}, "not yet valid"),
    ({"status": "urn:oasis:names:tc:SAML:2.0:status:Responder"}, "unsuccessful"),
    ({"email": "mallory@gmail.com"}, "not a verified domain"),
])
def test_saml_rejections(client, db, host, kwargs, needle):
    user, ws = host
    _setup_sso(db, ws)
    req_id = _start_login(client, ws)
    r = client.post(f"/sso/{ws.slug}/acs", data={"SAMLResponse": make_response(ws, req_id, **kwargs)})
    assert r.status_code == 403
    assert needle.lower() in r.text.lower()
    db.rollback(); db.expire_all()
    assert not db.scalar(select(User).where(User.email == kwargs.get("email", "jane@acme.com")))


def test_saml_tampered_assertion_and_unknown_request(client, db, host):
    user, ws = host
    _setup_sso(db, ws)
    req_id = _start_login(client, ws)
    good = base64.b64decode(make_response(ws, req_id)).replace(b"jane@acme.com", b"ceo@acme.com")
    r = client.post(f"/sso/{ws.slug}/acs", data={"SAMLResponse": base64.b64encode(good).decode()})
    assert r.status_code == 403 and "signature" in r.text.lower()
    r = client.post(f"/sso/{ws.slug}/acs", data={"SAMLResponse": make_response(ws, "_unknown", assertion_id="_a9")})
    assert r.status_code == 403 and "InResponseTo" in r.text
    # expired request
    db.rollback()
    req = db.get(SamlAuthnRequest, req_id)
    req.expires_at = utcnow() - dt.timedelta(minutes=1)
    db.commit()
    r = client.post(f"/sso/{ws.slug}/acs", data={"SAMLResponse": make_response(ws, req_id, assertion_id="_a10")})
    assert r.status_code == 403 and "expired" in r.text.lower()


def test_saml_existing_member_and_deactivated(client, db, host):
    user, ws = host
    _setup_sso(db, ws)
    bob = add_user_to_workspace(db, db.get(Workspace, ws.id), "bob@acme.com", "Bob", password="pw123456789")
    db.commit()
    req_id = _start_login(client, ws)
    r = client.post(f"/sso/{ws.slug}/acs", data={"SAMLResponse": make_response(ws, req_id, email="bob@acme.com")},
                    follow_redirects=False)
    assert r.status_code == 303
    db.rollback()
    m = db.scalar(select(WorkspaceMember).where(WorkspaceMember.user_id == bob.id))
    m.status = "deactivated"
    db.commit()
    req_id = _start_login(client, ws)
    r = client.post(f"/sso/{ws.slug}/acs",
                    data={"SAMLResponse": make_response(ws, req_id, email="bob@acme.com", assertion_id="_a2")})
    assert r.status_code == 403 and "deactivated" in r.text


def test_sso_login_disabled_workspace(client, db, host):
    user, ws = host
    assert client.get(f"/sso/{ws.slug}/login", follow_redirects=False).status_code == 404
    assert client.post(f"/sso/{ws.slug}/acs", data={"SAMLResponse": "x"}).status_code == 404


def test_sso_enforcement_and_policies(client, db, host):
    user, ws = host
    _setup_sso(db, ws, enforce=True)
    url = sso_svc.sso_required_for_email(db, "Jane@Acme.com")
    assert url and url.startswith(f"/sso/{ws.slug}/login")
    assert sso_svc.sso_required_for_email(db, "someone@gmail.com") is None
    assert not sso_svc.login_method_allowed(db, "jane@acme.com", "password")
    assert sso_svc.login_method_allowed(db, "jane@acme.com", "sso")
    # unverified domain -> no enforcement (a workspace cannot capture a domain it hasn't proven)
    claim = db.scalar(select(DomainClaim).where(DomainClaim.workspace_id == ws.id))
    claim.verified_at = None
    db.commit()
    assert sso_svc.sso_required_for_email(db, "jane@acme.com") is None
    claim.verified_at = utcnow()
    cfg = db.scalar(select(SSOConfig).where(SSOConfig.workspace_id == ws.id))
    cfg.enforce_sso = False
    w = db.get(Workspace, ws.id)
    w.settings_json = {"security_policy": {"allowed_login_methods": ["sso", "google"]}}
    db.commit()
    assert sso_svc.sso_required_for_email(db, "jane@acme.com")  # password not an allowed method
    assert sso_svc.login_method_allowed(db, "jane@acme.com", "google")
    assert not sso_svc.login_method_allowed(db, "jane@acme.com", "password")


def test_security_policy_form_and_session_max_age(client, db, host, login):
    user, ws = host
    _setup_sso(db, ws)
    login(client, user)
    r = client.post("/app/admin/security", data={"retention_days": "365", "policy_present": "1",
                                                 "session_max_age_minutes": "60", "method_password": "1",
                                                 "method_sso": "1", "require_sso": "1", "jit_provisioning": "1"})
    assert r.status_code == 200
    db.rollback(); db.expire_all()
    pol = db.get(Workspace, ws.id).settings_json["security_policy"]
    assert pol == {"session_max_age_minutes": 60, "allowed_login_methods": ["password", "sso"], "require_sso": True,
                   "jit_provisioning": True}
    assert sso_svc.sso_required_for_email(db, "x@acme.com")
    # no methods -> error, policy unchanged
    client.post("/app/admin/security", data={"retention_days": "365", "policy_present": "1",
                                             "session_max_age_minutes": "60"})
    db.rollback(); db.expire_all()
    assert db.get(Workspace, ws.id).settings_json["security_policy"]["allowed_login_methods"] == ["password", "sso"]
    # session older than the policy max age is rejected
    w = db.get(Workspace, ws.id)
    assert sso_svc.session_policy_ok({"iat": int(dt.datetime.now(dt.timezone.utc).timestamp()) - 30 * 60}, w)
    assert not sso_svc.session_policy_ok({"iat": int(dt.datetime.now(dt.timezone.utc).timestamp()) - 61 * 60}, w)


# --------------------------------------------------------------------------- domain claims
def test_domain_claim_and_dns_verification(client, db, host, login):
    user, ws = host
    w = db.get(Workspace, ws.id)
    w.plan_id = "enterprise"
    db.commit()
    login(client, user)
    client.post("/app/admin/domains", data={"domain": "Acme.com"})
    db.rollback()
    c = db.scalar(select(DomainClaim).where(DomainClaim.workspace_id == ws.id))
    assert c.domain == "acme.com" and not c.verified_at
    records = {}
    sso_svc.set_resolver(lambda name: records.get(name, []))
    try:
        client.post(f"/app/admin/domains/{c.id}/verify")
        db.rollback(); db.expire_all()
        assert not db.get(DomainClaim, c.id).verified_at
        records["_aal-verification.acme.com"] = ["aal-verification=" + c.token]
        page = client.post(f"/app/admin/domains/{c.id}/verify").text
        db.rollback(); db.expire_all()
        assert db.get(DomainClaim, c.id).verified_at
        assert "acme.com" in db.get(Workspace, ws.id).settings_json["approved_domains"]
        assert "Verified" in page
        # a second workspace cannot verify the same domain
        from app.services.bootstrap import create_user_with_workspace

        _, ows = create_user_with_workspace(db, "o@other.com", "Other", plan_id="enterprise")
        db.commit()
        oc = sso_svc.claim_domain(db, ows.id, "acme.com")
        oc.token = c.token
        assert sso_svc.verify_domain(db, oc) is False and "another workspace" in oc.last_error
    finally:
        sso_svc.set_resolver(None)


# --------------------------------------------------------------------------- SCIM groups + filters
def _scim(db, ws):
    w = db.get(Workspace, ws.id)
    w.plan_id = "enterprise"
    db.add(SSOConfig(workspace_id=ws.id, scim_token_hash=hash_secret("scim_good")))
    db.commit()
    return {"Authorization": "Bearer scim_good"}


def test_scim_user_filters_and_paging(client, db, host):
    user, ws = host
    h = _scim(db, ws)
    for i in range(5):
        client.post("/scim/v2/Users", headers=h, json={"userName": f"u{i}@acme.com", "displayName": f"User {i}",
                                                        "active": i != 3})
    r = client.get("/scim/v2/Users", headers=h, params={"filter": 'userName sw "u" and active eq false'}).json()
    assert r["totalResults"] == 1 and r["Resources"][0]["userName"] == "u3@acme.com"
    r = client.get("/scim/v2/Users", headers=h, params={"filter": 'emails.value co "acme" or userName eq "alice@example.com"',
                                                         "startIndex": 2, "count": 2}).json()
    assert r["totalResults"] == 6 and r["startIndex"] == 2 and r["itemsPerPage"] == 2
    r = client.get("/scim/v2/Users", headers=h, params={"filter": 'not (displayName co "User")'}).json()
    assert [x["userName"] for x in r["Resources"]] == ["alice@example.com"]
    bad = client.get("/scim/v2/Users", headers=h, params={"filter": 'userName xx "a"'})
    assert bad.status_code == 400 and bad.json()["scimType"] == "invalidFilter"
    rt = client.get("/scim/v2/ResourceTypes", headers=h).json()
    assert {x["id"] for x in rt["Resources"]} == {"User", "Group"}
    assert client.get("/scim/v2/Schemas", headers=h).json()["totalResults"] == 2


def test_scim_groups_lifecycle(client, db, host):
    user, ws = host
    h = _scim(db, ws)
    ids = [client.post("/scim/v2/Users", headers=h, json={"userName": f"g{i}@acme.com"}).json()["id"] for i in range(3)]
    r = client.post("/scim/v2/Groups", headers=h, json={"displayName": "Sales EMEA", "members": [{"value": ids[0]}]})
    assert r.status_code == 201, r.text
    gid = r.json()["id"]
    db.rollback()
    team = db.get(Team, gid)
    assert team.workspace_id == ws.id and team.slug == "sales-emea"
    assert client.post("/scim/v2/Groups", headers=h, json={"displayName": "sales emea"}).status_code == 409
    # add + remove (filter path) + rename
    r = client.patch(f"/scim/v2/Groups/{gid}", headers=h, json={"schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [{"op": "Add", "path": "members", "value": [{"value": ids[1]}, {"value": ids[2]}]},
                       {"op": "remove", "path": f'members[value eq "{ids[0]}"]'},
                       {"op": "replace", "value": {"displayName": "Sales Europe"}}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["displayName"] == "Sales Europe" and {m["value"] for m in body["members"]} == {ids[1], ids[2]}
    # replace members
    r = client.patch(f"/scim/v2/Groups/{gid}", headers=h, json={"Operations": [
        {"op": "replace", "path": "members", "value": [{"value": ids[0]}]}]})
    assert {m["value"] for m in r.json()["members"]} == {ids[0]}
    # non-member user rejected
    r = client.patch(f"/scim/v2/Groups/{gid}", headers=h, json={"Operations": [
        {"op": "add", "path": "members", "value": [{"value": "usr_nope"}]}]})
    assert r.status_code == 400
    # filters / paging
    client.post("/scim/v2/Groups", headers=h, json={"displayName": "Support"})
    lst = client.get("/scim/v2/Groups", headers=h, params={"filter": 'displayName eq "Sales Europe"'}).json()
    assert lst["totalResults"] == 1 and lst["Resources"][0]["id"] == gid
    lst = client.get("/scim/v2/Groups", headers=h, params={"filter": f'members.value eq "{ids[0]}"'}).json()
    assert lst["totalResults"] == 1
    lst = client.get("/scim/v2/Groups", headers=h, params={"startIndex": 2, "count": 1}).json()
    assert lst["totalResults"] == 2 and lst["itemsPerPage"] == 1
    # PUT replaces members
    r = client.put(f"/scim/v2/Groups/{gid}", headers=h, json={"displayName": "Sales", "members": [{"value": ids[2]}]})
    assert r.json()["displayName"] == "Sales" and [m["value"] for m in r.json()["members"]] == [ids[2]]
    # delete blocked while team owns event types, then works
    db.rollback()
    et = EventType(workspace_id=ws.id, owner_user_id=user.id, team_id=gid, name="Demo", slug="scim-demo", duration=30)
    db.add(et)
    db.commit()
    assert client.delete(f"/scim/v2/Groups/{gid}", headers=h).status_code == 409
    db.delete(db.get(EventType, et.id))
    db.commit()
    assert client.delete(f"/scim/v2/Groups/{gid}", headers=h).status_code == 204
    db.rollback(); db.expire_all()
    assert db.get(Team, gid) is None
    assert not db.scalars(select(TeamMember).where(TeamMember.team_id == gid)).all()
    assert client.get(f"/scim/v2/Groups/{gid}", headers=h).status_code == 404


def test_scim_groups_tenant_isolation(client, db, host):
    from app.services.bootstrap import create_user_with_workspace

    user, ws = host
    h = _scim(db, ws)
    _, ows = create_user_with_workspace(db, "o@other.com", "Other", plan_id="enterprise")
    t = Team(workspace_id=ows.id, name="Theirs", slug="theirs")
    db.add(t)
    db.commit()
    assert client.get(f"/scim/v2/Groups/{t.id}", headers=h).status_code == 404
    assert client.get("/scim/v2/Groups", headers=h).json()["totalResults"] == 0


# --------------------------------------------------------------------------- data deletion API + audit
def test_api_key_data_deletion(client, db, host):
    from app.services.apikeys import create_key

    user, ws = host
    w = db.get(Workspace, ws.id)
    w.plan_id = "enterprise"
    et = EventType(workspace_id=ws.id, owner_user_id=user.id, name="Intro", slug="intro-dd", duration=30)
    db.add(et)
    db.flush()
    b = Booking(workspace_id=ws.id, event_type_id=et.id, host_id=user.id, start_at=utcnow(),
                end_at=utcnow() + dt.timedelta(minutes=30), status="confirmed", manage_token="mt-dd")
    db.add(b)
    db.flush()
    db.add(Invitee(booking_id=b.id, name="Victim", email="victim@example.org"))
    _, raw = create_key(db, ws.id, user.id, "dd", ["read", "write"])
    _, ro = create_key(db, ws.id, user.id, "ro", ["read"])
    db.commit()
    assert client.delete("/api/v1/admin/data-deletion", params={"email": "victim@example.org"}).status_code == 401
    assert client.delete("/api/v1/admin/data-deletion", params={"email": "victim@example.org"},
                         headers={"Authorization": f"Bearer {ro}"}).status_code == 403
    assert client.delete("/api/v1/admin/data-deletion", params={"email": "bad"},
                         headers={"Authorization": f"Bearer {raw}"}).status_code == 422
    r = client.delete("/api/v1/data/invitees", params={"email": "Victim@Example.org"},
                      headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200, r.text
    assert r.json()["deleted"]["invitees"] == 1 and "victim" not in r.text.lower()
    db.rollback(); db.expire_all()
    inv = db.scalar(select(Invitee).where(Invitee.booking_id == b.id))
    assert inv.name == "Deleted" and "victim" not in inv.email
    assert db.get(Booking, b.id) is not None  # aggregate row kept
    # member-created keys cannot delete org data
    db.rollback()
    m = add_user_to_workspace(db, db.get(Workspace, ws.id), "m@acme.com", "Mem")
    _, mk = create_key(db, ws.id, m.id, "m", ["read", "write"])
    db.commit()
    assert client.post("/api/v1/admin/data-deletion", json={"email": "x@y.com"},
                       headers={"Authorization": f"Bearer {mk}"}).status_code == 403


def test_audit_free_text_search_and_json_export(client, db, host, login):
    user, ws = host
    w = db.get(Workspace, ws.id)
    w.plan_id = "enterprise"
    audit(db, "event_type.updated", workspace_id=ws.id, actor_id=user.id, entity_type="event_type", entity_id="et_1",
          metadata={"field": "duration-zebra"})
    audit(db, "member.role_changed", workspace_id=ws.id, actor_id=user.id, metadata={"to": "admin"})
    db.commit()
    login(client, user)
    page = client.get("/app/admin/audit", params={"q": "zebra"}).text
    assert "<code>event_type.updated</code>" in page and "<code>member.role_changed</code>" not in page
    r = client.get("/app/admin/audit.json", params={"q": "alice@example.com"})
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 2 and {d["actor_email"] for d in data["data"]} == {"alice@example.com"}
    r = client.get("/app/admin/audit.json", params={"action": "role_changed"}).json()
    assert r["count"] == 1 and r["data"][0]["metadata"] == {"to": "admin"}
