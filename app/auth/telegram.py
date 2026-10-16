"""Вход через Telegram Login Widget.

Подтверждение делает сам Telegram (oauth.telegram.org), а нам браузер
приносит данные пользователя с подписью. Подпись проверяется токеном бота
олимпиады — сам бот при этом продолжает работать в Botmother, его вебхук
не используется. Документация: https://core.telegram.org/widgets/login
"""

import hashlib
import hmac
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import TELEGRAM_BOT_TOKEN
from ..db import get_db
from ..security import csrf_ok
from .service import AlreadyLinked, link_to, login_via
from .sessions import SESSION_COOKIE, optional_account, start_session

router = APIRouter()

MAX_AUTH_AGE = 3600
LINK_INTENT_COOKIE = "tg_link"


def verify(data: dict[str, str], bot_token: str, now: float | None = None) -> bool:
    received = data.get("hash", "")
    check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data) if k != "hash")
    secret = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not received or not hmac.compare_digest(expected, received):
        return False
    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError:
        return False
    return (now or time.time()) - auth_date <= MAX_AUTH_AGE


@router.get("/auth/telegram")
async def telegram_callback(request: Request, db: AsyncSession = Depends(get_db)):
    data = dict(request.query_params)
    if not TELEGRAM_BOT_TOKEN or not verify(data, TELEGRAM_BOT_TOKEN):
        return RedirectResponse("/?error=telegram", status_code=303)

    name = " ".join(p for p in (data.get("first_name"), data.get("last_name")) if p) or None
    current = await optional_account(request, db)
    token = request.cookies.get(SESSION_COOKIE, "")
    # Привязка — только если родитель сам нажал «Привязать Telegram» в
    # кабинете (POST с CSRF ставит эту cookie). Иначе чужую подписанную
    # ссылку можно было бы подсунуть вошедшему родителю и привязать к его
    # аккаунту свой Telegram.
    if current is not None and csrf_ok(token, request.cookies.get(LINK_INTENT_COOKIE, "")):
        try:
            await link_to(db, current.id, "telegram", data["id"], name)
        except AlreadyLinked:
            response = RedirectResponse("/cabinet?error=linked_elsewhere", status_code=303)
        else:
            response = RedirectResponse("/cabinet?linked=telegram", status_code=303)
        response.delete_cookie(LINK_INTENT_COOKIE)
        return response

    account_id = await login_via(db, "telegram", data["id"], name)
    response = RedirectResponse("/cabinet", status_code=303)
    await start_session(db, response, account_id)
    return response
