"""Authorize, activate and roll back the Clinic OS live-data mode.

``python -m ops.release.activation preflight|activate|disable`` implements the
``A`` command contract. Live mode is a separately authorized transition: it is
bound to ``CLINIC_RELEASE_ID``, to the ``live`` environment and to the exact
release evidence validated under ``CLINIC_RELEASE_EVIDENCE_ROOT`` by
``ops.release.readiness``. ``activate`` additionally requires
``CLINIC_LIVE_ACTIVATION_APPROVAL``, the accountable authorization reference
recorded verbatim in the activation record; without it activation refuses and
the task stays ``waiting_external``.

The activation record lives at ``CLINIC_LIVE_ACTIVATION_STATE`` and is written
atomically. ``config.settings.contracts.require_data_mode`` re-validates the
whole contract on every startup — environment, record binding and a fresh live
readiness evaluation — so a missing, corrupt, disabled or drifted record, or
evidence that no longer passes, fails closed with ``ImproperlyConfigured``.
There is no silent fallback: invalid settings or evidence never degrade to
synthetic mode, and ``synthetic`` remains the default only when
``CLINIC_DATA_MODE`` is absent; an explicitly empty or unknown value fails.

``disable`` is the rollback path: it marks the record ``disabled``. Running
processes re-validate the record at the request boundary
(``apps.core.middleware.LiveModeHaltMiddleware``), the shared job boundary
(``apps.core.integration``/``apps.comms.tasks``/``apps.prescription.signing``)
and the attachment storage boundary (``apps.ehr.attachment_storage``), so a
disabled activation stops new live writes and jobs in already-running
processes, not just new ones. It deletes nothing — the record, the evidence
root, the database and the attachment store are all preserved — and it never
makes records public.

Storage is owned explicitly in both directions, independently of the
activation-state pointer a caller may or may not retain. Activation binds the
complete database endpoint (host, port, name), the broker URL and the
resolved attachment root; it claims the endpoint in the deployment-local
registry ``var/live-database-claims`` and the attachment root with a sibling
ownership marker. Live refuses loopback/test/synthetic-default databases,
database endpoints claimed by a different activation and any root claimed by
synthetic storage; synthetic startup refuses a database endpoint or
attachment root claimed by a live activation — with or without the state
pointer — and synthetic writes refuse a live-claimed root.

Live secrets must come from the approved managed store. ``synthetic-file`` is
a rehearsal-only backend and never satisfies the live prerequisite.
``CLINIC_LIVE_ACTIVATION_REHEARSAL`` opts ``activate``/``preflight`` into a
rehearsal of the transition mechanics only: the record is bound as
``rehearsal: true``, the flag itself is rejected by the live environment
contract, and a rehearsal record can never authorize live startup. Either way
the configured store must actually return the required key material — an
arbitrary directory is never live readiness.

Exit codes: 0 success, 1 refused/not-ready/corrupt state, 2 usage or
environment error. Nothing here is legal review or permission to deploy.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import ipaddress
import json
import os
import socket
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from django.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

from config.settings.contracts import (
    DEFAULT_EHR_ATTACHMENT_ROOT,
    LIVE_DATA_MODE,
    SYNTHETIC_DATA_MODE,
    parse_production_hosts,
    resolve_attachment_root,
    validate_runtime_secret,
    validate_secret_store_env,
    validate_secure_ssl_host,
)
from config.settings.database import (
    parse_database_url,
    resolve_app_database_config,
)

from ops.release import readiness

DATA_MODES: Final = frozenset({SYNTHETIC_DATA_MODE, LIVE_DATA_MODE})
LIVE_SETTINGS_MODULES: Final = frozenset(
    {"config.settings.prod", "config.settings.release"}
)
ACTIVATION_STATE_ENV: Final = "CLINIC_LIVE_ACTIVATION_STATE"
ACTIVATION_APPROVAL_ENV: Final = "CLINIC_LIVE_ACTIVATION_APPROVAL"
LIVE_REHEARSAL_ENV: Final = "CLINIC_LIVE_ACTIVATION_REHEARSAL"
RECORD_VERSION: Final = 1
RECORD_FIELDS: Final = frozenset(
    {
        "version",
        "status",
        "release_id",
        "environment",
        "system",
        "approval_reference",
        "activated_at",
        "disabled_at",
        "rehearsal",
        "evidence_root",
        "evidence_manifest_sha256",
        "storage",
    }
)
STORAGE_FIELDS: Final = frozenset(
    {
        "database_host",
        "database_port",
        "database_name",
        "attachment_root",
        "broker_url",
    }
)

# The synthetic defaults a live environment must not reuse; storage isolation
# is part of the live contract, not a convention. The attachment default is
# the shared contract seam's — the same value settings resolve through
# ``resolve_attachment_root``.
SYNTHETIC_DEFAULT_DATABASE: Final = ("localhost", "clinic")
SYNTHETIC_DEFAULT_ATTACHMENT_ROOT: Final = DEFAULT_EHR_ATTACHMENT_ROOT
SYNTHETIC_ONLY_ENV_VARS: Final = (
    "COMMS_SYNTHETIC_CHANNELS",
    "TELECONSULT_SYNTHETIC_PROVIDER",
    "TELECONSULT_SYNTHETIC_FAIL",
    "PRESCRIPTION_SYNTHETIC_SIGNING",
    "PHYSICIAN_SYNTHETIC_REGISTRY",
    "BILLING_SYNTHETIC_PIX",
    "BILLING_SYNTHETIC_PIX_SECRET",
    "COMMS_REVOKED_REMINDER_TEMPLATES",
    "CELERY_TASK_ALWAYS_EAGER",
)
FALSE_TOKENS: Final = frozenset({"0", "false", "no", "off"})

# Rehearsal-only secret backends can never authorize live mode; the approved
# managed store is integrated separately (task 43/6 boundary). Live also
# requires the configured store to return the key material the protected-field
# boundary needs — an existing directory alone proves nothing.
REHEARSAL_SECRET_BACKENDS: Final = frozenset({"synthetic-file"})
REQUIRED_SECRET_NAME: Final = "tenant-kek"  # noqa: S105 - a name, not a value

# Attachment-root ownership marker: a sibling file
# ``<root-name>.clinic-owner`` claiming the root for exactly one storage
# owner. Synthetic writes claim it lazily; live activation claims it at
# activate time. Either side refuses a root claimed by the other.
STORAGE_OWNER_MARKER_SUFFIX: Final = ".clinic-owner"

# Database-endpoint ownership registry: activation writes one claim file per
# bound endpoint so a synthetic process can refuse live-bound database
# storage without needing the activation-state pointer — a synthetic
# deployment is not required to retain live activation variables, and the
# endpoint itself has no filesystem anchor for a sibling marker. Claims
# persist after disable: the storage still holds live-bound data.
DATABASE_CLAIMS_DIR: Final = (
    Path(__file__).resolve().parents[2] / "var" / "live-database-claims"
)

# Database engines that speak the PostgreSQL wire protocol: the schemes
# ``environ.Env.db`` maps onto them (postgres/postgresql/psql/pgsql plus the
# postgis and prometheus variants) and the cockroachdb backend. Only these
# can reach a claimed PostgreSQL endpoint; any other configured engine
# resolves to a different backend and yields no endpoint identity.
POSTGRES_WIRE_ENGINES: Final = frozenset(
    {
        "django.db.backends.postgresql",
        "django.contrib.gis.db.backends.postgis",
        "django_prometheus.db.backends.postgresql",
        "django_prometheus.db.backends.postgis",
        "django_cockroachdb",
    }
)
POSTGRES_DEFAULT_PORT: Final = "5432"
POSTGRES_MAX_PORT: Final = 65_535
# libpq environment variables that can reroute even an explicit DSN
# endpoint: PGHOSTADDR overrides the address libpq dials and PGSERVICE
# delegates every parameter to pg_service.conf. The live contract binds
# the approved DSN authority, so both are refused there; the synthetic
# isolation check models them — plus PGHOST/PGPORT for parameters the
# DSN leaves unset — exactly like libpq.
LIBPQ_REROUTE_ENV_VARS: Final = frozenset({"PGHOSTADDR", "PGSERVICE"})

DISCLAIMER: Final = (
    "Activation records an accountable authorization reference; it is not "
    "legal review and it is not permission to deploy."
)

Report = tuple[int, dict[str, Any]]


class LiveModeHaltedError(Exception):
    """The live activation no longer authorizes this running process."""


class StorageOwnershipError(Exception):
    """The attachment root is claimed by a different storage owner."""


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_timestamp(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp; naive or malformed values return None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _env_flag_set(environment: Mapping[str, str], name: str) -> bool:
    """Return True when a synthetic-only env var carries an active value."""
    value = environment.get(name)
    if value is None:
        return False
    stripped = value.strip()
    return bool(stripped) and stripped.casefold() not in FALSE_TOKENS


def _database_identity(environment: Mapping[str, str]) -> tuple[str, str, str] | None:
    """Return the normalized (host, port, database) endpoint for APP_DATABASE_URL.

    The host is canonicalized by ``_normalize_dial_target`` so the claim
    registry, the record binding and the synthetic-side effective-target
    resolution all key the same endpoint.
    """
    try:
        parsed = parse_database_url(
            environment.get("APP_DATABASE_URL", ""), required_role="clinic_app"
        )
    except ImproperlyConfigured:
        return None
    host = parsed["HOST"]
    port = parsed["PORT"]
    name = parsed["NAME"]
    if (
        not isinstance(host, str)
        or not isinstance(port, str)
        or not isinstance(name, str)
    ):
        return None
    return _normalize_dial_target(host), port, name


def _effective_database_identities(
    environment: Mapping[str, str],
) -> list[tuple[str, str, str]] | None:
    """Resolve the (host, port, database) endpoints APP_DATABASE_URL reaches.

    The isolation check must see the same connection the selected synthetic
    settings would open, so the URL is resolved by the same
    ``environ.Env.db`` call ``config.settings.base`` uses — including its
    ``$VARIABLE`` proxy indirection, supported schemes, percent-decoding
    and cluster syntax — rather than a parallel loose parser, and the
    result is merged the way the installed
    PostgreSQL driver's ``get_connection_params`` merges it: OPTIONS
    override the parsed NAME (``?dbname=``), supply host/port only when
    the URL authority leaves them empty, ``hostaddr`` — not ``host`` —
    selects the address libpq dials (paired element-wise, an empty
    hostaddr member falling back to its paired host member), and
    parameters the DSN leaves unset fall through to libpq's environment
    (``PGHOST``/``PGHOSTADDR``/``PGPORT``/``PGSERVICE``). Dial targets
    and ports are canonicalized
    the way the resolver sees them — IPv4-mapped IPv6 addresses dial the
    embedded IPv4 endpoint and zero-padded or list ports are the same
    port — and each dial target expands to every spelling that reaches
    it: the literal plus every address the system resolver returns, so a
    DNS alias for a claimed address is refused like the address itself.
    A non-PostgreSQL engine yields no identity (it cannot reach a
    claimed endpoint); a PostgreSQL URL whose effective endpoint cannot
    be fully resolved (unparseable, missing host/name, a ``service``
    indirection, a host/hostaddr or port arity libpq cannot pair, an
    empty dial member selecting the default unix-socket behavior, an
    undialable port, a proxy chain that cannot resolve, or a unix-socket
    path that bypasses host comparison) returns None so callers fail
    closed instead of treating a supported-but-unrecognized URL as
    unclaimed. A dial target that
    does not resolve keeps only its literal spelling: it cannot reach a
    claimed database, and refusing it would make unrelated unclaimed
    names undeployable.
    """
    try:
        config = resolve_app_database_config(environment)
    except (ImproperlyConfigured, RecursionError, TypeError, ValueError):
        return None
    if not config or config.get("ENGINE") not in POSTGRES_WIRE_ENGINES:
        return []
    resolved = _effective_connection_target(config, environment)
    if resolved is None:
        return None
    targets, ports, name = resolved
    identities: list[tuple[str, str, str]] = []
    for index, member in enumerate(targets):
        spellings = _resolve_dial_target(member)
        if not spellings or spellings[0].startswith("/"):
            # A unix-socket directory bypasses host comparison entirely.
            return None
        if not ports:
            member_port = POSTGRES_DEFAULT_PORT
        elif len(ports) == 1:
            # A single port applies to every listed target.
            member_port = ports[0]
        else:
            member_port = ports[index]
        normalized_port = _normalize_port(member_port)
        if normalized_port is None:
            return None
        identities.extend((spelling, normalized_port, name) for spelling in spellings)
    return identities


def _normalize_dial_target(member: str) -> str:
    """Canonicalize one libpq dial target for endpoint comparison.

    libpq dials hostaddr/host strings through the system resolver, so
    spellings naming the same address must compare equal: bracketed IPv6
    literals lose their brackets, IPv4-mapped IPv6 addresses
    (``::ffff:a.b.c.d``) dial the embedded IPv4 endpoint, other IP
    literals canonicalize, non-canonical IPv4 spellings the resolver
    accepts (octal or hex octets, shortened forms) canonicalize through
    ``inet_aton``, and a trailing root dot is the same absolute name.
    Hostnames compare case-folded.
    """
    normalized = member.removeprefix("[").removesuffix("]").lower()
    normalized = normalized.removesuffix(".")
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        try:
            return socket.inet_ntoa(socket.inet_aton(normalized))
        except OSError:
            return normalized
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


def _resolve_dial_target(member: str) -> list[str]:
    """Return every endpoint spelling one dial target can reach.

    The first element is always the canonicalized literal; a hostname
    additionally yields every address ``getaddrinfo`` returns for it,
    canonicalized the same way, so a claim on ``172.17.0.2`` also
    refuses ``172.17.0.2.nip.io`` and vice versa. A target that does
    not resolve keeps only its literal spelling — it cannot reach a
    claimed database, and the live contract fails closed on it
    separately.
    """
    normalized = _normalize_dial_target(member)
    if not normalized or normalized.startswith("/"):
        return [normalized]
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        return [normalized]
    try:
        infos = socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM)
    except OSError:
        return [normalized]
    spellings = [normalized]
    for info in infos:
        address = _normalize_dial_target(str(info[4][0]))
        if address and address not in spellings:
            spellings.append(address)
    return spellings


def _normalize_port(member: str) -> str | None:
    """Canonicalize one libpq port element; None when libpq cannot dial it."""
    if not member.isdecimal():
        return None
    port = int(member)
    if not 1 <= port <= POSTGRES_MAX_PORT:
        return None
    return str(port)


def _effective_connection_target(
    config: Mapping[str, Any], environment: Mapping[str, str]
) -> tuple[list[str], list[str], str] | None:
    """Merge one parsed DSN the way the PostgreSQL driver merges it.

    Returns ``(dial targets, ports, database)`` or None when the effective
    target cannot be resolved. Mirrors ``get_connection_params``: OPTIONS
    override the parsed NAME (``?dbname=``), supply host/port only when
    the URL authority leaves them empty, and ``hostaddr`` — not ``host`` —
    selects the address libpq dials (``host`` then only names the server
    for authentication and TLS). Parameters the DSN leaves unset fall
    through to libpq's environment exactly like the driver's keyword
    forwarding: ``PGHOSTADDR`` reroutes even an explicit host, and
    ``PGHOST``/``PGPORT`` apply only when no conninfo value was given.
    libpq pairs host and hostaddr lists element-wise and an empty
    hostaddr member falls back to the paired host member — the list is
    resolved per member, never as a whole. A ``service`` option or
    ``PGSERVICE`` delegates routing to pg_service.conf, an empty
    authority without env routing falls back to the local unix socket,
    an empty dial member selects the default unix-socket behavior, and
    a host/hostaddr or port arity libpq cannot pair has no modelable
    target: all fail closed.
    """
    options = config.get("OPTIONS")
    if not isinstance(options, dict):
        options = {}
    effective_name = _effective_database_name(config, options, environment)
    if effective_name is None:
        return None
    hosts, hostaddrs, ports = _libpq_routing_parameters(config, options, environment)
    if hostaddrs and hosts and len(hostaddrs) != len(hosts):
        # libpq pairs host and hostaddr lists element-wise.
        return None
    if hostaddrs and hosts:
        # An empty hostaddr member dials the paired host member.
        targets = [
            hostaddr or host for hostaddr, host in zip(hostaddrs, hosts, strict=True)
        ]
    else:
        targets = hostaddrs or hosts
    if not targets or any(not member for member in targets):
        # No dial target, or an empty member selecting the default
        # unix-socket behavior that bypasses host comparison.
        return None
    if len(ports) not in (0, 1, len(targets)):
        # A port list libpq cannot pair with the target list.
        return None
    return targets, ports, effective_name


def _effective_database_name(
    config: Mapping[str, Any],
    options: Mapping[str, Any],
    environment: Mapping[str, str],
) -> str | None:
    """Resolve the database libpq would open; None when unresolvable."""
    name = config.get("NAME")
    if not isinstance(name, str) or not name:
        return None
    if options.get("service") or environment.get("PGSERVICE"):
        # pg_service.conf routing cannot be resolved here.
        return None
    # get_connection_params builds {"dbname": NAME, **OPTIONS}: an OPTIONS
    # dbname overrides the URL path name for the actual connection.
    effective_name = options.get("dbname", name)
    if not isinstance(effective_name, str) or not effective_name:
        return None
    return effective_name


def _libpq_routing_parameters(
    config: Mapping[str, Any],
    options: Mapping[str, Any],
    environment: Mapping[str, str],
) -> tuple[list[str], list[str], list[str]]:
    """Resolve the host, hostaddr and port lists libpq would dial.

    The driver forwards OPTIONS host/port only when the settings values
    are empty, so a non-empty URL authority wins over ?host=/?port=.
    Parameters the DSN leaves unset fall through to libpq's environment:
    ``PGHOSTADDR`` reroutes even an explicit host, and ``PGHOST``/
    ``PGPORT`` apply only when no conninfo value was given — an empty
    environment value is unset, while an empty conninfo value stands and
    blocks the fallback.
    """
    host = config.get("HOST")
    port = config.get("PORT")
    if not isinstance(host, str) or not host:
        host = options.get("host")
    if port in (None, ""):
        port = options.get("port")
    if host is None:
        host = environment.get("PGHOST") or None
    if port is None:
        port = environment.get("PGPORT") or None
    hostaddr = options.get("hostaddr")
    if hostaddr is None:
        hostaddr = environment.get("PGHOSTADDR") or None
    hosts = str(host).split(",") if isinstance(host, str) and host else []
    hostaddrs = (
        str(hostaddr).split(",") if isinstance(hostaddr, str) and hostaddr else []
    )
    ports = [] if port is None else str(port).split(",")
    return hosts, hostaddrs, ports


def _is_loopback_host(host: str) -> bool:
    """Return True for localhost names and loopback IP addresses."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _security_findings(environment: Mapping[str, str]) -> list[str]:
    """Validate the secret, host and debug parts of the live contract."""
    findings: list[str] = []
    try:
        validate_runtime_secret(environment.get("SECRET_KEY", ""))
    except ImproperlyConfigured:
        findings.append("SECRET_KEY violates the runtime secret contract")
    try:
        hosts = parse_production_hosts(environment.get("ALLOWED_HOSTS", ""))
    except ImproperlyConfigured:
        findings.append("ALLOWED_HOSTS violates the production host contract")
    else:
        try:
            validate_secure_ssl_host(environment.get("SECURE_SSL_HOST", ""), hosts)
        except ImproperlyConfigured:
            findings.append("SECURE_SSL_HOST violates the SSL host contract")
    if environment.get("DEBUG", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        findings.append("DEBUG must not be enabled for live")
    return findings


def _database_findings(environment: Mapping[str, str]) -> list[str]:
    """Validate the live database DSN and its isolation from synthetic."""
    identity = _database_identity(environment)
    if identity is None:
        return ["APP_DATABASE_URL violates the fail-closed database contract"]
    host, port, name = identity
    spellings = _resolve_dial_target(host)
    findings: list[str] = []
    if len(spellings) == 1 and not _is_ip_literal(host):
        # A live host that cannot be resolved cannot claim the storage it
        # would reach through a later-resolving alias: fail closed.
        findings.append(
            "APP_DATABASE_URL database host could not be resolved to an address"
        )
    if any(_is_loopback_host(spelling) for spelling in spellings):
        findings.append("APP_DATABASE_URL must not use a loopback address for live")
    if (host, name) == SYNTHETIC_DEFAULT_DATABASE:
        findings.append(
            "APP_DATABASE_URL must not use the synthetic default database identity"
        )
    if name.startswith("test_"):
        findings.append("APP_DATABASE_URL must not name a test database")
    findings.extend(
        f"{variable} must not reroute the approved live database endpoint"
        for variable in sorted(LIBPQ_REROUTE_ENV_VARS)
        if environment.get(variable)
    )
    for spelling in spellings:
        spelling_identity = (spelling, port, name)
        claim, error = _read_database_claim(_database_claim_path(spelling_identity))
        if claim is None:
            if error is not None:
                findings.append(f"APP_DATABASE_URL ownership claim is {error}")
        elif not _owns_database_claim(claim, environment, spelling_identity):
            findings.append(
                "APP_DATABASE_URL is claimed by a different live activation"
            )
    return findings


def _is_ip_literal(host: str) -> bool:
    """Return True when the normalized target is already an IP address."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _secret_material_findings(backend: str, directory: str) -> list[str]:
    """Probe that the configured store returns the required key material."""
    if backend != "synthetic-file":
        # Approved managed backends probe through their own integration when
        # one is implemented; until then no such backend can be configured.
        return []
    from apps.core.secrets import (  # noqa: PLC0415 - deferred; keeps the CLI
        FileSecretStore,  # importable without the app boundary
        SecretStoreContractError,
        SecretUnavailableError,
    )

    try:
        FileSecretStore(directory).get_secret(REQUIRED_SECRET_NAME)
    except (SecretUnavailableError, SecretStoreContractError):
        return [
            f"configured secret store cannot return {REQUIRED_SECRET_NAME!r} "
            "key material"
        ]
    return []


def _secret_store_findings(
    environment: Mapping[str, str], *, rehearsal: bool = False
) -> list[str]:
    """Validate the live secret-store contract and runtime capability.

    Rehearsal-only backends never satisfy the production contract; they are
    admitted only when ``rehearsal`` evaluates the transition mechanics for
    ``activate``/``preflight`` under the explicit rehearsal opt-in.
    """
    findings: list[str] = []
    backend = environment.get("CLINIC_SECRET_BACKEND")
    directory = environment.get("CLINIC_SECRET_DIR")
    if backend is None and directory is None:
        findings.append(
            "CLINIC_SECRET_BACKEND and CLINIC_SECRET_DIR are required for live"
        )
        return findings
    try:
        resolved_backend, secret_dir = validate_secret_store_env(backend, directory)
    except ImproperlyConfigured as error:
        findings.append(str(error))
        return findings
    if resolved_backend is None or secret_dir is None or not Path(secret_dir).is_dir():
        findings.append("CLINIC_SECRET_DIR must be an existing directory")
        return findings
    if resolved_backend in REHEARSAL_SECRET_BACKENDS and not rehearsal:
        findings.append(
            f"CLINIC_SECRET_BACKEND {resolved_backend!r} is a rehearsal-only "
            "backend; live requires the approved managed secret store"
        )
    findings.extend(_secret_material_findings(resolved_backend, secret_dir))
    return findings


def _owner_marker_path(resolved_root: Path) -> Path:
    """Return the sibling ownership-marker path for one attachment root."""
    return resolved_root.with_name(resolved_root.name + STORAGE_OWNER_MARKER_SUFFIX)


def _read_owner_marker(
    resolved_root: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    """Read the root's ownership marker; return (marker, error-kind)."""
    try:
        raw = _owner_marker_path(resolved_root).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, "unreadable"
    try:
        marker = json.loads(raw)
    except json.JSONDecodeError:
        return None, "invalid"
    if not isinstance(marker, dict) or not isinstance(marker.get("owner"), str):
        return None, "invalid"
    return marker, None


def _write_owner_marker(resolved_root: Path, payload: dict[str, Any]) -> None:
    """Create the ownership marker exclusively; never overwrite a claim."""
    marker_path = _owner_marker_path(resolved_root)
    payload_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    descriptor = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload_bytes)


def _live_marker_findings(
    environment: Mapping[str, str], resolved_root: Path
) -> list[str]:
    """Check the attachment root is not claimed by another storage owner."""
    marker, error = _read_owner_marker(resolved_root)
    if marker is None:
        if error is None:
            return []
        return [f"EHR_ATTACHMENT_ROOT ownership marker is {error}"]
    if marker.get("root") != str(resolved_root):
        return ["EHR_ATTACHMENT_ROOT ownership marker does not name this root"]
    owner = marker.get("owner")
    findings: list[str] = []
    if owner == SYNTHETIC_DATA_MODE:
        findings.append("EHR_ATTACHMENT_ROOT is claimed by synthetic storage")
    elif owner != LIVE_DATA_MODE:
        findings.append("EHR_ATTACHMENT_ROOT ownership marker is invalid")
    elif marker.get("activation_state") != environment.get(ACTIVATION_STATE_ENV, ""):
        findings.append("EHR_ATTACHMENT_ROOT is claimed by a different live activation")
    return findings


def _storage_findings(
    environment: Mapping[str, str], *, rehearsal: bool = False
) -> list[str]:
    """Validate attachment-root and secret-store isolation for live."""
    findings: list[str] = []
    attachment_root = environment.get("EHR_ATTACHMENT_ROOT", "")
    attachment_path = Path(attachment_root)
    if not attachment_root or not attachment_path.is_absolute():
        findings.append("EHR_ATTACHMENT_ROOT must be an absolute path")
    else:
        resolved = attachment_path.resolve()
        if resolved == SYNTHETIC_DEFAULT_ATTACHMENT_ROOT or (
            SYNTHETIC_DEFAULT_ATTACHMENT_ROOT in resolved.parents
        ):
            findings.append(
                "EHR_ATTACHMENT_ROOT must not use the synthetic default attachment root"
            )
        elif not resolved.is_dir():
            findings.append("EHR_ATTACHMENT_ROOT must be an existing directory")
        else:
            findings.extend(_live_marker_findings(environment, resolved))
    findings.extend(_secret_store_findings(environment, rehearsal=rehearsal))
    return findings


def _activation_env_findings(environment: Mapping[str, str]) -> list[str]:
    """Validate the activation-state, evidence and job-dispatch variables."""
    findings: list[str] = []
    state = environment.get(ACTIVATION_STATE_ENV, "")
    if not state or not Path(state).is_absolute():
        findings.append(f"{ACTIVATION_STATE_ENV} must be an absolute path")
    elif not Path(state).parent.is_dir():
        findings.append(f"{ACTIVATION_STATE_ENV} parent directory does not exist")
    evidence_root = environment.get(readiness.EVIDENCE_ROOT_ENV, "")
    if not evidence_root or not Path(evidence_root).is_dir():
        findings.append(
            f"{readiness.EVIDENCE_ROOT_ENV} must name an existing directory"
        )
    if not environment.get(readiness.RELEASE_ID_ENV, "").strip():
        findings.append(f"{readiness.RELEASE_ID_ENV} is required for live")
    if not environment.get("CELERY_BROKER_URL", "").strip():
        findings.append("CELERY_BROKER_URL is required for live")
    findings.extend(
        f"{name} is synthetic-only and must be unset"
        for name in SYNTHETIC_ONLY_ENV_VARS
        if _env_flag_set(environment, name)
    )
    return findings


def live_environment_findings(
    environment: Mapping[str, str], *, rehearsal: bool = False
) -> list[str]:
    """Return every reason the environment cannot run the live data mode.

    The checks mirror the production settings contract plus the live-only
    requirements: explicit non-synthetic storage with clean ownership, an
    approved (non-rehearsal) secret backend that returns the required key
    material, no synthetic-only provider flags and no debug/eager shortcuts.
    The rehearsal opt-in is itself rejected: it marks a rehearsal of the
    transition mechanics, never a live prerequisite. ``rehearsal=True``
    evaluates that rehearsal surface for ``activate``/``preflight`` only.
    """
    findings: list[str] = []
    if environment.get("CLINIC_DATA_MODE") != LIVE_DATA_MODE:
        findings.append("CLINIC_DATA_MODE must be 'live'")
    if environment.get("DJANGO_SETTINGS_MODULE") not in LIVE_SETTINGS_MODULES:
        findings.append(
            "DJANGO_SETTINGS_MODULE must be one of: "
            + ", ".join(sorted(LIVE_SETTINGS_MODULES))
        )
    if not rehearsal and _env_flag_set(environment, LIVE_REHEARSAL_ENV):
        findings.append(
            f"{LIVE_REHEARSAL_ENV} marks a rehearsal activation and is never "
            "a live prerequisite"
        )
    findings.extend(_security_findings(environment))
    findings.extend(_database_findings(environment))
    findings.extend(_storage_findings(environment, rehearsal=rehearsal))
    findings.extend(_activation_env_findings(environment))
    return findings


def _evidence_manifest_digest(root: Path) -> str:
    """Digest the sorted relative paths and bytes of every evidence file."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\x00")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _read_record(state_path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Read the activation record; return (record, error-kind)."""
    try:
        raw = state_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        return None, "corrupt"
    if not isinstance(record, dict):
        return None, "corrupt"
    return record, None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace one JSON file; never leave partial bytes."""
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            Path(temporary).unlink()
        raise


def _binding_findings(
    record: dict[str, Any],
    environment: Mapping[str, str],
    now: datetime,
    *,
    authorize: bool = True,
) -> list[str]:
    """Return every way an activation record fails to bind this environment.

    ``authorize=True`` is the startup/runtime gate: a rehearsal record fails
    unconditionally. ``authorize=False`` is the identity check used by
    re-activation and rehearsal preflight, where the record's rehearsal flag
    must match the environment's opt-in instead.
    """
    findings: list[str] = []
    if record.get("version") != RECORD_VERSION:
        findings.append("activation record version is unsupported")
    if record.get("status") != "active":
        findings.append("activation record is not active")
    if record.get("release_id") != environment.get(readiness.RELEASE_ID_ENV):
        findings.append("activation record release_id does not match")
    if record.get("environment") != LIVE_DATA_MODE:
        findings.append("activation record environment is not 'live'")
    if record.get("system") != readiness.SYSTEM_IDENTIFIER:
        findings.append("activation record system does not match")
    approval = record.get("approval_reference")
    if not isinstance(approval, str) or not approval.strip():
        findings.append("activation record lacks an approval reference")
    activated_at = _parse_timestamp(record.get("activated_at"))
    if activated_at is None or activated_at > now:
        findings.append("activation record activated_at is invalid")
    if record.get("disabled_at") is not None:
        findings.append("activation record carries a disabled_at timestamp")
    findings.extend(
        _rehearsal_binding_findings(record, environment, authorize=authorize)
    )
    findings.extend(_evidence_binding_findings(record, environment))
    findings.extend(_storage_binding_findings(record, environment))
    return findings


def _rehearsal_binding_findings(
    record: dict[str, Any], environment: Mapping[str, str], *, authorize: bool
) -> list[str]:
    """Check the record's rehearsal flag against this evaluation's purpose.

    A rehearsal record can never authorize live mode, whatever the current
    environment carries; the flag only marks the record. For identity
    checks (re-activation, rehearsal preflight) the record's flag must
    match the environment's opt-in instead.
    """
    if authorize:
        if record.get("rehearsal") is not False:
            return ["activation record is a rehearsal and cannot authorize live mode"]
        return []
    if record.get("rehearsal") is not _env_flag_set(environment, LIVE_REHEARSAL_ENV):
        return ["activation record rehearsal binding does not match"]
    return []


def _evidence_binding_findings(
    record: dict[str, Any], environment: Mapping[str, str]
) -> list[str]:
    """Check the record's evidence root and manifest digest binding."""
    evidence_root = Path(environment.get(readiness.EVIDENCE_ROOT_ENV, ""))
    if record.get("evidence_root") != str(evidence_root.resolve()):
        return ["activation record evidence_root does not match"]
    if _evidence_manifest_digest(evidence_root) != record.get(
        "evidence_manifest_sha256"
    ):
        return ["release evidence changed since activation"]
    return []


def _storage_binding_findings(
    record: dict[str, Any], environment: Mapping[str, str]
) -> list[str]:
    """Check the record's endpoint and attachment-root storage binding."""
    storage = record.get("storage")
    if not isinstance(storage, dict) or set(storage) != STORAGE_FIELDS:
        return ["activation record storage binding is malformed"]
    findings: list[str] = []
    identity = _database_identity(environment)
    if identity is None or identity != (
        storage["database_host"],
        storage["database_port"],
        storage["database_name"],
    ):
        findings.append("activation record database binding does not match")
    if storage["broker_url"] != environment.get("CELERY_BROKER_URL", ""):
        findings.append("activation record broker binding does not match")
    attachment = Path(environment.get("EHR_ATTACHMENT_ROOT", ""))
    if storage["attachment_root"] != str(attachment.resolve()):
        findings.append("activation record attachment_root binding does not match")
    return findings


def activation_record_findings(
    record: object,
    environment: Mapping[str, str],
    now: datetime,
    *,
    authorize: bool = True,
) -> list[str]:
    """Return every reason an activation record cannot authorize live mode."""
    if not isinstance(record, dict):
        return ["activation record is not a JSON object"]
    keys = set(record)
    if keys != RECORD_FIELDS:
        detail = []
        if missing := sorted(RECORD_FIELDS - keys):
            detail.append(f"missing fields {missing}")
        if extra := sorted(keys - RECORD_FIELDS):
            detail.append(f"unexpected fields {extra}")
        return [f"activation record violates the closed schema ({'; '.join(detail)})"]
    return _binding_findings(record, environment, now, authorize=authorize)


def _live_findings(environment: Mapping[str, str]) -> list[str]:
    """Every reason this environment cannot hold the live data mode now."""
    findings = live_environment_findings(environment)
    if not findings:
        state_path = Path(environment[ACTIVATION_STATE_ENV])
        record, error = _read_record(state_path)
        if record is None:
            findings.append(
                f"activation record at {state_path} is {error or 'missing'}"
            )
        else:
            findings.extend(activation_record_findings(record, environment, _now()))
    if not findings:
        report = readiness.evaluate_evidence(
            Path(environment[readiness.EVIDENCE_ROOT_ENV]),
            LIVE_DATA_MODE,
            environment[readiness.RELEASE_ID_ENV],
        )
        if not report["ready"]:
            findings.append(
                "release evidence is not ready for live "
                f"({len(report['missing'])} unsatisfied capabilities, "
                f"{len(report['errors'])} errors)"
            )
    return findings


def require_live_activation(environment: Mapping[str, str]) -> str:
    """Fail closed unless this exact environment holds a valid activation.

    Called from ``config.settings.contracts.require_data_mode`` on every
    settings import; raises ``ImproperlyConfigured`` listing the findings.
    """
    findings = _live_findings(environment)
    if findings:
        message = "live data mode is not approved: " + "; ".join(findings)
        raise ImproperlyConfigured(message)
    return LIVE_DATA_MODE


def require_live_runtime(environment: Mapping[str, str]) -> None:
    """Re-validate the live contract inside a running process.

    Request, job and storage boundaries call this on every unit of work so
    ``disable`` halts new live writes and jobs without waiting for a
    restart. No-op outside live mode; raises ``LiveModeHaltedError`` with
    the findings otherwise.
    """
    if environment.get("CLINIC_DATA_MODE") != LIVE_DATA_MODE:
        return
    findings = _live_findings(environment)
    if findings:
        message = "live data mode is halted: " + "; ".join(findings)
        raise LiveModeHaltedError(message)


def _claim_failure() -> StorageOwnershipError:
    """Build the shared foreign-claim failure for both storage owners."""
    message = "attachment root is already claimed by another storage owner"
    return StorageOwnershipError(message)


def claim_synthetic_storage(root: Path) -> None:
    """Claim an attachment root for synthetic storage or fail closed.

    Every synthetic storage mutation claims its root with a sibling
    ownership marker. A root already claimed by a live activation — or an
    unreadable/foreign marker — raises ``StorageOwnershipError`` so
    synthetic bytes can never land in approved live storage.
    """
    resolved = root.resolve()
    marker, error = _read_owner_marker(resolved)
    if marker is not None:
        if marker.get("owner") == SYNTHETIC_DATA_MODE and marker.get("root") == str(
            resolved
        ):
            return
        raise _claim_failure()
    if error is not None:
        message = f"attachment root ownership marker is {error}"
        raise StorageOwnershipError(message)
    payload = {
        "owner": SYNTHETIC_DATA_MODE,
        "root": str(resolved),
        "claimed_at": _now().isoformat(),
    }
    try:
        _write_owner_marker(resolved, payload)
    except FileExistsError:
        marker, error = _read_owner_marker(resolved)
        if (
            marker is not None
            and marker.get("owner") == SYNTHETIC_DATA_MODE
            and marker.get("root") == str(resolved)
        ):
            return
        raise _claim_failure() from None
    except OSError as exc:
        message = "attachment root ownership marker could not be written"
        raise StorageOwnershipError(message) from exc


def _claim_live(environment: Mapping[str, str], resolved_root: Path) -> None:
    """Claim an attachment root for this live activation or fail closed."""
    marker, error = _read_owner_marker(resolved_root)
    state = environment.get(ACTIVATION_STATE_ENV, "")
    release = environment.get(readiness.RELEASE_ID_ENV, "")

    def _owns(marker: dict[str, Any] | None) -> bool:
        return (
            marker is not None
            and marker.get("owner") == LIVE_DATA_MODE
            and marker.get("root") == str(resolved_root)
            and marker.get("activation_state") == state
            and marker.get("release_id") == release
        )

    if marker is not None:
        if _owns(marker):
            return
        raise _claim_failure()
    if error is not None:
        message = f"attachment root ownership marker is {error}"
        raise StorageOwnershipError(message)
    payload = {
        "owner": LIVE_DATA_MODE,
        "root": str(resolved_root),
        "activation_state": state,
        "release_id": release,
        "claimed_at": _now().isoformat(),
    }
    try:
        _write_owner_marker(resolved_root, payload)
    except FileExistsError:
        marker, _error = _read_owner_marker(resolved_root)
        if _owns(marker):
            return
        raise _claim_failure() from None
    except OSError as exc:
        message = "attachment root ownership marker could not be written"
        raise StorageOwnershipError(message) from exc


def authorize_live_storage_mutation(environment: Mapping[str, str], root: Path) -> None:
    """Authorize one live attachment mutation or fail closed.

    Running live processes call this before every storage mutation: the
    activation must still be valid for this environment and the root must
    carry this activation's ownership marker. A disabled or drifted
    activation raises ``LiveModeHaltedError``; a root not claimed by this
    activation raises ``StorageOwnershipError``.
    """
    require_live_runtime(environment)
    resolved = root.resolve()
    marker, _error = _read_owner_marker(resolved)
    if (
        marker is None
        or marker.get("owner") != LIVE_DATA_MODE
        or marker.get("root") != str(resolved)
        or marker.get("activation_state") != environment.get(ACTIVATION_STATE_ENV, "")
    ):
        message = "attachment root is not claimed by the current live activation"
        raise StorageOwnershipError(message)


def _effective_attachment_root(environment: Mapping[str, str]) -> Path | None:
    """Resolve the attachment root the synthetic settings would open.

    The isolation check must see the same root the selected synthetic
    settings derive, so ``EHR_ATTACHMENT_ROOT`` is resolved through the
    shared ``config.settings.contracts.resolve_attachment_root`` seam —
    the identical ``env`` call ``config.settings.base`` makes, including
    its ``$VARIABLE`` proxy indirection and the synthetic-default
    fallback — rather than re-reading the raw variable. A proxy chain
    that cannot resolve (a cycle) or a value that cannot form a path
    returns None so callers fail closed instead of treating an
    unresolvable root as unclaimed.
    """
    try:
        return Path(resolve_attachment_root(environment)).resolve()
    except (
        ImproperlyConfigured,
        OSError,
        RecursionError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return None


def synthetic_isolation_findings(environment: Mapping[str, str]) -> list[str]:
    """Return every way synthetic mode would reuse live-bound storage.

    Called from ``config.settings.contracts.require_data_mode`` on every
    synthetic startup. Isolation does not depend on the caller retaining the
    activation-state pointer: the configured database endpoint is checked
    against the deployment-local claim registry and the attachment root
    against its ownership marker unconditionally. When a readable activation
    record is also supplied, the configured storage must additionally not
    match the storage the record bound; a record that cannot be parsed fails
    closed.
    """
    findings: list[str] = []
    resolved = _effective_attachment_root(environment)
    if resolved is None:
        findings.append("EHR_ATTACHMENT_ROOT could not be resolved")
    else:
        findings.extend(_synthetic_marker_findings(resolved))
    findings.extend(_claimed_database_findings(environment))
    state = environment.get(ACTIVATION_STATE_ENV, "")
    if not state or not Path(state).is_absolute():
        return findings
    record, error = _read_record(Path(state))
    if record is None:
        if error == "missing":
            return findings
        findings.append(f"activation record at {state} is {error or 'unreadable'}")
        return findings
    findings.extend(_record_storage_findings(record, environment))
    return findings


def _record_storage_findings(
    record: dict[str, Any], environment: Mapping[str, str]
) -> list[str]:
    """Check configured storage against one activation record's binding."""
    findings: list[str] = []
    storage = record.get("storage")
    if not isinstance(storage, dict):
        return findings
    identities = _effective_database_identities(environment)
    bound_host = storage.get("database_host")
    bound_port = storage.get("database_port")
    bound_name = storage.get("database_name")
    if identities is not None and isinstance(bound_host, str):
        bound_spellings = _resolve_dial_target(bound_host)
        if any(
            (spelling, bound_port, bound_name) in identities
            for spelling in bound_spellings
        ):
            findings.append(
                "APP_DATABASE_URL matches the database bound to a live activation"
            )
    attachment = _effective_attachment_root(environment)
    bound_root = storage.get("attachment_root")
    if (
        attachment is not None
        and isinstance(bound_root, str)
        and str(attachment) == bound_root
    ):
        findings.append(
            "EHR_ATTACHMENT_ROOT matches the attachment root bound to a live activation"
        )
    return findings


def _database_claim_path(identity: tuple[str, str, str]) -> Path:
    """Return the registry file claiming one database endpoint."""
    key = "\x00".join(identity).encode("utf-8")
    digest = hashlib.sha256(key).hexdigest()
    return DATABASE_CLAIMS_DIR / f"{digest}.json"


def _read_database_claim(
    claim_path: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    """Read one database-ownership claim; return (claim, error-kind)."""
    try:
        raw = claim_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, "unreadable"
    try:
        claim = json.loads(raw)
    except json.JSONDecodeError:
        return None, "invalid"
    if not isinstance(claim, dict):
        return None, "invalid"
    return claim, None


def _database_claim_matches(
    claim: dict[str, Any] | None, identity: tuple[str, str, str]
) -> bool:
    """Return True when the claim is a live claim for this exact endpoint."""
    return (
        claim is not None
        and claim.get("owner") == LIVE_DATA_MODE
        and claim.get("database_host") == identity[0]
        and claim.get("database_port") == identity[1]
        and claim.get("database_name") == identity[2]
    )


def _owns_database_claim(
    claim: dict[str, Any] | None,
    environment: Mapping[str, str],
    identity: tuple[str, str, str],
) -> bool:
    """Return True when this activation owns the endpoint's live claim.

    Ownership is the activation identity (its state path), not the release:
    the same activation re-claiming after an interrupted write or a
    disable/re-activate cycle is idempotent, while a different release on
    the same state still fails the record binding downstream.
    """
    return (
        _database_claim_matches(claim, identity)
        and claim is not None
        and claim.get("activation_state") == environment.get(ACTIVATION_STATE_ENV, "")
    )


def _claimed_database_findings(environment: Mapping[str, str]) -> list[str]:
    """Refuse a database endpoint claimed by a live activation.

    Every endpoint the configured URL can reach is checked: a PostgreSQL
    URL whose endpoint cannot be resolved fails closed rather than passing
    as unclaimed.
    """
    identities = _effective_database_identities(environment)
    if identities is None:
        return ["APP_DATABASE_URL database endpoint could not be resolved"]
    findings: list[str] = []
    for identity in identities:
        claim, error = _read_database_claim(_database_claim_path(identity))
        if claim is None:
            if error is not None:
                findings.append(f"APP_DATABASE_URL ownership claim is {error}")
        elif _database_claim_matches(claim, identity):
            findings.append("APP_DATABASE_URL is claimed by a live activation")
        else:
            findings.append("APP_DATABASE_URL ownership claim is invalid")
    return list(dict.fromkeys(findings))


def _claim_live_database(
    environment: Mapping[str, str], identity: tuple[str, str, str]
) -> None:
    """Claim the database endpoint for this live activation or fail closed.

    One claim is written per spelling the endpoint answers to — the
    literal dial target plus every address the resolver returns for it —
    so the claim protects the reachable storage, not one spelling of it.
    The claims persist after ``disable`` — the endpoint still holds
    live-bound data — and are owned by the activation state path that
    created them, so only that same activation can re-claim them.
    """
    for spelling in _resolve_dial_target(identity[0]):
        _claim_database_spelling(environment, (spelling, identity[1], identity[2]))


def _claim_database_spelling(
    environment: Mapping[str, str], identity: tuple[str, str, str]
) -> None:
    """Claim one endpoint spelling for this live activation or fail closed."""
    payload = {
        "owner": LIVE_DATA_MODE,
        "database_host": identity[0],
        "database_port": identity[1],
        "database_name": identity[2],
        "activation_state": environment.get(ACTIVATION_STATE_ENV, ""),
        "release_id": environment.get(readiness.RELEASE_ID_ENV, ""),
        "claimed_at": _now().isoformat(),
    }
    try:
        DATABASE_CLAIMS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            _database_claim_path(identity),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(
                (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
            )
    except FileExistsError:
        claim, _error = _read_database_claim(_database_claim_path(identity))
        if _owns_database_claim(claim, environment, identity):
            return
        message = "database endpoint is already claimed by another live activation"
        raise StorageOwnershipError(message) from None
    except OSError as exc:
        message = "database endpoint ownership claim could not be written"
        raise StorageOwnershipError(message) from exc


def _synthetic_marker_findings(resolved_root: Path) -> list[str]:
    """Check the synthetic attachment root is not claimed by live storage."""
    marker, error = _read_owner_marker(resolved_root)
    if marker is None:
        if error is None:
            return []
        return [f"EHR_ATTACHMENT_ROOT ownership marker is {error}"]
    if marker.get("owner") == SYNTHETIC_DATA_MODE and marker.get("root") == str(
        resolved_root
    ):
        return []
    if marker.get("owner") == LIVE_DATA_MODE:
        return ["EHR_ATTACHMENT_ROOT is claimed by live storage"]
    return ["EHR_ATTACHMENT_ROOT ownership marker is invalid"]


def _synthetic_preflight(environment: Mapping[str, str]) -> Report:
    """Evaluate the synthetic evidence root and report the live gap."""
    configured = environment.get(readiness.EVIDENCE_ROOT_ENV)
    root = Path(configured) if configured else readiness.default_synthetic_root()
    if not root.is_dir():
        return 2, {
            "operation": "preflight",
            "error": f"evidence root {root} is not a directory",
        }
    release_id = environment.get(readiness.RELEASE_ID_ENV) or (
        readiness.DEFAULT_SYNTHETIC_RELEASE_ID
    )
    report = readiness.evaluate_evidence(root, SYNTHETIC_DATA_MODE, release_id)
    live_gap = live_environment_findings(
        {**environment, "CLINIC_DATA_MODE": LIVE_DATA_MODE}
    )
    isolation = synthetic_isolation_findings(environment)
    ready = report["ready"] and not isolation
    return 0 if ready else 1, {
        "operation": "preflight",
        "mode": SYNTHETIC_DATA_MODE,
        "ready": ready,
        "missing": report["missing"],
        "errors": report["errors"],
        "storage_isolation_findings": isolation,
        "live_gap_report": report["live_gap_report"],
        "live_environment_gap": live_gap,
        "disclaimer": DISCLAIMER,
    }


def _live_preflight(environment: Mapping[str, str]) -> Report:
    """Evaluate the full live contract: environment, approval and evidence.

    Under the rehearsal opt-in this evaluates the rehearsal surface instead:
    the transition mechanics run, but the resulting record can never
    authorize live mode and the report marks itself ``rehearsal: true``.
    """
    rehearsal = _env_flag_set(environment, LIVE_REHEARSAL_ENV)
    findings = live_environment_findings(environment, rehearsal=rehearsal)
    if not environment.get(ACTIVATION_APPROVAL_ENV, "").strip():
        findings.append(
            f"{ACTIVATION_APPROVAL_ENV} is required to authorize live activation"
        )
    root = Path(environment.get(readiness.EVIDENCE_ROOT_ENV, ""))
    release_id = environment.get(readiness.RELEASE_ID_ENV, "").strip()
    if not root.is_dir():
        return 2, {
            "operation": "preflight",
            "mode": LIVE_DATA_MODE,
            "ready": False,
            "rehearsal": rehearsal,
            "environment_findings": findings,
            "error": (
                f"{readiness.EVIDENCE_ROOT_ENV} must name an existing "
                "directory for live preflight"
            ),
        }
    report = readiness.evaluate_evidence(root, LIVE_DATA_MODE, release_id)
    state = environment.get(ACTIVATION_STATE_ENV, "")
    if state and Path(state).is_absolute():
        record, error = _read_record(Path(state))
        if record is None:
            if error != "missing":
                findings.append(
                    f"existing activation record at {state} is {error or 'unreadable'}"
                )
        elif record.get("status") == "active":
            findings.extend(
                activation_record_findings(
                    record, environment, _now(), authorize=not rehearsal
                )
            )
        elif set(record) != RECORD_FIELDS:
            findings.append("existing activation record violates the closed schema")
    ready = report["ready"] and not findings
    return 0 if ready else 1, {
        "operation": "preflight",
        "mode": LIVE_DATA_MODE,
        "ready": ready,
        "rehearsal": rehearsal,
        "environment_findings": findings,
        "missing": report["missing"],
        "errors": report["errors"],
        "capabilities": report["capabilities"],
        "disclaimer": DISCLAIMER,
    }


def _preflight_report(environment: Mapping[str, str]) -> Report:
    """Evaluate the activation contract without changing anything.

    ``synthetic`` is the default only when ``CLINIC_DATA_MODE`` is absent;
    an explicitly empty or unknown value fails the same way startup does.
    """
    mode = environment.get("CLINIC_DATA_MODE")
    if mode is None:
        mode = SYNTHETIC_DATA_MODE
    if mode == SYNTHETIC_DATA_MODE:
        return _synthetic_preflight(environment)
    if mode == LIVE_DATA_MODE:
        return _live_preflight(environment)
    return 2, {
        "operation": "preflight",
        "error": (
            f"CLINIC_DATA_MODE must be 'synthetic' or an approved 'live' (got {mode!r})"
        ),
    }


def _activate_report(environment: Mapping[str, str]) -> Report:
    """Write the activation record after a clean live preflight."""
    if environment.get("CLINIC_DATA_MODE") != LIVE_DATA_MODE:
        return 2, {
            "operation": "activate",
            "error": "activate requires CLINIC_DATA_MODE=live",
        }
    rehearsal = _env_flag_set(environment, LIVE_REHEARSAL_ENV)
    findings = live_environment_findings(environment, rehearsal=rehearsal)
    approval = environment.get(ACTIVATION_APPROVAL_ENV, "").strip()
    if not approval:
        findings.append(
            f"{ACTIVATION_APPROVAL_ENV} is required to authorize live activation"
        )
    root = Path(environment.get(readiness.EVIDENCE_ROOT_ENV, ""))
    release_id = environment.get(readiness.RELEASE_ID_ENV, "").strip()
    if root.is_dir() and release_id:
        report = readiness.evaluate_evidence(root, LIVE_DATA_MODE, release_id)
        if not report["ready"]:
            findings.append(
                "release evidence is not ready for live "
                f"({len(report['missing'])} unsatisfied capabilities, "
                f"{len(report['errors'])} errors)"
            )
    state_path = Path(environment.get(ACTIVATION_STATE_ENV, "activation.json"))
    existing: dict[str, Any] | None = None
    if state_path.exists():
        existing, error = _read_record(state_path)
        if existing is None:
            findings.append(
                f"existing activation record at {state_path} is "
                f"{error or 'unreadable'}; refusing to overwrite"
            )
    if findings:
        return 1, {
            "operation": "activate",
            "activated": False,
            "findings": findings,
            "disclaimer": DISCLAIMER,
        }
    if existing is not None and existing.get("status") == "active":
        return _rebind_report(existing, environment, state_path)
    return _write_activation(
        state_path,
        environment,
        root=root,
        release_id=release_id,
        approval=approval,
    )


def _rebind_report(
    existing: dict[str, Any],
    environment: Mapping[str, str],
    state_path: Path,
) -> Report:
    """Accept an identical active binding; refuse a different live release."""
    if not _binding_findings(existing, environment, _now(), authorize=False):
        return 0, {
            "operation": "activate",
            "activated": True,
            "idempotent": True,
            "state": str(state_path),
            "rehearsal": existing.get("rehearsal") is True,
            "disclaimer": DISCLAIMER,
        }
    return 1, {
        "operation": "activate",
        "activated": False,
        "findings": [
            "a different activation is already active; run disable before "
            "activating a new release"
        ],
        "disclaimer": DISCLAIMER,
    }


def _write_activation(
    state_path: Path,
    environment: Mapping[str, str],
    *,
    root: Path,
    release_id: str,
    approval: str,
) -> Report:
    """Claim the attachment root and atomically write the bound record."""
    identity = _database_identity(environment)
    if identity is None:
        return 1, {
            "operation": "activate",
            "activated": False,
            "findings": ["APP_DATABASE_URL violates the fail-closed database contract"],
            "disclaimer": DISCLAIMER,
        }
    attachment_root = Path(environment["EHR_ATTACHMENT_ROOT"]).resolve()
    try:
        _claim_live_database(environment, identity)
        _claim_live(environment, attachment_root)
    except StorageOwnershipError as error:
        return 1, {
            "operation": "activate",
            "activated": False,
            "findings": [str(error)],
            "disclaimer": DISCLAIMER,
        }
    record = {
        "version": RECORD_VERSION,
        "status": "active",
        "release_id": release_id,
        "environment": LIVE_DATA_MODE,
        "system": readiness.SYSTEM_IDENTIFIER,
        "approval_reference": approval,
        "activated_at": _now().isoformat(),
        "disabled_at": None,
        "rehearsal": _env_flag_set(environment, LIVE_REHEARSAL_ENV),
        "evidence_root": str(root.resolve()),
        "evidence_manifest_sha256": _evidence_manifest_digest(root),
        "storage": {
            "database_host": identity[0],
            "database_port": identity[1],
            "database_name": identity[2],
            "attachment_root": str(attachment_root),
            "broker_url": environment.get("CELERY_BROKER_URL", ""),
        },
    }
    try:
        _write_json_atomic(state_path, record)
    except OSError as error:
        return 2, {
            "operation": "activate",
            "activated": False,
            "error": f"could not write activation record: {error}",
        }
    return 0, {
        "operation": "activate",
        "activated": True,
        "idempotent": False,
        "state": str(state_path),
        "release_id": release_id,
        "rehearsal": record["rehearsal"],
        "disclaimer": DISCLAIMER,
    }


def _disable_report(environment: Mapping[str, str]) -> Report:
    """Mark the activation record disabled; preserve every record and byte."""
    state = environment.get(ACTIVATION_STATE_ENV, "")
    if not state or not Path(state).is_absolute():
        return 2, {
            "operation": "disable",
            "error": f"{ACTIVATION_STATE_ENV} must be an absolute path",
        }
    state_path = Path(state)
    record, error = _read_record(state_path)
    if error == "missing":
        return 0, {
            "operation": "disable",
            "disabled": True,
            "idempotent": True,
            "detail": "no activation record exists; live mode is not active",
            "disclaimer": DISCLAIMER,
        }
    if record is None or set(record) != RECORD_FIELDS:
        return 1, {
            "operation": "disable",
            "disabled": False,
            "error": (
                f"activation record at {state_path} is "
                f"{error or 'schema-invalid'}; refusing to modify it"
            ),
        }
    if record.get("status") == "disabled":
        return 0, {
            "operation": "disable",
            "disabled": True,
            "idempotent": True,
            "state": str(state_path),
            "disclaimer": DISCLAIMER,
        }
    updated = dict(record)
    updated["status"] = "disabled"
    updated["disabled_at"] = _now().isoformat()
    try:
        _write_json_atomic(state_path, updated)
    except OSError as error:
        return 1, {
            "operation": "disable",
            "disabled": False,
            "error": f"could not update activation record: {error}",
        }
    return 0, {
        "operation": "disable",
        "disabled": True,
        "idempotent": False,
        "state": str(state_path),
        "release_id": record.get("release_id"),
        "preserved": (
            "the activation record, evidence root, database and attachment "
            "store are unchanged; no records were deleted or made public"
        ),
        "disclaimer": DISCLAIMER,
    }


def main(argv: list[str] | None = None) -> int:
    """Dispatch the closed ``preflight|activate|disable`` grammar."""
    parser = argparse.ArgumentParser(prog="ops.release.activation")
    operations = parser.add_subparsers(dest="operation", required=True)
    operations.add_parser("preflight", help="evaluate the live-mode contract")
    operations.add_parser("activate", help="write the activation record")
    operations.add_parser("disable", help="roll back the live-mode transition")
    arguments = parser.parse_args(argv)
    handlers: dict[str, Callable[[Mapping[str, str]], Report]] = {
        "preflight": _preflight_report,
        "activate": _activate_report,
        "disable": _disable_report,
    }
    code, report = handlers[arguments.operation](os.environ)
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
