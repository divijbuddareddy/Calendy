"""Real-browser regression test of the booking page JavaScript (booking.js).

Runs only when Playwright + a Chromium-family browser (Edge/Chrome) are available; otherwise skipped.
Starts a uvicorn server on a free port against the test database.
"""
import socket
import threading
import time

import pytest

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def live_server(host, db):
    import uvicorn

    from app.main import app
    from app.models import EventType

    user, ws = host
    db.add(EventType(workspace_id=ws.id, owner_user_id=user.id, name="Intro", slug="intro", duration=30,
                     min_notice_minutes=0, max_days_ahead=30, location_json=[{"type": "phone", "value": ""}]))
    db.commit()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)


def _browser(p):
    for channel in ("msedge", "chrome", None):
        try:
            return p.chromium.launch(channel=channel, headless=True) if channel else p.chromium.launch(headless=True)
        except Exception:
            continue
    pytest.skip("no Chromium-family browser available")


def test_invitee_books_through_real_page(live_server):
    errors = []
    with playwright.sync_playwright() as p:
        b = _browser(p)
        page = b.new_page(timezone_id="America/New_York")
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{live_server}/alice/intro")
        page.wait_for_selector(".cal-day.available", timeout=10000)
        page.locator(".cal-day.available").first.click()
        page.wait_for_selector(".slot", timeout=10000)
        page.locator(".slot").first.click()
        nxt = page.locator("button:has-text('Next'), [data-action=next]")
        if nxt.count() and nxt.first.is_visible():
            nxt.first.click()
        page.wait_for_selector("input[name=name]", state="visible", timeout=10000)
        page.fill("input[name=name]", "Browser Invitee")
        page.fill("input[name=email]", "browser@example.org")
        page.locator("button[type=submit]:visible").last.click()
        page.wait_for_url("**/confirmed**", timeout=15000)
        assert "Browser Invitee" in page.content()
        b.close()
    assert not errors, errors
