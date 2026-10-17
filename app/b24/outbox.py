"""Фоновая отправка очереди b24_outbox в Битрикс24.

Кабинет при сохранении анкеты или ребёнка только добавляет строку в
очередь (быстро, Битрикс не нужен). Эта задача раз в несколько секунд
забирает строки и отправляет ТЕКУЩЕЕ состояние из базы — поэтому
несколько правок подряд схлопываются в одну отправку. При ошибке строка
откладывается с растущей паузой (до часа) и пробуется снова.

Модель в Битриксе: родитель — контакт, каждый ребёнок — сделка в воронке
B24_CATEGORY_ID. Повторная отправка не плодит дубли: контакт ищется по
телефону, сделка — по полю «ID в кабинете».
"""

import asyncio
import logging
import re
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import config
from ..db import SessionLocal
from ..models import Account, B24Outbox, Participant, RoundResult, TaskScore, utcnow
from .client import B24Error, call
from .fields import F

logger = logging.getLogger(__name__)

POLL_SECONDS = 5
BATCH = 20
MAX_DELAY = timedelta(hours=1)
DEAL_TITLE = "ГорОл 2026"

_grade_items: dict[int, str] | None = None


def split_fio(full_name: str) -> dict:
    parts = full_name.split()
    return {
        "LAST_NAME": parts[0] if parts else "",
        "NAME": parts[1] if len(parts) > 1 else "",
        "SECOND_NAME": " ".join(parts[2:]),
    }


async def grade_items() -> dict[int, str]:
    """Класс -> ID варианта в существующем поле-списке «класс»."""

    global _grade_items
    if _grade_items is None:
        fields = await call("crm.deal.fields")
        items = (fields or {}).get(config.B24_GRADE_FIELD, {}).get("items") or []
        mapping = {}
        for item in items:
            digits = re.findall(r"\d+", str(item.get("VALUE", "")))
            if len(digits) == 1:
                mapping[int(digits[0])] = str(item["ID"])
        _grade_items = mapping
    return _grade_items


async def ensure_contact(db: AsyncSession, account: Account) -> int:
    if account.b24_contact_id:
        return account.b24_contact_id

    contact_id = None
    if account.phone:
        found = await call(
            "crm.duplicate.findbycomm",
            {"entity_type": "CONTACT", "type": "PHONE", "values": [account.phone]},
        )
        ids = (found or {}).get("CONTACT") if isinstance(found, dict) else None
        if ids:
            # Родитель уже есть в CRM (например, клиент центра): привязываемся
            # к существующему контакту и не трогаем его данные.
            contact_id = int(ids[0])

    if contact_id is None:
        fields = split_fio(account.full_name or "")
        fields["SOURCE_DESCRIPTION"] = "Кабинет участника олимпиады"
        if account.phone:
            fields["PHONE"] = [{"VALUE": account.phone, "VALUE_TYPE": "MOBILE"}]
        if account.email:
            fields["EMAIL"] = [{"VALUE": account.email, "VALUE_TYPE": "HOME"}]
        contact_id = int(await call("crm.contact.add", {"fields": fields}))

    account.b24_contact_id = contact_id
    await db.commit()
    return contact_id


def _num(value) -> str:
    return f"{value.normalize():f}"


async def qual_fields(db: AsyncSession, participant_id: int) -> dict:
    """Баллы отборочного — только опубликованные."""

    result = (
        await db.execute(
            select(RoundResult).where(
                RoundResult.participant_id == participant_id,
                RoundResult.round == "qual",
                RoundResult.published.is_(True),
            )
        )
    ).scalar_one_or_none()
    if result is None:
        return {}
    scores = (
        await db.execute(
            select(TaskScore)
            .where(TaskScore.participant_id == participant_id, TaskScore.round == "qual")
            .order_by(TaskScore.task_no)
        )
    ).scalars().all()
    return {
        F["OL_QSCORE"]: float(result.total),
        F["OL_QTASKS"]: "; ".join(f"{s.task_no}: {_num(s.points)}" for s in scores),
    }


async def upsert_deal(db: AsyncSession, participant: Participant) -> None:
    account = await db.get(Account, participant.account_id)
    contact_id = await ensure_contact(db, account)

    # ФИО и класс — в полях сделки, в названии только олимпиада.
    fields = {
        "TITLE": DEAL_TITLE,
        "CONTACT_ID": contact_id,
        F["OL_PID"]: str(participant.id),
        F["OL_FIO"]: participant.full_name,
        F["OL_CODE"]: participant.code,
        F["OL_CITY"]: participant.city,
        F["OL_SCHOOL"]: participant.school,
    }
    fields.update(await qual_fields(db, participant.id))
    grade_id = (await grade_items()).get(participant.grade)
    if grade_id:
        fields[config.B24_GRADE_FIELD] = grade_id

    deal_id = participant.b24_deal_id
    if deal_id is None:
        # Защита от дубля, если прошлая попытка создала сделку, но не успела
        # сохранить её ID у нас.
        existing = await call(
            "crm.deal.list",
            {
                "filter": {"CATEGORY_ID": config.B24_CATEGORY_ID, F["OL_PID"]: str(participant.id)},
                "select": ["ID"],
            },
        )
        if existing:
            deal_id = int(existing[0]["ID"])

    if deal_id is None:
        fields["CATEGORY_ID"] = config.B24_CATEGORY_ID
        fields["STAGE_ID"] = f"C{config.B24_CATEGORY_ID}:NEW"
        deal_id = int(await call("crm.deal.add", {"fields": fields}))
    else:
        # Стадию не трогаем: её могут двигать вручную в Битриксе.
        await call("crm.deal.update", {"id": deal_id, "fields": fields})

    participant.b24_deal_id = deal_id
    await db.commit()


async def process(db: AsyncSession, row: B24Outbox) -> None:
    if row.entity == "account" and row.action == "upsert_contact":
        account = await db.get(Account, row.entity_id)
        if account is not None:
            await ensure_contact(db, account)
    elif row.entity == "participant" and row.action == "upsert_deal":
        participant = await db.get(Participant, row.entity_id)
        if participant is not None:
            await upsert_deal(db, participant)
    else:
        raise B24Error(f"неизвестное действие {row.entity}/{row.action}")


async def run_once() -> int:
    """Обрабатывает одну пачку; возвращает число обработанных строк."""

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(B24Outbox)
                .where(B24Outbox.done_at.is_(None), B24Outbox.next_try_at <= utcnow())
                .order_by(B24Outbox.id)
                .limit(BATCH)
            )
        ).scalars().all()
        seen: set[tuple] = set()
        for row in rows:
            key = (row.entity, row.entity_id, row.action)
            if key in seen:
                row.done_at = utcnow()
                await db.commit()
                continue
            seen.add(key)
            try:
                await process(db, row)
            except Exception as exc:
                await db.rollback()
                await db.refresh(row)
                row.attempts += 1
                row.last_error = str(exc)[:1000]
                row.next_try_at = utcnow() + min(timedelta(minutes=2**row.attempts), MAX_DELAY)
                await db.commit()
                logger.warning("Битрикс24: %s/%s %s — %s", *key, exc)
                continue
            # Все такие же строки, накопившиеся к этому моменту, тоже
            # выполнены: отправлено актуальное состояние.
            await db.execute(
                update(B24Outbox)
                .where(
                    B24Outbox.entity == row.entity,
                    B24Outbox.entity_id == row.entity_id,
                    B24Outbox.action == row.action,
                    B24Outbox.done_at.is_(None),
                    B24Outbox.id <= row.id,
                )
                .values(done_at=utcnow(), last_error=None)
            )
            await db.commit()
        return len(rows)


async def worker() -> None:
    logger.info("Битрикс24: очередь запущена")
    while True:
        try:
            if await run_once() < BATCH:
                await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Битрикс24: сбой обработки очереди")
            await asyncio.sleep(30)
