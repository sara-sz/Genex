"""
api/customization.py — Beta 2.1 current-week customization overlay (foundation).

Product rule: only the CURRENT plan/week is customizable; previous weeks stay
preserved and read-only. Customizations are stored as a SEPARATE overlay per
plan_id and never mutate the original generated plan:

    doc["plan_customizations"][plan_id] = {
        "removed_activity_ids": [activity_id, ...],
        "saved_for_later_activity_ids": [activity_id, ...],
        "activity_overrides": {
            "<activity_id>": {
                "mode": "easier" | "harder" | "swapped",
                "replacement_activity": { ...full resolved card incl. new id... },
                "replacement_internal": { ...domain/subdomain/family/...  },
                "created_at": "...", "reason": "parent_request"
            }
        },
        "added_activities": [
            {"activity": {...card incl. id...}, "internal": {...}, "day": "...", "created_at": "..."}
        ],
    }

This module is read-only/pure: it builds and resolves overlays but performs no
storage or auth. Mutation endpoints (remove/save/easier/harder/swap/add) are a
later slice; this foundation only exposes helpers + the resolver used by
GET /session and the overlay-aware feedback lookup.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional


def empty_overlay() -> Dict[str, Any]:
    """Return a fresh, empty overlay structure."""
    return {
        "removed_activity_ids": [],
        "saved_for_later_activity_ids": [],
        "activity_overrides": {},
        "added_activities": [],
    }


def get_overlay(doc: Dict[str, Any], plan_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return the overlay dict for plan_id, or None if none exists.

    Safe for old sessions that never had a plan_customizations key.
    """
    if not plan_id:
        return None
    return (doc.get("plan_customizations") or {}).get(plan_id)


def _overlay_changes_plan(overlay: Optional[Dict[str, Any]]) -> bool:
    """True only if the overlay would alter the resolved plan's activities.

    saved_for_later alone does NOT change the rendered plan, so an overlay that
    only contains saved IDs is treated as identity for resolution purposes.
    """
    if not overlay:
        return False
    return bool(
        overlay.get("removed_activity_ids")
        or overlay.get("activity_overrides")
        or overlay.get("added_activities")
    )


def is_overlay_empty(overlay: Optional[Dict[str, Any]]) -> bool:
    """True if the overlay has no customization data of any kind."""
    if not overlay:
        return True
    return not (
        overlay.get("removed_activity_ids")
        or overlay.get("saved_for_later_activity_ids")
        or overlay.get("activity_overrides")
        or overlay.get("added_activities")
    )


def resolve_plan_response(
    plan_response: Dict[str, Any],
    overlay: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return the current plan_response with the overlay applied.

    Identity-safe: when there is no overlay (or it does not change the plan), the
    ORIGINAL plan_response object is returned unchanged — byte-compatible with
    Beta 2.0. When the overlay changes the plan, a deep copy is modified and the
    original is never mutated.

    Resolution (current week only):
      - hide activities whose id is in removed_activity_ids,
      - replace activities that have an override with a replacement_activity,
      - append added_activities to their day (creating the day block if needed).

    All other plan_response fields (session_id, plan_period, age_in_months,
    daily_time_minutes, daily_card_count, progress_summary) and all per-card
    fields (instructions_steps, repeat_*, etc.) are preserved untouched.
    """
    if not plan_response or not _overlay_changes_plan(overlay):
        return plan_response

    resolved = copy.deepcopy(plan_response)
    removed = set(overlay.get("removed_activity_ids") or [])
    overrides = overlay.get("activity_overrides") or {}
    week: List[Dict[str, Any]] = resolved.get("week") or []

    # Hide removed + apply overrides, preserving day order and untouched cards.
    for day_entry in week:
        new_acts: List[Dict[str, Any]] = []
        for act in day_entry.get("activities", []):
            aid = act.get("id")
            if aid in removed:
                continue
            ov = overrides.get(aid)
            if ov and ov.get("replacement_activity"):
                new_acts.append(ov["replacement_activity"])
            else:
                new_acts.append(act)
        day_entry["activities"] = new_acts

    # Append parent-added activities to their day. Added activities are themselves
    # customizable: hide them when their id is in removed_activity_ids (remove /
    # save-for-later), and replace them when they have a swap override.
    added = overlay.get("added_activities") or []
    if added:
        day_index = {d.get("day"): d for d in week}
        for item in added:
            card = item.get("activity")
            if not card:
                continue
            cid = card.get("id")
            if cid in removed:
                continue  # added activity removed / saved for later
            ov = overrides.get(cid)
            if ov and ov.get("replacement_activity"):
                card = ov["replacement_activity"]  # added activity swapped
            day = item.get("day") or ""
            target = day_index.get(day)
            if target is None:
                target = {"day": day, "date": item.get("date", ""), "activities": []}
                week.append(target)
                day_index[day] = target
            target["activities"].append(card)

    resolved["week"] = week
    return resolved


def find_overlay_internal(
    overlay: Optional[Dict[str, Any]],
    activity_id: str,
) -> Optional[Dict[str, Any]]:
    """Return overlay-supplied internal metadata for a swapped/added activity.

    Used by the feedback enrichment fallback so domain/subdomain/care-team report
    routing keeps working for activities that exist only in the overlay (and thus
    have no entry in the frozen plan_internal).
    """
    if not overlay or not activity_id:
        return None

    # Swapped override → replacement_internal keyed by the ORIGINAL activity_id
    # and (when present) by the replacement card's own id.
    for orig_id, ov in (overlay.get("activity_overrides") or {}).items():
        if not ov:
            continue
        repl = ov.get("replacement_activity") or {}
        if activity_id in (orig_id, repl.get("id")):
            internal = ov.get("replacement_internal")
            if internal:
                return internal

    # Parent-added activity → its own internal block, keyed by the card id.
    for item in (overlay.get("added_activities") or []):
        card = item.get("activity") or {}
        if card.get("id") == activity_id:
            return item.get("internal")

    return None


def is_current_plan(doc: Dict[str, Any], plan_id: Optional[str]) -> bool:
    """Guard helper for mutation endpoints: only the current plan is editable.

    Returns True iff plan_id is the session's current_plan_id.
    """
    return bool(plan_id) and doc.get("current_plan_id") == plan_id


def plan_has_activity(plan_response: Dict[str, Any], activity_id: str) -> bool:
    """True if activity_id is one of the originally generated cards in plan_response.

    Customization targets the generated activities (by their stable plan_response
    `id`). Used for the 404 unknown-activity guard.
    """
    if not plan_response or not activity_id:
        return False
    for day_entry in plan_response.get("week", []):
        for act in day_entry.get("activities", []):
            if act.get("id") == activity_id:
                return True
    return False


def resolve_customization_target(
    doc: Dict[str, Any], plan_id: str, activity_id: str
) -> Optional[str]:
    """Map a VISIBLE activity id to its canonical overlay key for customization.

    Parent-facing rule: any activity visible in the current resolved plan is
    actionable (remove / save-for-later / swap). Returns the key to act on, or
    None if the id is not an actionable current-plan activity (→ 404):
      - original generated id  → itself
      - added activity id      → itself
      - swapped replacement id  → the override's source key (original or added id),
                                  so acting on the visible replacement maps back to
                                  the activity that owns the override.
    """
    if not activity_id:
        return None
    plan_entry = (doc.get("plans") or {}).get(plan_id, {})
    if plan_has_activity(plan_entry.get("plan_response") or {}, activity_id):
        return activity_id
    overlay = get_overlay(doc, plan_id) or {}
    for item in overlay.get("added_activities") or []:
        if (item.get("activity") or {}).get("id") == activity_id:
            return activity_id
    for key, ov in (overlay.get("activity_overrides") or {}).items():
        if (ov or {}).get("replacement_activity", {}).get("id") == activity_id:
            return key
    return None


def plan_day_labels(doc: Dict[str, Any], plan_id: str) -> List[str]:
    """Day labels present in the current resolved plan (what the parent sees)."""
    plan_entry = (doc.get("plans") or {}).get(plan_id, {})
    overlay = get_overlay(doc, plan_id)
    resolved = resolve_plan_response(plan_entry.get("plan_response") or {}, overlay)
    return [d.get("day", "") for d in resolved.get("week", []) if d.get("day")]


def match_plan_day(requested_day: Optional[str], valid_days: List[str]) -> Optional[str]:
    """Case-insensitive, trimmed match of requested_day to a canonical plan day
    label. Returns the canonical label, or None if blank / no match."""
    if not requested_day:
        return None
    norm = requested_day.strip().lower()
    if not norm:
        return None
    for d in valid_days:
        if (d or "").strip().lower() == norm:
            return d
    return None


def ensure_overlay(doc: Dict[str, Any], plan_id: str) -> Dict[str, Any]:
    """Return the (mutable) overlay for plan_id, creating an empty one if absent.

    The caller mutates the returned dict and persists the doc. Old sessions without
    a plan_customizations key get one initialised here.
    """
    pc = doc.setdefault("plan_customizations", {})
    if plan_id not in pc:
        pc[plan_id] = empty_overlay()
    return pc[plan_id]


def _add_unique(lst: List[str], value: str) -> bool:
    """Append value to lst if not already present. Returns True if it was added."""
    if value in lst:
        return False
    lst.append(value)
    return True


def overlay_summary(overlay: Optional[Dict[str, Any]], plan_id: Optional[str]) -> Dict[str, Any]:
    """Small additive summary of a plan's customizations for GET /session."""
    ov = overlay or {}
    removed = ov.get("removed_activity_ids") or []
    saved = ov.get("saved_for_later_activity_ids") or []
    overrides = ov.get("activity_overrides") or {}
    added = ov.get("added_activities") or []
    swapped = sum(1 for o in overrides.values() if (o or {}).get("mode") == "swapped")
    return {
        "plan_id": plan_id,
        "removed_count": len(removed),
        "saved_for_later_count": len(saved),
        "swapped_count": swapped,
        "added_count": len(added),
        "has_customizations": bool(removed or saved or overrides or added),
    }


# ── Bank-based suggestions for swap / add (Beta 2.1 Step 2D) ─────────────────
# LLM-free: replacement/add cards are drawn from the session's already-generated,
# already-safety-filtered activity banks in brain_state["activity_banks"]. The
# banks are never mutated.

import re as _re
import uuid as _uuid
from datetime import datetime as _datetime, timezone as _dt_timezone

from api.adapters import DOMAIN_LABELS, _split_instructions_into_steps
from api.planning_period import WEEK_DAY_NAMES, _local_date

_DURATION_LABEL = "5–15 min"


def _norm_root(title: str) -> str:
    """Normalized title root for near-duplicate detection (mirrors activity_engine)."""
    t = (title or "").lower()
    t = _re.sub(r"[^a-z0-9\s]", " ", t)
    t = _re.sub(
        r"\b(easier|stretch|harder|advanced|supported|slow|quick|simple|easy|gentle|"
        r"basic|little|tiny|short|fun|new|my|your|our|a|the|an)\b", "", t)
    t = _re.sub(r"\b(game|activity|practice|challenge|time|session|version|exercise)\b", "", t)
    return _re.sub(r"\s+", " ", t).strip()


def iter_bank_activities(brain_state: Dict[str, Any]):
    """Yield (domain_key, bank_activity) for every activity in every domain bank."""
    for domain, bank in (brain_state.get("activity_banks") or {}).items():
        for act in (bank or {}).get("activities", []):
            yield domain, act


def suggestion_id_for(domain: str, bank_activity: Dict[str, Any]) -> str:
    """Deterministic, stateless suggestion id from (domain, title).

    Titles are de-duplicated within a bank, so (domain, title) is unique. GET and
    POST compute the same id, so a suggestion can be resolved without server state.
    """
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"sugg|{domain}|{bank_activity.get('title', '')}"))


def find_bank_activity_by_suggestion_id(brain_state: Dict[str, Any], suggestion_id: str):
    """Resolve a suggestion_id back to (domain, bank_activity), or None."""
    if not suggestion_id:
        return None
    for domain, act in iter_bank_activities(brain_state):
        if suggestion_id_for(domain, act) == suggestion_id:
            return domain, act
    return None


def _suggestion_preview(domain: str, act: Dict[str, Any]) -> Dict[str, Any]:
    """Parent-facing suggestion preview (no card id yet — minted on apply)."""
    instr = act.get("instructions", "")
    return {
        "suggestion_id": suggestion_id_for(domain, act),
        "title": act.get("title", ""),
        "domain": domain,
        "domain_label": DOMAIN_LABELS.get(domain, domain),
        "duration_label": _DURATION_LABEL,
        "why": act.get("why", ""),
        "instructions": instr,
        "instructions_steps": _split_instructions_into_steps(instr),
        "materials": act.get("materials", ""),
        "success_criteria": act.get("success", ""),
        "make_easier": act.get("easier", ""),
        "make_harder": act.get("harder", ""),
        "avoid": act.get("avoid", ""),
    }


def build_card_from_bank(
    session_id: str, plan_id: str, key: str, domain: str,
    bank_activity: Dict[str, Any], source_bank_type: str,
):
    """Build a full parent-facing card + its internal metadata block from a bank
    activity. `key` makes the new card id deterministic (for idempotency)."""
    dbg = bank_activity.get("_debug", {}) or {}
    new_id = str(_uuid.uuid5(
        _uuid.NAMESPACE_URL,
        f"{source_bank_type}|{session_id}|{plan_id}|{key}|{bank_activity.get('title', '')}",
    ))
    instr = bank_activity.get("instructions", "")
    card = {
        "id": new_id,
        "title": bank_activity.get("title", ""),
        "domain": domain,
        "domain_label": DOMAIN_LABELS.get(domain, domain),
        "duration_label": _DURATION_LABEL,
        "why": bank_activity.get("why", ""),
        "instructions": instr,
        "instructions_steps": _split_instructions_into_steps(instr),
        "materials": bank_activity.get("materials", ""),
        "success_criteria": bank_activity.get("success", ""),
        "make_easier": bank_activity.get("easier", ""),
        "make_harder": bank_activity.get("harder", ""),
        "group_play": bank_activity.get("group_play", ""),
        "avoid": bank_activity.get("avoid", ""),
    }
    internal = {
        "domain": domain,
        "subdomain": dbg.get("subdomain", ""),
        "milestone_text": dbg.get("milestone", ""),
        "milestone_age_months": None,
        "bridge_step_index": dbg.get("bridge_step_number"),
        "bridge_step_text": dbg.get("bridge_step_1", ""),
        "activity_family": dbg.get("activity_family", ""),
        "theme": bank_activity.get("theme", ""),
        "difficulty_level": "",
        "source_bank_type": source_bank_type,
        "weekend_mode": "",
        "support_tier": "",
    }
    return card, internal


def get_plan_activity(plan_response: Dict[str, Any], activity_id: str) -> Optional[Dict[str, Any]]:
    """Return the original generated card with this id, or None."""
    for day_entry in (plan_response or {}).get("week", []):
        for act in day_entry.get("activities", []):
            if act.get("id") == activity_id:
                return act
    return None


def find_plan_internal(plan_entry: Dict[str, Any], activity_id: str) -> Optional[Dict[str, Any]]:
    """Return the plan_internal record for activity_id (by frontend_id), or None."""
    pi = plan_entry.get("plan_internal") or {}
    for day_entry in pi.get("week", []):
        for act in day_entry.get("activities", []):
            if act.get("frontend_id") == activity_id:
                return act
    return None


def _excluded_titles(plan_response: Dict[str, Any], overlay: Optional[Dict[str, Any]]):
    """Titles to avoid suggesting: everything currently in the resolved plan, plus
    any originally-removed activity (don't re-suggest something the parent removed)."""
    resolved = resolve_plan_response(plan_response, overlay)
    titles = {(a.get("title", "") or "").strip().lower()
              for d in resolved.get("week", []) for a in d.get("activities", [])}
    removed_ids = set((overlay or {}).get("removed_activity_ids") or [])
    for d in (plan_response or {}).get("week", []):
        for a in d.get("activities", []):
            if a.get("id") in removed_ids:
                titles.add((a.get("title", "") or "").strip().lower())
    roots = {_norm_root(t) for t in titles}
    return titles, roots


def _activity_target_profile(
    doc: Dict[str, Any], plan_id: str, activity_id: str
) -> Optional[Dict[str, Any]]:
    """Skill-target profile (domain/subdomain/activity_family/bridge/title) for any
    VISIBLE activity, reading from the correct source:
      - original generated → plan_internal + plan_response card
      - added activity      → its overlay `internal` block + card title
      - swapped replacement → the override's `replacement_internal` + replacement title
    Returns None only if the id is not a current-plan activity.
    """
    plan_entry = (doc.get("plans") or {}).get(plan_id, {})
    plan_response = plan_entry.get("plan_response") or {}
    overlay = get_overlay(doc, plan_id) or {}

    def _profile(internal: Dict[str, Any], title: str) -> Dict[str, Any]:
        internal = internal or {}
        return {
            "domain": internal.get("domain", ""),
            "subdomain": internal.get("subdomain", ""),
            "activity_family": internal.get("activity_family", ""),
            "bridge_step_index": internal.get("bridge_step_index"),
            "title": title or "",
        }

    # Original generated activity.
    internal = find_plan_internal(plan_entry, activity_id)
    card = get_plan_activity(plan_response, activity_id)
    if internal is not None or card is not None:
        return _profile(internal or {}, (card or {}).get("title", ""))

    # Added activity.
    for item in overlay.get("added_activities") or []:
        a = item.get("activity") or {}
        if a.get("id") == activity_id:
            return _profile(item.get("internal") or {}, a.get("title", ""))

    # Visible swapped replacement.
    for ov in (overlay.get("activity_overrides") or {}).values():
        repl = (ov or {}).get("replacement_activity") or {}
        if repl.get("id") == activity_id:
            return _profile((ov or {}).get("replacement_internal") or {}, repl.get("title", ""))

    return None


def swap_suggestions(doc: Dict[str, Any], plan_id: str, activity_id: str, limit: int = 8):
    """Up to `limit` safe bank alternatives for ANY visible activity (original,
    added, or a swapped replacement). Prefers same domain, then activity_family /
    bridge_step / subdomain; falls back to broader bank activities (and, if the
    same-domain pool is empty or the target domain is unknown, cross-domain) rather
    than returning []. Output is de-duplicated by normalized title root so parents
    don't see several versions of the same activity. Bank-only, no OpenAI."""
    plan_entry = (doc.get("plans") or {}).get(plan_id, {})
    plan_response = plan_entry.get("plan_response") or {}
    overlay = get_overlay(doc, plan_id)
    profile = _activity_target_profile(doc, plan_id, activity_id) or {}
    domain = profile.get("domain", "")
    fam = profile.get("activity_family", "")
    bridge = profile.get("bridge_step_index")
    sub = profile.get("subdomain", "")
    target_title = (profile.get("title", "") or "").strip().lower()
    target_root = _norm_root(target_title)

    # Exclude everything currently visible (incl. same-day), removed titles, and the
    # target activity itself + its root variants.
    titles, roots = _excluded_titles(plan_response, overlay)
    if target_title:
        titles = titles | {target_title}
    if target_root:
        roots = roots | {target_root}
    brain_state = doc.get("brain_state") or {}

    def _gather(domain_only: bool):
        out = []
        for d, act in iter_bank_activities(brain_state):
            if domain_only and domain and d != domain:
                continue
            t = (act.get("title", "") or "").strip().lower()
            if not t or t in titles or _norm_root(t) in roots:
                continue
            dbg = act.get("_debug", {}) or {}
            score = 0
            if domain and d == domain:
                score += 8
            if fam and dbg.get("activity_family") == fam:
                score += 4
            if bridge is not None and dbg.get("bridge_step_number") == bridge:
                score += 3
            if sub and dbg.get("subdomain") == sub:
                score += 2
            out.append((score, t, d, act))
        return out

    # Prefer same domain; broaden to all domains only if that pool is empty.
    candidates = _gather(domain_only=True) if domain else _gather(domain_only=False)
    if not candidates:
        candidates = _gather(domain_only=False)
    candidates.sort(key=lambda x: (-x[0], x[1]))

    # De-duplicate by normalized root, keeping the highest-scored variant.
    previews: List[Dict[str, Any]] = []
    seen_roots: set = set()
    for _, t, d, act in candidates:
        r = _norm_root(t)
        if r in seen_roots:
            continue
        seen_roots.add(r)
        previews.append(_suggestion_preview(d, act))
        if len(previews) >= limit:
            break
    return previews


def add_suggestions(
    doc: Dict[str, Any], plan_id: str, domain_filter: Optional[str] = None, limit: int = 5,
):
    """Up to `limit` bank activities not already in the resolved plan (optionally
    filtered to one domain). Removed activities are not re-suggested."""
    plan_entry = (doc.get("plans") or {}).get(plan_id, {})
    plan_response = plan_entry.get("plan_response") or {}
    overlay = get_overlay(doc, plan_id)
    titles, roots = _excluded_titles(plan_response, overlay)
    brain_state = doc.get("brain_state") or {}

    out = []
    seen_roots = set()
    for d, act in iter_bank_activities(brain_state):
        if domain_filter and d != domain_filter:
            continue
        t = (act.get("title", "") or "").strip().lower()
        r = _norm_root(t)
        if not t or t in titles or r in roots or r in seen_roots:
            continue
        seen_roots.add(r)  # one card per normalized root → meaningfully different
        out.append(_suggestion_preview(d, act))
        if len(out) >= limit:
            break
    return out


def choose_add_day(doc: Dict[str, Any], plan_id: str) -> str:
    """Pick a day to add an activity: prefer today (if it's a day in the plan),
    else the day with the fewest activities (ties → earliest weekday)."""
    plan_entry = (doc.get("plans") or {}).get(plan_id, {})
    overlay = get_overlay(doc, plan_id)
    resolved = resolve_plan_response(plan_entry.get("plan_response") or {}, overlay)
    week = resolved.get("week", [])
    if not week:
        return ""
    today = _local_date(doc.get("timezone") or "UTC", _datetime.now(_dt_timezone.utc)).isoformat()
    for d in week:
        if d.get("date") == today:
            return d.get("day", "")

    def _key(d):
        day = d.get("day", "")
        idx = WEEK_DAY_NAMES.index(day) if day in WEEK_DAY_NAMES else 99
        return (len(d.get("activities", [])), idx)

    return min(week, key=_key).get("day", "")
