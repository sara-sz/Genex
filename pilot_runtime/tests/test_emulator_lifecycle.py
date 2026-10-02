"""The emulator harness must not leak a JVM. Structural guard.

INFRASTRUCTURE, NOT PRODUCT BEHAVIOUR. Nothing here tests the pilot; it tests
the test harness, which had a defect worth a permanent guard.

## The defect

`gcloud emulators firestore start` is a SHELL WRAPPER that exec-spawns a child
JVM, and the JVM is the process that holds the emulator port.
`subprocess.Popen.terminate()` signals only the wrapper, so every pytest
session orphaned one JVM. The wrapper exited cleanly, `returncode` looked
healthy, and the leak was invisible in every signal the harness reported.

One leak per run is unnoticeable. It accumulated across 0.3, 0.4A-0.4F/G and
0.5A to **98 orphaned JVMs holding 2.1 GB of RSS and 392 listening sockets**,
with the machine 9.1 GB into a 10.2 GB swap — the most likely cause of a
mid-session `AbortError: Stream closed` during 0.5A. A 54-mutation sweep runs
the integration suite 55 times, which is what turned a slow drip into a
session-killer.

## Why this is a source-level guard

The runtime proof is the teardown check in `conftest.py` itself: it polls the
port after killing the group and RAISES if anything still answers. That fires
only when the emulator actually runs, so it cannot protect the fast unit lane
or a branch where the emulator is unavailable.

These assertions are cheap, need no emulator, and fail the moment someone
reverts to `process.terminate()` — which is exactly the edit that would
reintroduce a defect nobody would notice for another six slices.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

CONFTEST = (pathlib.Path(__file__).parent / "integration" / "conftest.py")


@pytest.fixture(scope="module")
def source() -> str:
    return CONFTEST.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tree(source: str) -> ast.Module:
    return ast.parse(source)


def _call_names(tree: ast.Module) -> set:
    """Every `foo(...)` and `a.b(...)` callee name in the module."""
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
        elif isinstance(node.func, ast.Name):
            names.add(node.func.id)
    return names


def test_the_emulator_starts_in_its_own_process_group(tree: ast.Module) -> None:
    """`start_new_session=True` is what makes group teardown possible.

    Without it the wrapper shares pytest's process group, and `killpg` would
    signal the test runner itself rather than the emulator.
    """
    popens = [node for node in ast.walk(tree)
              if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)
              and node.func.attr == "Popen"]
    assert popens, "the emulator is no longer started with subprocess.Popen"

    for call in popens:
        keywords = {kw.arg: kw.value for kw in call.keywords}
        assert "start_new_session" in keywords, (
            "Popen must pass start_new_session=True so the emulator wrapper "
            "leads its own process group")
        value = keywords["start_new_session"]
        assert isinstance(value, ast.Constant) and value.value is True, (
            "start_new_session must be literally True")


def test_teardown_signals_the_group_and_escalates(source: str,
                                                  tree: ast.Module) -> None:
    """The whole tree is signalled, SIGTERM then SIGKILL."""
    assert "os.killpg" in source, (
        "teardown must signal the process GROUP; killing the wrapper alone "
        "orphans the JVM that holds the port")
    assert "killpg" in _call_names(tree)
    assert "SIGTERM" in source, "a clean shutdown must be attempted first"
    assert "SIGKILL" in source, (
        "teardown must escalate; an emulator that ignores SIGTERM would "
        "otherwise survive the run")


def test_no_bare_terminate_survives_anywhere(tree: ast.Module) -> None:
    """Not one `process.terminate()` remains — including the startup path.

    The startup-timeout branch is the most likely case to have a half-started
    JVM behind the wrapper, so it is the last place that may settle for
    `terminate()`. This asserts over the WHOLE module rather than the teardown
    function, because the leak was reintroducible from either end.
    """
    offenders = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)
                 and node.func.attr in {"terminate", "kill"}]
    assert not offenders, (
        "found Popen.terminate()/kill() — every teardown path must go through "
        "the process-group helper instead")


def test_teardown_verifies_the_port_was_released(source: str,
                                                 tree: ast.Module) -> None:
    """A released port is the only observable proof the JVM is gone.

    A surviving JVM cannot be seen in `returncode` — the wrapper exits 0 while
    its child keeps running — but it cannot hide the socket it holds.
    """
    assert "_port_released" in _call_names(tree), (
        "teardown must check that the emulator port was actually released")
    assert "still listening" in source, (
        "the failure must name the leak explicitly so the next person does "
        "not have to rediscover what an occupied port means")


def test_the_helpers_tolerate_an_already_dead_process(source: str) -> None:
    """Teardown must not raise because the thing it is killing already exited.

    A `ProcessLookupError` escaping here would turn a clean run into a teardown
    error, which is how a leak guard earns a reputation for flakiness and gets
    deleted.
    """
    assert "ProcessLookupError" in source
