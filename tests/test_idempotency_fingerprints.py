from importlib import import_module
from importlib.util import find_spec

import pytest

CLINIC_ID = "11111111-1111-4111-8111-111111111111"
PRACTITIONER_ID = "22222222-2222-4222-8222-222222222222"
ENROLLMENT_ID = "33333333-3333-4333-8333-333333333333"
PATIENT_ID = "44444444-4444-4444-8444-444444444444"


def test_version_one_create_payloads_have_exact_bytes_and_hashes() -> None:
    module = (
        import_module("apps.core.idempotency")
        if find_spec("apps.core.idempotency")
        else None
    )

    assert module is not None
    patient = {
        "birth_date": "2000-01-02",
        "clinic_id": CLINIC_ID,
        "full_name": "Ana Synthetic",
    }
    availability = {
        "clinic_id": CLINIC_ID,
        "end_utc": "2030-01-02T15:00:00Z",
        "practitioner_id": PRACTITIONER_ID,
        "start_utc": "2030-01-02T14:00:00Z",
    }
    appointment = {
        "clinic_id": CLINIC_ID,
        "end_utc": "2030-01-02T15:00:00Z",
        "enrollment_id": ENROLLMENT_ID,
        "practitioner_id": PRACTITIONER_ID,
        "start_utc": "2030-01-02T14:00:00Z",
    }
    invoice = {
        "amount_minor": "18000",
        "clinic_id": CLINIC_ID,
        "currency": "BRL",
        "patient_id": PATIENT_ID,
    }

    assert module.canonical_create_payload("patient", patient) == (
        b'{"birth_date":"2000-01-02","clinic_id":"11111111-1111-4111-8111-'
        b'111111111111","full_name":"Ana Synthetic"}'
    )
    assert module.canonical_create_payload("availability", availability) == (
        b'{"clinic_id":"11111111-1111-4111-8111-111111111111","end_utc":"2030-'
        b'01-02T15:00:00Z","practitioner_id":"22222222-2222-4222-8222-222222222'
        b'222","start_utc":"2030-01-02T14:00:00Z"}'
    )
    assert module.canonical_create_payload("appointment", appointment) == (
        b'{"clinic_id":"11111111-1111-4111-8111-111111111111","end_utc":"2030-'
        b'01-02T15:00:00Z","enrollment_id":"33333333-3333-4333-8333-333333333'
        b'333","practitioner_id":"22222222-2222-4222-8222-222222222222","start_'
        b'utc":"2030-01-02T14:00:00Z"}'
    )
    assert module.create_fingerprint("patient", patient).hex() == (
        "20c1c89834478ea52142ffc8c8b5c798b06e9409ba455ebe76918ada8b288fe1"
    )
    assert module.create_fingerprint("availability", availability).hex() == (
        "42f0fcc492a1e0217c7cbe8760e3f66e0bc607f9ac40a70724eda1cd248f7714"
    )
    assert module.canonical_create_payload("invoice", invoice) == (
        b'{"amount_minor":"18000","clinic_id":"11111111-1111-4111-8111-1111111'
        b'11111","currency":"BRL","patient_id":"44444444-4444-4444-8444-4444444'
        b'44444"}'
    )
    assert module.create_fingerprint("appointment", appointment).hex() == (
        "200cb35971d1834752cd504547d8e00e287e953ef526942ad851190348c17ed8"
    )
    assert module.create_fingerprint("invoice", invoice).hex() == (
        "ed91d69045830d26a5f8053d6f2c4bd781bb6e115cb428fbfda65339c782dfa8"
    )

    invalid_values = (
        ("patient", patient | {"clinic_id": CLINIC_ID.replace("-", "")}),
        ("patient", patient | {"full_name": "A\N{COMBINING ACUTE ACCENT}na"}),
        ("patient", patient | {"clinic": CLINIC_ID}),
        ("availability", availability | {"start_utc": "2030-01-02T14:00:01Z"}),
        ("appointment", appointment | {"end_utc": "2030-01-02T15:00:00.000Z"}),
        ("invoice", invoice | {"amount_minor": "018000"}),
        ("invoice", invoice | {"amount_minor": "0"}),
        ("invoice", invoice | {"currency": "brl"}),
    )
    for kind, values in invalid_values:
        with pytest.raises(module.IdempotencyValueError):
            module.create_fingerprint(kind, values)
