"""Frozen SQL for the rnds exchange purpose migration."""


def delegate_check(purposes: tuple[str, ...]) -> str:
    names = ",".join(f"'{purpose}'" for purpose in ("care_processing", *purposes))
    return (
        "ALTER TABLE clinic_app.identity_patientdelegategrant\n"
        " DROP CONSTRAINT delegate_purposes_check,\n"
        " ADD CONSTRAINT delegate_purposes_check CHECK (\n"
        f"  coalesce(cardinality(purposes) BETWEEN 1 AND {len(purposes) + 1}\n"
        "  AND array_position(purposes,NULL) IS NULL\n"
        f"  AND purposes::text[] <@ ARRAY[{names}],false));\n"
    )
