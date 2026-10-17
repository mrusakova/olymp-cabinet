import asyncio
from datetime import timedelta

from sqlalchemy import select

from app import config
from app.auth.telegram import verify
from app.db import SessionLocal
from app.models import Account, B24Outbox, MessengerLink, Participant, utcnow

from .conftest import add_child, csrf_of, fill_profile, login_telegram, telegram_params


def _query(stmt):
    async def run():
        async with SessionLocal() as db:
            return (await db.execute(stmt)).scalars().all()

    return asyncio.run(run())


# --- Telegram ------------------------------------------------------------------


def test_telegram_signature():
    data = telegram_params()
    assert verify(data, "123:test-token")
    assert not verify(data, "123:other-token")
    tampered = dict(data, id="9999")
    assert not verify(tampered, "123:test-token")
    old = telegram_params()
    assert not verify(old, "123:test-token", now=int(old["auth_date"]) + 7200)


def test_bad_telegram_signature_does_not_log_in(client):
    params = telegram_params()
    params["hash"] = "0" * 64
    resp = client.get("/auth/telegram", params=params)
    assert resp.headers["location"] == "/?error=telegram"
    assert "sid" not in client.cookies


def test_first_login_requires_profile_and_consent(client):
    login_telegram(client)
    assert client.get("/cabinet").headers["location"] == "/profile"

    resp = client.post(
        "/profile",
        data={
            "csrf": csrf_of(client),
            "full_name": "Иванова Анна",
            "phone": "+79001234567",
            "email": "a@b.ru",
        },
    )
    assert resp.status_code == 200
    assert "Без согласия" in resp.text

    assert fill_profile(client).headers["location"] == "/cabinet"
    account = _query(select(Account))[0]
    assert account.phone == "+79001234567"
    assert account.email == "anna@example.ru"
    assert account.consent_version == config.CONSENT_VERSION
    assert client.get("/cabinet").status_code == 200


def test_repeat_login_finds_same_account(client, make_client):
    login_telegram(client)
    fill_profile(client)
    other = make_client()
    login_telegram(other)
    assert other.get("/cabinet").status_code == 200
    assert len(_query(select(Account))) == 1


def test_csrf_required(client):
    login_telegram(client)
    resp = client.post("/profile", data={"full_name": "X Y", "phone": "", "email": ""})
    assert resp.status_code == 403


def test_logout(client):
    login_telegram(client)
    fill_profile(client)
    client.post("/logout", data={"csrf": csrf_of(client)})
    assert client.get("/cabinet").headers["location"] == "/"


# --- дети и тест ----------------------------------------------------------------


def test_add_child_and_outbox(client, monkeypatch):
    monkeypatch.setattr(config, "QUAL_OPENS_AT", None)
    login_telegram(client)
    fill_profile(client)
    assert add_child(client).headers["location"] == "/cabinet"
    assert add_child(client, first_name="Мария", grade="5").status_code == 303

    children = _query(select(Participant).order_by(Participant.id))
    assert [c.first_name for c in children] == ["Пётр", "Мария"]
    assert all(len(c.code) == 8 for c in children)
    assert children[0].code != children[1].code

    page = client.get("/cabinet").text
    assert children[0].code in page and "Иванов Пётр" in page

    actions = sorted(r.action for r in _query(select(B24Outbox)))
    assert actions == ["upsert_contact", "upsert_deal", "upsert_deal"]


def test_child_validation(client):
    login_telegram(client)
    fill_profile(client)
    resp = add_child(client, grade="9", school="")
    assert resp.status_code == 200
    assert "Выберите класс" in resp.text
    assert _query(select(Participant)) == []


def test_cannot_edit_someone_elses_child(client, make_client):
    login_telegram(client, user_id=1)
    fill_profile(client)
    add_child(client)
    child_id = _query(select(Participant))[0].id

    other = make_client()
    login_telegram(other, user_id=2)
    fill_profile(other)
    assert other.get(f"/children/{child_id}/edit").status_code == 404
    assert other.get(f"/test/{child_id}").status_code == 404


def test_test_button_respects_window(client, monkeypatch):
    login_telegram(client)
    fill_profile(client)
    add_child(client)
    child = _query(select(Participant))[0]

    monkeypatch.setattr(config, "QUAL_OPENS_AT", utcnow() + timedelta(hours=1))
    assert client.get(f"/test/{child.id}").headers["location"] == "/cabinet"

    monkeypatch.setattr(config, "QUAL_OPENS_AT", utcnow() - timedelta(hours=1))
    monkeypatch.setattr(config, "QUAL_CLOSES_AT", utcnow() + timedelta(hours=1))
    assert (
        client.get(f"/test/{child.id}").headers["location"]
        == f"https://forms.yandex.ru/u/test/?code={child.code}"
    )
    # После старта данные ребёнка менять нельзя.
    assert add_child(client).status_code == 403

    monkeypatch.setattr(config, "QUAL_CLOSES_AT", utcnow() - timedelta(minutes=1))
    assert client.get(f"/test/{child.id}").headers["location"] == "/cabinet"


# --- MAX -----------------------------------------------------------------------


def _webhook(client, update, secret="max-secret"):
    return client.post("/max/webhook", json=update, headers={"X-Max-Bot-Api-Secret": secret})


def _start_max(client, **data):
    resp = client.post("/auth/max/start", data={"purpose": "login", **data})
    assert resp.status_code == 303
    request_id = int(resp.headers["location"].rsplit("/", 1)[1])
    page = client.get(resp.headers["location"])
    assert page.status_code == 200
    from app.models import LoginRequest

    return _query(select(LoginRequest).where(LoginRequest.id == request_id))[0]


def _press_confirm(client, sent, user_id=555):
    button = sent["messages"][-1]["buttons"][0][0]
    _webhook(
        client,
        {
            "update_type": "message_callback",
            "callback": {
                "callback_id": "cb1",
                "payload": button["payload"],
                "user": {"user_id": user_id, "name": "Анна И."},
            },
        },
    )


def test_max_login_via_start(client, sent):
    req = _start_max(client)
    assert client.get(f"/auth/max/status/{req.id}").json() == {"status": "pending"}

    _webhook(
        client,
        {"update_type": "bot_started", "chat_id": 1, "user": {"user_id": 555}, "payload": req.token},
    )
    assert sent["messages"][-1]["user_id"] == 555
    assert "Подтвердите вход" in sent["messages"][-1]["text"]
    # Одного «Старт» мало — без нажатия «Подтвердить» входа нет.
    assert client.get(f"/auth/max/status/{req.id}").json() == {"status": "pending"}

    _press_confirm(client, sent)
    assert sent["answers"] == ["Готово!"]
    # Повторное нажатие той же кнопки не ломает вход.
    _press_confirm(client, sent)
    assert sent["answers"][-1] == "Уже подтверждено."
    resp = client.get(f"/auth/max/status/{req.id}")
    assert resp.json() == {"status": "ok", "redirect": "/cabinet"}
    assert "sid" in client.cookies
    # Повторно забрать сессию по тому же запросу нельзя: cookie браузера
    # уже удалена, а сам запрос помечен used.
    assert client.get(f"/auth/max/status/{req.id}").status_code == 404

    link = _query(select(MessengerLink))[0]
    assert (link.provider, link.external_id, link.display_name) == ("max", "555", "Анна И.")


def test_max_login_via_code_fallback(client, sent):
    req = _start_max(client)
    _webhook(
        client,
        {
            "update_type": "message_created",
            "message": {"sender": {"user_id": 777}, "body": {"text": f"{req.code[:3]} {req.code[3:]}"}},
        },
    )
    _press_confirm(client, sent, user_id=777)
    assert client.get(f"/auth/max/status/{req.id}").json()["status"] == "ok"


def test_max_code_bruteforce_limited(client, sent):
    for _ in range(6):
        _webhook(
            client,
            {"update_type": "message_created", "message": {"sender": {"user_id": 9}, "body": {"text": "000000"}}},
        )
    assert "Слишком много попыток" in sent["messages"][-1]["text"]


def test_max_status_only_for_own_browser(client, make_client, sent):
    req = _start_max(client)
    other = make_client()
    assert other.get(f"/auth/max/status/{req.id}").status_code == 404
    assert other.get(f"/auth/max/wait/{req.id}").status_code == 404


def test_max_webhook_secret(client):
    assert _webhook(client, {"update_type": "bot_started"}, secret="wrong").status_code == 403


def test_max_start_without_payload_says_hello(client, sent):
    _webhook(client, {"update_type": "bot_started", "chat_id": 1, "user": {"user_id": 1}})
    assert "личный кабинет" in sent["messages"][-1]["text"]


def test_link_max_to_telegram_account(client, sent):
    login_telegram(client)
    fill_profile(client)
    req = _start_max(client, purpose="link", csrf=csrf_of(client))
    assert req.purpose == "link"
    _webhook(
        client,
        {"update_type": "bot_started", "chat_id": 1, "user": {"user_id": 555}, "payload": req.token},
    )
    _press_confirm(client, sent)
    assert client.get(f"/auth/max/status/{req.id}").json()["redirect"] == "/cabinet?linked=max"
    assert sorted(link.provider for link in _query(select(MessengerLink))) == ["max", "telegram"]
    assert len(_query(select(Account))) == 1


def test_link_requires_csrf(client):
    login_telegram(client)
    resp = client.post("/auth/max/start", data={"purpose": "link"})
    assert resp.status_code == 403


def test_telegram_link_needs_explicit_intent(client):
    login_telegram(client, user_id=1)
    fill_profile(client)
    # Чужая подписанная ссылка без нажатия «Привязать» — не привязывает,
    # а просто переключает на другой аккаунт.
    client.get("/auth/telegram", params=telegram_params(user_id=2))
    accounts = _query(select(Account))
    assert len(accounts) == 2

    client.cookies.clear()
    login_telegram(client, user_id=1)
    resp = client.post("/cabinet/link/telegram", data={"csrf": csrf_of(client)})
    assert resp.status_code == 200
    resp = client.get("/auth/telegram", params=telegram_params(user_id=3))
    assert resp.headers["location"] == "/cabinet?linked=telegram"
    resp = client.post("/cabinet/link/telegram", data={"csrf": csrf_of(client)})
    resp = client.get("/auth/telegram", params=telegram_params(user_id=2))
    assert resp.headers["location"] == "/cabinet?error=linked_elsewhere"
