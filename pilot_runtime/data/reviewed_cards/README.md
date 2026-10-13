# Clinician-reviewed activity cards — the supply location

This directory is the ONLY place the pilot accepts activity content that does
not already exist in a frozen Parent curated tier.

It exists because a goal's canonical anchor can bind more than one activity
family, and the frozen Parent curated tiers do not necessarily carry reviewed
content for every one of them. Where they do not, a clinician supplies cards
here and the generator admits them on the same terms as curated content.

**Which families are required, which are served, and how many cards each
holds are NOT recorded in this file.** They are properties of the generated
artifact (`required_families`, `served_families`, `population.by_family`,
`release_ready`) and of `REQUIRED_FAMILIES` in
`pilot_runtime/integration/activity_bank_generator.py`. A table of family
names and counts here would be a second record that goes stale the moment a
clinician adds a card — which is exactly what happened to the previous version
of this file. Read the artifact instead; it is regenerated and digest-checked
by the CI drift gate on every run.

The Parent engine's generic tier-3 template is NOT an answer for a missing
family. It produces placeholder prose that the Parent validator itself rejects
with `placeholder_wording` violations. The generator refuses that tier by
construction.

## How to supply cards

Create `<family>.json` in this directory, containing a JSON array of card
objects. Each card must have EXACTLY these nine fields, all non-empty strings
— the same schema the frozen Parent curated cards use, so there is one content
shape and not two:

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
interpretation. The frozen Parent curated cards are the reference standard.
For example:

> Hold one toy in each hand. Say 'which one?' and wait. If your child reaches
> or looks, say the name and give it. Try again with a different pair. Do 3-4
> choices.

## Deliberately absent

No `duration_minutes` (the Parent engine uses a global constant, not per-card
data), no frequency field (repetitions belong inside `instructions` as prose),
no difficulty tier (the curated source has none). Adding any of them here would
create a second content schema and a field the rest of the pipeline cannot
honour.

## Release gating

`release_ready` in the generated artifact is false while any REQUIRED family
has no reviewed content, and `assert_release_ready` refuses a week release in
that state. So an unsupplied family fails closed rather than producing a
partially covered week. Consult the artifact for the current state; do not
infer it from this document.
