"""
genex_core/scoring.py
---------------------
Band-stage classification, developmental age estimation, and language scoring profile.
Extracted from genex_interview_activity_v11.ipynb — logic unchanged.
"""

from typing import Any, Dict, List, Optional

from genex_core.config import (
    MOTOR_EMERGING_SUBDOMAINS,
    MOTOR_EMERGING_PARTIAL_WEIGHT,
    MOTOR_EMERGING_NO_PENALTY,
    GENERAL_EMERGING_PARTIAL_WEIGHT,
    GENERAL_EMERGING_NO_PENALTY,
    LANGUAGE_SCORING_TRACKS,
)
from genex_core.interview_engine import ensure_concern_profile


def _band_has_motor_emphasis(items: List[Dict[str, Any]]) -> bool:
    if not items:
        return False
    subdomains = [str(x.get("subdomain", "unspecified")).strip().lower() for x in items]
    motor_count = sum(1 for s in subdomains if s in MOTOR_EMERGING_SUBDOMAINS)
    return motor_count / max(len(subdomains), 1) >= 0.50


def classify_band_stage(
    *,
    total: int,
    yes_count: int,
    partial_count: int,
    no_count: int,
    motor_emphasis: bool,
    min_yes_confirm: int = 2,
    yes_ratio_confirm: float = 0.60,
) -> Dict[str, Any]:
    """Classify a month band into confirmed / emerging / not_demonstrated."""
    yes_ratio = yes_count / total if total else 0.0
    positive_ratio = (yes_count + partial_count) / total if total else 0.0

    if yes_count >= min_yes_confirm and yes_ratio >= yes_ratio_confirm:
        return {
            "stage": "confirmed",
            "motor_emphasis": motor_emphasis,
            "evidence_score": round(2.0 + yes_ratio, 3),
            "yes_ratio": round(yes_ratio, 2),
            "positive_ratio": round(positive_ratio, 2),
        }

    partial_weight = MOTOR_EMERGING_PARTIAL_WEIGHT if motor_emphasis else GENERAL_EMERGING_PARTIAL_WEIGHT
    no_penalty = MOTOR_EMERGING_NO_PENALTY if motor_emphasis else GENERAL_EMERGING_NO_PENALTY

    evidence_score = (
        float(yes_count) * 1.0
        + float(partial_count) * partial_weight
        - float(no_count) * no_penalty
    )

    emerging = False

    if positive_ratio >= 0.67 and (yes_count > 0 or partial_count >= 2):
        emerging = True

    if motor_emphasis:
        if partial_count >= 2 and no_count <= 1:
            emerging = True
        if partial_count == total and total >= 2:
            emerging = True

    if emerging:
        return {
            "stage": "emerging",
            "motor_emphasis": motor_emphasis,
            "evidence_score": round(float(evidence_score), 3),
            "yes_ratio": round(yes_ratio, 2),
            "positive_ratio": round(positive_ratio, 2),
        }

    return {
        "stage": "not_demonstrated",
        "motor_emphasis": motor_emphasis,
        "evidence_score": round(float(evidence_score), 3),
        "yes_ratio": round(yes_ratio, 2),
        "positive_ratio": round(positive_ratio, 2),
    }


def summarize_answers_by_band(
    answers: List[Dict[str, Any]],
    min_yes_confirm: int = 2,
    yes_ratio_confirm: float = 0.60,
) -> Dict[int, Dict[str, Any]]:
    """Summarize answers by month band with 3-stage labels."""
    band_summary = {}

    for a in answers:
        month = int(a["months"])
        norm = a["norm_answer"]

        if month not in band_summary:
            band_summary[month] = {
                "total": 0, "yes": 0, "partial": 0, "no": 0, "items": []
            }

        band_summary[month]["total"] += 1
        band_summary[month]["items"].append({
            "milestone": a["milestone"],
            "subdomain": a.get("subdomain", "unspecified"),
            "answer": norm,
        })

        if norm == "yes":
            band_summary[month]["yes"] += 1
        elif norm in {"sometimes", "with_help", "not_sure"}:
            band_summary[month]["partial"] += 1
        else:
            band_summary[month]["no"] += 1

    for month in band_summary:
        total = band_summary[month]["total"]
        yes_count = band_summary[month]["yes"]
        partial_count = band_summary[month]["partial"]
        no_count = band_summary[month]["no"]

        motor_emphasis = _band_has_motor_emphasis(band_summary[month]["items"])
        stage_info = classify_band_stage(
            total=total,
            yes_count=yes_count,
            partial_count=partial_count,
            no_count=no_count,
            motor_emphasis=motor_emphasis,
            min_yes_confirm=min_yes_confirm,
            yes_ratio_confirm=yes_ratio_confirm,
        )

        band_summary[month]["yes_ratio"] = round(yes_count / total, 2) if total else 0.0
        band_summary[month]["stage"] = stage_info["stage"]
        band_summary[month]["motor_emphasis"] = motor_emphasis
        band_summary[month]["evidence_score"] = stage_info["evidence_score"]
        band_summary[month]["positive_ratio"] = stage_info["positive_ratio"]

    return dict(sorted(band_summary.items()))


def compute_dev_age_from_answers(
    answers: List[Dict[str, Any]],
    min_yes_confirm: int = 2,
    yes_ratio_confirm: float = 0.60,
) -> int:
    """Estimate developmental age using confirmed / emerging / not_demonstrated band stages."""
    if not answers:
        return 6

    band_summary = summarize_answers_by_band(
        answers,
        min_yes_confirm=min_yes_confirm,
        yes_ratio_confirm=yes_ratio_confirm,
    )

    answered_months = sorted(band_summary.keys())
    confirmed_months = [m for m, info in band_summary.items() if info["stage"] == "confirmed"]

    if confirmed_months:
        return int(max(confirmed_months))

    emerging_months = [m for m, info in band_summary.items() if info["stage"] == "emerging"]
    if emerging_months:
        best_month = sorted(
            emerging_months,
            key=lambda m: (band_summary[m]["evidence_score"], m),
            reverse=True,
        )[0]
        return int(best_month)

    return int(min(answered_months))


def _answers_for_subdomains(
    answers: List[Dict[str, Any]],
    subdomains: set,
) -> List[Dict[str, Any]]:
    return [
        a for a in answers
        if str(a.get("subdomain", "unspecified")) in subdomains
    ]


def compute_language_scoring_profile(
    state: Dict[str, Any],
    min_yes_confirm: int = 2,
    yes_ratio_confirm: float = 0.60,
) -> Dict[str, Any]:
    """Compute a split scoring profile for Language / Communication.

    Separates expressive/speech, receptive, and gestural tracks to avoid
    strong comprehension or gesture masking expressive/speech weakness.
    """
    raw_dev_age = state.get("dev_age", {}).get("language_and_communication")
    qna_answers = [
        a for a in state.get("qna", {}).get("language_and_communication", [])
        if a.get("answer_status", "ok") != "api_error"
    ]

    if raw_dev_age is None or not qna_answers:
        return {
            "raw_dev_age_months": raw_dev_age,
            "effective_dev_age_months": raw_dev_age,
            "track_dev_ages": {},
            "track_weights": {},
            "track_counts": {},
        }

    track_dev_ages = {}
    track_counts = {}
    for track_name, cfg in LANGUAGE_SCORING_TRACKS.items():
        subset = _answers_for_subdomains(qna_answers, cfg["subdomains"])
        track_counts[track_name] = len(subset)
        if subset:
            track_dev_ages[track_name] = compute_dev_age_from_answers(
                subset,
                min_yes_confirm=min_yes_confirm,
                yes_ratio_confirm=yes_ratio_confirm,
            )

    concern_profile = ensure_concern_profile(state)
    sub_w = concern_profile.get("subdomain_weights", {})

    expressive_pressure = max(
        float(sub_w.get("expressive_language", 0.0)),
        float(sub_w.get("speech_intelligibility", 0.0)),
        float(sub_w.get("early_vocalization_and_babbling", 0.0)),
        float(sub_w.get("conversation_narrative", 0.0)),
    )
    receptive_pressure = float(sub_w.get("receptive_language", 0.0))
    gesture_pressure = float(sub_w.get("gestural_communication", 0.0))

    track_weights = {}
    if track_counts.get("expressive_speech", 0) > 0:
        track_weights["expressive_speech"] = max(0.35, expressive_pressure)
    if track_counts.get("receptive", 0) > 0:
        track_weights["receptive"] = max(0.15, receptive_pressure)
    if track_counts.get("gesture", 0) > 0:
        track_weights["gesture"] = min(0.35, max(0.05, gesture_pressure * 0.5))

    if not track_weights:
        effective_age = raw_dev_age
    else:
        weighted_total = 0.0
        weight_sum = 0.0
        for track_name, weight in track_weights.items():
            age = track_dev_ages.get(track_name, raw_dev_age)
            weighted_total += float(age) * float(weight)
            weight_sum += float(weight)

        weighted_age = round(weighted_total / max(weight_sum, 1e-9))
        effective_age = min(int(raw_dev_age), int(weighted_age))

        if expressive_pressure >= 0.50 and "expressive_speech" in track_dev_ages:
            effective_age = min(effective_age, int(track_dev_ages["expressive_speech"]))

    return {
        "raw_dev_age_months": int(raw_dev_age),
        "effective_dev_age_months": int(effective_age),
        "track_dev_ages": track_dev_ages,
        "track_weights": {k: round(v, 2) for k, v in track_weights.items()},
        "track_counts": track_counts,
    }


def finalize_domain_dev_age(state: Dict[str, Any], category_key: str) -> None:
    """After QnA for a domain, compute and store dev_age in state."""
    answers = [
        a for a in state.get("qna", {}).get(category_key, [])
        if a.get("answer_status", "ok") != "api_error"
    ]
    dev_age = compute_dev_age_from_answers(answers)
    state["dev_age"][category_key] = dev_age


def get_effective_dev_age(state: Dict[str, Any], category_key: str) -> Optional[int]:
    """Return effective dev age (uses language split scoring for language domain)."""
    raw = state.get("dev_age", {}).get(category_key)
    if raw is None:
        return None
    if category_key != "language_and_communication":
        return int(raw)
    profile = compute_language_scoring_profile(state)
    eff = profile.get("effective_dev_age_months", raw)
    return int(eff) if eff is not None else int(raw)
