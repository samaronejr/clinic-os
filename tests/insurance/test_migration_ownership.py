from __future__ import annotations

import pytest
import sqlparse
from apps.insurance.migrations._membership_sql import INTAKE_SQL
from apps.insurance.migrations._scheduling_sql import SQL
from django.db import connection

EXPECTED_ACLS = {
    "intake_membership_payer_guard()": "{clinic_resolver=X/clinic_resolver}",
    "insurance_link_registered_payer(uuid,uuid,uuid,text)": (
        "{clinic_resolver=X/clinic_resolver,clinic_app=X/clinic_resolver}"
    ),
    "insurance_booking_coverage(uuid,uuid,uuid,uuid)": (
        "{clinic_resolver=X/clinic_resolver,clinic_app=X/clinic_resolver}"
    ),
    "insurance_registration_guard()": "{clinic_resolver=X/clinic_resolver}",
    "insurance_payer_check_applied()": "{clinic_resolver=X/clinic_resolver}",
    "insurance_appointment_guard()": "{clinic_resolver=X/clinic_resolver}",
    "scheduling_lifecycle_guard()": "{clinic_resolver=X/clinic_resolver}",
    "scheduling_capacity_guard()": "{clinic_resolver=X/clinic_resolver}",
}


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        (
            INTAKE_SQL,
            {"intake_membership_payer_guard", "insurance_link_registered_payer"},
        ),
        (
            SQL,
            {
                "insurance_booking_coverage",
                "insurance_registration_guard",
                "insurance_payer_check_applied",
                "insurance_appointment_guard",
                "scheduling_lifecycle_guard",
                "scheduling_capacity_guard",
            },
        ),
    ],
    ids=["intake", "scheduling"],
)
def test_function_replacement_and_acl_ddl_use_the_resolver_owner(
    definition: str, expected: set[str]
) -> None:
    role = "clinic_owner"
    replaced = set()
    for statement in sqlparse.split(definition):
        if statement == "SET LOCAL ROLE clinic_resolver;":
            role = "clinic_resolver"
        elif statement == "RESET ROLE;":
            role = "clinic_owner"
        elif statement.startswith("CREATE OR REPLACE FUNCTION clinic_app."):
            name = statement.partition("(")[0].rsplit(".", 1)[1]
            assert role == "clinic_resolver", name
            replaced.add(name)
        elif statement.startswith(("GRANT ", "REVOKE ")):
            if "ON FUNCTION " in statement:
                assert role == "clinic_resolver", statement
            else:
                assert role == "clinic_owner", statement
        elif statement.startswith(
            ("ALTER TABLE ", "CREATE INDEX ", "CREATE TRIGGER ", "DROP TRIGGER ")
        ):
            assert role == "clinic_owner", statement
    assert replaced == expected
    assert role == "clinic_owner"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("installation", ["installed", "reapplied"])
def test_exact_resolver_owner_and_acl_for_every_integration_function(
    installation: str,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("clinic_owner",)
        if installation == "reapplied":
            cursor.execute(INTAKE_SQL)
            cursor.execute(SQL)
            cursor.execute("SELECT current_user")
            assert cursor.fetchone() == ("clinic_owner",)
        for signature, acl in EXPECTED_ACLS.items():
            cursor.execute(
                "SELECT pg_get_userbyid(proowner),proacl::text "
                "FROM pg_catalog.pg_proc WHERE oid=%s::regprocedure",
                ["clinic_app." + signature],
            )
            assert cursor.fetchone() == ("clinic_resolver", acl), signature
