"""Отборочный тур: ключ, импорт ответов, публикация, выгрузка.

Файлы кладутся на сервере в /opt/olymp-cabinet/data/ — в контейнере это
папка data/. Без --apply команды только показывают, что будет сделано.

    docker compose exec app python -m app.cli.qual key data/key.xlsx [--apply]
    docker compose exec app python -m app.cli.qual import data/grade3.xlsx --grade 3 [--apply]
    docker compose exec app python -m app.cli.qual publish [--grade 3] [--apply]
    docker compose exec app python -m app.cli.qual export data/results.csv
"""

import argparse
import asyncio
import csv
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, select

from ..db import SessionLocal, init_db
from ..models import AnswerKey, B24Outbox, Participant, RoundResult, TaskScore
from ..results.scoring import KeyItem, TableError, parse_submissions, read_key, read_table, score

ROUND = "qual"


def _fmt(value: Decimal) -> str:
    return f"{value.normalize():f}"


async def load_key(db, grade: int) -> dict[int, KeyItem]:
    rows = (
        await db.execute(
            select(AnswerKey).where(AnswerKey.round == ROUND, AnswerKey.grade == grade)
        )
    ).scalars().all()
    return {r.task_no: KeyItem(r.answer, Decimal(r.points)) for r in rows}


async def cmd_key(path: Path, apply: bool) -> None:
    key = read_key(path)
    for grade in sorted(key):
        tasks = key[grade]
        total = sum(item.points for item in tasks.values())
        print(f"{grade} класс: задач {len(tasks)}, максимум {_fmt(total)} баллов")
    if not apply:
        print("\nКлюч не сохранён. Чтобы сохранить, добавьте --apply")
        return
    async with SessionLocal() as db:
        for grade, tasks in key.items():
            await db.execute(
                delete(AnswerKey).where(AnswerKey.round == ROUND, AnswerKey.grade == grade)
            )
            for task_no, item in tasks.items():
                db.add(
                    AnswerKey(
                        round=ROUND, grade=grade, task_no=task_no, answer=item.answer, points=item.points
                    )
                )
        await db.commit()
    print("\nКлюч сохранён. Если ответы уже загружались, загрузите их заново — баллы пересчитаются.")


async def cmd_import(path: Path, grade: int, apply: bool) -> None:
    parsed = parse_submissions(read_table(path))
    async with SessionLocal() as db:
        key = await load_key(db, grade)
        if not key:
            raise TableError(f"Нет ключа для {grade} класса — сначала команда key")
        missing = sorted(set(key) - set(parsed.layout.task_cols))
        extra = sorted(set(parsed.layout.task_cols) - set(key))
        if missing:
            raise TableError(f"В выгрузке нет столбцов задач {missing}, а в ключе они есть")

        codes = [s.code for s in parsed.submissions]
        participants = {
            p.code: p
            for p in (
                await db.execute(select(Participant).where(Participant.code.in_(codes)))
            ).scalars().all()
        } if codes else {}

        unknown = [c for c in codes if c not in participants]
        wrong_grade = [p for c, p in participants.items() if p.grade != grade]
        max_total = sum(item.points for item in key.values())

        print(f"Строк с ответами: {len(parsed.submissions)}")
        print(f"Задачи в выгрузке: {sorted(parsed.layout.task_cols)}; максимум {_fmt(max_total)} баллов")
        if extra:
            print(f"Столбцы задач без ключа (не учитываются): {extra}")
        print(f"Найдено участников: {len(participants)}")
        if parsed.duplicates:
            print(f"Повторные ответы (засчитан первый): {len(parsed.duplicates)} участников")
        if parsed.empty_code_rows:
            print(f"Строк без кода: {parsed.empty_code_rows}")
        if unknown:
            print(f"Неизвестные коды: {len(unknown)} — " + ", ".join(unknown[:20]))
        if wrong_grade:
            print(
                f"Записаны в другой класс: {len(wrong_grade)} — "
                + ", ".join(f"{p.code} ({p.grade} кл)" for p in wrong_grade[:20])
            )

        report = path.with_name(path.stem + "_problems.csv")
        if unknown or wrong_grade or parsed.duplicates:
            with report.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f, delimiter=";")
                writer.writerow(["код", "проблема"])
                for c in unknown:
                    writer.writerow([c, "код не найден"])
                for p in wrong_grade:
                    writer.writerow([p.code, f"зарегистрирован в {p.grade} класс"])
                for c, n in parsed.duplicates.items():
                    writer.writerow([c, f"повторных ответов: {n}"])
            print(f"Полный список проблем: {report}")

        if not apply:
            print("\nБаллы не записаны. Чтобы записать, добавьте --apply")
            return

        published = 0
        for sub in parsed.submissions:
            p = participants.get(sub.code)
            if p is None:
                continue
            points = score(sub.answers, key)
            await db.execute(
                delete(TaskScore).where(TaskScore.participant_id == p.id, TaskScore.round == ROUND)
            )
            for task_no, pts in points.items():
                db.add(
                    TaskScore(
                        participant_id=p.id,
                        round=ROUND,
                        task_no=task_no,
                        answer=sub.answers.get(task_no, "")[:200],
                        points=pts,
                    )
                )
            result = (
                await db.execute(
                    select(RoundResult).where(
                        RoundResult.participant_id == p.id, RoundResult.round == ROUND
                    )
                )
            ).scalar_one_or_none()
            if result is None:
                result = RoundResult(participant_id=p.id, round=ROUND, published=False)
                db.add(result)
            result.total = sum(points.values())
            if result.published:
                # Уже опубликованные баллы изменились — обновить и Битрикс.
                db.add(B24Outbox(entity="participant", entity_id=p.id, action="upsert_deal"))
                published += 1
        await db.commit()
    print(f"\nЗаписано: {len(participants)}. Участникам баллы не видны, пока не выполнена команда publish.")
    if published:
        print(f"Из них уже опубликованных (обновятся сразу): {published}")


async def cmd_publish(grade: int | None, apply: bool) -> None:
    async with SessionLocal() as db:
        query = select(RoundResult, Participant).join(
            Participant, Participant.id == RoundResult.participant_id
        ).where(RoundResult.round == ROUND, RoundResult.published.is_(False))
        if grade is not None:
            query = query.where(Participant.grade == grade)
        rows = (await db.execute(query)).all()
        print(f"Будет опубликовано результатов: {len(rows)}")
        if not apply:
            print("Чтобы опубликовать, добавьте --apply")
            return
        for result, participant in rows:
            result.published = True
            db.add(B24Outbox(entity="participant", entity_id=participant.id, action="upsert_deal"))
        await db.commit()
    print("Опубликовано. Баллы видны в кабинете; в Битрикс уйдут через очередь.")


async def cmd_export(path: Path) -> None:
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Participant, RoundResult)
                .join(RoundResult, RoundResult.participant_id == Participant.id)
                .where(RoundResult.round == ROUND)
                .order_by(Participant.grade, RoundResult.total.desc(), Participant.last_name)
            )
        ).all()
        scores = (
            await db.execute(select(TaskScore).where(TaskScore.round == ROUND))
        ).scalars().all()
    by_participant: dict[int, dict[int, Decimal]] = {}
    for s in scores:
        by_participant.setdefault(s.participant_id, {})[s.task_no] = s.points
    tasks = sorted({t for d in by_participant.values() for t in d})

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(
            ["класс", "код", "фамилия", "имя", "отчество", "город", "школа", "сумма", "опубликовано"]
            + [f"з{t}" for t in tasks]
        )
        for p, r in rows:
            per_task = by_participant.get(p.id, {})
            writer.writerow(
                [p.grade, p.code, p.last_name, p.first_name, p.middle_name or "", p.city, p.school,
                 _fmt(Decimal(r.total)).replace(".", ","), "да" if r.published else "нет"]
                + [_fmt(Decimal(per_task[t])).replace(".", ",") if t in per_task else "" for t in tasks]
            )
    print(f"Выгружено строк: {len(rows)} → {path}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli.qual")
    sub = parser.add_subparsers(dest="command", required=True)
    p_key = sub.add_parser("key", help="загрузить ключ")
    p_key.add_argument("file", type=Path)
    p_key.add_argument("--apply", action="store_true")
    p_imp = sub.add_parser("import", help="загрузить ответы из выгрузки Яндекс.Формы")
    p_imp.add_argument("file", type=Path)
    p_imp.add_argument("--grade", type=int, required=True)
    p_imp.add_argument("--apply", action="store_true")
    p_pub = sub.add_parser("publish", help="показать баллы участникам")
    p_pub.add_argument("--grade", type=int)
    p_pub.add_argument("--apply", action="store_true")
    p_exp = sub.add_parser("export", help="выгрузить результаты в CSV")
    p_exp.add_argument("file", type=Path)
    args = parser.parse_args()

    async def run():
        await init_db()
        if args.command == "key":
            await cmd_key(args.file, args.apply)
        elif args.command == "import":
            await cmd_import(args.file, args.grade, args.apply)
        elif args.command == "publish":
            await cmd_publish(args.grade, args.apply)
        else:
            await cmd_export(args.file)

    try:
        asyncio.run(run())
    except (TableError, FileNotFoundError) as exc:
        sys.exit(f"Ошибка: {exc}")


if __name__ == "__main__":
    main()
