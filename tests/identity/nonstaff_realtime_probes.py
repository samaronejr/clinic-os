"""Realtime adapters for the behavioural classification census (todo 8).

The lease serializer is an infrastructure row, certified only by observation
plus the complete staff-state backstop. Patient topics and the invalidation
hook read authority relations, so they are observed boundaries whose executed
decisions (grant/denial, exact invalidated subjects) are compared exactly.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

from apps.identity.models import RoleGrant, UserClinicRole
from apps.realtime import authorization, hooks, scopes
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from identity.nonstaff_differential import DifferentialProbe
from identity.permission_support import owner_context

if TYPE_CHECKING:
    from functools import partial

    from identity.nonstaff_subjects import NonstaffSubjects


def _patient_topic(d: NonstaffSubjects, enrollment: UUID) -> bool:
    try:
        subscription = authorization._patient_topics(
            "synthetic-realtime-census",
            d.op.patient_session,
            (f"patient:{enrollment}:booking",),
            timezone.now() + timedelta(days=1),
        )
    except authorization.TopicDeniedError:
        return False
    return subscription.patient_session_id == d.op.patient_session


def _schedule_scope(clinic: UUID, subject: UUID) -> bool:
    # Enabled only inside the probe's rolled-back transaction: the Redis store
    # is an on_commit callback, so no broker or host Redis is ever reached.
    with override_settings(REALTIME_ENABLED=True):
        before = len(connection.run_on_commit)
        scopes._schedule_scope(
            f"clinic:{clinic}:inbox",
            subject,
            clinic,
            "appointment.read",
            None,
        )
        return len(connection.run_on_commit) == before + 1


def _invalidated(clinic: UUID, role: str) -> set[str]:
    with override_settings(REALTIME_ENABLED=True):
        before = len(connection.run_on_commit)
        hooks.permission_changed(RoleGrant, RoleGrant(clinic_id=clinic, role=role))
        # Each scheduled entry is (savepoints, callback[, robust]); the callback
        # is transport.publish bound to its exact keyword topic.
        return {
            cast("partial[None]", entry[1]).keywords["topic"]
            for entry in connection.run_on_commit[before:]
        }


def _members(d: NonstaffSubjects, clinic: UUID, role: str) -> set[str]:
    with owner_context(d.legacy.graph.organization_a):
        return {
            f"authz:user:{user_id}"
            for user_id in UserClinicRole.objects.filter(
                clinic_id=clinic, role=role
            ).values_list("user_id", flat=True)
        }


def realtime_probes(d: NonstaffSubjects) -> list[DifferentialProbe]:
    clinic = d.legacy.clinic
    # Fixture values are resolved outside the observed call; reading the
    # actor model's id inside it would itself count as an authority touch.
    subject = d.legacy.graph.shared_user
    # Expected subjects are read as the owner before any observed invocation.
    physicians = _members(d, clinic, UserClinicRole.Role.PHYSICIAN)
    foreign = _members(d, d.legacy.graph.clinic_c, UserClinicRole.Role.PHYSICIAN)
    assert physicians, "invalidation oracle needs a non-empty member set"
    return [
        DifferentialProbe(
            "apps.realtime.authorization._patient_topics",
            lambda: _patient_topic(d, d.op.enrollment),
            owns_transaction=True,
            patient_session=d.op.patient_session,
        ),
        DifferentialProbe(
            "apps.realtime.authorization._patient_topics",
            lambda: _patient_topic(d, uuid4()),
            expected=False,
            owns_transaction=True,
            patient_session=d.op.patient_session,
        ),
        DifferentialProbe(
            "apps.realtime.scopes._schedule_scope",
            lambda: _schedule_scope(clinic, subject),
        ),
        DifferentialProbe(
            "apps.realtime.hooks.permission_changed",
            lambda: _invalidated(clinic, UserClinicRole.Role.PHYSICIAN) == physicians,
        ),
        DifferentialProbe(
            "apps.realtime.hooks.permission_changed",
            lambda: (
                _invalidated(d.legacy.graph.clinic_c, UserClinicRole.Role.PHYSICIAN)
                == foreign
            ),
        ),
    ]
