import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    return None


def clean(raw: str | None, limit: int) -> str:
    return " ".join((raw or "").split())[:limit]


def valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match(value)) and len(value) <= 200
