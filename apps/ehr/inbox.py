"""Inbox feed for the actor's own SOAP drafts awaiting finalization."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connection
from django.db.models.signals import post_save
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

from apps.ehr.models import ClinicalDocumentVersion, Encounter
from apps.workflows.inbox import (
    InboxItem,
    due_before,
    publish_inbox_hint,
    register_inbox_feed,
)

if TYPE_CHECKING:
    from uuid import UUID

    from apps.workflows.inbox import InboxScope


def _assigned(encounters: set[UUID]) -> set[UUID]:
    """Decide authorship with the same predicate every clinical policy uses."""
    if not encounters:
        return set()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id FROM pg_catalog.unnest(%s::uuid[]) AS id "
            "WHERE clinic_app.ehr_assigned(id)",
            [sorted(encounters)],
        )
        return {row[0] for row in cursor.fetchall()}


def draft_items(scope: InboxScope) -> tuple[InboxItem, ...]:
    """Return drafts the actor authors on encounters still assigned to them.

    Every draft is the actor's own and has no due time, so the owner filter
    keeps all of them and a due filter matches none.
    """
    if due_before(scope) is not None:
        return ()
    own = ClinicalDocumentVersion.objects.filter(
        state=ClinicalDocumentVersion.State.DRAFT,
        author_id=scope.actor_id,
        document__encounter__clinic_id=scope.clinic_id,
        document__encounter__physician_id=scope.actor_id,
    )
    assigned = _assigned(
        set(own.order_by().values_list("document__encounter_id", flat=True))
    )
    drafts = (
        own.filter(document__encounter_id__in=assigned)
        .select_related("document__encounter__patient")
        .order_by("created_at", "pk")[: scope.limit]
    )
    return tuple(
        InboxItem(
            kind="draft",
            record_id=draft.pk,
            target_id=draft.document.encounter_id,
            revision=draft.revision,
            title=(
                _("Amendment awaiting finalization")
                if draft.amendment_of_id
                else _("Note awaiting finalization")
            ),
            patient_label=draft.document.encounter.patient.full_name,
            action="open_encounter",
        )
        for draft in drafts
    )


def draft_changed(
    sender: type[ClinicalDocumentVersion],
    instance: ClinicalDocumentVersion,
    **kwargs: object,
) -> None:
    """Hint subscribed inboxes after a draft is created or finalized."""
    del sender, kwargs
    clinic = (
        Encounter.objects.filter(clinicaldocument__id=instance.document_id)
        .values_list("clinic_id", flat=True)
        .first()
    )
    if clinic is not None:
        publish_inbox_hint(clinic)


def register() -> None:
    """Register the draft feed and its commit-bound refetch hint."""
    register_inbox_feed(
        "draft", draft_items, label=gettext_noop("Notes to finalize"), position=20
    )
    post_save.connect(
        draft_changed,
        sender=ClinicalDocumentVersion,
        dispatch_uid="ehr.inbox.draft",
    )
