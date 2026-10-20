# Clinical review — candidate activity content

Track 2's workspace. Candidate activity cards are drafted here, reviewed by a
clinician here, and only then promoted into the activity bank's admitted input.

**Nothing in this directory is approved, released, or in use.**

## Why this sits at the repository root and not under `pilot_runtime/`

Two independent reasons, both measured rather than assumed.

**It would ship.** All four container build contexts
(`pilot_runtime/deploy/gcloudignore{,-generation,-projection,-bootstrap}`)
ignore everything and then re-admit `pilot_backend/**` and `pilot_runtime/**`
wholesale, excluding only tests and `__pycache__`. Candidate content placed
under `pilot_runtime/` would therefore be copied into the PHI-serving browser
image — unreviewed clinical material inside the serving container, for no
reason. At the repository root it is excluded from every image automatically.

**The ledger could not be written there.** The CI no-mirror gate forbids six
canonical activity-family identifiers anywhere under those two trees, exempting
only two named constants in the activity-bank generator. A ledger row reading
`"activity_family_ref": "early_vocalizations"` would fail that gate. Here it is
simply data.

## Layout

## Scope of this checkpoint

Three in-scope families, all candidates, none approved, none promoted:

| Family | Canonical rungs | Candidate cards |
|---|---|---|
| `expressive_first_words` | 12m, 15m, 18m | 7 |
| `early_vocalizations` | 2m, 4m, 6m ×3, 9m | 6 |
| `action_picture_labeling` | 36m | 5 |

Counts are **derived, not quotas**. `early_vocalizations` is 6 rather than 7
because two proposed concepts — routine-embedded cooing and routine-embedded
babble — turned out to be one activity at two rungs and were merged rather than
kept apart to reach a number.

### A content constraint imposed by a machine rule

`action_picture_labeling` cannot use the most natural action-picture verbs.
`activity_validator._MOTOR_GAME_PATTERN` makes `jump`, `hop`, `toss`, `throw`,
`catch`, `race` and `climb` **critical failures** in the instructions of any
language-domain card — and the single rejected card in the live bank
(`_BUCKET_VARIANTS[expressive_word][7]`, "Action Word Match") is an
action-labeling card that failed exactly this way.

The five candidates therefore use `eating`, `washing`, `reading`, `drinking`,
`playing` and `sleeping`. All three verbs in the canonical rung text itself —
running, eating, playing — are safe.

**If the reviewing clinician needs a blocked verb, that is flagged as a
separate decision and never resolved by weakening the validator.** The ledger
note on card 1 asks for exactly that confirmation.

## Layout

```
clinical_review/
  candidates/<family>.json     nine-field cards; NOT the generator's input
  review_ledger.json           the sidecar: clinical status, keyed by template id
  tools/ledger.py              every ledger rule, in one place
  tools/build_review_packet.py renders the clinician's packet
  tools/promote_approved.py    the only route into the bank; human-run
  out/                         generated packets. GITIGNORED, never committed.
```

The filename stem is the canonical activity family, matching the convention the
activity-bank generator already uses for `pilot_runtime/data/reviewed_cards/`.
Nothing lists the families; they are derived from the filenames.

## The schema is the production schema

Candidate cards use **exactly** the nine fields in
`pilot_backend/domain/activity_template.py::CARD_FIELDS`, and nothing else.
There is no `review_status` field, no difficulty tier, no duration and no
frequency — a card file that carries an extra key is refused here and would be
refused by the generator too.

Review status is a **sidecar** because `activity_template_id` is a digest over
the family plus those nine fields. Adding a tenth would change the id of every
template that already exists, along with the artifact digest and any evidence
row naming a template.

`source_tier` is **not** review status. It records where a card's words came
from (`family_curated` / `bucket_curated`) and is deliberately not renamed to
anything resembling clinical sign-off. Origin and approval are different claims
and live in different places.

## The lifecycle

```
generated_candidate → clinician_edited → clinician_approved → promoted
                   ↘ rejected
```

**Approval names exact bytes.** Change one word and the template id changes, so
the edited card has no approval and cannot be promoted; the old approval still
refers to wording nobody will ship. "Approve with edits" needs no special
handling — the edited card simply gets a new id, a new entry, and a
`supersedes` pointer back. There is no transfer step, so there is nothing to
get wrong.

## Workflow

```bash
# 1. render the packets (writes to clinical_review/out/, gitignored)
PYTHONPATH=. python -m clinical_review.tools.build_review_packet

# 2. export the packets to the reviewing clinician; record each decision in
#    review_ledger.json. A clinician_approved entry needs reviewer + decided_on.

# 3. see what would be promoted. Dry run is the DEFAULT.
PYTHONPATH=. python -m clinical_review.tools.promote_approved
PYTHONPATH=. python -m clinical_review.tools.promote_approved --write

# 4. regenerate the bank; every existing gate then runs unchanged
PYTHONPATH=genex-parent python -m pilot_runtime.integration.activity_bank_generator
```

### Clinical approval does not bypass machine validation

Promotion only moves bytes into the generator's input directory. The nine-field
check, Parent's real `validate_activity`, the template-id computation and the
CI drift gate all run afterwards exactly as before. An approved card whose
`make_harder` says "climb higher" still fails the build. Both checks are
necessary; neither is sufficient.

## Packets are never committed

A packet shows the clinician verbatim canonical milestone text and concrete
`rung1:` references so a decision is made against the clinical target rather
than against a card in isolation. Both are forbidden under `pilot_backend/` and
`pilot_runtime/`, so packets render to `out/`, which is gitignored, and
`assert_outside_guarded_trees` refuses any destination under either tree —
checked on the resolved path, so `../pilot_runtime/x` is caught too.

The durable record is the **ledger**: template ids, statuses, reviewer, date,
and no milestone prose at all. A content-addressed id already identifies
exactly which wording was approved.

## `legacy_unresolved` is a migration register, not a status

The 18 cards already admitted under `pilot_runtime/data/reviewed_cards/`
predate this ledger. There is no evidence in the repository that a clinician
explicitly approved them, and presence in a directory is not evidence. They are
listed in `legacy_unresolved`, which asserts exactly one thing:

> this template was admitted before explicit clinical-review provenance existed

It never implies approval by Hannah or anyone else. The register **may shrink**
as legacy cards receive real review; it **must never grow**. CI pins the
migration baseline and fails on any addition, so a newly admitted card cannot
be filed as "unresolved" instead of being approved.

## One family serves several rungs — read this before judging difficulty

The activity bank selects content by **family**, never by rung: the nine-field
schema has no rung field, and `templates_for_families` returns every template
for the families a goal's canonical anchor binds. So a family spanning a wide
developmental range serves that whole range from one pool.

`early_vocalizations` binds six rungs from 2 to 9 months. A child whose target
is the 4-month cooing rung and a child whose target is the 9-month babble rung
receive the **same** candidate pool. Each card must therefore be safe and
useful across the whole span, with `make_easier` and `make_harder` carrying the
range *inside* the card.

### This is an explicit clinical-review requirement, not a note

Founder decision: **the reviewing clinician must confirm that every
`early_vocalizations` card is clinically safe and useful across the full 2–9
month family span, not merely at its nominal anchor.** A card that is only
right at its anchor must be rejected or rewritten, because there is currently
no mechanism to restrict it to that rung.

The review packet renders this as a bordered **Review requirement** block on
every multi-rung family, naming the rung count and the span in months. It is
deliberately absent on single-rung families such as `action_picture_labeling`,
where there is no span to confirm; a test asserts both the presence and that
absence, so the block cannot decay into boilerplate that appears everywhere
and is therefore read nowhere.

### Future architecture item — not a pilot blocker

**Rung-level activity eligibility / filtering** would let a goal select only
the cards appropriate to its target rung, instead of every card its families
bind. That would need a rung dimension the card schema does not have, and
adding one to `CARD_FIELDS` would rewrite every existing template id — so it
is a versioned decision of its own, not an incremental change.

Recorded here as a possible future direction. **Not built in Track 2, and not
blocking the pilot**: the in-card `make_easier` / `make_harder` range plus the
clinician's span confirmation is the approved strategy for now.

## Known dependency, not addressed here

Weekly allocation is deterministic and takes **no prior-cycle input**:
`pilot_backend/weekly/allocator.py::allocate()` has no history parameter, and
`difficulty_tier` has no consumer anywhere in `pilot_backend/` or
`pilot_runtime/`. With the same goal and the same approved pool, Week 2 places
the same activities as Week 1, however large the pool is. Extra approved
content is necessary for rotation but not sufficient.

Deferred to the later pilot plan by founder decision. **The allocator is not
modified by Track 2.**
