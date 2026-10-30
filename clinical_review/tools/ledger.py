"""clinical_review/tools/ledger.py — the sidecar clinical-review ledger.

ONE implementation of every ledger rule. The review-packet builder, the
promotion tool, the test suite and the CI gate all import from here, so a rule
cannot be enforced in one place and forgotten in another.

## WHY A SIDECAR AND NOT A CARD FIELD

`activity_template_id` is a digest over the activity family plus the NINE
authored card fields, in a fixed order (`pilot_backend/domain/activity_template
.py`). Adding a `review_status` field to that schema would change the id of
every template that already exists — 30 of them — along with the artifact
digest and any evidence row that names a template. So clinical status lives
beside the content, keyed BY the content's id.

That keying is the whole mechanism. An approval names exact bytes. Edit a word
and the id changes, so the edited card has no approval and cannot be promoted;
the old approval still refers to wording nobody will ship. No automated step
can transfer an approval across an edit, because there is nothing to transfer
it to.

## SOURCE TIER IS NOT REVIEW STATUS

`source_tier` (`family_curated` / `bucket_curated`) records where a card's
WORDS came from. It is deliberately NOT renamed to anything that sounds like
clinical sign-off, and this module never writes it. Origin and clinical
approval are different claims about a card and are recorded in different
places on purpose.

## legacy_unresolved IS A MIGRATION REGISTER, NOT A STATUS

The 18 cards already admitted under `pilot_runtime/data/reviewed_cards/`
predate this ledger. There is no evidence in the repository that a clinician
explicitly approved them, and their presence in a directory is not evidence.
They are therefore listed in `legacy_unresolved`, which asserts exactly one
thing: *this template was admitted before explicit clinical-review provenance
existed*. It never implies approval by Hannah or anyone else.

The register may SHRINK as legacy cards receive real review. It must never
GROW: every newly admitted card needs an exact-id `clinician_approved` entry.
`check_reviewed_cards` and `assert_register_never_grew` are what make that
enforceable rather than aspirational.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

#: Repository root, resolved from THIS file so a tool run from any working
#: directory reads the same bytes.
REPO_ROOT = Path(__file__).resolve().parents[2]

CLINICAL_REVIEW_DIR = REPO_ROOT / "clinical_review"
CANDIDATES_DIR = CLINICAL_REVIEW_DIR / "candidates"
LEDGER_PATH = CLINICAL_REVIEW_DIR / "review_ledger.json"
OUT_DIR = CLINICAL_REVIEW_DIR / "out"

#: The generator's admitted input. Promotion writes here and nowhere else.
REVIEWED_CARDS_DIR = REPO_ROOT / "pilot_runtime" / "data" / "reviewed_cards"

#: Paths a review packet must NEVER be written under. Both are copied wholesale
#: into every container build context (all four `gcloudignore` files ignore
#: everything then re-admit `pilot_backend/**` and `pilot_runtime/**`), and both
#: are scanned by the CI no-mirror gate.
GUARDED_TREES: Tuple[str, ...] = ("pilot_backend", "pilot_runtime")

LEDGER_VERSION = "genex-clinical-review-ledger-v1"

#: The four founder-approved statuses. No fifth one is added for supersession:
#: an edited card gets a NEW id and a NEW entry carrying `supersedes`, and the
#: old id simply never reaches `clinician_approved`. That absence IS the
#: invalidation, so it needs no status of its own.
STATUS_GENERATED_CANDIDATE = "generated_candidate"
STATUS_CLINICIAN_EDITED = "clinician_edited"
STATUS_CLINICIAN_APPROVED = "clinician_approved"
STATUS_REJECTED = "rejected"

STATUSES: Tuple[str, ...] = (
    STATUS_GENERATED_CANDIDATE,
    STATUS_CLINICIAN_EDITED,
    STATUS_CLINICIAN_APPROVED,
    STATUS_REJECTED,
)

#: The only status that permits promotion.
PROMOTABLE_STATUS = STATUS_CLINICIAN_APPROVED

ENTRY_FIELDS: Tuple[str, ...] = (
    "activity_family_ref",
    "status",
    "reviewer",
    "decided_on",
    "note",
    "supersedes",
    "adapted_from",
)


class LedgerError(Exception):
    """A ledger rule was violated. Build/tool time only; never served."""


# ---------------------------------------------------------------------------
# the one id implementation
# ---------------------------------------------------------------------------


def compute_card_id(activity_family_ref: str, card: Mapping[str, str]) -> str:
    """The template id, computed by the PRODUCTION function.

    Imported here rather than reimplemented: a second digest implementation
    that agreed with the first today would be free to disagree tomorrow, and
    an approval keyed on the wrong digest approves nothing.
    """
    import sys

    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from pilot_backend.domain.activity_template import (  # noqa: WPS433
        compute_template_id,
    )

    return compute_template_id(activity_family_ref, card)


def card_fields() -> Tuple[str, ...]:
    """The nine authored fields, from the production schema."""
    import sys

    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from pilot_backend.domain.activity_template import CARD_FIELDS  # noqa: WPS433

    return tuple(CARD_FIELDS)


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def read_cards(path: Path) -> List[Dict[str, str]]:
    """One card file: a non-empty array of nine-field objects.

    Enforces the SAME shape rule the generator enforces on its own input, so a
    candidate file that would be refused after promotion is refused now,
    while the author is still here to fix it.
    """
    try:
        cards = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LedgerError(f"{path.name} is not readable JSON") from exc
    if not isinstance(cards, list) or not cards:
        raise LedgerError(f"{path.name} must be a non-empty array")
    fields = set(card_fields())
    for card in cards:
        if not isinstance(card, dict):
            raise LedgerError(f"{path.name} holds a non-object")
        extra = sorted(set(card) - fields)
        if extra:
            raise LedgerError(
                f"{path.name} has fields outside the nine-field schema: {extra}")
        missing = sorted(f for f in fields
                         if not isinstance(card.get(f), str)
                         or not card[f].strip())
        if missing:
            raise LedgerError(f"{path.name} has empty fields: {missing}")
    return cards


def candidate_families() -> Tuple[str, ...]:
    """Families with a candidate file, derived from FILENAMES.

    Derived rather than listed because a hard-coded list here would be a second
    record of which families Track 2 touches — and because several canonical
    family identifiers may not be restated in guarded source at all. The
    generator uses the same filename-stem convention for the same reasons.
    """
    if not CANDIDATES_DIR.is_dir():
        return ()
    return tuple(sorted(p.stem for p in CANDIDATES_DIR.glob("*.json")))


def read_ledger(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load and structurally validate the ledger."""
    target = Path(path) if path is not None else LEDGER_PATH
    try:
        body = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LedgerError("the review ledger is not readable JSON") from exc
    if not isinstance(body, dict):
        raise LedgerError("the review ledger must be an object")
    if body.get("ledger_version") != LEDGER_VERSION:
        raise LedgerError("the review ledger declares an unsupported version")

    entries = body.get("entries")
    if not isinstance(entries, dict):
        raise LedgerError("the review ledger has no entries object")
    for template_id, entry in entries.items():
        if not isinstance(template_id, str) or not template_id.startswith("atpl1:"):
            raise LedgerError(f"ledger key is not a template id: {template_id!r}")
        if not isinstance(entry, dict):
            raise LedgerError(f"ledger entry for {template_id} is not an object")
        unknown = sorted(set(entry) - set(ENTRY_FIELDS))
        if unknown:
            raise LedgerError(
                f"ledger entry for {template_id} has unknown fields: {unknown}")
        if entry.get("status") not in STATUSES:
            raise LedgerError(
                f"ledger entry for {template_id} has status "
                f"{entry.get('status')!r}, which is not one of {STATUSES}")
        if entry["status"] == STATUS_CLINICIAN_APPROVED:
            # An approval with no reviewer and no date is not an approval; it is
            # an assertion nobody made. Refused at read time so it can never
            # reach the promotion check.
            for required in ("reviewer", "decided_on"):
                if not str(entry.get(required) or "").strip():
                    raise LedgerError(
                        f"{template_id} is {STATUS_CLINICIAN_APPROVED} but has "
                        f"no {required}")

    register = body.get("legacy_unresolved")
    if not isinstance(register, list):
        raise LedgerError("the review ledger has no legacy_unresolved list")
    if len(set(register)) != len(register):
        raise LedgerError("legacy_unresolved contains a duplicate id")
    overlap = sorted(set(register) & set(entries))
    if overlap:
        # A card cannot be both "provenance unresolved" and reviewed. Allowing
        # both would let a legacy id acquire an approval while still sitting in
        # the register, which is exactly the retroactive blessing decision 6
        # forbids. Shrinking the register is a deliberate REMOVAL.
        raise LedgerError(
            "ids appear in both legacy_unresolved and entries: "
            + ", ".join(overlap))
    return body


def approved_ids(ledger: Mapping[str, Any]) -> frozenset:
    return frozenset(
        tid for tid, entry in ledger["entries"].items()
        if entry["status"] == STATUS_CLINICIAN_APPROVED)


# ---------------------------------------------------------------------------
# the gates
# ---------------------------------------------------------------------------


def check_reviewed_cards(ledger: Mapping[str, Any],
                         reviewed_dir: Optional[Path] = None) -> List[str]:
    """Every admitted card is approved-by-id OR a registered legacy card.

    Returns the violations; an empty list is a pass. This is the gate that
    makes "no candidate becomes release-eligible through an automated
    transformation" enforceable: a card can only reach the generator's input
    directory by being approved, or by having been there before the ledger
    existed and being honestly recorded as unresolved.
    """
    directory = Path(reviewed_dir) if reviewed_dir is not None \
        else REVIEWED_CARDS_DIR
    approved = approved_ids(ledger)
    register = set(ledger["legacy_unresolved"])
    problems: List[str] = []
    if not directory.is_dir():
        return problems
    for path in sorted(directory.glob("*.json")):
        family = path.stem
        for card in read_cards(path):
            tid = compute_card_id(family, card)
            if tid in approved:
                entry = ledger["entries"][tid]
                if entry.get("activity_family_ref") != family:
                    problems.append(
                        f"{tid} is approved for family "
                        f"{entry.get('activity_family_ref')!r} but is admitted "
                        f"under {family!r}")
                continue
            if tid in register:
                continue
            problems.append(
                f"{family} :: {card['title']!r} ({tid}) is admitted but has no "
                f"{PROMOTABLE_STATUS} ledger entry and is not a registered "
                f"legacy card")
    return problems


def assert_register_never_grew(ledger: Mapping[str, Any],
                               baseline: Iterable[str]) -> None:
    """The register may shrink as legacy cards are reviewed; never grow.

    Growth would mean a newly admitted card had been labelled "provenance
    unresolved" instead of being approved — a route around the ledger that
    looks like bookkeeping. Checked against the MIGRATION BASELINE pinned in
    CI, not against the ledger's own previous value, because a file cannot
    witness its own history.
    """
    register = set(ledger["legacy_unresolved"])
    added = sorted(register - set(baseline))
    if added:
        raise LedgerError(
            "legacy_unresolved grew beyond the migration baseline; a newly "
            "admitted card must have an exact-id "
            f"{PROMOTABLE_STATUS} entry instead:\n  " + "\n  ".join(added))


def assert_outside_guarded_trees(destination: Path) -> Path:
    """Refuse any output path under `pilot_backend/` or `pilot_runtime/`.

    A review packet shows a clinician verbatim canonical milestone text and
    concrete `rung1:` references. Both are forbidden under those two trees by
    the CI no-mirror gate, and both would ship to a container if written there.
    Enforced on the RESOLVED path, so `../pilot_runtime/x` is caught too.
    """
    resolved = Path(destination).resolve()
    try:
        relative = resolved.relative_to(REPO_ROOT)
    except ValueError:
        return resolved          # outside the repo entirely; nothing to guard
    if relative.parts and relative.parts[0] in GUARDED_TREES:
        raise LedgerError(
            f"refusing to write a review packet under {relative.parts[0]}/: "
            "packets carry canonical milestone text and rung references, which "
            "the no-mirror gate forbids there and which must never reach a "
            "container image")
    return resolved


def candidate_status_report() -> Dict[str, Dict[str, str]]:
    """Per family: each candidate card's id, title and current ledger status.

    The one place that answers "where does every candidate stand?", so neither
    the packet nor a founder review has to join two files by hand.
    """
    ledger = read_ledger()
    report: Dict[str, Dict[str, str]] = {}
    for family in candidate_families():
        rows: Dict[str, str] = {}
        for card in read_cards(CANDIDATES_DIR / f"{family}.json"):
            tid = compute_card_id(family, card)
            entry = ledger["entries"].get(tid)
            rows[tid] = (f"{card['title']} :: "
                         f"{entry['status'] if entry else 'NOT IN LEDGER'}")
        report[family] = rows
    return report
