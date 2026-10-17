"""Вызовы REST API Битрикс24 через входящий вебхук.

У вебхука лимит около 2 запросов в секунду, поэтому все вызовы идут через
общую паузу: не чаще одного раза в MIN_INTERVAL секунд на весь процесс.
"""

import asyncio
import time

import httpx

from .. import config

MIN_INTERVAL = 0.6

_lock = asyncio.Lock()
_last_call = 0.0


class B24Error(Exception):
    pass


async def call(method: str, params: dict | None = None):
    """Возвращает поле result ответа; при ошибке — B24Error."""

    global _last_call
    if not config.BITRIX24_WEBHOOK_URL:
        raise B24Error("BITRIX24_WEBHOOK_URL не задан")
    async with _lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"{config.BITRIX24_WEBHOOK_URL}/{method}.json", json=params or {}
                )
        except httpx.HTTPError as exc:
            raise B24Error(f"{method}: {type(exc).__name__}") from exc
        finally:
            _last_call = time.monotonic()

    try:
        data = resp.json()
    except ValueError:
        raise B24Error(f"{method}: HTTP {resp.status_code}") from None
    if "error" in data:
        raise B24Error(f"{method}: {data['error']} {data.get('error_description', '')}".strip())
    if resp.status_code >= 400:
        raise B24Error(f"{method}: HTTP {resp.status_code}")
    return data.get("result")
