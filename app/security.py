import hashlib
import hmac
import secrets

from .config import SECRET_KEY

# Без похожих символов (0/O, 1/I/L), чтобы код можно было продиктовать.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def random_token() -> str:
    return secrets.token_urlsafe(32)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def participant_code() -> str:
    """8 символов из 31 — около 850 млрд вариантов, перебором не угадать."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))


def cipher() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))


def short_digits(n: int = 6) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(n))


def csrf_token(session_token: str) -> str:
    return hmac.new(SECRET_KEY.encode(), f"csrf:{session_token}".encode(), "sha256").hexdigest()


def csrf_ok(session_token: str, value: str) -> bool:
    return hmac.compare_digest(csrf_token(session_token), value or "")
