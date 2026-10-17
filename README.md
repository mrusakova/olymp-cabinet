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

- **MAX, повторный «Старт».** По документации событие `bot_started` приходит
  при первом запуске бота. Приходит ли оно, когда родитель уже писал боту, не
  проверено. Поэтому через 10 секунд страница показывает запасной 6-значный
  код, который можно отправить боту сообщением.
- **Яндекс.Форма.** Нужно проверить, что форма принимает код участника из
  параметра ссылки и сохраняет его в ответе. Формат ссылки задаётся в
  `FORM_URL_GRADE_*`.
- Очередь в Битрикс24 (`b24_outbox`) пока только накапливается — отправка
  будет добавлена отдельно, когда в сделках появятся поля.
