# Care-Team Report Routing (Beta 2.0)

Authoritative policy for how parent notes and activities are routed to care-team
reports. The rules here are mirrored in code (`api/report_generator.py`) and
asserted by tests (`tests/test_care_team_routing.py`). **If you change the
policy, update all three together.**

## 1. Beta 2.0 report types

The frontend offers three report types in Beta 2.0:

| Report type (API value) | Title | Provider bucket |
|---|---|---|
| `doctor` | Doctor Report | `doctor` (sees all) |
| `speech_therapist` | Speech Therapist Report | `st` |
| `ot_pt` | Occupational / Physical Therapy Report | `ot_pt` |

Legacy report types remain valid and backward-compatible:

| Legacy report type | Provider bucket used |
|---|---|
| `occupational_therapist` | `ot_pt` |
| `physical_therapist` | `ot_pt` |

`REPORT_TYPE_TO_PROVIDER` in `api/report_generator.py` is the source of truth.

## 2. Decision: combine OT/PT for now

For Beta 2.0, occupational therapy and physical therapy share a single combined
report (`ot_pt`). They are **not** separated yet (see roadmap below).

## 3. Two distinct concepts

- **Activity default relevance** — *suggested by Genex* from the activity's
  domain/subdomain. Tells which report(s) an activity is naturally relevant to.
  Implemented read-time as `activity_relevance_providers()` / `DOMAIN_RELEVANCE`.
  It is a suggestion only and is **not** stored on activities.
- **Parent note visibility** — *explicitly controlled by the parent* via the
  note's care-team tag(s). This is the hard rule that decides which report a
  parent note appears in. Implemented as `compute_note_visibility()` /
  `note_visible_in_report()`.

Parent note visibility **always governs** whether a note is shown. Activity
relevance never widens or narrows a note's visibility.

> Example: a color-learning (language-based cognitive) activity is *relevant* to
> Doctor + ST. But if the parent writes a note and tags only Doctor, that note
> appears **only** in the Doctor report.

## 4. Note visibility matrix

A note's computed visibility set is resolved in this order:

1. explicit `care_team_tags` (subset of `doctor`, `st`, `ot_pt`), else
2. legacy `care_team_member` mapped to a tag (see §7), else
3. `doctor` only.

A flagged note appears in a report when:

| Report | Shows the note when… |
|---|---|
| `doctor` | always (comprehensive) |
| `speech_therapist` | `st` ∈ visibility set |
| `ot_pt` (and legacy OT/PT) | `ot_pt` ∈ visibility set |

| Note tagged | Doctor | ST | OT/PT |
|---|:--:|:--:|:--:|
| `doctor` only | ✅ | ❌ | ❌ |
| `st` only | ✅ | ✅ | ❌ |
| `ot_pt` only | ✅ | ❌ | ✅ |
| `doctor` + `st` | ✅ | ✅ | ❌ |
| `doctor` + `ot_pt` | ✅ | ❌ | ✅ |
| `st` + `ot_pt` | ✅ | ✅ | ✅ |
| untagged (flagged) | ✅ | ❌ | ❌ |

## 5. Doctor-sees-all rule

The Doctor report always includes every flagged parent note, regardless of tag.
This preserves the Beta 1.0 comprehensive Doctor report exactly.

## 6. ST / OT-PT isolation rules

- A note tagged only `st` must **not** appear in the OT/PT report.
- A note tagged only `ot_pt` must **not** appear in the ST report.
- A `doctor`-only note must **not** appear in ST or OT/PT unless also tagged for
  that provider.

## 7. Legacy `care_team_member` mapping

Older feedback records used a single `care_team_member`. It maps to a tag:

| `care_team_member` | tag |
|---|---|
| `Doctor` | `doctor` |
| `ST` | `st` |
| `OT` | `ot_pt` |
| `PT` | `ot_pt` |

The legacy field is **kept** (not removed/renamed). New records may carry both
`care_team_member` and `care_team_tags`; when present, `care_team_tags` wins.

## 8. Untagged flagged note → doctor-only

A flagged note with neither `care_team_tags` nor a recognized `care_team_member`
is visible to the Doctor report only. This is the safe default ("unclear →
Doctor"), and the parent can re-tag it to ST or OT/PT to share more widely.

## 9. Domain / subdomain → provider relevance policy

Suggestion layer only (`DOMAIN_RELEVANCE` + subdomain hints). Doctor is always
relevant.

**ST-relevant:** language & communication; speech/language; receptive/expressive
language; social communication; joint attention, turn-taking, requesting,
naming, imitation, pretend play with communication; cognitive **when
language-based** (naming colors, identifying objects, "find the red block",
what/where questions, understanding concepts).

**OT/PT-relevant:** movement/physical; fine motor, gross motor, coordination,
motor planning; adaptive/self-help; sensory/regulation; transitions, tolerance,
body regulation, playground participation; attention/focus **when
regulation/visual-motor/sequencing/adaptive**; cognitive **when
visual-motor/attention/adaptive/motor-planning** (matching shapes by hand,
puzzles, sorting, staying with a task, sequencing routine steps).

**Edge rules:**
- social communication → `st` + `doctor`
- social regulation / sensory participation → `ot_pt` + `doctor`
- cognitive language-based → `st` + `doctor`
- cognitive attention / visual-motor / adaptive → `ot_pt` + `doctor`
- unclear cognitive/social → `doctor` by default; the parent's manual note tag
  controls note visibility.

## 10. Future roadmap (NOT in Beta 2.0)

- Separate OT and PT into distinct reports.
- Add psychologist.
- Add teacher / developmental specialist / ABA / genetics / neurology / early
  intervention reports.

None of these are implemented yet. Keep Beta 2.0 limited to `doctor`,
`speech_therapist`, and `ot_pt`.
