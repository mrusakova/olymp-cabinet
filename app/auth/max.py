"""Вход и привязка через собственного бота в MAX.

Схема:
1. Браузер создаёт LoginRequest и показывает ссылку
   max.ru/<бот>?start=<token> и запасной 6-значный код.
2. Родитель жмёт «Старт» (MAX присылает bot_started с payload=<token>)
   или, если событие не пришло, отправляет боту код сообщением.
3. Бот присылает кнопку «Подтвердить вход» с предупреждением; нажатие
   переводит запрос в confirmed с MAX-идентификатором нажавшего.
4. Браузер, опрашивая /auth/max/status, получает сессию.

Кнопка подтверждения нужна против подмены: иначе злоумышленник мог бы
прислать родителю свою ссылку входа и, когда тот нажмёт «Старт», войти
в кабинет от его имени.

Запасной код нужен потому, что по документации MAX bot_started приходит
при первом старте бота, и не проверено, приходит ли он снова, когда
родитель уже писал боту раньше.

API MAX вызывается напрямую через httpx (без maxapi): нужны всего три
метода, а отдельный HTTP-сервер библиотеки занимал бы лишнюю память.
"""

import logging
import time
from collections import defaultdict, deque
from datetime import timedelta

import httpx
from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import (
    BASE_URL,
    COOKIE_SECURE,
    MAX_API_URL,
    MAX_BOT_TOKEN,
    MAX_BOT_USERNAME,
    MAX_WEBHOOK_SECRET,
)
from ..db import get_db
from ..models import LoginRequest, aware, utcnow
from ..security import csrf_ok, random_token, sha256, short_digits
from ..templating import templates
from .service import AlreadyLinked, link_to, login_via
from .sessions import SESSION_COOKIE, optional_account, start_session

logger = logging.getLogger(__name__)
router = APIRouter()

BROWSER_COOKIE = "lr"
REQUEST_TTL = timedelta(minutes=10)
WEBHOOK_PATH = "/max/webhook"


# --- клиент API MAX --------------------------------------------------------


async def _api(method: str, path: str, params: dict | None = None, json: dict | None = None):
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.request(
            method,
            f"{MAX_API_URL}{path}",
            params=params,
            json=json,
            headers={"Authorization": MAX_BOT_TOKEN},
        )
        resp.raise_for_status()
        return resp.json()


async def send_message(user_id: int, text: str, buttons: list[list[dict]] | None = None) -> None:
    body: dict = {"text": text}
    if buttons:
        body["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]
    try:
        await _api("POST", "/messages", params={"user_id": user_id}, json=body)
    except httpx.HTTPError:
        logger.exception("MAX: не удалось отправить сообщение")


async def answer_callback(callback_id: str, notification: str) -> None:
    try:
        await _api(
            "POST", "/answers", params={"callback_id": callback_id}, json={"notification": notification}
        )
    except httpx.HTTPError:
        logger.exception("MAX: не удалось ответить на нажатие кнопки")


async def subscribe_webhook() -> None:
    try:
        await _api(
            "POST",
            "/subscriptions",
            json={
                "url": f"{BASE_URL}{WEBHOOK_PATH}",
                "update_types": ["bot_started", "message_created", "message_callback"],
                "secret": MAX_WEBHOOK_SECRET,
            },
        )
        logger.info("MAX: вебхук подписан")
    except httpx.HTTPError:
        logger.exception(
            "MAX: не удалось подписать вебхук — проверьте MAX_BOT_TOKEN и что "
            "домен открывается по HTTPS"
        )


# --- защита от перебора кодов ------------------------------------------------

_CODE_ATTEMPTS: dict[int, deque] = defaultdict(deque)
CODE_ATTEMPTS_LIMIT = 5
CODE_ATTEMPTS_WINDOW = 600


def _code_attempt_allowed(user_id: int) -> bool:
    now = time.monotonic()
    attempts = _CODE_ATTEMPTS[user_id]
    while attempts and now - attempts[0] > CODE_ATTEMPTS_WINDOW:
        attempts.popleft()
    if len(attempts) >= CODE_ATTEMPTS_LIMIT:
        return False
    attempts.append(now)
    return True


# --- браузерная часть ------------------------------------------------------


async def _unique_code(db: AsyncSession) -> str:
    while True:
        code = short_digits()
        busy = (
            await db.execute(
                select(LoginRequest.id).where(
                    LoginRequest.code == code,
                    LoginRequest.status == "pending",
                    LoginRequest.expires_at > utcnow(),
                )
            )
        ).first()
        if busy is None:
            return code


@router.post("/auth/max/start")
async def max_start(
    request: Request,
    purpose: str = Form("login"),
    csrf: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    account_id = None
    if purpose == "link":
        current = await optional_account(request, db)
        if current is None or not csrf_ok(request.cookies.get(SESSION_COOKIE, ""), csrf):
            raise HTTPException(403)
        account_id = current.id
    else:
        purpose = "login"

    browser_key = random_token()
    req = LoginRequest(
        token=random_token(),
        code=await _unique_code(db),
        browser_hash=sha256(browser_key),
        purpose=purpose,
        account_id=account_id,
        expires_at=utcnow() + REQUEST_TTL,
    )
    db.add(req)
    await db.commit()
    response = RedirectResponse(f"/auth/max/wait/{req.id}", status_code=303)
    response.set_cookie(
        BROWSER_COOKIE,
        browser_key,
        max_age=int(REQUEST_TTL.total_seconds()),
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
    )
    return response


async def _own_request(request: Request, db: AsyncSession, request_id: int) -> LoginRequest:
    req = await db.get(LoginRequest, request_id)
    key = request.cookies.get(BROWSER_COOKIE, "")
    if req is None or not key or sha256(key) != req.browser_hash:
        raise HTTPException(404)
    return req


@router.get("/auth/max/wait/{request_id}")
async def max_wait(request: Request, request_id: int, db: AsyncSession = Depends(get_db)):
    req = await _own_request(request, db, request_id)
    return templates.TemplateResponse(
        request,
        "max_wait.html",
        {
            "req": req,
            "deep_link": f"https://max.ru/{MAX_BOT_USERNAME}?start={req.token}",
            "bot_link": f"https://max.ru/{MAX_BOT_USERNAME}",
        },
    )


@router.get("/auth/max/status/{request_id}")
async def max_status(request: Request, request_id: int, db: AsyncSession = Depends(get_db)):
    req = await _own_request(request, db, request_id)
    if req.status == "pending":
        if aware(req.expires_at) < utcnow():
            return JSONResponse({"status": "expired"})
        return JSONResponse({"status": "pending"})
    if req.status != "confirmed":
        return JSONResponse({"status": "expired"})

    req.status = "used"
    if req.purpose == "link":
        try:
            await link_to(db, req.account_id, "max", req.external_id, req.display_name)
        except AlreadyLinked:
            await db.commit()
            return JSONResponse({"status": "ok", "redirect": "/cabinet?error=linked_elsewhere"})
        response = JSONResponse({"status": "ok", "redirect": "/cabinet?linked=max"})
    else:
        account_id = await login_via(db, "max", req.external_id, req.display_name)
        response = JSONResponse({"status": "ok", "redirect": "/cabinet"})
        await start_session(db, response, account_id)
    await db.commit()
    response.delete_cookie(BROWSER_COOKIE)
    return response


# --- вебхук бота -------------------------------------------------------------

HELLO = (
    "Здравствуйте! Этот бот нужен для входа в личный кабинет участника "
    f"городской олимпиады по математике: {BASE_URL}\n\n"
    "Нажмите на сайте «Войти через MAX» — бот попросит подтвердить вход."
)


async def _active_request(db: AsyncSession, **where) -> LoginRequest | None:
    query = select(LoginRequest).where(
        LoginRequest.status == "pending", LoginRequest.expires_at > utcnow()
    )
    for field, value in where.items():
        query = query.where(getattr(LoginRequest, field) == value)
    return (await db.execute(query)).scalars().first()


async def _ask_confirmation(user_id: int, req: LoginRequest) -> None:
    action = "привязку MAX к личному кабинету" if req.purpose == "link" else "вход в личный кабинет"
    await send_message(
        user_id,
        f"Подтвердите {action} олимпиады на сайте {BASE_URL}\n\n"
        "Если вы не начинали вход сами на этом сайте, ничего не нажимайте.",
        buttons=[[{"type": "callback", "text": "Подтвердить", "payload": f"ok:{req.token}"}]],
    )


def _display_name(user: dict) -> str | None:
    name = user.get("name") or " ".join(
        p for p in (user.get("first_name"), user.get("last_name")) if p
    )
    return name or None


@router.post(WEBHOOK_PATH)
async def max_webhook(
    request: Request,
    x_max_bot_api_secret: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    if not MAX_WEBHOOK_SECRET or x_max_bot_api_secret != MAX_WEBHOOK_SECRET:
        raise HTTPException(403)
    update = await request.json()
    kind = update.get("update_type")

    if kind == "bot_started":
        user_id = update["user"]["user_id"]
        payload = update.get("payload")
        req = await _active_request(db, token=payload) if payload else None
        if req is not None:
            await _ask_confirmation(user_id, req)
        else:
            await send_message(user_id, HELLO)

    elif kind == "message_created":
        message = update.get("message", {})
        user_id = (message.get("sender") or {}).get("user_id")
        text = ((message.get("body") or {}).get("text") or "").strip()
        if user_id is None:
            return {"ok": True}
        digits = text.replace(" ", "")
        if digits.isdigit() and len(digits) == 6:
            if not _code_attempt_allowed(user_id):
                await send_message(user_id, "Слишком много попыток. Подождите 10 минут.")
                return {"ok": True}
            req = await _active_request(db, code=digits)
            if req is not None:
                await _ask_confirmation(user_id, req)
            else:
                await send_message(
                    user_id, "Код не найден или устарел. Начните вход на сайте заново."
                )
        else:
            await send_message(user_id, HELLO)

    elif kind == "message_callback":
        callback = update.get("callback", {})
        payload = callback.get("payload") or ""
        user = callback.get("user") or {}
        if payload.startswith("ok:"):
            req = await _active_request(db, token=payload[3:])
            if req is None:
                await answer_callback(
                    callback["callback_id"], "Запрос устарел. Начните вход на сайте заново."
                )
            else:
                req.status = "confirmed"
                req.external_id = str(user["user_id"])
                req.display_name = _display_name(user)
                await db.commit()
                await answer_callback(callback["callback_id"], "Готово! Вернитесь на сайт.")

    return {"ok": True}
