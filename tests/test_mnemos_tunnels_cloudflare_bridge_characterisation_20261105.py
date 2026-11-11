"""Characterisation tests for ``mnemos/tunnels/cloudflare_bridge.py``.

The module had zero test references before this file. These tests PIN its
current behaviour — argv shape, the filtered child environment, the URL
regex, the event/state machine around ``_url``/``_url_seen``, and every
exception it raises — so that a future intentional change has to update a
pin instead of silently altering the tunnel API's contract.

They are not a design proposal. Where today's behaviour looks wrong it is
recorded as ``xfail(strict=True)`` with the reason, never "fixed" here.

No real network, no real subprocesses, no sleeping: the ``cloudflared``
child is a fake process object, and the clock is exercised only through
``asyncio.Event`` which the tests set explicitly.

One genuine defect is pinned as a strict xfail (reason on the test):

``_spawn_spec`` replaces ``_url_seen`` with a fresh ``asyncio.Event`` without
cancelling a waiter still blocked on the OLD event, so every restart that
abandons a pending ``_await_public_url`` leaks one task.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
from typing import Optional

import pytest

from mnemos.tunnels import base as tunnel_base
from mnemos.tunnels.base import (
    OUTPUT_RING_SIZE,
    TunnelBinaryMissingError,
    TunnelBridge,
    TunnelStartError,
    child_env,
)
from mnemos.tunnels.cloudflare_bridge import CloudflareBridge, _QUICK_TUNNEL_URL

URL = "https://foo-bar-baz-qux.trycloudflare.com"


# ---------------------------------------------------------------------------
# Fakes and fixtures
# ---------------------------------------------------------------------------


class FakeProcess:
    """Stand-in for ``asyncio.subprocess.Process``.

    Behaves like a live agent by default: it only "exits" when the test
    calls :meth:`die`, which is what makes the tests deterministic without
    any real process or wall-clock waiting.
    """

    def __init__(self, *, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: Optional[int] = None
        self.stdout = None
        self._exited = asyncio.Event()
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        await self._exited.wait()
        return self.returncode  # type: ignore[return-value]

    def die(self, code: int = 1) -> None:
        self.returncode = code
        self._exited.set()

    def terminate(self) -> None:
        self.terminated = True
        self.die(-15)

    def kill(self) -> None:
        self.killed = True
        self.die(-9)


class FakeProcThatNeverExits(FakeProcess):
    """A process whose ``wait()`` ignores SIGTERM/SIGKILL, so ``_reap``
    has to escalate and give up rather than block forever."""

    async def wait(self) -> int:
        await asyncio.sleep(3600)
        return self.returncode  # pragma: no cover

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


@pytest.fixture
def bridge() -> CloudflareBridge:
    return CloudflareBridge()


@pytest.fixture
def armed(bridge: CloudflareBridge) -> CloudflareBridge:
    """A bridge whose ``_spawn_spec`` has run, i.e. exactly the state the
    real ``start()`` is in while waiting for the URL."""
    bridge._spawn_spec(target_port=5004, authtoken=None)
    return bridge


# ---------------------------------------------------------------------------
# Class identity and contract attributes
# ---------------------------------------------------------------------------


def test_bridge_is_a_tunnel_bridge(bridge):
    assert isinstance(bridge, TunnelBridge)


def test_bridge_identifies_itself_as_the_cloudflare_backend(bridge):
    assert bridge.name == "cloudflare"


def test_bridge_binary_is_cloudflared(bridge):
    assert bridge.binary == "cloudflared"


def test_quick_tunnels_do_not_require_an_authtoken(bridge):
    """The zero-credential property is the reason this is the default
    backend; it must stay True unless the whole rationale changes."""
    assert bridge.requires_authtoken is False


def test_install_hint_names_the_binary_and_platforms(bridge):
    hint = bridge.install_hint
    assert "cloudflared" in hint
    assert "brew" in hint
    assert "apt" in hint
    assert "developers.cloudflare.com" in hint


def test_binary_path_is_none_when_cloudflared_is_absent(tmp_path, monkeypatch):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert CloudflareBridge.binary_path() is None


def test_binary_path_finds_an_executable_on_path(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "cloudflared"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    assert CloudflareBridge.binary_path() == str(exe)


def test_require_binary_raises_tunnel_binary_missing_error_with_hint(tmp_path, monkeypatch):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(TunnelBinaryMissingError) as excinfo:
        CloudflareBridge.require_binary()
    exc = excinfo.value
    assert exc.binary == "cloudflared"
    assert exc.install_hint == CloudflareBridge.install_hint
    assert str(exc) == (
        "`cloudflared` is not installed or not on PATH. " + CloudflareBridge.install_hint
    )


# ---------------------------------------------------------------------------
# Construction state
# ---------------------------------------------------------------------------


def test_new_bridge_starts_with_no_url_and_no_event(bridge):
    assert bridge._url is None
    assert bridge._url_seen is None


def test_new_bridge_has_empty_output_ring_and_no_process(bridge):
    assert bridge.recent_output() == "(no output captured)"
    assert bridge.status() is None


# ---------------------------------------------------------------------------
# _spawn_spec: argv
# ---------------------------------------------------------------------------


def test_spawn_spec_returns_exactly_the_quick_tunnel_argv(bridge):
    argv, _env = bridge._spawn_spec(target_port=5004, authtoken=None)
    assert argv == [
        "cloudflared",
        "tunnel",
        "--no-autoupdate",
        "--url",
        "http://localhost:5004",
    ]


def test_spawn_spec_targets_localhost_rather_than_127_0_0_1(bridge):
    """Pinned as-is: the literal host string is part of the argv contract
    even though both resolve to loopback."""

    argv, _env = bridge._spawn_spec(target_port=5004, authtoken=None)
    assert argv[argv.index("--url") + 1] == "http://localhost:5004"
    assert "127.0.0.1" not in argv


@pytest.mark.parametrize("port", [1, 80, 5002, 5004, 65535])
def test_spawn_spec_interpolates_every_valid_port(bridge, port):
    argv, _env = bridge._spawn_spec(target_port=port, authtoken=None)
    assert argv[-1] == f"http://localhost:{port}"


def test_spawn_spec_disables_the_self_update_check(bridge):
    argv, _env = bridge._spawn_spec(target_port=5004, authtoken=None)
    assert "--no-autoupdate" in argv


def test_spawn_spec_uses_a_bare_binary_name_so_path_lookup_happens_at_spawn(bridge):
    argv, _env = bridge._spawn_spec(target_port=5004, authtoken=None)
    assert argv[0] == "cloudflared"
    assert os.path.isabs(argv[0]) is False


# ---------------------------------------------------------------------------
# _spawn_spec: authtoken handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "authtoken",
    [None, "", "2zzzTOPSECRET_authtoken_value", "named-tunnel-token"],
)
def test_spawn_spec_accepts_and_ignores_any_authtoken_in_argv(bridge, authtoken):
    """A named-tunnel token must never be smuggled into the quick-tunnel
    command line — argv is world readable via `ps`."""
    argv, _env = bridge._spawn_spec(target_port=5004, authtoken=authtoken)
    assert argv == [
        "cloudflared",
        "tunnel",
        "--no-autoupdate",
        "--url",
        "http://localhost:5004",
    ]


def test_spawn_spec_never_puts_a_supplied_authtoken_in_argv(bridge):
    secret = "2zzzTOPSECRET_authtoken_value"
    argv, env = bridge._spawn_spec(target_port=5004, authtoken=secret)
    assert secret not in " ".join(argv)
    assert all(secret not in value for value in env.values())


def test_spawn_spec_does_not_export_the_authtoken_into_the_child_env(bridge, monkeypatch):
    monkeypatch.setenv("NGROK_AUTHTOKEN", "should-not-leak")
    _argv, env = bridge._spawn_spec(target_port=5004, authtoken="tok")
    assert "NGROK_AUTHTOKEN" not in env


# ---------------------------------------------------------------------------
# _spawn_spec: child environment
# ---------------------------------------------------------------------------


def test_spawn_spec_returns_the_filtered_child_env(bridge, monkeypatch):
    monkeypatch.setenv("MNEMOS_DATABASE_DSN", "postgres://u:p@h/db")
    _argv, env = bridge._spawn_spec(target_port=5004, authtoken=None)
    assert env == child_env()


def test_spawn_spec_withholds_mnemos_secrets_from_the_child(bridge, monkeypatch):
    monkeypatch.setenv("MNEMOS_DATABASE_DSN", "postgres://u:p@h/db")
    monkeypatch.setenv("MNEMOS_MCP_TOKEN", "bearer-secret")
    monkeypatch.setenv("MNEMOS_OAUTH_SIGNING_KEY", "signing-key")
    _argv, env = bridge._spawn_spec(target_port=5004, authtoken=None)
    for leaked in ("MNEMOS_DATABASE_DSN", "MNEMOS_MCP_TOKEN", "MNEMOS_OAUTH_SIGNING_KEY"):
        assert leaked not in env


def test_spawn_spec_keeps_path_for_the_child(bridge, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    _argv, env = bridge._spawn_spec(target_port=5004, authtoken=None)
    assert env.get("PATH") == "/usr/bin:/bin"


def test_spawn_spec_env_is_a_plain_dict(bridge):
    _argv, env = bridge._spawn_spec(target_port=5004, authtoken=None)
    assert isinstance(env, dict)


# ---------------------------------------------------------------------------
# _spawn_spec: state reset
# ---------------------------------------------------------------------------


def test_spawn_spec_creates_a_fresh_unset_url_event(bridge):
    _argv, _env = bridge._spawn_spec(target_port=5004, authtoken=None)
    assert isinstance(bridge._url_seen, asyncio.Event)
    assert bridge._url_seen.is_set() is False


def test_spawn_spec_clears_a_url_from_a_previous_attempt(bridge):
    bridge._url = "https://old-tunnel.trycloudflare.com"
    bridge._spawn_spec(target_port=5004, authtoken=None)
    assert bridge._url is None


def test_spawn_spec_replaces_rather_than_reuses_the_event(bridge):
    bridge._spawn_spec(target_port=5004, authtoken=None)
    first = bridge._url_seen
    bridge._spawn_spec(target_port=5004, authtoken=None)
    assert bridge._url_seen is not first


def test_spawn_spec_does_not_reset_the_output_ring(bridge):
    """start() clears the ring, not _spawn_spec() — pinned as-is."""
    bridge._output.append("previous line")
    bridge._spawn_spec(target_port=5004, authtoken=None)
    assert list(bridge._output) == ["previous line"]


# ---------------------------------------------------------------------------
# _on_output_line: URL discovery
# ---------------------------------------------------------------------------


def test_on_output_line_extracts_the_url_from_a_boxed_banner(armed):
    armed._on_output_line("+----------------------------------------------------+")
    armed._on_output_line(f"|  {URL}  |")
    assert armed._url == URL


def test_on_output_line_sets_the_url_event(armed):
    armed._on_output_line(f"|  {URL}  |")
    assert armed._url_seen.is_set() is True


def test_on_output_line_finds_a_bare_url(armed):
    armed._on_output_line(URL)
    assert armed._url == URL


def test_on_output_line_finds_a_url_inside_a_log_line(armed):
    armed._on_output_line(f"2026-09-14T00:00:00Z INF Registered tunnel {URL} ready")
    assert armed._url == URL


def test_on_output_line_ignores_a_line_without_a_url(armed):
    armed._on_output_line("2026-09-14T00:00:00Z INF Requesting new quick Tunnel")
    assert armed._url is None
    assert armed._url_seen.is_set() is False


def test_on_output_line_ignores_an_empty_line(armed):
    armed._on_output_line("")
    assert armed._url is None
    assert armed._url_seen.is_set() is False


@pytest.mark.parametrize(
    "line",
    [
        "https://abc.trycloudflare.com.evil.example",
        "https://abc.trycloudflare.com:8443",
        "https://abc.trycloudflare.com/path",
        "text before https://abc.trycloudflare.com text after",
    ],
    ids=["suffix-after-host", "port", "path", "embedded"],
)
def test_on_output_line_returns_only_the_bare_origin(armed, line):
    """The match is the scheme+host, not the token around it."""
    armed._on_output_line(line)
    assert armed._url == "https://abc.trycloudflare.com"


@pytest.mark.parametrize(
    "line",
    [
        "https://-abc.trycloudflare.com",  # label may not start with '-'
        "https://trycloudflare.com",  # empty label
        "https://abc.trycloudflare.co",  # suffix truncated
        "https://ABC.TRYCLOUDFLARE.COM",  # case sensitive
        "HTTP://abc.trycloudflare.com",  # scheme is case/anchor sensitive
        "https://a_b.trycloudflare.com",  # '_' not allowed in label
        "https://a.b.trycloudflare.com",  # nested label
        "https://.trycloudflare.com",
        "https://",
    ],
    ids=[
        "leading-hyphen",
        "empty-label",
        "truncated-suffix",
        "uppercase",
        "wrong-scheme",
        "underscore",
        "nested-label",
        "dot-before-label",
        "scheme-only",
    ],
)
def test_on_output_line_rejects_non_matching_variants(armed, line):
    armed._on_output_line(line)
    assert armed._url is None
    assert armed._url_seen.is_set() is False


def test_on_output_line_accepts_a_trailing_hyphen_label(armed):
    """Pinned as-is: only the FIRST character of the label is constrained,
    so a label ending in '-' is accepted by the current regex."""
    armed._on_output_line("https://abc-.trycloudflare.com")
    assert armed._url == "https://abc-.trycloudflare.com"


def test_on_output_line_accepts_single_character_labels(armed):
    armed._on_output_line("https://a.trycloudflare.com")
    assert armed._url == "https://a.trycloudflare.com"


def test_on_output_line_keeps_the_first_url_and_ignores_later_ones(armed):
    armed._on_output_line("https://first.trycloudflare.com")
    armed._on_output_line("https://second.trycloudflare.com")
    assert armed._url == "https://first.trycloudflare.com"


def test_on_output_line_is_a_no_op_once_a_url_is_known(armed):
    armed._on_output_line("https://first.trycloudflare.com")
    armed._url_seen.clear()
    armed._on_output_line("https://second.trycloudflare.com")
    assert armed._url == "https://first.trycloudflare.com"
    assert armed._url_seen.is_set() is False


def test_on_output_line_without_an_event_still_records_the_url(bridge):
    """``_on_output_line`` guards ``_url_seen is None``; only the URL is
    lost, nothing raises. Reachable only if the hook fires before
    _spawn_spec, which the base class never does — but the guard exists."""
    assert bridge._url_seen is None
    bridge._on_output_line(URL)
    assert bridge._url == URL
    assert bridge._url_seen is None


def test_on_output_line_does_not_raise_for_garbage(armed):
    armed._on_output_line("\x00\x01 not utf-8 surrogate \udcff")
    assert armed._url is None


def test_url_regex_is_compiled_into_a_pattern_object():
    assert hasattr(_QUICK_TUNNEL_URL, "search")
    assert _QUICK_TUNNEL_URL.pattern == (
        r"https://[A-Za-z0-9][A-Za-z0-9-]*\.trycloudflare\.com"
    )


# ---------------------------------------------------------------------------
# _await_public_url: happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_await_public_url_returns_the_url_once_the_event_fires(armed):
    proc = FakeProcess()
    task = asyncio.ensure_future(armed._await_public_url(proc))
    await asyncio.sleep(0)
    armed._on_output_line(f"|  {URL}  |")
    assert await asyncio.wait_for(task, timeout=1.0) == URL


@pytest.mark.asyncio
async def test_await_public_url_treats_a_set_event_without_a_url_as_a_start_failure(armed):
    """The guard is ``waiter in done`` AND ``self._url``. In production
    both are set together by ``_on_output_line``, so an event-without-URL
    can only mean the URL was lost; the method then takes the
    'exited with code N' error path rather than waiting forever."""
    proc = FakeProcess()
    armed._url_seen.set()
    armed._url = None
    with pytest.raises(TunnelStartError) as excinfo:
        await asyncio.wait_for(armed._await_public_url(proc), timeout=1.0)
    assert str(excinfo.value) == (
        "cloudflared exited with code None before reporting a tunnel URL. "
        "Output: (no output captured)"
    )


# ---------------------------------------------------------------------------
# _await_public_url: failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_await_public_url_raises_when_the_event_preregistered_no_url(armed):
    armed._url_seen.set()
    proc = FakeProcess()
    with pytest.raises(TunnelStartError) as excinfo:
        await armed._await_public_url(proc)
    assert str(excinfo.value) == (
        "cloudflared exited with code None before reporting a tunnel URL. "
        "Output: (no output captured)"
    )


@pytest.mark.asyncio
async def test_await_public_url_raises_tunnel_start_error_when_the_child_dies_first(armed):
    proc = FakeProcess()
    armed._output.append("ERR failed to dial cloudflare edge")
    proc.die(1)
    with pytest.raises(TunnelStartError) as excinfo:
        await armed._await_public_url(proc)
    message = str(excinfo.value)
    assert message.startswith("cloudflared exited with code 1 before reporting a tunnel URL.")
    assert "ERR failed to dial cloudflare edge" in message


@pytest.mark.asyncio
async def test_await_public_url_reports_the_exit_code_and_recent_output(armed):
    proc = FakeProcess()
    for index in range(3):
        armed._output.append(f"line-{index}")
    proc.die(2)
    with pytest.raises(TunnelStartError) as excinfo:
        await armed._await_public_url(proc)
    assert "code 2" in str(excinfo.value)
    assert "line-0 | line-1 | line-2" in str(excinfo.value)


@pytest.mark.asyncio
async def test_await_public_url_without_an_event_raises_the_unreachable_guard(bridge):
    """``_url_seen`` is created by ``_spawn_spec``; calling the method on a
    fresh bridge hits the guard, whose message is pinned verbatim."""
    proc = FakeProcess()
    with pytest.raises(TunnelStartError) as excinfo:
        await bridge._await_public_url(proc)
    assert str(excinfo.value) == "cloudflared URL event was never created"


@pytest.mark.asyncio
async def test_await_public_url_guard_fires_before_any_task_is_created(bridge):
    before = len(asyncio.all_tasks())
    with pytest.raises(TunnelStartError):
        await bridge._await_public_url(FakeProcess())
    assert len(asyncio.all_tasks()) == before


# ---------------------------------------------------------------------------
# _await_public_url: task cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_await_public_url_leaves_no_waiters_when_it_raises(armed):
    """The ``finally`` in _await_public_url cancels the event waiter on the
    failing path; nothing else stays parked on the loop."""
    proc = FakeProcess()
    proc.die(1)
    with pytest.raises(TunnelStartError):
        await armed._await_public_url(proc)
    await asyncio.sleep(0)  # let a cancelled waiter actually finish unwinding
    leftovers = [
        task
        for task in asyncio.all_tasks()
        if not task.done() and task is not asyncio.current_task()
    ]
    assert leftovers == []


@pytest.mark.asyncio
async def test_await_public_url_leaves_no_waiters_after_success(armed):
    """On success the event waiter has already finished, so the other side
    of the race — ``proc.wait()`` — is the only thing the ``finally`` has to
    cancel, and it does."""
    proc = FakeProcess()
    task = asyncio.ensure_future(armed._await_public_url(proc))
    await asyncio.sleep(0)
    armed._on_output_line(f"|  {URL}  |")
    assert await asyncio.wait_for(task, timeout=1.0) == URL
    await asyncio.sleep(0)  # let a cancelled waiter actually finish unwinding
    leftovers = [
        t
        for t in asyncio.all_tasks()
        if not t.done() and t is not asyncio.current_task()
    ]
    assert leftovers == []


@pytest.mark.asyncio
async def test_await_public_url_cancels_the_event_waiter_when_the_child_dies(armed):
    proc = FakeProcess()
    proc.die(1)
    seen_before = set(asyncio.all_tasks())
    with pytest.raises(TunnelStartError):
        await armed._await_public_url(proc)
    await asyncio.sleep(0)  # let a cancelled waiter actually finish unwinding
    new_pending = {
        task for task in asyncio.all_tasks() - seen_before if not task.done()
    }
    assert new_pending == set()


@pytest.mark.asyncio
async def test_await_public_url_is_cancellable_without_leaking_tasks(armed):
    proc = FakeProcess()
    task = asyncio.ensure_future(armed._await_public_url(proc))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)  # let the cancelled children finish unwinding
    pending = [
        t
        for t in asyncio.all_tasks()
        if not t.done() and t is not asyncio.current_task()
    ]
    assert pending == []


# ---------------------------------------------------------------------------
# Exceptions and types
# ---------------------------------------------------------------------------


def test_tunnel_start_error_is_a_runtime_error():
    assert issubclass(TunnelStartError, RuntimeError)


def test_spawn_spec_signature_is_keyword_only(bridge):
    params = inspect.signature(bridge._spawn_spec).parameters
    assert params["target_port"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["authtoken"].kind is inspect.Parameter.KEYWORD_ONLY


def test_spawn_spec_type_annotation_still_says_optional_env():
    """Pinned as-is: ``child_env()`` always returns a dict, so the
    ``Optional`` half of the annotation is dead. Cosmetic, not a defect —
    recorded so a future narrowing is a deliberate act."""
    annotation = inspect.signature(CloudflareBridge._spawn_spec).return_annotation
    assert "Optional" in str(annotation) or "None" in str(annotation)


def test_await_public_url_is_a_coroutine_function():
    assert inspect.iscoroutinefunction(CloudflareBridge._await_public_url)


# ---------------------------------------------------------------------------
# Base-class integration: ring, status, stop
# ---------------------------------------------------------------------------


def test_output_ring_is_bounded_to_the_base_class_size(bridge):
    for index in range(OUTPUT_RING_SIZE + 10):
        bridge._output.append(f"line-{index}")
    assert len(bridge._output) == OUTPUT_RING_SIZE
    assert bridge.recent_output().startswith("line-10 |")


def test_zero_output_is_reported_as_a_placeholder(bridge):
    assert bridge.recent_output() == "(no output captured)"


@pytest.mark.asyncio
async def test_stop_without_a_process_is_false(bridge):
    assert await bridge.stop() is False


@pytest.mark.asyncio
async def test_stop_with_a_live_process_terminates_it(bridge):
    proc = FakeProcess()
    bridge._proc = proc
    bridge._info = object()  # type: ignore[assignment]
    assert await bridge.stop() is True
    assert proc.terminated is True
    assert bridge._proc is None
    assert bridge._info is None


@pytest.mark.asyncio
async def test_stop_escalates_to_kill_when_the_agent_ignores_sigterm(bridge, monkeypatch):
    monkeypatch.setattr(tunnel_base, "TERMINATE_TIMEOUT_SECONDS", 0.01)
    proc = FakeProcThatNeverExits()
    bridge._proc = proc
    assert await bridge.stop() is True
    assert proc.terminated is True
    assert proc.killed is True


@pytest.mark.asyncio
async def test_stop_detaches_the_drain_task(bridge):
    drain = asyncio.ensure_future(asyncio.sleep(3600))
    bridge._proc = FakeProcess()
    bridge._drain_task = drain
    await bridge.stop()
    assert bridge._drain_task is None
    assert drain.done() or drain.cancelled()


def test_status_is_none_before_a_start(bridge):
    assert bridge.status() is None


# ---------------------------------------------------------------------------
# No-fault branch pins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_await_public_url_rechecks_the_url_before_returning(armed):
    """CHARACTERISATION, not a defect.

    ``_await_public_url`` keeps only the ``done`` set from
    ``asyncio.wait`` (it drops ``_pending``) and then branches on
    ``waiter in done and self._url``. The second half of that condition is
    not redundant: the only way the event gets set is ``_on_output_line``
    setting ``_url`` first in the same call, so testing the flag as well
    costs nothing and cannot go stale. If the child exits first,
    ``waiter in done`` is False and the 'exited with code N' error is
    raised.

    Pinned so a rewrite that, say, returns ``_url`` unconditionally — or
    that starts trusting ``done`` alone — is a visible behaviour change.
    """
    proc = FakeProcess()
    proc.die(3)
    with pytest.raises(TunnelStartError):
        await armed._await_public_url(proc)


# ---------------------------------------------------------------------------
# Defects pinned as strict xfail — do not "fix" these here.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT: CloudflareBridge._spawn_spec replaces self._url_seen with a "
        "fresh asyncio.Event() without cancelling a waiter still blocked on "
        "the OLD event. Any restart that abandons a pending "
        "_await_public_url() (agent died, client disconnected, second start) "
        "leaves a task parked on a dead event for the life of the event loop; "
        "at shutdown it is destroyed pending, and in a long-lived daemon each "
        "failed start leaks one. Fixed by cancelling the existing waiters, or "
        "by creating the event once in __init__ and clearing it in "
        "_spawn_spec instead of replacing it."
    ),
)
@pytest.mark.asyncio
async def test_spawn_spec_should_not_leave_a_waiter_on_the_discarded_event(armed):
    proc = FakeProcess()
    stale = asyncio.ensure_future(armed._await_public_url(proc))
    await asyncio.sleep(0)

    armed._spawn_spec(target_port=5005, authtoken=None)

    # A waiter abandoned by _spawn_spec must not still be parked on it.
    await asyncio.wait_for(stale, timeout=0.2)


# ---------------------------------------------------------------------------
# Module hygiene
# ---------------------------------------------------------------------------


def test_module_import_does_not_touch_the_network_or_spawn_anything(monkeypatch):
    """Importing the bridge must stay side-effect free: no cloudflared
    lookup at import time (binary resolution is lazy, per start())."""
    monkeypatch.setattr(sys, "argv", ["pytest"])
    import importlib

    import mnemos.tunnels.cloudflare_bridge as module

    reloaded = importlib.reload(module)
    assert reloaded.CloudflareBridge.name == "cloudflare"
