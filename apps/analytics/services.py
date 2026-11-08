"""Authorized catalog queries with immutable, permission-versioned read models."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from django.db import DatabaseError, connection, transaction
from django.utils import timezone
from psycopg import Error as PostgresError

from apps.analytics.catalog import METRICS
from apps.analytics.contracts import (
    AnalyticsAccessDeniedError,
    AnalyticsConflictError,
    Query,
    Result,
    parse_descriptor,
    render_value,
)
from apps.analytics.models import MetricDefinitionVersion, ReadModelRefresh, SavedReport
from apps.audit.services import record_phase1_event
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.models import Clinic

if TYPE_CHECKING:
    from uuid import UUID


def authorize(*, clinic_id: UUID, metric_id: str = "no_show") -> tuple[UUID, str]:
    """Authorize before reading a numerator, denominator, cache or source count."""
    try:
        actor = require_permission("charge.read", clinic_id=clinic_id)
    except CurrentActorError as error:
        raise AnalyticsAccessDeniedError from error
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.analytics_allowed(%s,%s)", [clinic_id, metric_id]
        )
        if cursor.fetchone() != (True,):
            raise AnalyticsAccessDeniedError
        cursor.execute("SELECT clinic_app.analytics_authorization(%s)", [clinic_id])
        row = cursor.fetchone()
    if row is None:
        raise AnalyticsAccessDeniedError
    return actor, str(row[0])


def _checked(clinic_id: UUID, query: Query) -> tuple[UUID, str]:
    actor, version = authorize(clinic_id=clinic_id, metric_id=query.metric_id)
    parse_descriptor(query.encode())
    if not Clinic.objects.filter(pk=clinic_id, timezone=query.tz).exists():
        raise AnalyticsAccessDeniedError
    return actor, version


def _result(snapshot: ReadModelRefresh, query: Query) -> Result:
    rows: list[dict[str, str | None]] = []
    for row in snapshot.rows:
        value = (
            None
            if snapshot.quality == "unavailable"
            else render_value(query.metric_id, row["numerator"], row["denominator"])
        )
        rows.append({**row, "value": value})
    status = "fresh"
    if all(row["value"] is None for row in rows):
        status = "null"
    if snapshot.quality != "complete":
        status = "partial"
    if timezone.now() - snapshot.as_of > timedelta(seconds=900):
        status = "stale"
    return Result(
        snapshot.pk,
        query,
        snapshot.authorization_version,
        snapshot.as_of,
        tuple(rows),
        status,
    )


def query_metric(*, clinic_id: UUID, query: Query) -> Result | None:
    """Return only a cache entry for the current effective authorization version."""
    actor, version = _checked(clinic_id, query)
    snapshot = (
        ReadModelRefresh.objects.filter(
            clinic_id=clinic_id,
            user_id=actor,
            authorization_version=version,
            descriptor=query.payload(),
        )
        .order_by("-as_of", "-id")
        .first()
    )
    return _result(snapshot, query) if snapshot is not None else None


@transaction.atomic
def refresh_metric(*, clinic_id: UUID, query: Query, command_id: UUID) -> Result:
    """Refresh by parameterized fixed SQL; a workflow step uses its UUID as command."""
    _checked(clinic_id, query)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clinic_app.analytics_refresh(%s,%s::jsonb,%s,%s,%s)",
                [clinic_id, query.encode(), command_id, query.start, query.end],
            )
            receipt = cursor.fetchone()
    except DatabaseError as error:
        cause = error.__cause__
        if isinstance(cause, PostgresError):
            if cause.sqlstate == "42501":
                raise AnalyticsAccessDeniedError from error
            if cause.sqlstate == "23514":
                raise AnalyticsConflictError from error
        raise
    if receipt is None:
        raise AnalyticsAccessDeniedError
    snapshot = ReadModelRefresh.objects.get(pk=receipt[0], clinic_id=clinic_id)
    if receipt[2]:
        record_phase1_event(
            "analytics.definition.registered",
            clinic_id=clinic_id,
            affected_record_id=snapshot.definition_id,
        )
    if receipt[1]:
        record_phase1_event(
            "analytics.read_model.refreshed",
            clinic_id=clinic_id,
            affected_record_id=snapshot.pk,
        )
    return _result(snapshot, query)


def drill_down(*, clinic_id: UUID, query: Query) -> tuple[dict[str, object], ...]:
    """Return bounded operational references only after the same metric permission."""
    _checked(clinic_id, query)
    if query.metric_id != "no_show":
        return ()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT record_id,occurred_at,state FROM clinic_app.analytics_metricfact "
            "WHERE clinic_id=%s AND metric_id=%s "
            "AND occurred_at>=%s AND occurred_at<%s "
            "AND (%s::int[]='{}'::int[] OR "
            "(extract(isodow FROM occurred_at AT TIME ZONE %s)::int+6)%%7=ANY(%s)) "
            "ORDER BY occurred_at,record_id LIMIT 100",
            [
                clinic_id,
                query.metric_id,
                query.start,
                query.end,
                list(query.weekdays),
                query.tz,
                list(query.weekdays),
            ],
        )
        return tuple(
            {"reference": str(row[0]), "occurred_at": row[1], "state": row[2]}
            for row in cursor.fetchall()
        )


@transaction.atomic
def save_report(*, clinic_id: UUID, query: Query) -> SavedReport:
    """Save governed filters; re-opening always rechecks current authority."""
    actor, _version = _checked(clinic_id, query)
    previous = SavedReport.objects.filter(
        clinic_id=clinic_id, user_id=actor, descriptor=query.payload()
    ).first()
    if previous is not None:
        return previous
    definition = MetricDefinitionVersion.objects.filter(
        clinic_id=clinic_id,
        metric_id=query.metric_id,
        version=1,
    ).first()
    if definition is None:
        refresh_metric(clinic_id=clinic_id, query=query, command_id=uuid4())
        definition = MetricDefinitionVersion.objects.get(
            clinic_id=clinic_id,
            metric_id=query.metric_id,
            version=1,
        )
    saved = SavedReport.objects.create(
        organization_id=definition.organization_id,
        clinic_id=clinic_id,
        user_id=actor,
        definition=definition,
        descriptor=query.payload(),
        window_start=query.start,
        window_end=query.end,
    )
    record_phase1_event(
        "analytics.report.saved", clinic_id=clinic_id, affected_record_id=saved.pk
    )
    return saved


def load_report(*, clinic_id: UUID, report_id: UUID) -> Query:
    """Reopen a saved preference without retaining its former authority."""
    actor, _version = authorize(clinic_id=clinic_id)
    saved = SavedReport.objects.filter(
        pk=report_id, clinic_id=clinic_id, user_id=actor
    ).first()
    if saved is None:
        raise AnalyticsAccessDeniedError
    query = parse_descriptor(json.dumps(saved.descriptor))
    _checked(clinic_id, query)
    return query


def require_snapshot(
    *, clinic_id: UUID, query: Query, authorization_version: str
) -> None:
    """Bind human approval and deferred AI work to current permission semantics."""
    _actor, current = _checked(clinic_id, query)
    if current != authorization_version:
        raise AnalyticsConflictError


def definitions(*, clinic_id: UUID) -> tuple[dict[str, object], ...]:
    """Return reviewed definitions only after reporting authority."""
    authorize(clinic_id=clinic_id)
    return tuple(
        {"label": metric.label, **metric.specification()} for metric in METRICS.values()
    )
