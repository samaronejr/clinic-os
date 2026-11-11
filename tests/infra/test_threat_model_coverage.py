"""Every threat-model mitigation maps to at least one passing test (todo 73)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
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
# Open rows recorded in the threat-model tables: PARTIAL clauses are marked
# **Unproved:**, GAPS (missing controls, owners todos 19 and 50) **Gap:**.
PARTIAL = ["IE-R1", "PY-R1", "PY-D1", "SM-I1", "SA-T1"]
GAPS = ["DG-R1"]
PROOF = "tests/core/test_x.py::test_proof"
BROWSER = "tests/renewal/browser/test_x.py::test_page"


def test_every_threat_model_mitigation_is_mapped_to_a_defined_test() -> None:
    mitigations = coverage.load_mitigations()
    inventory = coverage.mapping_inventory(mitigations, coverage.defined_tests())
    assert inventory["unmapped"] == []
    assert inventory["mapped"] == inventory["mitigations"] == len(mitigations)
    assert {row.model for row in mitigations} == MODELS
    # A mapping is never a verdict: the inventory has no covered state.
    rows = inventory["rows"]
    assert isinstance(rows, list)
    assert {row["state"] for row in rows if isinstance(row, dict)} == {"mapped"}


def test_every_mitigation_has_a_passing_mapped_test(tmp_path: Path) -> None:
    """Acceptance: run each mitigation's designated proof and require it to pass.

    The proof is the first non-browser id in its Tests cell. The child pytest
    uses this session's database settings; ci-serial-tests.txt runs this test
    in ci_pytest's single-process phase.
    """
    mitigations = coverage.load_mitigations()
    proofs = sorted(
        {proof for row in mitigations if (proof := coverage.proof_test(row))}
    )
    junit = tmp_path / "proofs.xml"
    completed = subprocess.run(  # noqa: S603 - fixed pytest argv over committed ids
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--reuse-db",
            f"--junitxml={junit}",
            *proofs,
        ],
        cwd=coverage.PROJECT_ROOT,
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stdout.decode()[-4000:]
    report = coverage.coverage_report(
        mitigations, coverage.defined_tests(), coverage.executed_outcomes([junit])
    )
    # Recorded open rows never count as covered: the exact totals are pinned so
    # closing or adding one is a deliberate change to this acceptance.
    assert report["uncovered"] == []
    assert report["partial"] == PARTIAL
    assert report["gaps"] == GAPS
    assert report["covered"] == len(mitigations) - len(PARTIAL) - len(GAPS) == 92
    assert report["complete"] is False


def _tree(
    tmp_path: Path, tests_cell: str, *, mitigation: str = "A mitigation."
) -> Path:
    root = tmp_path / "repository"
    (root / coverage.MODEL_DIRECTORY).mkdir(parents=True)
    (root / "tests/core").mkdir(parents=True)
    (root / "tests/renewal/browser").mkdir(parents=True)
    (root / "tests/core/test_x.py").write_text(
        "import pytest\n\n\n"
        "def test_proof():\n    pass\n\n\n"
        '@pytest.mark.skip(reason="always skipped control")\n'
        "def test_skipped():\n    raise AssertionError\n\n\n"
        "class TestGroup:\n    def test_member(self):\n        pass\n",
        encoding="utf-8",
    )
    (root / "tests/renewal/browser/test_x.py").write_text(
        "def test_page():\n    pass\n", encoding="utf-8"
    )
    (root / coverage.MODEL_DIRECTORY / "sample.md").write_text(
        "# Threat model: sample\n\n## Threats and mitigations\n\n"
        f"{coverage.TABLE_HEADER}\n{coverage.TABLE_RULE}\n"
        f"| XX-S1 | Spoofing | A threat. | {mitigation} | 7, 73 | {tests_cell} |\n\n"
        "## Residual risk\n",
        encoding="utf-8",
    )
    return root


def _report(root: Path, outcomes: dict[str, str]) -> JsonObject:
    return coverage.coverage_report(
        coverage.load_mitigations(root), coverage.defined_tests(root), outcomes
    )


def _run_pytest(root: Path, junit: Path) -> None:
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    subprocess.run(  # noqa: S603 - fixed pytest argv in a scratch tree
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:django",
            "-c",
            str(root / "pytest.ini"),
            "--rootdir",
            str(root),
            f"--junitxml={junit}",
            "tests/core/test_x.py",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )


def test_an_always_skipped_mapped_test_never_covers(tmp_path: Path) -> None:
    # Given a mitigation whose only mapped test is always skipped.
    root = _tree(tmp_path, "`tests/core/test_x.py::test_skipped`")
    junit = tmp_path / "run.xml"
    # When the mapped tests really run under pytest.
    _run_pytest(root, junit)
    outcomes = coverage.executed_outcomes([junit])
    assert outcomes["tests/core/test_x.py::test_skipped"] == "skipped"
    # Then the mitigation is uncovered and the CLI exits non-zero.
    report = _report(root, outcomes)
    assert report["uncovered"] == ["XX-S1"]
    output = tmp_path / "coverage.json"
    argv = ["report", "--root", str(root), "--output", str(output)]
    assert coverage.run_cli([*argv, "--junit", str(junit)]) == 1
    # And without execution evidence the report refuses to run at all.
    with pytest.raises(SystemExit):
        coverage.run_cli(argv)
    # A passing mapped test in the same run does cover it.
    root_ok = _tree(
        tmp_path / "ok", f"`{PROOF}`<br>`tests/core/test_x.py::test_skipped`"
    )
    _run_pytest(root_ok, tmp_path / "ok.xml")
    assert (
        _report(root_ok, coverage.executed_outcomes([tmp_path / "ok.xml"]))["uncovered"]
        == []
    )


def test_inventory_maps_class_members_but_never_says_covered(tmp_path: Path) -> None:
    member = "tests/core/test_x.py::TestGroup::test_member"
    root = _tree(tmp_path, f"`{PROOF}`<br>`{member}`")
    inventory = coverage.mapping_inventory(
        coverage.load_mitigations(root), coverage.defined_tests(root)
    )
    assert (inventory["mapped"], inventory["unmapped"]) == (1, [])
    assert "covered" not in json.dumps(inventory)
    (row,) = coverage.load_mitigations(root)
    assert (row.identifier, row.todos, row.tests) == ("XX-S1", (7, 73), (PROOF, member))
    assert coverage.proof_test(row) == PROOF
    output = tmp_path / "inventory.json"
    assert (
        coverage.run_cli(["inventory", "--root", str(root), "--output", str(output)])
        == 0
    )
    assert json.loads(output.read_text(encoding="utf-8"))["schema"] == (
        "clinic-threat-mapping-v1"
    )


@pytest.mark.parametrize(
    ("mitigation", "state", "key"),
    [
        ("An audited switch. **Gap:** the access log omits it.", "gap", "gaps"),
        (
            "An audited switch. **Unproved:** no test reads the log.",
            "partial",
            "partial",
        ),
    ],
)
def test_gap_and_unproved_rows_are_never_covered(
    tmp_path: Path, mitigation: str, state: str, key: str
) -> None:
    root = _tree(tmp_path, f"`{PROOF}`", mitigation=mitigation)
    (row,) = coverage.load_mitigations(root)
    assert (row.gap or row.unproved or "").startswith(("the access", "no test"))
    # Given a passing proof, the row is still not covered and the run not complete.
    report = _report(root, {PROOF: "passed"})
    assert report[key] == ["XX-S1"]
    assert (report["covered"], report["uncovered"], report["complete"]) == (
        0,
        [],
        False,
    )
    rows = report["rows"]
    assert isinstance(rows, list)
    assert isinstance(rows[0], dict)
    assert rows[0]["state"] == state
    # A failing proof still dominates as uncovered.
    assert _report(root, {PROOF: "failed"})["uncovered"] == ["XX-S1"]
    # The CLI refuses closure.
    junit = _junit(
        tmp_path / "pass.xml", [("tests.core.test_x", "test_proof", "passed")]
    )
    argv = ["report", "--root", str(root), "--output", str(tmp_path / "out.json")]
    assert coverage.run_cli([*argv, "--junit", str(junit)]) == 1
    inventory = coverage.mapping_inventory([row], coverage.defined_tests(root))
    assert inventory[key] == ["XX-S1"]


@pytest.mark.parametrize(
    "mitigation",
    [
        "M. **Gap:** a. **Gap:** b.",
        "M. **Unproved:**  ",
        "M. **Gap:** **Unproved:** b.",
    ],
)
def test_malformed_markers_are_refused(tmp_path: Path, mitigation: str) -> None:
    with pytest.raises(IsolationError):
        coverage.load_mitigations(_tree(tmp_path, f"`{PROOF}`", mitigation=mitigation))


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
    root = _tree(tmp_path, cell)
    report = _report(root, {BROWSER: "passed"})
    assert report["uncovered"] == ["XX-S1"]
    inventory = coverage.mapping_inventory(
        coverage.load_mitigations(root), coverage.defined_tests(root)
    )
    assert inventory["unmapped"] == ["XX-S1"]
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
    assert (written["execution"], written["covered"], written["complete"]) == (
        "junit",
        1,
        True,
    )
    assert (
        coverage.run_cli([*argv, "--junit", str(passed), "--junit", str(failed)]) == 1
    )
    assert json.loads(output.read_text(encoding="utf-8"))["uncovered"] == ["XX-S1"]
    empty = tmp_path / "empty.xml"
    empty.write_text("<testsuites/>", encoding="utf-8")
    with pytest.raises(IsolationError, match="records no test case"):
        coverage.run_cli([*argv, "--junit", str(empty)])
