"""Все настройки берутся из переменных окружения (файл .env на сервере).

В репозитории только .env.example с пустыми значениями — токены и пароли
сюда никогда не попадают.
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_dt(name: str) -> datetime | None:
    """Дата-время в формате 2026-12-06 10:00 по московскому времени."""
    raw = _get(name)
    if not raw:
        return None
    return datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


TZ = ZoneInfo(_get("TZ_NAME", "Europe/Moscow"))

DATABASE_URL = _get("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

# Публичный адрес кабинета, без слеша в конце.
BASE_URL = _get("BASE_URL", "https://lk.olymp.kazan-math.info")

# Случайная длинная строка (openssl rand -hex 32): подпись CSRF-токенов.
SECRET_KEY = _get("SECRET_KEY", "dev-only-not-secret")

# false только для локального запуска по http (cookie без флага Secure).
COOKIE_SECURE = _get("COOKIE_SECURE", "true").lower() != "false"

SESSION_DAYS = int(_get("SESSION_DAYS", "45"))

# Telegram: бот олимпиады в Botmother. Используем ТОЛЬКО для проверки
# подписи Login Widget — вебхук этого бота принадлежит Botmother, мы его
# не трогаем.
TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN")
TELEGRAM_BOT_USERNAME = _get("TELEGRAM_BOT_USERNAME")  # без @

# MAX: собственный бот для входа.
MAX_BOT_TOKEN = _get("MAX_BOT_TOKEN")
MAX_BOT_USERNAME = _get("MAX_BOT_USERNAME")  # как в ссылке max.ru/<имя>
MAX_WEBHOOK_SECRET = _get("MAX_WEBHOOK_SECRET")
MAX_API_URL = _get("MAX_API_URL", "https://platform-api2.max.ru")

# Текст согласия на обработку ПДн. При любой правке текста в
# app/templates/consent.html нужно увеличить версию — в базе у каждого
# аккаунта хранится, на какую версию он соглашался.
CONSENT_VERSION = _get("CONSENT_VERSION", "2026-10-draft")

# Окно отборочного тура: кнопка «Начать тест» активна только внутри него.
QUAL_OPENS_AT = _get_dt("QUAL_OPENS_AT")
QUAL_CLOSES_AT = _get_dt("QUAL_CLOSES_AT")

# Ссылки на Яндекс.Формы по классам. {code} заменяется на код участника.
# Пример: FORM_URL_GRADE_2=https://forms.yandex.ru/u/abc123/?code={code}
FORM_URLS = {g: _get(f"FORM_URL_GRADE_{g}") for g in range(2, 7)}

GRADES = [2, 3, 4, 5, 6]

# Битрикс24: входящий вебхук (права: CRM). Пусто — очередь только копится,
# в Битрикс ничего не отправляется.
BITRIX24_WEBHOOK_URL = _get("BITRIX24_WEBHOOK_URL").rstrip("/")
# Воронка «Олимпиада по математике 2026 (2-6 кл)».
B24_CATEGORY_ID = int(_get("B24_CATEGORY_ID", "20"))
# Существующее поле-список «класс» в сделках.
B24_GRADE_FIELD = _get("B24_GRADE_FIELD", "UF_CRM_1684757265")

# Показывать ли в результатах верный ответ (иначе только «верно/неверно»
# и балл). Если показывать — задачи нельзя использовать повторно.
SHOW_CORRECT_ANSWERS = _get("SHOW_CORRECT_ANSWERS", "false").lower() == "true"
