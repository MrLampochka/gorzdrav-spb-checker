"""
Отмена записи ответным письмом.

В письме о записи есть кнопка «Отменить запись» - это mailto-ссылка на ящик бота
с темой «Отменить запись cancel:<токен>». Бот при каждой проверке читает ящик по IMAP
(тот же логин и пароль приложения, что и для отправки) и отменяет записи,
токены которых нашёл в темах писем.

Ящик открывается только на чтение: флаги писем не меняются, поэтому неважно,
прочитали вы письмо или нет. Повторная обработка безопасна - отменённая запись
просто пропускается.
"""

import datetime
import email
import imaplib
import logging
import re
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.utils import parseaddr
from urllib.parse import quote

logger = logging.getLogger("mail_cancel")

TOKEN_PATTERN = re.compile(r"cancel:([A-Za-z0-9_-]{8,64})")
# письма старше этого не смотрим
LOOKBACK_DAYS = 3
IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class MailCancelError(Exception):
    """Не удалось прочитать ящик"""


@dataclass
class CancelRequest:
    """Письмо с просьбой отменить запись"""

    token: str
    sender: str


def _imap_date(day: datetime.date) -> str:
    """Дата для IMAP SEARCH: 09-Oct-2026, месяц всегда по-английски"""
    return f"{day.day:02d}-{IMAP_MONTHS[day.month - 1]}-{day.year}"


def _decode(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError):
        return value


def parse_request(raw_headers: bytes, allowed_senders: set[str]) -> list[CancelRequest]:
    """Достаёт токены из темы письма, если оно от разрешённого отправителя"""
    message = email.message_from_bytes(raw_headers)
    sender = parseaddr(_decode(message.get("From", "")))[1].lower()
    if sender not in allowed_senders:
        if TOKEN_PATTERN.search(_decode(message.get("Subject", ""))):
            logger.warning("письмо об отмене от чужого адреса %s, пропускаем", sender)
        return []
    subject = _decode(message.get("Subject", ""))
    return [CancelRequest(token=token, sender=sender) for token in TOKEN_PATTERN.findall(subject)]


class CancelMailbox:
    """Ящик, куда приходят письма с отменой. Он же даёт ссылку для письма о записи"""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        allowed_senders: list[str],
        timeout_secs: int = 30,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.allowed_senders = {item.strip().lower() for item in allowed_senders if item.strip()}
        self.timeout_secs = timeout_secs

    @classmethod
    def from_settings(cls, settings, allowed_senders: list[str]) -> "CancelMailbox":
        return cls(
            host=settings.imap_host,
            port=settings.imap_port,
            user=settings.smtp_user,
            password=settings.smtp_password,
            # письма с отменой вы отправляете со своего адреса, он же может быть ящиком бота
            allowed_senders=[*allowed_senders, settings.smtp_user],
        )

    # ссылка в письме о записи

    def url(self, booking) -> str:
        subject = f"Отменить запись cancel:{booking.token}"
        body = (
            f"Отмените запись на {booking.visit_start.strftime('%d.%m.%Y %H:%M')}, "
            + f"{booking.doctor_name}.\n\nПросто отправьте это письмо, тему не меняйте."
        )
        return f"mailto:{self.user}?subject={quote(subject)}&body={quote(body)}"

    def hint(self, booking) -> str:
        return f"отправьте на {self.user} письмо с темой cancel:{booking.token}"

    # чтение ящика

    def _connect(self) -> imaplib.IMAP4_SSL:
        try:
            imap = imaplib.IMAP4_SSL(self.host, self.port, timeout=self.timeout_secs)
            imap.login(self.user, self.password)
            return imap
        except (imaplib.IMAP4.error, OSError) as error:
            raise MailCancelError(f"не удалось войти в ящик {self.host}: {error}") from error

    def check_login(self) -> None:
        """Проверка, что IMAP доступен с этим паролем"""
        imap = self._connect()
        try:
            imap.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    def _folders(self, imap: imaplib.IMAP4_SSL) -> list[str]:
        """Входящие и отправленные: письмо самому себе иногда остаётся только в отправленных"""
        folders = ["INBOX"]
        status, items = imap.list()
        if status != "OK":
            return folders
        for item in items or []:
            line = item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item)
            if "\\Sent" in line:
                found = re.search(r'"([^"]+)"\s*$', line) or re.search(r"(\S+)\s*$", line)
                if found:
                    folders.append(found.group(1))
        return folders

    def fetch_requests(self, today: datetime.date | None = None) -> list[CancelRequest]:
        """
        Письма с просьбой отменить запись за последние дни
        Raises:
            MailCancelError: ящик недоступен
        """
        today = today or datetime.date.today()
        since = _imap_date(today - datetime.timedelta(days=LOOKBACK_DAYS))
        imap = self._connect()
        requests: list[CancelRequest] = []
        try:
            for folder in self._folders(imap):
                status, _ = imap.select(f'"{folder}"', readonly=True)
                if status != "OK":
                    continue
                status, data = imap.search(None, "SINCE", since, "SUBJECT", '"cancel:"')
                if status != "OK" or not data or not data[0]:
                    continue
                for message_id in data[0].split():
                    status, parts = imap.fetch(
                        message_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])"
                    )
                    if status != "OK":
                        continue
                    for part in parts:
                        if isinstance(part, tuple):
                            requests.extend(parse_request(part[1], self.allowed_senders))
        except (imaplib.IMAP4.error, OSError) as error:
            raise MailCancelError(f"ошибка чтения ящика: {error}") from error
        finally:
            try:
                imap.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
        # одна запись могла попасть и во входящие, и в отправленные
        unique: dict[str, CancelRequest] = {}
        for request in requests:
            unique.setdefault(request.token, request)
        return list(unique.values())
