from importlib import import_module

import pytest
from django.db import connection


@pytest.mark.django_db(transaction=True)
def test_appointment_migration_catalog_is_immediate_versioned_and_indexed() -> None:
    migration = import_module("apps.scheduling.migrations.0002_appointment")
    assert migration.Migration.dependencies == [
        ("audit", "0005_clinic_metadata_v2"),
        ("identity", "0005_clinic_timezone"),
        ("intake", "0001_patient_and_enrollment"),
        ("scheduling", "0001_availability_block"),
    ]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname FROM pg_catalog.pg_indexes "
            "WHERE schemaname='clinic_app' AND tablename='scheduling_appointment' "
            "AND indexname = ANY(%s) ORDER BY indexname",
            [
                [
                    "scheduling_appointment_clinic_start_idx",
                    "scheduling_appointment_patient_start_idx",
                    "scheduling_appointment_practitioner_start_idx",
                    "scheduling_appointment_status_start_idx",
                ]
            ],
        )
        assert cursor.fetchall() == [
            ("scheduling_appointment_clinic_start_idx",),
            ("scheduling_appointment_patient_start_idx",),
            ("scheduling_appointment_practitioner_start_idx",),
            ("scheduling_appointment_status_start_idx",),
        ]
        cursor.execute(
            "SELECT conname, contype, condeferrable, condeferred, "
            "pg_catalog.pg_get_constraintdef(oid) "
            "FROM pg_catalog.pg_constraint WHERE conrelid="
            "'clinic_app.scheduling_appointment'::regclass "
            "AND conname = ANY(%s) ORDER BY conname",
            [
                [
                    "scheduling_appointment_enrollment_fk",
                    "scheduling_appointment_fingerprint_32_check",
                    "scheduling_appointment_lifecycle_check",
                    "scheduling_appointment_org_clinic_fk",
                    "scheduling_appointment_positive_minute_range_check",
                    "scheduling_appointment_scheduled_patient_excl",
                    "scheduling_appointment_scheduled_practitioner_excl",
                ]
            ],
        )
        constraints = cursor.fetchall()
        assert [row[0] for row in constraints] == sorted(row[0] for row in constraints)
        assert {row[1] for row in constraints} == {"c", "f", "x"}
        assert all(row[2:4] == (False, False) for row in constraints)
        exclusions = [row[4] for row in constraints if row[1] == "x"]
        assert len(exclusions) == 2
        assert all(
            "tstzrange(start_at, end_at, '[)'::text) WITH &&" in row
            for row in exclusions
        )
        assert all(
            "WHERE (((status)::text = 'scheduled'::text))" in row for row in exclusions
        )
        cursor.execute(
            "SELECT tgname, tgenabled FROM pg_catalog.pg_trigger "
            "WHERE tgrelid='clinic_app.scheduling_appointment'::regclass "
            "AND NOT tgisinternal ORDER BY tgname"
        )
        assert cursor.fetchall() == [
            ("scheduling_appointment_guard", "O"),
            ("scheduling_appointment_no_delete", "O"),
        ]
        cursor.execute(
            "SELECT proname, proowner::regrole::text, "
            "has_function_privilege('clinic_app', oid, 'EXECUTE'), "
            "has_function_privilege('clinic_resolver', oid, 'EXECUTE') "
            "FROM pg_catalog.pg_proc WHERE pronamespace='clinic_app'::regnamespace "
            "AND proname = ANY(%s) ORDER BY proname",
            [
                [
                    "scheduling_appointment_guard_v1",
                    "scheduling_appointment_reject_delete_v1",
                ]
            ],
        )
        assert cursor.fetchall() == [
            ("scheduling_appointment_guard_v1", "clinic_owner", False, False),
            (
                "scheduling_appointment_reject_delete_v1",
                "clinic_owner",
                False,
                False,
            ),
        ]
