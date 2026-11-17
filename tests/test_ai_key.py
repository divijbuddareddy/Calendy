"""Workspace Anthropic API key: entered in Settings -> AI, stored encrypted, used by the assistant and recaps."""
from app.config import settings
from app.db import SessionLocal
from app.models import Workspace
from app.services import ai_key
from app.services import assistant as asst
from app.services import notetaker as nt


def _stored(ws_id):
    """Read the workspace through a fresh session (the fixture session keeps a stale identity map)."""
    s = SessionLocal()
    try:
        w = s.get(Workspace, ws_id)
        return ai_key.workspace_key(w), dict(w.settings_json or {})
    finally:
        s.close()


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_settings_ai_tab_save_verify_and_remove(client, host, login, db, monkeypatch):
    user, ws = host
    login(client, user)
    r = client.get("/app/settings?tab=ai")
    assert r.status_code == 200 and "Off · no API key" in r.text

    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(401))
    r = client.post("/app/settings/ai", data={"api_key": "sk-ant-bad-key-000000"})
    assert "rejected" in r.text  # flash shown after redirect
    assert _stored(ws.id)[0] is None

    seen = {}

    def fake_get(url, headers=None, **k):
        seen["key"] = headers["x-api-key"]
        return _Resp(200)

    monkeypatch.setattr("httpx.get", fake_get)
    client.post("/app/settings/ai", data={"api_key": "sk-ant-api03-goodkey1234"})
    assert seen["key"] == "sk-ant-api03-goodkey1234"
    key, raw = _stored(ws.id)
    assert key == "sk-ant-api03-goodkey1234"
    assert "goodkey1234" not in str(raw)  # encrypted at rest
    page = client.get("/app/settings?tab=ai").text
    assert "workspace key" in page and "sk-ant-api03-goodkey1234" not in page

    client.post("/app/settings/ai", data={"action": "remove"})
    assert _stored(ws.id)[0] is None


def test_effective_key_prefers_workspace_then_env(db, host, monkeypatch):
    _, ws = host
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    assert ai_key.effective_key(db, ws.id) == ""
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-env")
    assert ai_key.effective_key(db, ws.id) == "sk-env"
    w = db.get(Workspace, ws.id)
    ai_key.set_workspace_key(w, "sk-ws")
    db.commit()
    assert ai_key.effective_key(db, ws.id) == "sk-ws"


def test_assistant_and_recap_use_workspace_key(db, host, monkeypatch):
    user, ws = host
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    ai_key.set_workspace_key(db.get(Workspace, ws.id), "sk-ws-123")
    db.commit()
    calls = []

    def fake_post(url, headers=None, json=None, **k):
        calls.append(headers["x-api-key"])
        return _Resp(200, {"content": [{"type": "text", "text": '{"intent": "schedule", "duration_minutes": 45}'}]})

    monkeypatch.setattr("httpx.post", fake_post)
    parsed = asst.interpret(db, user, ws.id, "find some time next week")
    assert calls == ["sk-ws-123"]
    assert parsed["duration"] == 45 and parsed["parser"] == "llm+deterministic"

    assert nt.llm_recap("hello", api_key="") is None
    monkeypatch.setattr("httpx.post", lambda *a, **k: _Resp(200, {"content": [
        {"type": "text", "text": '{"summary": "S", "decisions": [], "action_items": []}'}]}))
    assert nt.llm_recap("hello", api_key="sk-ws-123")["summary"] == "S"



def test_assistant_page_key_form_saves_and_returns(client, host, login, monkeypatch):
    user, ws = host
    login(client, user)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    page = client.get("/app/assistant").text
    assert "Turn on Claude AI" in page and 'name="api_key"' in page
    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp(200))
    r = client.post("/app/settings/ai", data={"api_key": "sk-ant-api03-fromassistant", "next": "/app/assistant"},
                    follow_redirects=False)
    assert r.headers["location"] == "/app/assistant"
    assert _stored(ws.id)[0] == "sk-ant-api03-fromassistant"
    assert "Turn on Claude AI" not in client.get("/app/assistant").text
