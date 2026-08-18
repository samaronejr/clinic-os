from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = (
    "DPA-REVIEW.md",
    "INCIDENT-APPROVAL.md",
    "LIVE-DATA-GATE.md",
    "PRIVACY-REVIEW.md",
    "ROPA-REVIEW.md",
)
REQUIRED_PENDING = (
    "hosted CI/deploy/TLS",
    "encrypted provider PITR restore",
    "monitoring delivery",
    "legal approval",
    "privacy approval",
    "incident approval",
    "rotation approval",
    "retention approval",
)


def test_compliance_templates_are_explicitly_unapproved_and_fail_closed() -> None:
    compliance = ROOT / "docs/compliance"
    documents = {
        name: (compliance / name).read_text(encoding="utf-8") for name in TEMPLATES
    }

    assert all(
        "UNAPPROVED — DO NOT USE LIVE DATA" in text for text in documents.values()
    )
    live = documents["LIVE-DATA-GATE.md"]
    for item in REQUIRED_PENDING:
        assert f"- [ ] {item}" in live
    assert "- [x] encrypted provider PITR restore" not in live
    assert (
        "logical restore only; provider PITR not verified; "
        "encrypted provider backup not verified" in live
    )
    assert all("production approved" not in text.lower() for text in documents.values())


def test_phase_1a_docs_state_the_actual_synthetic_only_boundary() -> None:
    for name in ("ARCHITECTURE.md", "SECURITY.md", "RUNBOOK.md", "CONTRIBUTING.md"):
        text = (ROOT / "docs" / name).read_text(encoding="utf-8")
        assert "Phase 1A" in text
        assert "synthetic" in text.lower()
        assert "LIVE-DATA-GATE.md" in text
    runbook = (ROOT / "docs/RUNBOOK.md").read_text(encoding="utf-8")
    assert "make restore-rehearsal" in runbook
    assert "does not define a live backup principal" in runbook
    assert "does not claim archive encryption or provider recovery" in runbook
