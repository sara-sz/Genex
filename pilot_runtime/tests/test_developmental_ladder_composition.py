"""P0-B2a — the PRODUCTION composition root carries the ladder.

The 0.6A-2 lesson: route and emulator suites build their own application, so
only a test of `build_runtime` itself proves the served image is wired. Lives
apart from the artifact suite because `pilot_runtime.composition` imports
`firebase_admin` at module scope (integration job only).
"""

from __future__ import annotations

DEV_ENV = {"PILOT_ENVIRONMENT": "dev", "PILOT_DEV_AUTH_ENABLED": "true"}


def test_the_built_runtime_composes_the_ladder():
    """The 0.6A-2 lesson: prove the PRODUCTION composition, not a test app."""
    from pilot_runtime.composition import build_runtime

    runtime = build_runtime(dict(DEV_ENV), in_memory=True)
    assert runtime.developmental_ladder is not None
    assert runtime.application._developmental_ladder is \
        runtime.developmental_ladder
    assert runtime.developmental_ladder.ladder_ref.startswith("ldr1:")


def test_the_composed_ladder_matches_the_composed_bank():
    from pilot_runtime.composition import build_runtime

    runtime = build_runtime(dict(DEV_ENV), in_memory=True)
    ladder, bank = runtime.developmental_ladder, runtime.activity_bank
    for node in ladder.nodes:
        if node.pool_family_ref:
            assert set(node.pool_template_ids) == {
                t.activity_template_id for t in
                bank.templates_for_families([node.pool_family_ref])}
