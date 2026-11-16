from app.db import lock_hosts
from app.events import process_outbox, publish, subscribe
from app.security import decrypt, encrypt, sign, unsign


def test_host_fixture_and_lock(db, host):
    user, ws = host
    assert user.slug == "alice"
    lock_hosts(db, [user.id, "usr_other"])
    db.commit()


def test_outbox_roundtrip(db):
    seen = []

    @subscribe("test.topic")
    def _h(db_, ev):
        seen.append(ev.payload_json["x"])

    publish(db, "test.topic", {"x": 1})
    db.commit()
    assert process_outbox(db) == 1
    assert seen == [1]


def test_crypto():
    assert decrypt(encrypt("secret")) == "secret"
    assert unsign(sign({"a": 1}, "s"), "s") == {"a": 1}
