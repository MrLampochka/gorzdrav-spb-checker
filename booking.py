"""
Автозапись на талоны горздрава.

Два режима отслеживания:
    once   - записаться один раз на самый удобный талон и больше не трогать
    weekly - держать одну запись в каждой неделе, где есть талоны,
             и перезаписываться, если появилось более удобное время

Удобство времени задаётся порядком часов: по умолчанию 16, 17, 18, 19, 15, 14...
Если вы отменили запись бота, он больше не записывает на этот день и час,
но в ту же неделю на другое время записать может.
Все записи бота лежат в bookings.json, у каждой есть токен для ссылки отмены.
"""

import datetime
import json
import logging
import secrets
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

import gorzdrav

logger = logging.getLogger("booking")

SPB_TIMEZONE = datetime.timezone(offset=datetime.timedelta(hours=3))

MODE_ONCE = "once"
MODE_WEEKLY = "weekly"

# сначала вечер, потом всё более раннее время, поздний вечер в конце
DEFAULT_PREFERRED_HOURS = (16, 17, 18, 19, 15, 14, 13, 12, 11, 10, 9, 8, 7, 20, 21)
# сколько талонов пробовать за одну проверку, если их успевают занять
MAX_BOOKING_ATTEMPTS = 3
# не записываемся на талоны, до которых осталось меньше этого
MIN_LEAD = datetime.timedelta(hours=2)
# талон, на который не получилось записаться, не трогаем это время
FAILED_SLOT_TTL = datetime.timedelta(hours=6)
# сколько проверок подряд запись должна пропадать из горздрава, чтобы считать её отменённой
REQUIRED_MISSING_CHECKS = 2

STATUS_ACTIVE = "active"
STATUS_CANCELLED = "cancelled"
CANCELLED_BY_USER = "user"
CANCELLED_BY_BOT = "bot"
CANCELLED_OUTSIDE = "outside"


def spb_now() -> datetime.datetime:
    """Текущее время по Петербургу без таймзоны, как в ответах api"""
    return datetime.datetime.now(SPB_TIMEZONE).replace(tzinfo=None)


def hour_rank(visit_start: datetime.datetime, preferred_hours: list[int]) -> int:
    """Насколько удобен час талона: 0 - лучший. Часы не из списка - после, поздние раньше"""
    if visit_start.hour in preferred_hours:
        return preferred_hours.index(visit_start.hour)
    return len(preferred_hours) + 23 - visit_start.hour


def week_key(moment: datetime.datetime) -> str:
    """Неделя с понедельника: 2026-W42"""
    iso = moment.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


@dataclass
class Slot:
    """Свободный талон, на который можно записаться"""

    appointment: gorzdrav.Appointment
    doctor: gorzdrav.Doctor
    rank: int

    @property
    def week(self) -> str:
        return week_key(self.appointment.visit_start)


def rank_slots(
    found_doctors: list,
    preferred_hours: list[int],
    now: datetime.datetime,
) -> list[Slot]:
    """Все талоны найденных врачей от самого удобного: по часу, потом по дате"""
    slots = [
        Slot(
            appointment=appointment,
            doctor=found.doctor,
            rank=hour_rank(appointment.visit_start, preferred_hours),
        )
        for found in found_doctors
        for appointment in found.appointments
        if appointment.visit_start - now >= MIN_LEAD
    ]
    return sorted(slots, key=lambda slot: (slot.rank, slot.appointment.visit_start))


@dataclass
class Booking:
    """Запись, сделанная ботом"""

    token: str
    watch: str
    lpu_id: int
    patient_id: str
    appointment_id: str
    doctor_id: str
    doctor_name: str
    visit_start: datetime.datetime
    rank: int
    room: str = ""
    number: str = ""
    address: str = ""
    status: str = STATUS_ACTIVE
    cancelled_by: str = ""
    created_at: str = ""
    missing_checks: int = 0

    @property
    def week(self) -> str:
        return week_key(self.visit_start)

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    @property
    def appointment(self) -> gorzdrav.Appointment:
        """Талон записи - чтобы записаться на него снова"""
        return gorzdrav.Appointment(
            id=self.appointment_id,
            visit_start=self.visit_start,
            room=self.room,
            number=self.number,
            address=self.address,
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        data["visit_start"] = self.visit_start.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Booking":
        data = dict(data)
        data["visit_start"] = datetime.datetime.fromisoformat(data["visit_start"])
        return cls(**data)


@dataclass
class BookingEvent:
    """Что произошло с записью - из этого собирается письмо"""

    kind: str  # booked | rebooked | cancelled | failed
    watch: str
    booking: Booking | None = None
    previous: Booking | None = None
    message: str = ""
    recipients: list[str] = field(default_factory=list)


class BookingStore:
    """
    Записи бота, недавние ошибки и талоны, на которые не удалось записаться.
    Лежит в bookings.json, доступ из цикла проверок и из сервера отмены.
    """

    def __init__(self, path: Path, readonly: bool = False) -> None:
        self.path = path
        self.readonly = readonly
        self.lock = threading.RLock()
        self.bookings: list[Booking] = []
        self.failures: dict[str, dict] = {}
        self.failed_slots: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.bookings = [Booking.from_dict(item) for item in raw.get("bookings", [])]
            self.failures = raw.get("failures", {})
            self.failed_slots = raw.get("failed_slots", {})
        except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError) as error:
            # молча начинать с нуля опасно: бот забудет свои записи и запишется повторно
            raise RuntimeError(f"не удалось прочитать {self.path}: {error}") from error

    def save(self) -> None:
        if self.readonly:
            return
        with self.lock:
            data = {
                "bookings": [booking.to_dict() for booking in self.bookings],
                "failures": self.failures,
                "failed_slots": self.failed_slots,
            }
            tmp_path = self.path.with_suffix(".tmp")
            tmp_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp_path.replace(self.path)

    def get(self, token: str) -> Booking | None:
        with self.lock:
            return next((item for item in self.bookings if item.token == token), None)

    def add(self, booking: Booking) -> None:
        with self.lock:
            self.bookings.append(booking)

    def for_watch(self, watch_name: str) -> list[Booking]:
        with self.lock:
            return [item for item in self.bookings if item.watch == watch_name]

    def active_in_week(self, watch_name: str, week: str) -> list[Booking]:
        """Активные записи бота в неделе, самая удобная первой"""
        return sorted(
            (
                item
                for item in self.for_watch(watch_name)
                if item.is_active and item.week == week
            ),
            key=lambda item: (item.rank, item.visit_start),
        )

    def excluded_hours(self, watch_name: str) -> set[tuple[datetime.date, int]]:
        """День и час записей, которые отменили вы, а не бот - туда больше не записываем"""
        return {
            (item.visit_start.date(), item.visit_start.hour)
            for item in self.for_watch(watch_name)
            if item.cancelled_by in (CANCELLED_BY_USER, CANCELLED_OUTSIDE)
        }

    def owns(self, appointment_id: str) -> bool:
        with self.lock:
            return any(
                item.appointment_id == appointment_id and item.is_active
                for item in self.bookings
            )

    def mark_failed_slot(self, appointment_id: str, now: datetime.datetime) -> None:
        with self.lock:
            self.failed_slots[appointment_id] = now.isoformat()

    def slot_failed_recently(self, appointment_id: str, now: datetime.datetime) -> bool:
        with self.lock:
            failed_at = self.failed_slots.get(appointment_id)
            if not failed_at:
                return False
            if now - datetime.datetime.fromisoformat(failed_at) > FAILED_SLOT_TTL:
                del self.failed_slots[appointment_id]
                return False
            return True

    def should_report_failure(
        self,
        key: str,
        message: str,
        now: datetime.datetime,
        cooldown: datetime.timedelta,
    ) -> bool:
        """Одну и ту же ошибку пишем не чаще раза в cooldown"""
        with self.lock:
            previous = self.failures.get(key)
            if (
                previous
                and previous.get("message") == message
                and now - datetime.datetime.fromisoformat(previous["at"]) < cooldown
            ):
                return False
            self.failures[key] = {"message": message, "at": now.isoformat()}
            return True


class Booker:
    """Записывает, перезаписывает и отменяет записи для отслеживаний с autoBook"""

    def __init__(
        self,
        patient: gorzdrav.Patient | None,
        store: BookingStore,
        patient_error: str = "",
        dry_run: bool = False,
        default_email: str = "",
        failure_cooldown_hours: float = 6.0,
    ) -> None:
        self.patient = patient
        self.store = store
        self.patient_error = patient_error
        self.dry_run = dry_run
        # сюда горздрав пришлёт подтверждение записи
        self.default_email = default_email
        self.failure_cooldown = datetime.timedelta(hours=failure_cooldown_hours)
        # проверки и отмена по ссылке не должны менять записи одновременно
        self.lock = threading.RLock()
        self._patient_ids: dict[int, str] = {}

    def patient_id(self, lpu_id: int) -> str:
        if lpu_id not in self._patient_ids:
            self._patient_ids[lpu_id] = gorzdrav.find_patient(lpu_id, self.patient)
        return self._patient_ids[lpu_id]

    def is_done(self, watch) -> bool:
        """Разовая запись уже сделана - отслеживание больше не нужно"""
        return watch.auto_book == MODE_ONCE and bool(self.store.for_watch(watch.name))

    def process(self, watch, found_doctors: list) -> list[BookingEvent]:
        """
        Одна проверка отслеживания с автозаписью
        Args:
            watch: Watch: отслеживание с auto_book
            found_doctors: list[FoundDoctor]: врачи со свободными талонами
        Returns:
            list[BookingEvent]: события для письма
        """
        with self.lock:
            events = self._process(watch, found_doctors)
            self.store.save()
            return events

    def _process(self, watch, found_doctors: list) -> list[BookingEvent]:
        now = spb_now()
        if self.patient is None:
            return self._failure(watch, "patient", self.patient_error or "не заданы данные пациента")
        try:
            patient_id = self.patient_id(watch.lpu_id)
        except gorzdrav.GorzdravError as error:
            return self._failure(watch, "patient", f"пациент не найден в медучреждении: {error.message}")

        existing = [
            item
            for item in gorzdrav.get_patient_appointments(watch.lpu_id, patient_id)
            if item.visit_start > now
            and item.specialty_id == watch.specialty_id
            and watch.matches_doctor(gorzdrav.Doctor(item.doctor_id, item.doctor_name, 0, 0))
        ]
        self._sync(watch, existing, now)

        excluded = self.store.excluded_hours(watch.name)
        slots = [
            slot
            for slot in rank_slots(found_doctors, watch.preferred_hours, now)
            if not self.store.slot_failed_recently(slot.appointment.id, now)
            and (slot.appointment.visit_start.date(), slot.appointment.visit_start.hour)
            not in excluded
        ]
        if watch.auto_book == MODE_ONCE:
            groups = {"": slots}
        else:
            groups: dict[str, list[Slot]] = {}
            for slot in slots:
                groups.setdefault(slot.week, []).append(slot)

        events: list[BookingEvent] = []
        for week, week_slots in groups.items():
            in_week = [
                item for item in existing if not week or week_key(item.visit_start) == week
            ]
            # запись, сделанную вами вручную, бот не трогает
            if any(not self.store.owns(item.id) for item in in_week):
                continue
            mine = self.store.active_in_week(watch.name, week) if week else []
            if not mine:
                events += self._book(watch, patient_id, week_slots, week, now)
                continue
            events += self._cancel_duplicates(mine)
            better = [slot for slot in week_slots if slot.rank < mine[0].rank]
            if better:
                events += self._rebook(watch, patient_id, mine[0], better, now)
        return events

    def _sync(self, watch, existing: list, now: datetime.datetime) -> None:
        """Записи бота, которые пропали из горздрава, считаем отменёнными вами"""
        existing_ids = {item.id for item in existing}
        for booking in self.store.for_watch(watch.name):
            if not booking.is_active or booking.visit_start <= now:
                continue
            if booking.appointment_id in existing_ids:
                booking.missing_checks = 0
                continue
            booking.missing_checks += 1
            if booking.missing_checks >= REQUIRED_MISSING_CHECKS:
                logger.info(
                    "запись на %s пропала из горздрава, считаем отменённой вами - "
                    + "на этот час больше не записываем",
                    booking.visit_start,
                )
                booking.status = STATUS_CANCELLED
                booking.cancelled_by = CANCELLED_OUTSIDE

    def _new_booking(self, watch, patient_id: str, slot: Slot) -> Booking:
        appointment = slot.appointment
        return Booking(
            token=secrets.token_urlsafe(16),
            watch=watch.name,
            lpu_id=watch.lpu_id,
            patient_id=patient_id,
            appointment_id=appointment.id,
            doctor_id=slot.doctor.id,
            doctor_name=slot.doctor.name,
            visit_start=appointment.visit_start,
            rank=slot.rank,
            room=appointment.room,
            number=appointment.number,
            address=appointment.address,
            created_at=datetime.datetime.now(SPB_TIMEZONE).isoformat(),
        )

    def _create(self, watch, patient_id: str, appointment: gorzdrav.Appointment) -> None:
        gorzdrav.create_appointment(
            lpu_id=watch.lpu_id,
            patient_id=patient_id,
            patient=self.patient,
            appointment=appointment,
            recipient_email=(watch.email or [self.default_email])[0],
        )

    def _try_book(
        self,
        watch,
        patient_id: str,
        slots: list[Slot],
        now: datetime.datetime,
        errors: list[str],
    ) -> Booking | None:
        """Пробует записаться на талоны по очереди, ошибки складывает в errors"""
        for slot in slots[:MAX_BOOKING_ATTEMPTS]:
            visit = slot.appointment.visit_start.strftime("%d.%m.%Y %H:%M")
            try:
                self._create(watch, patient_id, slot.appointment)
            except gorzdrav.GorzdravError as error:
                logger.warning("не получилось записаться на %s: %s", visit, error)
                self.store.mark_failed_slot(slot.appointment.id, now)
                errors.append(f"{visit}: {error.message}")
                continue
            logger.info("'%s': записались к %s на %s", watch.name, slot.doctor.name, visit)
            booking = self._new_booking(watch, patient_id, slot)
            self.store.add(booking)
            # сохраняем сразу, чтобы не записаться второй раз, даже если дальше что-то упадёт
            self.store.save()
            return booking
        return None

    def _book(
        self,
        watch,
        patient_id: str,
        slots: list[Slot],
        week: str,
        now: datetime.datetime,
    ) -> list[BookingEvent]:
        if self.dry_run:
            best = slots[0]
            logger.info(
                "пробный запуск: записал бы к %s на %s",
                best.doctor.name,
                best.appointment.visit_start.strftime("%d.%m.%Y %H:%M"),
            )
            return []
        errors: list[str] = []
        booking = self._try_book(watch, patient_id, slots, now, errors)
        if booking is None:
            return self._failure(watch, f"book|{week}", "; ".join(errors))
        return [self._event("booked", watch, booking)]

    def _rebook(
        self,
        watch,
        patient_id: str,
        current: Booking,
        better: list[Slot],
        now: datetime.datetime,
    ) -> list[BookingEvent]:
        """
        Перезапись на более удобное время. Сначала пробуем записаться на новое,
        и только потом отменяем старое. Если горздрав не даёт держать две записи,
        отменяем старую, записываемся на новую, а при неудаче возвращаем старую.
        """
        if self.dry_run:
            logger.info(
                "пробный запуск: перезаписал бы с %s на %s",
                current.visit_start.strftime("%d.%m.%Y %H:%M"),
                better[0].appointment.visit_start.strftime("%d.%m.%Y %H:%M"),
            )
            return []
        errors: list[str] = []
        booking = self._try_book(watch, patient_id, better, now, errors)
        if booking is not None:
            event = self._event("rebooked", watch, booking, previous=current)
            try:
                self._cancel(current, CANCELLED_BY_BOT)
            except gorzdrav.GorzdravError as error:
                event.message = (
                    f"прежнюю запись отменить не получилось ({error.message}), "
                    "бот попробует ещё раз, или отмените её по ссылке"
                )
            return [event]

        best = better[0]
        logger.info("'%s': пробуем перезапись через отмену старой записи", watch.name)
        try:
            self._cancel(current, CANCELLED_BY_BOT)
        except gorzdrav.GorzdravError as error:
            logger.warning("не получилось отменить %s: %s", current.appointment_id, error)
            return []
        try:
            self._create(watch, patient_id, best.appointment)
        except gorzdrav.GorzdravError as error:
            logger.warning("перезапись не удалась: %s, возвращаем прежнюю запись", error)
            try:
                self._create(watch, patient_id, current.appointment)
            except gorzdrav.GorzdravError as restore_error:
                return [
                    self._event(
                        "failed",
                        watch,
                        current,
                        message=(
                            "пытался перезаписаться на более удобное время, но не вышло, "
                            + f"а прежнюю запись вернуть не удалось ({restore_error.message}). "
                            + "Запишитесь вручную!"
                        ),
                    )
                ]
            current.status = STATUS_ACTIVE
            current.cancelled_by = ""
            return []
        booking = self._new_booking(watch, patient_id, best)
        self.store.add(booking)
        self.store.save()
        return [self._event("rebooked", watch, booking, previous=current)]

    def _cancel_duplicates(self, mine: list[Booking]) -> list[BookingEvent]:
        """Если в неделе две записи бота (не отменилась старая) - оставляем удобную"""
        for extra in mine[1:]:
            try:
                self._cancel(extra, CANCELLED_BY_BOT)
            except gorzdrav.GorzdravError as error:
                logger.warning("не получилось отменить лишнюю запись: %s", error)
        return []

    def _cancel(self, booking: Booking, cancelled_by: str) -> None:
        gorzdrav.cancel_appointment(booking.lpu_id, booking.patient_id, booking.appointment_id)
        booking.status = STATUS_CANCELLED
        booking.cancelled_by = cancelled_by
        self.store.save()
        logger.info("запись на %s отменена (%s)", booking.visit_start, cancelled_by)

    def cancel_by_user(self, token: str) -> Booking:
        """
        Отмена по ссылке из письма. На этот день и час бот больше не записывает
        Raises:
            KeyError: нет записи с таким токеном
            gorzdrav.GorzdravError: горздрав не отменил
        """
        with self.lock:
            booking = self.store.get(token)
            if booking is None:
                raise KeyError(token)
            if booking.is_active:
                self._cancel(booking, CANCELLED_BY_USER)
            return booking

    def _event(self, kind: str, watch, booking: Booking | None, **kwargs) -> BookingEvent:
        return BookingEvent(
            kind=kind,
            watch=watch.name,
            booking=booking,
            recipients=list(watch.email),
            **kwargs,
        )

    def _failure(self, watch, key: str, message: str) -> list[BookingEvent]:
        logger.error("автозапись '%s': %s", watch.name, message)
        if not self.store.should_report_failure(
            f"{watch.name}|{key}", message, spb_now(), self.failure_cooldown
        ):
            return []
        return [self._event("failed", watch, None, message=message)]
