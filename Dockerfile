FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tests ./tests

RUN useradd --system --no-create-home cabinet
USER cabinet

# Один процесс: сервер общий с ботом, памяти мало. Нагрузка кабинета —
# в основном вход и кнопка «Начать тест», асинхронного процесса хватает.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-server-header"]
