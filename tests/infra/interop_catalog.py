"""Todo 67 pins derived from the migrated private PostgreSQL catalog."""

import json
from pathlib import Path
from typing import Final, cast

CATALOG: Final = cast(
    "dict[str, list[list[object]]]",
    json.loads((Path(__file__).parents[1] / "interop" / "posture.json").read_text()),
)
TABLE_GRANTS: Final = {
    (str(table), str(privilege))
    for grantee, table, privilege in CATALOG["table_grants"]
    if grantee == "clinic_resolver"
}
COLUMN_GRANTS: Final = {
    (str(table), str(column), str(privilege))
    for grantee, table, column, privilege in CATALOG["column_grants"]
    if grantee == "clinic_resolver"
}
RESULTS: Final = {
    (str(row[0]), str(row[1])): str(row[2]) for row in CATALOG["functions"]
}
POSTURE: Final = {
    str(row[0]): (str(row[3]), bool(row[8]), cast("list[str]", row[7]))
    for row in CATALOG["functions"]
}
INVOKERS: Final = {str(row[0]) for row in CATALOG["functions"] if not row[4]}
