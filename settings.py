"""Настройки скрипта: читаются из .env рядом со скриптом и из окружения"""

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

TRUE_VALUES = ("1", "true", "yes", "on", "да")


def load_env(path: Path = ENV_FILE) -> None:
    """
    Простейший разбор .env без сторонних библиотек.
    Переменные, уже заданные в окружении, не перезаписываются.
    """
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if name and name not in os.environ:
            os.environ[name] = _clean_value(value)


def _clean_value(value: str) -> str:
    """
    Убирает кавычки и комментарий в конце строки:
        PASSWORD=secret  # комментарий
        PASSWORD="secret # это часть пароля"
    """
    value = value.strip()
    quote = value[:1]
    if quote in ('"', "'"):
        closing = value.find(quote, 1)
        return value[1:closing] if closing > 0 else value[1:]
    # комментарий отделяется пробелом, чтобы не резать пароли с решёткой
    for separator in (" #", "\t#"):
        value = value.split(separator, 1)[0]
    return value.strip()


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _list_env(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


@dataclass
class Settings:
    """Настройки почты и работы скрипта"""

    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = ""
    email_to: list[str] = field(default_factory=list)
    smtp_ssl: bool = True
    watches_file: Path = BASE_DIR / "watches.json"
    state_file: Path = BASE_DIR / "state.json"
    interval_secs: int = 300
    cooldown_hours: float = 6.0
    log_level: str = "INFO"

    @classmethod
    def load(cls, env_file: Path = ENV_FILE) -> "Settings":
        """Собирает настройки из .env и переменных окружения"""
        load_env(env_file)
        smtp_port = _int_env("SMTP_PORT", 465)
        smtp_ssl_raw = os.environ.get("SMTP_SSL", "").strip().lower()
        smtp_user = os.environ.get("SMTP_USER", "")
        return cls(
            smtp_host=os.environ.get("SMTP_HOST", ""),
            smtp_port=smtp_port,
            smtp_user=smtp_user,
            smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            email_from=os.environ.get("EMAIL_FROM", "") or smtp_user,
            email_to=_list_env("EMAIL_TO"),
            # ssl для 465, starttls для 587 - если не задано явно
            smtp_ssl=(
                smtp_ssl_raw in TRUE_VALUES if smtp_ssl_raw else smtp_port == 465
            ),
            watches_file=Path(
                os.environ.get("WATCHES_FILE", str(BASE_DIR / "watches.json"))
            ),
            state_file=Path(os.environ.get("STATE_FILE", str(BASE_DIR / "state.json"))),
            interval_secs=_int_env("CHECK_INTERVAL_SECS", 300),
            cooldown_hours=_float_env("COOLDOWN_HOURS", 6.0),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
