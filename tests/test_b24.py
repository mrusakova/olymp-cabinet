import asyncio

import pytest
from sqlalchemy import select

from app.b24 import outbox
from app.b24.client import B24Error
from app.b24.fields import DEAL_FIELDS, F, userfield_payload
from app.db import SessionLocal
from app.models import Account, B24Outbox, Participant

from .conftest import add_child, csrf_of, fill_profile, login_telegram


def _query(stmt):
    async def run():
        async with SessionLocal() as db:
            return (await db.execute(stmt)).scalars().all()

    return asyncio.run(run())


class FakeB24:
    def __init__(self):
        self.calls = []
        self.contacts = {}
        self.deals = {}
        self.phones = {}  # телефон -> id уже существующего контакта
        self.fail_methods = set()

    async def __call__(self, method, params=None):
        params = params or {}
        self.calls.append(method)
        if method in self.fail_methods:
            raise B24Error(f"{method}: QUERY_LIMIT_EXCEEDED")
        if method == "crm.deal.fields":
            return {
                "UF_CRM_1684757265": {
                    "items": [{"ID": "101", "VALUE": "2 класс"}, {"ID": "102", "VALUE": "3 класс"}]
                }
            }
        if method == "crm.duplicate.findbycomm":
            found = [self.phones[v] for v in params["values"] if v in self.phones]
            return {"CONTACT": found} if found else []
        if method == "crm.contact.add":
            new_id = 500 + len(self.contacts)
            self.contacts[new_id] = params["fields"]
            return new_id
        if method == "crm.deal.list":
            pid = params["filter"][F["OL_PID"]]
            return [{"ID": str(i)} for i, d in self.deals.items() if d.get(F["OL_PID"]) == pid]
        if method == "crm.deal.add":
            new_id = 900 + len(self.deals)
            self.deals[new_id] = dict(params["fields"])
            return new_id
        if method == "crm.deal.update":
            self.deals[params["id"]].update(params["fields"])
            return True
        raise AssertionError(f"неожиданный вызов {method}")


@pytest.fixture
def b24(monkeypatch):
    fake = FakeB24()
    monkeypatch.setattr(outbox, "call", fake)
    monkeypatch.setattr(outbox, "_grade_items", None)
    return fake


def _register(client):
    login_telegram(client)
    fill_profile(client)
    add_child(client)


def test_contact_and_deal_created(client, b24):
    _register(client)
    asyncio.run(outbox.run_once())

    assert len(b24.contacts) == 1
    contact = next(iter(b24.contacts.values()))
    assert (contact["LAST_NAME"], contact["NAME"], contact["SECOND_NAME"]) == ("Иванова", "Анна", "Петровна")
    assert contact["PHONE"] == [{"VALUE": "+79001234567", "VALUE_TYPE": "MOBILE"}]

    deal_id, deal = next(iter(b24.deals.items()))
    child = _query(select(Participant))[0]
    assert deal["CATEGORY_ID"] == 20 and deal["STAGE_ID"] == "C20:NEW"
    assert deal["CONTACT_ID"] == 500
    assert deal["UF_CRM_1684757265"] == "102"
    assert deal[F["OL_CODE"]] == child.code and deal[F["OL_PID"]] == str(child.id)
    assert deal["TITLE"] == "ГорОл 2026"
    assert deal[F["OL_FIO"]] == "Иванов Пётр"

    assert child.b24_deal_id == deal_id
    assert _query(select(Account))[0].b24_contact_id == 500
    assert all(r.done_at for r in _query(select(B24Outbox)))


def test_existing_contact_is_reused_not_changed(client, b24):
    b24.phones["+79001234567"] = 77
    _register(client)
    asyncio.run(outbox.run_once())
    assert b24.contacts == {}
    assert next(iter(b24.deals.values()))["CONTACT_ID"] == 77


def test_edit_updates_same_deal_without_stage(client, b24):
    _register(client)
    asyncio.run(outbox.run_once())
    child = _query(select(Participant))[0]
    client.post(
        f"/children/{child.id}/edit",
        data={
            "csrf": csrf_of(client),
            "last_name": "Иванов",
            "first_name": "Пётр",
            "middle_name": "",
            "grade": "3",
            "city": "Казань",
            "school": "Гимназия № 7",
        },
    )
    asyncio.run(outbox.run_once())
    assert len(b24.deals) == 1
    deal = next(iter(b24.deals.values()))
    assert deal[F["OL_SCHOOL"]] == "Гимназия № 7"
    assert b24.calls.count("crm.deal.update") == 1


def test_failure_is_retried_later(client, b24):
    _register(client)
    b24.fail_methods.add("crm.deal.add")
    asyncio.run(outbox.run_once())
    rows = {r.action: r for r in _query(select(B24Outbox))}
    assert rows["upsert_contact"].done_at is not None
    assert rows["upsert_deal"].done_at is None
    assert rows["upsert_deal"].attempts == 1
    assert "QUERY_LIMIT_EXCEEDED" in rows["upsert_deal"].last_error

    # Пауза ещё не прошла — повторной попытки нет.
    asyncio.run(outbox.run_once())
    assert _query(select(B24Outbox).where(B24Outbox.action == "upsert_deal"))[0].attempts == 1


def test_lost_deal_id_does_not_duplicate(client, b24):
    _register(client)
    asyncio.run(outbox.run_once())

    async def forget():
        async with SessionLocal() as db:
            child = (await db.execute(select(Participant))).scalar_one()
            child.b24_deal_id = None
            db.add(B24Outbox(entity="participant", entity_id=child.id, action="upsert_deal"))
            await db.commit()

    asyncio.run(forget())
    asyncio.run(outbox.run_once())
    assert len(b24.deals) == 1


def test_field_codes_fit_bitrix_limit():
    for spec in DEAL_FIELDS:
        assert len(spec.code) <= 20, spec.code
        assert spec.label.startswith("Олимпиада: ")
    result = next(s for s in DEAL_FIELDS if s.name == "OL_RESULT")
    assert [i["VALUE"] for i in userfield_payload(result)["LIST"]] == ["Победитель", "Призёр", "Участник"]


def test_resync_fills_existing_deals(client, b24):
    from app.cli.b24 import resync

    _register(client)
    asyncio.run(outbox.run_once())
    asyncio.run(resync())
    asyncio.run(outbox.run_once())
    assert len(b24.deals) == 1
    assert b24.calls.count("crm.deal.update") == 1
