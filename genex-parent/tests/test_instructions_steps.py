"""
tests/test_instructions_steps.py — Beta 2.0 Step 2B

Unit tests for the adapter-only `instructions_steps` derivation in api/adapters.py.

Scope: pure response-shaping. No backend logic, generation, auth, beta-code,
schemas, endpoints, feedback, progress, or reports are exercised or changed.

Run: PYTHONPATH=. python3 tests/test_instructions_steps.py
"""

import sys

from api.adapters import _split_instructions_into_steps, _normalize_slot

_passed = 0
_failed = 0


def check(label, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ✓ {label}")
    else:
        _failed += 1
        print(f"  ✗ FAIL: {label} — {detail}")


# The exact fields the parent-facing card has had since Beta 1.0. `instructions_steps`
# is additive and intentionally NOT in this list — the test asserts none of these
# are removed.
_EXISTING_FIELDS = {
    "id", "title", "domain", "domain_label", "duration_label",
    "why", "instructions", "materials", "success_criteria",
    "make_easier", "make_harder", "group_play", "avoid",
}


def test_splitter_basic_multi_sentence():
    print("\n── splitter: normal paragraph → multiple steps")
    text = (
        "Dump the sock basket on the floor between you and your child. "
        "Pick up one sock and say 'I need the match!' "
        "Wait for your child to pick up a sock and bring it to you. "
        "Help them press the pair together. Aim for 3-4 pairs."
    )
    steps = _split_instructions_into_steps(text)
    check("returns a list", isinstance(steps, list), type(steps).__name__)
    check("splits into multiple steps", len(steps) >= 4, f"got {len(steps)}: {steps}")
    check("keeps quoted clause intact (no break inside \"match!'\")",
          any("I need the match!'" in s for s in steps), steps)
    check("does not break the range '3-4 pairs'",
          any("3-4 pairs" in s for s in steps), steps)
    check("no empty/whitespace-only steps",
          all(s and s.strip() == s for s in steps), steps)
    check("joining steps reproduces the sentences (no content invented)",
          " ".join(steps) == text, " ".join(steps))


def test_splitter_decimal_not_split():
    print("\n── splitter: decimals and lowercase abbreviations are not over-split")
    steps = _split_instructions_into_steps("Pour 1.5 cups of water. Stir slowly.")
    check("decimal '1.5' kept whole", any("1.5 cups" in s for s in steps), steps)
    check("two steps from two real sentences", len(steps) == 2, steps)

    steps2 = _split_instructions_into_steps("Use soft items, e.g. cups, then stack them.")
    check("lowercase 'e.g.' not split (conservative)", len(steps2) == 1, steps2)


def test_splitter_empty_and_single():
    print("\n── splitter: empty and single-sentence inputs")
    check("empty string → []", _split_instructions_into_steps("") == [], "")
    check("whitespace-only → []", _split_instructions_into_steps("   ") == [], "")
    check("None → []", _split_instructions_into_steps(None) == [], "")
    single = _split_instructions_into_steps("Roll the ball back and forth")
    check("no terminal punctuation → single step (no over-split)",
          single == ["Roll the ball back and forth"], single)


def _make_slot(instructions):
    return {
        "category_key": "language_and_communication",
        "title": "Quick Naming Walk",
        "why": "Builds early words.",
        "instructions": instructions,
        "materials": "none",
        "success": "Your child says or points at one thing.",
        "make_easier": "Name it first, then pause.",
        "make_harder": "Ask for two words.",
        "group_play": "Take turns naming with a sibling.",
        "avoid": "Avoid quizzing or pressure.",
    }


def test_normalize_slot_preserves_instructions_and_is_additive():
    print("\n── _normalize_slot: instructions preserved + instructions_steps additive")
    instructions = (
        "Walk to one window together. Point and name what you see. "
        "Wait for your child to copy a word. Praise any attempt."
    )
    slot = _make_slot(instructions)
    card = _normalize_slot(slot, session_id="sess-1", day="Monday", slot_index=0)

    check("instructions preserved EXACTLY",
          card["instructions"] == instructions, repr(card.get("instructions")))
    check("instructions_steps present", "instructions_steps" in card, list(card.keys()))
    check("instructions_steps is a list", isinstance(card["instructions_steps"], list),
          type(card["instructions_steps"]).__name__)
    check("instructions_steps has multiple steps",
          len(card["instructions_steps"]) >= 3, card["instructions_steps"])
    check("no existing field removed",
          _EXISTING_FIELDS.issubset(card.keys()),
          f"missing: {_EXISTING_FIELDS - set(card.keys())}")


def test_normalize_slot_empty_instructions():
    print("\n── _normalize_slot: empty instructions → []")
    slot = _make_slot("")
    card = _normalize_slot(slot, session_id="sess-1", day="Monday", slot_index=1)
    check("instructions stays ''", card["instructions"] == "", repr(card["instructions"]))
    check("instructions_steps is []", card["instructions_steps"] == [], card["instructions_steps"])
    check("no existing field removed", _EXISTING_FIELDS.issubset(card.keys()),
          f"missing: {_EXISTING_FIELDS - set(card.keys())}")


def test_feedback_id_stable_and_present():
    print("\n── _normalize_slot: feedback linkage `id` present and deterministic")
    slot = _make_slot("Do one small step. Celebrate it.")
    a = _normalize_slot(slot, session_id="sess-9", day="Tuesday", slot_index=2)
    b = _normalize_slot(slot, session_id="sess-9", day="Tuesday", slot_index=2)
    check("card has `id`", bool(a.get("id")), a)
    check("`id` is deterministic for same session/day/slot/title",
          a["id"] == b["id"], (a.get("id"), b.get("id")))


def run_all():
    test_splitter_basic_multi_sentence()
    test_splitter_decimal_not_split()
    test_splitter_empty_and_single()
    test_normalize_slot_preserves_instructions_and_is_additive()
    test_normalize_slot_empty_instructions()
    test_feedback_id_stable_and_present()

    print(f"\n{'=' * 50}")
    print(f"Results: {_passed} passed, {_failed} failed")
    if _failed:
        print("❌ instructions_steps tests FAILED")
        sys.exit(1)
    print("✅ All instructions_steps tests PASSED")


if __name__ == "__main__":
    run_all()
