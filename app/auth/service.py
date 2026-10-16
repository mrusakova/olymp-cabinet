from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Account, MessengerLink


class AlreadyLinked(Exception):
    """Этот аккаунт мессенджера уже привязан к другому родителю."""


async def find_link(db: AsyncSession, provider: str, external_id: str) -> MessengerLink | None:
    return (
        await db.execute(
            select(MessengerLink).where(
                MessengerLink.provider == provider, MessengerLink.external_id == external_id
            )
        )
    ).scalar_one_or_none()


async def login_via(
    db: AsyncSession, provider: str, external_id: str, display_name: str | None
) -> int:
    """Возвращает id аккаунта; при первом входе создаёт пустой аккаунт,
    который родитель затем заполняет анкетой на сайте."""

    link = await find_link(db, provider, external_id)
    if link is not None:
        return link.account_id
    account = Account()
    db.add(account)
    await db.flush()
    db.add(
        MessengerLink(
            account_id=account.id,
            provider=provider,
            external_id=external_id,
            display_name=display_name,
        )
    )
    await db.commit()
    return account.id


async def link_to(
    db: AsyncSession, account_id: int, provider: str, external_id: str, display_name: str | None
) -> None:
    link = await find_link(db, provider, external_id)
    if link is not None:
        if link.account_id != account_id:
            raise AlreadyLinked()
        return
    db.add(
        MessengerLink(
            account_id=account_id,
            provider=provider,
            external_id=external_id,
            display_name=display_name,
        )
    )
    await db.commit()
