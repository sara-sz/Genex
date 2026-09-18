# Parent 2.4 — Taxonomy Foundation (Phase PARENT-0.2)

**Fictional/dev work. No Firestore, no Firebase Auth, no RTM, no real data, no
production change.** Nothing in this phase switches a runtime consumer.

Branch `feature/parent-2.4-taxonomy-foundation`, worktree
`/Users/sara/Projects/Genex-worktrees/genex-parent-2.4`, created from the frozen
therapist checkpoint `therapist-alpha-0.7.4-private-note-write` →
`7d1257b60aba5d150e9595da92a9b7d0b65e5614`.

## 1. Source snapshot + SHA

The authoritative runtime Gold Standard, treated here as an **immutable input
snapshot** and never written:

```
genex-parent/data/cdc_milestones_with_bridges_family_cleaned_final_app_ready.xlsx
sha256    c2b6735d9f099c916973c98c1953e7eb1f2ca8e1a60430011f805a3bf9c3487c
bytes     44,682
git blob  29bbd8b3657324d1c2d1a32b61b57063c4d7cd66
sheets    all_with_bridge_family (369×10) · subdomain_legend (25×3) · activity_family_legend (134×2)
```

Historical categories: `movement and physical` 104 · `cognitive` 93 ·
`social and emotional` 89 · `language and communication` 83. 24 distinct
subdomains, zero nulls.

## 2. Provenance and the missing builder

```
milestone-cdc-table.xlsx                        159×3  original CDC import
  └─ milestone-cdc-table-improved-subdomains.xlsx  159×4  +subdomain   ┐ 3 byte-identical
  └─ …-advisor.xlsx                                +review sheet       │ copies, sha
  └─ genex-parent/data/cdc_milestones.xlsx         (orphan)            │ 59fe6837…
  └─ genex-alpha/data/cdc_milestones.xlsx          (orphan)            ┘
        ↓  bridge expansion 159 → 369  ← BUILDER NOT RECOVERABLE
cdc_milestones_with_bridges_family_cleaned_final_app_ready.xlsx  369×10  ★ RUNTIME
```

**Builder recovery result: NOT FOUND.** Exhaustive read-only search:

| Search | Result |
|---|---|
| Current tree for a generator | no `.py` anywhere writes an `.xlsx` |
| `git log --diff-filter=A` for `*bridge*`/`*app_ready*` | workbook added by `16b54b4` *"Add Genex Brain v22 source assets"* alongside one notebook — as a **binary asset** |
| `git log --diff-filter=D` for builder patterns | none deleted |
| Notebooks referencing `app_ready`/`with_bridges`/`bridge_step` | **zero** |
| Ancestor workbooks (`cdc_milestones_with_bridges*.xlsx`) | existed in history at `93b4cd0`, absent from the tree |

The 369-row table was produced **outside this repository** and imported. Per the
phase instruction, no attempt was made to reconstruct 369 rows from the 159-row
ancestor. **The workbook is therefore the immutable input snapshot**, and
preserving it byte-exact is non-negotiable: it is the only copy of work that
cannot currently be rebuilt.

## 3. The seven-domain contract

Defined once, in `genex-parent/parent_taxonomy/domains.py`
(`TAXONOMY_VERSION = "parent-2.4-domains-v1"`):

| Order | Key | Display | Content |
|---|---|---|---|
| 1 | `talking_and_communicating` | Talking & Communicating | available |
| 2 | `social_and_emotional` | Social & Emotional | available |
| 3 | `learning_and_thinking` | Learning & Thinking | available |
| 4 | `fine_motor` | Fine Motor | available |
| 5 | `gross_motor` | Gross Motor | available |
| 6 | `daily_living` | Daily Living | available |
| 7 | `sensory` | Sensory | **pending** |

Ordering is part of the contract; consumers must not re-sort. **Movement &
Physical is not a canonical Parent 2.4 domain** — it survives only in the legacy
compatibility layer.

Machine keys follow the founder's locked spelling exactly. No deviation was
required, so none was made.

## 4. Subdomain → domain mapping

`genex-parent/parent_taxonomy/subdomain_map.py`
(`SUBDOMAIN_MAP_VERSION = "parent-2.4-subdomain-map-v1"`). All 24 subdomains map
exactly once; counts below are **derived from the source workbook**, never
hardcoded in the mapping.

| Canonical domain | Subdomain | Old category | Rows |
|---|---|---|---|
| talking_and_communicating | expressive_language | language and communication | 34 |
| | receptive_language | language and communication | 17 |
| | early_vocalization_and_babbling | language and communication | 11 |
| | conversation_narrative | language and communication | 10 |
| | gestural_communication | language and communication | 8 |
| | speech_intelligibility | language and communication | 3 |
| **subtotal** | | | **83** |
| social_and_emotional | social_engagement_and_joint_attention | social and emotional | 49 |
| | peer_interaction_and_social_rules | social and emotional | 20 |
| | attachment_and_separation | social and emotional | 6 |
| | emotional_regulation | social and emotional | 6 |
| | empathy_and_prosocial_behavior | social and emotional | 4 |
| | play_and_symbolic_social_play | social and emotional | 4 |
| **subtotal** | | | **89** |
| learning_and_thinking | concepts_and_following_directions | cognitive | 18 |
| | pre_academic_skills | cognitive | 18 |
| | exploration_and_object_use | cognitive | 17 |
| | attention_and_processing | cognitive | 16 |
| | imitation_and_play_skills | cognitive | 8 |
| | object_permanence_and_problem_solving | cognitive | 7 |
| **subtotal** | | | **84** |
| **fine_motor** | fine_motor_hand_use | ⟵ movement and physical | 29 |
| **subtotal** | | | **29** |
| **gross_motor** | gross_motor_mobility_and_coordination | ⟵ movement and physical | 28 |
| | postural_control_and_transitions | ⟵ movement and physical | 21 |
| **subtotal** | | | **49** |
| **daily_living** | self_help_motor_skills | ⟵ movement and physical | 26 |
| | adaptive_feeding_cues | ⟵ **cognitive** | 6 |
| | safety_awareness | ⟵ **cognitive** | 3 |
| **subtotal** | | | **35** |
| **sensory** | *(none — see §6)* | | **0** |
| **TOTAL** | | | **369 ✅** |

**Rows re-parented: 113.** 104 out of `movement and physical` (29 Fine / 49
Gross / 26 Daily Living) and 9 out of `cognitive` (6 + 3 → Daily Living). No
milestone text was re-rated — the subdomain column already encoded these
distinctions.

`learning_and_thinking` = 93 − 9 = **84**, asserted as derived arithmetic rather
than a literal.

## 5. Migration methodology — additive, not destructive

`genex-parent/parent_taxonomy/migrate.py`
(`MIGRATION_VERSION = "parent-2.4-migration-v1"`).

**Chosen representation: keep `category`, add `canonical_domain`.** Considered
and rejected: overwriting `category` in place. Two reasons for the additive
form — `category` is the only in-file record of the pre-2.4 classification, so
overwriting it destroys the lineage evidence that makes the re-parenting
auditable; and with both columns present the migration is row-by-row verifiable
and trivially reversible. Given the builder is unrecoverable, discarding
provenance would have been the wrong trade.

Output: `genex-parent/data/parent_2_4/cdc_milestones_parent_2_4_candidate.xlsx`
(369×11, all three sheets preserved).

Protected columns, proven identical by `verify_candidate` (raw-string compare
after NaN normalisation, so a silent numeric re-typing is still caught):
`months`, `category`, `subdomain`, `milestone`, `parent_explanation`,
`bridge_step_number`, `bridge_step`, `activity_family`, `previous_bridge_step`,
`previous_anchor_age`.

**Fail-closed:** unknown, blank, or non-canonical subdomain targets raise. There
is no default bucket — the pre-2.4 failure mode was exactly that `motor` /
`physical` / `adaptive` quietly folded into `movement_and_physical` /
`cognitive`, which is what kept the collapse invisible.

Determinism: repeated runs produce identical cell content (asserted). Byte-for-byte
XLSX equality is **not** asserted — `openpyxl` embeds a zip timestamp, so the
guarantee is semantic determinism over cell values.

## 6. Sensory content gap

`sensory` is a canonical domain with `ContentStatus.PENDING` and an explicit
`content_note`. The Gold Standard holds **zero** sensory rows, and none were
invented.

Guarded by tests: no subdomain maps to `sensory`; no migrated row claims
`sensory`; `emotional_regulation` maps to `social_and_emotional` and never to
`sensory`. This deliberately ends the pre-2.4 behaviour recorded in
`genex_core/config.py` — *"Sensory concerns map to the closest supported domain
(Social/Emotional regulation) until a dedicated sensory domain exists."*

The model can now distinguish **"this domain exists"** from **"Genex has
validated content for this domain"** via `Domain.has_content` /
`CONTENT_READY_KEYS` / `CONTENT_PENDING_KEYS`. **UI copy is not decided here.**

## 7. Legacy compatibility design (designed, not implemented)

Four pre-2.4 keys remain readable via `LEGACY_DOMAIN_KEYS` and
`resolve_legacy_domain`:

| Legacy key | Resolution | Note |
|---|---|---|
| `language_and_communication` | → `talking_and_communicating` | unambiguous |
| `cognitive` | → `learning_and_thinking` | unambiguous *(historical `cognitive` also contained 9 Daily Living rows; a domain-level read cannot recover that — see below)* |
| `social_and_emotional` | → `social_and_emotional` | identity; the one key both legacy and canonical |
| `movement_and_physical` | → **`None` (ambiguous)** | spanned Fine Motor + Gross Motor + Daily Living |

**`movement_and_physical` deliberately returns `None`, not a guess.** Collapsing
it onto any single 2.4 domain would fabricate a precision the historical
domain-level state never had. `None` means *"ambiguous — do not guess"*;
genuinely unknown keys raise instead, so the two cases stay distinguishable.

Rules for the later implementation:
* Beta 2.3 sessions stay readable; **no destructive migration of production data**.
* Where a historical session recorded **subdomain-level** evidence, that evidence
  may be resolved through `subdomain_map` to recover a precise 2.4 domain.
* Where it recorded only the domain, the ambiguous result must be surfaced —
  **never** a fabricated Fine/Gross/Daily breakdown.
* Same caution applies to `cognitive`: the domain-level mapping to
  `learning_and_thinking` is correct for interpretation, but is not evidence
  that the session contained no Daily Living content.

## 8. Alias / label consolidation plan (designed; consumers NOT switched)

The audit found 2 alias maps and 3 display-label maps. Target shape:

```
parent_taxonomy.domains   ← ONE canonical vocabulary + display labels
parent_taxonomy.domains   ← ONE legacy alias layer (LEGACY_*, resolve_legacy_domain)
        ↑ consumers import; nobody restates strings
```

Consumers to switch in the **next** phase — none changed here:

| File | Current | Action |
|---|---|---|
| `genex_core/config.py` | `DOMAIN_CONFIG`, `ALIAS_TO_CATEGORY` | import; ⚠️ gated on the genex_core freeze decision (§10) |
| `genex_core/table_loader.py` | `_CATEGORY_DISPLAY_TO_KEY` | import; same gate |
| `api/adapters.py:151` | label map | import `display_for` |
| `api/pipeline.py:43` | identical copy | delete, import |
| `api/focus_selector.py:9–12` | **divergent** labels incl. "Fine & Gross Motor & Daily Skills" | import; re-key for 7 domains |

## 9. Future contracts (documentation only)

### 9a. Functional baseline

1. Chronological age indicates **relevance**, not possession.
2. **Observed functional ability** determines starting level.
3. Diagnosis never overrides observed ability.
4. Baseline seeds the selected domain's starting level **before** normal
   milestone-question generation.
5. "No observed answer" must **not** be read as a 6-month developmental age in
   Parent 2.4. *(Today `scoring.compute_dev_age_from_answers` returns `6` for an
   empty answer set.)*
6. Diagnosis-derived delay may remain fallback/context, but **loses precedence**
   once observed data exists. *(Today `delay_engine` sets the anchor from
   diagnosis via LLM; `interview_engine.py:486` falls back to chronological age
   when `dev_age` is empty, which is the case for the first questions in every
   domain.)*
7. Parent 2.4 initial setup focuses on **one** domain.
8. The parent may add another domain later.
9. Genex may later **suggest** another domain.

**No questions were authored in this phase.**

### 9b. Discipline

Developmental domain **≠** provider discipline. Future first-class disciplines:
**SLP**, **OT**, **PT**.

Forbidden identities: `OT = Fine Motor`, `PT = Gross Motor`,
`SLP = Talking & Communicating`. A discipline may support several domains; a
domain may involve several disciplines — so the eventual model is
**many-to-many**, not a column on either side.

Legacy `ot_pt` (Parent care-team note routing, which today collapses OT and PT
into one identity) becomes **compatibility-only**.

**Not implemented in this phase** — guarded by a test asserting no discipline
symbols exist in the taxonomy package.

## 10. Protected systems and the genex_core decision

**`genex_core` was not modified.** The therapist repository-integrity tests
hash-pin `genex-parent/genex_core` **and** `genex-alpha/genex_core` to
`beta-2.1-freeze` (`therapist_api/tests/test_repo_integrity.py`,
`GENEX_CORE_PATHS`), comparing object hashes at the **commit** level.

Placing the new package at `genex-parent/parent_taxonomy/` — a sibling of
`genex_core`, not a module inside it — means:

* every historical integrity assertion is **preserved unmodified**;
* no frozen tag, commit, or freeze metadata is touched;
* the Parent 2.4 lineage can still evolve independently.

This satisfies the phase rule *"if an integrity test is scoped specifically to
frozen therapist history, preserve it and create Parent-specific validation
instead"* without triggering the STOP condition. **No integrity test was
weakened, skipped, or deleted.**

⚠️ **Carried forward:** the moment Parent 2.4 needs `genex_core` itself to
change (switching `DOMAIN_CONFIG`/`table_loader` to import from
`parent_taxonomy`), the freeze-repin decision becomes unavoidable. That is a
founder decision and is **not** made here.

Untouched and verified: Parent Beta 2.3 production · `genex-alpha/genex_core` ·
`genex-parent/genex_core` · all 19 therapist-alpha tags · `develop`/`main`/`prod`
· Therapist Dev/Prod · the root provenance workbooks.

## 11. Intentionally unimplemented

* Functional-baseline questions (contract only)
* Discipline model (contract only)
* Sensory clinical content
* Runtime consumer switching (`genex_core`, `adapters`, `pipeline`, `focus_selector`)
* Beta 2.3 session migration
* Multi-child · multi-caregiver · Firestore · Firebase · RTM
* Parent Dev frontend changes
* Advisor-item resolution (5 open `needs_review` rows)
