"""
genex_core/activity_engine.py
-----------------------------
Home-support activity bank generation per domain.
Uses OpenAI if available; falls back to milestone-based deterministic activities.
AI is used only for parent-friendly wording — scoring and tier logic are deterministic.
"""

import json
import os
from typing import Any, Dict, List, Optional

from genex_core.config import DOMAIN_CONFIG
from genex_core.interview_engine import ensure_concern_profile
from genex_core.safety import (
    ensure_safety_profile,
    format_safety_constraints_for_prompt,
    apply_safety_constraints_to_activities,
    is_context_dependent_bonus_activity,
)
from genex_core.support_tiers import (
    get_support_tier,
    no_special_support_needed,
    is_family_guidance_category,
    select_next_milestones,
    compute_support_metrics,
)
from genex_core.scoring import get_effective_dev_age

# Lazy OpenAI client
_openai_client = None
_openai_initialized = False


def _get_openai_client():
    global _openai_client, _openai_initialized
    if _openai_initialized:
        return _openai_client
    _openai_initialized = True
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if api_key:
        try:
            from openai import OpenAI
            _openai_client = OpenAI(api_key=api_key)
        except ImportError:
            _openai_client = None
    return _openai_client


def get_category_activity_guardrails(category_key: str) -> str:
    """Return domain-specific activity guardrails to reduce cross-domain drift."""
    if category_key == "movement_and_physical":
        return (
            "Category-specific rules for Movement / Physical:\n"
            "- Focus on posture, strength, balance, coordination, reaching, grasping, sitting, "
            "crawling, standing, walking, or fine-motor use.\n"
            "- The main goal should be physical skill practice.\n"
            "- Do NOT make this mainly about speech, naming, or social-emotional labeling."
        )
    if category_key == "language_and_communication":
        return (
            "Category-specific rules for Language / Communication:\n"
            "- Focus on sounds, babbling, turn-taking vocalization, following simple verbal directions, "
            "gestures for communication, word use, imitation of sounds, naming, requesting, "
            "commenting, comprehension, or speech clarity.\n"
            "- The main goal should be communication.\n"
            "- Do NOT make this mainly about gross motor practice."
        )
    if category_key == "social_and_emotional":
        return (
            "Category-specific rules for Social / Emotional:\n"
            "- Focus on eye contact, shared attention, social reciprocity, imitation of facial "
            "expressions, joint play, turn-taking, response to name, emotional connection, "
            "emotional regulation, or interaction with caregiver or peers.\n"
            "- The main goal should be social engagement or emotional regulation."
        )
    if category_key == "cognitive":
        return (
            "Category-specific rules for Cognitive / Adaptive:\n"
            "- Focus on attention, problem-solving, object permanence, simple cause-and-effect, "
            "routines, imitation of actions, functional play, early self-help, or following directions.\n"
            "- The main goal should be cognitive/adaptive skill building."
        )
    return ""


def _milestone_to_activity_title(milestone: str, category_key: str) -> str:
    """Convert a CDC milestone description into a short activity-style title."""
    DOMAIN_VERB = {
        "movement_and_physical": "Move & Play",
        "language_and_communication": "Talk & Listen",
        "social_and_emotional": "Connect & Play",
        "cognitive": "Think & Explore",
    }
    prefix = DOMAIN_VERB.get(category_key, "Practice")
    # Take the key verb + object from the milestone (first 4 words)
    words = milestone.strip().split()
    core = " ".join(words[:4]).rstrip(".,;:")
    return f"{prefix}: {core.lower()}"


# ---------------------------------------------------------------------------
# Fallback helpers: materials + instructions keyed to domain and milestone text
# ---------------------------------------------------------------------------

_MOVEMENT_KEYWORD_MATERIALS = [
    (["walk", "steps", "stand", "balance"],
     "non-slip floor space, furniture to hold for balance"),
    (["crawl", "creep", "climb"],
     "soft mat or blanket on the floor, low cushions or pillows"),
    (["reach", "grasp", "pick up", "transfer", "hold"],
     "small soft toy, block, or safe household object to grasp"),
    (["sit", "seated", "upright"],
     "firm surface or supported seat, small pillow for positioning"),
    (["kick", "jump", "hop", "run"],
     "open floor space, soft ball"),
    (["roll", "tummy"],
     "soft mat or blanket on the floor"),
    (["throw", "catch"],
     "soft ball or balloon"),
    (["draw", "scribble", "pincer", "finger", "stacking"],
     "large crayons or chunky blocks, plain paper"),
]

_LANGUAGE_KEYWORD_MATERIALS = [
    (["sound", "babble", "vocali", "coo"],
     "quiet room, face-to-face positioning; no materials needed"),
    (["word", "say", "name", "label", "request"],
     "favourite toy or familiar object to name and request"),
    (["gesture", "point", "wave", "sign"],
     "familiar objects placed slightly out of reach"),
    (["follow", "direction", "listen", "command"],
     "two or three simple familiar objects (cup, ball, shoe)"),
    (["book", "story", "picture", "read"],
     "simple board book with clear pictures"),
    (["song", "rhyme", "sing"],
     "a short nursery rhyme or favourite song; no materials needed"),
    (["imitat", "copy"],
     "mirror or a face-to-face space"),
]

_SOCIAL_KEYWORD_MATERIALS = [
    (["eye contact", "look at", "gaze"],
     "quiet room; face-to-face positioning — no materials needed"),
    (["smile", "laugh", "facial"],
     "mirror; no other materials needed"),
    (["turn-tak", "back-and-forth", "reciproc"],
     "simple cause-and-effect toy or soft ball for rolling back and forth"),
    (["play", "peer", "friend", "sibling"],
     "a shared toy such as blocks, simple board game, or ball"),
    (["imitat", "copy", "mimic"],
     "common household objects (spoon, cup) to imitate in play"),
    (["comfort", "regulat", "calm", "sooth"],
     "comfort item (stuffed animal, blanket), calm space"),
    (["response to name", "respond", "attention"],
     "no materials; use child's name and a favourite toy as reward"),
    (["separat", "goodbye", "transition"],
     "a transitional comfort object; predictable routine cues"),
]

_COGNITIVE_KEYWORD_MATERIALS = [
    (["object permanence", "hide", "peek", "under"],
     "small toy and a cloth or cup to hide it under"),
    (["cause", "effect", "push", "button", "activate"],
     "simple cause-and-effect toy (pop-up, light-up toy)"),
    (["sort", "match", "categor"],
     "two sets of objects by colour or shape (blocks, cups)"),
    (["stack", "nest", "put in", "container"],
     "stacking cups or nesting bowls"),
    (["puzzle", "shape"],
     "simple shape sorter or 2–4 piece wooden puzzle"),
    (["imitat", "pretend", "play with"],
     "everyday objects for imitation (spoon, phone, cup)"),
    (["routine", "self-help", "dress", "feed", "wash"],
     "child's clothing items, spoon or cup, or a wash cloth"),
    (["follow", "direction", "instruction"],
     "two or three familiar household objects"),
    (["attention", "focus", "concentrate"],
     "preferred toy or activity with minimal distractions"),
]

_DOMAIN_DEFAULT_MATERIALS = {
    "movement_and_physical": "open floor space, soft mat, age-appropriate small toys",
    "language_and_communication": "familiar toys, simple board book, face-to-face space",
    "social_and_emotional": "quiet space, favourite toy, mirror",
    "cognitive": "simple household objects, blocks, cause-and-effect toy",
}

_DOMAIN_KEYWORD_MATERIALS = {
    "movement_and_physical": _MOVEMENT_KEYWORD_MATERIALS,
    "language_and_communication": _LANGUAGE_KEYWORD_MATERIALS,
    "social_and_emotional": _SOCIAL_KEYWORD_MATERIALS,
    "cognitive": _COGNITIVE_KEYWORD_MATERIALS,
}


def _infer_materials(milestone_text: str, category_key: str) -> str:
    """Pick the most specific materials string for this milestone."""
    text = milestone_text.lower()
    for keywords, materials in _DOMAIN_KEYWORD_MATERIALS.get(category_key, []):
        if any(kw in text for kw in keywords):
            return materials
    return _DOMAIN_DEFAULT_MATERIALS.get(category_key, "common household items")


# ---------------------------------------------------------------------------
# Domain-specific instruction templates
# ---------------------------------------------------------------------------

def _build_fallback_instructions(
    child_name: str,
    milestone_text: str,
    category_key: str,
    materials: str,
) -> str:
    """Return a warm, specific instruction paragraph for one fallback activity."""
    text = milestone_text.lower()

    if category_key == "movement_and_physical":
        if any(k in text for k in ["walk", "steps", "stand", "balance"]):
            return (
                f"Set up a safe open space for {child_name} to practice standing or walking. "
                f"Stand or kneel a short distance away and hold out your hands as a target. "
                f"Cheer every attempt — even one step counts. "
                f"Try 3–5 minutes and stop before {child_name} gets tired or frustrated."
            )
        if any(k in text for k in ["reach", "grasp", "pick up", "transfer", "hold"]):
            return (
                f"Place a small object just within {child_name}'s reach and encourage them to "
                f"pick it up or pass it between hands. You can slowly move the object to one "
                f"side to encourage reaching across the midline. Keep sessions short — 3 minutes "
                f"is plenty. Celebrate every grasp with a smile or clap."
            )
        if any(k in text for k in ["crawl", "creep", "climb"]):
            return (
                f"Put a favourite toy just beyond {child_name}'s reach on a soft mat. "
                f"Encourage them to crawl or creep toward it. You can also create a simple "
                f"obstacle path with cushions to crawl around or over. Keep it playful — "
                f"3 to 5 minutes at a time."
            )
        if any(k in text for k in ["roll", "tummy"]):
            return (
                f"Place {child_name} on a soft mat for tummy time or rolling practice. "
                f"Get down to their level and use a toy or your face to encourage head lifting "
                f"and weight shifting. Even 2–3 minutes of tummy time several times a day adds up. "
                f"Stop if {child_name} becomes upset."
            )
        if any(k in text for k in ["draw", "scribble", "pincer", "finger"]):
            return (
                f"Set up a flat surface with large crayons or safe objects for {child_name} to "
                f"handle. Demonstrate picking up, transferring, and placing the item, then let "
                f"{child_name} try. Narrate what you see: 'You're picking it up!' Aim for 3–5 minutes."
            )
        # generic movement
        return (
            f"Create a short movement game around this skill: {milestone_text}. "
            f"Get on the floor with {child_name}, demonstrate the movement, and invite them to try. "
            f"Keep sessions to 3–5 minutes and follow {child_name}'s energy level."
        )

    if category_key == "language_and_communication":
        if any(k in text for k in ["sound", "babble", "vocali", "coo"]):
            return (
                f"During a calm, quiet moment, get face-to-face with {child_name}. "
                f"Make a simple sound (like 'ba' or 'ma') and wait a few seconds — leave space "
                f"for {child_name} to respond. Mirror any sounds they make back to them. "
                f"This back-and-forth turn-taking builds the foundation for conversation. "
                f"3–5 minutes is enough."
            )
        if any(k in text for k in ["word", "say", "name", "label", "request"]):
            return (
                f"During play or daily routines, hold up a familiar object and name it clearly: "
                f"'Ball!' or 'Cup!' Pause and give {child_name} time to try the word or a gesture. "
                f"Accept any attempt — a sound or reaching counts. Don't prompt more than twice "
                f"in a row. Repeat naturally across the day rather than in a drill."
            )
        if any(k in text for k in ["follow", "direction", "listen", "command"]):
            return (
                f"During a familiar routine (like tidying up or getting ready), give {child_name} "
                f"one simple direction: 'Give me the cup.' Start with objects right in front of them. "
                f"Demonstrate first if needed, then repeat the direction and wait. Build up to "
                f"two-step directions once one-step is solid."
            )
        if any(k in text for k in ["book", "story", "picture", "read"]):
            return (
                f"Sit with {child_name} and open a simple picture book. Point to images and name "
                f"them: 'Dog! The dog is running.' Pause to let {child_name} point or make a sound. "
                f"You don't need to read every word — pointing and naming pictures is the goal. "
                f"5 minutes of shared book time, once or twice a day, makes a real difference."
            )
        if any(k in text for k in ["gesture", "point", "wave"]):
            return (
                f"Throughout the day, use and encourage gestures alongside words. "
                f"Wave 'bye-bye', point to things you see together, or hold out your hand to "
                f"request an object. When {child_name} points, immediately name what they're "
                f"pointing at. This connects gesture with meaning."
            )
        # generic language
        return (
            f"During play or a daily routine, create a natural opportunity for "
            f"{child_name} to practice: {milestone_text}. "
            f"Keep your language simple and clear. Pause and wait after you model — "
            f"give {child_name} at least 5 seconds to respond before prompting again."
        )

    if category_key == "social_and_emotional":
        if any(k in text for k in ["eye contact", "look at", "gaze"]):
            return (
                f"Get down to {child_name}'s level and place a favourite toy next to your face. "
                f"When {child_name} looks at the toy, slowly move it toward your eyes so their "
                f"gaze shifts to your face. Smile and react warmly the moment they make eye contact. "
                f"Keep moments short — 1–2 seconds of contact is a real success."
            )
        if any(k in text for k in ["turn-tak", "back-and-forth"]):
            return (
                f"Roll a ball back and forth, take turns stacking a block, or pass an object "
                f"between you. Each exchange is one turn. Narrate the turn-taking: 'My turn… "
                f"your turn!' Start with just 3–4 exchanges and build from there. "
                f"End before {child_name} loses interest."
            )
        if any(k in text for k in ["comfort", "regulat", "calm", "sooth"]):
            return (
                f"When {child_name} is starting to feel upset (not at peak distress), practice "
                f"a simple calming routine together: deep breaths, a gentle squeeze, or a comfort "
                f"object. Name the feeling calmly: 'You're frustrated. Let's take a breath.' "
                f"Stay close and keep your own voice and body calm."
            )
        if any(k in text for k in ["imitat", "copy", "mimic"]):
            return (
                f"Sit facing {child_name} and do a simple action — clap, wave, tap the table. "
                f"Then wait. If {child_name} copies you, copy them right back. This imitation "
                f"game builds social connection and attention. Keep it playful and take turns "
                f"being the leader. 3–5 minutes is ideal."
            )
        if any(k in text for k in ["response to name", "respond", "attention"]):
            return (
                f"From across the room or a short distance, say {child_name}'s name once in a "
                f"warm, clear voice — no other words. Wait up to 5 seconds. If they turn or look, "
                f"react with big enthusiasm. If not, move a little closer and try again. "
                f"Practice during natural moments throughout the day (not as a drill)."
            )
        # generic social
        return (
            f"Create a short, calm social moment around this goal: {milestone_text}. "
            f"Be face-to-face with {child_name}, keep distractions low, and follow their lead. "
            f"Celebrate any social bid — a look, a smile, a gesture — with warm attention."
        )

    if category_key == "cognitive":
        if any(k in text for k in ["hide", "peek", "object permanence", "under"]):
            return (
                f"Play a simple hiding game: show {child_name} a small toy, then cover it "
                f"with a cloth while they watch. Ask 'Where did it go?' and wait. Lift the "
                f"cloth together if needed at first. Gradually make it trickier by using "
                f"two cloths. 3–5 minutes per session."
            )
        if any(k in text for k in ["sort", "match", "categor"]):
            return (
                f"Set out two groups of objects (e.g., red and blue blocks, or animals and "
                f"vehicles). Sort one item yourself and narrate: 'This is red — it goes here.' "
                f"Then hand {child_name} one and wait. Don't correct errors harshly — just "
                f"model the right sort and move on."
            )
        if any(k in text for k in ["stack", "nest", "put in", "container"]):
            return (
                f"Put out stacking cups or nesting bowls. Demonstrate stacking 2–3 and then "
                f"knock them down — make it fun! Hand {child_name} a cup and wait to see "
                f"what they do. Encourage placing, nesting, or stacking. Keep it playful; "
                f"3–5 minutes is enough."
            )
        if any(k in text for k in ["cause", "effect", "push", "button"]):
            return (
                f"Use a cause-and-effect toy (e.g., one where pressing a button makes something "
                f"pop up or light up). Let {child_name} explore freely first. If they get stuck, "
                f"point to the button: 'Try pushing here.' Name the result: 'You pushed it — "
                f"it popped!' Repeat 5–6 times."
            )
        if any(k in text for k in ["routine", "self-help", "dress", "feed", "wash"]):
            return (
                f"During an everyday routine (dressing, mealtimes, washing hands), slow down "
                f"and give {child_name} a chance to participate. Hand them the sock to pull on, "
                f"or guide them to hold the spoon. Use simple, consistent words each time: "
                f"'Your turn — pull it up!' Repetition across daily routines builds the skill."
            )
        if any(k in text for k in ["pretend", "play with", "imagin"]):
            return (
                f"Set out a few simple props (a cup, spoon, toy phone, or stuffed animal) and "
                f"model a short pretend action: feed the teddy, talk on the phone. Then step "
                f"back and see if {child_name} imitates or extends the play. Join in but "
                f"don't direct — follow {child_name}'s imagination."
            )
        # generic cognitive
        return (
            f"Set up a brief, focused activity for {child_name} to practice: {milestone_text}. "
            f"Demonstrate once, then wait and let them try. Offer help only if they seem stuck. "
            f"Keep it to 3–5 minutes — short and successful beats long and frustrating."
        )

    # Fallback for unknown domain
    return (
        f"Help {child_name} practice: {milestone_text}. "
        f"Turn it into a short, playful activity at home — aim for 3–5 minutes. "
        f"Follow {child_name}'s lead and keep the mood light and positive."
    )


def _tier_to_display(planning_tier: str) -> str:
    """Convert a raw tier key to a parent-friendly label."""
    return {
        "needs_special_support": "Extra Support",
        "monitor_and_enrich": "Monitor & Enrich",
        "enrich_and_observe": "Monitor & Enrich",
        "no_special_support": "On Track",
    }.get(planning_tier, planning_tier)


def _make_fallback_activities(
    state: Dict[str, Any],
    category_key: str,
    planning_tier: str,
    next_steps: Dict[str, Any],
    activities_per_category: int,
) -> List[Dict[str, Any]]:
    """
    Deterministic fallback when no OpenAI key is configured.
    Generates activity cards from milestone targets with domain-specific
    materials, instruction templates, and readable goal labels.
    NOTE: Add OPENAI_API_KEY via Secret Manager (see DEPLOY.md) for full AI-generated activities.
    """
    child_name = state.get("child", {}).get("name", "your child")
    category_display = DOMAIN_CONFIG[category_key]["display"]
    goal_display = _tier_to_display(planning_tier)
    fallback_activities = []

    for i, m in enumerate(next_steps.get("milestones", [])[:activities_per_category], start=1):
        milestone_text = m["milestone"]
        title = _milestone_to_activity_title(milestone_text, category_key)
        materials = _infer_materials(milestone_text, category_key)
        instructions = _build_fallback_instructions(child_name, milestone_text, category_key, materials)
        fallback_activities.append({
            "activity_id": f"{category_key}_{i}",
            "title": title,
            "instructions": instructions,
            "duration_min": 5,
            "materials": materials,
            "level": "current_or_next",
            "goal": goal_display,
            "category": category_display,
            "is_extended_activity": False,
            "extended_reason": "",
        })

    while len(fallback_activities) < activities_per_category:
        i = len(fallback_activities) + 1
        default_materials = _DOMAIN_DEFAULT_MATERIALS.get(category_key, "common household items")
        fallback_activities.append({
            "activity_id": f"{category_key}_{i}",
            "title": f"{category_display} Activity {i}",
            "instructions": (
                f"Set up a short, calm activity with {child_name} that focuses on "
                f"{category_display.lower()} skills. Keep it to 3–5 minutes, follow "
                f"{child_name}'s lead, and stop before frustration sets in."
            ),
            "duration_min": 5,
            "materials": default_materials,
            "level": "current_or_next",
            "goal": goal_display,
            "category": category_display,
            "is_extended_activity": False,
            "extended_reason": "",
        })

    return apply_safety_constraints_to_activities(state, category_key, fallback_activities)


def generate_category_activity_bank(
    state: Dict[str, Any],
    category_key: str,
    activities_per_category: int = 6,
) -> Dict[str, Any]:
    """Generate one activity bank per category.

    AI (OpenAI) is used here only for parent-friendly activity wording.
    Tier assignment and milestone selection are always deterministic.
    Falls back gracefully if OpenAI is unavailable.
    """
    child = state["child"]
    category_display = DOMAIN_CONFIG[category_key]["display"]
    support_tier = get_support_tier(state, category_key)
    soft_floor_active = is_family_guidance_category(state, category_key)
    planning_tier = "enrich_and_observe" if soft_floor_active else support_tier
    support_metrics = compute_support_metrics(state, category_key)
    safety_profile = ensure_safety_profile(state)
    safety_constraints_block = format_safety_constraints_for_prompt(safety_profile)
    next_steps = select_next_milestones(state, category_key)

    if next_steps["status"] == "no_special_support":
        result = {
            "status": "no_special_support",
            "support_tier": support_tier,
            "planning_tier": planning_tier,
            "summary": next_steps["message"],
            "activities": [],
        }
        state["activity_banks"][category_key] = result
        return result

    chrono_months = min(child["chronological_months"], 60)
    dev_age = chrono_months if soft_floor_active else state["dev_age"].get(category_key, chrono_months)
    milestone_gap = max(0, chrono_months - dev_age)
    category_guardrails = get_category_activity_guardrails(category_key)

    soft_floor_block = ""
    if soft_floor_active:
        soft_floor_block = (
            "This category is being generated under the family guidance floor. "
            "Use age-appropriate, low-intensity enrichment and observation activities. "
            "Do not imply therapy-level intensity or a significant delay. "
            "The goal is support, confidence-building, and structured observation."
        )

    milestone_lines = "\n".join([
        f"- ({m['months']} months | {m.get('subdomain', 'unspecified')}) {m['milestone']}"
        for m in next_steps["milestones"]
    ]) or "- No specific milestone items available in this range."

    client = _get_openai_client()

    if client is None:
        fallback = _make_fallback_activities(state, category_key, planning_tier, next_steps, activities_per_category)
        result = {
            "status": "fallback",
            "support_tier": support_tier,
            "planning_tier": planning_tier,
            "summary": f"Fallback activity bank for {category_display} (no OpenAI key configured).",
            "activities": fallback,
        }
        state["activity_banks"][category_key] = result
        return result

    prompt = f"""
You are a pediatric home-support planning agent helping a parent at home.

This is NOT a diagnosis and NOT a formal treatment plan.
Create a CATEGORY ACTIVITY BANK, not a day-by-day schedule.

Child:
- Name: {child['name']}
- Chronological age: {child['chronological_months']} months
- Diagnosis / condition: {child['diagnosis']}
- Parent concern: {child['concern']}
- Category: {category_display}
- Support tier for this category: {planning_tier}
- Estimated developmental age in this category: {dev_age} months
- Estimated milestone gap in this category: {milestone_gap} months
- Continuous support score: {support_metrics['support_score']}

Relevant milestone targets:
{milestone_lines}

Category-specific guardrails:
{category_guardrails}

Safety / practical constraints inferred from diagnosis + concern:
{safety_constraints_block}

{soft_floor_block}

Task:
Create {activities_per_category} realistic home activities for this category.

Instructions:
1. Activities must fit the child's chronological age and estimated developmental level.
2. Activities should be practical for home use.
3. Keep language parent-friendly and warm.
4. Include a mix of current-level practice and near next-step practice.
5. Most activities should be short and repeatable: usually 3, 5, 7, or 10 minutes.
6. Context-dependent activities (playdates, park, playground, group) must NEVER be written as normal daily home activities.
7. If you include such an activity, mark is_extended_activity as true, duration 30-45 min.
8. Max 1 context-dependent bonus activity per category.
9. Each activity must clearly belong to THIS category.
10. Avoid cross-domain drift.
11. Set "goal" to exactly: {planning_tier}
12. Return strict JSON only.

Required JSON:
{{
  "summary": "...",
  "activities": [
    {{
      "activity_id": "1",
      "title": "...",
      "instructions": "...",
      "duration_min": 5,
      "materials": "...",
      "level": "current_or_next",
      "goal": "{planning_tier}",
      "category": "{category_display}",
      "is_extended_activity": false,
      "extended_reason": ""
    }}
  ]
}}
""".strip()

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.3,
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You return strict JSON only and stay non-diagnostic, "
                        "practical, and parent-friendly."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )

        bank = json.loads(resp.choices[0].message.content)

        if "activities" not in bank or not isinstance(bank["activities"], list):
            bank["activities"] = []

        for idx, activity in enumerate(bank["activities"], start=1):
            activity["activity_id"] = activity.get("activity_id", f"{category_key}_{idx}")
            activity["category"] = activity.get("category", category_display)
            activity["duration_min"] = activity.get("duration_min", 5)
            activity["materials"] = activity.get("materials", "common household items")
            activity["level"] = activity.get("level", "current_or_next")
            activity["goal"] = planning_tier
            activity["is_extended_activity"] = activity.get("is_extended_activity", False)
            activity["extended_reason"] = activity.get("extended_reason", "")

        activities = apply_safety_constraints_to_activities(
            state, category_key, bank["activities"][:activities_per_category]
        )

        result = {
            "status": "success",
            "support_tier": support_tier,
            "planning_tier": planning_tier,
            "summary": bank.get("summary", f"Created activity bank for {category_display}."),
            "activities": activities,
        }
        state["activity_banks"][category_key] = result
        return result

    except Exception as e:
        fallback = _make_fallback_activities(state, category_key, planning_tier, next_steps, activities_per_category)
        result = {
            "status": "fallback",
            "support_tier": support_tier,
            "planning_tier": planning_tier,
            "summary": f"Fallback activity bank for {category_display} (OpenAI failed: {e})",
            "activities": fallback,
        }
        state["activity_banks"][category_key] = result
        return result
