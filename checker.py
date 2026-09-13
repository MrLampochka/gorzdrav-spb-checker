#!/usr/bin/env python3
"""
Проверка свободных талонов на gorzdrav.spb.ru с оповещением на email.

Следит как за конкретными врачами, так и за целой специальностью
в медучреждении. Работает на голой стандартной библиотеке python 3.10+.

    python3 checker.py --once              # одна проверка (для cron)
    python3 checker.py                     # бесконечный цикл проверок
    python3 checker.py --once --dry-run    # проверка без отправки письма
    python3 checker.py --list-specialties 526
"""

import argparse
import datetime
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import gorzdrav
import notifier
from settings import BASE_DIR, Settings

logger = logging.getLogger("checker")

SPB_TIMEZONE = datetime.timezone(offset=datetime.timedelta(hours=3))
MAX_LIMIT_DAYS = 99

WATCHES_EXAMPLE = [
    {
        "name": "Терапия, поликлиника №1",
        "districtId": "2",
        "lpuId": 1054,
        "specialtyId": "98",
    },
    {
        "name": "Конкретный врач по ссылке с сайта",
        "url": "https://gorzdrav.spb.ru/service-free-schedule#...",
        "limitDays": 14,
    },
]


def spb_today() -> datetime.date:
    """Текущая дата по времени Санкт-Петербурга"""
    return datetime.datetime.now(SPB_TIMEZONE).date()


@dataclass
class Watch:
    """Что отслеживаем: врач или вся специальность в медучреждении"""

    name: str = ""
    url: str | None = None
    district_id: str = ""
    lpu_id: int | None = None
    specialty_id: str = ""
    doctor_ids: list[str] = field(default_factory=list)
    doctor_names: list[str] = field(default_factory=list)
    any_doctor: bool = False
    limit_days: int | None = None
    email: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "Watch":
        """
        Собирает отслеживание из записи в watches.json.
        Идентификаторы можно задать полями или одной ссылкой с сайта горздрава.
        """
        watch = cls(
            name=str(data.get("name") or ""),
            url=data.get("url"),
            district_id=str(data.get("districtId") or ""),
            lpu_id=int(data["lpuId"]) if data.get("lpuId") else None,
            specialty_id=str(data.get("specialtyId") or ""),
            doctor_ids=[str(item) for item in data.get("doctorIds") or []],
            doctor_names=[str(item) for item in data.get("doctorNames") or []],
            any_doctor=bool(data.get("anyDoctor")),
            limit_days=data.get("limitDays"),
            email=[str(item) for item in data.get("email") or []],
        )
        if watch.url:
            watch._fill_from_url()
        if not watch.name:
            watch.name = f"ЛПУ {watch.lpu_id}, специальность {watch.specialty_id}"
        return watch

    def _fill_from_url(self) -> None:
        """Дозаполняет идентификаторы из ссылки, не затирая заданные явно"""
        ids = gorzdrav.parse_link(self.url or "")
        self.district_id = self.district_id or ids.get("district", "")
        if self.lpu_id is None and "lpu" in ids:
            self.lpu_id = int(ids["lpu"])
        self.specialty_id = self.specialty_id or ids.get("speciality", "")
        doctor_from_url = ids.get("doctor") or ids.get("schedule")
        if doctor_from_url and not self.doctor_ids and not self.any_doctor:
            self.doctor_ids = [doctor_from_url]

    @property
    def is_valid(self) -> bool:
        return bool(self.lpu_id and self.specialty_id)

    @property
    def effective_limit_days(self) -> int | None:
        """Лимит дней, приведённый к разумным границам"""
        if not self.limit_days or self.limit_days < 0:
            return None
        return min(int(self.limit_days), MAX_LIMIT_DAYS)

    def matches_doctor(self, doctor: gorzdrav.Doctor) -> bool:
        """Подходит ли врач под фильтры отслеживания"""
        if self.doctor_ids and doctor.id not in self.doctor_ids:
            return False
        if self.doctor_names:
            doctor_name = doctor.name.lower()
            if not any(name.lower() in doctor_name for name in self.doctor_names):
                return False
        return True


@dataclass
class FoundDoctor:
    """Врач со свободными местами, про которого надо написать письмо"""

    watch_name: str
    doctor: gorzdrav.Doctor
    link: str
    appointments: list[gorzdrav.Appointment] = field(default_factory=list)
    recipients: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.watch_name}|{self.doctor.id}"

    @property
    def nearest_appointment(self) -> gorzdrav.Appointment | None:
        if not self.appointments:
            return None
        return min(self.appointments, key=lambda item: item.visit_start)

    @property
    def signature(self) -> str:
        """Отпечаток состояния врача, чтобы не слать одинаковые письма"""
        nearest = self.nearest_appointment
        return "|".join(
            (
                str(self.doctor.free_participant_count),
                str(self.doctor.free_ticket_count),
                nearest.visit_start.isoformat() if nearest else "-",
            )
        )


class NotifyState:
    """
    Помнит, про каких врачей письмо уже отправлено,
    чтобы не присылать одно и то же каждую проверку
    """

    def __init__(self, path: Path, cooldown_hours: float, readonly: bool = False) -> None:
        self.path = path
        self.readonly = readonly
        self.cooldown = datetime.timedelta(hours=cooldown_hours)
        self.data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            logger.warning("не удалось прочитать %s: %s", self.path, error)
            self.data = {}

    def save(self) -> None:
        if self.readonly:
            return
        try:
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as error:
            logger.warning("не удалось сохранить %s: %s", self.path, error)

    def should_notify(self, found: FoundDoctor) -> bool:
        """True, если про этого врача ещё не писали (или писали давно / другое)"""
        previous = self.data.get(found.key)
        if not previous:
            return True
        if previous.get("signature") != found.signature:
            return True
        try:
            notified_at = datetime.datetime.fromisoformat(previous["notified_at"])
        except (KeyError, ValueError):
            return True
        return datetime.datetime.now(SPB_TIMEZONE) - notified_at > self.cooldown

    def mark_notified(self, found: FoundDoctor) -> None:
        self.data[found.key] = {
            "doctor": found.doctor.name,
            "signature": found.signature,
            "notified_at": datetime.datetime.now(SPB_TIMEZONE).isoformat(),
            "missing_checks": 0,
        }

    def forget_gone(self, active_keys: set[str], required_missing_checks: int = 2) -> None:
        """
        Забывает врачей только после нескольких проверок без мест.
        Это защищает от временного пустого ответа API.
        """
        for key, previous in list(self.data.items()):
            if key in active_keys:
                previous["missing_checks"] = 0
                continue
            missing_checks = int(previous.get("missing_checks", 0)) + 1
            if missing_checks >= required_missing_checks:
                del self.data[key]
            else:
                previous["missing_checks"] = missing_checks


def appointments_in_limit(
    appointments: list[gorzdrav.Appointment],
    limit_days: int | None,
) -> list[gorzdrav.Appointment]:
    """
    Оставляет талоны, которые не в прошлом и попадают
    в лимит дней от сегодня (1 день - это сегодня)
    """
    today = spb_today()
    actual = [item for item in appointments if item.visit_start.date() >= today]
    if limit_days is None:
        return actual
    return [
        item
        for item in actual
        if (item.visit_start.date() - today).days + 1 <= limit_days
    ]


def check_watch(watch: Watch) -> list[FoundDoctor]:
    """
    Проверяет одно отслеживание
    Args:
        watch: Watch: что отслеживаем
    Returns:
        list[FoundDoctor]: врачи со свободными местами
    """
    doctors = gorzdrav.get_doctors(
        lpu_id=watch.lpu_id,
        specialty_id=watch.specialty_id,
    )
    logger.debug("%s: врачей в выдаче %s", watch.name, len(doctors))

    found: list[FoundDoctor] = []
    limit_days = watch.effective_limit_days
    for doctor in doctors:
        if not watch.matches_doctor(doctor) or not doctor.has_free_places:
            continue

        appointments = appointments_in_limit(
            appointments=gorzdrav.get_appointments(
                lpu_id=watch.lpu_id,
                doctor_id=doctor.id,
            ),
            limit_days=limit_days,
        )
        # с лимитом дней верим только реальным талонам в нужном диапазоне
        if limit_days and not appointments:
            logger.debug(
                "%s: у врача %s нет талонов в пределах %s дней",
                watch.name,
                doctor.name,
                limit_days,
            )
            continue

        found.append(
            FoundDoctor(
                watch_name=watch.name,
                doctor=doctor,
                link=gorzdrav.generate_link(
                    district_id=watch.district_id,
                    lpu_id=watch.lpu_id,
                    specialty_id=watch.specialty_id,
                    doctor_id=doctor.id,
                ),
                appointments=appointments,
                recipients=watch.email,
            )
        )
    return found


def load_watches(path: Path) -> list[Watch]:
    """
    Читает список отслеживаний из json файла.
    Если файла нет - создаёт пример и завершает работу.
    """
    if not path.exists():
        path.write_text(
            json.dumps(WATCHES_EXAMPLE, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.error(
            "Файл %s не найден, создан пример. Отредактируйте его и запустите снова.",
            path,
        )
        sys.exit(1)

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        logger.error("не удалось прочитать %s: %s", path, error)
        sys.exit(1)

    if isinstance(raw, dict):
        raw = [raw]

    watches: list[Watch] = []
    for item in raw:
        try:
            watch = Watch.from_dict(item)
        except (TypeError, ValueError, AttributeError) as error:
            logger.error("плохое отслеживание %s: %s", item, error)
            continue
        if not watch.is_valid:
            logger.error(
                "у отслеживания '%s' нет lpuId или specialtyId, пропускаем", watch.name
            )
            continue
        watches.append(watch)
    return watches


def notify(
    found_doctors: list[FoundDoctor],
    sender: notifier.EmailSender | None,
    default_recipients: list[str],
) -> None:
    """Отправляет письма, группируя врачей по получателям"""
    by_recipients: dict[tuple[str, ...], list[FoundDoctor]] = {}
    for found in found_doctors:
        recipients = tuple(found.recipients or default_recipients)
        by_recipients.setdefault(recipients, []).append(found)

    for recipients, doctors in by_recipients.items():
        subject = notifier.compose_subject(doctors)
        text = notifier.compose_text(doctors)
        if sender is None:
            print(f"\n--- {subject} -> {', '.join(recipients) or 'никому'} ---")
            print(text)
            continue
        if not recipients:
            logger.error("не задан EMAIL_TO и в отслеживании нет поля email")
            continue
        sender.send(
            to=list(recipients),
            subject=subject,
            text=text,
            html_body=notifier.compose_html(doctors),
        )


def run_once(
    watches: list[Watch],
    state: NotifyState,
    sender: notifier.EmailSender | None,
    default_recipients: list[str],
) -> int:
    """
    Одна проверка всех отслеживаний
    Returns:
        int: сколько врачей попало в письма
    """
    found_doctors: list[FoundDoctor] = []
    for watch in watches:
        try:
            found_doctors.extend(check_watch(watch))
        except gorzdrav.GorzdravError as error:
            # медучреждение может не отвечать, ждём следующей проверки
            logger.warning("ошибка проверки '%s': %s", watch.name, error)
        except Exception:
            logger.exception("неожиданная ошибка проверки '%s'", watch.name)

    logger.info("врачей со свободными местами: %s", len(found_doctors))

    state.forget_gone({found.key for found in found_doctors})
    to_notify = [found for found in found_doctors if state.should_notify(found)]
    if not to_notify:
        state.save()
        return 0

    for found in to_notify:
        logger.info(
            "письмо про врача %s (%s), мест %s, талонов %s",
            found.doctor.name,
            found.watch_name,
            found.doctor.free_participant_count,
            found.doctor.free_ticket_count,
        )

    try:
        notify(to_notify, sender, default_recipients)
    except notifier.EmailError as error:
        logger.error("%s", error)
        state.save()
        return 0

    for found in to_notify:
        state.mark_notified(found)
    state.save()
    return len(to_notify)


def print_reference(args: argparse.Namespace) -> bool:
    """Справочные запросы к горздраву для поиска нужных идентификаторов"""
    if args.list_districts:
        for district in gorzdrav.get_districts():
            print(f"{district.id}\t{district.name}")
        return True
    if args.list_lpus is not None:
        for lpu in gorzdrav.get_lpus(district_id=args.list_lpus):
            print(f"{lpu.id}\t{lpu.full_name}\t{lpu.address}")
        return True
    if args.list_specialties is not None:
        for specialty in gorzdrav.get_specialties(lpu_id=args.list_specialties):
            print(
                f"{specialty.id}\t{specialty.name}\t"
                + f"мест: {specialty.free_participant_count}, "
                + f"талонов: {specialty.free_ticket_count}"
            )
        return True
    if args.list_doctors is not None:
        lpu_id, specialty_id = args.list_doctors
        for doctor in gorzdrav.get_doctors(int(lpu_id), specialty_id):
            print(
                f"{doctor.id}\t{doctor.name}\t"
                + f"мест: {doctor.free_participant_count}, "
                + f"талонов: {doctor.free_ticket_count}"
            )
        return True
    return False


def parse_args(settings: Settings) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проверка свободных талонов горздрава с оповещением на email",
    )
    parser.add_argument("--config", default=str(settings.watches_file),
                        help="json файл со списком отслеживаний")
    parser.add_argument("--url", help="ссылка с горздрава вместо файла конфигурации")
    parser.add_argument("--limit-days", type=int,
                        help="искать талоны только в пределах N дней (для --url)")
    parser.add_argument("--email", help="получатели через запятую, вместо EMAIL_TO")
    parser.add_argument("--once", action="store_true", help="одна проверка и выход")
    parser.add_argument("--interval", type=int, default=settings.interval_secs,
                        help=f"период проверок в секундах (по умолчанию {settings.interval_secs})")
    parser.add_argument("--dry-run", action="store_true",
                        help="не отправлять письма, а печатать их в консоль")
    parser.add_argument("--state-file", default=str(settings.state_file),
                        help="файл с историей отправленных писем")
    parser.add_argument("--check-email", action="store_true",
                        help="проверить настройки почты (письмо не отправляется)")
    parser.add_argument("--list-districts", action="store_true", help="список районов")
    parser.add_argument("--list-lpus", metavar="DISTRICT_ID", help="список медучреждений")
    parser.add_argument("--list-specialties", metavar="LPU_ID", type=int,
                        help="список специальностей")
    parser.add_argument("--list-doctors", nargs=2, metavar=("LPU_ID", "SPECIALTY_ID"),
                        help="список врачей по специальности")
    return parser.parse_args()


def main() -> None:
    settings = Settings.load()
    args = parse_args(settings)
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    if print_reference(args):
        return

    if args.check_email:
        sender = notifier.EmailSender.from_settings(settings)
        sender.check_login()
        print(
            f"почта в порядке: {settings.smtp_host}:{settings.smtp_port} "
            + f"({'ssl' if settings.smtp_ssl else 'starttls'}), "
            + f"письма пойдут с {settings.email_from} на "
            + f"{', '.join(settings.email_to) or 'не задано (EMAIL_TO)'}"
        )
        return

    if args.url:
        watch = Watch.from_dict({"url": args.url, "limitDays": args.limit_days})
        if not watch.is_valid:
            logger.error("из ссылки не получилось достать lpu и специальность")
            sys.exit(1)
        watches = [watch]
    else:
        watches = load_watches(Path(args.config))

    if not watches:
        logger.error("нет ни одного корректного отслеживания")
        sys.exit(1)

    default_recipients = (
        [item.strip() for item in args.email.split(",") if item.strip()]
        if args.email
        else settings.email_to
    )

    sender: notifier.EmailSender | None = None
    if not args.dry_run:
        sender = notifier.EmailSender.from_settings(settings)
        if not default_recipients and not any(watch.email for watch in watches):
            logger.error("не заданы получатели: EMAIL_TO в .env или --email")
            sys.exit(1)

    state = NotifyState(
        path=Path(args.state_file),
        cooldown_hours=settings.cooldown_hours,
        readonly=args.dry_run,
    )

    logger.info("отслеживаний: %s", len(watches))
    for watch in watches:
        logger.info(
            "- %s (лпу %s, специальность %s, врачи: %s, лимит дней: %s)",
            watch.name,
            watch.lpu_id,
            watch.specialty_id,
            ", ".join(watch.doctor_ids) if watch.doctor_ids else "любые",
            watch.effective_limit_days or "без лимита",
        )

    if args.once:
        run_once(watches, state, sender, default_recipients)
        return

    logger.info("проверка каждые %s секунд, выход по Ctrl+C", args.interval)
    while True:
        run_once(watches, state, sender, default_recipients)
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except notifier.EmailError as error:
        logger.error("%s", error)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("остановлено пользователем")
