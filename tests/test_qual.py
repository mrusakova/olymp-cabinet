import asyncio
from datetime import datetime
from decimal import Decimal

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app import config
from app.b24 import outbox
from app.b24.fields import F
from app.cli import qual
from app.db import SessionLocal
from app.models import B24Outbox, Participant, RoundResult, TaskScore
from app.results.scoring import (
    TableError,
    is_correct,
    normalize_code,
    parse_submissions,
    read_key,
    read_table,
)

from .conftest import add_child, fill_profile, login_telegram
from .test_b24 import FakeB24


def _query(stmt):
    async def run():
        async with SessionLocal() as db:
            return (await db.execute(stmt)).scalars().all()

    return asyncio.run(run())


def _xlsx(path, rows):
    wb = Workbook()
    for row in rows:
        wb.active.append(row)
    wb.save(path)
    return path


# --- чистые функции ------------------------------------------------------------


def test_answer_normalization():
    assert is_correct(" 12 ", "12")
    assert is_correct(12.0, "12")
    assert is_correct("2,5", "2.5")
    assert is_correct("Ёлка", "елка")
    assert is_correct("Б) 7", "А) 5|Б) 7")
    assert not is_correct("", "0")
    assert not is_correct("13", "12")
    assert normalize_code(" abcd-efgh ") == "ABCDEFGH"


def test_parse_takes_first_answer_by_time():
    rows = [
        ["ID", "Время создания", "Код участника", "1. Сколько?", "2. А тут?", "Комментарий"],
        [2, "2026-12-06 10:20:00", "AAAA2222", "5", "7", ""],
        [1, "2026-12-06 10:05:00", "aaaa2222", "4", "6", ""],
        [3, "2026-12-06 10:07:00", "BBBB3333", "4", "", ""],
        [4, "2026-12-06 10:08:00", "", "1", "1", ""],
    ]
    parsed = parse_submissions(rows)
    assert parsed.layout.task_cols == {1: 3, 2: 4}
    assert [s.code for s in parsed.submissions] == ["AAAA2222", "BBBB3333"]
    assert parsed.submissions[0].answers == {1: "4", 2: "6"}
    assert parsed.duplicates == {"AAAA2222": 1}
    assert parsed.empty_code_rows == 1


def test_parse_errors_list_headers():
    with pytest.raises(TableError, match="Код участника"):
        parse_submissions([["ID", "1. Вопрос"]])
    with pytest.raises(TableError, match="столбцы задач"):
        parse_submissions([["Код участника", "Вопрос без номера"]])


def test_read_key_and_csv(tmp_path):
    path = tmp_path / "key.csv"
    path.write_text("класс;задача;ответ;баллы\n3;1;4;1\n3;2;6|шесть;2\n4;1;10;\n", encoding="utf-8")
    key = read_key(path)
    assert key[3][2].answer == "6|шесть" and key[3][2].points == Decimal(2)
    assert key[4][1].points == Decimal(1)
    assert read_table(path)[1] == ["3", "1", "4", "1"]

    bad = tmp_path / "bad.csv"
    bad.write_text("класс;задача;ответ;баллы\n3;один;4;1\n", encoding="utf-8")
    with pytest.raises(TableError, match="строка 2"):
        read_key(bad)


# --- полный цикл ---------------------------------------------------------------


@pytest.fixture
def registered(client, monkeypatch):
    monkeypatch.setattr(config, "QUAL_OPENS_AT", None)
    login_telegram(client)
    fill_profile(client)
    add_child(client)
    add_child(client, first_name="Мария", grade="4")
    return {p.first_name: p for p in _query(select(Participant))}


def _key(tmp_path):
    return _xlsx(
        tmp_path / "key.xlsx",
        [["класс", "задача", "ответ", "баллы"], [3, 1, "4", 1], [3, 2, "6", 2], [3, 3, "А", 1]],
    )


def test_full_cycle(client, registered, tmp_path, capsys):
    petr, maria = registered["Пётр"], registered["Мария"]
    asyncio.run(qual.cmd_key(_key(tmp_path), apply=True))

    export = _xlsx(
        tmp_path / "grade3.xlsx",
        [
            ["Время создания", "Код участника", "1. Первая", "2. Вторая", "3. Третья"],
            [datetime(2026, 12, 6, 10, 5), petr.code.lower(), 4, "7", "а"],
            [datetime(2026, 12, 6, 10, 6), "ZZZZ9999", 4, 6, "А"],
            [datetime(2026, 12, 6, 10, 7), maria.code, 1, 1, ""],
        ],
    )

    # Без --apply ничего не пишется, но отчёт есть.
    asyncio.run(qual.cmd_import(export, grade=3, apply=False))
    out = capsys.readouterr().out
    assert "Неизвестные коды: 1 — ZZZZ9999" in out
    assert f"{maria.code} (4 кл)" in out
    assert (tmp_path / "grade3_problems.csv").exists()
    assert _query(select(TaskScore)) == []

    asyncio.run(qual.cmd_import(export, grade=3, apply=True))
    result = _query(select(RoundResult).where(RoundResult.participant_id == petr.id))[0]
    assert result.total == Decimal(2) and not result.published
    points = {s.task_no: s.points for s in _query(select(TaskScore).where(TaskScore.participant_id == petr.id))}
    assert points == {1: Decimal(1), 2: Decimal(0), 3: Decimal(1)}

    # До публикации в кабинете баллов нет.
    assert "Отборочный тур:" not in client.get("/cabinet").text

    asyncio.run(qual.cmd_publish(grade=None, apply=True))
    page = client.get("/cabinet").text
    assert "Отборочный тур: 2 из 4" in page
    assert "неверно" in page and "Верный ответ" not in page

    # Повторная загрузка с исправленным ключом пересчитывает и опубликованное.
    key2 = _xlsx(
        tmp_path / "key2.xlsx",
        [["класс", "задача", "ответ", "баллы"], [3, 1, "4", 1], [3, 2, "6|7", 2], [3, 3, "А", 1]],
    )
    asyncio.run(qual.cmd_key(key2, apply=True))
    asyncio.run(qual.cmd_import(export, grade=3, apply=True))
    assert "Отборочный тур: 4 из 4" in client.get("/cabinet").text


def test_show_correct_answers(client, registered, tmp_path, monkeypatch):
    petr = registered["Пётр"]
    asyncio.run(qual.cmd_key(_key(tmp_path), apply=True))
    export = _xlsx(
        tmp_path / "g3.xlsx",
        [["Код участника", "1. a", "2. b", "3. c"], [petr.code, 4, 6, "Б"]],
    )
    asyncio.run(qual.cmd_import(export, grade=3, apply=True))
    asyncio.run(qual.cmd_publish(grade=3, apply=True))
    monkeypatch.setattr(config, "SHOW_CORRECT_ANSWERS", True)
    page = client.get("/cabinet").text
    assert "Верный ответ" in page and "Отборочный тур: 3 из 4" in page


def test_import_needs_key_and_columns(client, registered, tmp_path):
    export = _xlsx(tmp_path / "g3.xlsx", [["Код участника", "1. a"], ["X", 1]])
    with pytest.raises(TableError, match="Нет ключа"):
        asyncio.run(qual.cmd_import(export, grade=3, apply=True))
    asyncio.run(qual.cmd_key(_key(tmp_path), apply=True))
    with pytest.raises(TableError, match=r"нет столбцов задач \[2, 3\]"):
        asyncio.run(qual.cmd_import(export, grade=3, apply=True))


def test_published_scores_go_to_bitrix(client, registered, tmp_path, monkeypatch):
    fake = FakeB24()
    monkeypatch.setattr(outbox, "call", fake)
    monkeypatch.setattr(outbox, "_grade_items", None)
    petr = registered["Пётр"]
    asyncio.run(outbox.run_once())
    deal = next(d for d in fake.deals.values() if d[F["OL_PID"]] == str(petr.id))
    assert F["OL_QSCORE"] not in deal

    asyncio.run(qual.cmd_key(_key(tmp_path), apply=True))
    export = _xlsx(tmp_path / "g3.xlsx", [["Код участника", "1. a", "2. b", "3. c"], [petr.code, 4, 6, ""]])
    asyncio.run(qual.cmd_import(export, grade=3, apply=True))
    asyncio.run(outbox.run_once())
    assert F["OL_QSCORE"] not in deal  # не опубликовано — в Битрикс не уходит

    asyncio.run(qual.cmd_publish(grade=None, apply=True))
    assert _query(select(B24Outbox).where(B24Outbox.done_at.is_(None)))
    asyncio.run(outbox.run_once())
    assert deal[F["OL_QSCORE"]] == 3.0
    assert deal[F["OL_QTASKS"]] == "1: 1; 2: 2; 3: 0"


def test_export(client, registered, tmp_path):
    petr = registered["Пётр"]
    asyncio.run(qual.cmd_key(_key(tmp_path), apply=True))
    export = _xlsx(tmp_path / "g3.xlsx", [["Код участника", "1. a", "2. b", "3. c"], [petr.code, 4, 6, ""]])
    asyncio.run(qual.cmd_import(export, grade=3, apply=True))
    out = tmp_path / "results.csv"
    asyncio.run(qual.cmd_export(out))
    lines = out.read_text(encoding="utf-8-sig").splitlines()
    assert lines[0].startswith("класс;код;фамилия")
    assert lines[1].split(";")[:4] == ["3", petr.code, "Иванов", "Пётр"]
    assert lines[1].split(";")[7] == "3"
