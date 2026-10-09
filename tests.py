"""
Тесты без сторонних библиотек:
    python3 -m unittest -v
Сеть не используется - только разбор ссылок, фильтры и логика повторных писем.
"""

import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import booking
import gorzdrav
import notifier
from checker import (
    SPB_TIMEZONE,
    FoundDoctor,
    NotifyState,
    Watch,
    appointments_in_limit,
    reload_watches,
)
from settings import Settings

DOCTOR_URL = (
    "https://gorzdrav.spb.ru/service-free-schedule#"
    "%5B%7B%22district%22:%222%22%7D,%7B%22lpu%22:%22526%22%7D,"
    "%7B%22speciality%22:%2292%22%7D,%7B%22schedule%22:%2234%22%7D,"
    "%7B%22doctor%22:%2234%22%7D%5D"
)
SPECIALTY_URL = (
    "https://gorzdrav.spb.ru/service-free-schedule#"
    "%5B%7B%22district%22:%222%22%7D,%7B%22lpu%22:%22526%22%7D,"
    "%7B%22speciality%22:%2292%22%7D%5D"
)


def make_doctor(doctor_id: str = "1", name: str = "Иванов Иван", free: int = 3):
    return gorzdrav.Doctor(
        id=doctor_id,
        name=name,
        free_participant_count=free,
        free_ticket_count=free,
    )


def make_appointment(days_from_today: int) -> gorzdrav.Appointment:
    visit_start = datetime.datetime.now(SPB_TIMEZONE) + datetime.timedelta(
        days=days_from_today
    )
    return gorzdrav.Appointment(id=f"a{days_from_today}", visit_start=visit_start)


def make_found(free: int = 3, appointment_day: int | None = 1) -> FoundDoctor:
    return FoundDoctor(
        watch_name="тест",
        doctor=make_doctor(free=free),
        link="https://example.com",
        appointments=[make_appointment(appointment_day)] if appointment_day else [],
    )


class TestLinks(unittest.TestCase):
    def test_parse_link_with_doctor(self):
        ids = gorzdrav.parse_link(DOCTOR_URL)
        self.assertEqual(ids["district"], "2")
        self.assertEqual(ids["lpu"], "526")
        self.assertEqual(ids["speciality"], "92")
        self.assertEqual(ids["doctor"], "34")

    def test_generate_link_is_parsable_back(self):
        link = gorzdrav.generate_link("2", 526, "92", "34")
        self.assertEqual(gorzdrav.parse_link(link)["lpu"], "526")
        self.assertEqual(gorzdrav.parse_link(link)["doctor"], "34")

    def test_parse_link_without_ids(self):
        self.assertEqual(gorzdrav.parse_link("https://example.com"), {})


class TestWatch(unittest.TestCase):
    def test_from_doctor_url(self):
        watch = Watch.from_dict({"url": DOCTOR_URL})
        self.assertTrue(watch.is_valid)
        self.assertEqual(watch.lpu_id, 526)
        self.assertEqual(watch.specialty_id, "92")
        self.assertEqual(watch.doctor_ids, ["34"])

    def test_specialty_url_watches_any_doctor(self):
        watch = Watch.from_dict({"url": SPECIALTY_URL})
        self.assertEqual(watch.doctor_ids, [])
        self.assertTrue(watch.matches_doctor(make_doctor(doctor_id="777")))

    def test_any_doctor_flag_ignores_doctor_in_url(self):
        self.assertEqual(Watch.from_dict({"url": DOCTOR_URL, "anyDoctor": True}).doctor_ids, [])

    def test_explicit_fields_win_over_url(self):
        watch = Watch.from_dict({"url": DOCTOR_URL, "lpuId": 1054, "specialtyId": "98"})
        self.assertEqual(watch.lpu_id, 1054)
        self.assertEqual(watch.specialty_id, "98")

    def test_watch_without_ids_is_invalid(self):
        self.assertFalse(Watch.from_dict({"name": "пусто"}).is_valid)

    def test_default_name(self):
        self.assertEqual(
            Watch.from_dict({"lpuId": 526, "specialtyId": "92"}).name,
            "ЛПУ 526, специальность 92",
        )

    def test_matches_doctor_by_id(self):
        watch = Watch.from_dict({"lpuId": 1, "specialtyId": "1", "doctorIds": ["1"]})
        self.assertTrue(watch.matches_doctor(make_doctor(doctor_id="1")))
        self.assertFalse(watch.matches_doctor(make_doctor(doctor_id="2")))

    def test_matches_doctor_by_name(self):
        watch = Watch.from_dict({"lpuId": 1, "specialtyId": "1", "doctorNames": ["иванов"]})
        self.assertTrue(watch.matches_doctor(make_doctor(name="Иванов Иван")))
        self.assertFalse(watch.matches_doctor(make_doctor(name="Петров Пётр")))

    def test_effective_limit_days(self):
        def limit(value):
            return Watch.from_dict(
                {"lpuId": 1, "specialtyId": "1", "limitDays": value}
            ).effective_limit_days

        self.assertIsNone(limit(0))
        self.assertIsNone(limit(-5))
        self.assertIsNone(limit(None))
        self.assertEqual(limit(500), 99)
        self.assertEqual(limit(7), 7)


class TestDoctor(unittest.TestCase):
    def test_has_free_places(self):
        self.assertTrue(make_doctor(free=1).has_free_places)
        self.assertFalse(make_doctor(free=0).has_free_places)


class TestAppointments(unittest.TestCase):
    def setUp(self):
        self.appointments = [
            make_appointment(0),
            make_appointment(4),
            make_appointment(30),
        ]

    def test_without_limit(self):
        self.assertEqual(len(appointments_in_limit(self.appointments, None)), 3)

    def test_with_limits(self):
        for limit_days, expected in ((1, 1), (5, 2), (99, 3)):
            with self.subTest(limit_days=limit_days):
                self.assertEqual(
                    len(appointments_in_limit(self.appointments, limit_days)), expected
                )

    def test_drops_past(self):
        self.assertEqual(appointments_in_limit([make_appointment(-2)], None), [])


class TestNotifyState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_notifies_once(self):
        state = NotifyState(self.path, cooldown_hours=6)
        found = make_found()
        self.assertTrue(state.should_notify(found))
        state.mark_notified(found)
        self.assertFalse(state.should_notify(found))

    def test_notifies_when_places_changed(self):
        state = NotifyState(self.path, cooldown_hours=6)
        state.mark_notified(make_found(free=3))
        self.assertTrue(state.should_notify(make_found(free=5)))

    def test_forget_gone_allows_new_letter(self):
        state = NotifyState(self.path, cooldown_hours=6)
        found = make_found()
        state.mark_notified(found)
        state.forget_gone(active_keys=set())
        self.assertFalse(state.should_notify(found))
        state.forget_gone(active_keys=set())
        self.assertTrue(state.should_notify(found))

    def test_keeps_state_when_one_check_temporarily_has_no_results(self):
        state = NotifyState(self.path, cooldown_hours=6)
        found = make_found()
        state.mark_notified(found)
        state.forget_gone(active_keys=set())
        state.forget_gone(active_keys={found.key})
        self.assertFalse(state.should_notify(found))

    def test_notifies_after_cooldown(self):
        state = NotifyState(self.path, cooldown_hours=1)
        found = make_found()
        state.mark_notified(found)
        state.data[found.key]["notified_at"] = (
            datetime.datetime.now(SPB_TIMEZONE) - datetime.timedelta(hours=2)
        ).isoformat()
        self.assertTrue(state.should_notify(found))

    def test_survives_restart(self):
        found = make_found()
        state = NotifyState(self.path, cooldown_hours=6)
        state.mark_notified(found)
        state.save()
        self.assertFalse(NotifyState(self.path, cooldown_hours=6).should_notify(found))

    def test_readonly_does_not_write(self):
        state = NotifyState(self.path, cooldown_hours=6, readonly=True)
        state.mark_notified(make_found())
        state.save()
        self.assertFalse(self.path.exists())

    def test_ignores_broken_file(self):
        self.path.write_text("{битый json", encoding="utf-8")
        state = NotifyState(self.path, cooldown_hours=6)
        self.assertEqual(state.data, {})
        state.save()
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {})


class TestReloadWatches(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "watches.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_picks_up_removed_watch(self):
        first = {"name": "первое", "lpuId": 526, "specialtyId": "92"}
        second = {"name": "второе", "lpuId": 1138, "specialtyId": "89"}
        self.write([first, second])
        watches = reload_watches(self.path, [])
        self.assertEqual(len(watches), 2)
        self.write([first])
        watches = reload_watches(self.path, watches)
        self.assertEqual([watch.name for watch in watches], ["первое"])

    def test_keeps_previous_list_when_file_is_broken(self):
        current = [Watch.from_dict({"lpuId": 526, "specialtyId": "92"})]
        self.path.write_text("[{битый json", encoding="utf-8")
        self.assertIs(reload_watches(self.path, current), current)

    def test_keeps_previous_list_when_file_is_missing(self):
        current = [Watch.from_dict({"lpuId": 526, "specialtyId": "92"})]
        self.assertIs(reload_watches(self.path, current), current)


PATIENT = gorzdrav.Patient(
    last_name="Иванов",
    first_name="Иван",
    middle_name="Иванович",
    birthdate=datetime.date(1990, 1, 31),
)


class FakeGorzdrav:
    """Горздрав в памяти: помнит записи пациента, умеет запрещать вторую запись"""

    def __init__(self, allow_two: bool = True):
        self.allow_two = allow_two
        self.booked: dict[str, gorzdrav.Appointment] = {}
        self.taken: set[str] = set()
        self.create_calls: list[str] = []
        self.cancel_calls: list[str] = []

    def create(self, lpu_id, patient_id, patient, appointment, recipient_email=""):
        self.create_calls.append(appointment.id)
        if appointment.id in self.taken:
            raise gorzdrav.GorzdravError("талон занят")
        if self.booked and not self.allow_two:
            raise gorzdrav.GorzdravError("пациент уже записан")
        self.booked[appointment.id] = appointment

    def cancel(self, lpu_id, patient_id, appointment_id):
        self.cancel_calls.append(appointment_id)
        self.booked.pop(appointment_id, None)

    def patient_appointments(self, lpu_id, patient_id):
        return [
            gorzdrav.PatientAppointment(
                id=item.id,
                visit_start=item.visit_start,
                doctor_id="1",
                doctor_name="Иванов Иван",
                specialty_id="92",
            )
            for item in self.booked.values()
        ]


# понедельник далеко в будущем, чтобы тесты не зависели от сегодняшней даты
MONDAY = datetime.datetime(2030, 1, 7)


def slot_at(day: int, hour: int, minute: int = 0) -> gorzdrav.Appointment:
    visit_start = MONDAY + datetime.timedelta(days=day, hours=hour, minutes=minute)
    return gorzdrav.Appointment(
        id=visit_start.strftime("%Y%m%d%H%M"), visit_start=visit_start, room="208"
    )


def found_with(*appointments) -> list[FoundDoctor]:
    return [
        FoundDoctor(
            watch_name="тест",
            doctor=make_doctor(),
            link="https://example.com",
            appointments=list(appointments),
        )
    ]


class TestHourRank(unittest.TestCase):
    def test_order(self):
        hours = list(booking.DEFAULT_PREFERRED_HOURS)
        ranked = sorted(range(7, 22), key=lambda hour: booking.hour_rank(
            datetime.datetime(2030, 1, 7, hour), hours))
        self.assertEqual(ranked[:7], [16, 17, 18, 19, 15, 14, 13])

    def test_unknown_hours_go_last_later_first(self):
        hours = [16, 18]
        rank = lambda hour: booking.hour_rank(datetime.datetime(2030, 1, 7, hour), hours)
        self.assertLess(rank(18), rank(17))
        self.assertLess(rank(17), rank(9))

    def test_week_key(self):
        self.assertEqual(booking.week_key(MONDAY), booking.week_key(MONDAY + datetime.timedelta(days=6)))
        self.assertNotEqual(booking.week_key(MONDAY), booking.week_key(MONDAY + datetime.timedelta(days=7)))


class TestBooker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "bookings.json"
        self.fake = FakeGorzdrav()
        for name, target in (
            ("find_patient", lambda lpu_id, patient: "p1"),
            ("create_appointment", self.fake.create),
            ("cancel_appointment", self.fake.cancel),
            ("get_patient_appointments", self.fake.patient_appointments),
        ):
            patcher = mock.patch.object(gorzdrav, name, side_effect=target)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(booking, "spb_now", return_value=MONDAY - datetime.timedelta(days=1))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.weekly = Watch.from_dict(
            {"name": "тест", "lpuId": 526, "specialtyId": "92", "autoBook": "weekly"}
        )

    def booker(self, **kwargs) -> booking.Booker:
        return booking.Booker(
            patient=PATIENT,
            store=booking.BookingStore(self.path),
            default_email="me@mail.ru",
            **kwargs,
        )

    def test_auto_book_modes(self):
        self.assertEqual(Watch.from_dict({"autoBook": True}).auto_book, "once")
        self.assertEqual(Watch.from_dict({}).auto_book, "")
        with self.assertRaises(ValueError):
            Watch.from_dict({"autoBook": "daily"})

    def test_books_preferred_hour_not_earliest(self):
        events = self.booker().process(
            self.weekly, found_with(slot_at(0, 9), slot_at(1, 18), slot_at(3, 16))
        )
        self.assertEqual([event.kind for event in events], ["booked"])
        self.assertEqual(events[0].booking.visit_start.hour, 16)
        self.assertEqual(list(self.fake.booked), [slot_at(3, 16).id])

    def test_one_booking_per_week_and_each_week(self):
        booker = self.booker()
        events = booker.process(
            self.weekly,
            found_with(slot_at(0, 16), slot_at(1, 16), slot_at(7, 17), slot_at(8, 9)),
        )
        self.assertEqual(
            sorted(event.booking.visit_start for event in events),
            [slot_at(0, 16).visit_start, slot_at(7, 17).visit_start],
        )
        # повторная проверка ничего не меняет
        self.assertEqual(booker.process(self.weekly, found_with(slot_at(1, 16))), [])

    def test_rebooks_to_better_time_and_cancels_old(self):
        booker = self.booker()
        booker.process(self.weekly, found_with(slot_at(0, 9)))
        events = booker.process(self.weekly, found_with(slot_at(2, 17)))
        self.assertEqual([event.kind for event in events], ["rebooked"])
        self.assertEqual(events[0].previous.visit_start.hour, 9)
        self.assertEqual(list(self.fake.booked), [slot_at(2, 17).id])

    def test_rebook_when_gorzdrav_allows_only_one_booking(self):
        self.fake.allow_two = False
        booker = self.booker()
        booker.process(self.weekly, found_with(slot_at(0, 9)))
        events = booker.process(self.weekly, found_with(slot_at(2, 16)))
        self.assertEqual([event.kind for event in events], ["rebooked"])
        self.assertEqual(list(self.fake.booked), [slot_at(2, 16).id])

    def test_failed_rebook_restores_old_booking(self):
        self.fake.allow_two = False
        booker = self.booker()
        booker.process(self.weekly, found_with(slot_at(0, 9)))
        self.fake.taken.add(slot_at(2, 16).id)
        self.assertEqual(booker.process(self.weekly, found_with(slot_at(2, 16))), [])
        self.assertEqual(list(self.fake.booked), [slot_at(0, 9).id])
        active = [item for item in booker.store.bookings if item.is_active]
        self.assertEqual([item.appointment_id for item in active], [slot_at(0, 9).id])
        # битый талон не дёргаем снова, пока не пройдёт FAILED_SLOT_TTL
        calls = len(self.fake.create_calls)
        booker.process(self.weekly, found_with(slot_at(2, 16)))
        self.assertEqual(len(self.fake.create_calls), calls)

    def test_tries_next_slot_when_taken(self):
        self.fake.taken.add(slot_at(0, 16).id)
        events = self.booker().process(self.weekly, found_with(slot_at(0, 16), slot_at(1, 17)))
        self.assertEqual(events[0].booking.visit_start, slot_at(1, 17).visit_start)

    def test_manual_booking_in_week_is_left_alone(self):
        self.fake.booked[slot_at(0, 9).id] = slot_at(0, 9)
        self.assertEqual(self.booker().process(self.weekly, found_with(slot_at(1, 16))), [])
        self.assertEqual(list(self.fake.booked), [slot_at(0, 9).id])

    def test_user_cancel_skips_week(self):
        booker = self.booker()
        events = booker.process(self.weekly, found_with(slot_at(0, 16)))
        booker.cancel_by_user(events[0].booking.token)
        self.assertEqual(self.fake.booked, {})
        self.assertEqual(booker.process(self.weekly, found_with(slot_at(1, 16))), [])

    def test_cancel_outside_skips_week_after_two_checks(self):
        booker = self.booker()
        booker.process(self.weekly, found_with(slot_at(0, 16)))
        self.fake.booked.clear()
        self.assertEqual(booker.process(self.weekly, found_with(slot_at(1, 16))), [])
        self.assertEqual(booker.process(self.weekly, found_with(slot_at(1, 16))), [])
        self.assertEqual(self.fake.booked, {})

    def test_once_mode_books_single_best(self):
        watch = Watch.from_dict({"name": "раз", "lpuId": 526, "specialtyId": "92", "autoBook": True})
        booker = self.booker()
        events = booker.process(watch, found_with(slot_at(0, 9), slot_at(9, 17)))
        self.assertEqual([event.booking.visit_start.hour for event in events], [17])
        self.assertTrue(booker.is_done(watch))

    def test_dry_run_does_not_book(self):
        self.assertEqual(self.booker(dry_run=True).process(self.weekly, found_with(slot_at(0, 16))), [])
        self.assertEqual(self.fake.create_calls, [])

    def test_failure_reported_once(self):
        self.fake.taken.add(slot_at(0, 16).id)
        booker = self.booker()
        events = booker.process(self.weekly, found_with(slot_at(0, 16)))
        self.assertEqual([event.kind for event in events], ["failed"])
        # тот же талон в ближайшие часы не пробуем
        self.assertEqual(booker.process(self.weekly, found_with(slot_at(0, 16))), [])
        self.assertEqual(self.fake.create_calls, [slot_at(0, 16).id])
        # а когда пробуем снова, про ту же ошибку не пишем
        booker.store.failed_slots.clear()
        self.assertEqual(booker.process(self.weekly, found_with(slot_at(0, 16))), [])
        self.assertEqual(len(self.fake.create_calls), 2)

    def test_store_survives_restart(self):
        self.booker().process(self.weekly, found_with(slot_at(0, 16)))
        store = booking.BookingStore(self.path)
        self.assertEqual(len(store.bookings), 1)
        self.assertEqual(store.bookings[0].visit_start, slot_at(0, 16).visit_start)

    def test_broken_store_is_an_error(self):
        self.path.write_text("{битый", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            booking.BookingStore(self.path)

    def test_email_has_cancel_link(self):
        events = self.booker().process(self.weekly, found_with(slot_at(0, 16)))
        text = notifier.compose_booking_text(events, "https://example.ru/gz")
        self.assertIn("/cancel/" + events[0].booking.token, text)
        self.assertIn("вы записаны", notifier.compose_booking_subject(events))


class TestCancelServer(unittest.TestCase):
    def test_get_shows_page_and_post_cancels(self):
        import urllib.error
        import urllib.request

        import cancel_server

        with tempfile.TemporaryDirectory() as tmp:
            store = booking.BookingStore(Path(tmp) / "bookings.json")
            item = booking.Booking(
                token="tok_abcdefgh", watch="тест", lpu_id=45, patient_id="p1",
                appointment_id="a1", doctor_id="1", doctor_name="Сардачук ЮН",
                visit_start=MONDAY, rank=0,
            )
            store.add(item)
            booker = booking.Booker(patient=PATIENT, store=store)
            cancelled = []
            with mock.patch.object(gorzdrav, "cancel_appointment") as cancel:
                server = cancel_server.start("127.0.0.1:0", booker, cancelled.append)
                self.addCleanup(server.server_close)
                self.addCleanup(server.shutdown)
                base = f"http://127.0.0.1:{server.server_address[1]}"
                page = urllib.request.urlopen(f"{base}/cancel/tok_abcdefgh").read().decode()
                self.assertIn("Отменить запись", page)
                cancel.assert_not_called()
                urllib.request.urlopen(
                    urllib.request.Request(f"{base}/cancel/tok_abcdefgh", data=b"", method="POST")
                )
                cancel.assert_called_once_with(45, "p1", "a1")
            self.assertFalse(item.is_active)
            self.assertEqual(item.cancelled_by, "user")
            self.assertEqual(cancelled, [item])
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(f"{base}/cancel/unknown_token")


class TestSettings(unittest.TestCase):
    def test_patient_from_settings(self):
        settings = Settings(
            patient_last_name="Иванов",
            patient_first_name="Иван",
            patient_birthdate="31.01.1990",
        )
        self.assertEqual(settings.patient().birthdate, datetime.date(1990, 1, 31))
        self.assertEqual(settings.patient().birthdate_param, "1990-01-31T00:00:00")
        with self.assertRaises(ValueError):
            Settings(patient_last_name="Иванов", patient_first_name="Иван",
                     patient_birthdate="1990-01-31").patient()
        with self.assertRaises(ValueError):
            Settings().patient()

    def test_clean_value(self):
        from settings import _clean_value

        self.assertEqual(_clean_value(' "secret" '), "secret")
        self.assertEqual(_clean_value("'secret'"), "secret")
        self.assertEqual(_clean_value("secret  # комментарий"), "secret")
        self.assertEqual(_clean_value('"secret # внутри кавычек" # комментарий'),
                         "secret # внутри кавычек")
        self.assertEqual(_clean_value("pa#ssword"), "pa#ssword")

    def test_env_file_parsing_and_ssl_by_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "# комментарий\n"
                'SMTP_HOST="smtp.mail.ru"\n'
                "SMTP_PORT=587\n"
                "SMTP_USER=me@mail.ru\n"
                "SMTP_PASSWORD=secret\n"
                'EMAIL_TO="one@mail.ru, two@mail.ru"\n'
                "\n",
                encoding="utf-8",
            )
            settings = Settings.load(env_file)
        self.assertEqual(settings.smtp_host, "smtp.mail.ru")
        self.assertEqual(settings.email_to, ["one@mail.ru", "two@mail.ru"])
        self.assertEqual(settings.email_from, "me@mail.ru")
        # 587 - это starttls
        self.assertFalse(settings.smtp_ssl)


if __name__ == "__main__":
    unittest.main()
