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
FUNCTION_Q = "function_question_answering"
SENTENCES = "sentence_building"
NARRATION = "narration_storytelling"

#: Candidate families that ALSO have live legacy content under
#: `reviewed_cards/`. `sentence_building` is the first of these: it carries six
#: cards admitted before this ledger existed, all six in `legacy_unresolved`,
#: and a new founder-review candidate set beside them.
#:
#: That combination breaks an assumption the promotion check used to be able to
#: make — "a Track 2 candidate family has no reviewed-cards file" — so the check
#: now asserts the thing that actually matters instead: no CANDIDATE card's id
#: appears in the admitted set. See `test_nothing_has_been_promoted`.
FAMILIES_WITH_LEGACY_CONTENT = (SENTENCES,)

#: Candidate counts, each DERIVED and recorded so a later edit that pads or
#: thins a family has to say so here.
#:
#: `early_vocalizations` was 6 at the Track 2 checkpoint, because two proposed
#: concepts — routine-embedded cooing and routine-embedded babble — proved to be
#: one activity at different rungs and were merged rather than kept apart to
#: reach a number. It is 7 as of the founder/product review on 2026-10-27, which
#: added `Peekaboo Sounds` as a deliberate seventh concept. That merge still
#: stands; the seventh card is an addition, not an un-merge.
#:
#: `action_picture_labeling` was 5 at the Track 2 checkpoint — the five steps of
#: the support gradient. It is 7 as of the founder/product review on 2026-10-27,
#: which added `Action Charades` and `Mini Action Story`. THE GRADIENT IS STILL
#: FIVE STEPS: both new cards sit alongside it rather than extending it, which is
#: why `test_action_picture_support_gradient_is_recorded_for_review` still looks
#: for "step N of 5" and why that assertion was NOT widened to 7.
#:
#: `function_question_answering` is a FOURTH family as of the 3-5y content
#: phase. It had zero candidate and zero reviewed content, and it is the 48m
#: sibling of `sentence_building` — the band a 4.5-5y child most likely anchors
#: in — so its absence turned a legitimate target into a generation refusal. Its
#: 7 cards are seven DIFFERENT mechanisms for answering a function question, not
#: seven variations of one; see the ledger notes, which record each card's
#: mechanism and why it is not redundant with its siblings.
#:
#: `sentence_building` is a FIFTH family as of the 3-5y content phase, and the
#: first one authored BESIDE live legacy content rather than into an empty
#: family. The audit of its six legacy cards found that none required the
#: canonical four-or-more words, five of six were requests or fixed social
#: formulas, and commenting, single-event recount, negation and systematic
#: expansion were absent. The seven candidates are a balanced replacement SET
#: offered for review; the live six are untouched.
#:
#: `narration_storytelling` is the SIXTH family and the last planned 3-5y one.
#: It had zero candidate and zero reviewed content. Its target is EVENT
#: STRUCTURE — at least two connected events told out loud — which is why no
#: card here imposes a word count: that is `sentence_building`'s rung, and the
#: two families are kept apart by event count, not by utterance length.
#:
#: `test_promotion_plan_is_empty_while_nothing_is_approved` takes its total from
#: `sum(EXPECTED_COUNTS.values())` rather than a second literal, and
#: `test_packet_renders_every_candidate_with_its_id` takes its packet count from
#: `len(EXPECTED_COUNTS)`, so this dict stays the only place a count is stated.
EXPECTED_COUNTS = {FIRST_WORDS: 7, PRE_VERBAL: 7, ACTION_PICTURE: 7,
                   FUNCTION_Q: 7, SENTENCES: 7, NARRATION: 7}

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


def test_every_in_scope_family_has_candidates():
    """Set equality in BOTH directions.

    A candidate file on disk that nobody declared would be unreviewed content
    arriving silently, and a declared family with no file would be a count
    nobody can check. Six families as of the 3-5y content phase.
    """
    assert set(L.candidate_families()) == set(EXPECTED_COUNTS)
    assert len(EXPECTED_COUNTS) == 6


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

    RESTATED for the 3-5y content phase. This used to assert that NO candidate
    family had a reviewed-cards file at all, which was true while every Track 2
    family was authored into an empty slot. `sentence_building` broke that:
    it has six legacy cards under `reviewed_cards/` AND a new candidate set, so
    the old form would have failed for a reason that has nothing to do with
    promotion.

    The claim that actually matters is unchanged and is now asserted directly:
    not one CANDIDATE card's id appears in the admitted set. That is stronger
    than the file check it replaces — a file could exist and still contain none
    of our candidates, and conversely a promoted candidate could have been
    appended to a file that already existed, which the old assertion would have
    missed entirely.
    """
    admitted = set()
    for path in sorted(L.REVIEWED_CARDS_DIR.glob("*.json")):
        for card in L.read_cards(path):
            admitted.add(L.compute_card_id(path.stem, card))

    for family in EXPECTED_COUNTS:
        if family not in FAMILIES_WITH_LEGACY_CONTENT:
            # Still the strongest available check for a family with no legacy
            # content: the file must not exist at all.
            assert not (L.REVIEWED_CARDS_DIR / f"{family}.json").exists(), family
        for card in L.read_cards(L.CANDIDATES_DIR / f"{family}.json"):
            tid = L.compute_card_id(family, card)
            assert tid not in admitted, (family, card["title"], tid)

    # The served bank is unchanged from the pre-Track-2 three families.
    present = sorted(p.stem for p in L.REVIEWED_CARDS_DIR.glob("*.json"))
    assert present == ["book_object_naming", "sentence_building",
                       "two_word_phrases"], present
    # ...and the one family with legacy content still has exactly its six.
    assert len(L.read_cards(L.REVIEWED_CARDS_DIR / f"{SENTENCES}.json")) == 6


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

    Approve a card, change any authored content, and the approved id no longer
    describes any card on disk — so the edited wording is unapproved and
    unpromotable. There is no transfer step to get it wrong.

    THE MUTATION IS DERIVED FROM THE SCHEMA, NOT FROM TODAY'S PROSE. An earlier
    version of this test replaced the literal phrase "Wait five seconds" in the
    first candidate's instructions. A founder content revision removed that
    phrase, `str.replace` silently became a no-op, and the test fell over on its
    own `edited != card` guard — it was pinned to wording that was always going
    to change. Iterating `card_fields()` instead means no editorial revision can
    turn the mutation into a no-op, and it widens the claim: EVERY authored
    field is now shown to be load-bearing for identity, not just `instructions`.
    """
    card, approved_id = _first_candidate()
    approved = frozenset({approved_id})

    seen = {approved_id}
    for field in L.card_fields():
        edited = dict(card)
        # Appended text must be NON-whitespace: `compute_template_id` strips
        # each value, so a whitespace-only "edit" is correctly not an edit.
        edited[field] = card[field] + " (edited)"
        assert edited[field] != card[field], field

        edited_id = L.compute_card_id(FIRST_WORDS, edited)
        assert edited_id != approved_id, field
        assert edited_id not in approved, field
        # Distinct per field: the digest joins values positionally, so the same
        # suffix in a different field is a different template.
        assert edited_id not in seen, field
        seen.add(edited_id)

    assert len(seen) == len(L.card_fields()) + 1, sorted(seen)


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
    # The span is what this block is about, and it comes from the rungs the
    # family BINDS. It used to also assert how many of the six were reachable
    # as v1 month-step targets; that field is gone — see
    # `test_the_packet_carries_no_stale_v1_resolver_language`.
    assert [r["months"] for r in rungs] == [2, 4, 6, 6, 6, 9], rungs


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


def test_the_packet_carries_no_stale_v1_resolver_language(tmp_path):
    """FOUNDER DECISION, 2026-10-28: the resolver-reach column is REMOVED.

    It reported, per rung, which monthly floors the resolver would step from to
    reach it, read out of the rung artifact's enumerated `steps` table. That
    table IS the F-B v1 month-stepping mechanism, which the evidence-driven v2
    path replaced — v2 selects a band from projected per-skill evidence and
    anchors on the `rung_ref` the evidence named. So the column described a
    mechanism that no longer chooses targets, in a document whose purpose is
    clinical CONTENT review, and it labelled five legitimate v2 targets "not
    currently selected by the resolver": the 48m sentence rung, the 36m
    action-picture rung and three pre-verbal rungs.

    It was removed rather than re-derived because a v2-accurate replacement
    would have to be `mappable`, and that is a CONSTANT here: `rungs_for_family`
    filters on non-empty `family_bindings`, and the four unmappable
    declared-track rungs have empty bindings, so no unmappable rung can appear
    in any family's table. `test_a_mappable_column_would_be_a_constant` proves
    that rather than asserting it.

    Asserted across EVERY family, because stale text in one packet is as
    misleading as stale text in all of them.
    """
    for family in EXPECTED_COUNTS:
        PACKET.build(out_dir=tmp_path, families=[family])
        page = (tmp_path / f"{family}_review_packet.html").read_text()
        for stale in ("selected for baselines at",
                      "not currently selected by the resolver",
                      "Resolver reach"):
            assert stale not in page, (family, stale)
        # The table still answers the question the packet exists to answer.
        assert "Canonical rungs this family binds" in page
        for rung in PACKET.rungs_for_family(family):
            assert rung["rung_ref"] in page, (family, rung["rung_ref"])
            assert "reachable_from" not in rung, family


def test_a_mappable_column_would_be_a_constant(tmp_path):
    """NON-VACUITY for the removal above: option 1 really was degenerate.

    If an unmappable rung could ever reach a family's table, a
    mappable/unmappable column would carry information and removing it would
    have lost something. It cannot, and this measures that from the artifact.
    """
    import json

    body = json.loads(
        (L.REPO_ROOT / "pilot_runtime" / "data"
         / "rung_table_talking_v1.json").read_text(encoding="utf-8"))
    unmappable = [r for r in body["rungs"].values() if not r["mappable"]]
    assert unmappable, "no unmappable rung in the artifact — control is vacuous"
    for rung in unmappable:
        assert rung["family_bindings"] == [], rung["rung_ref"]

    # ...so every rung every family binds is mappable, for all five families.
    seen = set()
    for family in EXPECTED_COUNTS:
        for row in PACKET.rungs_for_family(family):
            seen.add(body["rungs"][row["rung_ref"]]["mappable"])
    assert seen == {True}, seen


# ---------------------------------------------------------------------------
# function_question_answering — the 3-5y content phase
# ---------------------------------------------------------------------------


def test_function_question_cards_require_a_SPOKEN_function_answer():
    """The family's defining boundary, asserted rather than described.

    `function_question_answering` is the child ANSWERING what an object is used
    for. Every neighbouring skill is a different family — asking wh-questions,
    naming an object, following a direction, matching to a picture — and each of
    those is a plausible thing a card could drift into. So every success
    criterion must demand a SPOKEN answer, and no receptive or non-verbal
    response may satisfy one on its own.
    """
    import re

    says = re.compile(r"\bsays?\b", re.I)
    nonverbal_in_success = re.compile(
        r"\b(points?|pointing|fetch(es)?|matches|matching|chooses? correctly)\b",
        re.I)
    for card in L.read_cards(L.CANDIDATES_DIR / f"{FUNCTION_Q}.json"):
        success = card["success_criteria"]
        assert says.search(success), card["title"]
        assert not nonverbal_in_success.search(success), card["title"]


def test_function_question_cards_impose_no_sentence_length_bar():
    """Semantic skill, not a syntactic one.

    The 48m band holds TWO skills and `sentence_building` owns the four-or-more
    word bar. Importing that bar here would make a child who knows perfectly
    well what a cup is for fail for saying "drinking" — and would make the two
    siblings test the same thing.
    """
    import re

    length_bar = re.compile(
        r"four or more|4\+ word|complete sentence|full sentence|sentence with",
        re.I)
    for card in L.read_cards(L.CANDIDATES_DIR / f"{FUNCTION_Q}.json"):
        assert not length_bar.search(card["success_criteria"]), card["title"]
        # ...and the family says so positively at least once.
    blob = " ".join(c["success_criteria"]
                    for c in L.read_cards(L.CANDIDATES_DIR / f"{FUNCTION_Q}.json"))
    assert re.search(r"single word|one-word|short phrase|accurate word", blob, re.I)


def test_function_question_make_harder_stays_inside_the_rung():
    """The out-of-scope directions the founder named, asserted as absent.

    Abstract why-reasoning, category definitions, multi-step inference,
    narration and child-asked wh-questions are each a DIFFERENT canonical skill.
    A `make_harder` that reached into one of them would quietly retarget the
    card.
    """
    import re

    out_of_scope = re.compile(
        r"\bwhy\b|category|categor|riddle|guessing game|multi-step|"
        r"several steps|tell (me )?a story|narrat", re.I)
    for card in L.read_cards(L.CANDIDATES_DIR / f"{FUNCTION_Q}.json"):
        assert not out_of_scope.search(card["make_harder"]), card["title"]


def test_each_function_question_card_records_its_mechanism_for_review():
    """Seven mechanisms, numbered, so a reviewer can check for padding.

    The founder asked for seven genuinely different ways of answering a
    function question rather than seven flash-card variants. The ledger note on
    each card states which mechanism it is and why it is not redundant; this
    asserts all seven are present and distinct, which is the cheapest guard
    against a later card being added without that justification.
    """
    ledger = L.read_ledger()
    notes = []
    for card in L.read_cards(L.CANDIDATES_DIR / f"{FUNCTION_Q}.json"):
        entry = ledger["entries"][L.compute_card_id(FUNCTION_Q, card)]
        notes.append(entry["note"])
    for n in range(1, 8):
        assert any(f"Mechanism {n} of 7" in note for note in notes), n
    # The no-reuse decision is recorded on every card, not just one.
    assert all("NO EXISTING CONTENT WAS REUSED" in note for note in notes)


# ---------------------------------------------------------------------------
# sentence_building — the founder-review replacement set
#
# This family is the one with a live legacy set beside its candidates, and the
# one whose canonical target is a MEASURABLE utterance length. Both of those
# make it the easiest family to get quietly wrong, so the five properties the
# founder named are pinned rather than reviewed.
# ---------------------------------------------------------------------------

#: Mechanisms whose primary communicative function is REQUESTING. Listed so the
#: balance check below is a statement about functions rather than about titles.
_REQUEST_TITLES = ("I Need / I Want — Full Sentence",)


def test_at_least_six_of_seven_require_a_four_word_sentence():
    """The canonical 48m target is four or more words, so the SET must measure it.

    The audit of the legacy six found the opposite: criteria said "three-to-four
    word", which three words satisfy, and every `make_harder` that reached four
    said "without requiring it". A family named for a four-word milestone that
    never asks for four words is the defect this replaces.
    """
    import re

    four = re.compile(r"at least four words|four or more words", re.I)
    hedged = re.compile(r"three-to-four|three to four|3-4 word", re.I)
    cards = L.read_cards(L.CANDIDATES_DIR / f"{SENTENCES}.json")
    requiring = [c["title"] for c in cards if four.search(c["success_criteria"])]
    assert len(requiring) >= 6, requiring
    for card in cards:
        # No criterion may hedge to a band three words satisfies.
        assert not hedged.search(card["success_criteria"]), card["title"]
        # ...and the bar may not hide in make_harder while success stays short.
        if not four.search(card["success_criteria"]):
            assert not four.search(card["make_harder"]), card["title"]


def test_the_set_is_not_request_dominated():
    """Five of the legacy six were requests or fixed social formulas.

    One functional-request card is kept deliberately; a second would start the
    family drifting back toward the shape the audit criticised.
    """
    cards = L.read_cards(L.CANDIDATES_DIR / f"{SENTENCES}.json")
    requests = [c["title"] for c in cards if c["title"] in _REQUEST_TITLES]
    assert len(requests) == 1, requests
    assert len(cards) - len(requests) == 6


def test_the_past_event_card_is_single_event_not_narration():
    """The boundary against `narration_storytelling` (60m, two events).

    A single past-event sentence is `sentence_building`; a two-event story is a
    different family and a later rung. Asserted on the success criterion,
    because that is what a caregiver reads as the target.
    """
    import re

    card = [c for c in L.read_cards(L.CANDIDATES_DIR / f"{SENTENCES}.json")
            if c["title"] == "Tell Me What Happened"][0]
    success = card["success_criteria"]
    assert re.search(r"\bONE\b.*happened|one thing that happened", success)
    assert re.search(r"two events.*NOT required|NOT required", success)
    for forbidden in ("sequence", "beginning and middle", "retell the story"):
        assert forbidden not in success.lower(), forbidden
    # And the caregiver-facing guardrail repeats it.
    assert "order" in card["what_to_avoid"].lower()


def test_no_nonverbal_response_satisfies_sentence_success():
    """Every criterion names a SPOKEN sentence, and any non-verbal behaviour it
    mentions is mentioned only to EXCLUDE it.

    `_family_bucket('sentence_building')` resolves to `general`, so the content
    validator imposes nothing here at all — the expressive boundary is held
    entirely by this wording.

    Checked on POLARITY rather than on absence. An earlier version of this test
    asserted the words simply did not appear, and it failed on `Add Where`,
    whose criterion says "Pointing at the place does not count" — the very
    disclaimer the rule wants. Absence and exclusion are different properties,
    and only the second is worth asserting.
    """
    import re

    says = re.compile(r"\bsays\b|\basks for\b", re.I)
    nonverbal = re.compile(
        r"\b(points?|pointing|gestures?|head shake|nods?|hands you|fetch(es)?)\b",
        re.I)
    excluded = re.compile(
        r"does not count|is not this card's target|not success|NOT success|"
        r"is not the target", re.I)
    checked = 0
    for card in L.read_cards(L.CANDIDATES_DIR / f"{SENTENCES}.json"):
        success = card["success_criteria"]
        assert says.search(success), card["title"]
        if nonverbal.search(success):
            assert excluded.search(success), card["title"]
            checked += 1
    # Non-vacuity: at least one criterion really does name and exclude one.
    assert checked >= 1


def test_the_self_advocacy_card_cannot_gate_an_essential_need():
    """THE ANTI-COERCION INVARIANT, and the only reason a length bar is safe here.

    A four-word bar on a request card is dangerous in exactly one way: it could
    read as "a short request does not count", which for a stop, a break, pain,
    the toilet, food, water or safety would be coercive. So the criterion must
    scope the bar to a NON-ESSENTIAL practice moment AND state that a short
    essential message is successful communication, honoured at once.

    The legacy line is carried over verbatim because it is the clearest
    statement of the rule anyone has written for this family.
    """
    card = [c for c in L.read_cards(L.CANDIDATES_DIR / f"{SENTENCES}.json")
            if c["title"] in _REQUEST_TITLES][0]
    success = card["success_criteria"]
    assert "non-essential" in success
    assert "NOT a failed attempt" in success
    assert "honoured at once" in success

    avoid = card["what_to_avoid"]
    assert ("Never ignore a clear request to stop in order to get a longer "
            "sentence.") in avoid
    for essential in ("food", "drink", "toilet", "comfort", "help", "safety"):
        assert essential in avoid.lower(), essential
    # The instructions must name the immediate-response rule too, not just the
    # avoid line: a caregiver reads the instructions first.
    assert "respond immediately" in card["instructions"]


def test_every_sentence_candidate_records_truthful_legacy_provenance():
    """One `supersedes`, and it points at the one card this set revises.

    `supersedes` is a single string, so a one-to-many lineage is NOT
    representable. The functional-request card consolidates ideas from three
    legacy cards and therefore carries `supersedes: null` with the relationship
    stated in prose — recording one of the three would be a false claim about
    the other two.
    """
    ledger = L.read_ledger()
    LEGACY_WIDW = "atpl1:2c317105a4151f55be1d2122a16fc3fc"
    supersedes = {}
    for card in L.read_cards(L.CANDIDATES_DIR / f"{SENTENCES}.json"):
        entry = ledger["entries"][L.compute_card_id(SENTENCES, card)]
        supersedes[card["title"]] = entry["supersedes"]

    assert supersedes["Who Is Doing What?"] == LEGACY_WIDW
    assert sum(1 for v in supersedes.values() if v) == 1, supersedes
    # The legacy target is a registered legacy card, never an approved one.
    assert LEGACY_WIDW in ledger["legacy_unresolved"]
    assert LEGACY_WIDW not in ledger["entries"]
    assert LEGACY_WIDW not in L.approved_ids(ledger)


def test_the_packet_warns_when_a_family_already_has_live_content(tmp_path):
    """`sentence_building` is the only family with admitted content today.

    A reviewer handed seven candidates for a family that already has six live
    cards could reasonably conclude the seven had replaced them. They have not,
    so the packet says so in a bordered block — and says it where it is true.
    """
    PACKET.build(out_dir=tmp_path, families=[SENTENCES])
    page = (tmp_path / f"{SENTENCES}_review_packet.html").read_text()

    assert "This family already has live content" in page
    assert "6 card(s) for this family are already" in page
    assert "have not replaced anything" in page
    assert "legacy_unresolved" in page
    assert "does <strong>not</strong> mean anyone approved them" in page
    assert "separate, explicit promotion decision" in page


def test_the_live_content_warning_is_absent_where_it_would_be_false(tmp_path):
    """Non-vacuity for the block above.

    Without this the notice could appear on every packet, which is how a
    warning stops being read. The other four families have no admitted content,
    so claiming they do would be false.
    """
    for family in EXPECTED_COUNTS:
        if family in FAMILIES_WITH_LEGACY_CONTENT:
            continue
        PACKET.build(out_dir=tmp_path, families=[family])
        page = (tmp_path / f"{family}_review_packet.html").read_text()
        assert "This family already has live content" not in page, family
        assert PACKET.legacy_admitted_count(family) == 0, family


def test_the_legacy_count_is_derived_from_the_directory(tmp_path):
    """Counted from `reviewed_cards/`, never restated in the tool.

    Proven by pointing the counter at an empty directory and at a fabricated
    one: a hard-coded 6 would survive both.
    """
    assert PACKET.legacy_admitted_count(SENTENCES) == 6
    assert PACKET.legacy_admitted_count(SENTENCES, reviewed_dir=tmp_path) == 0

    card = L.read_cards(L.REVIEWED_CARDS_DIR / f"{SENTENCES}.json")[0]
    (tmp_path / f"{SENTENCES}.json").write_text(
        json.dumps([card, card], indent=2), encoding="utf-8")
    assert PACKET.legacy_admitted_count(SENTENCES, reviewed_dir=tmp_path) == 2


# ---------------------------------------------------------------------------
# narration_storytelling — the 60m two-event family
#
# This family is defined by EVENT STRUCTURE, and every neighbouring skill is a
# plausible place for a card to drift: one-event recount (sentence_building),
# answering questions about a story (story_comprehension, deliberately
# unresolved), ordering pictures (receptive sequencing), naming what is in a
# photo (object naming), and pretend play. `_family_bucket` resolves this
# family to `general`, so the validator imposes nothing — the boundaries are
# held by the success criteria, and so they are pinned.
# ---------------------------------------------------------------------------


def _narration_cards():
    return L.read_cards(L.CANDIDATES_DIR / f"{NARRATION}.json")


def test_every_narration_success_requires_at_least_two_events():
    """The canonical 60m target is a story with at least two events.

    Semantic rather than literal: any criterion that commits to two events
    passes, however it words it — "at least TWO things", "BOTH events", "a
    two-event story". What must not appear is a criterion satisfiable by one.
    """
    import re

    two = re.compile(
        r"at least two\b|both events\b|two-event story|holds? two\b", re.I)
    for card in _narration_cards():
        assert two.search(card["success_criteria"]), card["title"]


def test_the_ledger_file_contains_no_duplicate_keys():
    """A shadowed duplicate key parses cleanly and hides stale content.

    Added because it actually happened: a note was patched by appending the new
    line without removing the old one, so two entries carried two `note` keys.
    `json.load` keeps the LAST, so every consumer — the gate, the packet, the
    promotion plan, this suite — read the corrected text and passed, while the
    superseded note (including a review question the founder had already
    answered) was still physically in the file. A review ledger is an audit
    record, so text that is present but unread is exactly the wrong failure
    mode: the only way to catch it is to parse strictly.
    """
    def no_duplicates(pairs):
        seen = set()
        for key, _ in pairs:
            if key in seen:
                raise AssertionError(f"duplicate key in the ledger: {key!r}")
            seen.add(key)
        return dict(pairs)

    raw = L.LEDGER_PATH.read_text(encoding="utf-8")
    json.loads(raw, object_pairs_hook=no_duplicates)
    # Non-vacuity: the hook really does reject a duplicate.
    with pytest.raises(AssertionError, match="duplicate key"):
        json.loads('{"note": "old", "note": "new"}',
                   object_pairs_hook=no_duplicates)


def test_full_narration_success_requires_the_child_to_express_both_events():
    """FOUNDER CORRECTION: a split telling is scaffolding, not full success.

    The failure mode is subtle, and the two-event test above did NOT catch it.
    A criterion can name a two-event story and still be satisfied when the
    CAREGIVER supplied event one and the child supplied only event two. The
    child has then expressed a single event — which is precisely
    `sentence_building`'s 48m target — so the 60m rung would be credited on
    lower-rung evidence. Caregiver scaffolding stays legitimate; what must not
    happen is scaffolding being scored as the target.

    Asserted three ways rather than by one literal string, so a rewording that
    preserves the meaning passes and a rewording that loses it fails:

      1. the child is the subject of the telling AND the two-event quantity is
         attached to that same telling, in one clause;
      2. no criterion credits a partial contribution;
      3. polarity — where a criterion raises the caregiver-supplied case, it
         must deny it full success rather than award it.
    """
    import re

    # 1. "The child tells ... <two events>", with no sentence boundary between
    #    the subject and the quantity, so the two cannot be attributed apart.
    child_tells_both = re.compile(
        r"the child tells\b[^.]{0,120}?"
        r"(at least two\b|both events\b|two-event story)", re.I)
    for card in _narration_cards():
        assert child_tells_both.search(card["success_criteria"]), card["title"]

    # 2. A partial contribution must never be the criterion. This is the exact
    #    shape the founder correction removed from cards 3 and 7.
    partial = re.compile(
        r"(supplying|supplies|supply|contribut\w*|adds?|tells?)\s+"
        r"(at least\s+)?(one|either)\s+of\s+the\s+(two\s+)?events", re.I)
    for card in _narration_cards():
        assert not partial.search(card["success_criteria"]), card["title"]
    # Non-vacuity: the pattern really does catch the wording that was removed,
    # so this is a live check and not decoration.
    assert partial.search(
        "with the child supplying at least one of the events in words")

    # 3. The two cards that scaffold by design must say which side of the line
    #    the scaffolded form falls on.
    split = re.compile(
        r"caregiver supplies|you supply|supplies the first|"
        r"supply the first event", re.I)
    denied = re.compile(
        r"not yet|does not yet count|scaffolding|toward the target|"
        r"full success is recorded when", re.I)
    raised = 0
    for card in _narration_cards():
        if split.search(card["success_criteria"]):
            assert denied.search(card["success_criteria"]), card["title"]
            raised += 1
    assert raised >= 2, raised


def test_narration_scaffolding_is_labelled_as_scaffolding_not_as_the_target():
    """The other half of the correction: `make_easier` must not promise success.

    `make_easier` is where the caregiver start and the spoken choices live, so
    it is the field most likely to read as "and that counts". It has to offer
    the support AND say the support is not yet the target — otherwise the
    criterion says one thing and the ladder beside it says another.
    """
    import re

    scaffold = re.compile(
        r"supply the first event|give the first event|offer two spoken|"
        r"two spoken endings|caregiver supplies", re.I)
    # Deliberately formulation-agnostic. The two corrected cards say
    # "scaffolding toward the target"; cards 1, 2 and 5 said the same thing
    # first, in the founder-accepted form "one event is a real step ... but
    # only two events meet this target". Both distinguish the support from the
    # target, which is the whole invariant — so both must pass, and a regex
    # that only knew the newer wording would have demanded edits to accepted
    # content to satisfy itself.
    labelled = re.compile(
        r"scaffolding|not (yet )?the target|toward the target|"
        r"what to look for|full success is|is a real step|"
        r"(meet|is|are) (this )?the target|meet this target", re.I)
    checked = 0
    for card in _narration_cards():
        easier = card["make_easier"]
        if scaffold.search(easier):
            assert labelled.search(easier), card["title"]
            checked += 1
    assert checked >= 2, checked
    # Non-vacuity: a make_easier that offers the scaffold and says nothing
    # about where the target sits must fail, or this test proves nothing.
    assert not labelled.search(
        "Supply the first event yourself and let your child add the second.")


def test_one_event_production_is_never_narration_success():
    """A single event, or a single sentence, must not satisfy this family.

    The failure mode is a criterion that reads as satisfied by one good
    sentence — which is exactly `sentence_building`'s target and would make the
    two families measure the same thing.
    """
    import re

    single_ok = re.compile(
        r"success.{0,40}\bone (event|sentence|thing)\b|"
        r"\bone (event|sentence|thing)\b.{0,30}(counts|is success|is enough)",
        re.I)
    for card in _narration_cards():
        assert not single_ok.search(card["success_criteria"]), card["title"]
    # ...and at least one card states the exclusion positively, so the
    # distinction is visible to a reviewer rather than merely implied.
    stated = [c["title"] for c in _narration_cards()
              if re.search(r"one event.{0,80}not success|"
                           r"one event on its own is not", c["success_criteria"],
                           re.I)]
    assert stated, "no card states the one-event exclusion explicitly"


def test_nonverbal_ordering_or_naming_is_never_narration_success():
    """Ordering, pointing and naming are each a DIFFERENT family's skill.

    Where a criterion mentions one of them it must be to exclude it. Checked on
    polarity, not absence — a card that names the trap in order to rule it out
    is doing the right thing.
    """
    import re

    nonverbal = re.compile(
        r"\b(point(ing)?|right order|arrang\w*|naming|acted out|"
        r"moving the toys|doing the actions)\b", re.I)
    excluded = re.compile(
        r"not success|is not enough|NOT success|rather than", re.I)
    checked = 0
    for card in _narration_cards():
        success = card["success_criteria"]
        if nonverbal.search(success):
            assert excluded.search(success), card["title"]
            checked += 1
    assert checked >= 4, checked


def test_no_narration_card_imposes_a_word_count():
    """Event structure, not utterance length.

    `sentence_building` owns the four-or-more-word bar. Importing it here would
    fail a child who said "we went out and then we ate" — two events in seven
    words — for the wrong reason, and would blur the two rungs.
    """
    import re

    length = re.compile(
        r"four or more words|at least four words|\d+\+? words?\b|"
        r"complete sentence|full sentence", re.I)
    for card in _narration_cards():
        assert not length.search(card["success_criteria"]), card["title"]
    # The family says so positively at least once: grammar is not the measure.
    blob = " ".join(c["success_criteria"] for c in _narration_cards())
    assert re.search(r"imperfect grammar|not required|does not (need|have) to",
                     blob, re.I)


def test_the_set_holds_both_real_event_and_invented_mechanisms():
    """The milestone says "heard or made up", so the set must cover both.

    Derived from the ledger's mechanism notes rather than from titles, so a
    retitled card cannot quietly collapse the coverage.
    """
    ledger = L.read_ledger()
    notes = {c["title"]: ledger["entries"][L.compute_card_id(NARRATION, c)]["note"]
             for c in _narration_cards()}
    assert any("REAL RECENT EVENTS" in n for n in notes.values())
    assert any("PERSONAL PHOTOS" in n for n in notes.values())
    assert any("INVENTED story" in n for n in notes.values())
    assert any("ENACTED EVENTS" in n for n in notes.values())
    for n in range(1, 8):
        assert any(f"Mechanism {n} of 7" in note for note in notes.values()), n


def test_narration_and_sentence_building_measure_different_things():
    """THE BOUNDARY TEST THE FOUNDER ASKED FOR, asserted from both sides.

    `sentence_building`'s `Tell Me What Happened` deliberately requires ONE
    event and a 4+ word sentence. `narration_storytelling` requires TWO events
    and NO word count. If either family drifted toward the other, the 48m and
    60m rungs would be measuring the same skill from one child's evidence.
    """
    import re

    recount = [c for c in L.read_cards(L.CANDIDATES_DIR / f"{SENTENCES}.json")
               if c["title"] == "Tell Me What Happened"][0]
    # sentence_building side: one event, with a length bar.
    assert re.search(r"\bONE\b", recount["success_criteria"])
    assert re.search(r"at least four words", recount["success_criteria"])
    assert re.search(r"two events.{0,80}NOT required", recount["success_criteria"])

    # narration side: two events, with no length bar anywhere.
    two = re.compile(r"at least two\b|both events\b|two-event story", re.I)
    length = re.compile(r"at least four words|four or more words", re.I)
    for card in _narration_cards():
        assert two.search(card["success_criteria"]), card["title"]
        assert not length.search(card["success_criteria"]), card["title"]


def test_no_narration_card_requires_exact_recall_or_perfect_grammar():
    """Supports are free; accurate independent recall is not the target."""
    import re

    forbidden = re.compile(
        r"word[- ]for[- ]word|exactly as|correct grammar|accurate(ly)? recall|"
        r"remembers? every", re.I)
    # Scoped to the fields that can IMPOSE something. An earlier version swept
    # all nine and failed on "Do not correct grammar while a story is being
    # told" — a PROHIBITION in `what_to_avoid`, i.e. the rule working, not a
    # violation of it. Requirement and prohibition are opposite claims and only
    # the first belongs in this sweep.
    for card in _narration_cards():
        for field in ("success_criteria", "make_harder", "instructions"):
            assert not forbidden.search(card[field]), (card["title"], field)
    # ...and the prohibition really is stated, which is the other half.
    avoid = " ".join(c["what_to_avoid"] for c in _narration_cards())
    assert re.search(r"memory is wrong", avoid, re.I)
    assert re.search(r"not correct grammar|do not correct", avoid, re.I)
    assert re.search(r"quiz", avoid, re.I)


def test_narration_make_harder_stays_narration():
    """Progression adds events and removes supports — never abstract reasoning.

    Complex causal reasoning, story grammar, multiple characters and exact
    transition vocabulary are each a different and later skill; a `make_harder`
    reaching into one of them would retarget the card.
    """
    import re

    out_of_scope = re.compile(
        r"\bwhy\b|because of|cause and effect|beginning, middle|story grammar|"
        r"several characters|moral of", re.I)
    # Every legitimate progression for THIS family: add an event, remove a
    # support, reduce the salience of the cue, or hand the telling over.
    progression = re.compile(
        r"third event|third picture|third thing|no (choices|model|cue|photo|starter)|"
        r"invent both|supply both|put (them|the photos) away|set (them|it) aside|"
        r"longer|unseen|not seen before|has not seen|detail|less obvious|"
        r"let your child (tell|decide|invent|start)", re.I)
    for card in _narration_cards():
        assert not out_of_scope.search(card["make_harder"]), card["title"]
        assert progression.search(card["make_harder"]), card["title"]
