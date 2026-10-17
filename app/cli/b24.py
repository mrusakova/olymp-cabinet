"""Битрикс24: создание полей и состояние очереди.

    docker compose exec app python -m app.cli.b24 setup           # только показать
    docker compose exec app python -m app.cli.b24 setup --apply   # создать поля
    docker compose exec app python -m app.cli.b24 status          # очередь
    docker compose exec app python -m app.cli.b24 resync          # переотправить всех
"""

import argparse
import asyncio

from sqlalchemy import func, select

from .. import config
from ..b24.client import B24Error, call
from ..b24.fields import DEAL_FIELDS, userfield_payload
from ..db import SessionLocal, init_db
from ..models import B24Outbox, Participant


async def setup(apply: bool) -> None:
    try:
        category = await call("crm.category.get", {"entityTypeId": 2, "id": config.B24_CATEGORY_ID})
        name = ((category or {}).get("category") or {}).get("name") or "НЕ НАЙДЕНА"
    except B24Error as exc:
        name = f"не удалось проверить ({exc})"
    print(f"Воронка {config.B24_CATEGORY_ID}: {name}")

    fields = await call("crm.deal.fields") or {}
    grade = fields.get(config.B24_GRADE_FIELD)
    if grade:
        values = ", ".join(str(i.get("VALUE")) for i in grade.get("items") or [])
        print(f"Поле класса {config.B24_GRADE_FIELD}: {values}")
    else:
        print(f"Поле класса {config.B24_GRADE_FIELD}: НЕ НАЙДЕНО")

    missing = [spec for spec in DEAL_FIELDS if spec.code not in fields]
    for spec in DEAL_FIELDS:
        mark = "создать" if spec in missing else "есть"
        print(f"  [{mark:7}] {spec.code:20} {spec.label} ({spec.type})")

    if not missing:
        print("Все поля уже созданы.")
        return
    if not apply:
        print(f"\nБудет создано полей: {len(missing)}. Чтобы создать, добавьте --apply")
        return
    for spec in missing:
        field_id = await call("crm.deal.userfield.add", {"fields": userfield_payload(spec)})
        print(f"Создано {spec.code} (id {field_id})")


async def status() -> None:
    await init_db()
    async with SessionLocal() as db:
        pending = await db.scalar(
            select(func.count()).select_from(B24Outbox).where(B24Outbox.done_at.is_(None))
        )
        done = await db.scalar(
            select(func.count()).select_from(B24Outbox).where(B24Outbox.done_at.is_not(None))
        )
        failing = (
            await db.execute(
                select(B24Outbox)
                .where(B24Outbox.done_at.is_(None), B24Outbox.attempts > 0)
                .order_by(B24Outbox.id)
                .limit(20)
            )
        ).scalars().all()
    print(f"Вебхук: {'задан' if config.BITRIX24_WEBHOOK_URL else 'НЕ ЗАДАН'}")
    print(f"Отправлено: {done}, ждут отправки: {pending}, из них с ошибками: {len(failing)}")
    for row in failing:
        print(f"  #{row.id} {row.entity} {row.entity_id} {row.action}: попыток {row.attempts}, {row.last_error}")


async def resync() -> None:
    """Ставит в очередь обновление сделок всех участников — например, после
    создания новых полей, чтобы заполнить их и в уже созданных сделках."""

    await init_db()
    async with SessionLocal() as db:
        ids = (await db.execute(select(Participant.id))).scalars().all()
        for pid in ids:
            db.add(B24Outbox(entity="participant", entity_id=pid, action="upsert_deal"))
        await db.commit()
    print(f"В очередь поставлено сделок: {len(ids)}. Отправка займёт около {len(ids) * 2 // 60 + 1} мин.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli.b24")
    sub = parser.add_subparsers(dest="command", required=True)
    setup_parser = sub.add_parser("setup", help="создать поля сделок")
    setup_parser.add_argument("--apply", action="store_true")
    sub.add_parser("status", help="состояние очереди")
    sub.add_parser("resync", help="переотправить сделки всех участников")
    args = parser.parse_args()
    if args.command == "setup":
        if not config.BITRIX24_WEBHOOK_URL:
            raise SystemExit("Сначала впишите BITRIX24_WEBHOOK_URL в .env и перезапустите контейнер.")
        asyncio.run(setup(args.apply))
    elif args.command == "resync":
        asyncio.run(resync())
    else:
        asyncio.run(status())


if __name__ == "__main__":
    main()
