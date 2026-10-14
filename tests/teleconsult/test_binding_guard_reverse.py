"""The v2 reverse restores the v1 binding guard byte for byte."""

from apps.teleconsult.migrations._teleconsult_sql import SQL as V1_SQL
from apps.teleconsult.migrations._teleconsult_v2_sql import BINDING_GUARD_V1


def test_reverse_binding_guard_is_the_v1_definition() -> None:
    start = V1_SQL.index("CREATE FUNCTION clinic_app.teleconsult_binding_guard()")
    end = V1_SQL.index("\n$f$;\n", start) + len("\n$f$;\n")
    restored = "CREATE OR REPLACE " + V1_SQL[start + len("CREATE ") : end]

    assert restored == BINDING_GUARD_V1
