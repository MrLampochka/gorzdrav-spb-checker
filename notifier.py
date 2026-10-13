"""Отправка писем и сообщений в телеграм о свободных талонах и записях"""

import html
import http.client
import json
import logging
import smtplib
import socket
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org"
# длиннее телеграм не принимает, берём с запасом
TELEGRAM_MAX_LENGTH = 4000
# сколько талонов врача перечислять в сообщении
TELEGRAM_MAX_SLOTS = 10
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


class EmailError(Exception):
    """Ошибка настройки или отправки почты"""


class TelegramError(Exception):
    """Ошибка отправки в телеграм"""


class EmailSender:
    """Отправка писем через smtp"""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        sender: str = "",
        use_ssl: bool = True,
        timeout_secs: int = 30,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.sender = sender or user
        self.use_ssl = use_ssl
        self.timeout_secs = timeout_secs

    @classmethod
    def from_settings(cls, settings) -> "EmailSender":
        """
        Собирает отправщика из настроек
        Raises:
            EmailError: если не хватает обязательных настроек
        """
        missing = [
            name
            for name, value in (
                ("SMTP_HOST", settings.smtp_host),
                ("SMTP_USER", settings.smtp_user),
                ("SMTP_PASSWORD", settings.smtp_password),
            )
            if not value
        ]
        if missing:
            raise EmailError("Не заданы настройки: " + ", ".join(missing))
        return cls(
            host=settings.smtp_host,
            port=settings.smtp_port,
            user=settings.smtp_user,
            password=settings.smtp_password,
            sender=settings.email_from,
            use_ssl=settings.smtp_ssl,
        )

    def check_login(self) -> None:
        """
        Проверяет, что smtp пускает с этим логином и паролем. Письмо не шлёт.
        Raises:
            EmailError: если подключиться или залогиниться не удалось
        """
        with self._connect() as server:
            server.login(self.user, self.password)

    def send(
        self,
        to: list[str],
        subject: str,
        text: str,
        html_body: str | None = None,
    ) -> None:
        """
        Отправляет письмо
        Args:
            to: list[str]: получатели
            subject: str: тема письма
            text: str: текст письма
            html_body: str | None: html версия письма
        Raises:
            EmailError: если письмо не удалось отправить
        """
        if not to:
            raise EmailError("Не указан ни один получатель")

        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = ", ".join(to)
        message["Subject"] = subject
        message.set_content(text)
        if html_body:
            message.add_alternative(html_body, subtype="html")

        with self._connect() as server:
            server.login(self.user, self.password)
            server.send_message(message)
        logger.info("письмо отправлено на %s", ", ".join(to))

    def _connect(self):
        """Подключение к smtp: ssl или starttls"""
        context = ssl.create_default_context()
        try:
            if self.use_ssl:
                return smtplib.SMTP_SSL(
                    host=self.host,
                    port=self.port,
                    timeout=self.timeout_secs,
                    context=context,
                )
            server = smtplib.SMTP(
                host=self.host,
                port=self.port,
                timeout=self.timeout_secs,
            )
            server.starttls(context=context)
            return server
        except (smtplib.SMTPException, OSError) as error:
            raise EmailError(f"Не удалось подключиться к почте: {error}") from error


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise TelegramError("прокси закрыл соединение")
        data += chunk
    return data


def socks5_connect(proxy: str, host: str, port: int, timeout: float | None) -> socket.socket:
    """
    Соединение с host:port через socks5 прокси вида socks5://[логин:пароль@]host:port.
    Имя host разрешает сам прокси - сервер его в dns не спрашивает
    Raises:
        TelegramError: прокси задан неверно или не соединил
        OSError: прокси недоступен
    """
    parts = urlsplit(proxy)
    # в тексте ошибок нет самого адреса: в нём может быть пароль
    if parts.scheme not in ("socks5", "socks5h") or not parts.hostname or not parts.port:
        raise TelegramError("TELEGRAM_PROXY должен быть вида socks5://host:port")
    user = unquote(parts.username or "").encode()
    password = unquote(parts.password or "").encode()
    sock = socket.create_connection((parts.hostname, parts.port), timeout=timeout)
    try:
        # приветствие: без авторизации или по логину и паролю
        sock.sendall(b"\x05\x02\x00\x02" if user else b"\x05\x01\x00")
        version, method = _recv_exact(sock, 2)
        if version != 5:
            raise TelegramError("по адресу TELEGRAM_PROXY не socks5 прокси")
        if method == 2:
            sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
            if _recv_exact(sock, 2)[1] != 0:
                raise TelegramError("socks5 прокси не принял логин и пароль")
        elif method != 0:
            raise TelegramError("socks5 прокси требует логин и пароль в TELEGRAM_PROXY")
        name = host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big"))
        reply = _recv_exact(sock, 4)
        if reply[1] != 0:
            raise TelegramError(f"socks5 прокси не соединил с {host}, код {reply[1]}")
        # дальше адрес, к которому привязался прокси - он не нужен, но его надо дочитать
        address_size = {1: 4, 4: 16}.get(reply[3]) or _recv_exact(sock, 1)[0]
        _recv_exact(sock, address_size + 2)
    except BaseException:
        sock.close()
        raise
    return sock


class _SocksHTTPSConnection(http.client.HTTPSConnection):
    """https поверх socks5: tls до самого сервера, сертификат проверяется как обычно"""

    def __init__(self, host: str, proxy: str, timeout=None, **kwargs) -> None:
        self.ssl_context = ssl.create_default_context()
        super().__init__(host, timeout=timeout, context=self.ssl_context)
        self.proxy = proxy

    def connect(self) -> None:
        sock = socks5_connect(self.proxy, self.host, self.port, self.timeout)
        self.sock = self.ssl_context.wrap_socket(sock, server_hostname=self.host)


class _SocksHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, proxy: str) -> None:
        super().__init__()
        self.proxy = proxy

    def https_open(self, req):
        return self.do_open(
            lambda host, **kwargs: _SocksHTTPSConnection(host, self.proxy, **kwargs), req
        )


class TelegramSender:
    """Сообщения в телеграм от бота, созданного через @BotFather"""

    def __init__(
        self,
        token: str,
        chat_ids: list[str],
        api_url: str = TELEGRAM_API_URL,
        proxy: str = "",
        timeout_secs: int = 30,
    ) -> None:
        self.token = token
        self.chat_ids = chat_ids
        self.api_url = api_url.rstrip("/")
        # socks5 прокси, если телеграм с сервера напрямую недоступен
        self.proxy = proxy
        self.timeout_secs = timeout_secs

    @classmethod
    def from_settings(cls, settings) -> "TelegramSender | None":
        """Отправщик из настроек, None - если TELEGRAM_BOT_TOKEN не задан"""
        if not settings.telegram_token:
            return None
        return cls(
            token=settings.telegram_token,
            chat_ids=settings.telegram_chat_ids,
            api_url=settings.telegram_api_url or TELEGRAM_API_URL,
            proxy=settings.telegram_proxy,
        )

    def bot_name(self) -> str:
        """
        Имя бота - заодно проверка токена
        Raises:
            TelegramError: токен неверный или телеграм недоступен
        """
        return "@" + str((self._call("getMe") or {}).get("username", ""))

    def find_chats(self) -> list[tuple[str, str]]:
        """Чаты, которые недавно писали боту: (id, название) - так узнаётся TELEGRAM_CHAT_ID"""
        chats: dict[str, str] = {}
        for update in self._call("getUpdates") or []:
            message = update.get("message") or update.get("channel_post") or {}
            chat = message.get("chat") or {}
            if "id" not in chat:
                continue
            name = " ".join(filter(None, (chat.get("first_name"), chat.get("last_name"))))
            chats[str(chat["id"])] = chat.get("title") or name or chat.get("username", "")
        return list(chats.items())

    def send(self, text: str) -> None:
        """
        Отправляет сообщение с html разметкой телеграма во все чаты
        Raises:
            TelegramError: если хоть в один чат не ушло
        """
        if not self.chat_ids:
            raise TelegramError("не задан TELEGRAM_CHAT_ID")
        for chat_id in self.chat_ids:
            self._call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True},
                },
            )
        logger.info("сообщение в телеграм отправлено")

    def _call(self, method: str, body: dict | None = None):
        # в тексте ошибок не должно быть адреса: в нём токен бота
        request = urllib.request.Request(
            url=f"{self.api_url}/bot{self.token}/{method}",
            data=json.dumps(body or {}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if self.proxy:
            # ProxyHandler({}) - чтобы HTTPS_PROXY из окружения не встал поверх socks
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _SocksHTTPSHandler(self.proxy)
            )
            open_url = opener.open
        else:
            open_url = urllib.request.urlopen
        try:
            with open_url(request, timeout=self.timeout_secs) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            # телеграм объясняет ошибку в теле ответа: "Bad Request: chat not found"
            try:
                description = json.loads(error.read().decode("utf-8")).get("description")
            except (OSError, ValueError, AttributeError):
                description = ""
            raise TelegramError(description or f"http ошибка {error.code}") from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise TelegramError(f"телеграм недоступен: {error}") from error
        return payload.get("result")


def compose_subject(found_doctors: list) -> str:
    """Тема письма"""
    if len(found_doctors) == 1:
        return f"Горздрав: свободен врач {found_doctors[0].doctor.name}"
    return f"Горздрав: свободных врачей - {len(found_doctors)}"


def compose_text(found_doctors: list) -> str:
    """Текстовая версия письма"""
    blocks: list[str] = []
    for found in found_doctors:
        lines = [
            found.doctor.name,
            f"  Что отслеживаем: {found.watch_name}",
            f"  Мест для записи: {found.doctor.free_participant_count}, "
            + f"талонов: {found.doctor.free_ticket_count}",
        ]
        nearest = found.nearest_appointment
        if nearest:
            lines.append(
                "  Ближайший талон: " + nearest.visit_start.strftime("%d.%m.%Y %H:%M")
            )
        if found.appointments:
            lines.append(f"  Всего талонов в выдаче: {len(found.appointments)}")
        lines.append(f"  Записаться: {found.link}")
        blocks.append("\n".join(lines))
    return (
        "Появились свободные места для записи:\n\n"
        + "\n\n".join(blocks)
        + "\n\nПисьмо отправлено скриптом gorzdrav-email-checker."
    )


def compose_html(found_doctors: list) -> str:
    """Html версия письма"""
    items: list[str] = []
    for found in found_doctors:
        nearest = found.nearest_appointment
        nearest_block = ""
        if nearest:
            nearest_block = (
                "<div>Ближайший талон: <b>"
                + html.escape(nearest.visit_start.strftime("%d.%m.%Y %H:%M"))
                + "</b></div>"
            )
        items.append(
            "<li style='margin-bottom:16px'>"
            + f"<div><b>{html.escape(found.doctor.name)}</b></div>"
            + f"<div>Что отслеживаем: {html.escape(found.watch_name)}</div>"
            + f"<div>Мест для записи: {found.doctor.free_participant_count}, "
            + f"талонов: {found.doctor.free_ticket_count}</div>"
            + nearest_block
            + f"<div><a href='{html.escape(found.link)}'>Записаться на приём</a></div>"
            + "</li>"
        )
    return (
        '<html><body style="font-family:Arial,sans-serif">'
        + "<p>Появились свободные места для записи:</p>"
        + "<ul style='list-style:none;padding-left:0'>"
        + "".join(items)
        + "</ul>"
        + "<p style='color:#888;font-size:12px'>"
        + "Письмо отправлено скриптом gorzdrav-email-checker.</p>"
        + "</body></html>"
    )


def describe_booking(booking) -> str:
    """Время, кабинет и адрес записи"""
    parts = [booking.visit_start.strftime("%d.%m.%Y %H:%M")]
    if booking.room:
        parts.append(f"кабинет {booking.room}")
    if booking.address:
        parts.append(booking.address)
    return ", ".join(parts)


def compose_booking_subject(events: list) -> str:
    """Тема письма об автозаписи"""
    event = events[0]
    if len(events) > 1:
        return f"Горздрав: изменения в записях - {len(events)}"
    when = event.booking.visit_start.strftime("%d.%m %H:%M") if event.booking else ""
    return {
        "booked": f"Горздрав: вы записаны на {when}",
        "rebooked": f"Горздрав: перезаписал на {when}",
        "cancelled": f"Горздрав: запись на {when} отменена",
    }.get(event.kind, f"Горздрав: автозапись не удалась ({event.watch})")


def _booking_lines(event) -> list[str]:
    """Строки письма о событии"""
    titles = {
        "booked": "Записал на приём",
        "rebooked": "Перезаписал на более удобное время",
        "cancelled": "Запись отменена",
        "failed": "Автозапись не удалась",
    }
    lines = [f"{titles.get(event.kind, event.kind)} - {event.watch}"]
    if event.booking:
        lines.append(event.booking.doctor_name)
        lines.append(describe_booking(event.booking))
    if event.previous:
        # прежнюю запись могло не получиться отменить - тогда об этом скажет message
        label = "Прежняя запись" if event.previous.is_active else "Прежняя запись отменена"
        lines.append(f"{label}: " + describe_booking(event.previous))
    if event.message:
        lines.append(event.message)
    return lines


def _cancel_link(event, canceller) -> tuple[str, str] | None:
    """
    Ссылка отмены и подсказка к ней. canceller - объект с методами url(booking)
    и hint(booking): веб-сервер отмены или ящик для писем с отменой
    """
    if canceller is None or event.booking is None or not event.booking.is_active:
        return None
    return canceller.url(event.booking), canceller.hint(event.booking)


def compose_booking_text(events: list, canceller=None) -> str:
    blocks = []
    for event in events:
        title, *rest = _booking_lines(event)
        lines = [title] + [f"  {text}" for text in rest]
        cancel = _cancel_link(event, canceller)
        if cancel:
            lines.append(f"  Отменить запись: {cancel[1]}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n\nПисьмо отправлено скриптом gorzdrav-email-checker."


def compose_booking_html(events: list, canceller=None) -> str:
    items = []
    for event in events:
        title, *rest = _booking_lines(event)
        rows = [f"<div><b>{html.escape(title)}</b></div>"]
        rows += [f"<div>{html.escape(text)}</div>" for text in rest]
        cancel = _cancel_link(event, canceller)
        if cancel:
            link, hint = cancel
            rows.append(
                f"<div style='margin-top:10px'><a href='{html.escape(link)}' "
                + "style='display:inline-block;padding:8px 14px;border-radius:6px;"
                + "background:#b42318;color:#fff;text-decoration:none'>"
                + "Отменить запись</a></div>"
            )
            if not link.startswith("http"):
                rows.append(
                    "<div style='color:#888;font-size:12px;margin-top:4px'>"
                    + f"Откроется готовое письмо - просто отправьте его. Или вручную: {html.escape(hint)}"
                    + "</div>"
                )
        items.append("<li style='margin-bottom:16px'>" + "".join(rows) + "</li>")
    return (
        '<html><body style="font-family:Arial,sans-serif">'
        + "<ul style='list-style:none;padding-left:0'>"
        + "".join(items)
        + "</ul>"
        + "<p style='color:#888;font-size:12px'>"
        + "Письмо отправлено скриптом gorzdrav-email-checker.</p>"
        + "</body></html>"
    )


def format_slot(visit_start) -> str:
    """Время талона с днём недели: ср 15.10 16:00"""
    return f"{WEEKDAYS[visit_start.weekday()]} {visit_start.strftime('%d.%m %H:%M')}"


def _join_for_telegram(blocks: list[str]) -> str:
    """Склеивает блоки сообщения, пока оно влезает в лимит телеграма"""
    text = ""
    for index, block in enumerate(blocks):
        if len(text) + len(block) > TELEGRAM_MAX_LENGTH:
            return text + f"\n\n…и ещё {len(blocks) - index}"
        text += ("\n\n" if text else "") + block
    return text


def compose_telegram(found_doctors: list) -> str:
    """Сообщение в телеграм о свободных талонах: врач, время талонов, ссылка"""
    blocks = ["Появились свободные талоны:"]
    for found in found_doctors:
        lines = [
            f"<b>{html.escape(found.doctor.name)}</b>",
            html.escape(found.watch_name),
        ]
        times = sorted(item.visit_start for item in found.appointments)
        if times:
            shown = ", ".join(format_slot(item) for item in times[:TELEGRAM_MAX_SLOTS])
            if len(times) > TELEGRAM_MAX_SLOTS:
                shown += f" и ещё {len(times) - TELEGRAM_MAX_SLOTS}"
            lines.append(f"Талоны: {shown}")
        else:
            lines.append(f"Мест для записи: {found.doctor.free_participant_count}")
        lines.append(f'<a href="{html.escape(found.link)}">Записаться</a>')
        blocks.append("\n".join(lines))
    return _join_for_telegram(blocks)


def compose_booking_telegram(events: list, canceller=None) -> str:
    """Сообщение в телеграм о записях, отменах и ошибках автозаписи"""
    blocks = []
    for event in events:
        title, *rest = _booking_lines(event)
        lines = [f"<b>{html.escape(title)}</b>"] + [html.escape(text) for text in rest]
        cancel = _cancel_link(event, canceller)
        if cancel:
            link, hint = cancel
            # ссылку mailto телеграм не откроет, для неё только подсказка
            if link.startswith("http"):
                lines.append(f'<a href="{html.escape(link)}">Отменить запись</a>')
            else:
                lines.append("Отменить запись: " + html.escape(hint))
        blocks.append("\n".join(lines))
    return _join_for_telegram(blocks)
