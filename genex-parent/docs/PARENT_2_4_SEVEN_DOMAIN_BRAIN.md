# Parent 2.4 — Seven-Domain-Native Brain (Phase 0.3B)

**Fictional/dev work. No Firestore, no Firebase Auth, no RTM, no real data, no
production change, no API consumer switched, no frontend touched.**

Branch `feature/parent-2.4-0.3b-seven-domain-brain`, from
`parent-2.4-0.3a-hosted-ci` → `489582e5952542333baffa21113cf8c000736cd8`.

## OLD → NEW

```
BEFORE (Beta 2.3 Brain)              AFTER (Parent 2.4 Brain)
─────────────────────────            ──────────────────────────────────
language_and_communication      →    talking_and_communicating
social_and_emotional            →    social_and_emotional
cognitive                       →    learning_and_thinking  (minus 9 rows)
movement_and_physical           →    fine_motor  +  gross_motor  +  daily_living
                                →    sensory  (canonical, content PENDING)
4 domains                            7 domains
```

This is **not a display rename**. `DOMAIN_CONFIG` is iterated in 13 places
across the Brain, and `category_key` is what every module groups by — so
widening both makes milestone selection, focus ranking, scheduling, support
tiers, progress and summaries seven-domain-native where they are built.

## Exact 369-row distribution

| Canonical domain | Rows | From legacy |
|---|---|---|
| `talking_and_communicating` | 83 | language_and_communication |
| `social_and_emotional` | 89 | social_and_emotional |
| `learning_and_thinking` | 84 | cognitive **− 9** |
| `fine_motor` | **29** | movement_and_physical |
| `gross_motor` | **49** | movement_and_physical |
| `daily_living` | **35** | movement_and_physical (26) + **cognitive (9)** |
| `sensory` | **0** | — |
| **Total** | **369** | |

Counts are asserted from the workbook, never hardcoded in the mapping.

## Fine / Gross split

`fine_motor` ← `fine_motor_hand_use` (29).
`gross_motor` ← `gross_motor_mobility_and_coordination` (28) +
`postural_control_and_transitions` (21).

They are disjoint at subdomain level, which the suite asserts directly.

## Daily Living extraction

`daily_living` ← `self_help_motor_skills` (26, from Movement) +
`adaptive_feeding_cues` (6) + `safety_awareness` (3) — the last two from
**Cognitive**. Daily Living is the subtle case: it is the only domain drawing
from *two* historical categories, which is why `learning_and_thinking` is 84
rather than 93.

## Sensory: content-pending, fails closed

`sensory` is a native domain with `has_content = False` and
`content_status = "pending"`. Enforced, not merely documented:

* zero rows carry it (`canonical_domain` never equals `sensory`);
* `get_category_questions("sensory", …)` returns `[]` — no fabricated milestones;
* `delay_engine.DOMAIN_KEYWORDS` has **no sensory list**, and
  `estimate_all_delays` filters to `CONTENT_READY_DOMAIN_KEYS`, so no delay
  anchor is produced for a domain Genex cannot plan;
* `emotional_regulation` still maps to `social_and_emotional` — the pre-2.4
  behaviour of routing sensory concerns there is not reintroduced.

Nothing was invented and nothing borrowed.

## Motor scoring preserved — by construction

`MOTOR_EMERGING_SUBDOMAINS` = `{postural_control_and_transitions,
gross_motor_mobility_and_coordination, fine_motor_hand_use}`, unchanged.
Weights unchanged: **0.70 / 0.25** motor vs **0.45 / 0.40** general.

The reason the split could not disturb them is structural:
`scoring._band_has_motor_emphasis` keys on **`subdomain`**, never on the domain
(`scoring.py` references `MOTOR_EMERGING_SUBDOMAINS` and never imports
`DOMAIN_CONFIG` — asserted by AST). Subdomains are untouched by this phase, so
the same rows receive the same treatment before and after, and motor emphasis
still fires for Fine Motor *and* Gross Motor now that they are separate domains.

No clinical scoring semantics were changed.

## Components changed

| File | Change |
|---|---|
| `genex_core/config.py` | `DOMAIN_CONFIG` derived from `parent_taxonomy.domains` (7 keys + `has_content`); `CONTENT_READY_DOMAIN_KEYS`; legacy bundle renamed `LEGACY_ALIAS_TO_CATEGORY` + `LEGACY_DOMAIN_KEYS` + `is_legacy_domain_key()`; `ALIAS_TO_CATEGORY` kept as an alias of the legacy map |
| `genex_core/table_loader.py` | Prefers the Parent 2.4 candidate workbook; `category_key` **read from** `canonical_domain`; `legacy_category_key` retained as provenance; archival workbook remains a fallback |
| `genex_core/delay_engine.py` | `DOMAIN_KEYWORDS` split across Fine/Gross/Daily Living; self-care terms moved out of Cognitive; **no sensory list**; `estimate_all_delays` filters to content-ready |
| `genex_core/support_tiers.py` | Language tier rule + fallback keyed on `talking_and_communicating` |
| `genex_core/interview_engine.py` | Cognitive-strength suppression keyed on `learning_and_thinking` |

The last three were **silent-failure bugs in waiting**: left on legacy spellings
those rules would never have fired again, which reads as "no concern detected"
rather than as an error.

### Activity validator — converted after founder clinical review

`genex_core/activity_validator.py` carried `FAMILY_TO_CATEGORY`, a hard-coded map
from 56 **activity families** to a single legacy domain each. Re-classifying
those families was a **clinical content decision**, not a mechanical taxonomy
change, so it was isolated and exported for founder review rather than guessed.
The reviewed result is now the source of truth and the dict is **retired**.

**Why it had to change.** Only `social_and_emotional` is spelled the same in both
vocabularies, so under the seven-domain Brain **45 of the 56 families** could
never equal their canonical `category_key`. `activity_family_category_mismatch`
is a CRITICAL warning, so each one flipped `is_valid` to False and blocked every
card that used it.

**What replaced it.** `data/parent_2_4/activity_family_taxonomy_v1.xlsx`
(founder-reviewed) plus `parent_taxonomy/activity_families.py`:

| | |
|---|---|
| Families | 56 (`talking` 17 · `social` 12 · `learning` 9 · `gross_motor` 7 · `fine_motor` 6 · `daily_living` 5 · **`sensory` 0**) |
| Multi-domain | 27 of 56 carry a secondary domain |
| Allowed domains | **derived**: `{primary} ∪ secondaries` — never stored twice |
| Aliases | read from the `activity_family_aliases` sheet (`helper_context` → `helper_role_chores`), not hard-coded |
| Disciplines | SLP/OT/PT kept on a **separate axis**; never treated as domains |

The mismatch check is now a **membership** test, which is what makes multi-domain
families expressible at all: `buttoning_fasteners` is Daily Living *and* Fine
Motor; `conversation_turn_taking` is Talking *and* Social. A single-valued map
structurally cannot say that.

It is not a blanket permit — `buttoning_fasteners` + `gross_motor` still blocks,
and the suite sweeps all 56 families across every domain they do *not* serve.

**Scope.** The taxonomy covers exactly the 56 families the retired dict knew, not
the 128 distinct `activity_family` values in the Gold Standard. An unknown family
stays **permissive**, matching the old `.get() → None` behaviour; widening the
blocker to unknown families would be a new and much broader rule.

Fail-closed at load, permissive at lookup — a malformed workbook raises, a
legitimately unknown family does not.

### Two further legacy-keyed rules found during the migration

`activity_validator` rules 5 and 6 were still keyed on legacy spellings:

* rule 5 (`language_card_contains_motor_game`, **critical**) tested
  `category_key == "language_and_communication"` — it would never fire again;
* rule 6 (`non_movement_card_contains_motor`) tested
  `!= "movement_and_physical"` — it would fire on *every* card, including
  genuine motor ones.

Both are now derived from `parent_taxonomy` rather than restated:
`resolve_legacy_domain("language_and_communication")` and
`LEGACY_AMBIGUOUS["movement_and_physical"]` (= Fine/Gross/Daily Living). Same
class of silent-failure bug as `delay_engine` / `support_tiers` /
`interview_engine`.

## Repair passes 1 and 2 — the regressions 0.3B introduced

The first 0.3B commit converted every site that *iterates `DOMAIN_CONFIG`*.
It missed sites that hard-code domain **string literals**, which a
`DOMAIN_CONFIG`-shaped audit cannot see. `tests/test_regression.py` fell from
41/41 to 19/41 and the audit separated two causes: 9 failures were legacy test
inputs, **13 were real runtime regressions**.

### Runtime repairs

| Module | Bug | Fix |
|---|---|---|
| `safety.py` 388 / 419 | jump/stomp/climb hard-block **and** the stable-support marker were gated on `movement_and_physical` — dead for every high-fall/seizure profile | `_FALL_RISK_DOMAINS = {"gross_motor"}` |
| `interview_engine.choose_focus_domains` | Fine + Gross consumed both focus slots, silently dropping an explicit speech concern | second-slot diversity rule |
| `final_plan_gate.SAFE_FILLER_CARDS` | all 11 carried legacy keys, so domain-matched filler never matched | migrated via each card's `activity_family` |
| `activity_engine._DOMAIN_WHY` | legacy-keyed, so **all seven** domains fell back to generic parent copy | 6 canonical keys |
| `activity_engine._family_bucket` | legacy-keyed domain fallback | seven-domain native |
| `scoring.py` 221/223/312 | language split scoring only applied to the legacy key | canonical, math byte-identical |
| `bridge_selector.py` 121 | language-specific regression detection could never fire | canonical |
| `scheduler.py` | a narrower canonical bank exhausted the core-only Week-1 pool and repeated a title | try real variant cards before repeating |

**Fall risk is Gross Motor only.** Legacy Movement split three ways, but these
rules are about locomotor risk — jump, hop, climb, race. Fine Motor (table-top
hand use) and Daily Living (self-help routines) do not inherit them simply
because they descend from the same historical bucket. This narrows the
stable-support marker relative to Beta 2.3; that is a founder decision, recorded
rather than silent.

**Focus diversity is selection only.** When both slots would go to the
fine/gross pair and another explicit concern exists outside it, slot 2 goes to
that concern. `max_domains` stays 2, the primary is still the top-ranked domain,
no score changes, Daily Living is not a sibling, and Fine and Gross remain two
distinct domains. Chao ("speech delay, OT delay, PT delay") goes from
`[gross_motor, fine_motor]` to `[gross_motor, talking_and_communicating]`, with
`fine_motor` recorded in `noted_concerns`.

### The regression suite was vacuous, not merely failing

A legacy domain key yields **zero** questions and **zero** activities, so a test
that fed one and then iterated asserted nothing and still printed `✓`. Ten of
the nineteen "passing" tests were green for that reason —
`test_case18_dravet_stomp_squat_blocked`, a Dravet safety test, looped over an
empty list. Migration was therefore not cosmetic: it made those tests execute
for the first time under the seven-domain Brain.

`tests/test_regression.py` is now **41/41**, migrated by intent rather than by
find-and-replace — locomotor sites to `gross_motor`, hand-use to `fine_motor`,
and "a motor domain" assertions to membership over
`{fine_motor, gross_motor, daily_living}`, which is exactly equivalent to the
old single-domain test. No test was deleted, skipped, xfailed, or had a count
lowered. `tests/test_parent_24_regression_nonvacuity.py` pins the property so
the suite cannot go quiet again.

The suite is now part of the hosted Parent 2.4 gate.

## No duplicate source of truth

`DOMAIN_CONFIG` is **derived** from `parent_taxonomy.domains`, asserted by test.
Dependency direction `genex_core → parent_taxonomy` is acyclic — `parent_taxonomy`
imports nothing from `genex_core` (its only mentions are docstrings).

## Provenance and immutability

The original Gold Standard remains **byte-identical**
(`c2b6735d9f099c916973c98c1953e7eb1f2ca8e1a60430011f805a3bf9c3487c`) and is
never written; its 159→369 bridge builder is unrecoverable. The Brain loads the
**candidate** workbook — the same 369 rows plus `canonical_domain` — and keeps
`category` / `legacy_category_key` so every row can be traced back to its
pre-2.4 classification.

## Legacy compatibility boundary

Legacy keys remain **recognisable** (`LEGACY_DOMAIN_KEYS`,
`is_legacy_domain_key`, `LEGACY_ALIAS_TO_CATEGORY`) but are never produced by
new Brain code, and **no projection is performed here**.

`movement_and_physical` is legacy-only because it genuinely spanned Fine Motor,
Gross Motor and Daily Living; `resolve_legacy_domain` returns `None` for it
rather than guessing. Legacy `cognitive` is likewise not equivalent to
`learning_and_thinking`, since it historically contained 9 Daily Living rows.

**Historical stored sessions are not migrated or rewritten.** The full read
projection at the session boundary is **PARENT-0.3C**.

## Deferred to PARENT-0.4

Functional baseline is untouched by design: first-question calibration,
`compute_dev_age_from_answers` fallback, the diagnosis-derived starting anchor
and interview sequencing all behave exactly as before. This phase is taxonomy
architecture only — changing both at once would make a scoring regression
impossible to attribute.

## Not implemented

Sensory clinical content · SLP/OT/PT disciplines · API consumer switch ·
legacy read projection · Parent frontend · multi-child · multi-caregiver ·
Firebase · Firestore · RTM · deployment · real data.
