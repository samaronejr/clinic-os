"""The ``secret-scan`` pre-commit hook refuses committed credentials (todo 73)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from ops.testing import secret_scan
from ops.testing.isolation_common import IsolationError

if TYPE_CHECKING:
    from pathlib import Path

# Assembled at runtime so this file never contains a credential-shaped literal.
PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE KEY-----"
AWS_KEY = "AKIA" + "Q" * 16
GITHUB_TOKEN = "ghp_" + "a1B2" * 9
JWT = ".".join(("eyJ" + "a" * 12, "eyJ" + "b" * 12, "c" * 12))
OTP_URI = "otpauth://totp/clinic:user?secret=" + "JBSWY3DPEHPK3PXP" + "&issuer=x"


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        (PRIVATE_KEY, "private-key"),
        (f"key = '{AWS_KEY}'", "aws-access-key"),
        (f"token={GITHUB_TOKEN}", "github-token"),
        (f"auth: {JWT}", "jwt"),
        (OTP_URI, "otp-provisioning-uri"),
        ("postgresql://clinic_app:" + "Zk8Qw2" + "@db/clinic", "credentialed-url"),
        ("redis://default:" + "s3cr3t-Value" + "@cache:6379/0", "credentialed-url"),
    ],
)
def test_each_rule_reports_path_line_and_rule_only(text: str, rule: str) -> None:
    findings = secret_scan.scan_text("config/x.py", f"first line\n{text}\n")
    assert findings == [secret_scan.Finding("config/x.py", 2, rule)]


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://clinic_app:pw@127.0.0.1:5432/clinic",
        "postgresql://clinic_owner:clinic_owner_password@host/db",
        "postgresql://clinic_app:${CLINIC_APP_PASSWORD}@host/db",
        "postgresql://{role}:synthetic@host/db",
        "postgresql://user:p%40ss@host/db",
        "postgresql://host/db",
    ],
)
def test_synthetic_placeholder_urls_are_not_credentials(url: str) -> None:
    assert secret_scan.scan_text("tests/x.py", url) == []


def _repository(tmp_path: Path, allowlist: str = "") -> Path:
    root = tmp_path / "repository"
    (root / "ops/testing").mkdir(parents=True)
    (root / secret_scan.ALLOWLIST).write_text(
        f"# header\n{allowlist}", encoding="utf-8"
    )
    return root


def test_forbidden_files_fail_by_name_and_env_example_is_allowed(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    for name in (".env", ".env.example", "infra/prod.tfstate", "infra/prod.tfvars"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("X=1\n", encoding="utf-8")
    findings = secret_scan.scan_paths(
        root, [".env", ".env.example", "infra/prod.tfstate", "infra/prod.tfvars"]
    )
    assert {item.path for item in findings} == {
        ".env",
        "infra/prod.tfstate",
        "infra/prod.tfvars",
    }
    assert {item.rule for item in findings} == {"forbidden-file"}


def test_binary_files_are_not_text_scanned(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "image.png").write_bytes(b"\x89PNG\0" + PRIVATE_KEY.encode())
    assert secret_scan.scan_paths(root, ["image.png"]) == []


def test_allowlist_suppresses_only_its_entry_and_stale_entries_fail(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path, "fixtures/pki.json\tprivate-key\tsynthetic PKI\n")
    (root / "fixtures").mkdir()
    (root / "fixtures/pki.json").write_text(PRIVATE_KEY, encoding="utf-8")
    (root / "leak.py").write_text(f"x = '{AWS_KEY}'\n", encoding="utf-8")
    allowlist = secret_scan.load_allowlist(root)
    findings = secret_scan.scan_paths(root, ["fixtures/pki.json", "leak.py"])
    assert secret_scan.verdict(
        findings, allowlist, scanned={"fixtures/pki.json", "leak.py"}, complete=False
    ) == ["leak.py:1: aws-access-key"]
    (root / "fixtures/pki.json").write_text("{}", encoding="utf-8")
    findings = secret_scan.scan_paths(root, ["fixtures/pki.json"])
    assert secret_scan.verdict(
        findings, allowlist, scanned={"fixtures/pki.json"}, complete=False
    ) == [f"{secret_scan.ALLOWLIST}: stale entry fixtures/pki.json private-key"]
    assert (
        secret_scan.verdict(findings, allowlist, scanned={"other.py"}, complete=False)
        == []
    )


@pytest.mark.parametrize(
    "line",
    [
        "only-a-path\n",
        "path\tno-such-rule\treason\n",
        "path\tjwt\treason\npath\tjwt\tagain\n",
    ],
)
def test_malformed_allowlist_is_refused(tmp_path: Path, line: str) -> None:
    with pytest.raises(IsolationError):
        secret_scan.load_allowlist(_repository(tmp_path, line))


def test_committed_allowlist_entries_are_real_and_still_needed() -> None:
    root = secret_scan.PROJECT_ROOT
    allowlist = secret_scan.load_allowlist(root)
    assert allowlist
    paths = {path for path, _rule in allowlist}
    findings = secret_scan.scan_paths(root, paths)
    assert secret_scan.verdict(findings, allowlist, scanned=paths, complete=False) == []
    assert {(item.path, item.rule) for item in findings} == set(allowlist)


def test_cli_exit_follows_the_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository(tmp_path)
    (root / "clean.py").write_text("x = 1\n", encoding="utf-8")
    (root / "leak.py").write_text(f"x = '{AWS_KEY}'\n", encoding="utf-8")
    assert secret_scan.run_cli(["clean.py"], root) == 0
    assert secret_scan.run_cli(["clean.py", "leak.py"], root) == 1
    error = capsys.readouterr().err
    assert error == "secret-scan: leak.py:1: aws-access-key\n"
    assert AWS_KEY not in error
