import hashlib
import hmac
import os
import tempfile
import time
from pathlib import Path

import pytest

_DB_PATH = Path(tempfile.gettempdir()) / "olymp_cabinet_test.db"

os.environ.update(
    DATABASE_URL=f"sqlite+aiosqlite:///{_DB_PATH.as_posix()}",
    COOKIE_SECURE="false",
    SECRET_KEY="test-secret",
    TELEGRAM_BOT_TOKEN="123:test-token",
    TELEGRAM_BOT_USERNAME="olymp_test_bot",
    MAX_BOT_TOKEN="max-test-token",
    MAX_BOT_USERNAME="olymp_login_bot",
    MAX_WEBHOOK_SECRET="max-secret",
    FORM_URL_GRADE_3="https://forms.yandex.ru/u/test/?code={code}",
)

from fastapi.testclient import TestClient  # noqa: E402

from app.auth import max as max_auth  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture
def sent(monkeypatch):
    """Сообщения и ответы бота MAX вместо настоящих запросов к API."""
    calls = {"messages": [], "answers": []}

    async def fake_send(user_id, text, buttons=None):
        calls["messages"].append({"user_id": user_id, "text": text, "buttons": buttons})

    async def fake_answer(callback_id, notification):
        calls["answers"].append(notification)

    async def fake_subscribe():
        pass

    monkeypatch.setattr(max_auth, "send_message", fake_send)
    monkeypatch.setattr(max_auth, "answer_callback", fake_answer)
    monkeypatch.setattr(max_auth, "subscribe_webhook", fake_subscribe)
    max_auth._CODE_ATTEMPTS.clear()
    return calls


@pytest.fixture
def client(sent):
    _DB_PATH.unlink(missing_ok=True)
    with TestClient(app, follow_redirects=False) as c:
        yield c
    _DB_PATH.unlink(missing_ok=True)


@pytest.fixture
def make_client(sent):
    """Второй «браузер» с теми же данными в базе."""
    clients = []

    def factory():
        c = TestClient(app, follow_redirects=False)
        c.__enter__()
        clients.append(c)
        return c

    yield factory
    for c in clients:
        c.__exit__(None, None, None)


def telegram_params(user_id: int = 1001, first_name: str = "Анна", token: str = "123:test-token"):
    data = {"id": str(user_id), "first_name": first_name, "auth_date": str(int(time.time()))}
    check = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret = hashlib.sha256(token.encode()).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return data


def csrf_of(client) -> str:
    from app.security import csrf_token

    return csrf_token(client.cookies.get("sid"))


def login_telegram(client, user_id: int = 1001):
    resp = client.get("/auth/telegram", params=telegram_params(user_id))
    assert resp.status_code == 303
    return resp


def fill_profile(client):
    return client.post(
        "/profile",
        data={
            "csrf": csrf_of(client),
            "full_name": "Иванова Анна Петровна",
            "phone": "8 (900) 123-45-67",
            "email": "Anna@Example.ru",
            "consent": "yes",
        },
    )


def add_child(client, **overrides):
    data = {
        "csrf": csrf_of(client),
        "last_name": "Иванов",
        "first_name": "Пётр",
        "middle_name": "",
        "grade": "3",
        "city": "Казань",
        "school": "Лицей № 131",
    }
    data.update(overrides)
    return client.post("/children/new", data=data)
