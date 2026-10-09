"""
Маленький веб-сервер для отмены записи по ссылке из письма.

GET  /cancel/<токен>  - страница с записью и кнопкой «Отменить»
POST /cancel/<токен>  - собственно отмена

Отмена только по кнопке: почтовые сервисы сами открывают ссылки из писем
для проверки, и отмена по простому переходу срабатывала бы без вас.
"""

import html
import logging
import re
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import gorzdrav
from booking import Booker, Booking

logger = logging.getLogger("cancel_server")

CANCEL_PATH = re.compile(r"^/cancel/([A-Za-z0-9_-]{8,64})/?$")


def cancel_link(base_url: str, booking: Booking) -> str:
    return f"{base_url.rstrip('/')}/cancel/{booking.token}"


def _page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        + "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        + f"<title>{html.escape(title)}</title>"
        + "<style>body{font-family:Arial,sans-serif;max-width:480px;margin:40px auto;"
        + "padding:0 16px;color:#222}button{font-size:18px;padding:12px 20px;"
        + "background:#b42318;color:#fff;border:0;border-radius:8px;cursor:pointer}"
        + ".muted{color:#777}</style></head><body>"
        + f"<h2>{html.escape(title)}</h2>{body}</body></html>"
    ).encode("utf-8")


def _describe(booking: Booking) -> str:
    parts = [
        f"<p><b>{html.escape(booking.doctor_name)}</b></p>",
        f"<p>{booking.visit_start.strftime('%d.%m.%Y %H:%M')}",
    ]
    if booking.room:
        parts.append(f", кабинет {html.escape(booking.room)}")
    parts.append("</p>")
    if booking.address:
        parts.append(f"<p class='muted'>{html.escape(booking.address)}</p>")
    return "".join(parts)


def make_handler(
    booker: Booker,
    on_cancel: Callable[[Booking], None] | None = None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, title: str, body: str) -> None:
            content = _page(title, body)
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Robots-Tag", "noindex")
            self.end_headers()
            self.wfile.write(content)

        def _booking(self) -> Booking | None:
            found = CANCEL_PATH.match(self.path.split("?", 1)[0])
            booking = booker.store.get(found.group(1)) if found else None
            if booking is None:
                self._send(404, "Запись не найдена", "<p>Ссылка устарела или неверна.</p>")
            return booking

        def do_GET(self) -> None:
            booking = self._booking()
            if booking is None:
                return
            if not booking.is_active:
                self._send(200, "Запись уже отменена", _describe(booking))
                return
            self._send(
                200,
                "Отменить запись?",
                _describe(booking)
                + "<form method='post'><button type='submit'>Отменить запись</button></form>"
                + "<p class='muted'>Бот не будет записывать вас на эту неделю снова.</p>",
            )

        def do_POST(self) -> None:
            booking = self._booking()
            if booking is None:
                return
            was_active = booking.is_active
            try:
                booker.cancel_by_user(booking.token)
            except gorzdrav.GorzdravError as error:
                logger.error("отмена по ссылке не удалась: %s", error)
                self._send(
                    502,
                    "Не получилось отменить",
                    _describe(booking)
                    + f"<p>Горздрав ответил: {html.escape(error.message)}</p>"
                    + "<p>Попробуйте ещё раз или отмените на gorzdrav.spb.ru.</p>",
                )
                return
            logger.info("запись %s отменена по ссылке", booking.appointment_id)
            if was_active and on_cancel is not None:
                try:
                    on_cancel(booking)
                except Exception:
                    logger.exception("не удалось отправить письмо об отмене")
            self._send(200, "Запись отменена", _describe(booking))

        def log_message(self, format: str, *args) -> None:
            # в логе не нужны токены из адресов
            logger.debug("%s %s", self.command, self.address_string())

    return Handler


def start(
    listen: str,
    booker: Booker,
    on_cancel: Callable[[Booking], None] | None = None,
) -> ThreadingHTTPServer:
    """
    Запускает сервер в фоновом потоке
    Args:
        listen: str: адрес и порт, например 127.0.0.1:8085
    """
    host, _, port = listen.rpartition(":")
    server = ThreadingHTTPServer((host or "127.0.0.1", int(port)), make_handler(booker, on_cancel))
    threading.Thread(target=server.serve_forever, daemon=True, name="cancel-server").start()
    logger.info("сервер отмены слушает %s", listen)
    return server
