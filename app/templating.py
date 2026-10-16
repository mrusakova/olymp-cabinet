from pathlib import Path

from fastapi.templating import Jinja2Templates

from .config import BASE_URL, CONSENT_VERSION, TELEGRAM_BOT_USERNAME

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals.update(
    telegram_bot_username=TELEGRAM_BOT_USERNAME,
    consent_version=CONSENT_VERSION,
    base_url=BASE_URL,
)
