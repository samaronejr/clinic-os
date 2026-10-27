"""Offline, checksum-bound catalog imports and private retained favorites."""

from __future__ import annotations

import json
from hashlib import sha256
from io import StringIO
from typing import TYPE_CHECKING

import pytest
from apps.prescription.catalog import catalog_entries, set_favorite
from apps.prescription.models import (
    MedicationCatalogEntry,
    MedicationCatalogImport,
    MedicationFavorite,
    PrescriptionDraft,
    PrescriptionItem,
)
from apps.prescription.policy import MAX_ITEMS
from apps.prescription.services import create_draft
from apps.tenancy.db import tenant_context
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, transaction

from patient_service_support import runtime_role
from renewal.test_encounters import physician_client
from renewal.test_prescription_drafts import payload, seed

if TYPE_CHECKING:
    from pathlib import Path

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def catalog_file(
    tmp_path: Path,
    *,
    description: str = "Synthetic medicine",
    retrieved_on: str = "2026-10-20",
) -> Path:
    source = tmp_path / "catalog.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_url": "https://www.gov.br/anvisa/pt-br/synthetic-cmed.json",
                "retrieved_on": retrieved_on,
                "entries": [
                    {
                        "code": "SYNTHETIC-001",
                        "description": description,
                        "strength_form": "Synthetic 10 mg tablet",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return source


def load_catalog(source: Path) -> None:
    call_command(
        "load_medication_catalog",
        source=str(source),
        sha256=sha256(source.read_bytes()).hexdigest(),
        stdout=StringIO(),
    )


def test_import_is_schema_checked_checksum_bound_and_repeatable(tmp_path: Path) -> None:
    # Given: a local, normalized open-data snapshot with explicit provenance.
    source = catalog_file(tmp_path)
    load_catalog(source)
    # When: the same refresh job is run again.
    load_catalog(source)
    # Then: no duplicate batch or catalog rows are produced.
    batch = MedicationCatalogImport.objects.get()
    assert batch.checksum == sha256(source.read_bytes()).hexdigest()
    assert batch.source_url.startswith("https://www.gov.br/anvisa/")
    assert batch.retrieved_on.isoformat() == "2026-10-20"
    assert MedicationCatalogEntry.objects.get().description == "Synthetic medicine"


@pytest.mark.parametrize("defect", ["checksum", "schema", "classification", "url"])
def test_invalid_import_writes_no_partial_batch(tmp_path: Path, defect: str) -> None:
    # Given: one defect at the file trust boundary.
    source = catalog_file(tmp_path)
    raw = json.loads(source.read_text(encoding="utf-8"))
    if defect == "schema":
        del raw["entries"][0]["description"]
    if defect == "classification":
        raw["entries"][0]["category"] = "simple"
    if defect == "url":
        raw["source_url"] = "https://example.invalid/catalog"
    source.write_text(json.dumps(raw), encoding="utf-8")
    digest = (
        "0" * 64 if defect == "checksum" else sha256(source.read_bytes()).hexdigest()
    )
    # When / Then: the owner command rejects before any batch or entry exists.
    with pytest.raises(CommandError):
        call_command(
            "load_medication_catalog",
            source=str(source),
            sha256=digest,
            stdout=StringIO(),
        )
    assert not MedicationCatalogImport.objects.exists()
    assert not MedicationCatalogEntry.objects.exists()


def test_favorites_are_issuer_scoped_and_removed_without_deletion(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    # Given: an imported public entry, with no patient content or class.
    load_catalog(catalog_file(tmp_path))
    entry = MedicationCatalogEntry.objects.get()
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        # When: the physician explicitly favorites, removes and restores it.
        favorite = set_favorite(
            clinic_id=rbac_graph.clinic_a, entry_id=entry.pk, active=True
        )
        set_favorite(clinic_id=rbac_graph.clinic_a, entry_id=entry.pk, active=False)
        restored = set_favorite(
            clinic_id=rbac_graph.clinic_a, entry_id=entry.pk, active=True
        )
        # Then: the same retained row is reused and cannot be hard-deleted.
        assert favorite.pk == restored.pk
        with pytest.raises(DatabaseError), transaction.atomic():
            MedicationFavorite.objects.filter(pk=favorite.pk).delete()
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        assert not MedicationFavorite.objects.exists()
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_b),
    ):
        assert not MedicationFavorite.objects.exists()


def test_refresh_retains_old_entries_but_searches_only_the_latest_batch(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    # Given: two immutable source snapshots.
    load_catalog(catalog_file(tmp_path))
    old = MedicationCatalogEntry.objects.get()
    load_catalog(
        catalog_file(
            tmp_path,
            description="Synthetic refreshed medicine",
            retrieved_on="2026-10-21",
        )
    )
    # When: the physician searches the active snapshot.
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        rows = catalog_entries(clinic_id=rbac_graph.clinic_a, query="refreshed")
        # Then: the refresh is visible without rewriting historical references.
        assert [row.description for row in rows] == ["Synthetic refreshed medicine"]
        assert (
            MedicationCatalogEntry.objects.get(pk=old.pk).description
            == "Synthetic medicine"
        )
        with pytest.raises(DatabaseError), transaction.atomic():
            MedicationCatalogEntry.objects.filter(pk=old.pk).update(
                description="forged"
            )


def test_runtime_cannot_import_or_mutate_public_reference_data(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    # Given: a valid staged snapshot but an unprivileged runtime connection.
    source = catalog_file(tmp_path)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        # When / Then: a caller cannot turn runtime authority into an owner import.
        with pytest.raises(CommandError):
            load_catalog(source)
        with pytest.raises(DatabaseError), transaction.atomic():
            MedicationCatalogImport.objects.create(
                source_url="https://www.gov.br/anvisa/pt-br/synthetic.json",
                retrieved_on="2026-10-20",
                checksum="0" * 64,
            )


def test_catalog_search_preserves_partial_input_without_save_errors_or_class(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    # Given: a partially entered medication, not a request to save or classify it.
    load_catalog(catalog_file(tmp_path))
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope)
    url = f"/prescription/clinics/{rbac_graph.clinic_a}/draft/"
    with physician_client(rbac_graph) as client:
        client.post(url, {"action": "open", "encounter_id": scope["encounter_id"]})
        data = {
            **payload(scope, draft),
            "category": "",
            "action": "catalog_search",
            "catalog_query": "Synthetic",
            "items-0-strength_form": "",
            "items-0-dose": "Synthetic unsaved partial dose",
        }
        # When: the author searches without submitting a save.
        response = client.post(url, data)
        # Then: required-field validation waits for save and input remains visible.
        assert response.status_code == 200
        assert (
            response.context["items"].forms[0]["dose"].value() == data["items-0-dose"]
        )
        assert response.context["items"].forms[0].errors == {}
        assert response.context["form"]["category"].value() in {None, ""}
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        stored = PrescriptionDraft.objects.get(pk=draft.pk)
        assert stored.version == 1
        assert stored.category is None


@pytest.mark.parametrize("defect", ["action", "empty-formset", "oversized-formset"])
def test_catalog_post_rejects_invalid_actions_and_form_counts_without_saving(
    rbac_graph: RbacGraph, defect: str
) -> None:
    # Given: the current physician's bound draft with no saved medication or class.
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope)
    url = f"/prescription/clinics/{rbac_graph.clinic_a}/draft/"
    with physician_client(rbac_graph) as client:
        client.post(url, {"action": "open", "encounter_id": scope["encounter_id"]})
        before = dict(client.session)
        data = {**payload(scope, draft), "action": "catalog_search"}
        if defect == "action":
            data["action"] = "forged"
        else:
            data["items-TOTAL_FORMS"] = (
                "0" if defect == "empty-formset" else str(MAX_ITEMS + 1)
            )
        # When: a native POST has an unsupported action or invalid form count.
        response = client.post(url, data)
        # Then: the refusal neither saves content nor changes the session.
        assert response.status_code == 403
        assert dict(client.session) == before
        assert "sessionid" not in response.cookies
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        stored = PrescriptionDraft.objects.get(pk=draft.pk)
        assert stored.version == 1
        assert stored.category is None
        assert not PrescriptionItem.objects.filter(draft=draft).exists()
        assert not MedicationFavorite.objects.exists()
