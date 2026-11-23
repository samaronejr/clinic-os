"""Staged candidate migration with representative rows in every new domain.

``python -m ops.testing.migration_upgrade_stages`` runs on the candidate
against the base-seeded upgrade database. It runs Django's own ``migrate``
command (the same plan, order and signals as ``manage.py migrate``) and,
right after each migration that first brings a new model into the migration
state, loads that model's representative synthetic rows through the
*historical* model of that state. Every remaining migration then runs over
real rows. No candidate model class is used for a write.

Each stage is one transaction with one savepoint per row. CHECK, UNIQUE,
FOREIGN KEY, NOT NULL, row-level security, every AFTER trigger and every
deferred constraint trigger stay active for every load. Only the named
BEFORE ROW INSERT guards in ``GUARDED_TRIGGERS`` (the ones that authorize
against a live request session) are suspended, before the stage's first row
and restored after its deferred constraints fire; the stage refuses to
commit unless exactly those triggers were the disabled ones. The verifier
then requires no disabled user trigger at the leaf and runs the domains'
cross-row invariants over the seeded graph.

Secret-bearing columns never reach the record: ``SECRET_COLUMNS`` are kept
as digests, and any unclassified column whose name marks secret material
fails the load. Encryption is checked for every column that was protected in
any migration state the row passed through, not only in the leaf model.

The set of new models is derived from the migration graph, never declared:
concrete managed models of the leaf state that are absent from the state of
the migrations the base applied. The loaded values are written to
``$UPGRADE_DOMAIN_SEED_FILE``; ``verify_stages`` re-derives the set at the
leaf and requires every new model to have a seeded row that reads back
unchanged, with every encrypted column a ciphertext envelope that decrypts
to its seeded plaintext.
"""

# ruff: noqa: SLF001 - historical models expose schema only via _meta/_base_manager
from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Never, cast
from uuid import UUID, uuid4

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.commands import migrate
from django.db import DatabaseError, connection, transaction
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import BinaryField, Value
from django.utils import timezone
from psycopg import sql

from ops.testing.migration_upgrade_domains import (
    DOMAIN_SEEDS,
    GUARDED_TRIGGERS,
    LEAF_INVARIANTS,
    LOAD_ROLES,
    PRESERVATION_EXCEPTIONS,
    SECRET_COLUMNS,
    TRANSITION_GUARDS,
    Row,
    Seed,
    SeedContext,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

    from django.apps.registry import Apps
    from django.db.backends.utils import CursorWrapper
    from django.db.migrations import Migration
    from django.db.models import Field, Model, QuerySet

Node = tuple[str, str]
JsonMap = dict[str, object]
LOAD_ERRORS = (DatabaseError, LookupError, TypeError, ValueError, ValidationError)
DIGEST_TOKENS = ("sha256", "digest", "hash", "fingerprint", "checksum")
PHYSICIAN_TOKENS = ("physician", "practitioner", "clinician", "author", "prescriber")
END_TOKENS = ("expire", "until", "valid_to", "ends", "end_at", "due", "deadline")
START_TOKENS = ("start", "valid_from", "begin", "opened")
SECRET_TOKENS = ("secret", "password", "private", "wrapped_key", "seed")
DIGEST_SUFFIXES = ("_hash", "_digest", "_sha256", "_fingerprint")
# pg_trigger.tgtype bits: ROW and BEFORE, then the INSERT and UPDATE events.
ROW_BEFORE = 1 | 2
EVENT_BITS = {"INSERT": 4, "UPDATE": 16}
SCHEMA_VERSION = 2


def _fail(reason: str) -> Never:
    raise RuntimeError(reason)


def new_models(loader: MigrationLoader, base_nodes: Iterable[Node]) -> dict[str, str]:
    """Leaf-state concrete models absent from the base state, label -> app."""
    nodes = sorted(base_nodes)
    if not nodes:
        _fail("no base migration state recorded: refusing a blank database")
    base = loader.project_state(nodes=nodes, at_end=True)
    leaf = loader.project_state()
    return {
        f"{model.app_label}.{model.name}": model.app_label
        for key, model in sorted(leaf.models.items())
        if key not in base.models
        and not model.options.get("proxy")
        and model.options.get("managed", True)
    }


def _plain(value: object) -> object:
    """JSON-stable form of one stored column value."""
    if isinstance(value, (bytes, memoryview)):
        return "bytes:" + bytes(value).hex()
    if isinstance(value, (date, time)):
        moment = value.astimezone(UTC) if isinstance(value, datetime) else value
        return moment.isoformat()
    if isinstance(value, (UUID, Decimal, timedelta)):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _purpose(field: Field[object, object]) -> str | None:
    """Envelope purpose of an encrypted column, ``None`` for plain columns."""
    purpose = getattr(field, "envelope_purpose", None)
    return purpose if isinstance(purpose, str) else None


def _moment(name: str, now: datetime) -> datetime:
    if any(token in name for token in END_TOKENS) or name.endswith("_to"):
        return now + timedelta(days=30)
    if any(token in name for token in START_TOKENS) or name.endswith("_from"):
        return now - timedelta(hours=1)
    return now


def _text(name: str, max_length: int | None) -> str:
    lowered = name.lower()
    if any(token in lowered for token in DIGEST_TOKENS):
        value = "a" * 64
    elif "currency" in lowered:
        value = "BRL"
    elif "email" in lowered:
        value = "upgrade@example.invalid"
    elif lowered.endswith("uf"):
        value = "SP"
    elif "timezone" in lowered:
        value = "America/Sao_Paulo"
    elif "locale" in lowered or "language" in lowered:
        value = "pt-BR"
    else:
        value = "synthetic-" + lowered.replace("_", "-")
    return value[:max_length] if max_length else value


def _encrypted(field: Field[object, object], label: str) -> object:
    marker = f"Synthetic upgrade {label}.{field.name}"
    kind = type(field).__name__
    if kind == "EncryptedDateField":
        return date(1991, 3, 14)
    if kind == "EncryptedJSONField":
        return {"synthetic": True, "note": marker}
    if kind == "EncryptedBytesField":
        return marker.encode()
    if kind == "EncryptedPatientNameField":
        return "Synthetic Upgrade Persona"
    return marker


_CONSTANTS: dict[str, object] = {
    "BooleanField": False,
    "DecimalField": Decimal(1),
    "FloatField": 1.0,
    "TimeField": time(9, 0),
    "DurationField": timedelta(minutes=30),
    "GenericIPAddressField": "192.0.2.10",
}


def _scalar(field: Field[object, object], label: str, now: datetime) -> object:
    """Choose a synthetic value for one required, default-less plain column."""
    if field.choices:
        return field.flatchoices[0][0]
    kind = field.get_internal_type()
    name = field.name
    if kind in _CONSTANTS:
        return _CONSTANTS[kind]
    if kind.endswith("IntegerField"):
        return 1
    if kind in {"DateTimeField", "DateField"}:
        moment = _moment(name, now)
        return moment if kind == "DateTimeField" else moment.date()
    factories: dict[str, Callable[[], object]] = {
        "UUIDField": uuid4,
        "JSONField": dict,
        "ArrayField": list,
        "BinaryField": lambda: sha256(f"{label}.{name}".encode()).digest(),
    }
    if kind in factories:
        return factories[kind]()
    return _text(name, cast("int | None", getattr(field, "max_length", None)))


class Stager:
    """Seed each new model right after the migration that first adds it."""

    def __init__(self, markers: Mapping[str, object]) -> None:
        """Bind the witness tenant and the exact base migration state."""
        tenant = cast("dict[str, str]", markers["witness"])
        self.organization = UUID(tenant["organization_id"])
        self.clinic = UUID(tenant["clinic_id"])
        self.owner = UUID(tenant["owner_id"])
        self.physician = UUID(tenant["physician_id"])
        self.patient = UUID(tenant["patient_id"])
        self.enrollment = UUID(tenant["enrollment_id"])
        self.companion_patient = UUID(tenant["companion_patient_id"])
        self.companion_enrollment = UUID(tenant["companion_enrollment_id"])
        self.now = timezone.now()
        self.loader = MigrationLoader(connection)
        applied = set(MigrationRecorder(connection).applied_migrations())
        if applied - set(self.loader.graph.nodes):
            _fail("the base applied migrations the candidate graph does not know")
        self.base_nodes = sorted(applied)
        self.new = new_models(self.loader, applied)
        self.state = self.loader.project_state(nodes=self.base_nodes, at_end=True)
        # Render once; each migration's state_forwards then reloads only the
        # models it touches, exactly like the executor's own state.
        self.state.apps.get_models()
        self.present = set(self.state.models)
        self.seeded: dict[str, list[object]] = {}
        self.legacy_rows: dict[tuple[str, str], object] = {}
        self.rows: list[JsonMap] = []
        self.stages: list[JsonMap] = []
        self.unseeded: list[str] = []
        self.protected: dict[str, set[str]] = {}

    def applied(self, migration: Migration) -> None:
        """Advance the historical state; seed the models it first adds."""
        self.state = migration.mutate_state(self.state, preserve=False)
        added = sorted(
            key
            for key in self.state.models
            if key not in self.present
            and f"{self.state.models[key].app_label}.{self.state.models[key].name}"
            in self.new
        )
        self.present = set(self.state.models)
        self._track_protection()
        if added:
            self._stage(f"{migration.app_label}.{migration.name}", added)

    def _track_protection(self) -> None:
        """Remember every column protected in any state a seeded row crossed."""
        for label in self.seeded:
            app, name = label.split(".")
            state = self.state.models.get((app, name.lower()))
            if state is None:
                continue
            self.protected.setdefault(label, set()).update(
                field_name
                for field_name, field in state.fields.items()
                if _purpose(field) is not None
            )

    def _stage(self, stage: str, keys: list[tuple[str, str]]) -> None:
        apps = self.state.apps
        models = {
            f"{model._meta.app_label}.{model._meta.object_name}": model
            for model in (apps.get_model(*key) for key in keys)
        }
        errors: list[str] = []
        loaded = len(self.rows)
        suspended = sorted(
            {
                (apps.get_model(label)._meta.db_table, name, event)
                for label in _stage_labels(models)
                for name in GUARDED_TRIGGERS.get(label, ())
                for event in ("INSERT",)
            }
            | {
                (apps.get_model(label)._meta.db_table, name, "UPDATE")
                for label in _transition_labels(models)
                for name in TRANSITION_GUARDS.get(label, ())
            }
        )
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                # Suspend before any row exists, restore after every deferred
                # check has fired: no pending trigger event can block either,
                # and deferred constraint triggers see the whole stage graph.
                _require_no_disabled(cursor, set())
                for table, name, event in suspended:
                    _suspend_guard(cursor, table, name, event)
                for label in _dependency_order(models):
                    self._seed_model(stage, label, models[label], errors)
                if errors:
                    self.failed(errors)
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                _require_no_disabled(cursor, {(t, n) for t, n, _e in suspended})
                for table, name, _event in suspended:
                    cursor.execute(
                        sql.SQL("ALTER TABLE clinic_app.{} ENABLE TRIGGER {}").format(
                            sql.Identifier(table), sql.Identifier(name)
                        )
                    )
                _require_no_disabled(cursor, set())
                if errors:
                    self.failed(errors)
        except LOAD_ERRORS as error:
            for row in self.rows[loaded:]:
                self.seeded[str(row["model"])].remove(row["pk"])
            del self.rows[loaded:]
            self.failed([f"{stage}: commit: {type(error).__name__}: {error}"])
        self.stages.append(
            {
                "migration": stage,
                "models": sorted(models),
                "suspended_triggers": [
                    f"{table}.{name} ({event})" for table, name, event in suspended
                ],
            }
        )

    def _seed_model(
        self, stage: str, label: str, model: type[Model], errors: list[str]
    ) -> None:
        from apps.tenancy.envelope import EnvelopeError  # noqa: PLC0415

        seed = _seed_for(label)
        if seed is None:
            self.unseeded.append(label)
            print(f"  stage {stage}: {label} has no seed entry")  # noqa: T201
            return
        records: list[JsonMap] = []

        def keep(record: JsonMap) -> None:
            # Visible to the next row's references at once.
            records.append(record)
            self.rows.append(record)
            self.seeded.setdefault(str(record["model"]), []).append(record["pk"])

        try:
            for support, helper in seed.support:
                for values in helper.rows(self._context(self.state.apps)):
                    keep(
                        self._load(
                            self.state.apps.get_model(support),
                            support,
                            helper,
                            values,
                            stage,
                        )
                    )
            if seed.adopt:
                keep(self._adopt(model, label, seed, stage))
            else:
                for values in seed.rows(self._context(self.state.apps)):
                    keep(self._load(model, label, seed, values, stage))
            for target, change in seed.updates:
                self._transition(target, *change(self._context(self.state.apps)))
        except (*LOAD_ERRORS, EnvelopeError) as error:
            errors.append(f"{stage}: {label}: {type(error).__name__}: {error}")
            return
        print(f"  stage {stage}: seeded {len(records)} {label} row(s)")  # noqa: T201

    def _transition(self, label: str, key: object, changes: Row) -> None:
        """Move one seeded row through a product transition and re-record it."""
        model = self.state.apps.get_model(label)
        names = [field.attname for field in model._meta.concrete_fields]
        with _scope(self.organization, self.owner, LOAD_ROLES.get(label, "")):
            if model._base_manager.filter(pk=key).update(**changes) != 1:
                message = f"{label} transition matched no seeded row"
                raise LookupError(message)
        stored, _raw = _visible(
            model, key, self.organization, names, (), _readers(self.owner, "", self)
        )
        for row in self.rows:
            if row["model"] == label and row["pk"] == _plain(key) and stored:
                row["values"] = {
                    name: _recorded(label, name, value)
                    for name, value in stored.items()
                }
                row["transitions"] = [
                    *cast("list[Row]", row.get("transitions", [])),
                    {name: _plain(v) for name, v in changes.items()},
                ]

    def failed(self, errors: list[str]) -> None:
        """Stop the upgrade: a stage that cannot load its rows is a defect."""
        _fail("domain stage load failed:\n" + "\n".join(errors))

    def _context(self, apps: Apps) -> SeedContext:
        return SeedContext(
            organization=self.organization,
            clinic=self.clinic,
            owner=self.owner,
            physician=self.physician,
            patient=self.patient,
            enrollment=self.enrollment,
            companion_patient=self.companion_patient,
            companion_enrollment=self.companion_enrollment,
            now=self.now,
            seeded=self.seeded,
            legacy=lambda label, filters: self._legacy(apps, label, filters),
            fetch=lambda label, name, filters: self._fetch(apps, label, name, filters),
        )

    def _fetch(
        self, apps: Apps, label: str, name: str, filters: Mapping[str, object]
    ) -> object:
        """One stored value of a base row, through the historical model."""
        model = apps.get_model(label)
        key = self._legacy(apps, label, filters)
        with _scope(self.organization, self.owner, ""):
            return model._base_manager.filter(pk=key).values_list(name, flat=True).get()

    def _legacy(
        self, apps: Apps, label: str, filters: Mapping[str, object] | None = None
    ) -> object:
        """Primary key of the witness tenant's base row for ``label``."""
        fixed: dict[str, object] = {
            "identity.Organization": self.organization,
            "identity.Clinic": self.clinic,
            "identity.User": self.owner,
            "intake.Patient": self.patient,
            "intake.PatientClinicEnrollment": self.enrollment,
        }
        if label in fixed and not filters:
            return fixed[label]
        if label in self.new:
            message = f"new model {label} has no seeded row to reference"
            raise LookupError(message)
        cache = (label, json.dumps(_plain(dict(filters or {})), sort_keys=True))
        if cache not in self.legacy_rows:
            key = self._first(apps.get_model(label), dict(filters or {}))
            if key is None:
                message = f"no base {label} row to reference"
                raise LookupError(message)
            self.legacy_rows[cache] = key
        return self.legacy_rows[cache]

    def _scoped(self, model: type[Model], filters: Mapping[str, object]) -> object:
        names = {field.name for field in model._meta.concrete_fields}
        rows = model._base_manager.filter(**filters)
        if "organization" in names:
            rows = rows.filter(organization_id=self.organization)
        elif "patient" in names:
            rows = rows.filter(patient_id__in=[self.patient, self.companion_patient])
        elif "user" in names:
            rows = rows.filter(user_id__in=[self.owner, self.physician])
        elif "device" in names:
            rows = rows.filter(device__user_id__in=[self.owner, self.physician])
        return rows.order_by("pk")

    def _first(self, model: type[Model], filters: Mapping[str, object]) -> object:
        """First visible row: owner first, then the runtime role per actor."""
        for actor, role in (
            (self.owner, ""),
            (self.owner, "clinic_app"),
            (self.physician, "clinic_app"),
        ):
            try:
                with _scope(self.organization, actor, role):
                    rows = cast("QuerySet[Model]", self._scoped(model, filters))
                    key = rows.values_list("pk", flat=True).first()
            except DatabaseError:
                continue
            if key is not None:
                return key
        return None

    def _adopt(self, model: type[Model], label: str, seed: Seed, stage: str) -> JsonMap:
        """Record the creating migration's own backfilled row for ``label``."""
        names = [field.attname for field in model._meta.concrete_fields]
        actor = self.physician if seed.actor == "physician" else self.owner
        with _scope(self.organization, actor, LOAD_ROLES.get(label, "")):
            rows = cast("QuerySet[Model]", self._scoped(model, {}))
            stored = rows.values(*names).first()
        if stored is None:
            message = f"{label} has no backfilled row to adopt"
            raise LookupError(message)
        return self._record(model, label, seed, stage, stored)

    def _relation(self, field: Field[object, object], apps: Apps) -> object:
        target = field.related_model
        label = f"{target._meta.app_label}.{target._meta.object_name}"  # type: ignore[union-attr]
        if label == "identity.User":
            # Staff defaults are the witness owner or physician, never a
            # support user another seed created for a specific role.
            physician = any(token in field.name for token in PHYSICIAN_TOKENS)
            return self.physician if physician else self.owner
        return self._context(apps).ref(label)

    def _fill(
        self, model: type[Model], label: str, overrides: Mapping[str, object]
    ) -> JsonMap:
        values: JsonMap = dict(overrides)
        for field in model._meta.concrete_fields:
            if field.name in values or field.attname in values:
                continue
            if field.primary_key and (
                field.has_default() or field.get_internal_type().endswith("AutoField")
            ):
                continue
            if field.is_relation:
                if not field.null:
                    values[field.attname] = self._relation(field, self.state.apps)
            elif _purpose(field) is not None:
                values[field.attname] = _encrypted(field, label)
            elif not field.has_default() and not field.null:
                values[field.attname] = _scalar(field, label, self.now)
        return values

    def _load(
        self,
        model: type[Model],
        label: str,
        seed: Seed,
        overrides: Mapping[str, object],
        stage: str,
    ) -> JsonMap:
        values = self._fill(model, label, overrides)
        names = [field.attname for field in model._meta.concrete_fields]
        actor = self.physician if seed.actor == "physician" else self.owner
        role = LOAD_ROLES.get(label, "")
        if role == "clinic_resolver":
            # Resolver-only tables receive envelopes already sealed on the
            # application side, as the SECURITY DEFINER write path does.
            with _scope(self.organization, actor, ""):
                for field in model._meta.concrete_fields:
                    if _purpose(field) is not None and values.get(field.attname):
                        values[field.attname] = Value(
                            field.get_db_prep_value(values[field.attname], connection),
                            output_field=BinaryField(),
                        )
        pinned = any(
            getattr(field, "auto_now_add", False) or getattr(field, "auto_now", False)
            for field in model._meta.concrete_fields
            if field.attname in overrides or field.name in overrides
        )
        with _scope(self.organization, actor, role):
            if pinned:
                # An explicit timestamp the product binds across rows: keep it
                # instead of letting auto_now restamp this statement.
                instance = model(**values)
                instance.save_base(raw=True, force_insert=True)
                key = instance.pk
            else:
                key = model._base_manager.create(**values).pk
        stored, _raw = _visible(
            model, key, self.organization, names, (), _readers(actor, role, self)
        )
        if stored is None:
            message = f"loaded {label} row is not readable by any reader"
            raise LookupError(message)
        return self._record(model, label, seed, stage, stored)

    def _record(
        self,
        model: type[Model],
        label: str,
        seed: Seed,
        stage: str,
        stored: Mapping[str, object],
    ) -> JsonMap:
        encrypted = {
            field.attname
            for field in model._meta.concrete_fields
            if _purpose(field) is not None
        }
        self.protected.setdefault(label, set()).update(encrypted)
        return {
            "model": label,
            "stage": stage,
            "pk": _plain(stored[model._meta.pk.attname]),
            "role": LOAD_ROLES.get(label, ""),
            "actor": seed.actor,
            "adopted": bool(seed.adopt),
            "suspended_triggers": list(GUARDED_TRIGGERS.get(label, ())),
            "values": {
                name: _recorded(label, name, value) for name, value in stored.items()
            },
            "encrypted": sorted(encrypted),
        }

    def record(self) -> JsonMap:
        """Everything the leaf verifier needs to read each row back."""
        domains: dict[str, dict[str, int]] = {}
        for label, app in self.new.items():
            entry = domains.setdefault(
                app, {"models": 0, "seeded_models": 0, "rows": 0}
            )
            entry["models"] += 1
            count = len(self.seeded.get(label, ()))
            entry["seeded_models"] += 1 if count else 0
            entry["rows"] += count
        return {
            "schema_version": SCHEMA_VERSION,
            "base_nodes": [list(node) for node in self.base_nodes],
            "organization_id": str(self.organization),
            "owner_id": str(self.owner),
            "physician_id": str(self.physician),
            "new_models": len(self.new),
            "domains": domains,
            "stages": self.stages,
            "unseeded": sorted(self.unseeded),
            "rows": [
                {**row, "protected": sorted(self.protected.get(str(row["model"]), ()))}
                for row in self.rows
            ],
        }


@contextmanager
def _scope(organization: UUID, actor: UUID, role: str) -> Iterator[CursorWrapper]:
    """One savepoint with tenant/actor GUCs and an optional role switch."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true), "
            "pg_catalog.set_config('app.current_user_id', %s, true)",
            [str(organization), str(actor)],
        )
        if role:
            cursor.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
        yield cursor
        if role:
            cursor.execute("RESET ROLE")


def _seed_for(label: str) -> Seed | None:
    app, name = label.split(".")
    return DOMAIN_SEEDS.get(app, {}).get(name)


def _stage_labels(models: Mapping[str, type[Model]]) -> list[str]:
    """Return the stage's models plus the base models their seeds load first."""
    labels = list(models)
    for label in models:
        seed = _seed_for(label)
        if seed is not None:
            labels.extend(support for support, _helper in seed.support)
    return sorted(set(labels))


def _transition_labels(models: Mapping[str, type[Model]]) -> list[str]:
    """Earlier rows this stage's seeds move through a product transition."""
    labels: set[str] = set()
    for label in models:
        seed = _seed_for(label)
        if seed is not None:
            labels.update(target for target, _change in seed.updates)
    return sorted(labels)


def _suspend_guard(cursor: CursorWrapper, table: str, name: str, event: str) -> None:
    """Disable one named BEFORE ROW guard of ``event``; refuse anything else."""
    cursor.execute(
        "SELECT tgtype, tgenabled, tgconstraint <> 0 FROM pg_trigger "
        "WHERE tgrelid=%s::regclass AND tgname=%s AND NOT tgisinternal",
        ["clinic_app." + table, name],
    )
    row = cursor.fetchone()
    if row is None:
        _fail(f"allowlisted guard {table}.{name} does not exist at this stage")
    tgtype, enabled, constraint = row
    wanted = ROW_BEFORE | EVENT_BITS[event]
    if constraint or int(tgtype) & wanted != wanted:
        _fail(f"{table}.{name} is not a BEFORE ROW {event} guard; it stays active")
    if enabled != "O":
        _fail(f"{table}.{name} was already disabled before the stage")
    cursor.execute(
        sql.SQL("ALTER TABLE clinic_app.{} DISABLE TRIGGER {}").format(
            sql.Identifier(table), sql.Identifier(name)
        )
    )


def disabled_triggers(cursor: CursorWrapper) -> set[tuple[str, str]]:
    """Every disabled user trigger in the application schema."""
    cursor.execute(
        "SELECT c.relname, t.tgname FROM pg_trigger t "
        "JOIN pg_class c ON c.oid=t.tgrelid "
        "WHERE c.relnamespace='clinic_app'::regnamespace "
        "AND NOT t.tgisinternal AND t.tgenabled<>'O'"
    )
    return {(str(table), str(name)) for table, name in cursor.fetchall()}


def _require_no_disabled(cursor: CursorWrapper, allowed: set[tuple[str, str]]) -> None:
    found = disabled_triggers(cursor)
    if found != allowed:
        unexpected = sorted(f"{t}.{n}" for t, n in found ^ allowed)
        _fail("trigger posture differs from the allowlist: " + ", ".join(unexpected))


def _recorded(label: str, name: str, value: object) -> object:
    """Record a value, keeping secret material as a digest only."""
    if f"{label}.{name}" in SECRET_COLUMNS:
        raw = bytes(value) if isinstance(value, (bytes, memoryview)) else value
        content = raw if isinstance(raw, bytes) else json.dumps(_plain(raw)).encode()
        return "sha256:" + sha256(content).hexdigest()
    lowered = name.lower()
    if (
        any(token in lowered for token in SECRET_TOKENS)
        and not lowered.endswith(DIGEST_SUFFIXES)
        and isinstance(value, (bytes, memoryview, str))
        and value
    ):
        message = f"{label}.{name} looks secret-bearing but is not classified"
        raise LookupError(message)
    return _plain(value)


def _dependency_order(models: Mapping[str, type[Model]]) -> list[str]:
    """Order one stage's models so foreign-key targets load first."""
    pending = dict(models)
    ordered: list[str] = []
    while pending:
        ready = [
            label
            for label, model in sorted(pending.items())
            if not any(
                earlier in pending for earlier in getattr(_seed_for(label), "after", ())
            )
            and not any(
                f"{field.related_model._meta.app_label}."  # type: ignore[union-attr]
                f"{field.related_model._meta.object_name}"  # type: ignore[union-attr]
                in pending
                and field.related_model is not model
                and not field.null
                for field in model._meta.concrete_fields
                if field.is_relation
            )
        ]
        if not ready:
            _fail("cyclic required foreign keys among one stage's new models")
        ordered.extend(ready)
        for label in ready:
            del pending[label]
    return ordered


class StagedMigrate(migrate.Command):
    """The stock ``migrate`` command with the stager on its progress hook."""

    def __init__(self, stager: Stager) -> None:
        """Bind the stager that runs after each applied migration."""
        super().__init__()
        self.stager = stager

    def migration_progress_callback(
        self, action: str, migration: Migration | None = None, fake: bool = False
    ) -> None:
        """Report progress, then seed whatever this migration first added."""
        super().migration_progress_callback(action, migration, fake)
        if action == "apply_success" and migration is not None:
            if fake:
                _fail("staged upgrade never fakes a migration")
            self.stager.applied(migration)


def _column(model: type[Model], attname: str) -> str:
    field = next(f for f in model._meta.concrete_fields if f.attname == attname)
    return str(field.column)


def _readers(
    actor: UUID, role: str, identities: Stager | tuple[UUID, UUID]
) -> tuple[tuple[UUID, str], ...]:
    """Who may read a staged row: its loader first, then every other reader."""
    owner, physician = (
        identities
        if isinstance(identities, tuple)
        else (identities.owner, identities.physician)
    )
    return (
        (actor, role),
        (owner, ""),
        (owner, "clinic_app"),
        (physician, "clinic_app"),
        (owner, "clinic_resolver"),
    )


def _visible(  # noqa: PLR0913 - one row, its tenant and its readers
    model: type[Model],
    key: object,
    organization: UUID,
    names: list[str],
    encrypted: Iterable[str],
    attempts: Iterable[tuple[UUID, str]],
) -> tuple[Mapping[str, object] | None, dict[str, object]]:
    """Read one row, first as it was loaded, then as the other readers.

    A later policy may narrow who sees a row without touching it, and a
    resolver-only table may be readable only through the runtime policy;
    only a row no reader can see (or decrypt) counts as missing.
    """
    from apps.tenancy.envelope import EnvelopeError  # noqa: PLC0415

    for actor, role in attempts:
        try:
            with _scope(organization, actor, role) as cursor:
                stored = model._base_manager.filter(pk=key).values(*names).first()
                raw: dict[str, object] = {}
                for name in encrypted if stored is not None else ():
                    cursor.execute(
                        sql.SQL("SELECT {} FROM clinic_app.{} WHERE {} = %s").format(
                            sql.Identifier(_column(model, name)),
                            sql.Identifier(model._meta.db_table),
                            sql.Identifier(_column(model, model._meta.pk.attname)),
                        ),
                        [cast("int | str", key)],
                    )
                    raw[name] = cast("tuple[object]", cursor.fetchone())[0]
        except (DatabaseError, EnvelopeError):
            continue
        if stored is not None:
            return stored, raw
    return None, {}


def envelope_failures(
    label: str, name: str, raw: object, seeded: object, *, leaf_protected: bool
) -> list[str]:
    """Require ciphertext for a column protected in any crossed state."""
    if not isinstance(raw, (bytes, memoryview)):
        return [f"{label}.{name} is not stored as a ciphertext envelope"]
    failures = []
    if any(token in bytes(raw) for token in _plaintext_tokens(seeded)):
        failures.append(f"{label}.{name} stores readable plaintext")
    if not leaf_protected:
        failures.append(f"{label}.{name} lost its envelope protection at the leaf")
    return failures


def protected_columns(
    row: Mapping[str, object], leaf_columns: set[str]
) -> tuple[list[str], int]:
    """Columns protected in any crossed state with a value, and the empty count.

    The set is the union of every state's protected fields the stager saw,
    never the leaf model's alone: a column that was an envelope and is now a
    plain leaf field is still checked (and fails).
    """
    values = cast("dict[str, object]", row["values"])
    protected = set(cast("list[str]", row.get("protected", []))) | set(
        cast("list[str]", row["encrypted"])
    )
    checked = sorted(
        name
        for name in protected
        if name in leaf_columns and values.get(name) not in (None, "")
    )
    return checked, sum(1 for name in protected if values.get(name) in (None, ""))


def _read_back(
    apps: Apps, row: Mapping[str, object], owner: UUID, physician: UUID
) -> tuple[list[str], int, int]:
    """Failures, non-empty protected values checked, empty protected values."""
    label = str(row["model"])
    model = apps.get_model(label)
    fields = {field.attname: field for field in model._meta.concrete_fields}
    values = cast("dict[str, object]", row["values"])
    failures = [
        f"{label}.{name} no longer exists at the leaf"
        for name in values
        if name not in fields and f"{label}.{name}" not in PRESERVATION_EXCEPTIONS
    ]
    names = [name for name in values if name in fields]
    checked, empty = protected_columns(row, set(fields))
    actor = physician if row["actor"] == "physician" else owner
    stored, raw = _visible(
        model,
        row["pk"],
        UUID(str(row["organization_id"])),
        names,
        checked,
        _readers(actor, str(row["role"]), (owner, physician)),
    )
    if stored is None:
        failures.append(f"seeded {label} row {row['pk']} is missing at the leaf")
        return failures, len(checked), empty
    failures.extend(
        f"{label}.{name} changed across the upgrade"
        for name in names
        if _recorded(label, name, stored[name]) != values[name]
        and f"{label}.{name}" not in PRESERVATION_EXCEPTIONS
    )
    for name in checked:
        failures.extend(
            envelope_failures(
                label,
                name,
                raw[name],
                values[name],
                leaf_protected=_purpose(fields[name]) is not None,
            )
        )
    return failures, len(checked), empty


def _plaintext_tokens(value: object) -> Iterator[bytes]:
    if isinstance(value, str):
        if value.startswith("bytes:"):
            value = bytes.fromhex(value.removeprefix("bytes:")).decode(
                "utf-8", "replace"
            )
        if len(value) >= len("Synthetic"):
            yield value.encode()
    elif isinstance(value, dict):
        for item in value.values():
            yield from _plaintext_tokens(item)
    elif isinstance(value, list):
        for item in value:
            yield from _plaintext_tokens(item)


def coverage_failures(record: Mapping[str, object]) -> list[str]:
    """Every model the graph adds after the recorded base needs a seeded row."""
    if record.get("schema_version") != SCHEMA_VERSION:
        return ["domain seed record is missing or has an unknown schema"]
    base = {
        (str(app), str(name))
        for app, name in cast("list[list[object]]", record["base_nodes"])
    }
    if not base:
        return ["no base migration state recorded: refusing a blank database"]
    loader = MigrationLoader(None, ignore_no_migrations=True)
    if not base <= set(loader.graph.nodes):
        return ["recorded base migration state is not in the candidate graph"]
    seeded = {str(row["model"]) for row in cast("list[JsonMap]", record["rows"])}
    return [
        f"domain {app}: new model {label} has no seeded row"
        for label, app in sorted(new_models(loader, base).items())
        if label not in seeded
    ]


def invariant_failures(record: Mapping[str, object]) -> list[str]:
    """Run the domains' cross-row invariants over the seeded tenant."""
    import psycopg  # noqa: PLC0415 - verifier-only raw catalog access

    from ops.testing.migration_upgrade_witness import super_dsn  # noqa: PLC0415

    failures = []
    with psycopg.connect(super_dsn()) as raw:
        for name, statement in LEAF_INVARIANTS.items():
            found = raw.execute(
                statement, {"organization": str(record["organization_id"])}
            ).fetchone()
            count = int(cast("tuple[int]", found)[0])
            if count:
                failures.append(f"invariant {name} violated by {count} row(s)")
    return failures


def verify_stages(path: Path) -> list[str]:
    """Leaf readback: every new model seeded, preserved and still encrypted."""
    from django.apps import apps  # noqa: PLC0415 - leaf registry, Django-ready

    record = cast("JsonMap", json.loads(path.read_text("utf-8")))
    failures = coverage_failures(record)
    if record.get("schema_version") != SCHEMA_VERSION:
        return failures
    with connection.cursor() as cursor:
        failures.extend(
            f"user trigger {table}.{name} is disabled at the leaf"
            for table, name in sorted(disabled_triggers(cursor))
        )
    owner = UUID(str(record["owner_id"]))
    physician = UUID(str(record["physician_id"]))
    rows = cast("list[JsonMap]", record["rows"])
    checked = empty = 0
    for row in rows:
        bound = {**row, "organization_id": record["organization_id"]}
        found, row_checked, row_empty = _read_back(apps, bound, owner, physician)
        failures.extend(found)
        checked += row_checked
        empty += row_empty
    failures.extend(invariant_failures(record))
    print(  # noqa: T201 - gate transcript
        f"upgrade-domains: read back {len(rows)} staged rows across "
        f"{len({row['model'] for row in rows})} models; protected columns: "
        f"{checked} non-empty checked as ciphertext, {empty} empty; "
        f"{len(LEAF_INVARIANTS)} cross-row invariants"
    )
    return failures


def main() -> int:
    """Run the staged candidate migration and publish the seed record."""
    markers = json.loads(Path(os.environ["UPGRADE_SEED_FILE"]).read_text("utf-8"))
    stager = Stager(markers)
    call_command(StagedMigrate(stager), interactive=False, verbosity=1)
    record = stager.record()
    with Path(os.environ["UPGRADE_DOMAIN_SEED_FILE"]).open(
        "x", encoding="utf-8"
    ) as out:
        json.dump(record, out, indent=2, sort_keys=True)
        out.write("\n")
    print(  # noqa: T201 - gate transcript
        f"staged {len(stager.rows)} rows into "
        f"{sum(1 for label in stager.new if stager.seeded.get(label))}/"
        f"{len(stager.new)} new models; unseeded: {len(stager.unseeded)}"
    )
    return 0


if __name__ == "__main__":
    import django

    django.setup()
    sys.exit(main())
