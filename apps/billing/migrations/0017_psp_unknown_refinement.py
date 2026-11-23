"""A definite lookup can refine an unknown observation at the same sequence."""

from typing import ClassVar

from django.db import migrations

from apps.billing.migrations._psp_observer_sql import GUARD, SQL


def _refine(definition: str, *, prior: str, current: str) -> str:
    """Keep later versions as fences; only a concrete equal-version fact conflicts."""
    old = f"({prior}.version={current}.version AND"
    if definition.count(old) != 1:
        msg = "unexpected frozen PSP version fence"
        raise ValueError(msg)
    return definition.replace(old, f"{old} {prior}.status<>'unknown' AND")


_start = SQL.index("FUNCTION clinic_app.billing_psp_observer_finish(")
_end = SQL.index("END $f$;", _start) + len("END $f$;")
_finish = "CREATE OR REPLACE " + SQL[_start:_end]


class Migration(migrations.Migration):
    """Install exactly the two revised predicates without rewriting a money fact."""

    dependencies: ClassVar = [("billing", "0016_psp_observer")]
    operations: ClassVar = [
        migrations.RunSQL(
            "SET LOCAL ROLE clinic_resolver;\n"
            + _refine(GUARD, prior="e", current="NEW")
            + _refine(_finish, prior="n", current="e")
            + "\nRESET ROLE;"
        )
    ]
