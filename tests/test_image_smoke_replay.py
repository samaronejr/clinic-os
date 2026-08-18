from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from ops.testing import image_smoke_controller as controller
from ops.testing import isolation_common as c

if TYPE_CHECKING:
    from pathlib import Path

SHA = "a" * 40
CLAIM_ID = "11111111-1111-4111-8111-111111111111"


def test_existing_candidate_is_validated_without_reserving_another_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, attempt = _repository(tmp_path)
    envelope = attempt / "candidate-images" / SHA / "application-envelope.json"
    envelope.parent.mkdir(parents=True)
    envelope.write_bytes(b"{}\n")

    def smoke(
        _repository: Path,
        _revision: str,
        _kind: str,
        _required_suites: list[str] | None,
    ) -> str:
        return "sha256:existing"

    def refuse_uuid() -> UUID:
        message = "existing candidates must not allocate a claim"
        raise AssertionError(message)

    monkeypatch.setattr(controller, "smoke_candidate", smoke)
    monkeypatch.setattr(controller, "uuid4", refuse_uuid)

    assert controller.build_candidate(repository, SHA) == "sha256:existing"


def test_failed_candidate_build_removes_and_releases_its_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, attempt = _repository(tmp_path)
    released: list[str] = []

    def reserve(_ledger: Path, _spec: Path) -> None:
        return None

    def activate(_ledger: Path, claim_id: str, _observed: Path) -> None:
        (attempt / "claims" / claim_id).mkdir(parents=True)

    def fail_assembly(
        _repository: Path,
        context: Path,
        _revision: str,
        _kind: str,
    ) -> c.JsonObject:
        context.mkdir()
        message = "synthetic assembly failure"
        raise RuntimeError(message)

    def release(_ledger: Path, claim_id: str) -> None:
        released.append(claim_id)

    monkeypatch.setattr(controller, "uuid4", lambda: UUID(CLAIM_ID))
    monkeypatch.setattr(controller, "reserve_claim", reserve)
    monkeypatch.setattr(controller, "activate_claim", activate)
    monkeypatch.setattr(controller, "assemble_candidate_context", fail_assembly)
    monkeypatch.setattr(controller, "release_claim", release)

    with pytest.raises(RuntimeError, match="assembly failure"):
        _ = controller.build_candidate(repository, SHA)

    assert released == [CLAIM_ID]
    assert not (attempt / "claims" / CLAIM_ID).exists()


def _repository(root: Path) -> tuple[Path, Path]:
    repository = root / "repository"
    evidence = repository / ".omo/evidence"
    attempt = root / "attempt"
    evidence.mkdir(parents=True)
    attempt.mkdir()
    ledger: c.JsonObject = {"attempt_root": str(attempt)}
    c.write_no_replace(
        evidence / "isolation-ledger-phase1a.json",
        c.canonical_bytes(ledger),
        mode=c.MODE_IMMUTABLE,
    )
    return repository, attempt
