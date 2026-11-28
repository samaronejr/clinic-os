"""The strace audit proves every mutation private or fails it."""

import os
import re

import pytest
from ops.testing import migration_upgrade_custody as custody
from ops.testing import migration_upgrade_strace_audit as audit_module
from ops.testing.migration_upgrade_strace_audit import (
    SEALED_TREES,
    AuditReport,
    _Audit,
    audit_lines,
)

NAME = "migration-upgrade-secrets." + "a" * 32
RUN = f"/dev/shm/{NAME}"  # noqa: S108 - the private run mount inside the sandbox
PUBLIC = "/work/migration-upgrade"
FOREIGN = "/home/reviewer/foreign"
CWD = f"AT_FDCWD<{RUN}>"  # the run directory, as the kernel names the cwd
HELD = f"7<{FOREIGN}>"  # a descriptor on a directory outside the run
UNPROVEN = f"{RUN}/native/x"  # a link or late mount the trace never saw made
SETUP = (
    f'1 mkdirat(5</dev/shm>, "{NAME}", 0700) = 0',
    f'1 mkdirat(6<{RUN}>, "native", 0700) = 0',
)
WRITE_OPEN = "O_WRONLY|O_CREAT, 0600"

# Six calls, each through three evasions: an absolute foreign path, a foreign
# descriptor, and run-prefixed text under a directory the trace never proved.
CORPUS = {
    "open-absolute": f'1 openat({CWD}, "{FOREIGN}/f", O_WRONLY) = 7<{FOREIGN}/f>',
    "open-descriptor": f'1 openat({HELD}, "f", {WRITE_OPEN}) = 8<{FOREIGN}/f>',
    "open-unproven": f'1 openat({CWD}, "native/x/f", {WRITE_OPEN}) = 7<{UNPROVEN}/f>',
    "mkdir-absolute": f'1 mkdirat({CWD}, "{FOREIGN}/d", 0700) = 0',
    "mkdir-descriptor": f'1 mkdirat({HELD}, "d", 0700) = 0',
    "mkdir-unproven": f'1 mkdirat({CWD}, "native/x/d", 0700) = 0',
    "rename-absolute": f'1 renameat({CWD}, "{FOREIGN}/a", {CWD}, "{FOREIGN}/b") = 0',
    "rename-descriptor": f'1 renameat({HELD}, "a", {HELD}, "b") = 0',
    "rename-unproven": f'1 renameat({CWD}, "native/x/a", {CWD}, "native/x/b") = 0',
    "unlink-absolute": f'1 unlinkat({CWD}, "{FOREIGN}/f", 0) = 0',
    "unlink-descriptor": f'1 unlinkat({HELD}, "f", 0) = 0',
    "unlink-unproven": f'1 unlinkat({CWD}, "native/x/f", 0) = 0',
    "link-absolute": f'1 linkat({CWD}, "{FOREIGN}/f", {CWD}, "{FOREIGN}/g", 0) = 0',
    "link-descriptor": f'1 linkat({HELD}, "f", {HELD}, "g", 0) = 0',
    "link-unproven": f'1 linkat({CWD}, "native/x/f", {CWD}, "native/x/g", 0) = 0',
    "symlink-absolute": f'1 symlinkat("t", {CWD}, "{FOREIGN}/l") = 0',
    "symlink-descriptor": f'1 symlinkat("t", {HELD}, "l") = 0',
    "symlink-unproven": f'1 symlinkat("t", {CWD}, "native/x/l") = 0',
}
BENIGN = (
    f'1 mkdirat({CWD}, "native/ok", 0700) = 0',
    f'1 openat({CWD}, "native/ok/f", {WRITE_OPEN}) = 7<{RUN}/native/ok/f>',
    f'1 renameat({CWD}, "native/ok/f", {CWD}, "native/ok/g") = 0',
    f'1 linkat({CWD}, "native/ok/g", {CWD}, "native/ok/h", 0) = 0',
    f'1 symlinkat("g", {CWD}, "native/ok/l") = 0',
    f'1 unlinkat({CWD}, "native/ok/h", 0) = 0',
    f'1 openat(AT_FDCWD<{PUBLIC}>, ".verdict-1.pending", {WRITE_OPEN}) '
    f"= 8<{PUBLIC}/.verdict-1.pending>",
)


def _audit(*lines: str) -> AuditReport:
    return audit_lines([*SETUP, *lines], PUBLIC)


def test_a_benign_run_is_clean() -> None:
    report = _audit(*BENIGN)

    assert report.failures == []
    assert report.clean


@pytest.mark.parametrize("operation", CORPUS)
def test_each_foreign_operation_is_flagged(operation: str) -> None:
    report = _audit(CORPUS[operation])

    assert report.failures, operation
    assert not report.clean


def test_all_eighteen_foreign_operations_are_flagged_together() -> None:
    flagged = {name for name in CORPUS if _audit(CORPUS[name]).failures}

    assert len(CORPUS) == 18
    assert flagged == set(CORPUS)


@pytest.mark.parametrize(
    "line",
    [
        '1 mkdir("native/x", 0700) = 0',
        f'1 chmod("{UNPROVEN}/f", 0600) = 0',
        f'1 truncate("{RUN}/native/never-created", 0) = 0',
        f'1 openat({CWD}, "uid_map", O_WRONLY) = 9</home/reviewer/foreign>',
    ],
)
def test_what_cannot_be_resolved_or_proven_fails(line: str) -> None:
    assert not _audit(line).clean


def test_a_write_through_a_link_the_run_made_is_flagged() -> None:
    report = _audit(
        f'1 symlinkat("{FOREIGN}", {CWD}, "native/link") = 0',
        f'1 mkdirat({CWD}, "native/link/d", 0700) = 0',
    )

    assert any(failure.startswith("via_link") for failure in report.failures)


def test_a_blocked_write_is_counted_and_not_a_failure() -> None:
    blocked = "= -1 EROFS (Read-only file system)"
    report = _audit(f'1 openat({CWD}, "native/link", O_WRONLY|O_TRUNC) {blocked}')

    assert report.clean
    assert report.counts["blocked"] == 1


def test_a_trace_without_a_run_directory_fails() -> None:
    assert not audit_lines([BENIGN[-1]], PUBLIC).clean


def test_the_wrapper_publishes_through_a_moved_descriptor_it_can_prove() -> None:
    # bash moves its directory descriptor with F_DUPFD, then renames through it.
    moved = (
        f"1 fcntl(3<{PUBLIC}>, F_DUPFD, 10) = 11<{PUBLIC}>",
        '1 rename("/proc/1/fd/11/.verdict-aa-1.pending", '
        '"/proc/1/fd/11/verdict.json") = 0',
    )

    assert _audit(*moved).clean
    # Without the descriptor's origin in the trace it cannot be proven private.
    assert not _audit(moved[1]).clean


PROBE_FILE = f"{RUN}/base/.review-probe-f"  # a link planted by an outside process
PROBE_DIR = f"{RUN}/base/.review-probe-d"
LATE = "/media/reviewer/late-mount"
# The 18 successful foreign operations of the round 17 attack, from the
# verdict's own list. Five show a foreign path in the kernel's annotation; the
# other thirteen name only a path under the run, through a link the trace never
# saw made, or the traced process's own /proc/<pid>/fd/0, and were counted private.
DIR_OPEN = f'1 openat({CWD}, "{PROBE_DIR}/created", {WRITE_OPEN}) = '
REVIEWER_ATTACK = {
    "open-late-file": (
        f'1 openat({CWD}, "{PROBE_FILE}", O_WRONLY|O_TRUNC) = 7<{LATE}/victim-file>'
    ),
    "create-in-late-dir": DIR_OPEN + f"8<{LATE}/victim-dir/created>",
    "open-stdin": (
        f'1 openat({CWD}, "/proc/self/fd/0", O_WRONLY|O_TRUNC) = '
        "9</home/reviewer/stdin-victim>"
    ),
    "open-stdout": (
        f'1 openat({CWD}, "/proc/self/fd/1", O_WRONLY) = 10</home/reviewer/stdout.log>'
    ),
    "open-stderr": (
        f'1 openat({CWD}, "/dev/stderr", O_WRONLY) = 11</home/reviewer/stderr.log>'
    ),
    "file-chmod": f'1 chmod("{PROBE_FILE}", 0600) = 0',
    "file-utimensat": f'1 utimensat({CWD}, "{PROBE_FILE}", NULL, 0) = 0',
    "file-truncate": f'1 truncate("{PROBE_FILE}", 0) = 0',
    "dir-mkdir": f'1 mkdirat({CWD}, "{PROBE_DIR}/made-dir", 0700) = 0',
    "dir-rename": (
        f'1 renameat({CWD}, "{PROBE_DIR}/a", {CWD}, "{PROBE_DIR}/a.renamed") = 0'
    ),
    "dir-unlink": f'1 unlinkat({CWD}, "{PROBE_DIR}/b", 0) = 0',
    "dir-symlink": f'1 symlinkat("t", {CWD}, "{PROBE_DIR}/made-link") = 0',
    "dir-link": (
        f'1 linkat({CWD}, "{PROBE_DIR}/c", {CWD}, "{PROBE_DIR}/c.hard", 0) = 0'
    ),
    "dir-chmod": f'1 fchmodat({CWD}, "{PROBE_DIR}", 0700) = 0',
    "dir-rmdir": f'1 rmdir("{PROBE_DIR}/") = 0',
    "stdin-chmod": '1 chmod("/proc/1/fd/0", 0600) = 0',
    "stdin-utimensat": f'1 utimensat({CWD}, "/proc/1/fd/0", NULL, 0) = 0',
    "stdin-truncate": '1 truncate("/proc/1/fd/0", 0) = 0',
}


def test_all_eighteen_of_the_reviewers_foreign_operations_are_flagged() -> None:
    flagged = {name for name, line in REVIEWER_ATTACK.items() if _audit(line).failures}

    assert len(REVIEWER_ATTACK) == 18
    assert flagged == set(REVIEWER_ATTACK), set(REVIEWER_ATTACK) - flagged
    assert not _audit(*REVIEWER_ATTACK.values()).clean


def test_a_directory_created_with_a_trailing_slash_is_private() -> None:
    # git init creates .git/branches, .git/info and .git/hooks this way.
    assert _audit(f'1 mkdir("{RUN}/native/branches/", 0777) = 0').clean


def test_a_write_refused_by_landlock_is_counted_as_blocked() -> None:
    refused = "= -1 EACCES (Permission denied)"
    report = _audit(f'1 openat({CWD}, "native/link", O_WRONLY|O_TRUNC) {refused}')

    assert report.clean
    assert report.counts["blocked"] == 1


def test_a_file_named_like_the_old_seal_is_a_foreign_write() -> None:
    """No seal file exists any more: a write to that name is not the supervisor's."""
    name = "migration-upgrade-seal." + "b" * 32
    report = _audit(
        f'1 openat({CWD}, "/runner/{name}", O_WRONLY|O_CREAT, 0600) = 9</runner/{name}>'
    )

    assert not report.clean


SEAL_NONCE = "c" * 32
SEALED = f"/dev/shm/migration-upgrade-sealed.{SEAL_NONCE}"  # noqa: S108 - private mount
OTHER = "/dev/shm/migration-upgrade-sealed." + "d" * 32  # noqa: S108
IDENTITY = f"{os.makedev(0, 0x2E)}:77:1000"
STAT_ROOT = (
    f'1 newfstatat(AT_FDCWD<{RUN}>, "{SEALED}", {{st_dev=makedev(0, 0x2e), '
    f"st_ino=77, st_mode=S_IFDIR|0700, st_nlink=4, st_uid=1000}}, "
    "AT_SYMLINK_NOFOLLOW) = 0"
)
USE_SEALED = (
    '1 execve("/usr/bin/python3", ["python3", "-B", "-I", '
    f'"{SEALED}/ops/testing/migration_upgrade_custody.py"], 0x0 /* 3 vars */) = 0'
)


def _creating(path: str) -> str:
    return (
        f'1 openat(AT_FDCWD<{RUN}>, "{path}", O_WRONLY|O_CREAT|O_EXCL, 0400) '
        f"= 9<{path}>"
    )


SEALING = (
    f'1 mkdir("{SEALED}", 0700) = 0',
    f'1 mkdir("{SEALED}/ops", 0700) = 0',
    f'1 mkdir("{SEALED}/ops/testing", 0700) = 0',
    _creating(f"{SEALED}/ops/testing/migration_upgrade_custody.py"),
    f'1 mkdir("{SEALED}/ops/db", 0700) = 0',
    _creating(f"{SEALED}/ops/db/bootstrap.sql"),
)
CLEANUP = (
    f'1 unlink("{SEALED}/ops/db/bootstrap.sql") = 0',
    f'1 rmdir("{SEALED}/ops/db") = 0',
    f'1 rmdir("{SEALED}") = 0',
)


def _sealed_audit(
    *lines: str, nonce: str | None = SEAL_NONCE, identity: str | None = IDENTITY
) -> AuditReport:
    return audit_lines([*SETUP, *lines], PUBLIC, nonce, identity)


def test_the_sealed_trees_are_the_custodys_planned_trees() -> None:
    assert SEALED_TREES == custody.HELPER_TREES


def test_the_supervisors_own_sealed_root_is_clean_from_creation_to_removal() -> None:
    report = _sealed_audit(*SEALING, USE_SEALED, STAT_ROOT, *CLEANUP)

    assert report.failures == []
    assert report.clean
    assert report.counts["sealed_identity"] == 1


# Each of these is foreign: a root this run did not make, one made another way,
# or a write the sealing never plans. The blanket allowance accepts the first
# group (see the mutation below); the exact binding refuses all of them.
PATH_BASED = {
    "another_nonce": (f'1 mkdir("{OTHER}", 0700) = 0',),
    "another_nonce_write": (_creating(f"{OTHER}/ops/testing/x.py"),),
    "pre_created": (
        f'1 mkdir("{SEALED}", 0700) = -1 EEXIST (File exists)',
        f'1 mkdir("{SEALED}/ops", 0700) = 0',
        _creating(f"{SEALED}/ops/testing/x.py"),
    ),
    "symlinked_root": (
        f'1 symlink("/home/reviewer/foreign", "{SEALED}") = 0',
        _creating(f"{SEALED}/ops/testing/x.py"),
    ),
    "renamed_root": (
        *SEALING,
        f'1 rename("{SEALED}", "{OTHER}") = 0',
    ),
    "write_beside_the_trees": (
        *SEALING,
        _creating(f"{SEALED}/evil.py"),
    ),
    "write_in_an_unplanned_tree": (
        *SEALING,
        f'1 mkdir("{SEALED}/ops/other", 0700) = 0',
        _creating(f"{SEALED}/ops/other/x.py"),
    ),
    "write_after_sealing": (
        *SEALING,
        USE_SEALED,
        _creating(f"{SEALED}/ops/testing/late.py"),
    ),
    "create_directory_after_sealing": (
        *SEALING,
        USE_SEALED,
        f'1 mkdir("{SEALED}/ops/testing/late", 0700) = 0',
    ),
    "link_after_sealing": (
        *SEALING,
        USE_SEALED,
        f'1 symlink("/home/reviewer/foreign", "{SEALED}/ops/testing/l") = 0',
    ),
}


@pytest.mark.parametrize("case", PATH_BASED)
def test_a_look_alike_of_the_sealed_root_is_flagged(case: str) -> None:
    report = _sealed_audit(*PATH_BASED[case])

    assert report.failures, case
    assert not report.clean


def test_a_sealed_root_with_no_recorded_nonce_is_never_allowed() -> None:
    report = _sealed_audit(*SEALING, nonce=None)

    assert report.failures
    assert not report.clean


def test_a_sealed_root_whose_identity_differs_from_the_record_is_flagged() -> None:
    swapped = STAT_ROOT.replace("st_ino=77", "st_ino=78")

    report = _sealed_audit(*SEALING, USE_SEALED, swapped)

    assert any("identity differs" in failure for failure in report.failures)


def test_a_sealed_root_whose_identity_was_never_observed_is_flagged() -> None:
    report = _sealed_audit(*SEALING, USE_SEALED)

    assert any("never observed" in failure for failure in report.failures)


def test_the_old_blanket_allowance_would_let_the_look_alikes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: allow any sealed-looking path, as the first repair did.

    Every case below is refused by the exact binding (the test above) and clean
    under the blanket rule, so the refusal tests fail against it.
    """

    class Blanket(_Audit):
        def _sealed(self, target: str, kind: str) -> str | None:
            blanket = r"/dev/shm/migration-upgrade-sealed\.[0-9a-f]{32}(/.*)?"  # noqa: S108
            return "sealed_helper" if re.fullmatch(blanket, target) else None

    monkeypatch.setattr(audit_module, "_Audit", Blanket)
    blind = [
        "another_nonce",
        "another_nonce_write",
        "pre_created",
        "symlinked_root",
        "renamed_root",
        "write_beside_the_trees",
        "write_in_an_unplanned_tree",
        "write_after_sealing",
        "create_directory_after_sealing",
        "link_after_sealing",
    ]

    for case in blind:
        report = _sealed_audit(*PATH_BASED[case])

        assert report.failures == [], case
