"""
Минимальный клиент API gorzdrav.spb.ru.

Использует только стандартную библиотеку, чтобы скрипт можно было
положить на любую машину с python3 и запустить без установки пакетов.

Документация API:
https://github.com/egorantonov/gorzdrav/wiki/SPB-Gorzdrav-API-Documentation
"""

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import unquote, urlencode

API_URL = "https://gorzdrav.spb.ru/_api/api/v2"
HEADERS = {"User-Agent": "gorzdrav-email-checker"}
REQUEST_TIMEOUT_SECS = 30
# небольшая пауза между запросами, чтобы не долбить горздрав
REQUEST_DELAY_SECS = 1.0

# коды ошибок, которые означают просто пустой результат, а не поломку
ERROR_NO_SPECIALTIES = 37
ERROR_NO_DOCTORS = 38
ERROR_NO_TICKETS = 39
EMPTY_RESULT_ERROR_CODES = frozenset(
    (ERROR_NO_SPECIALTIES, ERROR_NO_DOCTORS, ERROR_NO_TICKETS)
)


class GorzdravError(Exception):
    """Ошибка обращения к API горздрава"""

    def __init__(
        self,
        message: str,
        error_code: int | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.url = url

    def __str__(self) -> str:
        return f"{self.message} (код {self.error_code}, {self.url})"


@dataclass
class District:
    """Район города"""

    id: str
    name: str


@dataclass
class Lpu:
    """Медучреждение"""

    id: int
    full_name: str
    address: str


@dataclass
class Specialty:
    """Специальность врача в медучреждении"""

    id: str
    name: str
    free_participant_count: int
    free_ticket_count: int


@dataclass
class Doctor:
    """Врач"""

    id: str
    name: str
    free_participant_count: int
    free_ticket_count: int

    @property
    def has_free_places(self) -> bool:
        """Есть ли у врача свободные места или талоны"""
        return self.free_participant_count > 0 or self.free_ticket_count > 0


@dataclass
class Appointment:
    """Свободный талон к врачу"""

    id: str
    visit_start: datetime
    visit_end: datetime | None = None
    room: str = ""
    number: str = ""
    address: str = ""


@dataclass
class PatientAppointment:
    """Запись пациента, уже сделанная в медучреждении"""

    id: str
    visit_start: datetime
    doctor_id: str
    doctor_name: str
    specialty_id: str


@dataclass
class Patient:
    """Пациент, которого записываем: ищется в базе медучреждения по ФИО и дате рождения"""

    last_name: str
    first_name: str
    middle_name: str
    birthdate: date

    @property
    def birthdate_param(self) -> str:
        """Дата рождения в формате api: 1991-11-11T00:00:00"""
        return self.birthdate.strftime("%Y-%m-%dT00:00:00")


def _parse_datetime(value: Any) -> datetime | None:
    """Разбирает дату из ответа api, отдает None если её нет или она кривая"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _request(
    path: str,
    delay_secs: float = REQUEST_DELAY_SECS,
    body: dict | None = None,
) -> Any:
    """
    Запрашивает api горздрава и возвращает содержимое поля `result`
    Args:
        path: str: путь относительно API_URL
        delay_secs: float: пауза перед запросом
        body: dict | None: тело POST запроса, без него запрос GET
    Returns:
        Any: содержимое поля result
    Raises:
        GorzdravError: если запрос не удался или api ответил success=false
    """
    url = f"{API_URL}{path}"
    if delay_secs > 0:
        time.sleep(delay_secs)

    if body is None:
        request = urllib.request.Request(url=url, headers=HEADERS)
    else:
        request = urllib.request.Request(
            url=url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={**HEADERS, "Content-Type": "application/json"},
            method="POST",
        )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise GorzdravError(f"http ошибка {error.code}", url=url) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise GorzdravError(f"сеть недоступна: {error}", url=url) from error
    except json.JSONDecodeError as error:
        raise GorzdravError("api вернул не json", url=url) from error

    if not payload.get("success"):
        raise GorzdravError(
            message=payload.get("message") or "неизвестная ошибка api",
            error_code=payload.get("errorCode"),
            url=url,
        )
    return payload.get("result")


def _request_list(path: str) -> list[dict]:
    """
    То же, что _request, но пустой список вместо ошибок вида
    "нет врачей" / "нет талонов" - это нормальное состояние, а не поломка
    """
    try:
        result = _request(path)
    except GorzdravError as error:
        if error.error_code in EMPTY_RESULT_ERROR_CODES:
            return []
        raise
    return result or []


def get_districts() -> list[District]:
    """Список районов города"""
    return [
        District(id=str(item["id"]), name=item.get("name") or "")
        for item in _request_list("/shared/districts")
    ]


def get_lpus(district_id: str | None = None) -> list[Lpu]:
    """Список медучреждений: во всём городе или в одном районе"""
    path = "/shared/lpus" if district_id is None else f"/shared/district/{district_id}/lpus"
    return [
        Lpu(
            id=int(item["id"]),
            full_name=item.get("lpuFullName") or "",
            address=item.get("address") or "",
        )
        for item in _request_list(path)
    ]


def get_lpu(lpu_id: int) -> Lpu:
    """Информация об одном медучреждении"""
    item = _request(f"/shared/lpu/{lpu_id}")
    return Lpu(
        id=int(item["id"]),
        full_name=item.get("lpuFullName") or "",
        address=item.get("address") or "",
    )


def get_specialties(lpu_id: int) -> list[Specialty]:
    """Список специальностей в медучреждении"""
    return [
        Specialty(
            id=str(item["id"]),
            name=item.get("name") or "",
            free_participant_count=item.get("countFreeParticipant") or 0,
            free_ticket_count=item.get("countFreeTicket") or 0,
        )
        for item in _request_list(f"/schedule/lpu/{lpu_id}/specialties")
    ]


def get_doctors(lpu_id: int, specialty_id: str) -> list[Doctor]:
    """Список врачей в медучреждении по специальности"""
    return [
        Doctor(
            id=str(item["id"]),
            name=item.get("name") or "",
            free_participant_count=item.get("freeParticipantCount") or 0,
            free_ticket_count=item.get("freeTicketCount") or 0,
        )
        for item in _request_list(
            f"/schedule/lpu/{lpu_id}/speciality/{specialty_id}/doctors"
        )
    ]


def get_appointments(lpu_id: int, doctor_id: str) -> list[Appointment]:
    """Свободные талоны к врачу"""
    appointments: list[Appointment] = []
    for item in _request_list(f"/schedule/lpu/{lpu_id}/doctor/{doctor_id}/appointments"):
        visit_start = _parse_datetime(item.get("visitStart"))
        if visit_start is None:
            continue
        appointments.append(
            Appointment(
                id=str(item.get("id") or ""),
                visit_start=visit_start,
                visit_end=_parse_datetime(item.get("visitEnd")),
                room=str(item.get("room") or ""),
                number=str(item.get("number") or ""),
                address=str(item.get("address") or ""),
            )
        )
    return appointments


def find_patient(lpu_id: int, patient: Patient) -> str:
    """
    Ищет пациента в медучреждении
    Returns:
        str: patientId для записи
    Raises:
        GorzdravError: пациент не найден (например, не прикреплён к этому лпу)
    """
    query = urlencode(
        {
            "lpuId": lpu_id,
            "lastName": patient.last_name,
            "firstName": patient.first_name,
            "middleName": patient.middle_name,
            "birthdate": patient.birthdate_param,
            "birthdateValue": patient.birthdate.strftime("%d.%m.%Y"),
        }
    )
    patient_id = _request(f"/patient/search?{query}")
    if not patient_id:
        raise GorzdravError("пациент не найден", url=f"{API_URL}/patient/search")
    return str(patient_id)


def create_appointment(
    lpu_id: int,
    patient_id: str,
    patient: Patient,
    appointment: Appointment,
    recipient_email: str = "",
) -> None:
    """
    Записывает пациента на талон
    Raises:
        GorzdravError: запись не удалась (талон уже заняли, пациент уже записан и т.п.)
    """
    _request(
        "/appointment/create",
        body={
            "esiaId": None,
            "lpuId": lpu_id,
            "patientId": patient_id,
            "appointmentId": appointment.id,
            "referralId": None,
            "ipmpiCardId": None,
            "recipientEmail": recipient_email or None,
            "patientLastName": patient.last_name,
            "patientFirstName": patient.first_name,
            "patientMiddleName": patient.middle_name,
            "patientBirthdate": patient.birthdate_param,
            "room": appointment.room,
            "num": appointment.number,
            "address": appointment.address,
        },
    )


def generate_link(
    district_id: str,
    lpu_id: int,
    specialty_id: str,
    doctor_id: str,
) -> str:
    """Ссылка на запись к врачу на сайте горздрава"""
    return (
        "https://gorzdrav.spb.ru/service-free-schedule#"
        + f"%5B%7B%22district%22:%22{district_id}%22%7D,"
        + f"%7B%22lpu%22:%22{lpu_id}%22%7D,"
        + f"%7B%22speciality%22:%22{specialty_id}%22%7D,"
        + f"%7B%22schedule%22:%22{doctor_id}%22%7D,"
        + f"%7B%22doctor%22:%22{doctor_id}%22%7D%5D"
    )


def parse_link(url: str) -> dict[str, str]:
    """
    Достает district / lpu / speciality / doctor из ссылки горздрава.
    Врач в ссылке не обязателен - следить можно за всей специальностью.
    Args:
        url: str: ссылка с сайта горздрава
    Returns:
        dict[str, str]: найденные идентификаторы
    """
    unquoted = unquote(string=url, encoding="utf-8")
    ids: dict[str, str] = {}
    for key in ("district", "lpu", "speciality", "schedule", "doctor"):
        found = re.search(rf'"{key}"\s*:\s*"([^"]+)"', unquoted)
        if found:
            ids[key] = found.group(1)
    return ids


def get_patient_appointments(lpu_id: int, patient_id: str) -> list[PatientAppointment]:
    """Текущие записи пациента в медучреждении"""
    query = urlencode({"lpuId": lpu_id, "patientId": patient_id})
    appointments: list[PatientAppointment] = []
    for item in _request_list(f"/appointments?{query}"):
        visit_start = _parse_datetime(item.get("visitStart"))
        if visit_start is None:
            continue
        doctor = item.get("doctorRendingConsultation") or {}
        specialty = item.get("specialityRendingConsultation") or {}
        appointments.append(
            PatientAppointment(
                id=str(item.get("appointmentId") or ""),
                visit_start=visit_start,
                doctor_id=str(doctor.get("id") or ""),
                doctor_name=doctor.get("name") or "",
                specialty_id=str(specialty.get("id") or ""),
            )
        )
    return appointments


def cancel_appointment(lpu_id: int, patient_id: str, appointment_id: str) -> None:
    """
    Отменяет запись
    Raises:
        GorzdravError: отменить не получилось
    """
    _request(
        "/appointment/cancel",
        body={
            "appointmentId": appointment_id,
            "lpuId": lpu_id,
            "patientId": patient_id,
            "esiaId": None,
        },
    )
