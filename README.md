# Личный кабинет участника городской олимпиады по математике (2–6 классы)

Адрес: https://lk.olymp.kazan-math.info

Родитель входит через Telegram (Login Widget бота олимпиады из Botmother)
или MAX (собственный бот входа), заполняет анкету с согласием на обработку
ПДн и добавляет детей. Каждый ребёнок получает код участника, который
подставляется в ссылку на Яндекс.Форму отборочного тура.

Стек: FastAPI + Postgres в Docker, отдельный compose-проект на том же
сервере, где работают боты учеников (TEST и PROD). Лимиты памяти и CPU — в
`docker-compose.yml`.

## Что где

| Путь | Что |
|---|---|
| `app/auth/telegram.py` | вход и привязка через Telegram Login Widget |
| `app/auth/max.py` | вход и привязка через бота MAX (вебхук `/max/webhook`) |
| `app/web/routes.py` | анкета родителя, дети, кабинет, кнопка «Начать тест» |
| `app/models.py` | схема базы на все этапы олимпиады |
| `app/templates/consent.html` | текст согласия на ПДн (**черновик**, утверждает ответственный за ПДн) |
| `deploy/` | конфиг nginx, бэкап базы, cron |

## Первое развёртывание (сервер TEST)

Код лежит в `/opt/olymp-cabinet`.

**1. Ключ доступа к репозиторию.** Репозиторий приватный, поэтому серверу
нужен ключ только для чтения:

```bash
ssh-keygen -t ed25519 -N "" -f /root/.ssh/olymp_cabinet_deploy -C olymp-cabinet-deploy
cat /root/.ssh/olymp_cabinet_deploy.pub
```

Выведенную строку добавить на GitHub: репозиторий → Settings → Deploy keys →
Add deploy key (галочку «Allow write access» НЕ ставить).

```bash
git clone -c core.sshCommand="ssh -i /root/.ssh/olymp_cabinet_deploy" git@github.com:mrusakova/olymp-cabinet.git /opt/olymp-cabinet
cd /opt/olymp-cabinet && git config core.sshCommand "ssh -i /root/.ssh/olymp_cabinet_deploy"
```

**2. Настройки.**

```bash
cd /opt/olymp-cabinet && cp .env.example .env && chmod 600 .env && nano .env
```

Пароли и секреты генерировать командой `openssl rand -hex 32`
(`POSTGRES_PASSWORD` — `openssl rand -hex 24`).

**3. Запуск и проверка.**

```bash
cd /opt/olymp-cabinet && docker compose up -d --build && docker compose exec app pytest -q
```

```bash
curl -s http://127.0.0.1:8200/health
```

**4. nginx и HTTPS** (DNS-запись `lk.olymp` уже должна указывать на сервер):

```bash
cp /opt/olymp-cabinet/deploy/nginx/lk.olymp.kazan-math.info /etc/nginx/sites-available/ && ln -s /etc/nginx/sites-available/lk.olymp.kazan-math.info /etc/nginx/sites-enabled/ && nginx -t && systemctl reload nginx
```

```bash
certbot --nginx -d lk.olymp.kazan-math.info
```

После выпуска сертификата перезапустить кабинет, чтобы бот MAX подписался
на вебхук уже по HTTPS: `docker compose restart app`.

**5. Бэкап.**

```bash
cp /opt/olymp-cabinet/deploy/cron /etc/cron.d/olymp-cabinet && sh /opt/olymp-cabinet/deploy/backup.sh && ls -la /var/backups/olymp-cabinet/
```

## Отборочный тур: ключ, ответы, результаты

Файлы кладутся в `/opt/olymp-cabinet/data/` (например, через WinSCP), в
командах они указываются как `data/имя_файла`. Папку один раз создать:

```bash
mkdir -p /opt/olymp-cabinet/data && chown 10001 /opt/olymp-cabinet/data && chmod 700 /opt/olymp-cabinet/data
```

**Ключ** — таблица (XLSX или CSV) из четырёх столбцов: класс, задача, ответ,
баллы; первая строка — заголовки. Несколько верных вариантов ответа — через
`|`. Пустые баллы = 1. Без `--apply` команда только показывает, что прочитала.

```bash
cd /opt/olymp-cabinet && docker compose exec app python -m app.cli.qual key data/key.xlsx
```

**Ответы** — выгрузка Яндекс.Формы одного класса. Нужен столбец «Код
участника» и столбцы задач, заголовок которых начинается с номера
(«1. …», «Задача 1»). Повторный ответ того же участника не засчитывается —
берётся первый. Без `--apply` — только отчёт: сколько найдено, неизвестные
коды, повторы, участники из другого класса (полный список — в файле
`*_problems.csv` рядом с выгрузкой).

```bash
cd /opt/olymp-cabinet && docker compose exec app python -m app.cli.qual import data/grade3.xlsx --grade 3
```

Загрузка заново заменяет баллы этих участников. Участникам баллы не видны,
пока не выполнена публикация (можно по одному классу, `--grade 3`):

```bash
cd /opt/olymp-cabinet && docker compose exec app python -m app.cli.qual publish --apply
```

После публикации баллы и разбор по задачам видны в кабинете, а сумма и
разбор уходят в сделки Битрикс24 (5000 участников — около часа).
Показывать ли верный ответ — настройка `SHOW_CORRECT_ANSWERS` в `.env`.

Таблица результатов для организаторов (по классам, по убыванию суммы):

```bash
cd /opt/olymp-cabinet && docker compose exec app python -m app.cli.qual export data/results.csv
```

## Битрикс24

Родитель — контакт (если телефон уже есть в CRM, кабинет привязывается к
существующему контакту и не меняет его), каждый ребёнок — сделка в воронке
20. Отправка идёт фоновой очередью не чаще ~2 запросов в секунду; при
ошибке повторяется с растущей паузой. Стадию сделки кабинет ставит только
при создании, дальше её можно двигать вручную.

1. Создать входящий вебхук с правами CRM, вписать адрес в `.env`
   (`BITRIX24_WEBHOOK_URL`), перезапустить: `docker compose up -d`.
2. Посмотреть, какие поля будут созданы (ничего не меняет):

```bash
cd /opt/olymp-cabinet && docker compose exec app python -m app.cli.b24 setup
```

3. Создать поля:

```bash
cd /opt/olymp-cabinet && docker compose exec app python -m app.cli.b24 setup --apply
```

Состояние очереди:

```bash
cd /opt/olymp-cabinet && docker compose exec app python -m app.cli.b24 status
```

## Обновление

```bash
cd /opt/olymp-cabinet && git pull && docker compose up -d --build && docker compose exec app pytest -q
```

Код запекается в образ, поэтому без `--build` изменения не применятся.

## Логи

```bash
cd /opt/olymp-cabinet && docker compose logs --tail=100 app
```

## Известные ограничения и что не проверено

- **MAX, повторный «Старт».** Проверено 17.10.2026: при повторном входе
  событие `bot_started` приходит. Запасной 6-значный код (появляется на
  странице через 10 секунд) оставлен на случай сбоев.
- **Яндекс.Форма.** Нужно проверить, что форма принимает код участника из
  параметра ссылки и сохраняет его в ответе. Формат ссылки задаётся в
  `FORM_URL_GRADE_*`.
