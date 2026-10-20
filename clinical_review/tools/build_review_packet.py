"""clinical_review/tools/build_review_packet.py — Hannah's review packet.

Renders one self-contained HTML page per activity family: every candidate card
in full, its exact template id, and the canonical rungs that family binds with
their verbatim milestone text — so a clinical decision is made against the
clinical target, not against a card in isolation.

## WHY THIS TOOL LIVES OUTSIDE pilot_runtime/

A packet necessarily shows verbatim canonical milestone text and concrete
`rung1:` references. Both are forbidden under `pilot_backend/` and
`pilot_runtime/` by the CI no-mirror gate, and both would be copied into every
container image, since all four build contexts re-admit those two trees
wholesale. So the tool sits at the repository root, reads the generated rung
artifact, and writes to `clinical_review/out/`, which is gitignored.

`assert_outside_guarded_trees` enforces that on the RESOLVED destination, so a
mistyped `--out` cannot put milestone prose somewhere the gate would catch it
later — or somewhere a deploy would carry it.

## THE PACKET IS NOT THE RECORD

Nothing here is committed. The durable record of a review is the LEDGER, which
holds template ids, statuses, reviewer and date — and no milestone prose at
all. A content-addressed id already identifies exactly which wording was
approved, so the audit trail is complete without the packet ever entering git.

## IT DOES NOT DECIDE ANYTHING

No status is written by this tool. It reads candidates and the ledger and
renders them. Approval is a human writing a decision into the ledger.
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .ledger import (
    CANDIDATES_DIR,
    OUT_DIR,
    REPO_ROOT,
    LedgerError,
    assert_outside_guarded_trees,
    candidate_families,
    card_fields,
    compute_card_id,
    read_cards,
    read_ledger,
)

#: The generated rung artifact — an `ALLOWED_DATA` no-mirror exception that is
#: regenerated and digest-checked by its own CI drift gate. Read, never written.
RUNG_ARTIFACT = REPO_ROOT / "pilot_runtime" / "data" / "rung_table_talking_v1.json"

#: Field order as the clinician should read it: what the activity is, then what
#: the caregiver does, then how it flexes, then the guardrail.
DISPLAY_ORDER: Sequence[str] = (
    "title", "theme", "materials", "instructions", "success_criteria",
    "make_easier", "make_harder", "group_play_line", "what_to_avoid",
)

DECISIONS: Sequence[str] = ("Approve", "Approve with edits", "Reject")


def rungs_for_family(family: str,
                     artifact_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Canonical rungs this family binds, in developmental order.

    Read out of the generated artifact rather than recomputed: the rung table
    is produced by traversing the frozen Parent ladder once, and a second
    traversal here would be a second clinical opinion about ordering.

    Also reports whether each rung is reachable as a step target, measured from
    the artifact's own enumerated step table — a clinician should know that a
    rung exists but that the resolver may not currently select it.
    """
    path = Path(artifact_path) if artifact_path is not None else RUNG_ARTIFACT
    body = json.loads(path.read_text(encoding="utf-8"))
    reachable_from: Dict[str, List[int]] = {}
    for from_months, step in body.get("steps", {}).items():
        reachable_from.setdefault(step["target_rung_ref"], []).append(
            int(from_months))

    rows = []
    for rung in body.get("rungs", {}).values():
        if not any(b["family_ref"] == family
                   for b in rung.get("family_bindings", ())):
            continue
        floors = sorted(reachable_from.get(rung["rung_ref"], ()))
        rows.append({
            "months": rung["source_rung_months"],
            "rung_ref": rung["rung_ref"],
            "milestone_text": rung["milestone_text"],
            "subdomain": rung["subdomain"],
            "co_bound": sorted(b["family_ref"]
                               for b in rung["family_bindings"]
                               if b["family_ref"] != family),
            "reachable_from": floors,
        })
    return sorted(rows, key=lambda r: (r["months"], r["milestone_text"]))


def _style() -> str:
    return """
      :root { color-scheme: light dark; }
      body { font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI",
             Roboto, sans-serif; margin: 0 auto; max-width: 54rem;
             padding: 2rem 1.25rem 4rem; }
      h1 { font-size: 1.5rem; margin-bottom: .25rem; }
      .sub { opacity: .7; font-size: .9rem; margin-bottom: 2rem; }
      .warn { border-left: 3px solid #b8860b; background: rgba(184,134,11,.08);
              padding: .7rem .9rem; margin: 1.25rem 0; font-size: .92rem; }
      .require { border: 2px solid #b22222; border-radius: 6px;
                 background: rgba(178,34,34,.07); padding: .9rem 1.1rem;
                 margin: 1.5rem 0 2rem; font-size: .93rem; }
      .require-h { font-weight: 700; text-transform: uppercase;
                   letter-spacing: .05em; font-size: .8rem; color: #b22222;
                   margin-bottom: .5rem; }
      .require p { margin: .5rem 0 0; }
      table.rungs { border-collapse: collapse; width: 100%; font-size: .88rem;
                    margin: 1rem 0 2.5rem; }
      table.rungs th, table.rungs td { border: 1px solid rgba(128,128,128,.35);
                    padding: .4rem .55rem; text-align: left; vertical-align: top; }
      table.rungs th { background: rgba(128,128,128,.12); }
      .card { border: 1px solid rgba(128,128,128,.4); border-radius: 6px;
              padding: 1.1rem 1.25rem; margin: 0 0 1.75rem; }
      .card h3 { margin: 0 0 .15rem; font-size: 1.12rem; }
      .tid { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
             font-size: .76rem; opacity: .65; word-break: break-all; }
      dl { margin: .9rem 0 0; }
      dt { font-weight: 600; font-size: .78rem; text-transform: uppercase;
           letter-spacing: .04em; opacity: .62; margin-top: .75rem; }
      dd { margin: .15rem 0 0; }
      .note { font-size: .88rem; background: rgba(128,128,128,.1);
              padding: .6rem .8rem; border-radius: 4px; margin: .9rem 0 0; }
      .decide { margin-top: 1.1rem; font-size: .92rem; }
      .box { display: inline-block; border: 1px solid currentColor;
             width: .85rem; height: .85rem; vertical-align: -1px;
             margin-right: .4rem; }
      .decide span { margin-right: 1.6rem; }
      .edits { margin-top: .7rem; border: 1px dashed rgba(128,128,128,.55);
               border-radius: 4px; min-height: 3.2rem; padding: .45rem .6rem;
               font-size: .85rem; opacity: .75; }
      @media print { .card { break-inside: avoid; } }
    """


def render_family(family: str, cards: Sequence[Dict[str, str]],
                  rungs: Sequence[Dict[str, Any]],
                  ledger: Dict[str, Any]) -> str:
    """One family's packet as a standalone HTML document."""
    esc = html.escape
    parts: List[str] = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>Clinical review — {esc(family)}</title>",
        f"<style>{_style()}</style></head><body>",
        f"<h1>Clinical review: {esc(family)}</h1>",
        f"<p class='sub'>{len(cards)} candidate activities &middot; "
        "nothing in this packet is approved, released, or in use.</p>",
        "<div class='warn'><strong>Every card below is a generated candidate.</strong> "
        "A card becomes eligible for a child's plan only when it carries an "
        "exact-template-id <code>clinician_approved</code> entry in the review "
        "ledger. Approving wording approves <em>those exact words</em>: if any "
        "wording changes, the template id changes and the approval does not "
        "carry over. Clinical approval is also not sufficient on its own — "
        "every card is still re-checked by the automated content validator "
        "before it can be built.</div>",
    ]

    parts.append("<h2>Canonical rungs this family binds</h2>")
    parts.append("<table class='rungs'><tr><th>Age</th><th>Milestone "
                 "(verbatim)</th><th>Subdomain</th><th>Resolver reach</th></tr>")
    for rung in rungs:
        if rung["reachable_from"]:
            floors = rung["reachable_from"]
            reach = f"selected for baselines at {floors[0]}&ndash;{floors[-1]}m"
        else:
            reach = "not currently selected by the resolver"
        co = (f"<br><em>also binds: {esc(', '.join(rung['co_bound']))}</em>"
              if rung["co_bound"] else "")
        parts.append(
            f"<tr><td>{rung['months']}m</td>"
            f"<td>{esc(rung['milestone_text'])}{co}"
            f"<div class='tid'>{esc(rung['rung_ref'])}</div></td>"
            f"<td>{esc(rung['subdomain'])}</td><td>{reach}</td></tr>")
    parts.append("</table>")

    if len(rungs) > 1:
        months = [r["months"] for r in rungs]
        parts.append(
            "<div class='require'>"
            "<div class='require-h'>Review requirement for this family</div>"
            f"<p>This family binds <strong>{len(rungs)} canonical rungs, "
            f"spanning {min(months)}&ndash;{max(months)} months</strong>. The "
            "activity bank selects content by <strong>family</strong>, never by "
            "rung &mdash; the card schema has no rung field, and the bank "
            "returns every template for a family the child's goal binds.</p>"
            "<p><strong>So each card you approve may be given to any child "
            "whose target is any rung above</strong>, not only the rung the "
            "card was written for. A child working on the "
            f"{min(months)}-month rung and a child working on the "
            f"{max(months)}-month rung receive the same pool.</p>"
            "<p>Please confirm, for <strong>every</strong> card, that it is "
            "clinically safe and useful <strong>across the full span</strong> "
            "and not merely at its nominal anchor &mdash; using "
            "<em>Make it easier</em> and <em>Make it harder</em> as the "
            "in-card range that has to carry it. A card that is only right at "
            "its anchor should be rejected or rewritten, because there is "
            "currently no way to restrict it to that rung.</p>"
            "</div>")

    parts.append("<h2>Candidate activities</h2>")
    for index, card in enumerate(cards, start=1):
        tid = compute_card_id(family, card)
        entry = ledger["entries"].get(tid)
        parts.append("<div class='card'>")
        parts.append(f"<h3>{index}. {esc(card['title'])}</h3>")
        parts.append(f"<div class='tid'>{esc(tid)}</div>")
        if entry and entry.get("note"):
            parts.append(f"<div class='note'><strong>For review:</strong> "
                         f"{esc(entry['note'])}</div>")
        if entry and entry.get("adapted_from"):
            parts.append(f"<div class='note'><strong>Adapted from:</strong> "
                         f"{esc(entry['adapted_from'])}<br>This is new content "
                         "with a new id. It inherits no approval and no "
                         "provenance from its source card.</div>")
        parts.append("<dl>")
        labels = {
            "theme": "Theme", "materials": "Materials",
            "instructions": "What the caregiver does",
            "success_criteria": "What counts as success",
            "make_easier": "Make it easier", "make_harder": "Make it harder",
            "group_play_line": "With another child",
            "what_to_avoid": "What to avoid",
        }
        for field in DISPLAY_ORDER:
            if field == "title":
                continue
            parts.append(f"<dt>{labels[field]}</dt>"
                         f"<dd>{esc(card[field])}</dd>")
        parts.append("</dl>")
        parts.append("<div class='decide'>" + "".join(
            f"<span><span class='box'></span>{esc(d)}</span>"
            for d in DECISIONS) + "</div>")
        parts.append("<div class='edits'>Edits / clinical comment:</div>")
        parts.append("</div>")

    parts.append("</body></html>")
    return "\n".join(parts)


def build(out_dir: Optional[Path] = None,
          families: Optional[Sequence[str]] = None) -> List[Path]:
    """Render a packet per family. Returns the paths written."""
    destination = Path(out_dir) if out_dir is not None else OUT_DIR
    assert_outside_guarded_trees(destination)
    destination.mkdir(parents=True, exist_ok=True)

    ledger = read_ledger()
    wanted = tuple(families) if families else candidate_families()
    if not wanted:
        raise LedgerError("no candidate families were found")

    written: List[Path] = []
    for family in wanted:
        source = CANDIDATES_DIR / f"{family}.json"
        if not source.is_file():
            raise LedgerError(f"no candidate file for {family!r}")
        cards = read_cards(source)
        page = render_family(family, cards, rungs_for_family(family), ledger)
        target = assert_outside_guarded_trees(
            destination / f"{family}_review_packet.html")
        target.write_text(page, encoding="utf-8")
        written.append(target)
    return written


def main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=None,
                        help="output directory (default clinical_review/out)")
    parser.add_argument("--family", action="append", default=None,
                        help="limit to one family; repeatable")
    args = parser.parse_args(argv)

    try:
        written = build(Path(args.out) if args.out else None, args.family)
    except LedgerError as exc:
        print(f"refused: {exc}")
        return 2
    for path in written:
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    print(f"{len(written)} packet(s). Not committed; "
          f"{OUT_DIR.relative_to(REPO_ROOT)} is gitignored.")
    print("Decisions go into clinical_review/review_ledger.json. "
          "Nothing is approved by generating this packet.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
