"""pilot_runtime/integration/activity_bank_generator.py — 0.6A-1.

Emits the static activity-bank artifact the BROWSER Pilot serves lookups from,
built OFFLINE out of frozen Parent curated cards plus clinician-reviewed cards
supplied under `pilot_runtime/data/reviewed_cards/`.

Same shape as `rung_table_generator`, and for the same reasons: the serving
image has no `genex_core`, no `parent_taxonomy`, no pandas and no model client,
so anything it needs from Parent has to be precomputed and shipped as data.

## NO LLM RUNS HERE, AND NONE CAN

The Parent engine's activity writer is only reached through
`_v22_make_activity`, which this generator never calls. It reads the curated
POOLS directly — module-level literals — and assembles cards itself. There is
no OpenAI import, no `ACTIVITY_MODEL`, and the artifact records no model
provenance because no model participated.

## THREE TIERS EXIST IN PARENT; THIS ADMITS TWO

    tier 1  _FAMILY_VARIANTS[family]     curated, complete       ADMITTED
    tier 2  _BUCKET_VARIANTS[bucket]     curated, complete       ADMITTED
    tier 3  generic base/overrides       PLACEHOLDER prose       REFUSED

Tier 3 is the generator's central exclusion, not an oversight. For a family
with no curated cards, `_v22_fallback_instructions` returns
`"Set up a quick {theme} activity. Show your child one small step..."` with
materials `"items for {theme} (from around the home)"`. Parent's own
`validate_activity` rejects that with seven `placeholder_wording` violations.
This generator never reads the generic structures at all, so the refusal is
STRUCTURAL: there is no code path by which a placeholder could be admitted,
even if the validator were bypassed.

## EVERY CARD IS VALIDATED BY PARENT'S OWN VALIDATOR

Each assembled card goes through the real `activity_validator.validate_activity`
before admission. A rejected card fails the BUILD. That matters most for the
reviewed-card directory: being present there earns a card nothing.

## A REQUIRED FAMILY WITH NO CARDS FAILS THE RELEASE GATE, NOT THE BUILD

These are deliberately separate. The artifact is still generated for the
families that DO have content, so the mechanism is testable and reviewable
today; but `release_ready` is false while any required family is unserved, and
the runtime refuses to plan a goal that binds one. Collapsing the two would
mean no artifact could exist until all content was written, which would leave
the whole mechanism unreviewable.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pilot_backend.domain.activity_template import (
    ADMITTED_SOURCE_TIERS,
    CARD_FIELDS,
    FORBIDDEN_SOURCE_TIER,
    ActivityTemplate,
    ActivityTemplateError,
    compute_template_id,
)

#: The artifact's own schema. A shape change takes a new version, which changes
#: the digest and therefore cannot pass the drift gate unnoticed.
ARTIFACT_SCHEMA_VERSION = "pilot-activity-bank-v1"

#: The generator's version — this procedure, not the content it reads.
GENERATOR_VERSION = "pilot-activity-bank-generator-v1"

#: The pilot's only generatable domain, matching `SUPPORTED_DOMAIN` in
#: `baseline_suggestion_generation`.
DOMAIN_KEY = "talking_and_communicating"

#: Families the October pilot's first goal binds. Both are REQUIRED: the goal's
#: canonical anchor names both, so a week covering only one practises half the
#: clinical target. `two_word_phrases` has no curated content today, which is
#: why `release_ready` is false.
REQUIRED_FAMILIES: Tuple[str, ...] = (
    "expressive_vocabulary_growth",
    "two_word_phrases",
)

#: The ONLY approved family -> bucket reuse. A family absent from this map gets
#: ZERO bucket-derived cards, even when `_family_bucket()` happens to resolve it
#: to a populated bucket.
#:
#: This exists because `_family_bucket` is a REGEX over the family name, and
#: `expressive_word` is broad: `pronouns`, `wh_question_asking`,
#: `expressive_name_response` and `book_object_naming` all resolve to it.
#: Without this map, making any of them canonical would silently hand it the 12
#: single-word VOCABULARY cards — a right-looking family serving clinically
#: wrong content. Bucket reuse is therefore an explicit, per-family, recorded
#: founder decision rather than a consequence of how a name is spelled.
BUCKET_REUSE_APPROVED: Dict[str, str] = {
    # Approved 0.6A-1: these ARE the vocabulary cards, for the vocabulary
    # family. 13 cards in the pool, 12 admitted (one fails the validator).
    "expressive_vocabulary_growth": "expressive_word",
}

ARTIFACT_RELPATH = "pilot_runtime/data/activity_bank_talking_v1.json"
REVIEWED_CARDS_RELPATH = "pilot_runtime/data/reviewed_cards"

#: Where the curated pools and the validator live, recorded in provenance.
ACTIVITY_ENGINE_RELPATH = "genex_core/activity_engine.py"
TAXONOMY_RELPATH = "data/parent_2_4/activity_family_taxonomy_v1.xlsx"


class ActivityBankBuildError(Exception):
    """The artifact could not be built. Build-time only; never served."""

    PHI_SAFE_MESSAGE = True


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(payload: Any) -> str:
    """The one serialisation the digest and the drift gate both use."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def artifact_digest(body: Dict[str, Any]) -> str:
    """sha256 over the canonical body with any existing digest excluded."""
    without = {k: v for k, v in body.items() if k != "artifact_digest"}
    return hashlib.sha256(canonical_json(without).encode("utf-8")).hexdigest()


def _import_parent(parent_root: Path):
    """Put `genex-parent` on the path and import ONLY what is needed.

    Deliberately does not import `functional_baseline` or anything that
    reaches `activity_engine._v22_make_activity`: this generator reads curated
    literals and runs the validator, and nothing else.
    """
    root = str(parent_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    from genex_core import activity_engine  # noqa: WPS433
    from genex_core import activity_validator  # noqa: WPS433
    from parent_taxonomy import activity_families  # noqa: WPS433
    return activity_engine, activity_validator, activity_families


def _assembled_for_validation(card: Dict[str, str], family: str) -> Dict[str, Any]:
    """The card in the shape `validate_activity` expects.

    The validator reads the ASSEMBLED activity, whose keys differ from the
    card's (`success` not `success_criteria`, `easier`/`harder`/`avoid`/
    `group_play`). Mapped explicitly rather than by a loop, so a future
    validator field cannot be silently satisfied by a coincidental name match.
    """
    return {
        "title": card["title"],
        "theme": card["theme"],
        "materials": card["materials"],
        "instructions": card["instructions"],
        "success": card["success_criteria"],
        "easier": card["make_easier"],
        "harder": card["make_harder"],
        "group_play": card["group_play_line"],
        "avoid": card["what_to_avoid"],
        "activity_family": family,
        "category_key": DOMAIN_KEY,
        "domain": DOMAIN_KEY,
    }


def _admit(card: Dict[str, str], family: str, tier: str, pool_ref: str,
           validator, rejected: List[Dict[str, Any]]
           ) -> Optional[ActivityTemplate]:
    """Validate one card and build its template, or record why it was refused."""
    if tier not in ADMITTED_SOURCE_TIERS:
        raise ActivityBankBuildError(
            f"tier {tier!r} is not admitted (never {FORBIDDEN_SOURCE_TIER!r})")
    missing = [f for f in CARD_FIELDS
               if not isinstance(card.get(f), str) or not card[f].strip()]
    if missing:
        rejected.append({"family": family, "pool_ref": pool_ref,
                         "reason": "incomplete_card",
                         "detail": sorted(missing)})
        return None

    ok, problems = validator.validate_activity(
        _assembled_for_validation(card, family), DOMAIN_KEY)
    if not ok:
        rejected.append({"family": family, "pool_ref": pool_ref,
                         "reason": "validator_rejected",
                         "detail": sorted(str(p) for p in problems)})
        return None
    try:
        return ActivityTemplate.build(
            activity_family_ref=family, source_tier=tier,
            source_pool_ref=pool_ref, card=card)
    except ActivityTemplateError as exc:
        rejected.append({"family": family, "pool_ref": pool_ref,
                         "reason": "template_refused", "detail": [str(exc)]})
        return None


def _reviewed_cards(directory: Path, known_families) -> Dict[str, List[Dict]]:
    """Read clinician-supplied cards. Absent directory or file is NOT an error.

    An absent file means "no reviewed content yet", which is a true and
    expected state that the release gate reports. A MALFORMED file is an error,
    because silently skipping it would look identical to absence.
    """
    found: Dict[str, List[Dict]] = {}
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.json")):
        family = path.stem.strip()
        if family not in known_families:
            raise ActivityBankBuildError(
                f"reviewed-card file names a family the taxonomy does not "
                f"define: {family!r}")
        try:
            cards = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ActivityBankBuildError(
                f"reviewed-card file for {family!r} is not valid JSON") from exc
        if not isinstance(cards, list) or not cards:
            raise ActivityBankBuildError(
                f"reviewed-card file for {family!r} must be a non-empty array")
        for card in cards:
            if not isinstance(card, dict):
                raise ActivityBankBuildError(
                    f"reviewed-card file for {family!r} holds a non-object")
            extra = sorted(set(card) - set(CARD_FIELDS))
            if extra:
                # Refused rather than ignored: an extra key means the author
                # expected it to do something.
                raise ActivityBankBuildError(
                    f"reviewed card for {family!r} has fields outside the "
                    f"nine-field schema: {extra}")
        found[family] = cards
    return found


def build_artifact(parent_root: Optional[Path] = None,
                   reviewed_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Generate the artifact body. Reads only; writes nothing."""
    repo_root = Path(__file__).resolve().parents[2]
    root = Path(parent_root) if parent_root is not None \
        else repo_root / "genex-parent"
    if not root.is_dir():
        raise ActivityBankBuildError("the Parent package root is not present")
    reviewed = Path(reviewed_dir) if reviewed_dir is not None \
        else repo_root / REVIEWED_CARDS_RELPATH

    engine, validator, families_mod = _import_parent(root)
    taxonomy = families_mod.get_taxonomy()
    known_families = set(taxonomy.families)

    templates: Dict[str, ActivityTemplate] = {}
    rejected: List[Dict[str, Any]] = []
    by_family: Dict[str, List[str]] = {}

    def _record(template: Optional[ActivityTemplate]) -> None:
        if template is None:
            return
        if template.activity_template_id in templates:
            # Two identical cards in different pools. Collapsed rather than
            # duplicated: the id IS the content, so these are one template.
            return
        templates[template.activity_template_id] = template
        by_family.setdefault(template.activity_family_ref, []).append(
            template.activity_template_id)

    # -- tier 1: family-level curated cards --------------------------------
    for family in sorted(REQUIRED_FAMILIES):
        for card in engine._FAMILY_VARIANTS.get(family, ()):
            _record(_admit(dict(card), family, "family_curated",
                           f"_FAMILY_VARIANTS[{family}]", validator, rejected))

    # -- tier 2: bucket-level curated cards, ONLY where explicitly approved --
    #
    # Iterating BUCKET_REUSE_APPROVED rather than REQUIRED_FAMILIES is the
    # guard. Under the previous shape, declaring a family REQUIRED was enough to
    # give it whatever `_family_bucket()` resolved to — so a future
    # `wh_question_asking` would have inherited the vocabulary cards just by
    # being required. Now approval is the ONLY route in.
    #
    # `bucket_for` still records what Parent's resolver says for EVERY required
    # family, approved or not, so the artifact shows which families would have
    # been at risk.
    bucket_for: Dict[str, str] = {
        family: engine._family_bucket(family, DOMAIN_KEY)
        for family in sorted(set(REQUIRED_FAMILIES) | set(BUCKET_REUSE_APPROVED))
    }
    for family, approved_bucket in sorted(BUCKET_REUSE_APPROVED.items()):
        resolved = engine._family_bucket(family, DOMAIN_KEY)
        if resolved != approved_bucket:
            # The approval was recorded against a different bucket than Parent
            # now resolves. Refused rather than followed: an approval is for a
            # specific pool of cards, not for whatever the regex says today.
            raise ActivityBankBuildError(
                f"approved bucket reuse for {family!r} names "
                f"{approved_bucket!r} but Parent resolves {resolved!r}")
        if by_family.get(family):
            # Explicit family cards win over bucket reuse — the Parent engine's
            # own tier order.
            continue
        for card in engine._BUCKET_VARIANTS.get(approved_bucket, ()):
            _record(_admit(dict(card), family, "bucket_curated",
                           f"_BUCKET_VARIANTS[{approved_bucket}]", validator,
                           rejected))

    # -- clinician-reviewed cards ------------------------------------------
    reviewed_files: Dict[str, str] = {}
    for family, cards in _reviewed_cards(reviewed, known_families).items():
        reviewed_files[family] = _sha256_file(reviewed / f"{family}.json")
        for card in cards:
            _record(_admit(dict(card), family, "family_curated",
                           f"reviewed_cards/{family}.json", validator,
                           rejected))

    # -- the release gate --------------------------------------------------
    unserved = sorted(f for f in REQUIRED_FAMILIES if not by_family.get(f))
    body: Dict[str, Any] = {
        "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "domain_key": DOMAIN_KEY,
        "provenance": {
            "activity_engine_relpath": ACTIVITY_ENGINE_RELPATH,
            "activity_engine_sha256": _sha256_file(
                root / ACTIVITY_ENGINE_RELPATH),
            "taxonomy_relpath": TAXONOMY_RELPATH,
            "taxonomy_sha256": _sha256_file(root / TAXONOMY_RELPATH),
            "taxonomy_version": families_mod.ACTIVITY_TAXONOMY_VERSION,
            "reviewed_card_files": dict(sorted(reviewed_files.items())),
            "admitted_source_tiers": list(ADMITTED_SOURCE_TIERS),
            "forbidden_source_tier": FORBIDDEN_SOURCE_TIER,
            # The explicit allowlist, recorded so the approval is auditable from
            # the artifact rather than only from the generator's source.
            "bucket_reuse_approved": dict(sorted(BUCKET_REUSE_APPROVED.items())),
            # Stated as data so a reader does not have to trust the docstring.
            "llm_used": False,
        },
        "required_families": list(REQUIRED_FAMILIES),
        "family_bucket_resolution": dict(sorted(bucket_for.items())),
        "served_families": sorted(by_family),
        "unserved_required_families": unserved,
        "release_ready": not unserved,
        "population": {
            "templates": len(templates),
            "by_family": {f: len(ids) for f, ids in sorted(by_family.items())},
            "rejected": len(rejected),
        },
        "rejected_cards": rejected,
        "templates": {
            tid: {
                "activity_family_ref": t.activity_family_ref,
                "source_tier": t.source_tier,
                "source_pool_ref": t.source_pool_ref,
                **{f: getattr(t, f) for f in CARD_FIELDS},
            }
            for tid, t in sorted(templates.items())
        },
    }
    body["artifact_digest"] = artifact_digest(body)
    return body


def render(body: Dict[str, Any]) -> str:
    """The committed file's bytes: pretty, sorted, newline-terminated."""
    return json.dumps(body, sort_keys=True, indent=2,
                      ensure_ascii=False) + "\n"


def write_artifact(destination: Path, parent_root: Optional[Path] = None,
                   reviewed_dir: Optional[Path] = None) -> Dict[str, Any]:
    body = build_artifact(parent_root, reviewed_dir)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render(body), encoding="utf-8")
    return body


def main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover - CLI
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=None)
    parser.add_argument("--parent-root", default=None)
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[2]
    out = Path(args.out) if args.out else repo_root / ARTIFACT_RELPATH
    body = write_artifact(
        out, Path(args.parent_root) if args.parent_root else None)
    print(f"wrote {out}")
    print(f"  digest        {body['artifact_digest']}")
    print(f"  templates     {body['population']}")
    print(f"  served        {body['served_families']}")
    print(f"  unserved      {body['unserved_required_families']}")
    print(f"  release_ready {body['release_ready']}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
