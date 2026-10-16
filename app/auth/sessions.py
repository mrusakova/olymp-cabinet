from datetime import timedelta

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import COOKIE_SECURE, SESSION_DAYS
from ..db import get_db
from ..models import Account, Session, aware, utcnow
from ..security import random_token, sha256

SESSION_COOKIE = "sid"


async def start_session(db: AsyncSession, response: Response, account_id: int) -> None:
    token = random_token()
    db.add(
        Session(
            token_hash=sha256(token),
            account_id=account_id,
            expires_at=utcnow() + timedelta(days=SESSION_DAYS),
        )
    )
    await db.commit()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
    )


async def end_session(db: AsyncSession, request: Request, response: Response) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await db.execute(delete(Session).where(Session.token_hash == sha256(token)))
        await db.commit()
    response.delete_cookie(SESSION_COOKIE)


async def optional_account(
    request: Request, db: AsyncSession = Depends(get_db)
) -> Account | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = (
        await db.execute(select(Session).where(Session.token_hash == sha256(token)))
    ).scalar_one_or_none()
    if row is None or aware(row.expires_at) < utcnow():
        return None
    return (
        await db.execute(
            select(Account)
            .where(Account.id == row.account_id)
            .options(selectinload(Account.links), selectinload(Account.participants))
        )
    ).scalar_one_or_none()


class LoginRequired(Exception):
    pass


async def require_account(account: Account | None = Depends(optional_account)) -> Account:
    if account is None:
        raise LoginRequired()
    return account


def session_token(request: Request) -> str:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(403)
    return token
