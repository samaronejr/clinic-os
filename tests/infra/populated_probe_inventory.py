"""Map existing synthetic service scenarios to real populated-row probes."""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg
from django.db import connection
from psycopg import sql

from database_urls import database_url_for_name
from infra.populated_probe import (
    Witness,
    Writes,
    assert_read,
    assert_writes,
    foreign_staff,
    runtime_connection,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

MAP = Path(__file__).with_name("populated_probe_map.tsv")


@dataclass(frozen=True, slots=True)
class Probe:
    relation: str
    model: str
    scope: str
    select: str
    insert: str
    update: str
    update_column: str
    delete: str
    truncate: str
    seed: str
    id_boundary: str

    @property
    def writes(self) -> Writes:
        return Writes(self.update_column, self.update, self.delete, self.insert)


def inventory() -> tuple[Probe, ...]:
    with MAP.open(encoding="utf-8") as stream:
        return tuple(Probe(**row) for row in csv.DictReader(stream, delimiter="\t"))


def database_url(name: str) -> str:
    return database_url_for_name(
        os.environ[name], str(connection.settings_dict["NAME"])
    )


PARENT_SCOPE = {
    "profile": ("profile_id", "identity_physicianprofile", "organization_id"),
    "recovery_batch": ("batch_id", "identity_recoverybatch", "user_id"),
    "static_device": ("device_id", "otp_static_staticdevice", "user_id"),
    "totp_device": ("device_id", "otp_totp_totpdevice", "user_id"),
}
ACTOR_SCOPES = frozenset({"user_id", "recovery_batch", "static_device", "totp_device"})


def _scope_statement(scope: str) -> tuple[sql.Composable, sql.Composable]:
    if scope in PARENT_SCOPE:
        field, parent, owner = PARENT_SCOPE[scope]
        join = sql.SQL("JOIN {} AS p ON p.id = t.{}").format(
            sql.Identifier("clinic_app", parent), sql.Identifier(field)
        )
        return sql.SQL("p.{}").format(sql.Identifier(owner)), join
    return sql.SQL("t.{}").format(sql.Identifier(scope)), sql.SQL("")


def source_row(
    db: psycopg.Connection[tuple[object, ...]],
    relation: str,
    scope: str,
    *,
    for_insert: bool = True,
) -> tuple[Witness, str | None]:
    """Check a concrete committed row, with an actual tenant/actor binding."""
    qualified = sql.Identifier("clinic_app", relation)
    pk = db.execute(
        """
        SELECT a.attname
          FROM pg_index AS i
          JOIN pg_attribute AS a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
         WHERE i.indrelid = to_regclass(%s) AND i.indisprimary
        """,
        [qualified.as_string(db)],
    ).fetchall()
    assert len(pk) == 1, f"{relation}: expected a single-column primary key"
    key = pk[0][0]
    assert isinstance(key, str)
    binding, join = _scope_statement(scope)
    owner_table = "identity_user" if scope in ACTOR_SCOPES else "identity_organization"
    row = db.execute(
        sql.SQL(
            "SELECT t.{key}, {payload} FROM {table} AS t {join} "
            "JOIN {owner} AS own ON own.id = {binding} ORDER BY t.{key} LIMIT 1"
        ).format(
            key=sql.Identifier(key),
            payload=sql.SQL("to_jsonb(t)::text" if for_insert else "NULL"),
            table=qualified,
            join=join,
            owner=sql.Identifier("clinic_app", owner_table),
            binding=binding,
        )
    ).fetchone()
    assert row is not None, f"{relation}: no populated row after synthetic fixture"
    witness = Witness(key, row[0])
    if not for_insert:
        return witness, None
    assert isinstance(row[1], str)
    return witness, _insert_payload(key, row[0], row[1])


def _insert_payload(key: str, value: object, serialized: str) -> str:
    """Avoid confusing a primary-key collision with a tenant boundary."""
    if key != "id":
        # These keys encode a binding (e.g. identity_userpreference.user_id).
        # Keep the relationship to the foreign actor intact.
        return serialized
    payload = json.loads(serialized)
    if isinstance(value, UUID):
        payload[key] = str(uuid4())
    else:
        assert isinstance(value, int), f"no fresh key for {type(value).__name__}"
        payload[key] = -value
    return json.dumps(payload)


def check_seeded_relations(probes: Iterable[Probe]) -> None:
    with psycopg.connect(database_url("TEST_SUPERUSER_DATABASE_URL")) as owner:
        sources = [
            (
                probe,
                *source_row(
                    owner, probe.relation, probe.scope, for_insert=probe.insert != "ACL"
                ),
            )
            for probe in probes
        ]
    outsider = foreign_staff()
    with runtime_connection(database_url("APP_DATABASE_URL"), outsider) as db:
        for probe, witness, payload in sources:
            assert_read(db, probe.relation, witness, probe.select)
            assert_writes(db, probe.relation, witness, probe.writes, payload)
            if probe.insert not in {"ACL", "WITH CHECK"}:
                assert payload is not None
                _assert_insert_policy_denies(db, probe.relation, payload)
            if probe.relation == "ehr_timelinerefresh":
                # A private queue has no table grant; the real worker
                # resolver must refuse both selectors in a staff context.
                for selector in (witness.value, uuid4()):
                    assert db.execute(
                        "SELECT clinic_app.ehr_timeline_refresh(%s)", [selector]
                    ).fetchone() == (False,)


def _assert_insert_policy_denies(
    db: psycopg.Connection[tuple[object, ...]],
    relation: str,
    payload: str,
) -> None:
    """Test the effective WITH CHECK when a BEFORE trigger wins the error race.

    PostgreSQL checks the proposed row after BEFORE ROW triggers. A trigger
    rejecting the clone for lifecycle reasons is not proof of tenant scoping.
    Evaluate the live policies against the actual row under the same clinic_app
    transaction, without changing either triggers or policies.
    """
    policies = db.execute(
        """
        SELECT p.polpermissive,
               coalesce(pg_get_expr(p.polwithcheck, p.polrelid),
                        pg_get_expr(p.polqual, p.polrelid), 'true')
          FROM pg_policy p
         WHERE p.polrelid = to_regclass(%s) AND p.polcmd IN ('*', 'a')
           AND EXISTS (
               SELECT 1 FROM unnest(p.polroles) AS role(oid)
                WHERE CASE WHEN role.oid = 0 THEN true
                           ELSE pg_has_role(role.oid, 'USAGE') END
           )
        """,
        [sql.Identifier("clinic_app", relation).as_string(db)],
    ).fetchall()
    permissive: list[sql.Composed] = []
    restrictive: list[sql.Composed] = []
    for grants, expr in policies:
        assert isinstance(expr, str)
        # Trusted migration expression from PostgreSQL, not a fixture string.
        atom = sql.SQL("coalesce(({}), false)").format(sql.SQL(expr))
        (permissive if grants else restrictive).append(atom)
    claim = sql.SQL(
        "SELECT ({}) AND ({}) FROM jsonb_populate_record(NULL::{}, %s::jsonb) AS {}"
    ).format(
        sql.SQL(" OR ").join(permissive) if permissive else sql.SQL("false"),
        sql.SQL(" AND ").join(restrictive) if restrictive else sql.SQL("true"),
        sql.Identifier("clinic_app", relation),
        sql.Identifier(relation),
    )
    assert db.execute(claim, [payload]).fetchone() == (False,), relation
