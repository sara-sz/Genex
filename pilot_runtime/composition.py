"""pilot_runtime/composition.py — the application composition root.

The one place that decides which real thing is plugged into which port. Every
other module receives its dependencies and cannot choose them, which is what
makes the production rules below enforceable in one reviewable file.

    configuration
      -> logging policy
      -> Firebase Admin TokenDecoder
      -> authentication verifier
      -> identity resolution (already inside authz)
      -> authorization
      -> Firestore DocumentStore
      -> repositories
      -> audit recorder
      -> revision/history (via repositories)
      -> HTTP transport

## Production cannot be assembled out of test parts

`build_runtime` refuses, in production, to construct:

    fake or dev authentication    - `PilotSettings` already refuses the flag;
                                    re-checked here so a hand-built settings
                                    object cannot route around it
    in-memory persistence         - explicitly rejected, not merely "not the
                                    default"
    an emulator                   - both the configured host AND the ambient
                                    FIRESTORE_EMULATOR_HOST are refused
    a dev-looking project         - `PilotSettings` rejects dev markers in
                                    prod project ids
    Parent 2.3 storage            - no GCS client exists here, and the
                                    settings layer refuses those names in
                                    every environment

The ambient environment variable check matters more than it looks. The
Firestore client library reads `FIRESTORE_EMULATOR_HOST` on its own, with no
involvement from this code — so a production process that happened to inherit
it would silently talk to an emulator and every write would appear to succeed
while landing nowhere. Configuration alone cannot catch that; the process
environment has to be inspected.

## Dev and test may use fakes, but only when asked

Nothing is implicit. A dev runtime gets an in-memory store only if it
explicitly asks for one, and an emulator only if a host is configured. There
is no "if we can't reach Firestore, fall back" path in either direction.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from pilot_backend.apisurface.surface import cors_policy_for
from pilot_backend.audit.recorder import AuditRecorder
from pilot_backend.auth.verifiers import build_verifier
from pilot_backend.config import ConfigError, Environment, PilotSettings
from pilot_backend.persistence import FakeDocumentStore, FirestoreRepositories
from pilot_backend.transport import build_application

from .auth.firebase_decoder import FirebaseTokenDecoder, initialize_firebase_app
from .persistence.firestore_store import (
    EMULATOR_ENV_VAR,
    FirestoreDocumentStore,
    build_firestore_client,
)
from .workflows.child_context import ChildContextService


class CompositionError(Exception):
    """The runtime could not be assembled safely for this environment.

    PHI-safe: names the environment and the missing or forbidden binding.
    """

    PHI_SAFE_MESSAGE = True


@dataclass(frozen=True)
class PilotRuntime:
    """Everything a running pilot service needs, already wired."""

    settings: PilotSettings
    store: Any
    repos: FirestoreRepositories
    verifier: Any
    recorder: AuditRecorder
    child_context: ChildContextService
    application: Any
    cors: Any

    @property
    def is_production(self) -> bool:
        return self.settings.environment.is_prod


def _assert_no_ambient_emulator(env: Mapping[str, str]) -> None:
    """Production must not inherit an emulator endpoint from its environment."""
    if (env.get(EMULATOR_ENV_VAR) or "").strip():
        raise CompositionError(
            f"prod refuses to start with {EMULATOR_ENV_VAR} set in the environment")


def build_store(settings: PilotSettings, *, process_env: Optional[Mapping[str, str]] = None,
                in_memory: bool = False, firestore_client: Any = None):
    """Select the persistence binding for this environment.

    Returns a `DocumentStore`. Production always returns a Firestore-backed
    one; there is no branch in which it returns anything else.
    """
    env = process_env if process_env is not None else os.environ

    if settings.environment.is_prod:
        if in_memory:
            raise CompositionError("prod must not use in-memory persistence")
        if settings.firestore_emulator_host.strip():
            raise CompositionError("prod must not use a Firestore emulator")
        _assert_no_ambient_emulator(env)
        if not settings.gcp_project_id.strip():
            raise CompositionError("prod requires an explicit Firestore project id")
        client = firestore_client or build_firestore_client(
            project_id=settings.gcp_project_id,
            database=settings.firestore_database)
        return FirestoreDocumentStore(client)

    # dev / test — fakes and emulators allowed, but only when asked for.
    if in_memory:
        return FakeDocumentStore()
    client = firestore_client or build_firestore_client(
        project_id=settings.gcp_project_id or "pilot-dev",
        database=settings.firestore_database,
        emulator_host=settings.firestore_emulator_host)
    return FirestoreDocumentStore(client)


def build_token_decoder(settings: PilotSettings, *, credential: Any = None,
                        decoder: Any = None):
    """Select the authentication binding.

    A supplied `decoder` is honoured only outside production — that is the
    seam the fictional end-to-end test uses, and production must not have it.
    """
    if decoder is not None:
        if settings.environment.is_prod:
            raise CompositionError("prod must not use an injected token decoder")
        return decoder

    if not settings.firebase_project_id.strip():
        if settings.environment.is_prod:
            raise CompositionError("prod requires an explicit Firebase project id")
        return None  # dev/test without Firebase: verifier will fail closed

    app = initialize_firebase_app(
        project_id=settings.firebase_project_id, credential=credential)
    return FirebaseTokenDecoder(app)


def build_runtime(env: Mapping[str, str], *,
                  process_env: Optional[Mapping[str, str]] = None,
                  in_memory: bool = False,
                  firestore_client: Any = None,
                  decoder: Any = None,
                  credential: Any = None,
                  log_sink: Optional[list] = None) -> PilotRuntime:
    """Assemble the runtime from an explicit environment mapping.

    Raises `ConfigError` or `CompositionError` rather than starting degraded.
    """
    settings = PilotSettings.from_env(env)
    process = process_env if process_env is not None else os.environ

    # EVERY production prohibition is evaluated here, before anything is
    # constructed. Ordering is load-bearing and was got wrong first: the
    # injected-decoder prohibition originally lived inside
    # `build_token_decoder`, which runs after `build_store` — so on a machine
    # without ambient credentials, store construction failed first and the
    # SECURITY check was never reached. A prohibition that only fires when
    # the infrastructure happens to succeed is not a prohibition.
    if settings.environment.is_prod:
        # Re-assert the dev-auth prohibition. `PilotSettings` already refuses
        # it, so this only fires for a hand-built object — exactly the case a
        # single check would miss.
        if settings.dev_auth_enabled:
            raise CompositionError("prod must not enable dev auth")
        if in_memory:
            raise CompositionError("prod must not use in-memory persistence")
        if decoder is not None:
            raise CompositionError("prod must not use an injected token decoder")
        if settings.firestore_emulator_host.strip():
            raise CompositionError("prod must not use a Firestore emulator")
        _assert_no_ambient_emulator(process)
        if not settings.firebase_project_id.strip():
            raise CompositionError("prod requires an explicit Firebase project id")
        if not settings.gcp_project_id.strip():
            raise CompositionError("prod requires an explicit Firestore project id")

    store = build_store(settings, process_env=process, in_memory=in_memory,
                        firestore_client=firestore_client)
    repos = FirestoreRepositories(store)

    token_decoder = build_token_decoder(settings, credential=credential, decoder=decoder)
    verifier = build_verifier(settings, decoder=token_decoder)

    recorder = AuditRecorder(repos.audit_events, environment=settings.environment.value)
    child_context = ChildContextService(verifier=verifier, repos=repos, recorder=recorder)

    application = build_application(settings=settings, repos=repos, verifier=verifier,
                                    recorder=recorder, log_sink=log_sink)

    return PilotRuntime(
        settings=settings, store=store, repos=repos, verifier=verifier,
        recorder=recorder, child_context=child_context,
        application=application, cors=cors_policy_for(settings),
    )
