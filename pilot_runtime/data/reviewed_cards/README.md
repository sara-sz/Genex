# Clinician-reviewed activity cards — the supply location

This directory is the ONLY place the pilot accepts activity content that does
not already exist in a frozen Parent curated tier.

It exists because the October pilot's first goal binds two activity families
and only one of them has curated content:

| family | source | status |
|---|---|---|
| `expressive_vocabulary_growth` | Parent `_BUCKET_VARIANTS["expressive_word"]` | 13 cards, validator-passing |
| `two_word_phrases` | Parent bucket `sentence` | **no cards exist** |

The Parent engine's generic tier-3 template is NOT an answer. It produces
placeholder prose (`"items for photo sentence (from around the home)"`) that
the Parent validator itself rejects with seven `placeholder_wording`
violations. The generator refuses that tier by construction.

## How to supply cards

Create `two_word_phrases.json` in this directory, containing a JSON array of
card objects. Each card must have EXACTLY these nine fields, all non-empty
strings — the same schema the frozen Parent curated cards use, so there is one
content shape and not two:

```json
[
  {
    "title": "",
    "theme": "",
    "materials": "",
    "instructions": "",
    "success_criteria": "",
    "make_easier": "",
    "make_harder": "",
    "group_play_line": "",
    "what_to_avoid": ""
  }
]
```

The filename stem is the canonical activity family. It must be a family the
taxonomy defines; the generator rejects an unknown one rather than minting a
family by filename.

## What the generator does with them

1. rejects the file unless every card has exactly the nine fields, non-empty
2. runs the real Parent `validate_activity` over each assembled card — the same
   validator that rejects the placeholder tier
3. computes a content-addressed `activity_template_id`
4. records the file's sha256 in the artifact's provenance, alongside the
   activity-engine and taxonomy SHAs

A card that fails validation fails the BUILD. Nothing is admitted on the
strength of being present here.

## Quality bar

`instructions` must say what the parent does, what the child does, what counts
as success, and when to stop — and must be specific enough to run without
interpretation. The 13 `expressive_word` cards are the reference standard. For
example:

> Hold one toy in each hand. Say 'which one?' and wait. If your child reaches
> or looks, say the name and give it. Try again with a different pair. Do 3-4
> choices.

## Deliberately absent

No `duration_minutes` (the Parent engine uses a global constant, not per-card
data), no frequency field (repetitions belong inside `instructions` as prose),
no difficulty tier (the curated source has none). Adding any of them here would
create a second content schema and a field the rest of the pipeline cannot
honour.

## Not done by this slice

No content was authored here. `two_word_phrases.json` is intentionally absent,
so the release gate fails closed until a clinician supplies it.
