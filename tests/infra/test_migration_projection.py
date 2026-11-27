"""Keep pg_dump role ordering out of the logical schema comparison."""

from apps.tenancy.migration_preflight import _normalize

HEADER = (
    b"--\n-- Name: patient selected; Type: POLICY; Schema: clinic_app;"
    b" Owner: clinic_owner\n--\n\n"
)
POLICY = b'CREATE POLICY "selected" ON "clinic_app"."patient" AS RESTRICTIVE TO '
TRAILER = b"\n\n--\n-- PostgreSQL database dump complete\n--\n"


def test_policy_role_order_is_ignored_but_actual_drift_is_detected() -> None:
    def dump(roles: bytes, expression: bytes) -> bytes:
        return _normalize(
            HEADER + POLICY + roles + b" USING (" + expression + b");" + TRAILER
        )

    roles = b'"clinic_owner", "clinic_app", "role ""with"", a comma"'
    reordered = b'"role ""with"", a comma", "clinic_app", "clinic_owner"'
    assert dump(roles, b"true") == dump(reordered, b"true")
    assert dump(roles, b"true") != dump(b'"clinic_app"', b"true")
    assert dump(roles, b"true") != dump(reordered, b"false")
