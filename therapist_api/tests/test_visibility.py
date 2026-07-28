"""Derived activity-version visibility through the generic catalog (Phase 1B.2A.2).

A therapist-derived version must never reach the generic activity-template /
version-history endpoints just because it hangs off the same canonical
`activity_template_id`. Ownership is required for every save_scope, and
`child_only` additionally requires a live (active) connection to the child the
version was derived for.

All identities and children here are fictional.
"""

from __future__ import annotations

from app.fixtures import data as D
from tests.conftest import ELENA, HANNAH, PRIYA, UNCONNECTED, read_slice_client
from tests.test_modify_proposal import NOAH_AP, body

BUBBLES = D.T_BUBBLES
TEMPLATES = "/api/v1/activity-templates"
BUBBLES_DETAIL = f"{TEMPLATES}/{BUBBLES}"


def _c():
    return read_slice_client()


def _version_ids(client, headers, template_id=BUBBLES):
    """Version ids this principal can see for a template, via list AND detail.

    Both generic read paths must agree — a version hidden from one must be
    hidden from the other.
    """
    listed = client.get(TEMPLATES, headers=headers)
    detail = client.get(f"{TEMPLATES}/{template_id}", headers=headers)
    assert listed.status_code == 200 and detail.status_code == 200
    from_list = {
        v["activity_version_id"]
        for t in listed.json()["items"] if t["activity_template_id"] == template_id
        for v in t["versions"]
    }
    from_detail = {v["activity_version_id"] for v in detail.json()["versions"]}
    assert from_list == from_detail, (from_list, from_detail)
    return from_list


# ── 1. creating therapist reaches the version through authorized child routes ──
def test_creator_reaches_child_only_version_through_authorized_routes():
    c = _c()
    created = c.post(NOAH_AP, headers={**HANNAH, "Idempotency-Key": "vis-a"}, json=body()).json()
    proposal_id = created["proposal"]["proposal_id"]
    version_id = created["proposed_activity_version"]["id"]

    plan = c.get("/api/v1/children/child_noah/weekly-plan", headers=HANNAH).json()
    assignment = plan["assignments"][0]
    assert assignment["pending_proposal"]["proposed_activity_version_id"] == version_id
    # the ORIGINAL version stays current; the proposed one is never shown as active
    assert assignment["activity_version_id"] == "ver_turn_taking_v1"

    detail = c.get(f"/api/v1/children/child_noah/proposals/{proposal_id}", headers=HANNAH)
    assert detail.status_code == 200
    assert detail.json()["proposed_activity_version_id"] == version_id

    # ...and the generic catalog also shows it to its owner while the connection is active
    assert version_id in _version_ids(c, HANNAH, D.T_TURNTAKE)


# ── 2. unconnected therapist ─────────────────────────────────────────────────
def test_unconnected_therapist_cannot_see_child_only_version():
    c = _c()
    created = c.post(NOAH_AP, headers={**HANNAH, "Idempotency-Key": "vis-b"}, json=body()).json()
    version_id = created["proposed_activity_version"]["id"]
    assert version_id not in _version_ids(c, UNCONNECTED, D.T_TURNTAKE)
    assert D.V_BUBBLES_DERIVED not in _version_ids(c, UNCONNECTED)


# ── 3. therapist connected only to a different child ─────────────────────────
def test_therapist_of_another_child_cannot_see_child_only_version():
    c = _c()
    created = c.post(NOAH_AP, headers={**HANNAH, "Idempotency-Key": "vis-c"}, json=body()).json()
    version_id = created["proposed_activity_version"]["id"]
    # Priya has a live caseload (Theo) but no connection to Noah or Maya.
    assert c.get("/api/v1/children", headers=PRIYA).json()["total"] == 1
    assert version_id not in _version_ids(c, PRIYA, D.T_TURNTAKE)
    assert D.V_BUBBLES_DERIVED not in _version_ids(c, PRIYA)


# ── 4. non-active connections grant nothing, even to the owner ───────────────
def test_pending_paused_ended_connections_do_not_grant_visibility():
    visible = _version_ids(_c(), HANNAH)
    # Hannah OWNS all three, but her connection is pending / paused / ended.
    assert D.V_PENDING_HANNAH not in visible   # pending_parent_acceptance
    assert D.V_PAUSED_HANNAH not in visible    # paused_by_parent
    assert D.V_ENDED_HANNAH not in visible     # ended


def test_child_only_version_without_child_association_is_hidden():
    """No resolvable proposal/assignment -> hidden, even from its creator."""
    assert D.V_ORPHAN_HANNAH not in _version_ids(_c(), HANNAH)


# ── 5. parent principal ──────────────────────────────────────────────────────
def test_parent_forbidden_on_generic_template_routes():
    c = _c()
    assert c.get(TEMPLATES, headers=ELENA).status_code == 403
    assert c.get(BUBBLES_DETAIL, headers=ELENA).status_code == 403


# ── 6/7. owner-scoped library and review submissions ─────────────────────────
def test_therapist_library_version_is_owner_only():
    c = _c()
    assert D.V_LIB_HANNAH in _version_ids(c, HANNAH)
    assert D.V_LIB_HANNAH not in _version_ids(c, UNCONNECTED)
    assert D.V_LIB_HANNAH not in _version_ids(c, PRIYA)


def test_submitted_for_genex_review_version_is_submitter_only():
    """Submission metadata only — no Genex-review role or publication exists."""
    c = _c()
    assert D.V_REVIEW_HANNAH in _version_ids(c, HANNAH)
    assert D.V_REVIEW_HANNAH not in _version_ids(c, UNCONNECTED)
    assert D.V_REVIEW_HANNAH not in _version_ids(c, PRIYA)


def test_save_scope_is_honoured_at_creation_time():
    """A version created with each scope stays scoped to its creator."""
    for scope, key in (("therapist_library", "sc-lib"), ("submitted_for_genex_review", "sc-rev")):
        c = _c()
        created = c.post(NOAH_AP, headers={**HANNAH, "Idempotency-Key": key},
                         json={**body(), "save_scope": scope}).json()
        version_id = created["proposed_activity_version"]["id"]
        assert created["proposed_activity_version"]["save_scope"] == scope
        assert version_id in _version_ids(c, HANNAH, D.T_TURNTAKE)
        assert version_id not in _version_ids(c, UNCONNECTED, D.T_TURNTAKE)
        assert version_id not in _version_ids(c, PRIYA, D.T_TURNTAKE)


# ── 8. no scope is globally visible ──────────────────────────────────────────
def test_no_derived_scope_is_globally_visible():
    """No derived version is visible to every therapist principal."""
    c = _c()
    hannah = _version_ids(c, HANNAH)
    others = _version_ids(c, UNCONNECTED) | _version_ids(c, PRIYA)
    derived_owned_by_hannah = {
        D.V_BUBBLES_DERIVED, D.V_LIB_HANNAH, D.V_REVIEW_HANNAH,
        D.V_ORPHAN_HANNAH, D.V_PAUSED_HANNAH, D.V_PENDING_HANNAH, D.V_ENDED_HANNAH,
    }
    assert others & derived_owned_by_hannah == set()
    # Hannah still sees exactly the ones she is entitled to.
    assert hannah & derived_owned_by_hannah == {
        D.V_BUBBLES_DERIVED, D.V_LIB_HANNAH, D.V_REVIEW_HANNAH,
    }


# ── 9. canonical templates and versions are unaffected ───────────────────────
def test_canonical_versions_remain_visible_to_every_therapist():
    c = _c()
    for headers in (HANNAH, UNCONNECTED, PRIYA):
        assert D.V_BUBBLES in _version_ids(c, headers)
        assert D.V_TURNTAKE in _version_ids(c, headers, D.T_TURNTAKE)
    # the canonical templates themselves are untouched
    listed = c.get(TEMPLATES, headers=UNCONNECTED).json()
    assert {t["activity_template_id"] for t in listed["items"]} == {D.T_BUBBLES, D.T_TURNTAKE}
    bubbles = c.get(BUBBLES_DETAIL, headers=UNCONNECTED).json()
    assert bubbles["title"] == "Bubble requesting" and bubbles["immutable"] is True


# ── 10. filtering does not disclose that a hidden version exists ─────────────
def test_filtering_does_not_reveal_hidden_versions():
    c = _c()
    created = c.post(NOAH_AP, headers={**HANNAH, "Idempotency-Key": "vis-d"}, json=body()).json()
    version_id = created["proposed_activity_version"]["id"]

    hidden = c.get(BUBBLES_DETAIL, headers=UNCONNECTED).json()
    # No count, placeholder, id or title hints at the filtered rows.
    assert hidden["versions"] == [{"activity_version_id": D.V_BUBBLES, "title": "Bubble requesting",
                                  "is_derived": False, "created_by_type": "genex",
                                  "original_activity_version_id": None}]
    body_text = c.get(BUBBLES_DETAIL, headers=UNCONNECTED).text
    for hidden_id in (D.V_BUBBLES_DERIVED, D.V_LIB_HANNAH, D.V_REVIEW_HANNAH, D.V_ORPHAN_HANNAH):
        assert hidden_id not in body_text
    assert version_id not in c.get(f"{TEMPLATES}/{D.T_TURNTAKE}", headers=UNCONNECTED).text

    # An unknown template id and a template whose derived rows are all hidden
    # behave identically apart from the canonical rows themselves.
    assert c.get(f"{TEMPLATES}/tmpl_does_not_exist", headers=UNCONNECTED).status_code == 404
