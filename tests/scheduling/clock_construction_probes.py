"""Round-6 exact escapes and construction-level variants (test-owned DDL)."""

from .clock_residual_probes import Probe

REMOVE_RLS = (
    "ALTER TABLE clinic_app.scheduling_resource DROP COLUMN r6_probe; "
    "DROP SCHEMA r6 CASCADE"
)
RLS = """
CREATE SCHEMA r6;
CREATE TABLE r6.hol (ends_at timestamptz);
INSERT INTO r6.hol VALUES ('2001-01-01'), ('2999-01-01');
ALTER TABLE r6.hol ENABLE ROW LEVEL SECURITY;
ALTER TABLE r6.hol FORCE ROW LEVEL SECURITY;
CREATE POLICY p ON r6.hol USING (ends_at > now());
CREATE FUNCTION r6.h() RETURNS timestamptz LANGUAGE plpgsql STABLE
AS $$ BEGIN RETURN (SELECT ends_at FROM r6.hol LIMIT 1); END $$;
ALTER TABLE clinic_app.scheduling_resource
ADD COLUMN r6_probe timestamptz DEFAULT r6.h();
GRANT USAGE ON SCHEMA r6 TO clinic_owner,clinic_app;
GRANT SELECT ON ALL TABLES IN SCHEMA r6 TO clinic_owner,clinic_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA r6 TO clinic_owner,clinic_app;
"""
RELATIONS = {
    "relation-exact-stable": RLS,
    "relation-exact-immutable": RLS.replace("STABLE", "IMMUTABLE"),
    "relation-variant-quoted": RLS.replace("r6.hol", 'r6."ClockRows"').replace(
        "ends_at", '"EndsAt"'
    ),
    "relation-variant-rowtype": RLS.replace(
        "BEGIN RETURN (SELECT ends_at FROM r6.hol LIMIT 1); END",
        "DECLARE chosen r6.hol%ROWTYPE; BEGIN "
        "SELECT source.* INTO chosen FROM r6.hol AS source LIMIT 1; "
        "RETURN chosen.ends_at; END",
    ),
}
PROBES = {
    name: Probe(
        "B3",
        source,
        REMOVE_RLS,
        "SELECT extract(year FROM r6.h())::integer",
        (2001,),
        (2999,),
    )
    for name, source in RELATIONS.items()
}
PROBES["relation-unresolved-identifier"] = Probe(
    "B3",
    "CREATE FUNCTION clinic_app.scheduling_r6_root() RETURNS integer "
    "LANGUAGE plpgsql AS $$ BEGIN IF false THEN PERFORM r6_unresolved_helper(); "
    "END IF; RETURN 42; END $$",
    "DROP FUNCTION clinic_app.scheduling_r6_root()",
    "SELECT clinic_app.scheduling_r6_root()",
    (42,),
)

TEXT_SETUP = """
CREATE SCHEMA r6;
CREATE DOMAIN r6.txt AS text;
CREATE DOMAIN r6.instant AS timestamptz;
CREATE FUNCTION r6.id_ts(timestamp) RETURNS timestamp LANGUAGE sql IMMUTABLE
AS $$ SELECT $1 $$;
GRANT USAGE ON SCHEMA r6 TO clinic_owner;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA r6 TO clinic_owner;
"""
TEXT = {
    "text-exact-domain": (
        "sql",
        "timestamp",
        "SELECT ('now'::r6.txt)::timestamp",
        "localtimestamp",
    ),
    "text-exact-chain": (
        "sql",
        "timestamp",
        "SELECT 'now'::text::timestamp",
        "localtimestamp",
    ),
    "text-exact-nested-cast": (
        "sql",
        "timestamp",
        "SELECT CAST(CAST('now' AS text) AS timestamp)",
        "localtimestamp",
    ),
    "text-exact-argument": (
        "sql",
        "timestamp",
        "SELECT r6.id_ts('now')",
        "localtimestamp",
    ),
    "text-exact-return": ("plpgsql", "timestamptz", "BEGIN RETURN 'now'; END", "now()"),
    "text-exact-domain-variable": (
        "plpgsql",
        "timestamptz",
        "DECLARE x r6.instant; BEGIN x := 'now'; RETURN x; END",
        "now()",
    ),
    "text-exact-percent-type": (
        "plpgsql",
        "timestamptz",
        "DECLARE x clinic_app.scheduling_availabilityblock.end_at%TYPE; "
        "BEGIN x := 'now'; RETURN x; END",
        "now()",
    ),
    "text-variant-whitespace-case": (
        "plpgsql",
        "timestamptz",
        "BEGIN RETURN '  NoW  '; END",
        "now()",
    ),
    "text-variant-dollar-value": (
        "plpgsql",
        "timestamptz",
        "BEGIN RETURN $value$  ToMoRrOw  $value$; END",
        "(current_date+1)::timestamptz",
    ),
    "text-variant-escape": (
        "sql",
        "timestamp",
        r"SELECT E'\156ow'::text::timestamp",
        "localtimestamp",
    ),
    "text-variant-unicode": (
        "sql",
        "timestamp",
        r"SELECT U&'\006eow'::text::timestamp",
        "localtimestamp",
    ),
    "text-variant-prefix-message": (
        "plpgsql",
        "integer",
        "BEGIN RAISE NOTICE '  NoWhere is a message'; RETURN 42; END",
        "42",
    ),
}
for name, (language, result, body, comparison) in TEXT.items():
    PROBES[name] = Probe(
        "B5-TEXT",
        TEXT_SETUP
        + f"CREATE FUNCTION clinic_app.scheduling_r6_root() RETURNS {result} "
        f"LANGUAGE {language} STABLE AS $body$ {body} $body$",
        "DROP FUNCTION clinic_app.scheduling_r6_root(); DROP SCHEMA r6 CASCADE",
        f"SELECT clinic_app.scheduling_r6_root() = {comparison}",
    )

EVENT = """
CREATE SCHEMA r6;
CREATE FUNCTION r6.evt() RETURNS event_trigger LANGUAGE plpgsql
AS $$ BEGIN PERFORM now(); END $$;
CREATE EVENT TRIGGER r6_evt ON ddl_command_end EXECUTE FUNCTION r6.evt();
ALTER EVENT TRIGGER r6_evt DISABLE;
"""
EVENTS = {
    "event-exact-enable": (EVENT, "ALTER EVENT TRIGGER r6_evt ENABLE", "O", ""),
    "event-variant-replica-drop": (
        EVENT.replace("ddl_command_end", "sql_drop"),
        "ALTER EVENT TRIGGER r6_evt ENABLE REPLICA",
        "R",
        "",
    ),
    "event-variant-extension": (
        EVENT + "ALTER EXTENSION pgcrypto ADD EVENT TRIGGER r6_evt;",
        "ALTER EVENT TRIGGER r6_evt ENABLE ALWAYS",
        "A",
        "ALTER EXTENSION pgcrypto DROP EVENT TRIGGER r6_evt;",
    ),
}

SOURCES = {
    "runsql-percent": (
        "apps/c/migrations/0099_pct.py",
        'migrations.RunSQL(sql="SELECT 1", reverse_sql="CREATE %s TRIGGER r6_evt '
        'ON ddl_command_end EXECUTE FUNCTION clinic_app.evt()" % "EVENT")',
    ),
    "runsql-join": (
        "apps/d/migrations/0099_join.py",
        'migrations.RunSQL(sql="SELECT 1", reverse_sql=" ".join(["CREATE", "EVENT", '
        '"TRIGGER r6_evt ON ddl_command_end EXECUTE FUNCTION clinic_app.evt()"]))',
    ),
    "runsql-readfile": (
        "apps/e/migrations/0099_file.py",
        'migrations.RunSQL(sql="SELECT 1", '
        'reverse_sql=Path(__file__).with_name("evt.pgsql").read_text())',
    ),
    "pgsql-file": ("apps/e/migrations/evt.pgsql", EVENT),
    "makefile": (
        "Makefile",
        'install:\n\tpsql -c "CREATE EVENT TRIGGER r6_evt ON ddl_command_end '
        'EXECUTE FUNCTION clinic_app.evt()"\n',
    ),
    "psql-file": ("ops/db/init.psql", EVENT),
    "alter-enable": (
        "apps/f/migrations/0099_alter.py",
        'migrations.RunSQL(sql="ALTER EVENT TRIGGER r6_evt ENABLE ALWAYS", '
        'reverse_sql="ALTER EVENT TRIGGER r6_evt DISABLE")',
    ),
    "unknown-suffix": ("ops/db/change.custom", EVENT),
    "runsql-function": (
        "apps/g/migrations/0099_callable.py",
        'migrations.RunSQL(sql=unresolved_sql_builder(), reverse_sql="SELECT 1")',
    ),
    "runsql-partial": (
        "ops/partial.py",
        'migrations.RunSQL("SELECT 1;" + unresolved_sql_builder())',
    ),
    "runsql-alias": (
        "ops/alias.py",
        "from django.db.migrations import RunSQL as Operation\n"
        'Operation(reverse_sql=unresolved_sql_builder(), sql="SELECT 1")',
    ),
    "runsql-conditional-binding": (
        "ops/conditional.py",
        'SQL = "SELECT 1"\nif runtime_state:\n SQL = unresolved_sql_builder()\n'
        "class Migration:\n operations = [migrations.RunSQL(SQL)]\n",
    ),
    "runsql-class-binding": (
        "ops/class_scope.py",
        'SQL = "SELECT 1"\nclass Migration:\n SQL = unresolved_sql_builder()\n'
        " operations = [migrations.RunSQL(SQL)]\n",
    ),
}
