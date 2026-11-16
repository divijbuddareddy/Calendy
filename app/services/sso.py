"""Enterprise identity (PRD 17.2): SAML 2.0 SP-initiated SSO, domain claims, security policies.

SAML flow
---------
1. `/sso/{ws}/login` -> `build_authn_request()` stores the request ID (SamlAuthnRequest) and returns
   the IdP URL with a DEFLATE+base64 `SAMLRequest` (HTTP-Redirect binding) and a RelayState.
2. The IdP POSTs `SAMLResponse` to `/sso/{ws}/acs` -> `consume_response()`:
     * XML signature verified with signxml against the *configured* IdP certificate only
       (KeyInfo certificates are never trusted on their own); only the signed subtree is read
       afterwards (defeats XML-signature-wrapping);
     * Status Success, Issuer == configured IdP entity id, Destination/Recipient == our ACS URL,
       Audience == our SP entity id, NotBefore / NotOnOrAfter (+-3 min skew), Bearer
       SubjectConfirmation, InResponseTo == an unused, unexpired request we issued;
     * replay protection: the Assertion ID is recorded until it expires and the request ID is
       single-use.
3. `login_or_provision()` signs in the member or JIT-provisions them. Only emails on a domain the
   workspace has *verified* (DNS TXT) are accepted, so a workspace IdP can never assert an
   arbitrary third-party address and take over that account.

Hooks for other modules
-----------------------
* `sso_required_for_email(db, email) -> Optional[str]`: login URL when password login must be
  refused for this email (enforce_sso / require_sso policy / password not an allowed method).
* `login_method_allowed(db, email, method) -> bool`  (method: password | google | sso)
* `session_policy_ok(session, workspace) -> bool` workspace session max-age policy.
"""
from __future__ import annotations

import base64
import datetime as dt
import re
import secrets
import zlib
from typing import Callable, Optional
from urllib.parse import urlencode
from xml.sax.saxutils import escape as xml_escape

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.entitlements import has_feature
from app.models import SSOConfig, User, Workspace, WorkspaceMember, utcnow
from app.models_enterprise import DomainClaim, SamlAssertionSeen, SamlAuthnRequest

NS = {
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
}
STATUS_SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"
BEARER = "urn:oasis:names:tc:SAML:2.0:cm:bearer"
CLOCK_SKEW = dt.timedelta(minutes=3)
REQUEST_TTL = dt.timedelta(minutes=10)
LOGIN_METHODS = ("password", "google", "sso")


class SamlError(Exception):
    """Assertion rejected. The message is safe to show to the user (no secrets)."""


# =========================================================================== SP info
def sp_info(ws: Workspace) -> dict:
    base = settings.base_url.rstrip("/")
    return {
        "entity_id": f"{base}/sso/{ws.slug}/metadata",
        "acs_url": f"{base}/sso/{ws.slug}/acs",
        "login_url": f"{base}/sso/{ws.slug}/login",
        "metadata_url": f"{base}/sso/{ws.slug}/metadata",
        "scim_base": f"{base}/scim/v2",
    }


def normalize_cert(pem: str) -> str:
    text = (pem or "").strip()
    if "BEGIN CERTIFICATE" not in text:
        body = re.sub(r"\s+", "", text)
        text = "-----BEGIN CERTIFICATE-----\n" + "\n".join(body[i:i + 64] for i in range(0, len(body), 64)) + \
            "\n-----END CERTIFICATE-----"
    return text + ("\n" if not text.endswith("\n") else "")


def get_config(db: Session, ws_id: str) -> Optional[SSOConfig]:
    return db.scalar(select(SSOConfig).where(SSOConfig.workspace_id == ws_id))


def sso_active(ws: Workspace, cfg: Optional[SSOConfig]) -> bool:
    return bool(cfg and cfg.enabled and cfg.idp_sso_url and cfg.idp_x509_cert and has_feature(ws, "sso"))


# =========================================================================== AuthnRequest
def _iso(t: dt.datetime) -> str:
    return t.replace(microsecond=0).isoformat() + "Z"


def build_authn_request(db: Session, ws: Workspace, cfg: SSOConfig, next_url: str = "",
                        login_hint: str = "") -> str:
    """Create + persist an AuthnRequest and return the IdP redirect URL (HTTP-Redirect binding)."""
    sp = sp_info(ws)
    req_id = "_" + secrets.token_hex(20)
    now = utcnow()
    xml = (
        f'<samlp:AuthnRequest xmlns:samlp="{NS["samlp"]}" xmlns:saml="{NS["saml"]}" ID="{req_id}" Version="2.0" '
        f'IssueInstant="{_iso(now)}" Destination="{xml_escape(cfg.idp_sso_url or "")}" '
        f'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'AssertionConsumerServiceURL="{xml_escape(sp["acs_url"])}">'
        f'<saml:Issuer>{xml_escape(sp["entity_id"])}</saml:Issuer>'
        '<samlp:NameIDPolicy Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress" AllowCreate="true"/>'
        '</samlp:AuthnRequest>'
    )
    deflated = zlib.compress(xml.encode())[2:-4]  # raw DEFLATE (strip zlib header + adler32)
    db.add(SamlAuthnRequest(id=req_id, workspace_id=ws.id, relay_next=(next_url or "")[:500] or None,
                            created_at=now, expires_at=now + REQUEST_TTL))
    # housekeeping: drop long-expired requests / seen assertions
    db.execute(delete(SamlAuthnRequest).where(SamlAuthnRequest.expires_at < now - dt.timedelta(days=1)))
    db.execute(delete(SamlAssertionSeen).where(SamlAssertionSeen.expires_at < now - dt.timedelta(days=1)))
    db.flush()
    params = {"SAMLRequest": base64.b64encode(deflated).decode(), "RelayState": req_id}
    if login_hint:
        params["login_hint"] = login_hint
    sep = "&" if "?" in (cfg.idp_sso_url or "") else "?"
    return f"{cfg.idp_sso_url}{sep}{urlencode(params)}"


def decode_authn_request(saml_request: str) -> str:
    """Inverse of the redirect encoding (used by tests / debugging)."""
    return zlib.decompress(base64.b64decode(saml_request), -15).decode()


# =========================================================================== Response validation
def _parse_time(v: Optional[str]) -> Optional[dt.datetime]:
    if not v:
        return None
    v = v.strip().replace("Z", "+00:00")
    try:
        t = dt.datetime.fromisoformat(v)
    except ValueError:
        # fractional seconds with >6 digits
        m = re.match(r"^(.*?\.\d{6})\d*(.*)$", v)
        if not m:
            raise SamlError("Malformed timestamp in SAML assertion")
        t = dt.datetime.fromisoformat(m.group(1) + m.group(2))
    if t.tzinfo:
        t = t.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return t


def _verify_signature(xml_bytes: bytes, cert_pem: str):
    try:
        from signxml import XMLVerifier
    except ImportError as e:  # pragma: no cover
        raise SamlError("SAML signature verification library (signxml) is not installed") from e
    try:
        return XMLVerifier().verify(xml_bytes, x509_cert=normalize_cert(cert_pem)).signed_xml
    except Exception as e:
        raise SamlError("The SAML response signature is invalid or was not made with the configured IdP certificate") from e


def validate_response(db: Session, ws: Workspace, cfg: SSOConfig, saml_response_b64: str,
                      now: Optional[dt.datetime] = None) -> dict:
    """Validate a base64 SAMLResponse. Returns {"email","name","attributes","request","assertion_id"}.
    Raises SamlError. Records replay-protection state in the session (caller commits)."""
    from lxml import etree

    now = now or utcnow()
    sp = sp_info(ws)
    try:
        raw = base64.b64decode(saml_response_b64, validate=False)
    except Exception as e:
        raise SamlError("SAMLResponse is not valid base64") from e
    if len(raw) > 512 * 1024:
        raise SamlError("SAMLResponse too large")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, remove_comments=False)
    try:
        root = etree.fromstring(raw, parser=parser)
    except Exception as e:
        raise SamlError("SAMLResponse is not well-formed XML") from e
    if root.tag != f"{{{NS['samlp']}}}Response":
        raise SamlError("Expected a samlp:Response")
    if root.find("saml:EncryptedAssertion", NS) is not None:
        raise SamlError("Encrypted assertions are not supported; disable assertion encryption at the IdP")

    # Status (outer, before signature: a failed login need not be signed)
    code = root.find("samlp:Status/samlp:StatusCode", NS)
    if code is None or code.get("Value") != STATUS_SUCCESS:
        raise SamlError("The identity provider reported an unsuccessful sign-in")
    dest = root.get("Destination")
    if dest and dest != sp["acs_url"]:
        raise SamlError("Response Destination does not match this workspace's ACS URL")

    signed = _verify_signature(raw, cfg.idp_x509_cert or "")
    if signed.tag == f"{{{NS['samlp']}}}Response":
        assertions = signed.findall("saml:Assertion", NS)
        if len(assertions) != 1:
            raise SamlError("Signed response must contain exactly one assertion")
        assertion = assertions[0]
        in_resp_outer = signed.get("InResponseTo")
    elif signed.tag == f"{{{NS['saml']}}}Assertion":
        assertion = signed
        in_resp_outer = root.get("InResponseTo")
    else:
        raise SamlError("The signature does not cover the assertion")
    if len(root.findall(".//saml:Assertion", NS)) != 1:
        raise SamlError("Response must contain exactly one assertion")

    issuer = (assertion.findtext("saml:Issuer", namespaces=NS) or "").strip()
    if cfg.idp_entity_id and issuer != cfg.idp_entity_id:
        raise SamlError("Assertion Issuer does not match the configured IdP entity ID")

    # Conditions
    cond = assertion.find("saml:Conditions", NS)
    if cond is None:
        raise SamlError("Assertion has no Conditions")
    nb, noa = _parse_time(cond.get("NotBefore")), _parse_time(cond.get("NotOnOrAfter"))
    if nb and now + CLOCK_SKEW < nb:
        raise SamlError("Assertion is not yet valid (NotBefore)")
    if not noa or now - CLOCK_SKEW >= noa:
        raise SamlError("Assertion has expired (NotOnOrAfter)")
    audiences = [(a.text or "").strip() for a in cond.findall("saml:AudienceRestriction/saml:Audience", NS)]
    if sp["entity_id"] not in audiences:
        raise SamlError("Assertion Audience does not match this service provider")

    # Subject + bearer confirmation
    subject = assertion.find("saml:Subject", NS)
    name_id = (subject.findtext("saml:NameID", namespaces=NS) if subject is not None else "") or ""
    scd_ok, in_resp = False, None
    for sc in (subject.findall("saml:SubjectConfirmation", NS) if subject is not None else []):
        if sc.get("Method") != BEARER:
            continue
        data = sc.find("saml:SubjectConfirmationData", NS)
        if data is None:
            continue
        exp = _parse_time(data.get("NotOnOrAfter"))
        if not exp or now - CLOCK_SKEW >= exp:
            continue
        if data.get("Recipient") != sp["acs_url"]:
            continue
        scd_ok, in_resp = True, data.get("InResponseTo")
        break
    if not scd_ok:
        raise SamlError("No valid bearer SubjectConfirmation (Recipient / NotOnOrAfter) in the assertion")

    # InResponseTo: SP-initiated only, single use
    in_resp = in_resp or in_resp_outer
    if not in_resp:
        raise SamlError("Unsolicited (IdP-initiated) responses are not accepted; start sign-in from the app")
    if in_resp_outer and in_resp_outer != in_resp:
        raise SamlError("InResponseTo mismatch")
    req = db.get(SamlAuthnRequest, in_resp)
    if not req or req.workspace_id != ws.id:
        raise SamlError("InResponseTo does not match a sign-in request from this workspace")
    if req.used_at:
        raise SamlError("This sign-in request was already used (possible replay)")
    if req.expires_at < now:
        raise SamlError("The sign-in request has expired; please try again")

    # Replay protection on Assertion ID
    aid = assertion.get("ID") or ""
    if not aid:
        raise SamlError("Assertion has no ID")
    if db.scalar(select(SamlAssertionSeen.id).where(SamlAssertionSeen.workspace_id == ws.id,
                                                     SamlAssertionSeen.assertion_id == aid)):
        raise SamlError("This assertion was already used (replay)")
    req.used_at = now
    db.add(SamlAssertionSeen(workspace_id=ws.id, assertion_id=aid, expires_at=noa + CLOCK_SKEW))
    db.flush()

    attrs: dict[str, list[str]] = {}
    for a in assertion.findall("saml:AttributeStatement/saml:Attribute", NS):
        key = a.get("Name") or ""
        attrs[key] = [(v.text or "").strip() for v in a.findall("saml:AttributeValue", NS)]

    def first(*names):
        for n in names:
            for k, v in attrs.items():
                if k.lower() == n.lower() or k.lower().endswith("/" + n.lower()):
                    if v and v[0]:
                        return v[0]
        return ""

    email = (first("email", "mail", "emailaddress") or name_id).strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise SamlError("The assertion does not carry an email address")
    name = first("displayName", "name") or " ".join(p for p in (first("givenName", "firstName"),
                                                                  first("surname", "sn", "lastName")) if p)
    return {"email": email, "name": name.strip(), "attributes": attrs, "request": req, "assertion_id": aid,
            "next": req.relay_next}


def login_or_provision(db: Session, ws: Workspace, cfg: SSOConfig, ident: dict) -> User:
    """Resolve the SAML identity to a workspace member, JIT-provisioning if allowed."""
    from app.audit import audit
    from app.services.bootstrap import add_user_to_workspace

    email = ident["email"]
    domain = email.split("@", 1)[1]
    if domain not in verified_domains(db, ws.id):
        raise SamlError(f"'{domain}' is not a verified domain of this workspace. An admin must verify it before "
                        "members on it can use SSO.")
    user = db.scalar(select(User).where(User.email == email))
    member = None
    if user:
        member = db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == ws.id,
                                                         WorkspaceMember.user_id == user.id))
        if user.deleted_at:
            raise SamlError("This account has been deleted")
    if member and member.status == "deactivated":
        raise SamlError("Your membership in this workspace is deactivated. Contact your administrator.")
    if not member or member.status != "active":
        if not policy(ws).get("jit_provisioning", True):
            raise SamlError("You are not a member of this workspace and just-in-time provisioning is disabled")
        user = add_user_to_workspace(db, ws, email, ident.get("name") or email.split("@")[0])
        member = db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == ws.id,
                                                         WorkspaceMember.user_id == user.id))
        member.status = "active"
        audit(db, "sso.user_jit_provisioned", workspace_id=ws.id, actor_id="sso", entity_type="user",
              entity_id=user.id, metadata={"domain": domain})
    if not user.email_verified_at:
        user.email_verified_at = utcnow()
    if ident.get("name") and not user.name:
        user.name = ident["name"][:200]
    db.flush()
    return user


# =========================================================================== security policies
DEFAULT_POLICY = {"session_max_age_minutes": None, "require_sso": False,
                  "allowed_login_methods": list(LOGIN_METHODS), "jit_provisioning": True}


def policy(ws: Workspace) -> dict:
    p = dict(DEFAULT_POLICY)
    p.update((ws.settings_json or {}).get("security_policy") or {})
    return p


def _enterprise_workspaces_for_email(db: Session, email: str) -> list[tuple[Workspace, Optional[SSOConfig]]]:
    """Workspaces that have *verified* the email's domain (the only ones allowed to impose policy on it)."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return []
    domain = email.rsplit("@", 1)[1]
    claims = db.scalars(select(DomainClaim).where(DomainClaim.domain == domain, DomainClaim.verified_at.is_not(None))).all()
    out = []
    for c in claims:
        ws = db.get(Workspace, c.workspace_id)
        if ws:
            out.append((ws, get_config(db, ws.id)))
    return out


def sso_required_for_email(db: Session, email: str) -> Optional[str]:
    """Return the SSO login URL if password login must be refused for this email, else None.

    True when a workspace that verified the email's domain has active SAML SSO and either
    `enforce_sso`, the `require_sso` policy, or an allowed-login-methods policy without "password"."""
    for ws, cfg in _enterprise_workspaces_for_email(db, email):
        if not sso_active(ws, cfg):
            continue
        p = policy(ws)
        if cfg.enforce_sso or p.get("require_sso") or "password" not in (p.get("allowed_login_methods") or LOGIN_METHODS):
            return f"/sso/{ws.slug}/login?" + urlencode({"login_hint": email.strip().lower()})
    return None


def login_method_allowed(db: Session, email: str, method: str) -> bool:
    """Whether `method` (password|google|sso) may be used for this email under domain-owner policies."""
    for ws, cfg in _enterprise_workspaces_for_email(db, email):
        p = policy(ws)
        allowed = p.get("allowed_login_methods") or list(LOGIN_METHODS)
        if method == "sso":
            if not sso_active(ws, cfg):
                return False
            continue
        if sso_active(ws, cfg) and (cfg.enforce_sso or p.get("require_sso")):
            return False
        if method not in allowed:
            return False
    return True


def session_policy_ok(session, ws: Optional[Workspace]) -> bool:
    """Workspace-level absolute session lifetime (custom security policy)."""
    if not ws:
        return True
    max_age = policy(ws).get("session_max_age_minutes")
    if not max_age:
        return True
    iat = session.get("iat")
    if iat is None:
        return True
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    return now - int(iat) <= int(max_age) * 60


# =========================================================================== domain claims (DNS TXT)
TXT_PREFIX = "aal-verification="


def _default_resolver(name: str) -> list[str]:
    import dns.resolver  # dnspython

    try:
        answers = dns.resolver.resolve(name, "TXT", lifetime=5)
    except Exception:
        return []
    out = []
    for r in answers:
        out.append(b"".join(r.strings).decode(errors="replace"))
    return out


_resolver: Callable[[str], list[str]] = _default_resolver


def set_resolver(fn: Optional[Callable[[str], list[str]]]) -> None:
    """Swap the TXT resolver (tests / offline mode). None restores the DNS resolver."""
    global _resolver
    _resolver = fn or _default_resolver


def claim_domain(db: Session, ws_id: str, domain: str) -> DomainClaim:
    domain = domain.strip().lower().lstrip("@")
    c = db.scalar(select(DomainClaim).where(DomainClaim.workspace_id == ws_id, DomainClaim.domain == domain))
    if not c:
        c = DomainClaim(workspace_id=ws_id, domain=domain, token=secrets.token_urlsafe(24))
        db.add(c)
        db.flush()
    return c


def txt_record(c: DomainClaim) -> dict:
    return {"host": f"_aal-verification.{c.domain}", "alt_host": c.domain, "value": TXT_PREFIX + c.token}


def verify_domain(db: Session, claim: DomainClaim) -> bool:
    claim.last_checked_at = utcnow()
    other = db.scalar(select(DomainClaim).where(DomainClaim.domain == claim.domain,
                                                DomainClaim.verified_at.is_not(None),
                                                DomainClaim.workspace_id != claim.workspace_id))
    if other:
        claim.last_error = "This domain is already verified by another workspace."
        return False
    want = TXT_PREFIX + claim.token
    records = []
    for host in (f"_aal-verification.{claim.domain}", claim.domain):
        try:
            records += [r.strip().strip('"') for r in _resolver(host)]
        except Exception:
            pass
    if want in records:
        claim.verified_at = claim.verified_at or utcnow()
        claim.last_error = None
        return True
    claim.last_error = "TXT record not found yet (DNS changes can take a while to propagate)."
    return False


def verified_domains(db: Session, ws_id: str) -> set[str]:
    return set(db.scalars(select(DomainClaim.domain).where(DomainClaim.workspace_id == ws_id,
                                                           DomainClaim.verified_at.is_not(None))).all())
