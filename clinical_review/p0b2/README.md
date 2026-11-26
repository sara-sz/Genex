# P0-B2 clinician-review package (candidate, offline)

**Status:** review material only. Nothing here is read by `pilot_backend` or the
serving image, no runtime behaviour changes, and nothing is approved. Generated
from deployed source `65c68d4` (staging `pilot-api-staging-00017-qmj`).

Regenerate (deterministic; the test suite fails if these files drift):

    python clinical_review/tools/build_p0b2_ladder_package.py
    python -m pytest pilot_runtime/tests/test_p0b2_ladder_package.py -q

| File | Deliverable |
|---|---|
| `ladder_talking_candidate.json` | 1 — candidate ladder: nodes, edges, flags |
| `activity_mapping_candidate.json` | 2 — `activity_template_id` → candidate nodes |
| `coverage_candidate.json` | 3 — node → reviewed cards, capacity, gaps |
| `hannah_review_sheet.md` | 4 + 5 — baseline mapping, review sheet, envelope |

## Scope

Talking & Communicating · `expressive_language` · 12–36 months, around the
Pilot's 24m two-word target (reference anchor
`rung1:feb590cf2788978b383c11062ce67c1b`, never modified). 29 nodes over 11
milestones. Read only from frozen sources: the Gold Standard loader
(`genex_core.milestones.get_cdc_df`), the taxonomy alias resolver, the deployed
rung table and the 30-card bank.

## Node identity

`node1:` = sha256(`genex-ladder-node-v1` | `rung1:` of the milestone |
`bridge`/`endpoint` | `bridge_step_number` or `endpoint` | wording normalised
exactly as `rung1:` normalises milestone text)[:32]. Separate from `rung1:`,
which keeps its milestone-level meaning.

Every CDC milestone has an explicit ENDPOINT node: the workbook's final bridge
row when it IS the milestone text (8 of 11 here), otherwise appended with the
existing CDC `milestone` cell (3 of 11: 12m, 15m, 30m "…with one action word").

## Confidence vocabulary

* **STRUCTURED** — inside one milestone, by `bridge_step_number`.
* **TEXT_LINKED** — across milestones: the first step's `previous_bridge_step`
  matches EXACTLY one ENDPOINT at the stated `previous_anchor_age`.
* **NEEDS_CLINICIAN_REVIEW** — anything else (branch, gap, no match, a link that
  names a mid-milestone bridge, parallel milestones), with the alternatives.

## Key findings

1. **The positive child's milestone (24m) is fully structured inside itself:**
   Bridge 1 → Bridge 2 → Bridge 3 → endpoint, and the workbook's per-step
   families make Bridges 1–2 `expressive_vocabulary_growth` (12 cards) and
   Bridge 3 + endpoint `two_word_phrases` (6 cards).
2. **One node below 24m Bridge 1 is the 18m endpoint** ("Tries to say three or
   more words besides mama or dada", TEXT_LINKED) — **0 reviewed cards**
   (`expressive_first_words`). 7 *unreviewed* candidates already exist in
   `clinical_review/candidates/expressive_first_words.json`.
3. **Above the 24m endpoint the workbook branches** (36m action-picture, 48m
   expansion); and the 30m "two words with an action word" chain is linked to
   the 24m endpoint only by a mid-chain cell. Hannah must choose.
4. **The 18m endpoint also branches** (24m two-word chain vs 30m "about 50 words").
5. **No card has a unique structured node.** Each family spans 2–4 nodes, so all
   card placements need Hannah. Adjacent bridges sharing a family (24m B1/B2;
   B3/endpoint) would show the SAME cards unless Hannah splits them — without a
   split, a one-node move could change nothing the family sees.
6. **Appended endpoints have no workbook row and so no family**: they hold no
   card until Hannah maps one explicitly.
7. Several 30m/36m nodes name families that do not resolve in the taxonomy
   (`pronouns`, `wh_question_asking`, `expressive_name_response`).

## Deliverable 6 — implementation readiness

### Can implement immediately after Hannah approves

* Freeze the approved ladder + card→node mapping as a runtime artifact
  (generator in `pilot_runtime/integration/`, data in `pilot_runtime/data/`),
  same pattern as the bank and rung table.
* Plan envelope (min / start / max `node1:`) on the clinical plan at
  activation — `pilot_backend/domain/monthly_plan.py` (or a sidecar record),
  `planning/service.py`, the plan routes in `transport/wsgi_app.py`.
* Decision fields `node_before` / `node_after` / evidence ids / rule version in
  `domain/adaptation_decision.py`; policy in `weekly/repeated_signal_policy.py`
  (too_hard×2 → −1, too_easy×2 → +1, disengagement → same node, wasnt_ready →
  none, opposite repeated signals → none + review); node-scoped selection in
  `integration/adaptive_week.py`; `ladder_node_ref` (internal, forbidden in the
  Parent view) in `domain/weekly_plan_document.py`; codecs.
* Option A envelope (24m Bridge 1 … 24m endpoint) needs NO new cards.

### Still requires content / data / product decision

* Hannah: the BRANCH/GAP transitions above; the card→node split (finding 5).
* Reviewed `expressive_first_words` cards (review the 7 existing candidates) to
  allow any move below the 24m milestone (Option B).
* Baseline bridge questions (sheet section E) — product + clinical approval,
  then Parent onboarding + projection changes (`genex-parent` baseline v2 and
  `domain/parent_baseline_projection_v2.py`); until then Hannah sets the
  starting node.
* Therapist Dev UI for the envelope; CI step to run this package's test
  (hosted CI runs `pilot_runtime` tests file-by-file).
