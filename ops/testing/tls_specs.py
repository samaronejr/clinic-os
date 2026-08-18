"""Build closed ledger specifications for the private TLS materializer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ops.testing.tls_contract import HBA_RULES, VOLUME_SUFFIXES

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject, JsonValue


MATERIALIZE_SCRIPT = """
set -eu
umask 077
mkdir -p /clinic-pki/tls /clinic-source/tls /clinic-restore/tls \
  /clinic-web/tls /clinic-trust
chmod 0700 /clinic-pki/tls /clinic-source/tls /clinic-restore/tls /clinic-web/tls
chmod 0755 /clinic-trust
openssl genrsa -out /clinic-pki/tls/ca.key 4096
openssl req -x509 -new -sha256 -days 2 \
  -key /clinic-pki/tls/ca.key -subj /CN=clinic-phase1a-task-ca \
  -out /clinic-pki/tls/ca.crt
leaf() {
  name="$1"
  target="$2"
  openssl genrsa -out "$target/tls.key" 2048
  openssl req -new -key "$target/tls.key" -subj "/CN=$name" \
    -out /clinic-pki/tls/leaf.csr
  printf 'subjectAltName=DNS:%s\n' "$name" > /clinic-pki/tls/leaf.ext
  openssl x509 -req -sha256 -days 2 -in /clinic-pki/tls/leaf.csr \
    -CA /clinic-pki/tls/ca.crt -CAkey /clinic-pki/tls/ca.key \
    -CAcreateserial -extfile /clinic-pki/tls/leaf.ext \
    -out "$target/tls.crt"
}
leaf phase1a-db.qa.clinic-os.dev /clinic-source/tls
leaf phase1a-restore-db.qa.clinic-os.dev /clinic-restore/tls
leaf phase1a.qa.clinic-os.dev /clinic-web/tls
printf '%s\n' __HBA_RULES__ > /clinic-source/tls/pg_hba.conf
cp /clinic-source/tls/pg_hba.conf /clinic-restore/tls/pg_hba.conf
cp /clinic-pki/tls/ca.crt /clinic-trust/db-ca.pem
chown -R 999:999 /clinic-source/tls /clinic-restore/tls
chown -R 10001:10001 /clinic-web/tls /clinic-trust
chmod 0400 /clinic-pki/tls/ca.key /clinic-source/tls/tls.key \
  /clinic-restore/tls/tls.key /clinic-source/tls/pg_hba.conf \
  /clinic-restore/tls/pg_hba.conf /clinic-web/tls/tls.key
chmod 0444 /clinic-pki/tls/ca.crt /clinic-source/tls/tls.crt \
  /clinic-restore/tls/tls.crt /clinic-web/tls/tls.crt \
  /clinic-trust/db-ca.pem
rm -f /clinic-pki/tls/leaf.csr /clinic-pki/tls/leaf.ext /clinic-pki/tls/ca.srl
touch /clinic-pki/.materialized
chmod 0400 /clinic-pki/.materialized
openssl version | grep -F 'OpenSSL 3.5.6'
trap 'exit 0' TERM INT
while :; do sleep 1; done
""".replace("__HBA_RULES__", " ".join(f"'{rule}'" for rule in HBA_RULES)).strip()


def materializer_volume_names(project: str, claim_id: str) -> dict[str, str]:
    """Derive every actual volume name from project, claim, and logical role."""
    claim_token = claim_id.replace("-", "")
    return {suffix: f"{project}_{claim_token}_{suffix}" for suffix in VOLUME_SUFFIXES}


def materializer_spec(claim_id: str, project: str, image_id: str) -> JsonObject:
    """Return the no-network six-volume materializer stack reservation."""
    names = materializer_volume_names(project, claim_id)
    labels: list[JsonValue] = [{"name": "clinic.phase1a.claim", "value": claim_id}]
    targets = {
        "phase1a-pki-private": "/clinic-pki",
        "phase1a-source-db-tls": "/clinic-source",
        "phase1a-restore-db-tls": "/clinic-restore",
        "phase1a-web-tls": "/clinic-web",
        "phase1a-db-trust": "/clinic-trust",
        "phase1a-materializer-pgdata": "/var/lib/postgresql/data",
    }
    mount_objects: list[JsonObject] = sorted(
        (
            {"read_only": False, "target": targets[suffix], "volume_name": name}
            for suffix, name in names.items()
        ),
        key=lambda item: str(item["target"]),
    )
    mounts = _json_values(mount_objects)
    volume_objects: list[JsonObject] = sorted(
        (
            {"driver": "local", "labels": labels, "volume_name": name}
            for name in names.values()
        ),
        key=lambda item: str(item["volume_name"]),
    )
    volumes = _json_values(volume_objects)
    service: JsonObject = {
        "command": ["bash", "-ceu", MATERIALIZE_SCRIPT],
        "environment_contract": {
            "absent_keys": [],
            "literal": [],
            "secret_keys": [],
        },
        "extra_hosts": [],
        "filesystem_contract": None,
        "gid": 0,
        "image_contract": None,
        "image_id": image_id,
        "name": "materializer",
        "network_mode": "none",
        "network_refs": [],
        "published_ports": [],
        "start_policy": "running-before-activation",
        "uid": 0,
        "volume_mounts": mounts,
    }
    desired: JsonObject = {
        "borrowed_network_refs": [],
        "borrowed_volume_refs": [],
        "database_names": [],
        "loopback_ports": [],
        "owned_networks": [],
        "owned_volumes": volumes,
        "project": project,
        "services": [service],
    }
    return {
        "claim_id": claim_id,
        "dependency_claim_ids": [],
        "desired": desired,
        "kind": "stack",
        "purpose": "tls-materializer",
    }


def _json_values(objects: list[JsonObject]) -> list[JsonValue]:
    values: list[JsonValue] = []
    values.extend(objects)
    return values
