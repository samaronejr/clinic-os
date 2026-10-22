"""Plant unsupported constructs outside scheduling; the repository guard must fail."""

import ast
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from scheduling.clock_boundary import (
    application_boundary,
    repository_event_trigger_boundary,
)
from scheduling.clock_residual_probes import EVENTS
from scheduling.clock_source import _contexts, source_fragments

APPS = Path(__file__).resolve().parents[2] / "apps"


@pytest.mark.parametrize(
    ("other", "violations"),
    [
        ('"SELECT 2"', []),
        ('"PREPARE hidden AS SELECT now()"', ["server-prepare"]),
        ("unknown_query", ["unresolved-statement"]),
    ],
)
def test_conditional_query_checks_every_possible_branch(
    tmp_path: Path, other: str, violations: list[str]
) -> None:
    (tmp_path / "query.py").write_text(
        f'cursor.execute("SELECT 1" if selected else {other})'
    )
    assert application_boundary(tmp_path) == (
        {"query.py": violations} if violations else {}
    )


@pytest.mark.parametrize(
    ("source", "violations"),
    [
        (
            'flagged = any(fragment in folded for fragment in ("execute shell",))',
            [],
        ),
        ('cursor.execute("EXECUTE shell")', ["dynamic-or-prepared-execute"]),
        (
            'flagged = any(fragment in folded for fragment in ("EXECUTE shell",) '
            "if cursor.execute(fragment))",
            ["dynamic-or-prepared-execute", "unresolved-statement"],
        ),
    ],
)
def test_membership_data_does_not_hide_executable_sql(
    tmp_path: Path, source: str, violations: list[str]
) -> None:
    (tmp_path / "query.py").write_text(source)
    assert application_boundary(tmp_path) == (
        {"query.py": violations} if violations else {}
    )


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


pytestmark = [
    pytest.mark.django_db(transaction=True, available_apps=[]),
    pytest.mark.usefixtures("clock_catalog_session"),
]


def test_template_contexts_do_not_rewalk_the_module_per_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = ast.parse(
        "\n".join(
            f'def query_{index}(value):\n    return f"SELECT {{value}}"\n'
            f'query_{index}("{index}")'
            for index in range(20)
        )
    )
    original = ast.walk
    whole_module_walks = []

    def walk(node: ast.AST) -> Iterator[ast.AST]:
        if node is tree:
            whole_module_walks.append(node)
        return original(node)

    monkeypatch.setattr(ast, "walk", walk)
    assert {f"SELECT {index}" for index in range(20)} <= set(source_fragments(tree))
    assert len(whole_module_walks) <= 5


def test_application_sql_stays_inside_clock_census_boundary() -> None:
    assert application_boundary(APPS) == {}


def test_repository_refuses_event_trigger_ddl() -> None:
    reviewed = json.loads(
        Path(__file__).with_name("clock_source_inventory.json").read_text()
    )
    assert all(
        path.startswith("tests/") and entry["reason"]
        for path, entry in reviewed.items()
    )
    assert repository_event_trigger_boundary(APPS.parent) == {
        path: entry["findings"] for path, entry in reviewed.items()
    }


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
message = "Invalid text, purpose or language."
label = f"{label} ({minutes} min)"
for setting, value in (("app.current_tenant", tenant), ("app.current_user_id", user)):
    cursor.execute("SELECT set_config(%s, %s, true)", [setting, value])
"""
    (tmp_path / "safe.py").write_text(source)
    assert application_boundary(tmp_path) == {}


@pytest.mark.parametrize("statement", ["SELECT 1", "PREPARE hidden AS SELECT now()"])
def test_finite_queries_follow_nested_migration_factories(
    tmp_path: Path, statement: str
) -> None:
    source = f"""
SCRIPTS = {{'install': {statement!r}}}
QUERY = SCRIPTS['install']
def sql_step(query):
    def run():
        cursor.execute(query)
    return run
def step(query):
    return sql_step(query)
operations = [step(QUERY)]
"""
    (tmp_path / "migration.py").write_text(source)
    assert application_boundary(tmp_path) == (
        {} if statement == "SELECT 1" else {"migration.py": ["server-prepare"]}
    )


def test_finite_forwarding_does_not_duplicate_identical_contexts() -> None:
    functions = ["def leaf(query):\n    cursor.execute(query)"]
    previous = "leaf"
    for index in range(8):
        name = f"hop_{index}"
        functions.append(
            f"def {name}(query):\n    {previous}(query)\n    {previous}(query)"
        )
        previous = name
    tree = ast.parse("\n".join([*functions, f'{previous}("SELECT 1")']))
    contexts = _contexts(tree, ())
    assert all(len(values) == 1 for values in contexts.values())
    assert "SELECT 1" in source_fragments(tree)


@pytest.mark.parametrize("statement", ["SELECT 1", "PREPARE hidden AS SELECT now()"])
def test_finite_comprehension_queries_reach_migration_factories(
    tmp_path: Path, statement: str
) -> None:
    source = f"""
TABLES = ("first", "second")
STATEMENT = {statement!r}
def build(table):
    return STATEMENT + " FROM " + table
def sql_step(query):
    def run():
        cursor.execute(query)
    return run
operations = [sql_step(build(table)) for table in TABLES]
"""
    (tmp_path / "migration.py").write_text(source)
    assert application_boundary(tmp_path) == (
        {} if statement == "SELECT 1" else {"migration.py": ["server-prepare"]}
    )


def test_non_sql_type_alias_is_not_a_finite_value(tmp_path: Path) -> None:
    (tmp_path / "typed.py").write_text(
        "from datetime import date, datetime\n"
        "Temporal = date | datetime\n"
        'SQL = "SELECT 1"\n'
    )
    assert application_boundary(tmp_path) == {}


@pytest.mark.parametrize("query", ['"SELECT 1"', "BROKEN"])
def test_unsupported_fixture_value_refuses_only_sql_use(
    tmp_path: Path, query: str
) -> None:
    (tmp_path / "fixture.py").write_text(
        "from django.db import migrations\n"
        'BROKEN = {{"metadata": "fixture"}}\n'
        f"class Migration:\n operations = [migrations.RunSQL({query})]\n"
    )
    assert application_boundary(tmp_path) == (
        {} if query == '"SELECT 1"' else {"fixture.py": ["unresolved-runsql"]}
    )
