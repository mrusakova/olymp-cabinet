"""Результаты для страницы кабинета: только опубликованные."""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import config
from ..models import AnswerKey, Participant, RoundResult, TaskScore


@dataclass
class TaskRow:
    task_no: int
    answer: str
    points: Decimal
    max_points: Decimal
    correct_answer: str | None  # только если SHOW_CORRECT_ANSWERS

    @property
    def correct(self) -> bool:
        return self.points > 0


@dataclass
class RoundView:
    total: Decimal
    max_total: Decimal
    tasks: list[TaskRow]


def fmt(value: Decimal) -> str:
    return f"{Decimal(value).normalize():f}".replace(".", ",")


async def qual_results(db: AsyncSession, participants: list[Participant]) -> dict[int, RoundView]:
    ids = [p.id for p in participants]
    if not ids:
        return {}
    results = (
        await db.execute(
            select(RoundResult).where(
                RoundResult.participant_id.in_(ids),
                RoundResult.round == "qual",
                RoundResult.published.is_(True),
            )
        )
    ).scalars().all()
    if not results:
        return {}
    published_ids = [r.participant_id for r in results]
    scores = (
        await db.execute(
            select(TaskScore)
            .where(TaskScore.participant_id.in_(published_ids), TaskScore.round == "qual")
            .order_by(TaskScore.task_no)
        )
    ).scalars().all()
    grades = {p.id: p.grade for p in participants}
    keys = (
        await db.execute(
            select(AnswerKey).where(
                AnswerKey.round == "qual", AnswerKey.grade.in_(set(grades.values()))
            )
        )
    ).scalars().all()
    key = {(k.grade, k.task_no): k for k in keys}

    views = {}
    for r in results:
        grade = grades[r.participant_id]
        rows = []
        for s in scores:
            if s.participant_id != r.participant_id:
                continue
            k = key.get((grade, s.task_no))
            rows.append(
                TaskRow(
                    task_no=s.task_no,
                    answer=s.answer or "",
                    points=Decimal(s.points),
                    max_points=Decimal(k.points) if k else Decimal(s.points),
                    correct_answer=k.answer.split("|")[0] if k and config.SHOW_CORRECT_ANSWERS else None,
                )
            )
        views[r.participant_id] = RoundView(
            total=Decimal(r.total), max_total=sum((t.max_points for t in rows), Decimal(0)), tasks=rows
        )
    return views
