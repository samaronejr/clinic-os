"""Exact-head F5 E1 packet from the existing nine-purpose, database-backed generator."""

import ast
import hashlib
import importlib.util
import json
import os
import subprocess
from datetime import UTC, datetime
from enum import Enum
from functools import lru_cache
from pathlib import Path

from artifacts import regular, require
from manifest_pair import root_manifest
from runtime_common import run, write_json

E = Path(__file__).resolve().parent
ROOT = E.parents[4]
GENERATOR = "tests/ai/test_fresh_evaluation.py"
REGISTRATION = "apps/ai/evaluation/engineering-registration.json"
THRESHOLD_COMMIT = "34dc3f94a20b8e7ddb2aea96b5ceac0c94e8e528"
DOMAIN_PROTOCOL = "apps/ai/evaluation/domain-protocol-v2.json"
DOMAIN_COMMIT = "d7fd91b7cc6d324ad7bbfd2df171d365f17ddcf4"
PACKET_SOURCE = E.parent / "s14-eval/packet.py"
PACKET_SHA256 = "cf863feefe205be5fc1d4c4e703f9d76e06c99a08c880d56f34166964b117e28"
require(hashlib.sha256(regular(PACKET_SOURCE).read_bytes()).hexdigest() == PACKET_SHA256,
        "The reusable s14 packet verifier changed")
packet_spec = importlib.util.spec_from_file_location("f5_s14_packet", PACKET_SOURCE)
packet = importlib.util.module_from_spec(packet_spec)
packet_spec.loader.exec_module(packet)


def sha(content):
    return hashlib.sha256(content).hexdigest()


def git(*arguments):
    return subprocess.check_output(["git", "-C", str(ROOT), *arguments], text=True).strip()


def blobs(head, paths):
    response = subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "--batch"],
        input="".join(f"{head}:{path}\n" for path in paths).encode(),
        capture_output=True, check=True,
    ).stdout
    offset, result = 0, {}
    for path in paths:
        ending = response.index(b"\n", offset)
        header = response[offset:ending].split()
        require(len(header) == 3 and header[1] == b"blob", "Missing source blob")
        size = int(header[2])
        offset = ending + 1
        result[path] = response[offset:offset + size]
        require(response[offset + size:offset + size + 1] == b"\n", "Invalid source framing")
        offset += size + 1
    require(offset == len(response), "Unexpected source objects")
    return result


@lru_cache(maxsize=4)
def preparation(head):
    names = git("ls-tree", "-r", "--name-only", head).splitlines()
    governed = sorted({
        name for name in names
        if name.startswith(("apps/", "config/", "docs/ai/"))
        and Path(name).suffix in {".py", ".json", ".md", ".sql"}
    } | {"uv.lock", "pyproject.toml",
         "locale/pt_BR/LC_MESSAGES/django.po", "locale/pt_BR/LC_MESSAGES/django.mo"})
    paths = sorted(set(governed) | {GENERATOR})
    source = blobs(head, paths)
    require(sha(source[GENERATOR])
            == "ce3836b7a29332ddd49b60037324d1c69ed7551ae1aaf22fc7cbba80915ef6ea",
            "The reviewed nine-purpose generator changed")
    git("merge-base", "--is-ancestor", DOMAIN_COMMIT, head)
    require(source[DOMAIN_PROTOCOL] == blobs(DOMAIN_COMMIT, [DOMAIN_PROTOCOL])[DOMAIN_PROTOCOL],
            "The domain protocol differs from its preregistered version")
    original = blobs(THRESHOLD_COMMIT, [REGISTRATION])[REGISTRATION]
    require(source[REGISTRATION] == original, "The committed preregistration changed")
    registration = json.loads(original)
    protocol = ast.parse(source["apps/ai/evaluation/protocol.py"])
    thresholds = ast.literal_eval(next(
        node.value for node in protocol.body if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name) and node.target.id == "ENGINEERING_THRESHOLDS"
    ))
    require(thresholds == registration["thresholds"]
            and sha(json.dumps(thresholds, sort_keys=True, separators=(",", ":")).encode())
            == registration["thresholds_sha256"], "Thresholds differ from preregistration")
    identity = hashlib.sha256()
    for name in governed:
        content = source[name]
        identity.update(name.encode() + b"\0" + len(content).to_bytes(8, "big") + content)
    purpose_type = Enum("Purpose", {name.upper(): name for name in packet.REPORTS})
    prompt_ast = ast.parse(source["apps/ai/prompt_registry.py"])
    prompt_versions = next(
        node.value for node in prompt_ast.body if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name) and node.target.id == "PROMPT_VERSIONS"
    )
    versions = eval(compile(ast.Expression(prompt_versions), "reviewed-prompt-versions", "eval"),
                    {"__builtins__": {}, "dict": dict, "Purpose": purpose_type})
    source_bindings = {}
    for purpose in packet.REPORTS:
        prompt = f"apps/ai/prompts/{purpose}/v{versions[purpose_type(purpose)]}.md"
        source_bindings[purpose] = {
            "prompt": sha(source[prompt]), "retrieval": identity.hexdigest(),
            "schema": identity.hexdigest(),
        }
    corpus_paths = ["apps/ai/evaluation/corpora/engineering-v1.json",
                    "apps/ai/evaluation/corpora/audio-v1.json"]
    return {
        "reviewed_head": head, "tree": git("rev-parse", head + "^{tree}"),
        "source_committed_at": git("show", "-s", "--format=%cI", head),
        "generator": GENERATOR, "generator_sha256": sha(source[GENERATOR]),
        "packet_verifier_sha256": PACKET_SHA256,
        "threshold_registration_commit": THRESHOLD_COMMIT,
        "threshold_registration_committed_at": git("show", "-s", "--format=%cI", THRESHOLD_COMMIT),
        "thresholds_sha256": registration["thresholds_sha256"],
        "domain_registration_commit": DOMAIN_COMMIT,
        "domain_protocol_sha256": sha(source[DOMAIN_PROTOCOL]),
        "report_contract": {
            "domain_protocol_sha256": sha(source[DOMAIN_PROTOCOL]),
            "review_kind": "scripted_engineering",
            "vote_workflow_complete": True,
            "human_review_complete": False,
        },
        "corpus_sha256": sha(source[corpus_paths[0]] + b"\0" + source[corpus_paths[1]]),
        "source_files": {name: sha(source[name]) for name in [REGISTRATION, DOMAIN_PROTOCOL, *corpus_paths, GENERATOR]},
        "dependency_files": {name: sha(source[name]) for name in ("uv.lock", "pyproject.toml")},
        "source_versions": source_bindings, "requires_database": True,
    }


def command(root):
    return [
        "uv", "run", "--frozen", "--no-sync", "--no-env-file", "pytest",
        "--reuse-db", "-q", "-p", "f5_model_bindings", GENERATOR,
        f"--junitxml={root / 'junit.xml'}",
    ]


def source_identity(head):
    value = {
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "tree": subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], text=True).strip(),
        "status": subprocess.check_output(["git", "status", "--porcelain"], text=True),
    }
    require(value == {"commit": head, "tree": preparation(head)["tree"], "status": ""},
            "The evaluation source is dirty or not the exact final head/tree")
    return value


def preregistration(settings, started):
    """Produce the same complete v2 chronology in real and in-memory packets."""
    head = settings["reviewed_head"]
    source_time = git("show", "-s", "--format=%cI", head)
    require(datetime.fromisoformat(settings["threshold_registration_committed_at"])
            < datetime.fromisoformat(source_time) < started,
            "Threshold registration and final source must predate scoring")
    chronology = {
        "execution_started_at": started.isoformat(), "source_commit": head,
        "source_committed_at": source_time,
        "threshold_registration_commit": THRESHOLD_COMMIT,
        "threshold_registration_committed_at": settings["threshold_registration_committed_at"],
        "thresholds_sha256": settings["thresholds_sha256"],
        "domain_registration_commit": DOMAIN_COMMIT,
        "domain_protocol_sha256": settings["domain_protocol_sha256"],
        "corpus_sha256": settings["corpus_sha256"],
        "source_hash_chronology": [],
    }
    for path, digest in settings["source_files"].items():
        commit, committed_at = git("log", "-1", "--format=%H%x00%cI", head, "--", path).split("\0")
        require(sha(blobs(commit, [path])[path]) == digest
                and datetime.fromisoformat(committed_at) < started,
                "A pinned source did not predate the evaluation")
        chronology["source_hash_chronology"].append({
            "path": path, "sha256": digest, "commit": commit, "committed_at": committed_at,
        })
    return chronology


def generate(environment, output, request, deadline, commands):
    from ops.testing.runtime_paths import runtime_directory

    head = request["head"]
    settings = preparation(head)
    current = json.loads(root_manifest(E).read_bytes())
    require(current["reviewed_head"] == head and current["f5_eval"] == settings
            and request["f5_eval_sha256"]
            == sha(json.dumps(settings, sort_keys=True).encode()),
            "The F5 packet preparation does not bind this final head")
    root = output / "packet"
    root.mkdir(mode=0o700)
    chronology = preregistration(settings, datetime.now(UTC))
    write_json(root / "preregistration.json", chronology)
    before = source_identity(head)
    with runtime_directory(Path(environment["RUNNER_TEMP"]), purpose="f5-eval") as temporary:
        export = {
            "CPI80_FRESH_EVAL_DIR": str(root), "CPI80_SOURCE_COMMIT": head,
            "CPI80_THRESHOLD_COMMIT": THRESHOLD_COMMIT,
            "CPI80_THRESHOLD_COMMITTED_AT": settings["threshold_registration_committed_at"],
        }
        values = {**environment, **export, "TMPDIR": str(temporary), "RUNNER_TEMP": str(temporary),
                  "CELERY_BROKER_URL": "memory://",
                  "PYTHONPATH": os.pathsep.join((str(E), str(Path.cwd()), str(Path.cwd() / "tests")))}
        write_json(root / "command.json", {
            "argv": command(root), "cwd": str(Path.cwd()), "environment_keys": sorted(values),
            "export_environment": export,
        })
        record = run(command(root), values, root / "run.log", deadline, expected=None, stream=True)
        commands.append(record)
        write_json(root / "exit-code.json", {"exit_code": record["exit_code"]})
        require(record["exit_code"] == 0, "The nine-purpose export generator failed")
    write_json(root / "provenance.json", {
        "source_before": before, "source_after": source_identity(head),
        "preparation": settings, "commands": commands,
        "generator_runtime_removed": not temporary.exists(),
        "unit_manifest": request["preparation_manifest_sha256"],
        "external_gates_satisfied": [], "real_world_performance": False,
    })


def finalize(output, result):
    root = output / "packet"
    settings = preparation(result["head"])
    cleanup = {"owned_postgres_removed": result["cleanup"]["postgres_removed"],
               "runtime_removed": json.loads(regular(root / "provenance.json").read_bytes())[
                   "generator_runtime_removed"]}
    require(all(cleanup.values()), "The F5 generator database/runtime has not cleaned up")
    write_json(root / "cleanup.json", cleanup)
    for purpose, capability in packet.REPORTS.items():
        path = root / f"{capability}-{purpose}.json"
        value = json.loads(regular(path).read_bytes())
        require(value["source_commit"] == result["head"], "The generator exported another head")
        value["source_tree"] = settings["tree"]
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        path.chmod(0o600)
        html = root / f"{capability}-{purpose}.html"
        html.write_bytes(
            f"<!-- source-commit:{result['head']} source-tree:{settings['tree']} -->\n".encode()
            + regular(html).read_bytes()
        )
        html.chmod(0o600)
    read = lambda name: packet.read_file(root, name)
    chronology = json.loads(read("preregistration.json"))
    bindings = packet.inspect_reports(read, settings, chronology)
    write_json(root / "MANIFEST.json", {
        "source_commit": result["head"], "source_tree": settings["tree"],
        "provider": "synthetic", "review_kind": "scripted_engineering",
        "reports": 9, "passed_tests": 9, "includes_both_AI_01_legs": True,
        "chronology_verified": True, "preregistration": chronology,
        "domain_registration_commit": settings["domain_registration_commit"],
        "domain_protocol_sha256": settings["domain_protocol_sha256"],
        "threshold_registration_commit": THRESHOLD_COMMIT,
        "threshold_registration_committed_at": settings["threshold_registration_committed_at"],
        "thresholds_sha256": settings["thresholds_sha256"],
        "corpus_sha256": settings["corpus_sha256"], "corpus_version": "engineering-v1",
        "source_bindings": bindings, "command": command(root), "exit_code": 0,
        "junit": "junit.xml", "log": "run.log", "provenance": "provenance.json",
        "external_gates_satisfied": [], "real_world_performance": False,
        "clinical_owner_thresholds": "PENDING-EXTERNAL EG-3",
        "representative_corpus": "PENDING-EXTERNAL EG-11",
        "files": [{"path": path.name, "bytes": path.stat().st_size,
                   "sha256": sha(regular(path).read_bytes())}
                  for path in sorted(root.iterdir()) if path.is_file()],
    })
    verify_packet(root, settings)


def verify_packet(root, settings):
    read = lambda name: packet.read_file(root, name)
    chronology = json.loads(read("preregistration.json"))
    manifest = packet.verify_manifest(read, settings, chronology)
    require(manifest.get("domain_registration_commit") == DOMAIN_COMMIT
            and manifest.get("domain_protocol_sha256") == settings["domain_protocol_sha256"],
            "The manifest omits or substitutes the registered v2 protocol")
    require(manifest["source_tree"] == settings["tree"], "Wrong packet source tree")
    require({"model-bindings.json", "cleanup.json"} <= {
        row["path"] for row in manifest["files"]
    } and chronology["source_commit"] == settings["reviewed_head"]
            and chronology["source_committed_at"] == settings["source_committed_at"],
            "The packet omits model/cleanup hashes or source chronology")
    provenance = json.loads(read("provenance.json"))
    identity = {"commit": settings["reviewed_head"], "tree": settings["tree"], "status": ""}
    require(provenance["source_before"] == provenance["source_after"] == identity
            and provenance["preparation"] == settings
            and json.loads(read("cleanup.json"))
            == {"owned_postgres_removed": True, "runtime_removed": True},
            "The export lacks exact-head/tree provenance and cleanup")
    models = json.loads(read("model-bindings.json"))
    require(models["source_commit"] == settings["reviewed_head"]
            and set(models["versions"]) == set(packet.REPORTS),
            "The model binding receipt is incomplete or from another head")
    for purpose, capability in packet.REPORTS.items():
        stem = f"{capability}-{purpose}"
        value = json.loads(read(stem + ".json"))
        versions = value["report"]["versions"]
        report = value["report"]
        require(set(settings["report_contract"]) <= set(report)
                and {"domain_registration_commit","domain_protocol_sha256"} <= set(chronology)
                and report["domain_protocol_sha256"] == settings["domain_protocol_sha256"]
                and chronology["domain_registration_commit"] == DOMAIN_COMMIT
                and chronology["domain_protocol_sha256"] == settings["domain_protocol_sha256"]
                and report["review_kind"] == "scripted_engineering"
                and report["vote_workflow_complete"] is True
                and report["human_review_complete"] is False,
                "The packet confused scripted voting with human review or used a different protocol")
        require(value["source_tree"] == settings["tree"]
                and versions == models["versions"][purpose]
                and all(versions[key] == digest
                        for key, digest in settings["source_versions"][purpose].items()),
                "A model/prompt/retrieval/schema binding is stale or from another source")
        require(settings["reviewed_head"].encode() in read(stem + ".html")
                and settings["tree"].encode() in read(stem + ".html"),
                "An HTML report lacks its exact head/tree")
    return manifest
