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

import gorzdrav
from checker import (
    SPB_TIMEZONE,
    FoundDoctor,
    NotifyState,
    Watch,
    appointments_in_limit,
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


class TestSettings(unittest.TestCase):
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
