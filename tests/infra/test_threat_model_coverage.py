"""Every threat-model mitigation maps to at least one passing test (todo 73)."""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING

import pytest
from ops.testing import threat_model_coverage as coverage
from ops.testing.isolation_common import IsolationError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from ops.testing.isolation_common import JsonObject

MODELS = {
    "agents",
    "ai-gateway",
    "delegates",
    "import-export",
    "payments",
    "realtime",
    "scribe-media",
    "support-access",
}
PROOF = "tests/core/test_x.py::test_proof"
BROWSER = "tests/renewal/browser/test_x.py::test_page"


def test_every_threat_model_mitigation_maps_to_a_defined_test() -> None:
    mitigations = coverage.load_mitigations()
    report = coverage.coverage_report(mitigations, coverage.defined_tests())
    assert report["uncovered"] == []
    assert report["covered"] == report["mitigations"] == len(mitigations)
    assert {row.model for row in mitigations} == MODELS
    for row in mitigations:
        assert any(not test.startswith(coverage.BROWSER_PREFIX) for test in row.tests)


def _tree(tmp_path: Path, tests_cell: str) -> Path:
    root = tmp_path / "repository"
    (root / coverage.MODEL_DIRECTORY).mkdir(parents=True)
    (root / "tests/core").mkdir(parents=True)
    (root / "tests/renewal/browser").mkdir(parents=True)
    (root / "tests/core/test_x.py").write_text(
        "def test_proof():\n    pass\n\n\n"
        "class TestGroup:\n    def test_member(self):\n        pass\n",
        encoding="utf-8",
    )
    (root / "tests/renewal/browser/test_x.py").write_text(
        "def test_page():\n    pass\n", encoding="utf-8"
    )
    (root / coverage.MODEL_DIRECTORY / "sample.md").write_text(
        "# Threat model: sample\n\n## Threats and mitigations\n\n"
        f"{coverage.TABLE_HEADER}\n{coverage.TABLE_RULE}\n"
        f"| XX-S1 | Spoofing | A threat. | A mitigation. | 7, 73 | {tests_cell} |\n\n"
        "## Residual risk\n",
        encoding="utf-8",
    )
    return root


def _report(root: Path, outcomes: dict[str, str] | None = None) -> JsonObject:
    return coverage.coverage_report(
        coverage.load_mitigations(root), coverage.defined_tests(root), outcomes
    )


def test_mapped_defined_test_covers_and_class_members_resolve(tmp_path: Path) -> None:
    member = "tests/core/test_x.py::TestGroup::test_member"
    root = _tree(tmp_path, f"`{PROOF}`<br>`{member}`")
    report = _report(root)
    assert report["uncovered"] == []
    (row,) = coverage.load_mitigations(root)
    assert (row.identifier, row.todos, row.tests) == ("XX-S1", (7, 73), (PROOF, member))


@pytest.mark.parametrize(
    ("cell", "reason"),
    [
        ("", "no test id is mapped"),
        ("`tests/core/test_x.py::test_missing`", "is not a defined test"),
        (f"`{BROWSER}`", "only browser tests are mapped"),
    ],
)
def test_unmapped_unknown_or_browser_only_mitigation_is_uncovered(
    tmp_path: Path, cell: str, reason: str
) -> None:
    report = _report(_tree(tmp_path, cell))
    assert report["uncovered"] == ["XX-S1"]
    rows = report["rows"]
    assert isinstance(rows, list)
    assert isinstance(rows[0], dict)
    reasons = rows[0]["reasons"]
    assert isinstance(reasons, list)
    assert any(isinstance(item, str) and reason in item for item in reasons)


@pytest.mark.parametrize(
    ("outcomes", "uncovered"),
    [
        ({PROOF: "passed", BROWSER: "skipped"}, []),
        ({PROOF: "failed", BROWSER: "skipped"}, ["XX-S1"]),
        ({PROOF: "skipped", BROWSER: "passed"}, ["XX-S1"]),
        ({}, ["XX-S1"]),
    ],
)
def test_a_run_needs_a_passing_non_browser_test_and_no_failure(
    tmp_path: Path, outcomes: dict[str, str], uncovered: list[str]
) -> None:
    root = _tree(tmp_path, f"`{PROOF}`<br>`{BROWSER}`")
    assert _report(root, outcomes)["uncovered"] == uncovered


@pytest.mark.parametrize(
    "mutate",
    [
        lambda text: text.replace("| Tests |", "|"),
        lambda text: text.replace("| Spoofing |", "| Tampering |"),
        lambda text: text.replace("| 7, 73 |", "| todo 7 |"),
        lambda text: text.replace(f"`{PROOF}`", PROOF),
        lambda text: text.replace(f"`{PROOF}`", f"`{PROOF}`<br>`{PROOF}`"),
        lambda text: text + "\n| XX-T1 | Tampering | T. | M. | 7 | |\n",
    ],
)
def test_table_drift_is_refused(tmp_path: Path, mutate: Callable[[str], str]) -> None:
    root = _tree(tmp_path, f"`{PROOF}`")
    model = root / coverage.MODEL_DIRECTORY / "sample.md"
    model.write_text(mutate(model.read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(IsolationError):
        coverage.load_mitigations(root)


def test_duplicate_mitigation_ids_across_models_are_refused(tmp_path: Path) -> None:
    root = _tree(tmp_path, f"`{PROOF}`")
    directory = root / coverage.MODEL_DIRECTORY
    shutil.copy(directory / "sample.md", directory / "copy.md")
    with pytest.raises(IsolationError, match="appears twice"):
        coverage.load_mitigations(root)


def _junit(path: Path, cases: list[tuple[str, str, str]]) -> Path:
    body = "".join(
        f'<testcase classname="{classname}" name="{name}">'
        + ("" if outcome == "passed" else f"<{outcome}/>")
        + "</testcase>"
        for classname, name, outcome in cases
    )
    path.write_text(
        f'<testsuites><testsuite name="pytest">{body}</testsuite></testsuites>',
        encoding="utf-8",
    )
    return path


def test_junit_reports_fold_variants_classes_and_shards(tmp_path: Path) -> None:
    first = _junit(
        tmp_path / "shard-1.xml",
        [
            ("tests.core.test_x", "test_proof[a]", "passed"),
            ("tests.core.test_x", "test_proof[b]", "passed"),
            ("tests.core.test_x", "test_bad[a]", "passed"),
            ("tests.core.test_x.TestGroup", "test_member", "skipped"),
        ],
    )
    second = _junit(
        tmp_path / "shard-2.xml",
        [
            ("tests.core.test_x", "test_bad[b]", "error"),
            ("tests.core.test_x", "test_flaky", "failure"),
        ],
    )
    assert coverage.executed_outcomes([first, second]) == {
        PROOF: "passed",
        "tests/core/test_x.py::test_bad": "failed",
        "tests/core/test_x.py::TestGroup::test_member": "skipped",
        "tests/core/test_x.py::test_flaky": "failed",
    }


@pytest.mark.parametrize(
    ("classname", "name"), [("ops.testing.test_x", "test_a"), ("tests", "test_a")]
)
def test_junit_cases_outside_tests_are_refused(
    tmp_path: Path, classname: str, name: str
) -> None:
    report = _junit(tmp_path / "report.xml", [(classname, name, "passed")])
    with pytest.raises(IsolationError):
        coverage.executed_outcomes([report])


def test_cli_writes_the_report_and_exits_by_verdict(tmp_path: Path) -> None:
    root = _tree(tmp_path, f"`{PROOF}`")
    passed = _junit(
        tmp_path / "pass.xml", [("tests.core.test_x", "test_proof", "passed")]
    )
    failed = _junit(
        tmp_path / "fail.xml", [("tests.core.test_x", "test_proof", "failure")]
    )
    output = tmp_path / "threat-coverage.json"
    argv = ["report", "--root", str(root), "--output", str(output)]
    assert coverage.run_cli([*argv, "--junit", str(passed)]) == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert (written["execution"], written["covered"]) == ("junit", 1)
    assert (
        coverage.run_cli([*argv, "--junit", str(passed), "--junit", str(failed)]) == 1
    )
    assert json.loads(output.read_text(encoding="utf-8"))["uncovered"] == ["XX-S1"]
    empty = tmp_path / "empty.xml"
    empty.write_text("<testsuites/>", encoding="utf-8")
    with pytest.raises(IsolationError, match="records no test case"):
        coverage.run_cli([*argv, "--junit", str(empty)])
