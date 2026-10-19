"""Observe EHR permission delegations after their real subject/assignment gates."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from apps.ehr import addenda, episodes, services, views
from apps.ehr.models import (
    ClinicalDocument,
    ClinicalDocumentVersion,
    Encounter,
    EncounterAddendum,
    Episode,
)
from apps.identity.models import (
    CareTeamMembership,
    ProfessionalRegistration,
    RoleGrant,
    UserClinicRole,
)
from apps.intake.models import PatientClinicEnrollment
from django.utils import timezone

from identity.delegation_probes import _as_owner
from identity.nonstaff_differential import DifferentialProbe
from identity.permission_support import owner_context

if TYPE_CHECKING:
    from collections.abc import Callable

    from identity.nonstaff_subjects import NonstaffSubjects
    from rbac_fixtures import RbacGraph

SYMBOLS = frozenset(
    {
        "apps.ehr.addenda._authorized",
        "apps.ehr.addenda.open_addendum",
        "apps.ehr.episodes._require",
        "apps.ehr.episodes.close_episode",
        "apps.ehr.episodes.encounter_episodes",
        "apps.ehr.episodes.link_encounter",
        "apps.ehr.episodes.open_episode_for_encounter",
        "apps.ehr.services.open_unscheduled_encounter",
        "apps.ehr.views._unscheduled_options",
    }
)


def ehr_probes(data: NonstaffSubjects, graph: RbacGraph) -> list[DifferentialProbe]:
    """Reach the deciding permission with valid subjects and real subtractions.

    An unassigned actor would be refused before these compound delegations.
    Trusted preparation therefore satisfies assignment/registration, then
    subtracts each deciding permission. Every observed invocation still runs as
    clinic_app and is refused by the real permission resolver or its RLS policy.
    The scenario transaction rolls all prepared membership and clinical rows back.
    """
    calls: dict[str, Callable[[], object]] = {}

    def prepare() -> None:
        now = timezone.now()
        with _as_owner(), owner_context(graph.organization_a):
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                user=data.actor,
                role="physician",
            )
            enrolled = PatientClinicEnrollment.objects.get(pk=data.op.enrollment)
            ProfessionalRegistration.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                user=data.actor,
                role="physician",
                council="CRM",
                number="SINTETICO-EHR-CENSUS",
                jurisdiction="SP",
                specialty="Sintetico",
                status="regular",
                valid_from=now - timedelta(days=1),
                valid_to=now + timedelta(days=1),
            )
            CareTeamMembership.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                patient_enrollment=enrolled,
                user=data.actor,
                role="physician",
                valid_from=now - timedelta(days=1),
                valid_to=now + timedelta(days=1),
            )
            visit = Encounter.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                patient_id=enrolled.patient_id,
                physician_id=data.actor.pk,
                appointment=None,
                unscheduled_reason="documentation_only",
            )
            document = ClinicalDocument.objects.create(
                organization_id=graph.organization_a,
                encounter=visit,
                kind="soap",
            )
            ClinicalDocumentVersion.objects.create(
                organization_id=graph.organization_a,
                document=document,
                template=data.legacy.template,
                author=data.actor,
                version=1,
            )
            episode = Episode.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                patient_id=enrolled.patient_id,
                title="Sintetico census",
                opened_by=data.actor,
            )
            addendum = EncounterAddendum.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                encounter_id=data.legacy.encounter,
                patient_id=data.legacy.appointment.patient_id,
                author=data.actor,
            )
            for permission in (
                "clinical.read",
                "clinical.write",
                "encounter.open_unscheduled",
            ):
                RoleGrant.objects.create(
                    organization_id=graph.organization_a,
                    clinic_id=graph.clinic_a,
                    role="physician",
                    permission=permission,
                    bundle_version=2 if permission.startswith("encounter.") else 1,
                    valid_from=now - timedelta(days=1),
                )
        calls.update(
            {
                "apps.ehr.addenda._authorized": lambda: addenda._authorized(
                    graph.clinic_a, addendum.pk
                ),
                "apps.ehr.addenda.open_addendum": lambda: addenda.open_addendum(
                    clinic_id=graph.clinic_a, encounter_id=data.legacy.encounter
                ),
                "apps.ehr.episodes._require": lambda: episodes._require(
                    "clinical.write", graph.clinic_a, enrolled.pk
                ),
                "apps.ehr.episodes.close_episode": lambda: episodes.close_episode(
                    clinic_id=graph.clinic_a, episode_id=episode.pk
                ),
                "apps.ehr.episodes.encounter_episodes": (
                    lambda: episodes.encounter_episodes(
                        clinic_id=graph.clinic_a, encounter_id=visit.pk
                    )
                ),
                "apps.ehr.episodes.link_encounter": lambda: episodes.link_encounter(
                    clinic_id=graph.clinic_a,
                    encounter_id=visit.pk,
                    episode_id=episode.pk,
                ),
                "apps.ehr.episodes.open_episode_for_encounter": (
                    lambda: episodes.open_episode_for_encounter(
                        clinic_id=graph.clinic_a,
                        encounter_id=visit.pk,
                        title="Sintetico refused",
                    )
                ),
                "apps.ehr.services.open_unscheduled_encounter": (
                    lambda: services.open_unscheduled_encounter(
                        clinic_id=graph.clinic_a,
                        enrollment_id=enrolled.pk,
                        reason="walk_in",
                    )
                ),
                "apps.ehr.views._unscheduled_options": (
                    lambda: views._unscheduled_options(graph.clinic_a)
                ),
            }
        )
        assert set(calls) == SYMBOLS

    probes = []
    for symbol in sorted(SYMBOLS):

        def call(symbol: str = symbol) -> object:
            return calls[symbol]()

        probes.append(
            DifferentialProbe(
                symbol,
                call,
                decision=lambda result: result is not None,
                expected=False,
                prepare=prepare,
            )
        )
    return probes
