"""clinical_review/tools/promote_approved.py — the one route into the bank.

Copies candidate cards that carry an exact-template-id `clinician_approved`
ledger entry into `pilot_runtime/data/reviewed_cards/<family>.json`, which is
the activity-bank generator's admitted input.

## RUN BY A HUMAN, NEVER BY CI

Promotion is the moment unreviewed content becomes release-eligible, so no
automated pipeline invokes this. CI's job is the opposite one: to assert, after
the fact, that nothing in `reviewed_cards/` lacks an approval it should have
(`ledger.check_reviewed_cards`).

## IT REFUSES ON EVERYTHING EXCEPT AN EXACT MATCH

A card is promoted only when its id — recomputed from the file's bytes — has a
`clinician_approved` entry whose family matches the filename stem, with a
reviewer and a decision date. Approve-with-edits is handled by this same rule
and needs no special case: edited wording hashes to a different id, so the
edited card is promotable only once that NEW id is approved, and the pre-edit
approval can never be applied to it.

`--dry-run` is the default. Writing requires `--write`, so the ordinary
invocation cannot change the bank by accident.

## IT DOES NOT BYPASS ANY EXISTING GATE

Promotion only moves bytes into the generator's input directory. The nine-field
check, Parent's real `validate_activity`, template-id computation and the CI
drift gate all run afterwards exactly as they do today. A clinician-approved
card that fails the validator fails the build — clinical approval is necessary,
not sufficient, and this tool deliberately has no power to change that.

As a courtesy it pre-checks the two things that are legal in
`clinical_review/` but forbidden once the bytes land under `pilot_runtime/`:
guarded milestone prose and concrete rung references. Catching those here puts
the failure next to the decision rather than in a later CI run.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .ledger import (
    CANDIDATES_DIR,
    PROMOTABLE_STATUS,
    REPO_ROOT,
    REVIEWED_CARDS_DIR,
    LedgerError,
    candidate_families,
    card_fields,
    compute_card_id,
    read_cards,
    read_ledger,
)

#: Workbook milestone prose the CI no-mirror gate forbids under
#: `pilot_backend/` and `pilot_runtime/`. Not exceptionable anywhere there, so
#: a card carrying either phrase can never be promoted.
GUARDED_MILESTONE_PROSE: Tuple[str, ...] = (
    "says about 50 words",
    "calls parents mama or dada",
)

#: A concrete canonical rung or track reference. The bare `rung1:` prefix is a
#: legitimate id scheme; a full ref is workbook-derived identity.
GUARDED_REF = re.compile(r"(?:rung1|track1):[0-9a-f]{32}")


def _no_mirror_problems(card: Dict[str, str]) -> List[str]:
    blob = " ".join(card[f] for f in card_fields()).lower()
    problems = [f"carries guarded milestone prose {needle!r}"
                for needle in GUARDED_MILESTONE_PROSE if needle in blob]
    found = GUARDED_REF.search(blob)
    if found:
        problems.append(f"carries a concrete canonical ref {found.group(0)!r}")
    return problems


def plan(families: Optional[Sequence[str]] = None
         ) -> Tuple[Dict[str, List[Dict[str, str]]], List[str], List[str]]:
    """Decide what would be promoted. Reads only.

    Returns (promotable cards by family, skipped reasons, refusals).

    A SKIP is ordinary and expected — an unreviewed or rejected candidate.
    A REFUSAL is a defect: an approval that does not line up with the content
    it claims to approve. Skips let the run continue; refusals stop it, because
    a mismatched approval means the ledger and the cards disagree about what a
    clinician actually signed off.
    """
    ledger = read_ledger()
    wanted = tuple(families) if families else candidate_families()
    promotable: Dict[str, List[Dict[str, str]]] = {}
    skipped: List[str] = []
    refusals: List[str] = []

    for family in wanted:
        source = CANDIDATES_DIR / f"{family}.json"
        if not source.is_file():
            refusals.append(f"no candidate file for {family!r}")
            continue
        for card in read_cards(source):
            tid = compute_card_id(family, card)
            entry = ledger["entries"].get(tid)
            label = f"{family} :: {card['title']!r} ({tid})"
            if entry is None:
                skipped.append(f"{label}: not in the ledger")
                continue
            if entry["status"] != PROMOTABLE_STATUS:
                skipped.append(f"{label}: status {entry['status']}")
                continue
            if entry.get("activity_family_ref") != family:
                refusals.append(
                    f"{label}: approved for family "
                    f"{entry.get('activity_family_ref')!r}")
                continue
            for required in ("reviewer", "decided_on"):
                if not str(entry.get(required) or "").strip():
                    refusals.append(f"{label}: approval has no {required}")
                    break
            else:
                mirror = _no_mirror_problems(card)
                if mirror:
                    refusals.append(f"{label}: " + "; ".join(mirror))
                    continue
                promotable.setdefault(family, []).append(card)

    return promotable, skipped, refusals


def promote(families: Optional[Sequence[str]] = None,
            write: bool = False,
            reviewed_dir: Optional[Path] = None) -> Dict[str, int]:
    """Write approved cards into the generator's input. Default is dry-run.

    Rewrites each family's reviewed file as the FULL approved set rather than
    appending, so the file is always exactly "every approved card for this
    family". Appending would let a card that was later rejected survive in the
    bank because nothing ever removed it.
    """
    promotable, skipped, refusals = plan(families)
    if refusals:
        raise LedgerError("promotion refused:\n  " + "\n  ".join(refusals))

    directory = Path(reviewed_dir) if reviewed_dir is not None \
        else REVIEWED_CARDS_DIR
    counts: Dict[str, int] = {}
    for family, cards in sorted(promotable.items()):
        counts[family] = len(cards)
        if not write:
            continue
        directory.mkdir(parents=True, exist_ok=True)
        ordered = [{field: card[field] for field in card_fields()}
                   for card in cards]
        (directory / f"{family}.json").write_text(
            json.dumps(ordered, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
    return counts


def main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--family", action="append", default=None,
                        help="limit to one family; repeatable")
    parser.add_argument("--write", action="store_true",
                        help="actually write; omit for a dry run")
    args = parser.parse_args(argv)

    try:
        promotable, skipped, refusals = plan(args.family)
    except LedgerError as exc:
        print(f"refused: {exc}")
        return 2

    for line in skipped:
        print(f"  skip     {line}")
    for line in refusals:
        print(f"  REFUSE   {line}")
    if refusals:
        print("\nnothing promoted: an approval does not match the content it "
              "claims to approve.")
        return 2
    if not promotable:
        print("\nnothing to promote: no candidate carries an exact-id "
              f"{PROMOTABLE_STATUS} entry.")
        return 0

    counts = promote(args.family, write=args.write)
    verb = "promoted" if args.write else "would promote"
    for family, count in sorted(counts.items()):
        print(f"  {verb}  {count} card(s) -> "
              f"{(REVIEWED_CARDS_DIR / f'{family}.json').relative_to(REPO_ROOT)}")
    if args.write:
        print("\nNow regenerate the bank and run its gates:\n"
              "  PYTHONPATH=genex-parent python -m "
              "pilot_runtime.integration.activity_bank_generator")
    else:
        print("\ndry run; pass --write to apply.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
