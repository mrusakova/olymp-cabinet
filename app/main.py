import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .auth import max as max_auth
from .auth import telegram as telegram_auth
from .auth.sessions import LoginRequired
from .b24 import outbox
from .config import BITRIX24_WEBHOOK_URL, MAX_BOT_TOKEN, MAX_WEBHOOK_SECRET
from .db import init_db
from .web import routes as web_routes

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    if MAX_BOT_TOKEN and MAX_WEBHOOK_SECRET:
        await max_auth.subscribe_webhook()
    b24_task = asyncio.create_task(outbox.worker()) if BITRIX24_WEBHOOK_URL else None
    yield
    if b24_task is not None:
        b24_task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(telegram_auth.router)
app.include_router(max_auth.router)
app.include_router(web_routes.router)


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, exc: LoginRequired):
    return RedirectResponse("/", status_code=303)


@app.get("/health")
async def health():
    return {"ok": True}
