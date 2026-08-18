from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from ops.testing import https_stack_runtime
from ops.testing.https_contract import TLS_COMMAND
from ops.testing.https_stack_specs import HttpsStackInput, build_https_stack_plan
from ops.testing.isolation_stack_claim import validate_stack_desired
from ops.testing.process_helpers import run_process
from ops.testing.tls_specs import materializer_volume_names

if TYPE_CHECKING:
    import pytest
    from ops.testing.isolation_common import JsonObject

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_https_command_is_an_exact_harness_only_override() -> None:
    assert TLS_COMMAND == (
        "gunicorn",
        "--config=/app/ops/container/gunicorn_no_proxy.py",
        "--bind=0.0.0.0:8443",
        "--certfile=/run/clinic-test-tls/tls/tls.crt",
        "--keyfile=/run/clinic-test-tls/tls/tls.key",
        "--workers=2",
        "--threads=4",
        "--timeout=30",
        "--graceful-timeout=30",
        "--keep-alive=5",
        "--max-requests=1000",
        "--max-requests-jitter=100",
        "--access-logfile=-",
        "--error-logfile=-",
        "config.wsgi:application",
    )
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()
    assert "--bind=0.0.0.0:8443" not in dockerfile
    assert "--certfile=" not in dockerfile


def test_https_harness_cli_rejects_noncanonical_forms() -> None:
    script = PROJECT_ROOT / "ops/testing/https_image_harness.sh"
    for arguments in ((), ("unknown",), ("smoke", "extra")):
        result = run_process((str(script), *arguments))
        assert result.returncode == 2
        assert "https-image-harness: invalid invocation" in result.stderr


def test_https_cleanup_compares_full_container_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_id = "1" * 64
    calls: list[tuple[str, ...]] = []

    def fake_docker(arguments: tuple[str, ...]) -> str:
        calls.append(arguments)
        if arguments[0] == "container":
            return container_id + "\n"
        if arguments[0] == "inspect":
            return "false\n"
        return ""

    monkeypatch.setattr(https_stack_runtime, "run_docker_command", fake_docker)

    https_stack_runtime._remove_container(container_id)

    assert calls == [
        (
            "container",
            "ls",
            "-aq",
            "--no-trunc",
            "--filter",
            f"id={container_id}",
        ),
        ("inspect", "--format", "{{.State.Running}}", container_id),
        ("rm", container_id),
    ]


def test_https_runtime_proves_shm_is_not_writable() -> None:
    probe = (PROJECT_ROOT / "ops/testing/https_runtime_probes.py").read_text()

    assert "HostConfig.ShmSize" not in probe
    assert 'runtime.get("shm_write_denied") is not True' in probe


def test_https_role_bootstrap_matches_migration_prerequisites() -> None:
    probe = (PROJECT_ROOT / "ops/testing/https_role_bootstrap.py").read_text()

    assert "CREATE ROLE clinic_resolver NOLOGIN BYPASSRLS" in probe
    assert "GRANT clinic_app TO clinic_owner WITH INHERIT FALSE, SET TRUE" in probe
    assert "GRANT clinic_resolver TO clinic_owner WITH INHERIT FALSE, SET TRUE" in probe


def test_https_stack_is_dependency_linked_and_uses_the_candidate_image() -> None:
    claim_id = "11111111-1111-4111-8111-111111111111"
    materializer_id = "22222222-2222-4222-8222-222222222222"
    ca_export_id = "33333333-3333-4333-8333-333333333333"
    image_contract: JsonObject = {
        "available_suite_ids": [],
        "kind": "application",
        "revision_sha": "3" * 40,
        "source_entry_count": 1,
        "source_manifest_sha256": "4" * 64,
        "tree_sha": "5" * 40,
    }
    plan = build_https_stack_plan(
        HttpsStackInput(
            claim_id,
            materializer_id,
            materializer_volume_names("clinic_tls", materializer_id),
            "clinic_https_fixture",
            f"sha256:{'6' * 64}",
            f"sha256:{'7' * 64}",
            image_contract,
            ca_export_id,
            "source",
        )
    )
    desired = plan.spec["desired"]
    assert isinstance(desired, dict)
    validate_stack_desired(desired)
    services = desired["services"]
    assert isinstance(services, list)
    assert [service["name"] for service in services if isinstance(service, dict)] == [
        "cleartext",
        "database",
        "release",
        "web",
    ]
    web = services[3]
    assert isinstance(web, dict)
    assert web["command"] == list(TLS_COMMAND)
    assert web["image_id"] == f"sha256:{'7' * 64}"
    assert plan.spec["dependency_claim_ids"] == sorted([ca_export_id, materializer_id])
    loopback_ports = desired["loopback_ports"]
    assert isinstance(loopback_ports, list)
    assert len(loopback_ports) == 2
    for service in (services[0], services[2], services[3]):
        assert isinstance(service, dict)
        filesystem = service["filesystem_contract"]
        assert isinstance(filesystem, dict)
        assert filesystem["root_read_only"] is True
        assert filesystem["writable_paths"] == [PurePosixPath("/", "tmp").as_posix()]


def test_source_and_restore_plans_reuse_one_application_image() -> None:
    materializer_id = "22222222-2222-4222-8222-222222222222"
    export_id = "33333333-3333-4333-8333-333333333333"
    image_id = f"sha256:{'7' * 64}"
    image_contract: JsonObject = {
        "available_suite_ids": [],
        "kind": "application",
        "revision_sha": "3" * 40,
        "source_entry_count": 1,
        "source_manifest_sha256": "4" * 64,
        "tree_sha": "5" * 40,
    }
    volumes = materializer_volume_names("clinic_tls", materializer_id)
    plans = [
        build_https_stack_plan(
            HttpsStackInput(
                claim_id,
                materializer_id,
                volumes,
                f"clinic_https_{phase}",
                f"sha256:{'6' * 64}",
                image_id,
                image_contract,
                export_id,
                phase,
            )
        )
        for claim_id, phase in (
            ("11111111-1111-4111-8111-111111111111", "source"),
            ("44444444-4444-4444-8444-444444444444", "restore"),
        )
    ]

    web_images = []
    for plan in plans:
        desired = plan.spec["desired"]
        assert isinstance(desired, dict)
        services = desired["services"]
        assert isinstance(services, list)
        web = services[3]
        assert isinstance(web, dict)
        web_images.append(web["image_id"])
    assert web_images == [image_id, image_id]
    assert plans[0].database_host != plans[1].database_host
    assert plans[0].pgdata_name != plans[1].pgdata_name
