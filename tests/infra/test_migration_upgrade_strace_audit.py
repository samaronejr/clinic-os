"""The strace audit proves every mutation private or fails it."""

import pytest
from ops.testing.migration_upgrade_strace_audit import AuditReport, audit_lines

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


SEAL_NAME = "migration-upgrade-seal." + "b" * 32


def test_the_supervisors_own_seal_manifest_is_not_a_foreign_write() -> None:
    report = _audit(
        f'1 openat({CWD}, "/runner/{SEAL_NAME}.4242.new", O_WRONLY|O_CREAT, 0600)'
        f" = 9</runner/{SEAL_NAME}.4242.new>",
        f'1 rename("/runner/{SEAL_NAME}.4242.new", "/runner/{SEAL_NAME}") = 0',
    )

    assert report.clean, report.failures


def test_a_look_alike_of_the_seal_name_is_still_a_foreign_write() -> None:
    for name in (
        "migration-upgrade-seal.not-hex",
        f"{SEAL_NAME}.extra",
        f"{SEAL_NAME}.4242.new.more",
    ):
        report = _audit(
            f'1 openat({CWD}, "/runner/{name}", O_WRONLY|O_CREAT, 0600)'
            f" = 9</runner/{name}>"
        )

        assert not report.clean, name
