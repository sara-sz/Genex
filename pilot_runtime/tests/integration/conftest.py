"""Firestore emulator fixture — identical locally and in hosted CI.

## Why the fixture starts the emulator itself

The alternative was to wrap the pytest invocation in `firebase emulators:exec`,
which is the more common recipe. It was rejected because it needs Node and a
`firebase.json`, and — more importantly — it would mean the local command and
the CI command differ. A harness that is only ever exercised in CI is a
harness whose failures are discovered in CI.

Starting the emulator from a session fixture means `pytest
pilot_runtime/tests/integration` is the whole command, in both places, with
the only prerequisites being a JDK and the `cloud-firestore-emulator`
component.

## It fails; it never skips

If the emulator cannot be started, these tests error out. `pytest.skip` here
would turn "we never checked the real adapter" into a green run, which is the
one outcome this suite must not be able to produce.

## No credentials, ever

The emulator authenticates nothing and the client library skips credential
resolution entirely once `FIRESTORE_EMULATOR_HOST` is set. The project id is
a fictional `demo-` name, which Google tooling treats as offline-only, so
there is no real project this could accidentally reach.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from typing import Iterator

import pytest

#: `demo-` prefixed ids are reserved for offline emulator use and are not
#: resolvable real projects.
EMULATOR_PROJECT = "demo-genex-pilot"
_STARTUP_TIMEOUT_SECONDS = 90


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _responding(host_port: str) -> bool:
    host, _, port = host_port.partition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def emulator_host() -> Iterator[str]:
    """A running Firestore emulator; yields its host:port."""
    preset = (os.environ.get("FIRESTORE_EMULATOR_HOST") or "").strip()
    if preset:
        # Someone (a CI step, a developer) already started one.
        if not _responding(preset):
            raise RuntimeError(
                f"FIRESTORE_EMULATOR_HOST={preset} is set but nothing is listening")
        yield preset
        return

    if shutil.which("gcloud") is None:
        raise RuntimeError(
            "Firestore emulator integration tests require the gcloud CLI with the "
            "cloud-firestore-emulator component. These tests must not be skipped.")

    host_port = f"127.0.0.1:{_free_port()}"
    process = subprocess.Popen(
        ["gcloud", "emulators", "firestore", "start", f"--host-port={host_port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    deadline = time.time() + _STARTUP_TIMEOUT_SECONDS
    while time.time() < deadline:
        if process.poll() is not None:
            output = (process.stdout.read() if process.stdout else "")[-1500:]
            raise RuntimeError(f"Firestore emulator exited during startup:\n{output}")
        if _responding(host_port):
            break
        time.sleep(0.5)
    else:
        process.terminate()
        raise RuntimeError(
            f"Firestore emulator did not become ready within "
            f"{_STARTUP_TIMEOUT_SECONDS}s (needs a JDK on PATH)")

    # Deliberately NOT exported to the process environment. The emulator
    # endpoint is threaded to `build_firestore_client` as an argument instead.
    #
    # Setting it globally here leaked across the session: `integration/` sorts
    # before `test_composition.py`, so by the time the composition tests ran,
    # a production client could be constructed without credentials and the
    # "missing credentials are translated" test stopped failing for the right
    # reason. The same global is what the composition root refuses to start
    # with, so a test harness setting it was simulating the exact condition
    # production forbids.
    try:
        yield host_port
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture()
def firestore_client(emulator_host: str):
    """A real `google.cloud.firestore.Client` pointed at the emulator."""
    from pilot_runtime.persistence.firestore_store import build_firestore_client

    return build_firestore_client(project_id=EMULATOR_PROJECT,
                                  emulator_host=emulator_host)


@pytest.fixture()
def store(firestore_client):
    """The REAL adapter under test. Never a fake in this package."""
    from pilot_runtime.persistence.firestore_store import FirestoreDocumentStore

    return FirestoreDocumentStore(firestore_client)


@pytest.fixture()
def unique_suffix(request) -> str:
    """A per-test auth-subject suffix.

    The emulator database is shared across the session by design, so two
    topologies would otherwise bind two caregiver records to one auth subject
    — which the resolver now refuses as a data-integrity fault. The emulator
    found that defect precisely because it does not reset; keeping it shared
    and making subjects unique preserves that property instead of hiding it.
    """
    return "-" + request.node.name.replace("_", "-")[:60]


@pytest.fixture()
def topology(repos, unique_suffix):
    """The two-family topology, with subjects unique to this test."""
    from datetime import datetime, timezone

    from pilot_backend.fixtures.secure_topology import build_secure_topology

    return build_secure_topology(
        repos, now=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
        subject_suffix=unique_suffix)


@pytest.fixture()
def repos(store):
    """Repositories over the real Firestore adapter.

    Each test gets a fresh collection namespace by virtue of fresh random
    application ids; the emulator's data is not reset between tests, which is
    deliberate — it means ordering and query assertions run against a store
    that already contains other records, the way production will.
    """
    from pilot_backend.persistence import FirestoreRepositories

    return FirestoreRepositories(store)
