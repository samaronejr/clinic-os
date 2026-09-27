"""Round-7 live type, descendant-relation and constructed-value plants."""

from dataclasses import replace

from .clock_residual_probes import Probe

DOMAINS: dict[str, Probe] = {}
for language in ("sql", "plpgsql"):
    for nested in (False, True):
        target = "outer_fresh" if nested else "fresh"
        body = (
            f"SELECT p::r7.{target}"
            if language == "sql"
            else f"DECLARE x r7.{target}; BEGIN x := p; RETURN x; END"
        )
        DOMAINS[f"{language}-{'nested' if nested else 'exact'}"] = Probe(
            "B3-TYPE",
            "CREATE SCHEMA r7; CREATE DOMAIN r7.fresh AS timestamptz "
            "CONSTRAINT clock_check CHECK (VALUE <= now()); "
            + ("CREATE DOMAIN r7.outer_fresh AS r7.fresh; " if nested else "")
            + "CREATE FUNCTION clinic_app.scheduling_r7_root(p timestamptz) "
            f"RETURNS timestamptz LANGUAGE {language} VOLATILE "
            f"AS $body$ {body} $body$;",
            "DROP FUNCTION clinic_app.scheduling_r7_root(timestamptz); "
            "DROP SCHEMA r7 CASCADE;",
            "SELECT clinic_app.scheduling_r7_root('2001-01-01') = "
            "TIMESTAMPTZ '2001-01-01'",
        )
DOMAINS["array"] = Probe(
    "B3-TYPE",
    "CREATE SCHEMA r7; CREATE DOMAIN r7.fresh AS timestamptz "
    "CONSTRAINT clock_check CHECK (VALUE <= now()); "
    "CREATE FUNCTION clinic_app.scheduling_r7_root(p timestamptz) "
    "RETURNS timestamptz LANGUAGE plpgsql AS $$ DECLARE x r7.fresh[]; "
    "BEGIN x := ARRAY[p]; RETURN x[1]; END $$;",
    "DROP FUNCTION clinic_app.scheduling_r7_root(timestamptz); DROP SCHEMA r7 CASCADE;",
    "SELECT clinic_app.scheduling_r7_root('2001-01-01') = TIMESTAMPTZ '2001-01-01'",
)
DOMAINS["range"] = Probe(
    "B3-TYPE",
    "CREATE SCHEMA r7; CREATE DOMAIN r7.fresh AS timestamptz "
    "CONSTRAINT clock_check CHECK (VALUE <= now()); "
    "CREATE TYPE r7.period AS RANGE (subtype=r7.fresh); "
    "CREATE FUNCTION clinic_app.scheduling_r7_root(p timestamptz) "
    "RETURNS timestamptz LANGUAGE plpgsql AS $$ DECLARE x r7.period; "
    "BEGIN x := r7.period(p,p); RETURN p; END $$;",
    "DROP FUNCTION clinic_app.scheduling_r7_root(timestamptz); DROP SCHEMA r7 CASCADE;",
    "SELECT clinic_app.scheduling_r7_root('2001-01-01') = TIMESTAMPTZ '2001-01-01'",
)
DOMAIN_DEFAULT = Probe(
    "B3-TYPE",
    "CREATE SCHEMA r7; CREATE DOMAIN r7.fresh AS timestamptz DEFAULT now(); "
    "CREATE FUNCTION clinic_app.scheduling_r7_root() RETURNS timestamptz "
    "LANGUAGE plpgsql AS $$ DECLARE x r7.fresh; BEGIN RETURN x; END $$;"
    " GRANT USAGE ON SCHEMA r7 TO clinic_owner;",
    "DROP FUNCTION clinic_app.scheduling_r7_root(); DROP SCHEMA r7 CASCADE;",
    "SELECT clinic_app.scheduling_r7_root() IS NULL",
)

PARTITIONS: dict[str, Probe] = {}
for levels in (1, 2):
    children = (
        "CREATE TABLE r7.pt1 PARTITION OF r7.pt FOR VALUES IN (1); "
        if levels == 1
        else "CREATE TABLE r7.mid PARTITION OF r7.pt FOR VALUES IN (1) "
        "PARTITION BY RANGE (k); CREATE TABLE r7.pt1 PARTITION OF r7.mid "
        "FOR VALUES FROM (0) TO (2); "
    )
    PARTITIONS[f"levels-{levels}"] = Probe(
        "B3-PARTITION",
        "CREATE SCHEMA r7; CREATE TABLE r7.pt (k int, at timestamptz) "
        "PARTITION BY LIST(k); "
        + children
        + "CREATE FUNCTION r7.stamp() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN NEW.at := clock_timestamp(); RETURN NEW; END $$; "
        "CREATE TRIGGER r7_stamp BEFORE INSERT ON r7.pt1 "
        "FOR EACH ROW EXECUTE FUNCTION r7.stamp(); "
        "CREATE FUNCTION clinic_app.scheduling_r7_root() RETURNS timestamptz "
        "LANGUAGE sql VOLATILE AS $$ INSERT INTO r7.pt(k) VALUES(1) RETURNING at $$;",
        "DROP FUNCTION clinic_app.scheduling_r7_root(); DROP SCHEMA r7 CASCADE;",
        "SELECT clinic_app.scheduling_r7_root() BETWEEN statement_timestamp() "
        "AND clock_timestamp()",
    )

# The census parses as the migration owner, not the installing test superuser.
for cases in (DOMAINS, PARTITIONS):
    for name, probe in cases.items():
        cases[name] = replace(
            probe,
            install=probe.install + " GRANT USAGE ON SCHEMA r7 TO clinic_owner; "
            "GRANT ALL ON ALL TABLES IN SCHEMA r7 TO clinic_owner;",
        )
INHERITANCE = Probe(
    "B3-PARTITION",
    "CREATE SCHEMA r7; CREATE TABLE r7.parent(k integer,at timestamptz); "
    "CREATE TABLE r7.child() INHERITS(r7.parent); "
    "ALTER TABLE r7.child ALTER COLUMN at SET DEFAULT now(); "
    "ALTER TABLE r7.child ADD CONSTRAINT child_clock CHECK(at<=clock_timestamp()); "
    "ALTER TABLE r7.child ENABLE ROW LEVEL SECURITY; "
    "CREATE POLICY child_clock ON r7.child USING(at<=now()); "
    "CREATE TABLE r7.log(at timestamptz); "
    "CREATE RULE child_clock AS ON INSERT TO r7.child DO ALSO "
    "INSERT INTO r7.log VALUES(now()); "
    "CREATE FUNCTION r7.stamp() RETURNS trigger LANGUAGE plpgsql "
    "AS $$ BEGIN NEW.at:=statement_timestamp(); RETURN NEW; END $$; "
    "CREATE TRIGGER child_stamp BEFORE INSERT ON r7.child "
    "FOR EACH ROW EXECUTE FUNCTION r7.stamp(); "
    "INSERT INTO r7.child(k) VALUES(1); "
    "CREATE FUNCTION clinic_app.scheduling_r7_root() RETURNS timestamptz "
    "LANGUAGE sql AS $$ SELECT at FROM r7.parent ORDER BY k LIMIT 1 $$; "
    "GRANT USAGE ON SCHEMA r7 TO clinic_owner; "
    "GRANT ALL ON ALL TABLES IN SCHEMA r7 TO clinic_owner;",
    "DROP FUNCTION clinic_app.scheduling_r7_root(); DROP SCHEMA r7 CASCADE;",
    "SELECT clinic_app.scheduling_r7_root() IS NOT NULL",
)

LITERALS = ("10:00 today", "Mon today", ",now", "(now)")
CONSTRUCTIONS = {
    "concat": ("sql", "", "SELECT ('n'||'ow')::timestamptz"),
    "format": ("sql", "", "SELECT format('%s%s','n','ow')::timestamptz"),
    "text-parameter": ("sql", "p text", "SELECT p::timestamptz"),
    "text-variable": (
        "plpgsql",
        "",
        "DECLARE x text := 'n'||'ow'; BEGIN RETURN x::timestamptz; END",
    ),
    "implicit-return": ("plpgsql", "p text", "BEGIN RETURN p; END"),
    "implicit-assignment": (
        "plpgsql",
        "p text",
        "DECLARE x timestamptz; BEGIN x := p; RETURN x; END",
    ),
    "cast-form": ("sql", "p text", "SELECT CAST(p AS timestamp with time zone)"),
    "plpgsql-cast": ("plpgsql", "p text", "BEGIN RETURN CAST(p AS timestamptz); END"),
    "plpgsql-constructor": ("plpgsql", "p text", "BEGIN RETURN timestamptz(p); END"),
    "declaration-default": (
        "plpgsql",
        "",
        "DECLARE x timestamptz := ('n'||'ow'); BEGIN RETURN x; END",
    ),
    "substring": ("sql", "", "SELECT substr('snow',2)::timestamptz"),
}


def expression_probe(language: str, arguments: str, body: str) -> Probe:
    return Probe(
        "B5-CONSTRUCTION",
        "CREATE FUNCTION clinic_app.scheduling_r7_root(" + arguments + ") "
        "RETURNS timestamptz LANGUAGE " + language + " AS $body$ " + body + " $body$;",
        "DROP FUNCTION clinic_app.scheduling_r7_root("
        + ("text" if arguments else "")
        + ");",
        "SELECT clinic_app.scheduling_r7_root("
        + ("'now'" if arguments else "")
        + ") = now()",
    )
