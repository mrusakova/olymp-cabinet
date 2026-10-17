"""Схема базы кабинета на все три этапа олимпиады.

Родитель = Account (входит через Telegram/MAX), у него один или несколько
детей = Participant. В Битрикс24 это контакт и сделки в воронке 20.
"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(dt: datetime) -> datetime:
    """SQLite (в тестах) возвращает время без зоны — считаем его UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(20), index=True)
    email: Mapped[str | None] = mapped_column(String(200))
    # Пока согласие не дано, аккаунт считается незаполненным: в кабинете
    # доступна только анкета.
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_version: Mapped[str | None] = mapped_column(String(40))
    consent_ip: Mapped[str | None] = mapped_column(String(45))
    b24_contact_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    links: Mapped[list["MessengerLink"]] = relationship(back_populates="account")
    participants: Mapped[list["Participant"]] = relationship(
        back_populates="account", order_by="Participant.id"
    )


class MessengerLink(Base):
    """Аккаунт мессенджера, через который родитель входит.

    Один аккаунт Telegram/MAX привязан ровно к одному родителю; у родителя
    может быть и Telegram, и MAX.
    """

    __tablename__ = "messenger_links"
    __table_args__ = (UniqueConstraint("provider", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    provider: Mapped[str] = mapped_column(String(16))  # telegram | max
    external_id: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str | None] = mapped_column(String(200))
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[Account] = relationship(back_populates="links")


class LoginRequest(Base):
    """Попытка входа или привязки через бота MAX.

    Браузер создаёт запрос и показывает ссылку max.ru/<бот>?start=<token>,
    а на случай, если MAX не пришлёт событие старта, ещё и короткий код,
    который можно отправить боту сообщением. Бот подтверждает запрос,
    браузер (опрашивая статус) получает сессию.
    """

    __tablename__ = "login_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True)
    code: Mapped[str] = mapped_column(String(8), index=True)
    # Хеш случайного значения из cookie браузера, создавшего запрос: только
    # этот браузер может забрать сессию по подтверждённому запросу.
    browser_hash: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(8))  # login | link
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))  # для link
    status: Mapped[str] = mapped_column(String(12), default="pending")
    # pending -> confirmed (бот подтвердил) -> used (браузер забрал сессию)
    external_id: Mapped[str | None] = mapped_column(String(64))
    display_name: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Participant(Base):
    __tablename__ = "participants"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    # Код для Яндекс.Формы: подставляется в ссылку на тест, по нему
    # ответы связываются с участником.
    code: Mapped[str] = mapped_column(String(12), unique=True)
    last_name: Mapped[str] = mapped_column(String(100))
    first_name: Mapped[str] = mapped_column(String(100))
    middle_name: Mapped[str | None] = mapped_column(String(100))
    grade: Mapped[int] = mapped_column(Integer)
    city: Mapped[str] = mapped_column(String(100))
    school: Mapped[str] = mapped_column(String(200))
    # registered | finalist | not_finalist | withdrawn
    stage: Mapped[str] = mapped_column(String(16), default="registered")
    b24_deal_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    account: Mapped[Account] = relationship(back_populates="participants")

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.last_name, self.first_name, self.middle_name) if p)


class TaskScore(Base):
    """Балл за одну задачу одного тура (qual = отборочный, final = финал)."""

    __tablename__ = "task_scores"
    __table_args__ = (UniqueConstraint("participant_id", "round", "task_no"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), index=True)
    round: Mapped[str] = mapped_column(String(8))
    task_no: Mapped[int] = mapped_column(Integer)
    answer: Mapped[str | None] = mapped_column(String(200))
    points: Mapped[Decimal] = mapped_column(Numeric(6, 2))


class AnswerKey(Base):
    """Ключ: верный ответ и балл за задачу тура для класса.

    Верных вариантов может быть несколько, через «|».
    """

    __tablename__ = "answer_keys"
    __table_args__ = (UniqueConstraint("round", "grade", "task_no"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    round: Mapped[str] = mapped_column(String(8))
    grade: Mapped[int] = mapped_column(Integer)
    task_no: Mapped[int] = mapped_column(Integer)
    answer: Mapped[str] = mapped_column(String(200))
    points: Mapped[Decimal] = mapped_column(Numeric(6, 2))


class RoundResult(Base):
    __tablename__ = "round_results"
    __table_args__ = (UniqueConstraint("participant_id", "round"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), index=True)
    round: Mapped[str] = mapped_column(String(8))
    total: Mapped[Decimal] = mapped_column(Numeric(7, 2))
    scan_url: Mapped[str | None] = mapped_column(String(500))  # финал
    # Баллы загружены, но участнику не видны, пока организатор не опубликует.
    published: Mapped[bool] = mapped_column(Boolean, default=False)


class Venue(Base):
    """Точка проведения финала."""

    __tablename__ = "venues"

    id: Mapped[int] = mapped_column(primary_key=True)
    city: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    address: Mapped[str] = mapped_column(String(300))


class FinalInfo(Base):
    """Этап 2: данные финалиста."""

    __tablename__ = "final_info"

    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), primary_key=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    teacher_name: Mapped[str | None] = mapped_column(String(200))
    venue_id: Mapped[int | None] = mapped_column(ForeignKey("venues.id"))
    # Длинный случайный шифр для сканов, без порядковых номеров.
    cipher: Mapped[str | None] = mapped_column(String(16), unique=True)


class Snils(Base):
    """СНИЛС хранится отдельно и собирается отдельной формой со своим
    согласием; в общие выгрузки не попадает."""

    __tablename__ = "snils"

    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), primary_key=True)
    snils: Mapped[str] = mapped_column(String(14))
    consent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consent_version: Mapped[str] = mapped_column(String(40))


class Appeal(Base):
    """Апелляция: один текст на участника, без переписки."""

    __tablename__ = "appeals"

    id: Mapped[int] = mapped_column(primary_key=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), unique=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decision: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    score_before: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))
    score_after: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))


class Outcome(Base):
    """Итог олимпиады для участника."""

    __tablename__ = "outcomes"

    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), primary_key=True)
    final_score: Mapped[Decimal | None] = mapped_column(Numeric(7, 2))
    status: Mapped[str | None] = mapped_column(String(16))  # winner | prize | participant
    diploma_url: Mapped[str | None] = mapped_column(String(500))
    published: Mapped[bool] = mapped_column(Boolean, default=False)


class B24Outbox(Base):
    """Очередь записи в Битрикс24: кабинет пишет сюда, фоновая задача
    отправляет с ограничением частоты и повторами при ошибках."""

    __tablename__ = "b24_outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity: Mapped[str] = mapped_column(String(16))  # account | participant
    entity_id: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_try_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
