"""Round-5 reviewer SQL and distinct variants; all objects are test-owned."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Probe:
    blocker: str
    install: str
    remove: str
    runtime: str
    expected: tuple[object, ...] = (True,)
    owner_expected: tuple[object, ...] | None = None


RLS = """
CREATE SCHEMA r5;
CREATE TABLE r5.hol (ends_at timestamptz);
INSERT INTO r5.hol VALUES ('2001-01-01'), ('2999-01-01');
ALTER TABLE r5.hol ENABLE ROW LEVEL SECURITY;
ALTER TABLE r5.hol FORCE ROW LEVEL SECURITY;
CREATE POLICY p ON r5.hol USING (ends_at > now());
CREATE VIEW r5.v WITH (security_invoker) AS SELECT ends_at AS t FROM r5.hol;
CREATE FUNCTION r5.h() RETURNS timestamptz LANGUAGE sql STABLE
AS $$ SELECT t FROM r5.v LIMIT 1 $$;
ALTER TABLE clinic_app.scheduling_resource
ADD COLUMN r5_probe timestamptz DEFAULT r5.h();
GRANT USAGE ON SCHEMA r5 TO clinic_owner, clinic_app;
GRANT SELECT ON ALL TABLES IN SCHEMA r5 TO clinic_owner, clinic_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA r5 TO clinic_owner, clinic_app;
"""
REMOVE = (
    "ALTER TABLE clinic_app.scheduling_resource DROP COLUMN r5_probe; "
    "DROP SCHEMA r5 CASCADE;"
)
PROBES = {
    f"rls-{route}-{volatility.lower()}": Probe(
        "B3-VIEW",
        RLS.replace("STABLE", volatility).replace(
            "SELECT t FROM r5.v LIMIT 1",
            "SELECT ends_at FROM r5.hol LIMIT 1"
            if route == "direct"
            else "SELECT t FROM r5.v LIMIT 1",
        ),
        REMOVE,
        "SELECT extract(year FROM r5.h())::integer",
        (2001,),
        (2999,),
    )
    for route in ("direct", "view")
    for volatility in ("STABLE", "IMMUTABLE")
}
PROBES["rls-variant-two-invokers"] = Probe(
    "B3-VIEW",
    RLS.replace(
        "CREATE FUNCTION r5.h()",
        "CREATE VIEW r5.outer_v WITH (security_invoker) AS SELECT t FROM r5.v; "
        "CREATE FUNCTION r5.h()",
    ).replace("SELECT t FROM r5.v LIMIT 1", "SELECT t FROM r5.outer_v LIMIT 1"),
    REMOVE,
    "SELECT extract(year FROM r5.h())::integer",
    (2001,),
    (2999,),
)

LITERALS = {
    "literal-colon": ("sql", "SELECT 'now'::timestamptz", "now()"),
    "literal-cast": ("sql", "SELECT CAST('now' AS timestamptz)", "now()"),
    "literal-prefix": ("sql", "SELECT timestamptz 'now'", "now()"),
    "literal-today": (
        "sql",
        "SELECT 'today'::date::timestamptz",
        "CURRENT_DATE::timestamptz",
    ),
    "literal-plpgsql-return": (
        "plpgsql",
        "BEGIN RETURN 'now'::timestamptz; END",
        "now()",
    ),
    "literal-plpgsql-assignment": (
        "plpgsql",
        "DECLARE x timestamptz := 'now'; BEGIN RETURN x; END",
        "now()",
    ),
    "literal-variant-tomorrow": (
        "sql",
        "SELECT 'tomorrow'::date::timestamptz",
        "(CURRENT_DATE+1)::timestamptz",
    ),
    "literal-variant-yesterday": (
        "plpgsql",
        "DECLARE x date DEFAULT 'yesterday'; BEGIN RETURN x; END",
        "(CURRENT_DATE-1)::timestamptz",
    ),
}
for name, (language, body, expected) in LITERALS.items():
    PROBES[name] = Probe(
        "B5-TEXT",
        "CREATE FUNCTION clinic_app.scheduling_r5_root() RETURNS timestamptz "
        f"LANGUAGE {language} STABLE AS $$ {body} $$;",
        "DROP FUNCTION clinic_app.scheduling_r5_root();",
        f"SELECT clinic_app.scheduling_r5_root() = {expected}",
    )
PROBES["literal-variant-domain"] = Probe(
    "B5-TEXT",
    "CREATE SCHEMA r5; CREATE DOMAIN r5.instant AS timestamptz; "
    "CREATE FUNCTION clinic_app.scheduling_r5_root() RETURNS timestamptz "
    "LANGUAGE sql STABLE AS $$ SELECT 'now'::r5.instant $$; "
    "GRANT USAGE ON SCHEMA r5 TO clinic_owner;",
    "DROP FUNCTION clinic_app.scheduling_r5_root(); DROP SCHEMA r5 CASCADE;",
    "SELECT clinic_app.scheduling_r5_root() = now()",
)

ATTRIBUTE = """
CREATE SCHEMA r5;
CREATE FUNCTION r5.stamp(clinic_app.scheduling_resource) RETURNS timestamptz
LANGUAGE sql STABLE AS $$ SELECT now() $$;
CREATE FUNCTION r5.h() RETURNS timestamptz LANGUAGE plpgsql STABLE
SET search_path = r5, pg_catalog
AS $$ BEGIN RETURN (NULL::clinic_app.scheduling_resource).stamp; END $$;
ALTER TABLE clinic_app.scheduling_resource
ADD COLUMN r5_probe timestamptz DEFAULT r5.h();
GRANT USAGE ON SCHEMA r5 TO clinic_owner;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA r5 TO clinic_owner;
"""
PROBES["attribute-exact"] = Probe("B5-TEXT", ATTRIBUTE, REMOVE, "SELECT r5.h() = now()")
PROBES["attribute-variant-quoted"] = Probe(
    "B5-TEXT",
    ATTRIBUTE.replace(".stamp", '."MixedStamp"').replace("STABLE", "IMMUTABLE"),
    REMOVE,
    "SELECT r5.h() = now()",
)

EVENT_SQL = """
CREATE SCHEMA r5;
CREATE FUNCTION r5.evt() RETURNS event_trigger LANGUAGE plpgsql AS $$
BEGIN UPDATE clinic_app.scheduling_resource SET active = false
WHERE now() > TIMESTAMPTZ '2999-01-01'; END $$;
CREATE EVENT TRIGGER r5_evt ON ddl_command_end EXECUTE FUNCTION r5.evt();
"""
EVENTS = {
    "event-exact": EVENT_SQL,
    "event-variant-drop": EVENT_SQL.replace(
        "UPDATE clinic_app.scheduling_resource SET active = false\n"
        "WHERE now() > TIMESTAMPTZ '2999-01-01'",
        "PERFORM clock_timestamp()",
    )
    .replace("ddl_command_end", "sql_drop")
    .replace(
        "CREATE EVENT TRIGGER",
        "CREATE /* clock boundary */ EVENT\nTRIGGER",
    ),
}
