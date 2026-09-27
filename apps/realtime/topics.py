"""Closed selector grammar; raw selectors never appear in broker messages."""

import re
from typing import Final

UUID_PATTERN: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CLINIC_PATTERN: Final = rf"clinic:({UUID_PATTERN}):(agenda|inbox|queue|messages)"
PATIENT_PATTERN: Final = (
    rf"patient:({UUID_PATTERN}):"
    r"(enrollment_view|questionnaires|booking|records|consent|teleconsult|billing)"
)
JOB_PATTERN: Final = r"ai_job:[A-Za-z0-9_-]{22,64}"
CLINIC_TOPIC: Final = re.compile(CLINIC_PATTERN)
PATIENT_TOPIC: Final = re.compile(PATIENT_PATTERN)
JOB_TOPIC: Final = re.compile(JOB_PATTERN)
TOPIC_PATTERN: Final = re.compile(
    rf"(?:{CLINIC_PATTERN}|{PATIENT_PATTERN}|{JOB_PATTERN}|"
    rf"authz:user:{UUID_PATTERN}|authz:halt)"
)
TOPIC_PERMISSIONS: Final = {
    "agenda": "appointment.read",
    "queue": "appointment.read",
}
