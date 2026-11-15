"""Helpers of J5's connected result chain (todo 75 ``journey-results``)."""

from __future__ import annotations

import psycopg


def chain_identity(staff: dict[str, str], enrollment: str) -> tuple[str, str, int]:
    """Return the exact order, matched version and acknowledgment count.

    Read as the ordering physician inside the clinic's tenant scope, never as
    the owner, so the counts are what the runtime authorizes for that person.
    """
    with psycopg.connect(staff["dsn"]) as connection:
        connection.execute("SET ROLE clinic_app")
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true),"
            " set_config('app.current_user_id', %s, true)",
            [staff["organization"], staff["physician_a_id"]],
        )
        rows = connection.execute(
            "SELECT o.id::text,v.id::text,"
            " (SELECT count(*) FROM clinic_app.orders_resultacknowledgment a"
            "  WHERE a.version_id=v.id)"
            " FROM clinic_app.orders_order o"
            " JOIN clinic_app.orders_resultversion v ON v.proposed_order_id=o.id"
            " JOIN clinic_app.orders_resultmatch m ON m.version_id=v.id"
            "  AND m.order_id=o.id"
            " WHERE o.enrollment_id=%s AND o.clinic_id=%s AND o.ordered_by_id=%s",
            [enrollment, staff["clinic_a"], staff["physician_a_id"]],
        ).fetchall()
    assert len(rows) == 1, "the chain must retain exactly one matched source version"
    order, version, acknowledged = rows[0]
    return str(order), str(version), int(acknowledged)
