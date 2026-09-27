"""Plant unsupported constructs outside scheduling; the repository guard must fail."""

from pathlib import Path

import pytest

from scheduling.clock_boundary import (
    application_boundary,
    repository_event_trigger_boundary,
)
from scheduling.clock_residual_probes import EVENTS

APPS = Path(__file__).resolve().parents[2] / "apps"
PROBES = {
    "event-trigger-exact": (
        "SQL = " + repr(EVENTS["event-exact"]),
        "event-trigger-ddl",
    ),
    "event-trigger-variant": (
        "SQL = " + repr(EVENTS["event-variant-drop"]),
        "event-trigger-ddl",
    ),
    "dynamic-execute": (
        'SQL = "DO $$ BEGIN EXECUTE query_text; END $$"',
        "dynamic-or-prepared-execute",
    ),
    "quoted-dynamic-execute": (
        '''SQL = "CREATE FUNCTION public.probe() RETURNS void LANGUAGE plpgsql "
SQL += "AS 'BEGIN EXECUTE query_text; END'"''',
        "dynamic-or-prepared-execute",
    ),
    "prepared-statement": (
        'SQL = "PREPARE hidden AS SELECT clock_timestamp()"',
        "server-prepare",
    ),
    "split-prepared-statement": (
        'SQL = "PRE" + "PARE hidden AS SELECT clock_timestamp()"',
        "server-prepare",
    ),
    "execute-prepared": (
        'SQL = "EXECUTE hidden"',
        "dynamic-or-prepared-execute",
    ),
    "temporary-table": (
        'SQL = "CREATE /* scope */ TEMP TABLE hidden (at timestamptz DEFAULT now())"',
        "temporary-ddl",
    ),
    "temporary-reference": (
        """SQL = 'SELECT "pg_temp" /* scope */ . hidden()' """,
        "temporary-reference",
    ),
    "temporary-search-path": (
        'SQL = "SET search_path=pg_temp; '
        'CREATE TABLE hidden (at timestamptz DEFAULT now())"',
        "temporary-search-path",
    ),
    "mutable-search-path": (
        'cursor.execute("SELECT set_config(%s, %s, true)", '
        '["search_path", chosen_path])',
        "mutable-search-path",
    ),
    "reader-operator": (
        'SQL = "CREATE OPERATOR public.#@ (LEFTARG=timestamptz, RIGHTARG=interval, " '
        '"FUNCTION=pg_catalog.timestamptz_pl_interval)"',
        "operator-ddl",
    ),
    "unresolved-setting": (
        '''SQL = "SELECT current_setting('app.' || setting_suffix)"''',
        "unresolvable-setting-name",
    ),
    "bound-unresolved-setting": (
        """cursor.execute("SELECT current_setting(%s)", [setting_name])""",
        "unresolvable-setting-name",
    ),
    "dynamic-setting": (
        '''SQL = f"SELECT current_setting('{setting_name}')"''',
        "unresolvable-setting-name",
    ),
    "native-language": (
        '''SQL = "CREATE FUNCTION public.hidden() RETURNS timestamptz "
SQL += "LANGUAGE internal AS 'now'"''',
        "unsupported-language",
    ),
    "dynamic-callee": (
        'SQL = f"SELECT {function_name}()"',
        "dynamic-callee",
    ),
    "dynamic-keyword": (
        'SQL = "CREATE {} hidden()".format(object_kind)',
        "dynamic-keyword",
    ),
    "runtime-code": ("eval(source_text)", "runtime-code-generation"),
    "unresolved-statement": ("cursor.execute(query_text)", "unresolved-statement"),
    "computed-statement": ('cursor.execute("".join(parts))', "unresolved-statement"),
    "split-variable-statement": (
        'first = "PRE"\nlast = "PARE hidden AS SELECT now()"\n'
        "cursor.execute(first + last)",
        "server-prepare",
    ),
    "dynamic-statement-prefix": (
        'cursor.execute(prefix + " statement")',
        "unresolved-statement",
    ),
}


def test_application_sql_stays_inside_clock_census_boundary() -> None:
    assert application_boundary(APPS) == {}


def test_repository_refuses_event_trigger_ddl() -> None:
    assert repository_event_trigger_boundary(APPS.parent) == {}


@pytest.mark.parametrize(
    "relative",
    [
        "apps/unlisted/migrations/event.py",
        "ops/db/event.sql",
        "config/event.py",
        "hooks/event.sh",
        "entry.py",
        ".github/workflows/event.yml",
    ],
)
def test_event_trigger_boundary_covers_repo_sources(
    tmp_path: Path, relative: str
) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    source = EVENTS["event-variant-drop"]
    path.write_text("SQL = " + repr(source) if path.suffix == ".py" else source)
    assert repository_event_trigger_boundary(tmp_path) == {
        relative: ["event-trigger-ddl"]
    }


@pytest.mark.parametrize("name", PROBES)
def test_repository_guard_rejects_each_planted_construct(
    tmp_path: Path, name: str
) -> None:
    apps = tmp_path / "apps"
    planted = apps / "outside_scheduling" / "migrations" / "_probe.py"
    planted.parent.mkdir(parents=True)
    assert application_boundary(apps) == {}
    source, reason = PROBES[name]
    planted.write_text(source + "\n")
    assert reason in application_boundary(apps)[str(planted.relative_to(apps))]


def test_boundary_accepts_static_settings_and_supported_ddl(tmp_path: Path) -> None:
    source = """
SQL = "CREATE FUNCTION clinic_app.f() RETURNS boolean LANGUAGE sql "
SQL += "AS $$ SELECT current_setting('app.current_tenant', true) IS NOT NULL $$;"
SQL += "GRANT EXECUTE ON FUNCTION clinic_app.f() TO clinic_app;"
SQL += "CREATE TRIGGER t BEFORE INSERT ON tab FOR EACH ROW EXECUTE FUNCTION f();"
MODE = "prepare"
label = f"{label} ({minutes} min)"
for setting, value in (("app.current_tenant", tenant), ("app.current_user_id", user)):
    cursor.execute("SELECT set_config(%s, %s, true)", [setting, value])
"""
    (tmp_path / "safe.py").write_text(source)
    assert application_boundary(tmp_path) == {}
