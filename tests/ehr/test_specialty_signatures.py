"""Machine-consumed specialty service boundaries stay explicit and keyword-only."""

from collections.abc import Callable
from inspect import Parameter, signature

import pytest
from apps.ehr.packs import install_pack, installed_packs, reorder_packs, uninstall_pack
from apps.ehr.specialty_records import record_immunization, record_procedure


@pytest.mark.parametrize(
    ("service", "names"),
    [
        (install_pack, ("clinic_id", "pack_key", "version")),
        (uninstall_pack, ("clinic_id", "pack_key", "version")),
        (installed_packs, ("clinic_id",)),
        (reorder_packs, ("clinic_id", "pack_keys")),
        (record_immunization, ("clinic_id", "encounter_id", "record")),
        (record_procedure, ("clinic_id", "encounter_id", "record")),
    ],
)
def test_specialty_services_derive_actor_and_scope(
    service: Callable[..., object], names: tuple[str, ...]
) -> None:
    parameters = signature(service).parameters
    assert tuple(parameters) == names
    assert all(value.kind == Parameter.KEYWORD_ONLY for value in parameters.values())
