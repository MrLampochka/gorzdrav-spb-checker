"""Отправка писем о свободных талонах"""

import html
import logging
import smtplib
import ssl
from email.message import EmailMessage

logger = logging.getLogger(__name__)


class EmailError(Exception):
    """Ошибка настройки или отправки почты"""


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


def _booking_lines(event, cancel_base_url: str) -> list[tuple[str, str]]:
    """Строки письма о событии: (текст, ссылка)"""
    titles = {
        "booked": "Записал на приём",
        "rebooked": "Перезаписал на более удобное время",
        "cancelled": "Запись отменена",
        "failed": "Автозапись не удалась",
    }
    lines = [(f"{titles.get(event.kind, event.kind)} - {event.watch}", "")]
    if event.booking:
        lines.append((event.booking.doctor_name, ""))
        lines.append((describe_booking(event.booking), ""))
    if event.previous:
        lines.append(("Прежняя запись отменена: " + describe_booking(event.previous), ""))
    if event.message:
        lines.append((event.message, ""))
    if event.booking and event.booking.is_active and cancel_base_url:
        link = f"{cancel_base_url.rstrip('/')}/cancel/{event.booking.token}"
        lines.append(("Отменить запись", link))
    return lines


def compose_booking_text(events: list, cancel_base_url: str = "") -> str:
    blocks = []
    for event in events:
        title, *rest = _booking_lines(event, cancel_base_url)
        lines = [title[0]] + [
            f"  {text}: {link}" if link else f"  {text}" for text, link in rest
        ]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n\nПисьмо отправлено скриптом gorzdrav-email-checker."


def compose_booking_html(events: list, cancel_base_url: str = "") -> str:
    items = []
    for event in events:
        rows = []
        for index, (text, link) in enumerate(_booking_lines(event, cancel_base_url)):
            if link:
                rows.append(
                    f"<div style='margin-top:8px'><a href='{html.escape(link)}' "
                    + "style='color:#b42318'>"
                    + f"{html.escape(text)}</a></div>"
                )
            elif index == 0:
                rows.append(f"<div><b>{html.escape(text)}</b></div>")
            else:
                rows.append(f"<div>{html.escape(text)}</div>")
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
