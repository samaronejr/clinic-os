"""Real PostgreSQL plants for each round-8 detector class."""

from dataclasses import replace

from .clock_residual_probes import Probe

WORD = (
    "CREATE SCHEMA r8; CREATE FUNCTION r8.word() RETURNS text "
    "LANGUAGE sql AS $$ SELECT 'n'||'ow' $$; "
)
CLEAN = "DROP FUNCTION clinic_app.scheduling_r8_root(); DROP SCHEMA r8 CASCADE;"


def expression(
    body: str, *, language: str = "sql", ddl: str = "", result: str = "=now()"
) -> Probe:
    return Probe(
        "R8-VALUE",
        WORD + ddl + "CREATE FUNCTION clinic_app.scheduling_r8_root() "
        "RETURNS timestamptz LANGUAGE "
        + language
        + " AS $body$ "
        + body
        + " $body$; GRANT USAGE ON SCHEMA r8 TO clinic_owner;",
        CLEAN,
        "SELECT clinic_app.scheduling_r8_root() " + result,
    )


PL = {
    "select-into": expression(
        "DECLARE s text:=r8.word(); x timestamptz; "
        "BEGIN SELECT s INTO x; RETURN x; END",
        language="plpgsql",
    ),
    "nested-into": expression(
        "DECLARE s text:=r8.word(); x timestamptz; "
        "BEGIN SELECT (INTO x s); RETURN x; END",
        language="plpgsql",
    ),
    "nested-into-strict": expression(
        "DECLARE s text:=r8.word(); x timestamptz; "
        "BEGIN SELECT (INTO STRICT x s); RETURN x; END",
        language="plpgsql",
    ),
    "select-into-strict": expression(
        "DECLARE x timestamptz; BEGIN SELECT r8.word() INTO STRICT x; RETURN x; END",
        language="plpgsql",
    ),
    "for-query": expression(
        "DECLARE x timestamptz; BEGIN FOR x IN SELECT r8.word() "
        "LOOP RETURN x; END LOOP; END",
        language="plpgsql",
    ),
    "for-query-variant": expression(
        "DECLARE x timestamptz; BEGIN FOR x IN SELECT format('%s%s','n','ow') "
        "LOOP NULL; END LOOP; RETURN x; END",
        language="plpgsql",
    ),
    "foreach": expression(
        "DECLARE x timestamptz; BEGIN FOREACH x IN ARRAY ARRAY[r8.word()] "
        "LOOP RETURN x; END LOOP; END",
        language="plpgsql",
    ),
    "fetch": expression(
        "DECLARE c refcursor; x timestamptz; BEGIN OPEN c FOR SELECT r8.word(); "
        "FETCH c INTO x; CLOSE c; RETURN x; END",
        language="plpgsql",
    ),
    "diagnostics": expression(
        "DECLARE x timestamptz; BEGIN BEGIN RAISE EXCEPTION '%', r8.word(); "
        "EXCEPTION WHEN OTHERS THEN GET STACKED DIAGNOSTICS x=MESSAGE_TEXT; "
        "END; RETURN x; END",
        language="plpgsql",
    ),
    "constant-initializer": expression(
        "DECLARE x CONSTANT timestamptz:=r8.word(); BEGIN RETURN x; END",
        language="plpgsql",
    ),
    "unanalysed-type-modifier": expression(
        "DECLARE x timestamptz(6):=r8.word(); BEGIN RETURN x; END",
        language="plpgsql",
    ),
    "assignment-equals": expression(
        "DECLARE x timestamptz; BEGIN x=r8.word(); RETURN x; END", language="plpgsql"
    ),
    "composite-assignment": expression(
        "DECLARE r r8.rec; s text:=r8.word(); BEGIN r:=ROW(s); RETURN r.at; END",
        language="plpgsql",
        ddl="CREATE TYPE r8.rec AS (at timestamptz); ",
    ),
}
COMPOSITES = {
    "exact": expression(
        "SELECT (('('||r8.word()||')')::r8.rec).at",
        ddl="CREATE TYPE r8.rec AS (at timestamptz); ",
    ),
    "nested": expression(
        "SELECT (('('||r8.word()||')')::r8.wrapper).at",
        ddl="CREATE DOMAIN r8.instant AS timestamptz; "
        "CREATE TYPE r8.rec AS (at r8.instant); "
        "CREATE DOMAIN r8.wrapper AS r8.rec; ",
    ),
}
LITERALS = ("10:00today", "today10:00", "10:00:00today", "today_10:00")

for kind, body in {
    "return-next": "DECLARE s text:=r8.word(); BEGIN RETURN NEXT s; END",
    "return-query": "BEGIN RETURN QUERY SELECT r8.word()::timestamptz; END",
}.items():
    base = expression(body, language="plpgsql")
    PL[kind] = replace(
        base,
        install=base.install.replace(
            "RETURNS timestamptz LANGUAGE", "RETURNS SETOF timestamptz LANGUAGE"
        ),
    )
base = expression("BEGIN SELECT r8.word() INTO x; RETURN; END", language="plpgsql")
PL["out-parameter"] = replace(
    base,
    install=base.install.replace(
        "scheduling_r8_root() RETURNS", "scheduling_r8_root(OUT x timestamptz) RETURNS"
    ),
)
for kind, body in {
    "unknown-set": "BEGIN SET LOCAL application_name='synthetic_r8'; RETURN NULL; END",
    "unknown-reset": "BEGIN RESET application_name; RETURN NULL; END",
}.items():
    PL[kind] = expression(body, language="plpgsql", result="IS NULL")
COMPOSITES["recursive"] = expression(
    "SELECT (((ROW(ROW(r8.word())::text)::text)::r8.outer_rec).value).at",
    ddl="CREATE TYPE r8.rec AS (at timestamptz); "
    "CREATE TYPE r8.outer_rec AS (value r8.rec); ",
)
COMPOSITES["array"] = expression(
    "SELECT ((ARRAY[ROW(r8.word())::text]::text::r8.rec[])[1]).at",
    ddl="CREATE TYPE r8.rec AS (at timestamptz); ",
)
COMPOSITES["range"] = expression(
    'SELECT (lower(format(\'["%s","%s"]\',ROW(r8.word())::text,'
    "ROW(r8.word())::text)::r8.span)).at",
    ddl="CREATE TYPE r8.rec AS (at timestamptz); "
    "CREATE TYPE r8.span AS RANGE(subtype=r8.rec,subtype_opclass=record_ops); ",
)


def cascade(action: str, event: str, *, nested: bool = False) -> Probe:
    default = "DEFAULT 2 " if action == "SET DEFAULT" else ""
    child = "middle" if nested else "parent"
    middle = (
        "CREATE TABLE r8.middle(id integer PRIMARY KEY REFERENCES r8.parent "
        "ON DELETE CASCADE ON UPDATE CASCADE); "
        if nested
        else ""
    )
    command = (
        "DELETE FROM r8.parent WHERE id=1"
        if event == "DELETE"
        else "UPDATE r8.parent SET id=3 WHERE id=1"
    )
    return Probe(
        "R8-FK",
        "CREATE SCHEMA r8; CREATE TABLE r8.parent(id integer PRIMARY KEY); "
        + middle
        + f"CREATE TABLE r8.child(id integer {default}REFERENCES r8.{child} "
        f"ON {event} {action}); "
        "CREATE TABLE r8.log(at timestamptz); "
        "CREATE FUNCTION r8.stamp_del() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN INSERT INTO r8.log VALUES(clock_timestamp()); "
        "RETURN COALESCE(NEW,OLD); END $$; "
        "CREATE TRIGGER r8_stamp BEFORE DELETE OR UPDATE ON r8.child "
        "FOR EACH ROW EXECUTE FUNCTION r8.stamp_del(); "
        "INSERT INTO r8.parent VALUES(1),(2); "
        + ("INSERT INTO r8.middle VALUES(1),(2); " if nested else "")
        + "INSERT INTO r8.child VALUES(1); "
        "CREATE FUNCTION clinic_app.scheduling_r8_root() "
        "RETURNS integer LANGUAGE sql AS $$ " + command + " RETURNING id $$; "
        "GRANT USAGE ON SCHEMA r8 TO clinic_owner; "
        "GRANT ALL ON ALL TABLES IN SCHEMA r8 TO clinic_owner; "
        "SELECT clinic_app.scheduling_r8_root();",
        CLEAN,
        "SELECT count(*)=1 AND bool_and(at IS NOT NULL) FROM r8.log",
    )
