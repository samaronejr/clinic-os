"""Audit an ``strace -f -y -e trace=%file,fchdir`` log of a rehearsal run.

Every successful mutation (open for write, mkdir, rename, unlink, link,
symlink, chmod and the like) must land in the run's private directory, and the
audit must be able to prove it. A path's text is not proof: a link or a late
mount can make ``/run/native/x/y`` a foreign directory. The kernel's fd
annotation (``-y``) is the proof, because it prints the canonical path of the
directory or file the descriptor really names. So a mutation is private only if

* its target resolves to a path under the run directory, and
* the directory that holds the target is proven real: the run directory itself,
  a directory the run created, or one whose canonical path an annotation showed.

Anything that cannot be resolved or proven is a failure, never private.
"""

import argparse
import ast
import collections
import posixpath
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

RUN_NAME = re.compile(r"migration-upgrade-secrets\.[0-9a-f]{32}")
# The supervisor's own seal: nonce-bound, written exclusively then renamed into place.
SEAL = re.compile(r"^migration-upgrade-seal\.[0-9a-f]{32}(?:\.\d+\.new)?$")
PENDING = re.compile(r"^\.verdict-[0-9a-f]+(?:-\d+)?\.pending$")
NAMESPACE = re.compile(r"^/proc/(?:\d+|self)/(?:uid_map|gid_map|setgroups)$")
WRITE = re.compile(r"\bO_(?:WRONLY|RDWR|CREAT|TRUNC|APPEND|TMPFILE)\b")
ANNOTATION = re.compile(r"(\d+)<(/[^>]+)>")
CWD_ANNOTATION = re.compile(r"AT_FDCWD<(/[^>]+)>")
DIRFD = re.compile(r"^(?:AT_FDCWD|-?\d+)(?:<(/[^>]+)>)?$")
RESUMED = re.compile(r"<\.\.\. (\w+) resumed>\s*(.*)")
CALL = re.compile(r"^(\w+)\((.*)\)\s+=\s+(-?\d+)")
RESULT = re.compile(r"\s=\s+(\d+)<(/[^>]+)>")
PROC_FD = re.compile(r"^/proc/(\d+)/fd/(\d+)(/.*)?$")
PUBLIC_DIRECTORY = re.compile(r"^/proc/\d+/fd/\d+/migration-upgrade$")
LAUNCHER = ("/dev/tty", "/tmp/newroot", "/tmp/oldroot")  # noqa: S108 - bwrap setup
OPENS = ("open", "openat", "openat2")
FAILURES = {"outside", "unresolved", "via_link", "unproven"}
Indexes = tuple[tuple[int | None, int], ...]
# name -> ((directory-descriptor index or None, path index), ...) creating or
# removing a name, so the final component is not followed.
NAME_CALLS: dict[str, Indexes] = {
    "mkdir": ((None, 0),),
    "mkdirat": ((0, 1),),
    "rmdir": ((None, 0),),
    "unlink": ((None, 0),),
    "unlinkat": ((0, 1),),
    "rename": ((None, 0), (None, 1)),
    "renameat": ((0, 1), (2, 3)),
    "renameat2": ((0, 1), (2, 3)),
    "link": ((None, 0), (None, 1)),
    "linkat": ((0, 1), (2, 3)),
    "symlink": ((None, 1),),
    "symlinkat": ((1, 2),),
    "mknod": ((None, 0),),
    "mknodat": ((0, 1),),
}
# The same, for calls that follow a final link and so change what it points at.
FOLLOWING_CALLS: dict[str, Indexes] = {
    **dict.fromkeys(
        ("chmod", "chown", "truncate", "utime", "utimes", "setxattr", "removexattr"),
        ((None, 0),),
    ),
    **dict.fromkeys(
        ("fchmodat", "fchmodat2", "fchownat", "utimensat", "futimesat"), ((0, 1),)
    ),
}


def _entries(trace: Iterable[str]) -> Iterator[tuple[str, str]]:
    unfinished: dict[str, str] = {}
    for line in trace:
        pid, _, call = line.rstrip().partition(" ")
        call = call.lstrip()
        if call.endswith("<unfinished ...>"):
            unfinished[pid] = call[: -len("<unfinished ...>")]
            continue
        resumed = RESUMED.fullmatch(call)
        if resumed:
            if pid not in unfinished:
                continue
            call = unfinished.pop(pid) + resumed[2]
        yield pid, call


def _split(text: str) -> list[str]:
    return re.split(r', (?=(?:[^"]*"[^"]*")*[^"]*$)', text)


def _quoted(token: str) -> str | None:
    match = re.match(r'"(?:\\.|[^"\\])*"', token.strip())
    return ast.literal_eval(match[0]) if match else None


@dataclass(frozen=True)
class Call:
    """One successful or failed system call from the trace."""

    pid: str
    name: str
    arguments: str
    status: int
    text: str


@dataclass
class AuditReport:
    """What the audit saw and why it failed, if it did."""

    owned: str | None = None
    counts: collections.Counter[str] = field(default_factory=collections.Counter)
    failures: list[str] = field(default_factory=list)
    executables: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """Report whether a run directory was seen and nothing escaped it."""
        return bool(self.owned) and not self.failures


class _Audit:
    def __init__(self, public_dir: str) -> None:
        self.public_dir = public_dir
        self.report = AuditReport()
        self.cwd: dict[str, str] = {}
        self.descriptors: dict[tuple[str, str], str] = {}
        self.links: set[str] = set()
        self.real_dirs: set[str] = set()
        self.known: set[str] = set()

    # -- proofs ---------------------------------------------------------
    def _prove(self, path: str) -> None:
        """Record a canonical path and every ancestor under the run directory."""
        owned = self.report.owned
        while owned and path.startswith(owned):
            self.real_dirs.add(path)
            if path == owned:
                return
            path = posixpath.dirname(path)

    def _under_run(self, path: str) -> bool:
        owned = self.report.owned
        return bool(owned) and (path == owned or path.startswith(f"{owned}/"))

    def _through_link(self, path: str) -> bool:
        parent = posixpath.dirname(path)
        while self._under_run(parent) and parent != self.report.owned:
            if parent in self.links:
                return True
            parent = posixpath.dirname(parent)
        return False

    # -- resolution -----------------------------------------------------
    def _base(self, pid: str, directory: str | None, relative: str) -> str | None:
        if relative.startswith("/"):
            return "/"
        held = DIRFD.fullmatch(directory.strip()) if directory else None
        return held[1] if held and held[1] else self.cwd.get(pid)

    def _resolve(self, pid: str, directory: str | None, relative: str) -> str | None:
        base = self._base(pid, directory, relative)
        if base is None:
            return None
        target = posixpath.normpath(posixpath.join(base, relative))
        alias = PROC_FD.fullmatch(target)
        if alias:
            resolved = self.descriptors.get((alias[1], alias[2]))
            return resolved + (alias[3] or "") if resolved else None
        return target

    # -- verdicts -------------------------------------------------------
    def _record(self, verdict: str, kind: str, target: str | None, call: Call) -> None:
        self.report.counts[verdict] += 1
        if verdict in FAILURES:
            self.report.failures.append(
                f"{verdict} {kind} {target} :: {call.text[:200]}"
            )

    def _special(self, target: str) -> str | None:
        if target in LAUNCHER or target.startswith("/newroot/"):
            return "launcher_setup"
        if target == "/dev/null":
            return "null"
        if NAMESPACE.fullmatch(target):
            return "namespace_map"
        if SEAL.fullmatch(posixpath.basename(target)):
            return "seal_manifest"
        public = target == self.public_dir or (
            target.startswith(f"{self.public_dir}/")
            and (
                PENDING.fullmatch(posixpath.basename(target))
                or posixpath.basename(target) == "verdict.json"
            )
        )
        return "public_verdict" if public else None

    def _inside(self, target: str, *, follow: bool) -> str:
        if target == self.report.owned:
            return "private"
        if self._through_link(target):
            return "via_link"
        if follow:
            # chmod and friends act on what a final link points at, so the
            # path itself must be one the run created or saw opened.
            return "private" if target in self.known else "unproven"
        holder = posixpath.dirname(target)
        proven = holder == self.report.owned or holder in self.real_dirs
        return "private" if proven else "unproven"

    def _classify(self, target: str | None, *, follow: bool) -> str:
        if target is None:
            return "unresolved"
        special = self._special(target)
        if special:
            return special
        if not self._under_run(target):
            return "outside"
        return self._inside(target, follow=follow)

    # -- the trace ------------------------------------------------------
    def feed(self, call: Call) -> None:
        self._learn(call)
        if call.name == "execve":
            self.report.executables.append(call.arguments[:300])
        if call.status < 0:
            blocked = call.name in NAME_CALLS or call.name in FOLLOWING_CALLS
            blocked = blocked or (
                call.name in OPENS and bool(WRITE.search(call.arguments))
            )
            if ("EROFS" in call.text or "EACCES" in call.text) and blocked:
                self.report.counts["blocked"] += 1
            return
        result = RESULT.search(call.text)
        if call.name in OPENS and result:
            self._opened(call, result[2])
            return
        for table, follow in ((NAME_CALLS, False), (FOLLOWING_CALLS, True)):
            for indexes in table.get(call.name, ()):
                self._mutation(call, indexes, follow=follow)

    def _opened(self, call: Call, resolved: str) -> None:
        parts = _split(call.arguments)
        requested = _quoted(parts[1]) if len(parts) > 1 else None
        if requested and posixpath.normpath(requested) == resolved:
            self._prove(posixpath.dirname(resolved))
        if self._under_run(resolved):
            self.known.add(resolved)
        if WRITE.search(call.arguments):
            verdict = self._classify(resolved, follow=False)
            self._record(verdict, "write_open", resolved, call)

    def _learn(self, call: Call) -> None:
        for held in ANNOTATION.finditer(call.arguments):
            self.descriptors[call.pid, held[1]] = held[2]
            self._prove(held[2])
        result = RESULT.search(call.text)
        if call.name in {*OPENS, "dup", "dup2", "dup3", "fcntl"} and result:
            self.descriptors[call.pid, result[1]] = result[2]
        self._learn_cwd(call)
        self._learn_run_directory(call)

    def _learn_cwd(self, call: Call) -> None:
        working = CWD_ANNOTATION.search(call.arguments)
        if working:
            self.cwd[call.pid] = working[1]
            self._prove(working[1])
        if call.name == "chdir" and call.status == 0:
            target = _quoted(call.arguments)
            if target:
                base = self.cwd.get(call.pid, "/")
                self.cwd[call.pid] = posixpath.normpath(posixpath.join(base, target))
        if call.name == "fchdir" and call.status == 0:
            held = ANNOTATION.search(call.arguments)
            if held:
                self.cwd[call.pid] = held[2]
        forks = {"clone", "clone3", "fork", "vfork"}
        if call.name in forks and call.status > 0 and call.pid in self.cwd:
            self.cwd[str(call.status)] = self.cwd[call.pid]

    def _learn_run_directory(self, call: Call) -> None:
        if call.name != "mkdirat" or call.status != 0 or self.report.owned:
            return
        made = re.match(rf'\d+</dev/shm>, "({RUN_NAME.pattern})"', call.arguments)
        if made:
            self.report.owned = f"/dev/shm/{made[1]}"  # noqa: S108 - the private mount
            self.real_dirs.add(self.report.owned)

    def _mutation(
        self, call: Call, indexes: tuple[int | None, int], *, follow: bool
    ) -> None:
        directory_index, path_index = indexes
        parts = _split(call.arguments)
        relative = _quoted(parts[path_index]) if path_index < len(parts) else None
        if relative is None:
            return
        if PUBLIC_DIRECTORY.fullmatch(relative):
            self._record("public_dir", call.name, relative, call)
            return
        directory = parts[directory_index] if directory_index is not None else None
        target = self._resolve(call.pid, directory, relative)
        # rmdir with a trailing slash resolves a final link, so that entry is
        # judged by what it reaches, never as a plain name in the run. mkdir
        # with one (git does) creates a directory and cannot pass through a link.
        slash = relative.endswith("/") and call.name == "rmdir"
        verdict = self._classify(target, follow=follow or slash)
        self._record(verdict, call.name, target, call)
        if target and verdict == "private":
            self.known.add(target)
            if call.name in {"mkdir", "mkdirat"}:
                self.real_dirs.add(target)
            if call.name in {"symlink", "symlinkat"}:
                self.links.add(target)


def audit_lines(trace: Iterable[str], public_dir: str) -> AuditReport:
    """Audit an strace log and report every mutation that could not be proven."""
    audit = _Audit(public_dir)
    for pid, text in _entries(trace):
        match = CALL.match(text)
        if match:
            audit.feed(Call(pid, match[1], match[2], int(match[3]), text))
    report = audit.report
    if not report.owned:
        report.failures.append("no private run directory was created in the trace")
    for text in report.executables:
        if re.match(r'"[^"]*/tar"', text):
            report.failures.append(f"tar was executed: {text}")
        if "--output" in text and "archive" in text:
            report.failures.append(f"git archive wrote to a path: {text}")
    return report


def main(arguments: list[str]) -> int:
    """Audit each trace named; print a summary line per trace and any failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("traces", nargs="+", type=Path)
    parser.add_argument("--require-blocked", action="store_true")
    options = parser.parse_args(arguments)
    status = 0
    for trace in options.traces:
        resolved = trace.resolve()
        with resolved.open(encoding="utf-8") as lines:
            report = audit_lines(lines, str(resolved.parent / "migration-upgrade"))
        sys.stdout.write(f"{resolved} {report.owned} {dict(report.counts)}\n")
        for failure in report.failures[:25]:
            sys.stdout.write(f"{failure}\n")
        if not report.clean:
            status = 1
        if options.require_blocked and not report.counts["blocked"]:
            sys.stdout.write("no write was blocked by the sandbox\n")
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
