"""Characterisation tests for ``mnemos/installer/detect.py``.

``detect.py`` (280 lines) had zero test references before this file. It is the
installer's environment probe: it shells out to ``which``/``psql``/``pg_isready``
/``lsb_release``/``python -m venv``, reads ``/etc/os-release``, stats the disk
and binds a socket on localhost. Every one of those seams is replaced here with
a fake, so the suite is deterministic and never touches the real network, a
real database or the system clock.

Two deliberate exceptions, both safe on any host:

* the ``os_release`` fixture redirects ``builtins.open`` for the literal path
  ``/etc/os-release`` to an in-memory body and delegates every other open to
  the real one, so ``_detect_os`` never reads the machine's own file;
* ``check_port_free`` is exercised against the real kernel socket via port 0
  (an ephemeral bind), because the function IS a socket bind — faking it would
  test the fake. Port 0 is never in use, so the result does not depend on the
  host. The occupied-port case holds the port with its own listener instead of
  probing a hardcoded number.


These tests PIN the module's CURRENT behaviour — including the branches that
look wrong. They are not a design proposal: if a value changes on purpose,
update the pin together with the change.

The public surface pinned here:

* ``SystemInfo`` — the dataclass defaults and the field set ``detect()`` fills.
* ``detect()`` — assembly order, the derived ``python_ok``/``launchd`` flags,
  and the exact set of commands each probe runs.
* ``print_summary()`` — every OK/WARN/FAIL line and the disk thresholds.
* ``check_port_free()`` — free vs. bound port, and the exact exception types
  it swallows.
* the helpers ``_run``/``_which``/``_detect_os``/``_detect_postgres``/
  ``_detect_pgvector``/``_disk_free_gb``, which carry the branch logic.

One suspected defect is pinned with ``xfail(strict=True)``: see
``test_detect_os_swallows_any_exception_from_the_id_like_loop``.
"""

from __future__ import annotations

import builtins
import io
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from mnemos.installer import detect as detect_mod
from mnemos.installer.detect import (
    SystemInfo,
    _detect_os,
    _detect_pgvector,
    _detect_postgres,
    _disk_free_gb,
    _run,
    _which,
    check_port_free,
    detect,
    print_summary,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"


class _FakeCompleted:
    """The ``subprocess.CompletedProcess`` fields ``_run`` reads."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _CommandTable:
    """Record every ``subprocess.run`` call and answer from a table.

    Keys are the argv joined with a space; a value is either a
    ``_FakeCompleted`` or an exception instance to raise. An unlisted argv is a
    test failure, never a silent default, so an unexpected probe is visible.
    """

    def __init__(self, table: dict[str, object], default: object = None) -> None:
        self.table = dict(table)
        self.default = default
        self.calls: list[list[str]] = []
        self.kwargs: list[dict] = []

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        self.kwargs.append(kwargs)
        key = " ".join(str(p) for p in cmd)
        if key in self.table:
            value = self.table[key]
        elif self.default is not None:
            value = self.default
        else:
            raise AssertionError(f"unexpected subprocess call: {key!r}")
        if isinstance(value, BaseException):
            raise value
        return value

    def commands(self) -> list[str]:
        return [" ".join(str(p) for p in call) for call in self.calls]


def _argv(*parts: str) -> str:
    return " ".join(parts)


@pytest.fixture
def fake_run(monkeypatch):
    """Install a command table as ``detect.subprocess.run`` and return it."""

    def install(table: dict[str, object] | None = None, default: object = None):
        table = _CommandTable(table or {}, default=default)
        monkeypatch.setattr(detect_mod.subprocess, "run", table)
        return table

    return install


@pytest.fixture
def os_release(monkeypatch):
    """Serve ``/etc/os-release`` from an in-memory body (or raise).

    ``detect._detect_os`` opens the literal path ``/etc/os-release``, so the
    fake intercepts exactly that path in ``builtins.open`` and delegates
    everything else to the real opener.
    """

    state: dict[str, object] = {"body": None}
    real_open = builtins.open

    def fake_open(file, *args, **kwargs):
        if file == "/etc/os-release":
            body = state["body"]
            if isinstance(body, BaseException):
                raise body
            return io.StringIO(str(body))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(detect_mod.platform, "system", lambda: "Linux")

    def set_body(body: object) -> None:
        state["body"] = body

    return set_body


def _summary(info: SystemInfo) -> list[str]:
    """Run ``print_summary`` on a fresh ``io.StringIO`` (capsys-free)."""
    buf = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = buf
    try:
        print_summary(info)
    finally:
        sys.stdout = real_stdout
    return buf.getvalue().splitlines()


def _level(line: str) -> str:
    if line.startswith(f"{GREEN}[  OK  ]{RESET}"):
        return "ok"
    if line.startswith(f"{YELLOW}[ WARN ]{RESET}"):
        return "warn"
    if line.startswith(f"{RED}[ FAIL ]{RESET}"):
        return "fail"
    return "plain"


def _healthy(**overrides) -> SystemInfo:
    """A fully green ``SystemInfo``; ``overrides`` flip one field at a time."""
    base = dict(
        os_type="linux",
        distro="debian",
        distro_version="13.2",
        python_version=(3, 11, 0),
        python_ok=True,
        pg_installed=True,
        pg_version="16.2",
        pg_running=True,
        pgvector_available=True,
        systemd=True,
        launchd=False,
        disk_free_gb=100.0,
        pip_available=True,
        venv_available=True,
        git_available=True,
    )
    base.update(overrides)
    return SystemInfo(**base)


# ---------------------------------------------------------------------------
# SystemInfo
# ---------------------------------------------------------------------------


def test_system_info_defaults_are_all_empty_and_falsy():
    """Every field has a falsy default: the dataclass is safe to build bare."""
    info = SystemInfo()

    assert info.os_type == ""
    assert info.distro == ""
    assert info.distro_version == ""
    assert info.python_version == ()
    assert info.pg_version == ""
    assert info.disk_free_gb == 0.0
    for flag in (
        "python_ok",
        "pg_installed",
        "pg_running",
        "pgvector_available",
        "systemd",
        "launchd",
        "pip_available",
        "venv_available",
        "git_available",
    ):
        assert getattr(info, flag) is False, flag


def test_system_info_is_a_mutable_value_object():
    """Fields are plain attributes (``detect()`` assigns them one by one)."""
    info = SystemInfo()
    info.os_type = "macos"
    info.python_version = (3, 13, 5)

    assert info == SystemInfo(os_type="macos", python_version=(3, 13, 5))
    assert info != SystemInfo()


def test_system_info_field_set_is_exactly_the_documented_contract():
    """Downstream code (wizard, ``__main__``) reads these names verbatim."""
    assert set(SystemInfo.__dataclass_fields__) == {
        "os_type",
        "distro",
        "distro_version",
        "python_version",
        "python_ok",
        "pg_installed",
        "pg_version",
        "pg_running",
        "pgvector_available",
        "systemd",
        "launchd",
        "disk_free_gb",
        "pip_available",
        "venv_available",
        "git_available",
    }


# ---------------------------------------------------------------------------
# _run
# ---------------------------------------------------------------------------


def test_run_returns_code_stdout_and_stderr_stripped(fake_run):
    """stdout/stderr are stripped so callers can compare the text directly."""
    table = fake_run({_argv("tool"): _FakeCompleted(0, "  out \n", " err\n\n")})

    assert _run(["tool"]) == (0, "out", "err")
    assert table.kwargs == [{"capture_output": True, "text": True, "timeout": 10}]


def test_run_passes_an_explicit_timeout_through(fake_run):
    """The timeout is a parameter, so ``_detect_pgvector`` can shorten it."""
    table = fake_run({_argv("slow"): _FakeCompleted(0, "", "")})

    _run(["slow"], timeout=5)

    assert table.kwargs[0]["timeout"] == 5


def test_run_returns_non_zero_code_without_raising(fake_run):
    """A failing command is data, not an exception."""
    fake_run({_argv("no"): _FakeCompleted(2, "", "nope")})

    assert _run(["no"]) == (2, "", "nope")


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError(2, "No such file or directory"),
        subprocess.TimeoutExpired(cmd=["tool"], timeout=10),
        PermissionError(13, "Permission denied"),
        OSError("boom"),
        ValueError("weird"),
    ],
    ids=["file-not-found", "timeout", "permission", "oserror", "valueerror"],
)
def test_run_never_raises_and_reports_failure_as_code_1(fake_run, error):
    """Any exception (not only OSError) collapses to ``(1, "", "")``.

    The bare ``except Exception`` is the contract: the installer must be able
    to run its probe on a host where every tool is missing, so no exception
    from ``subprocess.run`` may escape.
    """
    fake_run({_argv("tool"): error})

    assert _run(["tool"]) == (1, "", "")


def test_run_does_not_use_a_shell(fake_run):
    """argv is passed as a list — no shell string is ever constructed."""
    table = fake_run({_argv("a", "b c"): _FakeCompleted(0, "", "")})

    _run(["a", "b c"])

    assert table.calls == [["a", "b c"]]


# ---------------------------------------------------------------------------
# _which
# ---------------------------------------------------------------------------


def test_which_is_true_only_for_return_code_zero(fake_run):
    fake_run(
        {
            _argv("which", "present"): _FakeCompleted(0, "/usr/bin/present", ""),
            _argv("which", "absent"): _FakeCompleted(1, "", ""),
        }
    )

    assert _which("present") is True
    assert _which("absent") is False


def test_which_queries_the_path_with_the_which_binary(fake_run):
    """PATH lookup goes through ``which`` rather than ``shutil.which``."""
    table = fake_run({_argv("which", "git"): _FakeCompleted(0, "/usr/bin/git", "")})

    _which("git")

    assert table.commands() == ["which git"]


def test_which_is_false_when_the_lookup_itself_fails(fake_run):
    """A host without ``which`` reports False instead of propagating."""
    fake_run({_argv("which", "git"): FileNotFoundError(2, "no which")})

    assert _which("git") is False


def test_which_is_false_for_an_empty_name(fake_run):
    """``which ""`` exits non-zero; the empty string is not special-cased."""
    table = fake_run({_argv("which", ""): _FakeCompleted(1, "", "")})

    assert _which("") is False
    assert table.calls == [["which", ""]]


# ---------------------------------------------------------------------------
# _detect_os
# ---------------------------------------------------------------------------


def test_detect_os_darwin_returns_macos_and_the_kernel_version(monkeypatch):
    monkeypatch.setattr(detect_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(detect_mod.platform, "mac_ver", lambda: ("15.4", ("", "", ""), ""))

    assert _detect_os() == ("macos", "macos", "15.4")


def test_detect_os_darwin_with_empty_mac_version(monkeypatch):
    """``platform.mac_ver()`` may return ``""`` — passed through unvalidated."""
    monkeypatch.setattr(detect_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(detect_mod.platform, "mac_ver", lambda: ("", ("", "", ""), ""))

    assert _detect_os() == ("macos", "macos", "")


def test_detect_os_windows_uses_platform_version(monkeypatch):
    monkeypatch.setattr(detect_mod.platform, "system", lambda: "Windows")
    monkeypatch.setattr(detect_mod.platform, "version", lambda: "10.0.22631")

    assert _detect_os() == ("windows", "unknown", "10.0.22631")


def test_detect_os_unknown_platform_is_unknown_unknown(monkeypatch):
    """Anything that is not darwin/windows/linux — including "FreeBSD"."""
    monkeypatch.setattr(detect_mod.platform, "system", lambda: "FreeBSD")

    assert _detect_os() == ("unknown", "unknown", "")


def test_detect_os_matches_platform_case_insensitively(os_release):
    """``platform.system()`` is lower-cased before comparison.

    ``os_release`` pins ``platform.system`` to ``"Linux"``; the variants below
    prove the lower-casing, not the exact spelling, is what selects the branch.
    """
    os_release('ID="debian"\nVERSION_ID="13"\n')

    for spelling in ("Linux", "LINUX", "LiNuX"):
        detect_mod.platform.system = lambda s=spelling: s
        assert _detect_os() == ("linux", "debian", "13")


@pytest.mark.parametrize(
    ("id_value", "expected"),
    [
        ("debian", "debian"),
        ("ubuntu", "ubuntu"),
        ("rhel", "rhel"),
        ("centos", "rhel"),
        ("fedora", "fedora"),
        ("arch", "arch"),
        ("manjaro", "arch"),
        ("opensuse", "rhel"),
        ("sles", "rhel"),
    ],
)
def test_detect_os_maps_each_known_distro_id(os_release, id_value, expected):
    os_release(f'ID="{id_value}"\nVERSION_ID="1.0"\n')

    assert _detect_os() == ("linux", expected, "1.0")


def test_detect_os_id_like_resolves_by_distro_map_order_not_file_order(os_release):
    """ID_LIKE matching follows ``distro_map`` insertion order, not the file.

    ``debian`` is inserted before ``ubuntu``, so an ID_LIKE naming both words
    in the other order still resolves to ``debian``.
    """
    os_release('ID="linuxmint"\nID_LIKE="ubuntu debian"\nVERSION_ID="22"\n')

    assert _detect_os() == ("linux", "debian", "22")


def test_detect_os_unknown_id_and_unknown_id_like_is_unknown(os_release):
    os_release('ID="gentoo"\nID_LIKE="none"\nVERSION_ID="2.15"\n')

    assert _detect_os() == ("linux", "unknown", "2.15")


def test_detect_os_id_and_id_like_are_lower_cased(os_release):
    """``ID``/``ID_LIKE`` are lowered before the map lookup and substring test."""
    os_release('ID="UBUNTU"\nID_LIKE="DEBIAN"\nVERSION_ID="24.04"\n')

    assert _detect_os() == ("linux", "ubuntu", "24.04")

    os_release('ID="MYSTERY"\nID_LIKE="RHEL"\n')

    assert _detect_os() == ("linux", "rhel", "")


def test_detect_os_strips_surrounding_quotes_only(os_release):
    """Values are unquoted with ``strip('"')`` on both ends."""
    os_release('ID="arch"\nVERSION_ID="rolling"\n')

    assert _detect_os() == ("linux", "arch", "rolling")


def test_detect_os_version_id_may_be_unquoted(os_release):
    """``VERSION_ID=24.0`` (no quotes) is read verbatim."""
    os_release("ID=fedora\nVERSION_ID=40\n")

    assert _detect_os() == ("linux", "fedora", "40")


def test_detect_os_empty_os_release_is_unknown_linux(os_release):
    """An empty (or comment-only) file yields ``linux/unknown/""``."""
    os_release("")

    assert _detect_os() == ("linux", "unknown", "")

    os_release("# just a comment\n\n")

    assert _detect_os() == ("linux", "unknown", "")


def test_detect_os_ignores_lines_without_an_equals_sign(os_release):
    """Only ``KEY=VALUE`` lines are parsed; junk lines are skipped silently."""
    os_release('garbage line\nID="debian"\nno equals here\nVERSION_ID="12"\n')

    assert _detect_os() == ("linux", "debian", "12")


def test_detect_os_splits_only_on_the_first_equals(os_release):
    """``KEY=A=B`` keeps ``A=B`` as the value (``split("=", 1)``)."""
    os_release('ID="debian"\nPRETTY_NAME="A=B"\nVERSION_ID="1=2"\n')

    assert _detect_os() == ("linux", "debian", "1=2")


def test_detect_os_missing_os_release_falls_back_to_lsb_release(os_release, fake_run):
    """OSError on the open is the documented trigger for the lsb_release path."""
    os_release(FileNotFoundError(2, "/etc/os-release"))
    table = fake_run(
        {
            _argv("lsb_release", "-is"): _FakeCompleted(0, "Ubuntu", ""),
            _argv("lsb_release", "-rs"): _FakeCompleted(0, "22.04", ""),
        }
    )

    assert _detect_os() == ("linux", "ubuntu", "22.04")
    assert table.commands() == ["lsb_release -is", "lsb_release -rs"]


def test_detect_os_lsb_release_distro_is_lower_cased(os_release, fake_run):
    os_release(FileNotFoundError(2, "/etc/os-release"))
    fake_run(
        {
            _argv("lsb_release", "-is"): _FakeCompleted(0, "Debian", ""),
            _argv("lsb_release", "-rs"): _FakeCompleted(0, "12", ""),
        }
    )

    assert _detect_os() == ("linux", "debian", "12")


def test_detect_os_lsb_release_failure_degrades_to_unknown(os_release, fake_run):
    """A non-zero ``lsb_release -is`` ends the probe with ``unknown``."""
    os_release(FileNotFoundError(2, "/etc/os-release"))
    table = fake_run({_argv("lsb_release", "-is"): _FakeCompleted(1, "", "not found")})

    assert _detect_os() == ("linux", "unknown", "")
    assert table.commands() == ["lsb_release -is"]


def test_detect_os_skips_lsb_release_entirely_when_os_release_is_readable(os_release, fake_run):
    """The fallback is never reached on a normal Linux host."""
    os_release('ID="debian"\nVERSION_ID="13"\n')
    table = fake_run(default=_FakeCompleted(1, "", ""))

    assert _detect_os() == ("linux", "debian", "13")
    assert table.calls == []


def test_detect_os_permission_error_on_os_release_also_falls_back(os_release, fake_run):
    """``except Exception`` around the read is broader than OSError."""
    os_release(PermissionError(13, "Permission denied"))
    fake_run(
        {
            _argv("lsb_release", "-is"): _FakeCompleted(0, "Fedora", ""),
            _argv("lsb_release", "-rs"): _FakeCompleted(0, "40", ""),
        }
    )

    assert _detect_os() == ("linux", "fedora", "40")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT (reported, not fixed — tests-only job). mnemos/installer/detect.py "
        "_detect_os(): the /etc/os-release parse runs inside `try:` (line 62), whose "
        "`except Exception: pass` is line 92, and the `return \"linux\", distro, "
        "version_id` at line 91 is INSIDE that try block. So when ID is unmapped and "
        "ID_LIKE does not resolve the distro, the function returns "
        "('linux', 'unknown', version_id) without ever reaching the lsb_release "
        "fallback documented at line 94. A supported-but-unmapped distro (e.g. Linux "
        "Mint without ID_LIKE) is reported as 'unknown' rather than resolved by "
        "lsb_release. The fallback only runs when the /etc/os-release read RAISES."
    ),
)
def test_detect_os_falls_back_to_lsb_release_for_an_unmapped_distro(os_release, fake_run):
    """Pin the INTENDED contract: an unmapped, ID_LIKE-less distro still resolves.

    ``/etc/os-release`` is readable but names nothing in ``distro_map`` and has
    no ``ID_LIKE``. The documented fallback ("Fallback: lsb_release") should
    therefore run. Today the early ``return`` at the end of the try body wins
    and the answer is ``unknown`` — see the xfail reason.
    """
    os_release('ID="linuxmint"\nVERSION_ID="22"\n')
    table = fake_run(
        {
            _argv("lsb_release", "-is"): _FakeCompleted(0, "LinuxMint", ""),
            _argv("lsb_release", "-rs"): _FakeCompleted(0, "22", ""),
        }
    )

    assert _detect_os() == ("linux", "linuxmint", "22")
    assert table.commands() == ["lsb_release -is", "lsb_release -rs"]


def test_detect_os_unknown_distro_short_circuits_before_lsb_release(os_release, fake_run):
    """CURRENT behaviour, pinned: an unknown distro never reaches lsb_release.

    This is the other half of the defect above — recorded as a passing pin so
    the day it is fixed this test fails loudly and gets updated with it.
    """
    os_release('ID="linuxmint"\nVERSION_ID="22"\n')
    table = fake_run(default=_FakeCompleted(0, "LinuxMint", "22"))

    assert _detect_os() == ("linux", "unknown", "22")
    assert table.calls == []


# ---------------------------------------------------------------------------
# _detect_postgres
# ---------------------------------------------------------------------------


def test_detect_postgres_absent_reports_not_installed_and_not_running(fake_run):
    """No ``psql``: only the lookup runs, then only ``pg_isready`` is probed.

    ``pg_isready`` is probed even when ``psql`` is missing, so "installed" and
    "running" are independent answers.
    """
    table = fake_run(
        {
            _argv("which", "psql"): _FakeCompleted(1, "", ""),
            _argv("which", "pg_isready"): _FakeCompleted(0, "", ""),
            _argv("pg_isready", "-q"): _FakeCompleted(2, "", "no server"),
        }
    )

    assert _detect_postgres() == (False, "", False)
    assert table.commands() == ["which psql", "which pg_isready", "pg_isready -q"]


def test_detect_postgres_parses_the_version_from_psql_output(fake_run):
    """``psql (PostgreSQL) 16.2`` → ``16.2`` (last digit-leading token)."""
    fake_run(
        {
            _argv("which", "psql"): _FakeCompleted(0, "/usr/bin/psql", ""),
            _argv("psql", "--version"): _FakeCompleted(0, "psql (PostgreSQL) 16.2", ""),
            _argv("which", "pg_isready"): _FakeCompleted(0, "", ""),
            _argv("pg_isready", "-q"): _FakeCompleted(0, "", ""),
        }
    )

    assert _detect_postgres() == (True, "16.2", True)


def test_detect_postgres_takes_the_last_digit_leading_token(fake_run):
    """Tokens are scanned in reverse, so a trailing version wins."""
    fake_run(
        {
            _argv("which", "psql"): _FakeCompleted(0, "", ""),
            _argv("psql", "--version"): _FakeCompleted(0, "psql (PostgreSQL) 9.6 16.2", ""),
            _argv("which", "pg_isready"): _FakeCompleted(1, "", ""),
            _argv("psql", "-U", "postgres", "-c", "SELECT 1", "-q", "--no-password"): _FakeCompleted(
                0, "", ""
            ),
        }
    )

    installed, version, running = _detect_postgres()
    assert installed is True
    assert version == "16.2"
    assert running is True


def test_detect_postgres_version_is_empty_when_no_token_starts_with_a_digit(fake_run):
    """A localized banner with no digit-leading token yields an empty version."""
    fake_run(
        {
            _argv("which", "psql"): _FakeCompleted(0, "", ""),
            _argv("psql", "--version"): _FakeCompleted(0, "psql (PostgreSQL)", ""),
            _argv("which", "pg_isready"): _FakeCompleted(0, "", ""),
            _argv("pg_isready", "-q"): _FakeCompleted(1, "", ""),
        }
    )

    assert _detect_postgres() == (True, "", False)


def test_detect_postgres_accepts_a_major_only_banner_verbatim(fake_run):
    """PG 10+ banners are major-only ("psql (PostgreSQL) 16"); kept as-is.

    ``pg_version`` is whatever the last digit-leading token says — no
    dotted-version validation and no format normalisation.
    """
    fake_run(
        {
            _argv("which", "psql"): _FakeCompleted(0, "", ""),
            _argv("psql", "--version"): _FakeCompleted(0, "psql (PostgreSQL) 16", ""),
            _argv("which", "pg_isready"): _FakeCompleted(0, "", ""),
            _argv("pg_isready", "-q"): _FakeCompleted(0, "", ""),
        }
    )

    assert _detect_postgres() == (True, "16", True)



def test_detect_postgres_version_lookup_failure_leaves_version_empty(fake_run):
    """A non-zero ``psql --version`` yields an empty version but 'installed'."""
    fake_run(
        {
            _argv("which", "psql"): _FakeCompleted(0, "", ""),
            _argv("psql", "--version"): _FakeCompleted(127, "", "not executable"),
            _argv("which", "pg_isready"): _FakeCompleted(0, "", ""),
            _argv("pg_isready", "-q"): _FakeCompleted(1, "", ""),
        }
    )

    assert _detect_postgres() == (True, "", False)


def test_detect_postgres_falls_back_to_psql_when_pg_isready_is_absent(fake_run):
    """Without ``pg_isready`` the running check is a passwordless ``SELECT 1``."""
    table = fake_run(
        {
            _argv("which", "psql"): _FakeCompleted(0, "", ""),
            _argv("psql", "--version"): _FakeCompleted(0, "psql (PostgreSQL) 15.4", ""),
            _argv("which", "pg_isready"): _FakeCompleted(1, "", ""),
            _argv("psql", "-U", "postgres", "-c", "SELECT 1", "-q", "--no-password"): _FakeCompleted(
                0, "1", ""
            ),
        }
    )

    assert _detect_postgres() == (True, "15.4", True)
    assert "psql -U postgres -c SELECT 1 -q --no-password" in table.commands()
    assert "pg_isready -q" not in table.commands()


def test_detect_postgres_fallback_connect_failure_is_not_running(fake_run):
    fake_run(
        {
            _argv("which", "psql"): _FakeCompleted(1, "", ""),
            _argv("which", "pg_isready"): _FakeCompleted(1, "", ""),
            _argv("psql", "-U", "postgres", "-c", "SELECT 1", "-q", "--no-password"): _FakeCompleted(
                2, "", "connection refused"
            ),
        }
    )

    assert _detect_postgres() == (False, "", False)


def test_detect_postgres_survives_every_command_raising(fake_run):
    """A host with no PATH tools at all still produces a full triple."""
    fake_run(default=FileNotFoundError(2, "no such file"))

    assert _detect_postgres() == (False, "", False)


# ---------------------------------------------------------------------------
# _detect_pgvector
# ---------------------------------------------------------------------------


def test_detect_pgvector_is_false_without_querying_when_pg_is_down():
    """The early return means zero subprocesses run when PG is not running."""

    def exploding(cmd, *args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError(f"no command may run: {cmd}")

    original = detect_mod.subprocess.run
    detect_mod.subprocess.run = exploding
    try:
        assert _detect_pgvector(False) is False
    finally:
        detect_mod.subprocess.run = original


def test_detect_pgvector_true_via_the_sudo_probe(fake_run):
    """The sudo probe answers first; the unprivileged probe is not run."""
    table = fake_run(
        {
            "sudo -u postgres psql -c SELECT 1 FROM pg_available_extensions WHERE name='vector' -t -A postgres": _FakeCompleted(
                0, "1\n", ""
            ),
        }
    )

    assert _detect_pgvector(True) is True
    assert len(table.calls) == 1


def test_detect_pgvector_falls_through_to_the_unprivileged_probe(fake_run):
    """A failing sudo probe is followed by the ``--no-password`` probe."""
    table = fake_run(
        {
            "sudo -u postgres psql -c SELECT 1 FROM pg_available_extensions WHERE name='vector' -t -A postgres": _FakeCompleted(
                1, "", "sudo: a password is required"
            ),
            "psql -U postgres -c SELECT 1 FROM pg_available_extensions WHERE name='vector' -t -A postgres --no-password": _FakeCompleted(
                0, "1", ""
            ),
        }
    )

    assert _detect_pgvector(True) is True
    assert len(table.calls) == 2


def test_detect_pgvector_requires_a_literal_1_in_the_output(fake_run):
    """An empty successful result means the extension is not available."""
    fake_run(
        {
            "sudo -u postgres psql -c SELECT 1 FROM pg_available_extensions WHERE name='vector' -t -A postgres": _FakeCompleted(
                0, "", ""
            ),
            "psql -U postgres -c SELECT 1 FROM pg_available_extensions WHERE name='vector' -t -A postgres --no-password": _FakeCompleted(
                0, "(0 rows)", ""
            ),
        }
    )

    assert _detect_pgvector(True) is False


def test_detect_pgvector_matches_a_1_anywhere_in_the_output(fake_run):
    """The check is ``"1" in out``, not equality — a row count of 10 matches."""
    fake_run(
        {
            "sudo -u postgres psql -c SELECT 1 FROM pg_available_extensions WHERE name='vector' -t -A postgres": _FakeCompleted(
                0, "10 rows", ""
            ),
            "psql -U postgres -c SELECT 1 FROM pg_available_extensions WHERE name='vector' -t -A postgres --no-password": _FakeCompleted(
                1, "", ""
            ),
        }
    )

    assert _detect_pgvector(True) is True


def test_detect_pgvector_uses_a_five_second_timeout_on_both_probes(fake_run):
    """A hung ``psql`` must not stall the installer for the default 10s twice."""
    table = fake_run(default=_FakeCompleted(1, "", ""))

    _detect_pgvector(True)

    assert [kw["timeout"] for kw in table.kwargs] == [5, 5]


def test_detect_pgvector_never_raises_when_psql_is_missing(fake_run):
    fake_run(default=FileNotFoundError(2, "no psql"))

    assert _detect_pgvector(True) is False


# ---------------------------------------------------------------------------
# _disk_free_gb
# ---------------------------------------------------------------------------


def test_disk_free_gb_converts_fragment_blocks_to_gib(monkeypatch):
    """``f_bavail * f_frsize / 1024**3`` — note: bavail, not bfree."""
    stat = os.statvfs_result(
        (4096, 4096, 1000, 500, 400, 950, 950, 0, 0, 255)
    )
    monkeypatch.setattr(detect_mod.os, "statvfs", lambda path: stat)

    assert _disk_free_gb("/") == pytest.approx(400 * 4096 / 1024**3)


def test_disk_free_gb_boundary_exactly_10_gib(monkeypatch):
    """Pin the threshold comparison at the 10 GiB OK boundary."""
    blocks = 10 * 1024**3 // 4096
    stat = os.statvfs_result((4096, 4096, blocks, blocks, blocks, blocks, blocks, 0, 0, 255))
    monkeypatch.setattr(detect_mod.os, "statvfs", lambda path: stat)

    assert _disk_free_gb() == pytest.approx(10.0)


def test_disk_free_gb_zero_blocks_is_zero(monkeypatch):
    stat = os.statvfs_result((4096, 4096, 0, 0, 0, 0, 0, 0, 0, 255))
    monkeypatch.setattr(detect_mod.os, "statvfs", lambda path: stat)

    assert _disk_free_gb() == 0.0


@pytest.mark.parametrize(
    "error",
    [OSError(2, "No such file or directory"), AttributeError("no statvfs"), ValueError("bad")],
    ids=["oserror", "attributeerror", "valueerror"],
)
def test_disk_free_gb_returns_zero_on_any_error(monkeypatch, error):
    """The Windows/``AttributeError`` case is why the handler is ``Exception``."""

    def boom(path):
        raise error

    monkeypatch.setattr(detect_mod.os, "statvfs", boom)

    assert _disk_free_gb("/") == 0.0


def test_disk_free_gb_defaults_to_root(monkeypatch):
    seen: list[str] = []
    stat = os.statvfs_result((4096, 4096, 1, 1, 1, 1, 1, 0, 0, 255))

    def spy(path):
        seen.append(path)
        return stat

    monkeypatch.setattr(detect_mod.os, "statvfs", spy)

    _disk_free_gb()
    _disk_free_gb("/data")

    assert seen == ["/", "/data"]


def test_disk_free_gb_returns_a_float(monkeypatch):
    stat = os.statvfs_result((4096, 4096, 8, 8, 8, 8, 8, 0, 0, 255))
    monkeypatch.setattr(detect_mod.os, "statvfs", lambda path: stat)

    assert isinstance(_disk_free_gb("/"), float)


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------


@pytest.fixture
def wired_detect(monkeypatch, fake_run, os_release):
    """Wire every seam ``detect()`` touches, then return a tuning helper.

    Returns a dict of setters so each test flips exactly one probe.
    """
    os_release('ID="debian"\nVERSION_ID="13.2"\n')
    state = {
        "which psql": _FakeCompleted(0, "", ""),
        "psql --version": _FakeCompleted(0, "psql (PostgreSQL) 16.2", ""),
        "which pg_isready": _FakeCompleted(0, "", ""),
        "pg_isready -q": _FakeCompleted(0, "", ""),
        "pgvector": _FakeCompleted(0, "1", ""),
        "which systemctl": _FakeCompleted(0, "", ""),
        "which launchctl": _FakeCompleted(0, "", ""),
        "which pip3": _FakeCompleted(0, "", ""),
        "which pip": _FakeCompleted(0, "", ""),
        _argv(sys.executable, "-m", "venv", "--help"): _FakeCompleted(0, "", ""),
        "which git": _FakeCompleted(0, "", ""),
    }
    table = fake_run({}, default=_FakeCompleted(1, "", ""))

    def responder(cmd, *args, **kwargs):
        key = " ".join(str(c) for c in cmd)
        if key.startswith("psql -U postgres -c SELECT 1 FROM") or key.startswith(
            "sudo -u postgres psql"
        ):
            value = state["pgvector"]
        else:
            value = state.get(key, _FakeCompleted(1, "", ""))
        table.calls.append(list(cmd))
        table.kwargs.append(kwargs)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(detect_mod.subprocess, "run", responder)

    def set_statvfs(gib: float) -> None:
        blocks = int(gib * 1024**3) // 4096
        stat = os.statvfs_result((4096, 4096, blocks, blocks, blocks, blocks, blocks, 0, 0, 255))
        monkeypatch.setattr(detect_mod.os, "statvfs", lambda path: stat)

    set_statvfs(100)

    return {"state": state, "table": table, "set_statvfs": set_statvfs}


def test_detect_assembles_a_fully_green_linux_host(wired_detect):
    info = detect()

    assert isinstance(info, SystemInfo)
    assert info.os_type == "linux"
    assert info.distro == "debian"
    assert info.distro_version == "13.2"
    assert info.pg_installed is True
    assert info.pg_version == "16.2"
    assert info.pg_running is True
    assert info.pgvector_available is True
    assert info.systemd is True
    assert info.launchd is False
    assert info.pip_available is True
    assert info.venv_available is True
    assert info.git_available is True
    assert info.disk_free_gb == pytest.approx(100.0, abs=1e-3)


def test_detect_uses_the_live_interpreter_version(monkeypatch, wired_detect):
    """``python_version``/``python_ok`` come from ``sys.version_info``."""
    info = detect()

    assert info.python_version == sys.version_info[:3]
    assert info.python_ok is (sys.version_info >= (3, 11))
    assert len(info.python_version) == 3


def test_detect_python_ok_is_false_below_3_11(monkeypatch, wired_detect):
    """The gate is ``>= (3, 11)`` — 3.10 is too old, 3.11 is fine."""

    class _OldVersion(tuple):
        major, minor, micro, releaselevel, serial = 3, 10, 9, "final", 0

        def __new__(cls):
            return super().__new__(cls, (3, 10, 9, "final", 0))

    monkeypatch.setattr(detect_mod.sys, "version_info", _OldVersion())

    info = detect()

    assert info.python_version == (3, 10, 9)
    assert info.python_ok is False


def test_detect_launchd_is_only_checked_when_the_os_is_macos(wired_detect):
    """On Linux ``launchctl`` is never even probed."""
    info = detect()

    assert info.launchd is False
    assert "which launchctl" not in wired_detect["table"].commands()


def test_detect_launchd_is_true_on_macos_with_launchctl(monkeypatch, wired_detect):
    monkeypatch.setattr(detect_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(detect_mod.platform, "mac_ver", lambda: ("15.4", ("", "", ""), ""))

    info = detect()

    assert info.os_type == "macos"
    assert info.launchd is True


def test_detect_launchd_is_false_on_macos_without_launchctl(monkeypatch, wired_detect):
    monkeypatch.setattr(detect_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(detect_mod.platform, "mac_ver", lambda: ("15.4", ("", "", ""), ""))
    wired_detect["state"]["which launchctl"] = _FakeCompleted(1, "", "")

    info = detect()

    assert info.os_type == "macos"
    assert info.launchd is False


def test_detect_pip_available_short_circuits_on_pip3(wired_detect):
    """``pip3`` wins; ``pip`` is not probed when ``pip3`` is present."""
    wired_detect["state"]["which pip3"] = _FakeCompleted(0, "", "")

    info = detect()

    assert info.pip_available is True
    assert "which pip" not in wired_detect["table"].commands()


def test_detect_pip_available_falls_back_to_pip(wired_detect):
    wired_detect["state"]["which pip3"] = _FakeCompleted(1, "", "")
    wired_detect["state"]["which pip"] = _FakeCompleted(0, "", "")

    assert detect().pip_available is True


def test_detect_pip_available_is_false_when_neither_exists(wired_detect):
    wired_detect["state"]["which pip3"] = _FakeCompleted(1, "", "")
    wired_detect["state"]["which pip"] = _FakeCompleted(1, "", "")

    assert detect().pip_available is False


def test_detect_venv_is_probed_with_the_running_interpreter(wired_detect):
    wired_detect["state"][_argv(sys.executable, "-m", "venv", "--help")] = _FakeCompleted(0, "usage: venv", "")

    info = detect()

    assert info.venv_available is True
    assert _argv(sys.executable, "-m", "venv", "--help") in wired_detect["table"].commands()


def test_detect_venv_unavailable_on_non_zero_exit(wired_detect):
    wired_detect["state"][_argv(sys.executable, "-m", "venv", "--help")] = _FakeCompleted(1, "", "No module named venv")

    assert detect().venv_available is False


def test_detect_venv_unavailable_when_the_probe_raises(wired_detect):
    wired_detect["state"][_argv(sys.executable, "-m", "venv", "--help")] = FileNotFoundError(2, "no interpreter")

    assert detect().venv_available is False


def test_detect_reports_pgvector_false_when_postgres_is_not_running(wired_detect):
    """A stopped cluster short-circuits pgvector detection to False."""
    wired_detect["state"]["pg_isready -q"] = _FakeCompleted(2, "", "no response")

    info = detect()

    assert info.pg_running is False
    assert info.pgvector_available is False


def test_detect_disk_free_gb_is_read_from_slash(wired_detect):
    """``detect()`` always stats ``/``, never the repo or cwd."""
    wired_detect["set_statvfs"](5.5)

    info = detect()

    assert info.disk_free_gb == pytest.approx(5.5, abs=1e-3)


def test_detect_is_resilient_when_every_probe_fails(wired_detect):
    """An empty host: no tools, no PG, no init system — every flag falsy."""
    for key in list(wired_detect["state"]):
        wired_detect["state"][key] = _FakeCompleted(1, "", "")
    wired_detect["state"]["pg_isready -q"] = _FakeCompleted(1, "", "")
    wired_detect["set_statvfs"](0)

    info = detect()

    assert info.pg_installed is False
    assert info.pg_version == ""
    assert info.pg_running is False
    assert info.pgvector_available is False
    assert info.systemd is False
    assert info.launchd is False
    assert info.pip_available is False
    assert info.venv_available is False
    assert info.git_available is False
    assert info.disk_free_gb == 0.0


def test_detect_returns_a_fresh_object_each_call(wired_detect):
    """No module-level cache: two calls are independent instances."""
    first = detect()
    second = detect()

    assert first is not second
    assert first == second


def test_detect_never_raises_for_an_unknown_platform(wired_detect, monkeypatch):
    monkeypatch.setattr(detect_mod.platform, "system", lambda: "Plan9")

    info = detect()

    assert info.os_type == "unknown"
    assert info.distro == "unknown"
    assert info.distro_version == ""


# ---------------------------------------------------------------------------
# print_summary
# ---------------------------------------------------------------------------


def test_print_summary_opens_with_the_banner_and_a_blank_line():
    lines = _summary(_healthy())

    assert lines[0] == ""
    assert lines[1] == "=== MNEMOS Environment Check ==="
    assert lines[2] == ""


def test_print_summary_ends_with_exactly_one_blank_line():
    lines = _summary(_healthy())

    assert lines[-1] == ""
    assert lines[-2] != ""


def test_print_summary_healthy_host_has_no_warn_or_fail_lines():
    lines = _summary(_healthy())

    assert all(_level(line) in ("ok", "plain") for line in lines), lines


def test_print_summary_prints_the_os_line_verbatim():
    info = _healthy(os_type="macos", distro="macos", distro_version="15.4")

    lines = _summary(info)

    assert f"{GREEN}[  OK  ]{RESET} OS: macos / macos 15.4" in lines


def test_print_summary_os_line_is_ok_even_for_an_unknown_platform():
    """The OS line is unconditional ``ok()`` — never WARN or FAIL."""
    lines = _summary(_healthy(os_type="unknown", distro="unknown", distro_version=""))

    assert _level(lines[3]) == "ok"
    assert lines[3].endswith("OS: unknown / unknown ")


def test_print_summary_python_ok_line_names_the_required_version():
    lines = _summary(_healthy(python_version=(3, 11, 4), python_ok=True))

    assert _level(lines[4]) == "ok"
    assert lines[4].endswith("Python 3.11.4 (>= 3.11 required)")


def test_print_summary_python_too_old_is_a_fail_with_an_em_dash():
    lines = _summary(_healthy(python_version=(3, 9, 18), python_ok=False))

    assert _level(lines[4]) == "fail"
    assert lines[4].endswith("Python 3.9.18 — need >= 3.11")


def test_print_summary_python_version_is_dotted_joined():
    """A two-part or four-part tuple joins with single dots, no padding."""
    lines = _summary(_healthy(python_version=(3, 13), python_ok=True))

    assert lines[4].endswith("Python 3.13 (>= 3.11 required)")


def test_print_summary_python_version_boundary_3_11_0_is_ok():
    assert _level(_summary(_healthy(python_version=(3, 11, 0), python_ok=True))[4]) == "ok"


def test_print_summary_postgres_installed_line_carries_the_version():
    lines = _summary(_healthy(pg_installed=True, pg_version="16.2"))

    assert f"{GREEN}[  OK  ]{RESET} PostgreSQL installed: 16.2" in lines


def test_print_summary_postgres_missing_is_a_warn_about_installing_it():
    lines = _summary(_healthy(pg_installed=False))

    assert f"{YELLOW}[ WARN ]{RESET} PostgreSQL not installed (will be installed)" in lines


def test_print_summary_postgres_installed_with_empty_version():
    lines = _summary(_healthy(pg_installed=True, pg_version=""))

    assert f"{GREEN}[  OK  ]{RESET} PostgreSQL installed: " in lines


def test_print_summary_postgres_not_running_is_a_warn():
    lines = _summary(_healthy(pg_running=False))

    assert f"{YELLOW}[ WARN ]{RESET} PostgreSQL not running" in lines


def test_print_summary_pgvector_available_is_ok():
    lines = _summary(_healthy(pgvector_available=True))

    assert f"{GREEN}[  OK  ]{RESET} pgvector extension available" in lines


def test_print_summary_pgvector_missing_is_a_warn_about_installing_it():
    lines = _summary(_healthy(pgvector_available=False))

    assert f"{YELLOW}[ WARN ]{RESET} pgvector not detected (will be installed)" in lines


def test_print_summary_init_system_prefers_systemd():
    lines = _summary(_healthy(systemd=True, launchd=True))

    assert f"{GREEN}[  OK  ]{RESET} systemd available" in lines
    assert "launchd available (macOS)" not in " ".join(lines)


def test_print_summary_init_system_uses_launchd_only_when_systemd_is_absent():
    lines = _summary(_healthy(systemd=False, launchd=True))

    assert f"{GREEN}[  OK  ]{RESET} launchd available (macOS)" in lines


def test_print_summary_no_init_system_is_a_warn():
    lines = _summary(_healthy(systemd=False, launchd=False))

    assert f"{YELLOW}[ WARN ]{RESET} No init system detected — manual service start only" in lines


@pytest.mark.parametrize(
    ("gib", "level", "suffix"),
    [
        (10.0, "ok", "Disk free: 10.0 GB"),
        (10.04, "ok", "Disk free: 10.0 GB"),
        (9.99, "warn", "Disk free: 10.0 GB (low — 10 GB recommended)"),
        (9.94, "warn", "Disk free: 9.9 GB (low — 10 GB recommended)"),
        (5.0, "warn", "Disk free: 5.0 GB (low — 10 GB recommended)"),
        (4.99, "fail", "Disk free: 5.0 GB (too low)"),
        (4.94, "fail", "Disk free: 4.9 GB (too low)"),
        (0.0, "fail", "Disk free: 0.0 GB (too low)"),
    ],
)
def test_print_summary_disk_thresholds(gib, level, suffix):
    """``>= 10`` OK, ``>= 5`` WARN, else FAIL; rendered with one decimal."""
    lines = _summary(_healthy(disk_free_gb=gib))

    disk = [line for line in lines if "Disk free:" in line]
    assert len(disk) == 1
    assert _level(disk[0]) == level
    assert disk[0].endswith(suffix)


def test_print_summary_disk_free_negative_is_a_fail():
    """A negative reading (a broken statvfs fake) still renders as FAIL."""
    lines = _summary(_healthy(disk_free_gb=-1.0))

    disk = [line for line in lines if "Disk free:" in line][0]
    assert _level(disk) == "fail"
    assert disk.endswith("Disk free: -1.0 GB (too low)")


@pytest.mark.parametrize(
    ("field", "ok_text", "warn_text"),
    [
        ("pip_available", "pip available", "pip not found (will bootstrap)"),
        ("venv_available", "venv module available", "venv not available"),
        ("git_available", "git available", "git not found"),
    ],
)
def test_print_summary_tool_lines_use_warn_not_fail(field, ok_text, warn_text):
    """Missing tools are WARN: the installer bootstraps them (``warn_mode=True``)."""
    present = _summary(_healthy(**{field: True}))
    missing = _summary(_healthy(**{field: False}))

    assert f"{GREEN}[  OK  ]{RESET} {ok_text}" in present
    assert f"{YELLOW}[ WARN ]{RESET} {warn_text}" in missing
    assert not any(RED in line for line in missing)


def test_print_summary_tool_lines_never_use_the_fail_branch():
    """DEAD BRANCH, pinned as observed behaviour (not a bug report).

    ``print_summary`` declares a ``status(..., warn_mode=False)`` helper whose
    third branch returns ``fail(...)``, but all three callers pass
    ``warn_mode=True``, so no tool line can ever print ``[ FAIL ]``. A missing
    pip/venv/git is always WARN ("will bootstrap" / manual). This test locks
    that in: if a caller ever starts failing hard on a missing tool, it fails
    here and the change gets a deliberate review.
    """
    info = SystemInfo(
        python_version=(3, 13, 0),
        python_ok=True,
        disk_free_gb=100.0,
        pip_available=False,
        venv_available=False,
        git_available=False,
    )

    tool_lines = [
        line
        for line in _summary(info)
        if line.endswith(("pip not found (will bootstrap)", "venv not available", "git not found"))
    ]

    assert len(tool_lines) == 3
    assert all(_level(line) == "warn" for line in tool_lines)


def test_print_summary_never_emits_a_fail_line_for_a_healthy_host():
    lines = _summary(_healthy())

    assert not any(_level(line) == "fail" for line in lines)


def test_print_summary_all_red_host_reports_every_failure():
    info = SystemInfo(
        os_type="unknown",
        python_version=(3, 6, 0),
        python_ok=False,
        pg_installed=False,
        pg_running=False,
        pgvector_available=False,
        systemd=False,
        launchd=False,
        disk_free_gb=1.0,
        pip_available=False,
        venv_available=False,
        git_available=False,
    )

    lines = _summary(info)

    assert _level(lines[4]) == "fail"
    assert any("Disk free: 1.0 GB (too low)" in line for line in lines)
    assert sum(1 for line in lines if _level(line) == "fail") == 2
    assert sum(1 for line in lines if _level(line) == "warn") >= 5


def test_print_summary_line_order_is_stable():
    """The sections are printed in a fixed order: OS → Python → PG → init → disk → tools."""
    lines = _summary(_healthy())
    body = lines[3:-1]
    markers = ["OS:", "Python ", "PostgreSQL installed", "PostgreSQL is running",
               "pgvector", "systemd", "Disk free:", "pip available",
               "venv module available", "git available"]
    positions = []
    for marker in markers:
        idx = next(i for i, line in enumerate(body) if marker in line)
        positions.append(idx)

    assert positions == sorted(positions)


def test_print_summary_writes_nothing_when_stdout_is_captured_by_pytest(capsys):
    """It uses bare ``print`` (stdout), never a logger or ``sys.stderr``."""
    print_summary(_healthy())

    captured = capsys.readouterr()
    assert "=== MNEMOS Environment Check ===" in captured.out
    assert captured.err == ""


def test_print_summary_accepts_a_default_constructed_system_info():
    """An empty ``SystemInfo()`` prints without raising (all-falsy branch set)."""
    lines = _summary(SystemInfo())

    assert "=== MNEMOS Environment Check ===" in lines
    assert any(_level(line) == "fail" for line in lines)


# ---------------------------------------------------------------------------
# check_port_free
# ---------------------------------------------------------------------------


def test_check_port_free_is_true_for_a_closed_port():
    """Port 0 always binds to an ephemeral port, so it is reliably free."""
    assert check_port_free(0) is True


def test_check_port_free_is_false_when_the_port_is_bound():
    """A listening socket on 127.0.0.1 makes the probe report occupied."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]

        assert check_port_free(port) is False


def test_check_port_free_releases_the_port_afterwards():
    """The probe closes its socket, so a free port stays usable."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]

    assert check_port_free(port) is True


def test_check_port_free_returns_true_when_the_port_freed_in_the_meantime():
    """Repeated probes on a free port are idempotent."""
    assert check_port_free(0) is True
    assert check_port_free(0) is True


@pytest.mark.parametrize("port", [-1, 65536, 70000])
def test_check_port_free_propagates_overflowerror_for_an_out_of_range_port(port):
    """CURRENT behaviour: an out-of-range port raises instead of returning False.

    ``socket.bind`` raises ``OverflowError`` (not ``OSError``) for a port
    outside 0-65535, and ``check_port_free`` only catches ``OSError``, so the
    exception escapes to ``wizard.run_wizard``'s prompt loop. Pinned as-is:
    the caller never passes an out-of-range port because ``_prompt_int``
    validates first.
    """
    with pytest.raises(OverflowError, match="port must be 0-65535"):
        check_port_free(port)


def test_check_port_free_uses_reuseaddr(monkeypatch):
    """SO_REUSEADDR is set before the bind (TIME_WAIT sockets are 'free')."""
    seen: list[tuple] = []
    real_socket = socket.socket

    class _Spy:
        def __init__(self, *args, **kwargs):
            self._wrapped = real_socket(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._wrapped.close()
            return False

        def setsockopt(self, level, opt, value):
            seen.append((level, opt, value))
            return self._wrapped.setsockopt(level, opt, value)

        def bind(self, address):
            return self._wrapped.bind(address)

    monkeypatch.setattr(detect_mod.socket, "socket", _Spy)

    assert check_port_free(0) is True
    assert (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) in seen


def test_check_port_free_binds_the_loopback_address(monkeypatch):
    """Only 127.0.0.1 is probed — never 0.0.0.0 or a hostname."""
    bound: list[tuple] = []
    real_socket = socket.socket

    class _Spy:
        def __init__(self, *args, **kwargs):
            self._wrapped = real_socket(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._wrapped.close()
            return False

        def setsockopt(self, *args):
            return self._wrapped.setsockopt(*args)

        def bind(self, address):
            bound.append(address)
            return self._wrapped.bind(address)

    monkeypatch.setattr(detect_mod.socket, "socket", _Spy)

    check_port_free(0)

    assert bound == [("127.0.0.1", 0)]


@pytest.mark.parametrize("error", [OSError(98, "Address already in use"), PermissionError(13, "denied")])
def test_check_port_free_swallows_only_oserror(monkeypatch, error):
    """A non-OSError from ``socket()`` is NOT caught — it propagates."""

    class _Boom:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def setsockopt(self, *args):
            pass

        def bind(self, address):
            raise error

    monkeypatch.setattr(detect_mod.socket, "socket", _Boom)

    assert check_port_free(5002) is False


def test_check_port_free_propagates_a_non_oserror(monkeypatch):
    """Pinned: only ``OSError`` is handled; anything else escapes to the caller."""

    class _Boom:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def setsockopt(self, *args):
            pass

        def bind(self, address):
            raise RuntimeError("unexpected")

    monkeypatch.setattr(detect_mod.socket, "socket", _Boom)

    with pytest.raises(RuntimeError, match="unexpected"):
        check_port_free(5002)


def test_check_port_free_does_not_listen_or_connect(monkeypatch):
    """The probe binds and closes; it never calls listen/accept/connect."""
    forbidden: list[str] = []
    opened: list[str] = []
    real_socket = socket.socket

    class _Spy:
        def __init__(self, *args, **kwargs):
            opened.append("socket")
            self._wrapped = real_socket(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._wrapped.close()
            return False

        def setsockopt(self, *args):
            return self._wrapped.setsockopt(*args)

        def bind(self, address):
            return self._wrapped.bind(address)

        def listen(self, *args):
            forbidden.append("listen")

        def connect(self, *args):
            forbidden.append("connect")

    monkeypatch.setattr(detect_mod.socket, "socket", _Spy)

    result = check_port_free(0)

    assert forbidden == []
    # A socket must genuinely be opened and bound: an implementation that
    # returned early would leave `opened` empty and make the assertion above
    # vacuous, so pin that the probe actually reached the kernel.
    assert opened == ["socket"]
    assert result is True


# ---------------------------------------------------------------------------
# module surface
# ---------------------------------------------------------------------------


def test_module_main_guard_calls_detect_then_print_summary(monkeypatch, capsys):
    """Under ``__name__ == "__main__"`` the module detects and prints, then exits.

    The guard is executed from the module's own source in a fresh namespace
    whose ``detect``/``print_summary`` are the recorder doubles, so the real
    probes never run.
    """
    calls: list[str] = []
    source = Path(detect_mod.__file__).read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source
    namespace = {
        "__name__": "__main__",
        "__file__": detect_mod.__file__,
        "detect": lambda: calls.append("detect") or _healthy(),
        "print_summary": lambda info: calls.append("print"),
    }
    guard = source[source.index('if __name__ == "__main__":'):]

    exec(compile(guard, detect_mod.__file__, "exec"), namespace)

    assert calls == ["detect", "print"]


def test_module_exposes_no_unexpected_public_names():
    """The public surface the installer imports is exactly this set."""
    public = {name for name in vars(detect_mod) if not name.startswith("_")}
    assert {
        "SystemInfo",
        "detect",
        "print_summary",
        "check_port_free",
        "os",
        "platform",
        "socket",
        "subprocess",
        "sys",
        "dataclass",
    } <= public
