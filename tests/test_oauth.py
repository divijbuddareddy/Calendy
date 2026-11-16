"""OAuth 2.0 authorization-code + PKCE flow, token refresh/rotation, revocation, API bearer auth."""
import base64
import re
import secrets
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select

from app.models import UsageEvent
from app.models_commerce import OAuthApp, OAuthToken
from app.services import oauth as O
from app.services.bootstrap import create_user_with_workspace

REDIRECT = "https://client.example.com/cb"


def register(db, user, ws, scopes=("read", "write")):
    db.commit()
    app, secret = O.register_app(db, ws.id, user.id, "Acme CRM", [REDIRECT], list(scopes))
    db.commit()
    return app, secret


def pkce():
    verifier = secrets.token_urlsafe(48)
    return verifier, O.pkce_challenge(verifier)


def authorize(client, app, challenge, scope="read write", state="xyz", decision="allow"):
    q = {"response_type": "code", "client_id": app.client_id, "redirect_uri": REDIRECT, "scope": scope,
         "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
    page = client.get("/oauth/authorize", params=q)
    assert page.status_code == 200 and "Authorize Acme CRM" in page.text
    r = client.post("/oauth/authorize", data=dict(q, decision=decision), follow_redirects=False)
    assert r.status_code == 303
    loc = urlparse(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == REDIRECT
    return parse_qs(loc.query)


def basic(app, secret):
    return {"Authorization": "Basic " + base64.b64encode(f"{app.client_id}:{secret}".encode()).decode()}


def test_full_flow_refresh_and_revoke(db, host, client, login):
    user, ws = host
    app, secret = register(db, user, ws)
    assert app.client_secret_hash != secret and secret not in app.client_secret_hash  # hashed at rest
    login(client, user)
    verifier, challenge = pkce()
    qs = authorize(client, app, challenge)
    assert qs["state"] == ["xyz"]
    code = qs["code"][0]
    # wrong verifier -> invalid_grant
    r = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                          "code_verifier": "x" * 50}, headers=basic(app, secret))
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    # bad client secret -> invalid_client
    r = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                          "code_verifier": verifier}, headers=basic(app, "nope"))
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    r = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                          "code_verifier": verifier, "client_id": app.client_id,
                                          "client_secret": secret})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    tok = r.json()
    assert tok["token_type"] == "Bearer" and tok["access_token"].startswith("aal_at_") and tok["scope"] == "read write"
    # API accepts the access token
    h = {"Authorization": f"Bearer {tok['access_token']}"}
    me = client.get("/api/v1/me", headers=h)
    assert me.status_code == 200 and me.json()["workspace"]["id"] == ws.id and me.json()["auth"]["type"] == "oauth"
    from app.services.usage import flush_usage

    db.commit()
    flush_usage(db)
    db.commit()
    assert db.scalar(select(UsageEvent).where(UsageEvent.meter == "api_calls", UsageEvent.workspace_id == ws.id))
    # code is single use; replay revokes the grant family
    r = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                          "code_verifier": verifier}, headers=basic(app, secret))
    assert r.status_code == 400
    assert client.get("/api/v1/me", headers=h).status_code == 401

    # fresh grant, then refresh rotation
    qs = authorize(client, app, challenge)
    tok = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": qs["code"][0],
                                            "redirect_uri": REDIRECT, "code_verifier": verifier},
                      headers=basic(app, secret)).json()
    r = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                          "scope": "read"}, headers=basic(app, secret))
    assert r.status_code == 200
    tok2 = r.json()
    assert tok2["scope"] == "read" and tok2["access_token"] != tok["access_token"]
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {tok['access_token']}"}).status_code == 401
    h2 = {"Authorization": f"Bearer {tok2['access_token']}"}
    assert client.get("/api/v1/me", headers=h2).status_code == 200
    # read-only token cannot write
    r = client.post("/api/v1/event-types", json={"name": "X", "duration": 30}, headers=h2)
    assert r.status_code == 403 and "Access token" in r.json()["error"]["message"]
    # widening scope on refresh is refused
    r = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tok2["refresh_token"],
                                          "scope": "read write"}, headers=basic(app, secret))
    assert r.json()["error"] == "invalid_scope"
    # RFC 7009 revocation
    r = client.post("/oauth/revoke", data={"token": tok2["access_token"]}, headers=basic(app, secret))
    assert r.status_code == 200
    assert client.get("/api/v1/me", headers=h2).status_code == 401
    r = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tok2["refresh_token"]},
                    headers=basic(app, secret))
    assert r.status_code == 400


def test_pkce_required_and_redirect_validation(db, host, client, login):
    user, ws = host
    app, secret = register(db, user, ws, scopes=("read",))
    login(client, user)
    base = {"response_type": "code", "client_id": app.client_id, "redirect_uri": REDIRECT, "state": "s"}
    r = client.get("/oauth/authorize", params=base, follow_redirects=False)
    assert r.status_code == 303 and "error=invalid_request" in r.headers["location"]
    _, challenge = pkce()
    r = client.get("/oauth/authorize", params=dict(base, code_challenge=challenge, code_challenge_method="plain"),
                   follow_redirects=False)
    assert "error=invalid_request" in r.headers["location"]
    # scope beyond registration
    r = client.get("/oauth/authorize", params=dict(base, code_challenge=challenge, code_challenge_method="S256",
                                                   scope="write"), follow_redirects=False)
    assert "error=invalid_scope" in r.headers["location"]
    # unregistered redirect URI: never redirect there
    r = client.get("/oauth/authorize", params=dict(base, redirect_uri="https://evil.example.com/cb",
                                                   code_challenge=challenge, code_challenge_method="S256"),
                   follow_redirects=False)
    assert r.status_code == 400 and "evil" not in r.headers.get("location", "")
    # deny -> access_denied
    q = dict(base, code_challenge=challenge, code_challenge_method="S256", scope="read")
    r = client.post("/oauth/authorize", data=dict(q, decision="deny"), follow_redirects=False)
    assert "error=access_denied" in r.headers["location"] and "state=s" in r.headers["location"]


def test_authorize_requires_login_and_management_ui(db, host, client, login):
    user, ws = host
    r = client.get("/oauth/authorize?client_id=x", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    login(client, user)
    r = client.post("/app/integrations/oauth-apps", data={"name": "Zap", "redirect_uris": REDIRECT,
                                                          "scopes": ["read", "write"]}, follow_redirects=True)
    assert r.status_code == 200 and "aal_cs_" in r.text
    secret = re.search(r"aal_cs_[\w-]+", r.text).group(0)
    app = db.scalar(select(OAuthApp).where(OAuthApp.name == "Zap"))
    assert app.client_secret_hash != secret
    assert "aal_cs_" not in client.get("/app/integrations/oauth-apps").text  # shown once
    # http (non-localhost) redirect is refused in prod-style validation only; bad URIs always refused
    r = client.post("/app/integrations/oauth-apps", data={"name": "Bad", "redirect_uris": "not a url"},
                    follow_redirects=True)
    assert not db.scalar(select(OAuthApp).where(OAuthApp.name == "Bad"))
    # authorize + user-side revocation
    verifier, challenge = pkce()
    q = {"response_type": "code", "client_id": app.client_id, "redirect_uri": REDIRECT, "scope": "read",
         "code_challenge": challenge, "code_challenge_method": "S256"}
    code = parse_qs(urlparse(client.post("/oauth/authorize", data=dict(q, decision="allow"),
                                         follow_redirects=False).headers["location"]).query)["code"][0]
    tok = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                                            "code_verifier": verifier}, headers=basic(app, secret)).json()
    page = client.get("/app/integrations/oauth-apps").text
    assert "Apps you&#39;ve authorized" in page or "Apps you've authorized" in page
    assert "Revoke access" in page
    client.post(f"/app/integrations/authorized-apps/{app.id}/revoke")
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {tok['access_token']}"}).status_code == 401
    # rotate secret: old one stops working
    r = client.post(f"/app/integrations/oauth-apps/{app.id}/rotate-secret", follow_redirects=True)
    new_secret = re.search(r"aal_cs_[\w-]+", r.text).group(0)
    assert new_secret != secret
    r = client.post("/oauth/revoke", data={"token": "x"}, headers=basic(app, secret))
    assert r.status_code == 401
    assert client.post("/oauth/revoke", data={"token": "x"}, headers=basic(app, new_secret)).status_code == 200
    # delete app revokes all tokens
    client.post(f"/app/integrations/oauth-apps/{app.id}/delete")
    db.commit()
    db.expire_all()
    assert db.get(OAuthApp, app.id).revoked_at is not None
    assert all(t.revoked_at for t in db.scalars(select(OAuthToken).where(OAuthToken.app_id == app.id)).all())
    assert client.get("/.well-known/oauth-authorization-server").json()["code_challenge_methods_supported"] == ["S256"]


def test_token_is_bound_to_the_consented_workspace(db, host, client, login):
    user, ws = host
    other, ows = create_user_with_workspace(db, "mallory@example.com", "Mallory", password="password123", slug="mallory")
    db.commit()
    app, secret = register(db, other, ows)  # third-party app registered by another workspace
    login(client, user)
    verifier, challenge = pkce()
    qs = authorize(client, app, challenge, scope="read")
    tok = client.post("/oauth/token", data={"grant_type": "authorization_code", "code": qs["code"][0],
                                            "redirect_uri": REDIRECT, "code_verifier": verifier},
                      headers=basic(app, secret)).json()
    me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {tok['access_token']}"}).json()
    assert me["workspace"]["id"] == ws.id and me["user"]["id"] == user.id
