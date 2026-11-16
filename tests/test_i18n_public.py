"""Invitee-facing pages are translated: ?lang=, lang cookie, host locale, Accept-Language (app/i18n.py)."""
import re

from app.i18n import CATALOG, SUPPORTED_LOCALES, catalog, format_date
from test_public import DAY, base_body, make_event, post_booking


def _book(client, db, host):
    user, ws = host
    make_event(db, user, ws)
    r = post_booking(client, base_body())
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_every_key_in_all_locales():
    en = set(CATALOG["en"])
    assert set(CATALOG) == set(SUPPORTED_LOCALES)
    for loc in SUPPORTED_LOCALES:
        assert set(CATALOG[loc]) == en, (loc, en ^ set(CATALOG[loc]))
        for key, text in CATALOG[loc].items():
            assert text.strip(), (loc, key)
            # placeholders must match English so .format() / tr() never drop a value
            # (date patterns may pick {day} or the zero-padded {dd})
            if key.startswith("date."):
                assert set(re.findall(r"\{(\w+)\}", text)) <= {"weekday", "day", "dd", "month", "year"}, (loc, key)
            else:
                assert set(re.findall(r"\{(\w+)\}", text)) == set(re.findall(r"\{(\w+)\}", CATALOG["en"][key])), (loc, key)
        assert len(CATALOG[loc]["date.weekdays"].split(",")) == 7
        assert len(CATALOG[loc]["date.weekdays_short"].split(",")) == 7
        assert len(CATALOG[loc]["date.months"].split(",")) == 12


def test_booking_page_lang_es_and_cookie(client, db, host):
    user, ws = host
    make_event(db, user, ws)
    r = client.get("/alice/discovery-call?lang=es")
    assert r.status_code == 200
    html = r.text
    assert '<html lang="es">' in html
    assert "Selecciona fecha y hora" in html and "Programar evento" in html and "Zona horaria" in html
    assert "Select a date &amp; time" not in html and "Schedule event" not in html
    assert "Discovery call" in html  # user content is not translated
    assert "window.AAL_I18N" in html and "Programar evento" in html.split("window.AAL_I18N", 1)[1]
    assert 'id="lang-select"' in html and '<option value="es" lang="es" selected' in html
    assert r.cookies.get("lang") == "es"
    # the cookie keeps Spanish on the next page without ?lang=
    assert "Selecciona fecha y hora" in client.get("/alice/discovery-call").text


def test_server_rendered_dates_localized(client, db, host):
    user, ws = host
    make_event(db, user, ws)
    html = client.get(f"/alice/discovery-call?lang=de&month={DAY:%Y-%m}&date={DAY.isoformat()}&tz=Europe/London").text
    assert format_date(DAY, "de", "day_month") in html  # e.g. "Mittwoch, 18. November"
    assert format_date(DAY, "de", "month_year") in html
    assert DAY.strftime("%A") not in html.split('id="cal-grid"', 1)[1].split("</section>")[0]


def test_confirmed_and_manage_pages_in_spanish(client, db, host):
    res = _book(client, db, host)
    page = client.get(res["next_url"] + "?lang=es")
    assert page.status_code == 200
    assert catalog("es")["confirm.scheduled"] in page.text
    assert "You are scheduled" not in page.text
    assert "America/New_York" in page.text
    manage = client.get(f"/booking/{res['manage_token']}?lang=es")
    assert manage.status_code == 200
    es = catalog("es")
    assert es["manage.your_booking"] in manage.text and es["manage.cancel_booking"] in manage.text
    assert es["status.confirmed"] in manage.text and es["cal.google"] in manage.text
    assert "Your booking" not in manage.text and "Cancel booking" not in manage.text
    cancel = client.get(f"/booking/{res['manage_token']}/cancel?lang=es")
    assert es["manage.keep_booking"] in cancel.text


def test_accept_language_fr(client, db, host):
    user, ws = host
    make_event(db, user, ws)
    r = client.get("/alice/discovery-call", headers={"Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5"})
    assert '<html lang="fr">' in r.text and catalog("fr")["book.select_datetime"] in r.text
    assert "lang" not in r.cookies  # only an explicit ?lang= is remembered
    res = post_booking(client, base_body()).json()
    page = client.get(res["next_url"], headers={"Accept-Language": "fr"})
    assert catalog("fr")["confirm.scheduled"] in page.text


def test_host_locale_and_query_precedence(client, db, host):
    user, ws = host
    user.locale = "de"
    db.commit()
    make_event(db, user, ws)
    assert catalog("de")["book.select_datetime"] in client.get("/alice/discovery-call",
                                                               headers={"Accept-Language": "fr"}).text
    assert catalog("hi")["book.select_datetime"] in client.get("/alice/discovery-call?lang=hi").text


def test_not_found_and_profile_translated(client, db, host):
    r = client.get("/nobody-here?lang=fr")
    assert r.status_code == 404 and catalog("fr")["err.page_missing"] in r.text.replace("&#39;", "'")
    user, ws = host
    make_event(db, user, ws)
    assert catalog("de")["profile.welcome"] in client.get("/alice?lang=de").text


def test_english_default_unchanged(client, db, host):
    user, ws = host
    make_event(db, user, ws)
    html = client.get("/alice/discovery-call").text
    assert '<html lang="en">' in html and "Select a date &amp; time" in html and "Schedule event" in html
