"""Track 2 — the clinical-review ledger, packet and promotion safeguards.

Lives under `pilot_runtime/tests/` rather than beside the tools because this is
where the pilot's CI already runs suites, and because the no-mirror gate exempts
test paths — a test may legitimately name canonical families, which the tools
themselves must not.

## WHAT IS ACTUALLY BEING PROVEN

Three claims, each with a negative control, because every one of them is a
claim that something CANNOT happen:

1. an unapproved card cannot be promoted — not by default, not by a status
   that merely looks approving, not by an approval missing a reviewer, and not
   by an approval recorded against a different family;
2. editing an approved card invalidates its approval, with no transfer step;
3. a review packet cannot be written under `pilot_backend/` or
   `pilot_runtime/`, where milestone prose is forbidden and container builds
   would carry it.

Plus the migration register's two rules — never grows, never doubles as an
approval — and the state assertion that NOTHING has been promoted yet.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clinical_review.tools import ledger as L
from clinical_review.tools import build_review_packet as PACKET
from clinical_review.tools import promote_approved as PROMOTE

FIRST_WORDS = "expressive_first_words"
PRE_VERBAL = "early_vocalizations"
ACTION_PICTURE = "action_picture_labeling"

#: Candidate counts, each DERIVED and recorded so a later edit that pads or
#: thins a family has to say so here. `early_vocalizations` is 6 rather than 7
#: because two proposed concepts — routine-embedded cooing and routine-embedded
#: babble — proved to be one activity at different rungs.
EXPECTED_COUNTS = {FIRST_WORDS: 7, PRE_VERBAL: 6, ACTION_PICTURE: 5}

#: The migration baseline: the 18 cards admitted under `reviewed_cards/` before
#: this ledger existed. Pinned so `assert_register_never_grew` has something to
#: compare against that a mutable file cannot quietly redefine. CI pins the
#: same list; see `.github/workflows/parent-2.4-ci.yml`.
MIGRATION_BASELINE = (
    "atpl1:01cdd9f34e37a62bed4feda4300902a7",
    "atpl1:041794bbb1acb1f86a27ca9460088c2d",
    "atpl1:06a431b9e9a8e118a900b7f407fecc02",
    "atpl1:1081de288d8fc18f41225d80bb291a63",
    "atpl1:1226e454bd64d869206574bb6ad9851f",
    "atpl1:166c4e5492dab00d41b9fb4d5162202b",
    "atpl1:2c317105a4151f55be1d2122a16fc3fc",
    "atpl1:3550b9ec30305b58d9cbbaa24c58e5b2",
    "atpl1:5f44b9eba107589ca26c6610f2c6cb05",
    "atpl1:797708a5514fd175e24bf0bac3f2a8ff",
    "atpl1:9b0facf380b755262eadbf9d76820e7a",
    "atpl1:9b9eb883a67400556702486fc177e75b",
    "atpl1:9e165d6d242e3e887ddb32f6d7c48486",
    "atpl1:bd6a98e7eef7d1a01c1e48baa8230317",
    "atpl1:c694ec43189c20633adeea5502833f6b",
    "atpl1:e22a325977fbaaeca903a6ea909d17d0",
    "atpl1:f7a99d017c398560058379637732ac58",
    "atpl1:f83b5f3cd8ef75cdba82700d6b389be7",
)


@pytest.fixture()
def real_ledger():
    return L.read_ledger()


# ---------------------------------------------------------------------------
# 1. the candidate set
# ---------------------------------------------------------------------------


def test_all_three_in_scope_families_have_candidates():
    assert set(L.candidate_families()) == set(EXPECTED_COUNTS)


def test_candidate_counts_are_the_derived_ones():
    for family, expected in EXPECTED_COUNTS.items():
        cards = L.read_cards(L.CANDIDATES_DIR / f"{family}.json")
        assert len(cards) == expected, (family, len(cards))


def test_every_candidate_is_in_the_ledger_as_a_candidate(real_ledger):
    """No candidate starts life in any state other than generated_candidate."""
    for family in L.candidate_families():
        for card in L.read_cards(L.CANDIDATES_DIR / f"{family}.json"):
            tid = L.compute_card_id(family, card)
            entry = real_ledger["entries"].get(tid)
            assert entry is not None, (family, card["title"], tid)
            assert entry["status"] == L.STATUS_GENERATED_CANDIDATE, tid
            assert entry["activity_family_ref"] == family, tid
            assert entry["reviewer"] is None, tid
            assert entry["decided_on"] is None, tid


def test_candidate_cards_pass_the_real_parent_validator():
    """The validator is the gate approval does NOT bypass, so candidates are
    held to it now — a card Hannah approves that cannot build is wasted review.
    """
    import sys

    root = str(L.REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    parent = str(L.REPO_ROOT / "genex-parent")
    if parent not in sys.path:
        sys.path.insert(0, parent)

    from genex_core.activity_validator import validate_activity
    from pilot_runtime.integration.activity_bank_generator import (
        DOMAIN_KEY,
        _assembled_for_validation,
    )

    for family in L.candidate_families():
        for card in L.read_cards(L.CANDIDATES_DIR / f"{family}.json"):
            ok, problems = validate_activity(
                _assembled_for_validation(card, family), DOMAIN_KEY)
            assert ok, (family, card["title"], problems)


def test_each_family_has_enough_distinct_content_for_a_three_activity_week():
    """Capacity 3 with headroom. Counts are derived, not padded to a number."""
    for family in L.candidate_families():
        cards = L.read_cards(L.CANDIDATES_DIR / f"{family}.json")
        assert len(cards) >= 4, (family, len(cards))
        titles = [c["title"] for c in cards]
        assert len(set(titles)) == len(titles), family
        ids = [L.compute_card_id(family, c) for c in cards]
        assert len(set(ids)) == len(ids), family


# ---------------------------------------------------------------------------
# 2. nothing is promoted, and nothing can be
# ---------------------------------------------------------------------------


def test_nothing_has_been_promoted():
    """The state assertion the founder asked to be able to check.

    No Track 2 family has a reviewed-cards file, and the served bank is
    unchanged from the pre-Track-2 three families.
    """
    for family in EXPECTED_COUNTS:
        assert not (L.REVIEWED_CARDS_DIR / f"{family}.json").exists(), family
    present = sorted(p.stem for p in L.REVIEWED_CARDS_DIR.glob("*.json"))
    assert present == ["book_object_naming", "sentence_building",
                       "two_word_phrases"], present


def test_promotion_plan_is_empty_while_nothing_is_approved():
    promotable, skipped, refusals = PROMOTE.plan()
    assert promotable == {}
    assert refusals == []
    assert len(skipped) == sum(EXPECTED_COUNTS.values()), skipped
    assert all("status generated_candidate" in line for line in skipped)


def test_dry_run_is_the_default_and_writes_nothing(tmp_path):
    counts = PROMOTE.promote(reviewed_dir=tmp_path)
    assert counts == {}
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# 3. NEGATIVE CONTROLS — the refusals must actually refuse
# ---------------------------------------------------------------------------


def _ledger_with(entries, register=()):
    return {"ledger_version": L.LEDGER_VERSION,
            "entries": entries, "legacy_unresolved": list(register)}


def _first_candidate(family=FIRST_WORDS):
    card = L.read_cards(L.CANDIDATES_DIR / f"{family}.json")[0]
    return card, L.compute_card_id(family, card)


def test_an_approval_without_a_reviewer_is_refused_at_read_time():
    card, tid = _first_candidate()
    body = _ledger_with({tid: {
        "activity_family_ref": FIRST_WORDS,
        "status": L.STATUS_CLINICIAN_APPROVED,
        "reviewer": None, "decided_on": "2026-10-20",
        "note": "", "supersedes": None, "adapted_from": None}})
    path = Path(L.REPO_ROOT) / "clinical_review" / "out" / "_control.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    try:
        with pytest.raises(L.LedgerError, match="no reviewer"):
            L.read_ledger(path)
    finally:
        path.unlink()


def test_an_unknown_status_is_refused(tmp_path):
    card, tid = _first_candidate()
    body = _ledger_with({tid: {
        "activity_family_ref": FIRST_WORDS, "status": "looks_fine",
        "reviewer": "H", "decided_on": "2026-10-20",
        "note": "", "supersedes": None, "adapted_from": None}})
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(L.LedgerError, match="not one of"):
        L.read_ledger(path)


def test_an_edit_invalidates_the_approval():
    """THE CENTRAL GUARANTEE.

    Approve a card, change one word, and the approved id no longer describes
    any card on disk — so the edited wording is unapproved and unpromotable.
    There is no transfer step to get it wrong.
    """
    card, approved_id = _first_candidate()
    edited = dict(card)
    edited["instructions"] = card["instructions"].replace(
        "Wait five seconds", "Wait ten seconds")
    assert edited["instructions"] != card["instructions"]

    edited_id = L.compute_card_id(FIRST_WORDS, edited)
    assert edited_id != approved_id

    approved = frozenset({approved_id})
    assert edited_id not in approved


def test_an_approval_for_the_wrong_family_cannot_promote(tmp_path):
    """An approval is for a card IN A FAMILY; the family is part of the id."""
    card, _ = _first_candidate()
    wrong_family_id = L.compute_card_id(PRE_VERBAL, card)
    right_family_id = L.compute_card_id(FIRST_WORDS, card)
    assert wrong_family_id != right_family_id

    ledger = _ledger_with({wrong_family_id: {
        "activity_family_ref": PRE_VERBAL,
        "status": L.STATUS_CLINICIAN_APPROVED,
        "reviewer": "H", "decided_on": "2026-10-20",
        "note": "", "supersedes": None, "adapted_from": None}})
    problems = L.check_reviewed_cards(ledger, reviewed_dir=tmp_path)
    assert problems == []          # nothing admitted yet

    (tmp_path / f"{FIRST_WORDS}.json").write_text(
        json.dumps([card], indent=2), encoding="utf-8")
    problems = L.check_reviewed_cards(ledger, reviewed_dir=tmp_path)
    assert len(problems) == 1
    assert "no clinician_approved ledger entry" in problems[0]


def test_an_admitted_card_with_no_approval_is_caught(tmp_path):
    card, _ = _first_candidate()
    (tmp_path / f"{FIRST_WORDS}.json").write_text(
        json.dumps([card], indent=2), encoding="utf-8")
    problems = L.check_reviewed_cards(_ledger_with({}), reviewed_dir=tmp_path)
    assert len(problems) == 1
    assert "is admitted but has no" in problems[0]


def test_guarded_milestone_prose_cannot_be_promoted():
    """Legal in clinical_review/, forbidden once under pilot_runtime/."""
    card, _ = _first_candidate()
    sneaky = dict(card)
    sneaky["instructions"] = card["instructions"] + " It calls parents mama or dada."
    problems = PROMOTE._no_mirror_problems(sneaky)
    assert problems and "guarded milestone prose" in problems[0]

    with_ref = dict(card)
    with_ref["theme"] = "rung1:" + "a" * 32
    problems = PROMOTE._no_mirror_problems(with_ref)
    assert problems and "concrete canonical ref" in problems[0]

    # ...and the real candidates are clean, or the control above is vacuous.
    for family in L.candidate_families():
        for real in L.read_cards(L.CANDIDATES_DIR / f"{family}.json"):
            assert PROMOTE._no_mirror_problems(real) == [], real["title"]


# ---------------------------------------------------------------------------
# 4. the migration register
# ---------------------------------------------------------------------------


def test_the_register_is_exactly_the_eighteen_legacy_cards(real_ledger):
    assert sorted(real_ledger["legacy_unresolved"]) == sorted(MIGRATION_BASELINE)


def test_the_register_matches_what_is_actually_admitted(real_ledger):
    """The register must describe reality, not a stale snapshot."""
    actual = set()
    for path in sorted(L.REVIEWED_CARDS_DIR.glob("*.json")):
        for card in L.read_cards(path):
            actual.add(L.compute_card_id(path.stem, card))
    assert actual == set(real_ledger["legacy_unresolved"])


def test_the_register_may_shrink_but_not_grow():
    shrunk = _ledger_with({}, register=MIGRATION_BASELINE[:-1])
    L.assert_register_never_grew(shrunk, MIGRATION_BASELINE)   # must not raise

    grown = _ledger_with({}, register=MIGRATION_BASELINE + ("atpl1:" + "b" * 32,))
    with pytest.raises(L.LedgerError, match="grew beyond"):
        L.assert_register_never_grew(grown, MIGRATION_BASELINE)


def test_the_register_cannot_double_as_an_approval(tmp_path):
    """A card may not be both provenance-unresolved and approved.

    Without this, a legacy id could acquire an approval while still sitting in
    the register — a retroactive blessing wearing migration clothes.
    """
    card, tid = _first_candidate()
    body = _ledger_with({tid: {
        "activity_family_ref": FIRST_WORDS,
        "status": L.STATUS_CLINICIAN_APPROVED,
        "reviewer": "H", "decided_on": "2026-10-20",
        "note": "", "supersedes": None, "adapted_from": None}},
        register=(tid,))
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(L.LedgerError, match="both legacy_unresolved and"):
        L.read_ledger(path)


def test_no_legacy_card_claims_clinician_approval(real_ledger):
    """Decision 6, asserted: the 18 legacy cards have no approval entries."""
    approved = L.approved_ids(real_ledger)
    assert approved == frozenset(), approved
    assert not (set(MIGRATION_BASELINE) & set(real_ledger["entries"]))


def test_the_live_bank_passes_the_ledger_gate(real_ledger):
    assert L.check_reviewed_cards(real_ledger) == []
    L.assert_register_never_grew(real_ledger, MIGRATION_BASELINE)


# ---------------------------------------------------------------------------
# 5. the packet cannot leak into a guarded tree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    "pilot_runtime/data/packets",
    "pilot_backend/anything",
    "clinical_review/../pilot_runtime/sneaky",
])
def test_packet_refuses_guarded_output_paths(bad):
    with pytest.raises(L.LedgerError, match="refusing to write"):
        L.assert_outside_guarded_trees(L.REPO_ROOT / bad)


def test_packet_allows_the_gitignored_out_dir():
    resolved = L.assert_outside_guarded_trees(L.OUT_DIR)
    assert resolved == L.OUT_DIR.resolve()


def test_packet_renders_every_candidate_with_its_id(tmp_path):
    """Card text is HTML-ESCAPED, so compare against the escaped form.

    `Who's Here` renders as `Who&#x27;s Here`. Asserting on the raw string
    would pass only for cards that happen to contain no apostrophe, which is
    the kind of test that quietly stops checking the interesting cards.
    """
    import html as _html

    written = PACKET.build(out_dir=tmp_path)
    assert len(written) == len(EXPECTED_COUNTS)
    for path in written:
        family = path.name.replace("_review_packet.html", "")
        page = path.read_text(encoding="utf-8")
        cards = L.read_cards(L.CANDIDATES_DIR / f"{family}.json")
        for card in cards:
            assert L.compute_card_id(family, card) in page
            assert _html.escape(card["title"]) in page
            for field in ("instructions", "success_criteria", "what_to_avoid"):
                assert _html.escape(card[field].split(".")[0][:40]) in page
        for decision in ("Approve", "Approve with edits", "Reject"):
            assert decision in page
        assert "generated candidate" in page


def test_packet_states_the_family_span_review_requirement(tmp_path):
    """Founder decision: the span requirement must be unmissable.

    A family spanning several rungs serves them all from ONE pool, so a card
    that is only right at its anchor is not safe to approve. The packet has to
    ask for confirmation across the FULL span, and name the span in months.
    """
    PACKET.build(out_dir=tmp_path, families=[PRE_VERBAL])
    page = (tmp_path / f"{PRE_VERBAL}_review_packet.html").read_text()
    assert "Review requirement for this family" in page
    assert "6 canonical rungs, spanning 2&ndash;9 months" in page
    assert "across the full span" in page
    assert "not merely at its nominal anchor" in page
    assert "selects content by <strong>family</strong>, never by" in page
    # ...and it says what to do about a card that fails the requirement.
    assert "rejected or rewritten" in page

    rungs = PACKET.rungs_for_family(PRE_VERBAL)
    assert len(rungs) == 6, rungs
    assert sum(1 for r in rungs if r["reachable_from"]) == 3, rungs


def test_single_rung_family_gets_no_span_requirement(tmp_path):
    """Non-vacuity for the block above: it must not appear where it is false.

    `action_picture_labeling` binds exactly one rung, so there is no span to
    confirm and the requirement would be noise.
    """
    PACKET.build(out_dir=tmp_path, families=[ACTION_PICTURE])
    page = (tmp_path / f"{ACTION_PICTURE}_review_packet.html").read_text()
    assert "Review requirement for this family" not in page
    rungs = PACKET.rungs_for_family(ACTION_PICTURE)
    assert len(rungs) == 1, rungs
    assert rungs[0]["months"] == 36


def test_action_picture_cards_avoid_the_blocked_motor_verbs():
    """The constraint is a MACHINE rule, so it is asserted as one.

    `_MOTOR_GAME_PATTERN` makes these critical failures in a language card's
    instructions, and the one rejected card in the live bank
    (`_BUCKET_VARIANTS[expressive_word][7]`) is an action-labeling card that
    failed exactly this way. If Hannah needs a blocked verb, that is a separate
    decision — it must not be resolved by weakening the validator.
    """
    import re

    blocked = re.compile(
        r"\b(jump|hop|toss|throw|catch|race|climb|trampoline"
        r"|obstacle course|balance beam)\b", re.I)
    required = re.compile(
        r"\b(action|picture|doing|running|eating|playing|point|show|act)\b",
        re.I)
    for card in L.read_cards(L.CANDIDATES_DIR / f"{ACTION_PICTURE}.json"):
        assert not blocked.search(card["instructions"]), card["title"]
        blob = " ".join(card[f] for f in L.card_fields())
        # The validator's `action_label` bucket guardrail.
        assert required.search(blob), card["title"]


def test_action_picture_support_gradient_is_recorded_for_review(real_ledger):
    """All five steps of the gradient are stated in the ledger, in order."""
    notes = []
    for card in L.read_cards(L.CANDIDATES_DIR / f"{ACTION_PICTURE}.json"):
        entry = real_ledger["entries"][L.compute_card_id(ACTION_PICTURE, card)]
        notes.append(entry["note"])
    for step in range(1, 6):
        assert any(f"step {step} of 5" in note for note in notes), step
    # The verb constraint is surfaced to the clinician, not buried in code.
    assert any("VERB SET IS CONSTRAINED BY A MACHINE RULE" in n for n in notes)


def test_packet_reports_resolver_reach_honestly(tmp_path):
    """Both reachable and unreachable rungs are shown, labelled."""
    PACKET.build(out_dir=tmp_path, families=[FIRST_WORDS])
    page = (tmp_path / f"{FIRST_WORDS}_review_packet.html").read_text()
    assert "selected for baselines at" in page
    rungs = PACKET.rungs_for_family(FIRST_WORDS)
    assert [r["months"] for r in rungs] == [12, 15, 18]
    assert all(r["reachable_from"] for r in rungs)
