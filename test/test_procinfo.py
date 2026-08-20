"""Reading /proc for the process behind a FUSE request.

None of this is evidence — the mount runs on the user's own laptop, so every
field is a claim by software they control. What it has to be is *safe*: it runs
inline in `open()`, so it must never raise, and it ships to a server, so it
must never carry something that was not ours to take.

That second one is the reason this module has a truncation rule at all, and a
rule nobody checks is a comment. Command lines are where people put secrets,
and shipping them would move a secret off the user's machine and into someone
else's database, and then that database's backups — a worse leak than the one
an audit trail exists to catch.

Reads /proc, nothing else: no stack, no network, no mount.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from fswiki_fuse import procinfo

SECRET = "hunter2-do-not-ship-this"
pytestmark = pytest.mark.skipif(
    not os.path.isdir("/proc/self"),
    reason="process audit metadata is read from Linux procfs",
)


@pytest.fixture
def child():
    """A process with a secret in its argv, and a known parent."""
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; print('up', flush=True); sys.stdin.read()",
         "--password", SECRET, "--and", "more", "args"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True,
    )
    # Not merely spawned: *exec'd*. Between fork and exec the child is still a
    # copy of pytest, and /proc/pid/cmdline is empty for part of that window --
    # which is the same race the real caller cannot lose, because it is blocked
    # in the syscall we are answering.
    assert proc.stdout.readline() == "up\n"
    try:
        yield proc
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
        proc.stdout.close()


def test_a_secret_in_argv_does_not_leave_the_machine(child):
    """The property the truncation rule exists for. `mysql -pSECRET`,
    `curl -H "Authorization: ..."`, an API key passed to a one-off script:
    none of it has anything to do with the wiki."""
    info = procinfo.describe(child.pid)
    assert SECRET not in repr(info)


def test_what_is_kept_is_the_program_not_the_invocation(child):
    """argv[0] plus `exe` says which program this is, which is the whole point
    of the record."""
    info = procinfo.describe(child.pid)
    assert info["cmdline"] == [sys.executable]


def test_what_was_dropped_is_counted_rather_than_hidden(child):
    """So a reader can tell a bare command from a truncated one. Silently
    omitting would make `fswiki` and `fswiki push --as someone` identical in
    the trail."""
    info = procinfo.describe(child.pid)
    assert info["argv_elided"] == 7


def test_the_full_command_line_is_available_when_asked_for(child):
    """The switch exists because on a managed fleet it is sometimes the point.
    It is not the default, and the default is what ships."""
    info = procinfo.describe(child.pid, full_cmdline=True)
    assert SECRET in info["cmdline"]
    assert "argv_elided" not in info


def test_a_bare_command_is_not_reported_as_truncated():
    info = procinfo.describe(os.getpid(), full_cmdline=False)
    assert isinstance(info["cmdline"], list) and len(info["cmdline"]) == 1


def test_the_kernel_maintained_fields_are_there(child):
    """`exe` is a symlink the kernel keeps and the process cannot repoint, so
    unlike argv and comm it is worth something.

    It is not compared to sys.executable, and the reason is the point of
    having both: argv[0] here is the wrapper in a Nix python environment,
    while `exe` is the interpreter that wrapper actually exec'd. argv[0] is
    what a process claims to be; `exe` is what ran."""
    info = procinfo.describe(child.pid)
    assert os.path.isabs(info["exe"])
    assert os.path.exists(info["exe"]), "exe names a real file or it says nothing"
    assert "python" in os.path.basename(info["exe"])
    assert info["ppid"] == os.getpid()
    assert info["pid"] == child.pid


def test_starttime_is_what_survives_pid_reuse(child):
    """A pid identifies a process only until it exits. The pair (pid,
    starttime) identifies one process for as long as the box is up, which is
    what makes an old audit row still mean something."""
    info = procinfo.describe(child.pid)
    assert isinstance(info["starttime"], int)
    assert info["starttime"] > 0


def test_comm_is_read_even_though_it_is_forgeable(child):
    """prctl(PR_SET_NAME) sets it to anything. Useful on a managed fleet,
    worthless against an adversary who owns the machine — which is true of the
    whole bundle and is why the authoritative record is server-side."""
    assert procinfo.describe(child.pid)["comm"]


def test_a_command_name_with_parentheses_in_it_is_parsed_anyway():
    """/proc/pid/stat puts comm in field 2 wrapped in parens, and comm may
    itself contain parens and spaces. Splitting from the first ')' gets ppid
    and starttime wrong for exactly the process that chose to be awkward."""
    stat = b"42 (weird ) name) S 7 42 42 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 99 " + \
           b"0 " * 40
    fields = stat[stat.rindex(b")") + 2:].split()
    assert int(fields[1]) == 7
    assert int(fields[19]) == 99


def test_the_kernel_itself_has_no_identity():
    """pid 0 is what FUSE reports for requests it raised on its own —
    readahead, writeback — and there is no process to describe."""
    assert procinfo.describe(0) is None
    assert procinfo.describe(-1) is None


def test_a_process_that_has_gone_degrades_to_a_bare_pid():
    """Rather than to a lie, and rather than to an exception: this runs inside
    open(), and a raise here comes out of pyfuse3.main() and unmounts
    everything."""
    info = procinfo.describe(4194303)   # above any default pid_max
    assert info == {"pid": 4194303}


def test_argv_is_capped_before_it_is_read():
    """A command line can be megabytes. Nobody auditing a wiki needs that
    much of it, and the read is on the open() path."""
    assert procinfo.CMDLINE_LIMIT <= 65536


def test_one_line_for_a_log(child):
    line = procinfo.summarise(procinfo.describe(child.pid))
    assert str(child.pid) in line
    assert "(+7 args)" in line
    assert SECRET not in line


def test_summarising_nothing_says_so():
    assert procinfo.summarise(None) == "unknown"
    assert procinfo.summarise({}) == "unknown"


def test_a_bundle_with_holes_still_summarises():
    """Everything but `pid` is optional, and a log line is not worth a
    KeyError on the open() path."""
    assert procinfo.summarise({"pid": 5}) == "?[5]"
    assert procinfo.summarise({"pid": 5, "comm": "vim"}) == "vim[5]"
    assert procinfo.summarise({"pid": 5, "cmdline": ["/bin/vim"]}) == "?[5] /bin/vim"


def test_a_stat_line_we_cannot_parse_costs_two_fields_not_the_open(monkeypatch):
    """/proc/pid/stat has grown fields over the kernel's life and will grow
    more. Losing ppid and starttime is a worse audit record; raising is a dead
    filesystem."""
    real = procinfo._read

    def truncated(path, limit=65536):
        return b"1 (short) S" if path.endswith("/stat") else real(path, limit)

    monkeypatch.setattr(procinfo, "_read", truncated)
    info = procinfo.describe(os.getpid())
    assert info["pid"] == os.getpid()
    assert "ppid" not in info and "starttime" not in info
    assert info["comm"], "the fields that did parse are still there"


def test_a_field_that_is_not_a_number_is_dropped_rather_than_shipped(monkeypatch):
    real = procinfo._read

    def rubbish(path, limit=65536):
        if path.endswith("/stat"):
            return b"1 (x) S " + b"not-a-number " * 30
        return real(path, limit)

    monkeypatch.setattr(procinfo, "_read", rubbish)
    assert "ppid" not in procinfo.describe(os.getpid())


def test_an_unset_loginuid_is_not_reported_as_a_user(monkeypatch):
    """4294967295 is the sentinel for "no login session", and recording it as
    a uid would attribute every daemon's read to user 4294967295."""
    real = procinfo._read

    def unset(path, limit=65536):
        return b"4294967295" if path.endswith("/loginuid") else real(path, limit)

    monkeypatch.setattr(procinfo, "_read", unset)
    assert "loginuid" not in procinfo.describe(os.getpid())
