"""
genex_core/activity_engine.py
------------------------------
V22 activity bank generation.

Rules:
- activity_family is a hard guardrail — LLM cannot switch domains.
- LLM is used only for parent-friendly wording (title, theme, instructions).
  All scoring, routing, and bridge selection are deterministic.
- Child first name is NEVER sent to the LLM ("your child" always).
- Uses ACTIVITY_MODEL env var; falls back to deterministic text if not set.
- initial plans: bridge_step_number = 1 only (enforced by bridge_selector).
- previous_bridge_step stored in activity debug fields but NOT used.
- Validators run before any activity is returned.
- Parent-facing card schema:
    title, duration_minutes, why, instructions, success,
    easier, harder, group_play, avoid, materials, feedback_options
- Debug-only fields (in _debug sub-dict):
    subdomain, milestone, bridge_step_1, activity_family,
    planning_mode, source_table_row, validation_warnings
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from genex_core.activity_validator import filter_valid_activities
from genex_core.bridge_selector import build_bridge_plan_for_category, select_next_milestones
from genex_core.config import (
    ACTIVITY_FEEDBACK_OPTIONS,
    ACTIVITY_MODEL,
    DOMAIN_CONFIG,
    ENGINE_VERSION,
    V22_MAX_MILESTONES_PER_DOMAIN,
    V22_MIN_MILESTONES_PER_DOMAIN,
    V22_PER_ACTIVITY_MIN,
    V22_WEEK1_DAYS,
    V22_MAX_DAILY_ACTIVITIES,
)
from genex_core.interview_engine import ensure_concern_profile
from genex_core.safety import (
    ensure_safety_profile,
    format_safety_constraints_for_prompt,
    apply_safety_constraints_to_activities,
)
from genex_core.table_loader import get_family_description

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OpenAI client (lazy)
# ---------------------------------------------------------------------------

_openai_client = None
_openai_initialized = False


def _get_openai_client():
    global _openai_client, _openai_initialized
    if _openai_initialized:
        return _openai_client
    _openai_initialized = True
    if not ACTIVITY_MODEL:
        logger.warning(
            "[activity_engine] ACTIVITY_MODEL env var not set. "
            "Using deterministic fallback activity wording."
        )
        return None
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        logger.warning("[activity_engine] OPENAI_API_KEY not set.")
        return None
    try:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=api_key)
    except ImportError:
        logger.warning("[activity_engine] openai package not installed.")
        _openai_client = None
    return _openai_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _clean(value: Any) -> str:
    s = str(value or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _family_bucket(fam: str, category_key: str = "") -> str:
    fam = _norm(fam)
    patterns = [
        ("book_page", r"book_page|page_turn"),
        ("fork_spoon", r"fork|spoon|utensil|feeding"),
        ("dressing_on", r"dressing_on|clothes_on"),
        ("dressing_off", r"dressing_off|clothes_off"),
        ("buttoning", r"button|fastener|zipper"),
        ("beading", r"bead|thread|peg|pincer|grasp|prewriting|scribble|crayon|draw|stack|block|fine_motor"),
        ("catch_ball", r"catch_ball|ball"),
        ("jump_prep", r"jump|hop|squat|balance|walk|stair|gross_motor|safe_"),
        ("expressive_word", r"expressive|vocabulary|first_word|single_word|three_words|book_object_naming|object_naming"),
        ("sound", r"sound|vocal|babbl|raspberr|squeal|coo|early_vocal"),
        ("gesture", r"gesture|request|attention_getting|arms_up|social_caregiver"),
        ("receptive_direction", r"receptive|direction|body_part|book_picture"),
        ("action_label", r"action_picture|action_label|action_words"),
        ("function_question", r"function_question"),
        ("sentence", r"sentence|phrase|two_word"),
        ("conversation", r"conversation"),
        ("time_words", r"time_words"),
        ("counting", r"count|number"),
        ("letters", r"letter"),
        ("attention", r"attention"),
        ("matching", r"match|sort|color|shape"),
        ("routine", r"routine|cleanup"),
        ("social_turn", r"peer|turn_taking|sharing|imitation|peekaboo|laughter|social|referencing|emotion|pretend|helper|affection|face"),
    ]
    for bucket, pat in patterns:
        if re.search(pat, fam):
            return bucket
    if category_key == "language_and_communication":
        return "expressive_word"
    if category_key == "social_and_emotional":
        return "social_turn"
    if category_key == "movement_and_physical":
        return "beading"
    if category_key == "cognitive":
        return "attention"
    return "general"


# ---------------------------------------------------------------------------
# Theme rotation for weeks 3-4
# ---------------------------------------------------------------------------

_FAMILY_THEMES: Dict[str, List[str]] = {
    "book_page": ["board book", "picture book", "peek-a-boo book", "interactive book"],
    "fork_spoon": ["snack time", "mealtime", "pretend restaurant", "teddy feeding"],
    "dressing_on": ["morning routine", "dress-up game", "laundry helper", "mirror routine"],
    "dressing_off": ["bath routine", "bedtime routine", "teddy dressing", "laundry pull"],
    "buttoning": ["button board", "dress-up fasteners", "button treasure"],
    "beading": ["bead game", "peg stacker", "art time", "block build"],
    "catch_ball": ["soft ball game", "basket target", "rolling game"],
    "jump_prep": ["frog game", "floor sticker", "squat toy game"],
    "expressive_word": ["toy choice", "snack choice", "family photo names", "book naming"],
    "sound": ["sound mirror", "animal sounds", "song pause", "silly sounds"],
    "gesture": ["choice request", "help request", "routine pause", "pointing game"],
    "receptive_direction": ["give-me game", "cleanup direction", "body part game"],
    "action_label": ["action picture", "family action", "puppet action"],
    "function_question": ["object function", "function basket", "pretend shopping"],
    "sentence": ["photo sentence", "toy scene talk", "phrase expansion"],
    "conversation": ["short chat", "puppet chat", "photo conversation"],
    "time_words": ["routine sort", "now/later choice", "first/then routine"],
    "counting": ["counting blocks", "snack counting", "toy lineup"],
    "letters": ["letter hunt", "book letter search", "letter basket"],
    "attention": ["two-minute finish", "sticker card", "block build finish"],
    "matching": ["same/different match", "color sort", "sock match"],
    "routine": ["cleanup routine", "helper job", "two-step routine"],
    "social_turn": ["my-turn-your-turn", "peekaboo", "copy-me game", "social referencing"],
}


def _variant_theme(fam: str, variant: int, week: int = 1) -> str:
    bucket = _family_bucket(fam)
    themes = _FAMILY_THEMES.get(bucket, ["home play", "daily routine", "family activity"])
    # Week 3+ rotates to later theme slots for novelty (same bridge, different context)
    offset = 2 if week >= 3 else 0
    idx = ((variant - 1) + offset) % max(1, len(themes))
    return themes[idx]


def _get_allowed_themes(fam: str) -> List[str]:
    bucket = _family_bucket(fam)
    return _FAMILY_THEMES.get(bucket, ["home play", "daily routine"])


# ---------------------------------------------------------------------------
# LLM prompt  (V22 — privacy: "your child" always)
# ---------------------------------------------------------------------------

def _v22_activity_prompt(
    state: Dict[str, Any],
    category_key: str,
    bridge: Dict[str, Any],
    variant: int,
    week: int = 1,
) -> str:
    child = state.get("child", {})
    fam = bridge.get("activity_family", "")
    desc = get_family_description(fam) or ""
    safety = format_safety_constraints_for_prompt(ensure_safety_profile(state))
    focus = _clean(bridge.get("bridge_step", "") or bridge.get("activity_focus_step", ""))
    theme = _variant_theme(fam, variant, week)
    allowed_themes = ", ".join(_get_allowed_themes(fam))

    return (
        "You are writing one parent-facing Genex home activity card.\n\n"
        "Hard rules:\n"
        "- Write ONLY for bridge_step_1 and the specified activity_family.\n"
        "- Do NOT use previous_bridge_step. It is hidden future troubleshooting metadata.\n"
        "- Do NOT regress to earlier prerequisites.\n"
        "- Do NOT create a motor game unless the domain is Movement / Physical.\n"
        "- Do NOT create a generic placeholder activity.\n"
        "- Instructions must say exactly what the parent does, what the child does, "
        "what counts as success, and when to stop.\n"
        "- Keep it playful, low-pressure, and doable in 5 minutes.\n"
        "- Use safe household materials only.\n"
        "- Privacy: refer to the child as 'your child' — never use a name.\n"
        "- PARENT-FACING LANGUAGE: Do NOT mention 'bridge step', CDC milestone text, "
        "subdomain names, internal planning terms, or any clinical/system terminology in "
        "the title, instructions, or any output field. Write as if speaking directly to a "
        "parent — warm, concrete, and jargon-free.\n"
        "- Return JSON only with exactly these keys: title, theme, instructions, "
        "success_criteria, make_easier, make_harder, group_play_line, what_to_avoid, materials.\n\n"
        f"Child profile (anonymised):\n"
        f"- age: {child.get('chronological_months', '')} months\n"
        f"- diagnosis/condition: {child.get('diagnosis', '') or 'none'}\n"
        f"- parent concern: {child.get('concern', '') or 'none'}\n\n"
        f"Planning inputs:\n"
        f"- domain: {DOMAIN_CONFIG.get(category_key, {}).get('display', category_key)}\n"
        f"- subdomain: {bridge.get('subdomain', '')}\n"
        f"- CDC milestone: {bridge.get('milestone', '')}\n"
        f"- bridge_step_1: {focus}\n"
        f"- activity_family: {fam}\n"
        f"- activity_family_description: {desc}\n"
        f"- suggested theme for this variant: {theme}\n"
        f"- allowed themes: {allowed_themes}\n"
        f"- variant number: {variant}\n"
        f"- safety notes: {safety}"
    )


# ---------------------------------------------------------------------------
# LLM writer
# ---------------------------------------------------------------------------

def _v22_call_llm_activity_writer(
    state: Dict[str, Any],
    category_key: str,
    bridge: Dict[str, Any],
    variant: int,
    week: int = 1,
) -> Optional[Dict[str, Any]]:
    client = _get_openai_client()
    if not client or not ACTIVITY_MODEL:
        return None
    prompt = _v22_activity_prompt(state, category_key, bridge, variant, week)
    try:
        response = client.chat.completions.create(
            model=ACTIVITY_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            response_format={"type": "json_object"},
            max_tokens=600,
        )
        raw = (response.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("title") and data.get("instructions"):
            return data
    except Exception as exc:
        logger.warning("[activity_engine] LLM call failed: %s", exc)
    return None


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Family-specific activity variant bank  (V22 — deterministic fallback)
# ---------------------------------------------------------------------------
# Each family has 3 genuinely distinct cards.
# _v22_fallback_instructions picks card (variant-1) % len(cards).
# ---------------------------------------------------------------------------

_FAMILY_VARIANTS: Dict[str, List[Dict[str, str]]] = {
    # ── social_and_emotional ──────────────────────────────────────────────
    "helper_role_chores": [
        {
            "title": "Sock Sort Shuffle",
            "theme": "laundry helper",
            "materials": "a basket of clean mixed socks",
            "instructions": (
                "Dump the sock basket on the floor between you and your child. "
                "Pick up one sock and say 'I need the match!' "
                "Wait for your child to pick up a sock and bring it to you. "
                "Help them press the pair together. Aim for 3-4 pairs."
            ),
            "success_criteria": "Your child finds and brings one matching sock (with or without help).",
            "make_easier": "Lay out only 4 socks (2 pairs) and point to the matching one.",
            "make_harder": "Add a third colour and let your child sort without pointing.",
            "group_play_line": "With another child, one holds the basket while the other sorts.",
            "what_to_avoid": "Avoid rushing or correcting non-matches — celebrate every attempt.",
        },
        {
            "title": "Table-Setting Crew",
            "theme": "mealtime helper",
            "materials": "plastic cups, napkins, and spoons — one set per seat at the table",
            "instructions": (
                "Before a meal or snack, give your child one item at a time: a cup, a napkin, a spoon. "
                "Say 'cup goes here' and point to the spot. "
                "Wait for your child to place it, then move to the next seat together. "
                "Do 2-3 place settings."
            ),
            "success_criteria": "Your child places at least one item at the right spot with minimal help.",
            "make_easier": "Do only one seat and hand-over-hand guide the first placement.",
            "make_harder": "Give your child two items at once and let them decide which goes where.",
            "group_play_line": "Two children can take turns: one carries the cup, one carries the spoon.",
            "what_to_avoid": "Avoid correcting placement direction — any attempt at the right spot counts.",
        },
        {
            "title": "Wipe-Down Helpers",
            "theme": "cleaning crew",
            "materials": "two damp cloths or baby wipes, a low table or coffee table",
            "instructions": (
                "Give your child a damp cloth and take one yourself. "
                "Wipe one section of a low table, then say 'your turn — wipe here' and point to a spot. "
                "Do 3-4 wipe-turns together. Celebrate finishing the table."
            ),
            "success_criteria": "Your child makes at least one wiping motion on the table.",
            "make_easier": "Guide your child's hand through the first wipe, then let them try alone.",
            "make_harder": "Add a second surface (chair seat) and let your child choose where to wipe next.",
            "group_play_line": "With another child, each takes one half of the table to wipe.",
            "what_to_avoid": "Avoid taking over when wiping misses a spot — the effort matters, not perfection.",
        },
    ],
    "game_rules_turn_taking": [
        {
            "title": "Rolling Ball Turns",
            "theme": "soft ball game",
            "materials": "soft ball or rolled socks",
            "instructions": (
                "Sit facing each other on the floor, close enough that a slow roll reaches easily. "
                "Say 'my turn' and roll the ball to your child. "
                "Say 'your turn' and wait. Gently redirect the ball if it goes sideways. "
                "Aim for 4-6 back-and-forth rolls."
            ),
            "success_criteria": "Your child rolls or pushes the ball back at least once.",
            "make_easier": "Sit closer and roll the ball directly into your child's hands.",
            "make_harder": "Add a rule: roll gently only — and name 'gentle' each turn.",
            "group_play_line": "With a third person, sit in a triangle and add a waiting turn.",
            "what_to_avoid": "Avoid keeping score or correcting the direction of the roll.",
        },
        {
            "title": "Fishing for Colours",
            "theme": "colour-sorting card game",
            "materials": "8-10 index cards coloured with 2-3 crayons, a small bowl",
            "instructions": (
                "Mix the colour cards face-down in the bowl. "
                "Say 'my turn' and pick one card, name its colour, and put it in a pile. "
                "Say 'your turn' and wait for your child to pick a card. "
                "Help name the colour together. Continue until all cards are sorted."
            ),
            "success_criteria": "Your child picks a card and holds it for at least one second.",
            "make_easier": "Use only 4 cards (2 colours) and hold your child's hand to pick the first one.",
            "make_harder": "Ask 'what colour?' before naming it yourself — wait 3 seconds.",
            "group_play_line": "With another child, each picks for their own colour pile.",
            "what_to_avoid": "Avoid naming the colour before your child has had a chance to.",
        },
        {
            "title": "Tower Build-Off",
            "theme": "block stacking game",
            "materials": "6-10 wooden or foam building blocks",
            "instructions": (
                "Put the blocks between you and your child. "
                "Say 'my turn' and stack one block. "
                "Say 'your turn' and wait. Help your child place the next block if needed. "
                "Keep building until the tower falls — then celebrate and start again."
            ),
            "success_criteria": "Your child places at least one block on the stack.",
            "make_easier": "Use large foam blocks and steady the tower while your child places theirs.",
            "make_harder": "Name a colour for each block as it goes on.",
            "group_play_line": "Two children can each add one block per round.",
            "what_to_avoid": "Avoid straightening blocks between your child's turns — the wobble is part of the fun.",
        },
    ],
    "performance_action_imitation": [
        {
            "title": "Mirror Dance Party",
            "theme": "copycat dancing",
            "materials": "music from a phone or speaker, clear floor space",
            "instructions": (
                "Play a short familiar song. "
                "Start moving — wave your arms, sway, or stomp — and say 'copy me!' "
                "When your child copies a move, switch: 'now you lead!' "
                "Alternate 2-3 times. Keep each turn just a few seconds."
            ),
            "success_criteria": "Your child imitates at least one movement during your turn.",
            "make_easier": "Do one simple slow move (arm raise) and hold the position while your child copies.",
            "make_harder": "Add a two-step sequence: clap then stomp, one after the other.",
            "group_play_line": "With another child, take turns leading the group of two.",
            "what_to_avoid": "Avoid correcting your child's version of the move — their interpretation counts.",
        },
        {
            "title": "Animal Act Show",
            "theme": "pretend animal performance",
            "materials": "no materials needed",
            "instructions": (
                "Say 'let's be animals!' and name one animal — frog, bear, bird, or snake. "
                "Show what that animal does (hop, stomp, flap, or slither). "
                "Wait for your child to try. "
                "After they try, say 'your turn to pick!' and let them name the next animal. "
                "Do 3-4 animals."
            ),
            "success_criteria": "Your child imitates at least one animal action.",
            "make_easier": "Do the action alongside your child and name each body part as you move it.",
            "make_harder": "Do the action without naming the animal and see if your child can guess it.",
            "group_play_line": "With another child, one acts while the other guesses the animal.",
            "what_to_avoid": "Avoid moving on too quickly — hold the pose a few seconds so your child can see it.",
        },
        {
            "title": "Song and Pose Show",
            "theme": "singing with actions",
            "materials": "a familiar action song your child knows (e.g. Head Shoulders Knees)",
            "instructions": (
                "Start the song and do the actions slowly. "
                "Pause on the second verse and wait — see if your child fills in the action or word. "
                "If they don't, do it together. "
                "Sing 2-3 verses, pausing at a different spot each time."
            ),
            "success_criteria": "Your child fills in at least one action or word during a pause.",
            "make_easier": "Don't pause — sing and do the actions side-by-side throughout.",
            "make_harder": "Leave out two actions in a row and see if your child does both from memory.",
            "group_play_line": "With another child, take turns deciding which action to do next.",
            "what_to_avoid": "Avoid correcting the timing — matching the spirit counts.",
        },
    ],
    # ── language_and_communication ────────────────────────────────────────
    "story_narration": [
        {
            "title": "Toy Adventure Reporter",
            "theme": "telling a story about a toy",
            "materials": "one favourite stuffed animal or toy figure",
            "instructions": (
                "Hold up the toy and say 'tell me a story about [toy name]!' "
                "If your child is quiet, start: 'One day [toy] went to…' then pause. "
                "Accept any words, sounds, or pointing as a story turn. "
                "Add one sentence and hand it back. Keep the story to 3-4 turns total."
            ),
            "success_criteria": "Your child contributes at least one word, sound, or gesture to the story.",
            "make_easier": "Ask yes/no questions: 'Did [toy] eat breakfast?'",
            "make_harder": "Ask 'what happened next?' after your child's turn and wait 5 seconds.",
            "group_play_line": "Pass the toy back and forth — each person adds one sentence.",
            "what_to_avoid": "Avoid correcting grammar — keep the story going, not perfect.",
        },
        {
            "title": "Dream Trip Teller",
            "theme": "telling an imaginary journey story",
            "materials": "a simple drawing your child makes (stick figures or scribbles — anything)",
            "instructions": (
                "Ask your child to draw anything — it doesn't need to look like something. "
                "When they're done, say 'tell me what's happening here.' "
                "Point to a part and ask 'who is this?' or 'where are they going?' "
                "Celebrate any response and write down one thing your child says."
            ),
            "success_criteria": "Your child labels or describes any part of their drawing with at least one word.",
            "make_easier": "Draw together and narrate your own drawing as a model before asking.",
            "make_harder": "Ask for a beginning, middle, and end: 'what happens first?', 'then what?', 'and then?'",
            "group_play_line": "Two children each draw and then swap — tell a story about the other's drawing.",
            "what_to_avoid": "Avoid asking 'what is this?' in a testing tone — keep it curious and warm.",
        },
        {
            "title": "Retell the Bedtime Story",
            "theme": "retelling a book just read",
            "materials": "a picture book your child has heard before",
            "instructions": (
                "Open the book to a page with a clear action happening. "
                "Cover the words and point to the picture. "
                "Say 'what's happening here?' and wait. "
                "If your child is quiet, say the first word and let them finish: 'The bear is…' "
                "Do 3-4 pages."
            ),
            "success_criteria": "Your child completes or starts a sentence about one picture.",
            "make_easier": "Give two choices: 'Is the bear sleeping or eating?'",
            "make_harder": "Ask 'why is the bear doing that?' and wait 5 seconds for an answer.",
            "group_play_line": "Two children take turns: one points, the other tells what's happening.",
            "what_to_avoid": "Avoid reading the text aloud on this page — the retelling is the activity.",
        },
    ],
    "conversation_exchange": [
        {
            "title": "Snack Chat",
            "theme": "talking during a snack",
            "materials": "a small snack your child enjoys",
            "instructions": (
                "Sit together at the table with the snack. "
                "Start with an easy open question: 'What do you want to do after this?' "
                "Wait at least 5 seconds for any response — words, pointing, or sounds. "
                "Reply in one sentence and ask one more question. "
                "Keep the conversation to 4-6 back-and-forth turns."
            ),
            "success_criteria": "Your child responds at least once with a word, gesture, or look.",
            "make_easier": "Ask yes/no questions only: 'Do you want more?' and wait for any response.",
            "make_harder": "Introduce a topic your child doesn't know: 'I saw a big dog today — have you seen one?'",
            "group_play_line": "With another child, ask each child one question and let them answer in turn.",
            "what_to_avoid": "Avoid filling silences too quickly — give your child 5 full seconds to respond.",
        },
        {
            "title": "Drawing Talk-Along",
            "theme": "chatting while drawing together",
            "materials": "paper and crayons for each of you",
            "instructions": (
                "Sit side by side with paper and crayons. "
                "Draw something simple and narrate as you go: 'I'm drawing a sun.' "
                "Ask your child 'what are you drawing?' "
                "Comment on what you see: 'I see you used blue — what is that?' "
                "Keep drawing and chatting for 5 minutes."
            ),
            "success_criteria": "Your child responds to at least one comment or question about their drawing.",
            "make_easier": "Just draw alongside your child without asking questions — model narrating.",
            "make_harder": "Ask your child to draw something you describe: 'draw something you love.'",
            "group_play_line": "Two children each draw then swap papers and add one thing to each other's drawing.",
            "what_to_avoid": "Avoid correcting the drawing — all responses to drawing questions are valid.",
        },
        {
            "title": "Walk and Wonder",
            "theme": "conversation on a short walk",
            "materials": "no materials needed",
            "instructions": (
                "Take a short walk around the home, yard, or block. "
                "Point to things and say 'I notice…' or 'Look at that…' "
                "Wait for your child to respond, then add one sentence. "
                "Point to 4-5 things and have a short back-and-forth about each."
            ),
            "success_criteria": "Your child responds to at least two of your comments with words, pointing, or looking.",
            "make_easier": "Point and name only: 'There's a tree' — no question needed.",
            "make_harder": "Ask your child to point to something and you have to guess what it is.",
            "group_play_line": "Two children each choose one thing to point out and explain.",
            "what_to_avoid": "Avoid turning it into a quiz — the goal is conversation, not correct answers.",
        },
    ],
    "rhyming": [
        {
            "title": "Rhyme Tap Game",
            "theme": "clapping rhyme pairs",
            "materials": "no materials needed",
            "instructions": (
                "Say a word and clap once: 'cat.' "
                "Then say 'cat — hat — they rhyme!' and clap twice. "
                "Try another pair: 'dog — log — rhyme!' "
                "After 3-4 modelled pairs, say a word and ask 'what rhymes with…?' "
                "Accept any rhyme — real or made-up."
            ),
            "success_criteria": "Your child produces or confirms at least one rhyming pair.",
            "make_easier": "Give the rhyming word and ask 'do these rhyme? cat — hat?' Wait for any response.",
            "make_harder": "Say a word and wait 5 seconds for your child to produce the rhyme alone.",
            "group_play_line": "Two children take turns: one says the word, the other finds the rhyme.",
            "what_to_avoid": "Avoid correcting silly rhymes — 'cat-splat' is valid and shows the skill.",
        },
        {
            "title": "Silly Rhyme Book Hunt",
            "theme": "finding rhymes in a picture book",
            "materials": "any rhyming picture book",
            "instructions": (
                "Read one page of the rhyming book aloud, slightly slower than usual. "
                "Stop just before the rhyming word: 'The cat sat on the…' "
                "Wait 3 seconds. Accept any attempt — point to the picture if needed. "
                "Finish the line together and move to the next rhyme. Do 4-5 rhymes per sitting."
            ),
            "success_criteria": "Your child fills in or attempts to fill in at least one rhyming word.",
            "make_easier": "Read the full line first, then go back and pause — your child hears the rhyme before the gap.",
            "make_harder": "After reading, close the book and ask 'what word came after sat?' from memory.",
            "group_play_line": "Two children alternate — one fills in the odd rhymes, one the even rhymes.",
            "what_to_avoid": "Avoid moving on before your child has had a full 3-second pause to try.",
        },
        {
            "title": "Name That Rhyme Bag",
            "theme": "pulling rhyming objects from a bag",
            "materials": "cloth bag with 4-6 small household objects (cup, spoon, block, sock…)",
            "instructions": (
                "Put the objects in the bag. "
                "Pull one out and name it: 'spoon!' "
                "Say 'I need something that rhymes with spoon — moon!' "
                "Help your child reach in and pull out the next object. "
                "Name it and find a rhyme together. Do 3-4 objects."
            ),
            "success_criteria": "Your child reaches in and pulls out an object, and attempts any rhyme.",
            "make_easier": "Lay the objects out and say the rhyme — your child just points to the matching one.",
            "make_harder": "Your child pulls out the object and has to find the rhyme without help.",
            "group_play_line": "Two children take turns pulling from the bag and finding the rhyme together.",
            "what_to_avoid": "Avoid correcting rhymes that are invented — made-up rhymes demonstrate the skill.",
        },
    ],
    "story_comprehension": [
        {
            "title": "Who Did What? Quiz",
            "theme": "character question game after reading",
            "materials": "a picture book with at least two characters",
            "instructions": (
                "Read or re-read a page or two of the book. "
                "Close or cover the page and ask one question: 'Who was hungry?' "
                "Wait 5 seconds. If your child is stuck, open the book to the picture. "
                "Ask 1-2 questions per page, 2-3 pages total."
            ),
            "success_criteria": "Your child answers at least one character question with a word, point, or gesture.",
            "make_easier": "Give two options: 'Was it the bear or the bunny who was hungry?'",
            "make_harder": "Ask 'why did [character] do that?' and wait for a reason.",
            "group_play_line": "Two children take turns asking each other one question from the book.",
            "what_to_avoid": "Avoid asking too many questions in a row — one question per page is enough.",
        },
        {
            "title": "Point and Tell",
            "theme": "using pictures to answer questions",
            "materials": "a wordless or picture-heavy picture book",
            "instructions": (
                "Open the book to a busy picture page. "
                "Point to a character or object and ask 'what is this person doing?' "
                "Wait for any response — pointing, a word, or a sound. "
                "Add one sentence: 'Yes, they're eating!' "
                "Move to 3-4 different parts of the picture."
            ),
            "success_criteria": "Your child responds to at least two 'what's happening?' prompts.",
            "make_easier": "Name what's happening yourself and ask 'do you see that?' — accept nods.",
            "make_harder": "Ask 'why is this person doing that?' and wait for a reason.",
            "group_play_line": "Two children each point to something different and take turns describing it.",
            "what_to_avoid": "Avoid saying 'no, that's wrong' — every description attempt is valid.",
        },
        {
            "title": "Story Sequence Shuffle",
            "theme": "putting story events in order",
            "materials": "a familiar picture book and 3 index cards",
            "instructions": (
                "After reading the book, draw or write one event on each card: start, middle, end. "
                "Mix the cards face-up on the table. "
                "Say 'what happened first?' and let your child pick a card. "
                "Then 'what happened next?' and 'how did it end?' Help as much as needed."
            ),
            "success_criteria": "Your child places at least one card in the correct position.",
            "make_easier": "Use only two cards: beginning and end.",
            "make_harder": "Use 4-5 cards for a longer sequence.",
            "group_play_line": "Two children each pick a card and decide together where it goes.",
            "what_to_avoid": "Avoid placing the cards for your child — wait and give hints before helping.",
        },
    ],
}

# ---------------------------------------------------------------------------
# Bucket-level variant bank  (safety net for families not in _FAMILY_VARIANTS)
# ---------------------------------------------------------------------------

_BUCKET_VARIANTS: Dict[str, List[Dict[str, str]]] = {
    "social_turn": [
        {
            "title": "Rolling Ball Turns",
            "theme": "soft ball game",
            "materials": "soft ball or rolled socks",
            "instructions": (
                "Sit facing each other on the floor. "
                "Say 'my turn' and roll the ball to your child. "
                "Say 'your turn' and wait. "
                "Aim for 4-6 back-and-forth rolls. Celebrate each exchange."
            ),
            "success_criteria": "Your child rolls or pushes the ball back at least once.",
            "make_easier": "Sit closer and roll the ball directly into your child's hands.",
            "make_harder": "Add a name rule: say each other's name before rolling.",
            "group_play_line": "With a third person, sit in a triangle and take turns.",
            "what_to_avoid": "Avoid keeping score or correcting the direction of the roll.",
        },
        {
            "title": "Copycat Moves",
            "theme": "imitation game",
            "materials": "no materials needed",
            "instructions": (
                "Sit facing your child. "
                "Do a simple action — wave, pat your head, or clap twice — and say 'your turn!' "
                "Wait for any attempt to copy. Celebrate it. "
                "Let your child choose the next action: 'now you pick one!' Do 4-5 rounds."
            ),
            "success_criteria": "Your child imitates at least one of your actions.",
            "make_easier": "Do the action slowly, hold the pose, and gently guide your child's hands if needed.",
            "make_harder": "Do two actions in a row (clap-stomp) and see if your child can copy the sequence.",
            "group_play_line": "With another child, take turns leading — each person picks one action to copy.",
            "what_to_avoid": "Avoid expecting a perfect copy — any attempt counts.",
        },
        {
            "title": "Helper's Helper",
            "theme": "shared task game",
            "materials": "a small household task (stacking towels, sorting toys into a bin)",
            "instructions": (
                "Choose a small job to do together. "
                "Say 'I do one, you do one.' Pick up an item, put it away, then wait. "
                "Gesture to your child's item and wait for them to place it. "
                "Celebrate each turn. Finish together."
            ),
            "success_criteria": "Your child takes at least one turn putting an item away.",
            "make_easier": "Hold the bin close and place your child's hand on the item to start.",
            "make_harder": "Add a small rule: big items in one pile, small in another.",
            "group_play_line": "Two children take turns: one hands the item, the other puts it away.",
            "what_to_avoid": "Avoid doing the task faster yourself — wait for your child's turn.",
        },
        {
            "title": "Peekaboo with Object",
            "theme": "peekaboo hiding game",
            "materials": "a cloth or small blanket, one favourite toy",
            "instructions": (
                "Hide a toy under a cloth in front of your child. "
                "Say 'where did it go?' and wait 3 seconds. "
                "If your child reaches or looks expectantly, lift the cloth: 'peekaboo!' "
                "Let your child hide it next — hold the cloth while they put the toy under. "
                "Do 4-5 turns."
            ),
            "success_criteria": "Your child reaches for, lifts, or looks toward the hidden toy.",
            "make_easier": "Only partially cover the toy so it is still visible.",
            "make_harder": "Use two cloths and hide the toy under one — ask 'which one?'",
            "group_play_line": "Two children take turns hiding and finding.",
            "what_to_avoid": "Avoid moving too fast — let your child lead the reveal.",
        },
        {
            "title": "Face-Copy Mirror",
            "theme": "face imitation game",
            "materials": "no materials needed",
            "instructions": (
                "Sit face-to-face with your child very close. "
                "Make a big, slow expression: wide eyes, puffed cheeks, or a big smile. "
                "Hold it for 3 seconds and say 'you try!' "
                "Copy whatever your child does — even if it is not the same. "
                "Take turns: one leads, one copies. Do 4-5 expressions."
            ),
            "success_criteria": "Your child attempts to change their face expression in response.",
            "make_easier": "Just smile big and wait — any facial response counts.",
            "make_harder": "Do two expressions in a row and see if your child copies the sequence.",
            "group_play_line": "Two children sit side by side and both copy the adult's expression.",
            "what_to_avoid": "Avoid laughing at attempts — keep it warm and encouraging.",
        },
        {
            "title": "Shared Book Point",
            "theme": "joint book looking",
            "materials": "a picture book with clear, interesting images",
            "instructions": (
                "Open the book between you. "
                "Point to something interesting and say 'look — a dog!' "
                "Wait to see if your child points too or looks where you pointed. "
                "Then let your child point to something — name it right away. "
                "Do 4-5 shared points per page."
            ),
            "success_criteria": "Your child points or looks at what you point to at least twice.",
            "make_easier": "Tap the picture gently so it is easier to follow your point.",
            "make_harder": "Say the name and wait — let your child point to it without your help.",
            "group_play_line": "Two children take turns pointing to one thing each on the same page.",
            "what_to_avoid": "Avoid turning pages too fast — stay on each page long enough to share a point.",
        },
        {
            "title": "Name and Wave Hello",
            "theme": "greeting routine game",
            "materials": "no materials needed",
            "instructions": (
                "Stand or sit across from your child. "
                "Wave and say 'hello [name]!' using your own name as a model. "
                "Wait for any response — a wave, a sound, a smile, or eye contact. "
                "Then encourage your child to wave and say their own name or 'hi.' "
                "Do 3-4 greetings with different pretend visitors (a toy, a sibling)."
            ),
            "success_criteria": "Your child waves or vocalises in response to a greeting.",
            "make_easier": "Gently guide your child's hand in a wave and celebrate the attempt.",
            "make_harder": "Turn away and wait for your child to initiate the greeting unprompted.",
            "group_play_line": "Two children greet each other with a wave and say each other's names.",
            "what_to_avoid": "Avoid correcting the wave form — any gesture toward the greeter counts.",
        },
    ],
    "expressive_word": [
        {
            "title": "Pick Your Toy",
            "theme": "toy choice game",
            "materials": "two favourite toys your child knows",
            "instructions": (
                "Hold one toy in each hand. "
                "Say 'which one?' and wait. "
                "If your child reaches or looks, say the name and give it. "
                "Try again with a different pair. Do 3-4 choices."
            ),
            "success_criteria": "Your child communicates a choice (reach, point, word, or sound).",
            "make_easier": "Hold out just one toy and wait for any reach or vocalization.",
            "make_harder": "Hold toys out of reach and wait for your child to use a word or sign.",
            "group_play_line": "With another child, one holds the toys and the other chooses each round.",
            "what_to_avoid": "Avoid giving the toy before any communication attempt.",
        },
        {
            "title": "Snack Choice Words",
            "theme": "snack naming game",
            "materials": "two small snacks your child likes",
            "instructions": (
                "Put two snacks on the table. "
                "Name each one slowly: 'cracker… banana.' "
                "Ask 'which one?' and wait 5 seconds. "
                "Give the chosen snack and name it as you hand it over. Repeat for 3-4 choices."
            ),
            "success_criteria": "Your child indicates a snack choice through any means (point, word, reach, or eye gaze).",
            "make_easier": "Put only one snack on the table and wait for any reach or sound before handing it over.",
            "make_harder": "Cover the snacks and ask your child to name one from memory before you uncover it.",
            "group_play_line": "Two children each name one snack per round and share the choosing.",
            "what_to_avoid": "Avoid handing the snack before your child has made any communicative attempt.",
        },
        {
            "title": "What's That? Book",
            "theme": "book object naming",
            "materials": "a picture book with clear single objects on each page",
            "instructions": (
                "Open the book to a clear picture. "
                "Point and ask 'what's that?' "
                "Wait 5 seconds. Accept any sound, word, or point. "
                "Say the name clearly once and turn the page. Do 5-6 pages."
            ),
            "success_criteria": "Your child attempts to name or point to at least 2 pictured items.",
            "make_easier": "Name the picture yourself first, then go back and ask again.",
            "make_harder": "Ask 'where is the [item]?' and wait for your child to point on a busy page.",
            "group_play_line": "Two children take turns pointing: one points, the other names.",
            "what_to_avoid": "Avoid moving past a page before waiting the full 5 seconds.",
        },
        {
            "title": "Family Photo Names",
            "theme": "family photo naming",
            "materials": "3-5 photos of familiar family members on your phone or printed",
            "instructions": (
                "Show one photo at a time. "
                "Ask 'who is this?' and wait 5 seconds. "
                "Accept any sound, word, or point. "
                "Say the name clearly: 'That's grandma!' and show the next photo. "
                "Do 4-5 photos per round."
            ),
            "success_criteria": "Your child attempts to name or point to at least two people in the photos.",
            "make_easier": "Name the person first, then show the photo again and wait for any response.",
            "make_harder": "Mix in a photo of an unfamiliar person and see if your child says 'don't know.'",
            "group_play_line": "Two children take turns guessing who is in each photo.",
            "what_to_avoid": "Avoid correcting close attempts — 'dada' for daddy is a great start.",
        },
        {
            "title": "Give Me Two! Game",
            "theme": "two-choice requesting game",
            "materials": "four small familiar objects (spoon, block, ball, cup)",
            "instructions": (
                "Lay out four objects in a row. "
                "Point to two and name them slowly: 'spoon… ball.' "
                "Ask 'can you give me the spoon?' and wait 5 seconds. "
                "If correct, celebrate and put it in a basket. "
                "Try all four objects across 2-3 rounds."
            ),
            "success_criteria": "Your child gives you the named object at least twice.",
            "make_easier": "Use only two objects and give a visual cue (point gently toward the correct one).",
            "make_harder": "Ask for two objects in one request: 'give me the cup AND the ball.'",
            "group_play_line": "Two children take turns: one asks, the other gives the object.",
            "what_to_avoid": "Avoid pointing to the correct object immediately — wait the full 5 seconds first.",
        },
        {
            "title": "What's Missing? Box",
            "theme": "object memory naming",
            "materials": "a shoe box or bag, 4-5 small familiar objects",
            "instructions": (
                "Put 4-5 objects on the table and name each one together. "
                "Ask your child to close or cover their eyes. "
                "Remove one object and hide it in the box. "
                "Say 'open your eyes — what's missing?' "
                "Wait for any attempt — a point, word, or sound."
            ),
            "success_criteria": "Your child notices something is gone and attempts to name or indicate the missing object.",
            "make_easier": "Use only 2 objects so the missing one is obvious.",
            "make_harder": "Use 6 objects and remove two at once.",
            "group_play_line": "Two children take turns hiding an object for the other to find.",
            "what_to_avoid": "Avoid removing the object before your child has had a chance to name all the objects first.",
        },
        {
            "title": "Quick Naming Walk",
            "theme": "home object naming walk",
            "materials": "no materials needed — walk around one room",
            "instructions": (
                "Walk slowly around one room with your child. "
                "Stop at an object, tap it, and ask 'what's this?' "
                "Wait 5 seconds. Accept any sound, point, or word. "
                "Name it and move on. Visit 5-6 objects per walk."
            ),
            "success_criteria": "Your child attempts to name or respond to at least 3 objects on the walk.",
            "make_easier": "Name the object first, then visit it again and ask 'what's this?'",
            "make_harder": "Ask 'what do we do with this?' for each object to practise function words.",
            "group_play_line": "Two children walk together — one points, the other names.",
            "what_to_avoid": "Avoid asking about the same object twice in one walk.",
        },
        {
            "title": "Action Word Match",
            "theme": "action word game",
            "materials": "no materials needed",
            "instructions": (
                "Do a simple action slowly: wave, jump, clap, or eat. "
                "Ask 'what am I doing?' and wait 5 seconds. "
                "Accept any sound or word attempt. "
                "Name the action: 'waving!' "
                "Do 4-5 different actions."
            ),
            "success_criteria": "Your child attempts to label or copy at least one action.",
            "make_easier": "Give a choice: 'Am I waving or sleeping?' and wait for any response.",
            "make_harder": "Describe what your child is doing in the moment: 'You are eating — say eat!'",
            "group_play_line": "Two children take turns doing an action while the other names it.",
            "what_to_avoid": "Avoid performing actions too quickly — slow, big movements are easier to name.",
        },
        {
            "title": "Song Fill-In Game",
            "theme": "song pause fill-in",
            "materials": "a simple song your child knows (e.g. Twinkle Twinkle, Old MacDonald)",
            "instructions": (
                "Start a familiar song together. "
                "Slow down and stop just before the last word of a line. "
                "Look at your child and wait 3 seconds. "
                "Accept any sound, word, or just a look — then finish the word together. "
                "Do 4-5 pauses across the song."
            ),
            "success_criteria": "Your child fills in, attempts, or reacts to at least one pause.",
            "make_easier": "Pause at the very last sound of a word (e.g. 'twin-kle twin-kle little…') and wait.",
            "make_harder": "Pause mid-song without slowing down — see if your child notices and fills in.",
            "group_play_line": "Two children take turns: one starts the line, the other fills in the last word.",
            "what_to_avoid": "Avoid filling in immediately — the 3-second wait is the key.",
        },
        {
            "title": "More Please Routine",
            "theme": "two-word carrier phrase",
            "materials": "a favourite snack, toy, or activity in small amounts",
            "instructions": (
                "Give your child a very small piece of snack or a brief turn with a toy. "
                "When it runs out, wait silently and look expectant. "
                "If your child reaches, points, or makes a sound, say 'more!' and model it. "
                "Wait for any attempt — a sound, approximation, or gesture — then give more. "
                "Do 4-5 turns. Try pairing a gesture: hold up one finger for 'more.'"
            ),
            "success_criteria": "Your child communicates 'more' using any word, sound, sign, or gesture.",
            "make_easier": "Hold the item in your hand and wait for any reach — give it immediately.",
            "make_harder": "Wait for 'more' plus a word: 'more cracker', modelling the two words together.",
            "group_play_line": "Two children take turns asking for more during a shared snack or game.",
            "what_to_avoid": "Avoid giving more before any communication attempt — the wait is what builds the habit.",
        },
        {
            "title": "Cleanup Request Game",
            "theme": "cleanup direction game",
            "materials": "5-8 small toys on the floor, a basket",
            "instructions": (
                "Scatter toys on the floor. "
                "Say '[item] in the basket!' and wait for your child to pick it up and drop it in. "
                "After each success, name the next item. "
                "Do 4-5 cleanup requests. "
                "Keep the instructions simple: one item at a time."
            ),
            "success_criteria": "Your child picks up and places at least 2 named items into the basket.",
            "make_easier": "Point to the item as you name it and hold the basket close.",
            "make_harder": "Name 2 items in one instruction: 'ball and cup in the basket!'",
            "group_play_line": "Two children take turns — one names the item, the other picks it up.",
            "what_to_avoid": "Avoid naming all items at once — one clear instruction at a time.",
        },
        {
            "title": "Two-Word Choice Game",
            "theme": "two-word carrier phrase",
            "materials": "2–3 small familiar objects (cracker, ball, box) on a tray",
            "instructions": (
                "Put 2–3 objects on a tray between you and your child. "
                "Hold one up and say 'want cracker?' then wait. "
                "If your child reaches or sounds, model the two words: 'want cracker!' "
                "Give it over immediately. "
                "Repeat with other objects, modelling 'want ball,' 'open box.' "
                "Do 4–5 turns."
            ),
            "success_criteria": "Your child uses any two-word combination or approximation to request an item.",
            "make_easier": "Accept one word plus a reach — say the two-word phrase yourself and give the item.",
            "make_harder": "Wait for a two-word phrase before handing over the item.",
            "group_play_line": "Two children each request one item — take turns being the one who holds the tray.",
            "what_to_avoid": "Avoid asking 'say want cracker' — model the phrase and wait.",
        },
        {
            "title": "Paint Choice Words",
            "theme": "colour choice carrier phrase",
            "materials": "2–3 small jars of finger paint or paint chips, a piece of paper",
            "instructions": (
                "Put 2–3 colour jars in front of your child. "
                "Model choosing: 'red paint!' and dip a finger. "
                "Hold the jars slightly back and wait. "
                "When your child reaches, model: 'blue paint!' and let them choose. "
                "Do 4–5 colour choices, celebrating each two-word attempt."
            ),
            "success_criteria": "Your child uses a colour word plus 'paint' or 'want' in any order.",
            "make_easier": "Name the colour first and wait for any response before giving access.",
            "make_harder": "Put the paint jars out of reach — your child must request before painting.",
            "group_play_line": "Two children take turns choosing a colour and painting one mark each.",
            "what_to_avoid": "Avoid correcting colour names mid-activity — any two-word attempt is a win.",
        },
    ],
    "attention": [
        {
            "title": "Finish It! Game",
            "theme": "tiny task completion",
            "materials": "4 blocks or 4 puzzle pieces",
            "instructions": (
                "Put 4 blocks or pieces out and say 'let's build it!' "
                "Do the first one together. "
                "Say 'one more!' after each piece to keep momentum. "
                "When the last one is done, celebrate with a high-five."
            ),
            "success_criteria": "Your child completes all 4 pieces with a consistent effort.",
            "make_easier": "Use 2 pieces only and celebrate immediately when done.",
            "make_harder": "Use 6 pieces and add a 10-second wait between the 3rd and 4th.",
            "group_play_line": "Two children take turns adding one piece each until the task is complete.",
            "what_to_avoid": "Avoid adding more pieces mid-task — the clear finish line is the key.",
        },
        {
            "title": "Sticker Finish Game",
            "theme": "sticker card task",
            "materials": "sticker sheet and a card with 5 circles drawn on it",
            "instructions": (
                "Draw 5 circles on a card. "
                "Say 'we need to fill all the circles!' "
                "Put the first sticker in together. "
                "Hand your child a sticker and point to an empty circle. "
                "Finish together — celebrate when the last circle is filled."
            ),
            "success_criteria": "Your child places at least 2 stickers with minimal redirection.",
            "make_easier": "Use 3 circles only and larger stickers.",
            "make_harder": "Let your child peel their own stickers off the sheet.",
            "group_play_line": "Two children each have their own card — race to fill theirs first.",
            "what_to_avoid": "Avoid redirecting after every sticker — let your child set their own pace.",
        },
        {
            "title": "Build and Finish",
            "theme": "block build to a model",
            "materials": "5-6 blocks and a simple 2-block model you've built",
            "instructions": (
                "Build a simple model (2 blocks stacked or in a row). "
                "Say 'can you make one like mine?' "
                "Point to each block in your model in turn. "
                "Celebrate when their build matches (or nearly matches)."
            ),
            "success_criteria": "Your child uses at least 2 blocks in a purposeful arrangement.",
            "make_easier": "Build side-by-side and do each step together.",
            "make_harder": "Build your model, then hide it and ask your child to build from memory.",
            "group_play_line": "Two children each build a model and swap — try to copy each other's.",
            "what_to_avoid": "Avoid correcting your child's version mid-build — wait until they're done.",
        },
        {
            "title": "Timer Cleanup Race",
            "theme": "toy cleanup with a timer",
            "materials": "phone or kitchen timer, basket, 6-8 small toys on the floor",
            "instructions": (
                "Scatter 6-8 toys on the floor. "
                "Show the timer and say 'let's put them all away before the beep!' "
                "Set 1 minute and start picking up together. "
                "Count each toy as it goes in: 'one… two… three!' "
                "Celebrate when the basket is full or the timer goes off."
            ),
            "success_criteria": "Your child puts at least 3 toys in the basket before the timer ends.",
            "make_easier": "Use 3 toys only and do the first one hand-over-hand together.",
            "make_harder": "Let your child set the timer themselves and try to beat their own record.",
            "group_play_line": "Two children each have their own basket and see who fills theirs first.",
            "what_to_avoid": "Avoid extending the task if your child loses momentum — stop at the timer.",
        },
        {
            "title": "Drawing Finish",
            "theme": "drawing to a finish line",
            "materials": "paper and crayons",
            "instructions": (
                "Draw a simple outline together — a house, a sun, or a face — and stop when there are "
                "3–4 parts left to add (windows, rays, eyes). "
                "Say 'let's finish it!' and point to each missing part in turn. "
                "Wait for your child to colour or draw each one before moving to the next. "
                "Celebrate when the last part is done."
            ),
            "success_criteria": "Your child adds at least 2 parts to finish the drawing.",
            "make_easier": "Trace the missing parts lightly in pencil so your child colours inside the lines.",
            "make_harder": "Ask your child to decide what to add next and do it without pointing.",
            "group_play_line": "Two children take turns adding one part each to the same drawing.",
            "what_to_avoid": "Avoid finishing parts yourself — the incompleteness is what drives attention.",
        },
        {
            "title": "First-Then Routine Board",
            "theme": "first/then visual routine",
            "materials": "2 index cards or sticky notes, a simple drawing or photo of each step",
            "instructions": (
                "Before a short task (snack, getting shoes on, putting toys away), "
                "hold up two cards: 'FIRST' and 'THEN.' "
                "Say 'first… shoes — then… snack!' and show each card. "
                "Help your child do the first step, then immediately do the reward. "
                "Keep the routine under 3 minutes total."
            ),
            "success_criteria": "Your child completes the first step and waits for the 'then' moment.",
            "make_easier": "Use only one card ('first') and give the reward right away.",
            "make_harder": "Add a middle card: first — then — and then.",
            "group_play_line": "With another child, each takes a turn doing the 'first' step while the other waits.",
            "what_to_avoid": "Avoid using the first-then board for steps your child truly cannot do yet.",
        },
        {
            "title": "Waiting Turn Game",
            "theme": "taking turns with a prize",
            "materials": "one small toy, snack, or sticker as a prize; a chair for each player",
            "instructions": (
                "Sit across from your child with the prize between you. "
                "Take a turn using the toy (roll the car, place the sticker, eat one cracker). "
                "Say 'my turn… your turn!' and slide it across. "
                "Each turn is very short — 10 seconds maximum. "
                "Do 5–6 turns, then declare 'all done — we both played!'"
            ),
            "success_criteria": "Your child waits for their turn and takes the prize without grabbing on your turn.",
            "make_easier": "Shorten your turn to 3 seconds so the wait is very brief.",
            "make_harder": "Add a 'stop and count to 3' rule before taking a turn.",
            "group_play_line": "With another adult or child, add a third turn in the rotation.",
            "what_to_avoid": "Avoid long turns — waiting is the practice; keep your turn visibly short.",
        },
        {
            "title": "Snack Count and Finish",
            "theme": "snack counting task",
            "materials": "5–8 small snack pieces (crackers, raisins, cereal) on a plate",
            "instructions": (
                "Put 5–8 small snack pieces on a plate. "
                "Point and count them together: 'one, two, three…' "
                "Then say 'let's finish them all!' and take turns eating one at a time. "
                "Count down as each one goes: 'four left… three left…' "
                "Celebrate when the plate is empty."
            ),
            "success_criteria": "Your child stays at the table and eats (or hands over) each piece until the plate is empty.",
            "make_easier": "Use 3 pieces only and count them together before eating each one.",
            "make_harder": "Ask your child to count the remaining pieces before each turn.",
            "group_play_line": "Two children take turns — each eats one piece and counts the ones left.",
            "what_to_avoid": "Avoid adding more snack mid-task — the clear empty plate is the finish line.",
        },
        {
            "title": "Helper Mission Card",
            "theme": "helper mission task",
            "materials": "a small index card with one simple job drawn or written on it",
            "instructions": (
                "Before a routine (snack, getting ready, tidying up), write or draw one simple job on a card. "
                "Hand the card to your child and say 'this is your mission!' "
                "Read it together: 'put 3 books on the shelf.' "
                "Let your child do the task independently, then report back: 'mission done!' "
                "Celebrate the finish."
            ),
            "success_criteria": "Your child completes the one-step mission and reports back.",
            "make_easier": "Stay nearby and narrate each step quietly as your child does it.",
            "make_harder": "Let your child pick the mission from a small set of 3 card choices.",
            "group_play_line": "With a sibling, each gets their own mission card to complete at the same time.",
            "what_to_avoid": "Avoid complicated missions — one clear job per card.",
        },
        {
            "title": "Simple Checklist Game",
            "theme": "checklist task",
            "materials": "paper and a marker, 3 short tasks written as a list",
            "instructions": (
                "Before a routine (getting ready, cleaning up, snack prep), "
                "write 3 simple steps on paper: 1. shoes, 2. bag, 3. jacket. "
                "Point to step 1 and say 'let's check it off!' "
                "When your child does each step, let them make a big tick or cross it off. "
                "Celebrate when all 3 are ticked."
            ),
            "success_criteria": "Your child completes all 3 steps and ticks off each one.",
            "make_easier": "Use only 2 steps and do the first one together.",
            "make_harder": "Let your child draw their own checklist for the next routine.",
            "group_play_line": "Two children each have their own checklist for the same routine.",
            "what_to_avoid": "Avoid more than 3 steps — a short, clear list builds the habit of finishing.",
        },
        {
            "title": "Table Job Practice",
            "theme": "table-setting job",
            "materials": "plastic cups, napkins, and spoons — one set per person at the table",
            "instructions": (
                "Before a meal or snack, tell your child their job: 'you're in charge of spoons today.' "
                "Show them where each spoon goes. "
                "Step back and let them place each spoon — one per seat. "
                "When they're done, say 'job done!' and sit down together."
            ),
            "success_criteria": "Your child places spoons (or cups or napkins) at each seat with minimal prompting.",
            "make_easier": "Do the first seat together hand-over-hand, then let them do the rest alone.",
            "make_harder": "Give your child two jobs: spoons AND napkins.",
            "group_play_line": "Two children share the table job — one does cups, one does spoons.",
            "what_to_avoid": "Avoid re-doing placements your child has made — any attempt at the right spot counts.",
        },
        {
            "title": "Snack Cup Helper",
            "theme": "snack prep first/then",
            "materials": "a small cup, yogurt, granola or cereal, and 2–3 berries",
            "instructions": (
                "Say 'we're making a snack — first yogurt, then granola, then berries!' "
                "Hold up one finger for each step. "
                "Let your child scoop one spoonful of yogurt, then pour a small amount of granola, "
                "then place 2–3 berries on top. "
                "Say each step aloud as they do it. Eat it together when done."
            ),
            "success_criteria": "Your child completes all 3 steps with minimal prompting.",
            "make_easier": "Do the first step yourself, then let your child do steps 2 and 3.",
            "make_harder": "Say 'what comes next?' before each step and wait for your child to tell you.",
            "group_play_line": "Two children each make their own cup — one does yogurt, one does granola.",
            "what_to_avoid": "Avoid rushing — each step is the practice; the eating is the reward.",
        },
        {
            "title": "Laundry Match Job",
            "theme": "laundry matching task",
            "materials": "a pile of 6–8 clean socks in 2–3 different colours",
            "instructions": (
                "Dump the sock pile on the floor between you. "
                "Say 'first find two that match, then put them together!' "
                "Hold up one sock and ask 'find its match!' "
                "When your child finds it, show them how to press the pair together. "
                "Do 3–4 pairs."
            ),
            "success_criteria": "Your child finds and pairs at least 2 matching socks.",
            "make_easier": "Use only 4 socks (2 matching pairs) and lay them all face up.",
            "make_harder": "Mix in a third colour and let your child sort without help.",
            "group_play_line": "Two children each find one sock from a pair — the first to match wins the round.",
            "what_to_avoid": "Avoid correcting near-matches mid-task — let your child finish before helping.",
        },
        {
            "title": "Backpack Helper",
            "theme": "backpack packing task",
            "materials": "a small backpack, a snack item, a water bottle or small toy",
            "instructions": (
                "Before heading out (or as pretend practice), say 'backpack helper — first snack in, then zip up!' "
                "Hand your child the snack. Say 'snack in the bag!' and wait. "
                "When they put it in, say 'now zip it!' and guide their hand to the zip if needed. "
                "Let your child carry the bag. Celebrate: 'bag is ready — let's go!'"
            ),
            "success_criteria": "Your child puts the item in the bag and attempts to zip it.",
            "make_easier": "Open the bag wide and hold it still — your child only needs to drop the item in.",
            "make_harder": "Add a third step: 'first snack, then water bottle, then zip.'",
            "group_play_line": "Two children take turns packing and being the 'bag inspector' who checks it's done.",
            "what_to_avoid": "Avoid zipping for your child — guide their fingers to the zip pull and let them try.",
        },
    ],
    "jump_prep": [
        {
            "title": "Frog Jump Game",
            "theme": "frog jumping",
            "materials": "clear flat floor space",
            "instructions": (
                "Say 'let's be frogs!' "
                "Model bending your knees and jumping forward a small step. "
                "Hold both your child's hands and try together — a knee bend or small hop counts. "
                "Do 4-5 hops across the room."
            ),
            "success_criteria": "Your child bends their knees and attempts any upward or forward movement.",
            "make_easier": "Sit on the floor and do bunny hops forward on your bottoms together.",
            "make_harder": "Jump over a line of tape on the floor.",
            "group_play_line": "Two children hop side-by-side across the room.",
            "what_to_avoid": "Avoid unsupported jumping off heights — stay on flat ground.",
        },
        {
            "title": "Stomp the Sticker",
            "theme": "floor sticker targets",
            "materials": "3-4 round stickers on the floor, caregiver nearby",
            "instructions": (
                "Put 3-4 stickers on the floor in a short path. "
                "Say 'stomp on it!' and model stomping the first sticker. "
                "Hold your child's hand and walk to the next sticker. "
                "Cheer on each stomp."
            ),
            "success_criteria": "Your child stomps on at least 2 stickers with intention.",
            "make_easier": "Hold both hands and stomp together on each sticker.",
            "make_harder": "Space stickers farther apart so your child takes 2 steps between each.",
            "group_play_line": "Two children take turns stomping down a path of stickers.",
            "what_to_avoid": "Avoid placing stickers near furniture edges or slippery surfaces.",
        },
        {
            "title": "Squat and Reach",
            "theme": "squat and pick-up game",
            "materials": "5-6 small toys scattered on the floor, plus a basket",
            "instructions": (
                "Scatter toys on the floor and put a basket nearby. "
                "Model squatting down, picking one up, and putting it in the basket. "
                "Say 'your turn!' and gesture to a toy on the floor. "
                "Stay close in case your child needs balance support."
            ),
            "success_criteria": "Your child squats or bends to pick up at least one toy and drops it in the basket.",
            "make_easier": "Put the basket on the floor so your child doesn't need to stand to drop the toy in.",
            "make_harder": "Put the basket on a slightly raised surface so your child must stand up to reach it.",
            "group_play_line": "Two children each have their own basket and race to fill them.",
            "what_to_avoid": "Avoid rushing — each squat-and-pick takes effort; let your child go at their own pace.",
        },
    ],
    "gesture": [
        {
            "title": "Show Me What You Want",
            "theme": "requesting a favourite thing",
            "materials": "two favourite small objects or a clear container with a treat inside",
            "instructions": (
                "Put a favourite item in view but slightly out of reach — or in a clear container. "
                "Wait silently. "
                "When your child looks, reaches, points, or makes any sound, give it immediately. "
                "Name the item as you hand it over: 'banana!' "
                "Repeat with a different item. Do 3-4 turns."
            ),
            "success_criteria": "Your child uses any gesture, reach, gaze, or sound to request at least one item.",
            "make_easier": "Place the item right in front of your child and just wait — any reaching counts.",
            "make_harder": "Move the item farther away so your child must point or come to you.",
            "group_play_line": "With another child, one holds the item and the other practises requesting.",
            "what_to_avoid": "Avoid giving the item before any communication attempt — the wait is the key step.",
        },
        {
            "title": "Help Me Open It",
            "theme": "requesting help with a container",
            "materials": "a small container (jar, bag, or box) with a favourite snack or toy inside",
            "instructions": (
                "Hand your child a closed container they can't easily open. "
                "Wait. "
                "If they struggle, look at you, or make a sound — say 'you need help!' and open it together. "
                "Do 3-4 turns, letting your child initiate the request each time."
            ),
            "success_criteria": "Your child looks at you, reaches toward you, or vocalises to ask for help.",
            "make_easier": "Slightly loosen the lid first so a small effort gets it open — celebrate the attempt.",
            "make_harder": "Wait until your child makes eye contact before you help.",
            "group_play_line": "Two children take turns: one holds the container, the other asks for help.",
            "what_to_avoid": "Avoid opening the container for your child before they've had a chance to request.",
        },
        {
            "title": "Point and Get",
            "theme": "pointing to get something",
            "materials": "two objects placed across the room or on a high shelf",
            "instructions": (
                "Stand with your child facing two items they like (placed a little out of reach). "
                "Ask 'which one do you want?' "
                "Wait for any pointing, reaching, or looking. "
                "When they indicate one, walk over and get it together. "
                "Do 3-4 choices."
            ),
            "success_criteria": "Your child points, reaches toward, or clearly looks at one item to make a choice.",
            "make_easier": "Hold one item in each hand and wait for any lean, reach, or gaze.",
            "make_harder": "Name three options and wait for your child to point to the right one.",
            "group_play_line": "Two children take turns pointing to what they want from a shared set of toys.",
            "what_to_avoid": "Avoid guessing which item your child wants before they've communicated.",
        },
        {
            "title": "Routine Pause Point",
            "theme": "pause and gesture routine",
            "materials": "a familiar daily routine (getting shoes, wash hands, snack time)",
            "instructions": (
                "During a familiar routine, pause before the next step. "
                "Wait silently and look expectantly at the next item needed. "
                "If your child points, reaches, or looks toward the correct item — do the step immediately. "
                "Do 3-4 pauses across the routine."
            ),
            "success_criteria": "Your child points to, reaches for, or looks at the next item in the routine.",
            "make_easier": "Hold the item close and just wait — any reach toward it counts.",
            "make_harder": "Pause at a less familiar step and see if your child navigates the break.",
            "group_play_line": "With a sibling, one child can model pointing to the next step.",
            "what_to_avoid": "Avoid completing the step before your child has had a chance to gesture.",
        },
        {
            "title": "Wave Bye-Bye Practice",
            "theme": "goodbye wave routine",
            "materials": "no materials needed",
            "instructions": (
                "At the end of a play session, a mealtime, or when leaving a room, "
                "slow down and say 'bye-bye!' with an exaggerated wave. "
                "Wait for any wave, arm movement, or sound. "
                "Copy whatever your child does and celebrate. "
                "Do this consistently at 3-4 natural goodbye moments."
            ),
            "success_criteria": "Your child produces any wave-like arm movement or vocalization at a goodbye moment.",
            "make_easier": "Gently pick up your child's hand and wave it together, then let go and wait.",
            "make_harder": "Step into another room and see if your child follows you to wave.",
            "group_play_line": "Two children wave to each other at the end of play.",
            "what_to_avoid": "Avoid doing the wave for your child every time — leave space for their attempt.",
        },
        {
            "title": "High-Five Choice",
            "theme": "high-five request game",
            "materials": "two small preferred objects or photos",
            "instructions": (
                "Hold up one object in each hand at your child's eye level. "
                "Say 'which one?' and open both hands flat like a high-five. "
                "Wait for your child to tap or point to one hand. "
                "Give whatever is in that hand immediately. "
                "Do 3-4 choices."
            ),
            "success_criteria": "Your child taps or touches one of your hands to communicate a choice.",
            "make_easier": "Hold just one hand flat and wait for any tap or touch.",
            "make_harder": "Slowly move the hands apart so your child must reach to make their choice.",
            "group_play_line": "With another adult, each holds one option — your child chooses between two people.",
            "what_to_avoid": "Avoid giving the item before your child has touched your hand.",
        },
    ],
    "sound": [
        {
            "title": "Animal Sound Game",
            "theme": "animal sounds",
            "materials": "no materials needed",
            "instructions": (
                "Say an animal name and make its sound slowly: 'cow — moo!' "
                "Wait and look expectantly at your child. "
                "Accept any attempt — a sound, a word, or just a smile. "
                "Try 4-5 animals. Celebrate each attempt."
            ),
            "success_criteria": "Your child attempts any sound or word for at least one animal.",
            "make_easier": "Just make the sound and pause — wait for any reaction at all.",
            "make_harder": "Make the sound without naming the animal and see if your child names it.",
            "group_play_line": "Two children take turns: one names the animal, the other makes the sound.",
            "what_to_avoid": "Avoid repeating the same sound more than twice before moving to the next animal.",
        },
        {
            "title": "Sing and Pause",
            "theme": "song pause game",
            "materials": "a simple song your child knows",
            "instructions": (
                "Start singing a familiar song together. "
                "Pause just before a well-known word or sound. "
                "Wait 3 seconds for your child to fill it in — any sound or approximation is perfect. "
                "Finish the word together and keep going. Pause 4-5 times."
            ),
            "success_criteria": "Your child fills in or attempts at least one word or sound during a pause.",
            "make_easier": "Sing the word yourself first, then go back and pause at the same spot again.",
            "make_harder": "Pause at a less predictable part of the song and wait a full 5 seconds.",
            "group_play_line": "Two children take turns filling in the pauses — one pause each.",
            "what_to_avoid": "Avoid moving on before giving your child the full 3-second pause to try.",
        },
        {
            "title": "Silly Sounds Mirror",
            "theme": "sound imitation game",
            "materials": "no materials needed",
            "instructions": (
                "Sit facing your child. "
                "Make a simple mouth sound — a pop, a raspberry, a click, or a hum. "
                "Say 'you try!' and wait. Accept any sound. "
                "Let your child make a sound for you to copy back. Do 4-5 rounds each way."
            ),
            "success_criteria": "Your child attempts to copy or produce any sound during the game.",
            "make_easier": "Make a very simple sound (just a hum or a long 'aah') and wait.",
            "make_harder": "Do two sounds in a row and see if your child can copy the sequence.",
            "group_play_line": "With another child, one leads the sounds while the other copies.",
            "what_to_avoid": "Avoid expecting an exact copy — any sound attempt counts.",
        },
        {
            "title": "Clap and Sound Game",
            "theme": "clapping rhythm with sounds",
            "materials": "no materials needed",
            "instructions": (
                "Clap a slow rhythm (clap… clap… clap) and add a sound: 'bah… bah… bah!' "
                "Do 3-4 claps in rhythm and stop. "
                "Wait for your child to continue the sound or clap. "
                "Accept any vocalization or clap. "
                "Try a different sound next round: 'moo… moo… moo!'"
            ),
            "success_criteria": "Your child produces any sound or clap during or after the rhythm.",
            "make_easier": "Just clap once and wait for any reaction — sound or clap both count.",
            "make_harder": "Change the rhythm mid-game and see if your child adjusts.",
            "group_play_line": "Two children sit side by side — one claps, the other makes the sound.",
            "what_to_avoid": "Avoid doing more than 4 claps before pausing — the gap is where the learning happens.",
        },
        {
            "title": "Noisy Toy Turn",
            "theme": "cause-and-effect sound toy",
            "materials": "one noisy toy (squeaky toy, drum, shaker, or xylophone)",
            "instructions": (
                "Put the noisy toy in front of your child. "
                "Model making one sound with it: squeeze, tap, or shake. "
                "Say 'your turn!' and wait. "
                "After your child makes a sound, copy exactly what they did. "
                "Alternate: you model, they copy. Do 4-5 turns."
            ),
            "success_criteria": "Your child makes the toy produce a sound intentionally at least twice.",
            "make_easier": "Guide your child's hand to the toy and help them make the first sound.",
            "make_harder": "Make a specific sound (two taps) and see if your child copies the pattern.",
            "group_play_line": "Two children take turns — one makes a sound, the other copies it.",
            "what_to_avoid": "Avoid taking long turns — keep each model very short so your child gets many turns.",
        },
        {
            "title": "First Sound Try",
            "theme": "first-sound practice game",
            "materials": "3-4 familiar objects (ball, cup, spoon, book)",
            "instructions": (
                "Hold up an object and say just the first sound very slowly: 'buh…' for ball. "
                "Wait 3 seconds for your child to add a sound or finish the word. "
                "Then say the full word: 'ball!' "
                "Try 4-5 objects. Celebrate any sound attempt."
            ),
            "success_criteria": "Your child produces any sound within 5 seconds of hearing the first sound cue.",
            "make_easier": "Say the full word slowly and then just wait for any response.",
            "make_harder": "Ask for the first sound without holding up the object — description only.",
            "group_play_line": "Two children take turns: one gives the first sound, the other finishes the word.",
            "what_to_avoid": "Avoid moving to the next object before the full 3-second wait.",
        },
    ],
    # ── Fine motor / OT (beading bucket) ─────────────────────────────────────
    "beading": [
        {
            "title": "Big Bead Threading",
            "activity_family": "beading_threading",
            "theme": "large bead threading",
            "materials": "6–8 large wooden beads and a thick shoelace or cord with a stiff tip",
            "instructions": (
                "Put 6–8 large beads and the lace on a low table. "
                "Thread one bead yourself and say 'your turn!' "
                "Hand a bead to your child and hold the lace steady while they push it on. "
                "Celebrate each bead. Aim for 4–5 beads threaded together."
            ),
            "success_criteria": "Your child pushes at least one bead onto the lace with minimal help.",
            "make_easier": "Use a stiffer rod or pipe cleaner instead of a lace — easier to aim.",
            "make_harder": "Let your child hold the lace themselves and thread without support.",
            "group_play_line": "Two children take turns threading one bead each onto the same lace.",
            "what_to_avoid": "Avoid small beads — use beads at least 3 cm wide for safety.",
        },
        {
            "title": "Sticker Peel and Place",
            "activity_family": "pincer_grasp",
            "theme": "sticker peeling task",
            "materials": "a sheet of round or star stickers and a piece of paper with 5 circles drawn on it",
            "instructions": (
                "Draw 5 circles on the paper. "
                "Peel the first sticker yourself and press it into a circle. "
                "Hand the sheet to your child and say 'peel one!' "
                "Help only with the first corner peel if needed. "
                "Let your child press each sticker into a circle. Celebrate each one."
            ),
            "success_criteria": "Your child peels and places at least 2 stickers.",
            "make_easier": "Pre-peel the sticker halfway — your child just lifts and places.",
            "make_harder": "Let your child peel the sticker fully and choose which circle to put it in.",
            "group_play_line": "Two children each have their own paper — see who fills their circles first.",
            "what_to_avoid": "Avoid helping unless the sticker is truly stuck — the peeling is the work.",
        },
        {
            "title": "Peg-and-Ring Stack",
            "activity_family": "prewriting_scribble",
            "theme": "peg and ring stacking",
            "materials": "a peg board or stacking toy with 4–5 rings, or stacked cups",
            "instructions": (
                "Place the base on a stable surface. "
                "Stack the first ring yourself and say 'you try!' "
                "Hand each ring to your child one at a time. "
                "Guide their hand down to the peg if needed. "
                "Celebrate when the last ring goes on."
            ),
            "success_criteria": "Your child places at least 2 rings on the peg.",
            "make_easier": "Hold the base in your hand at your child's chest height — easier aim.",
            "make_harder": "Mix up the ring order and let your child figure out the correct sequence.",
            "group_play_line": "Two children take turns — one places a ring, the other counts them.",
            "what_to_avoid": "Avoid guiding hand-over-hand past the first ring — wait after each one.",
        },
        {
            "title": "Big Crayon Marks",
            "activity_family": "prewriting_scribble",
            "theme": "big crayon mark-making",
            "materials": "large chunky crayons and white paper",
            "instructions": (
                "Tape the paper to a low table so it stays still. "
                "Make a big slow line yourself: 'look — a long line!' "
                "Hand your child a chunky crayon and say 'make a big mark!' "
                "Don't ask for a specific shape — any mark counts. "
                "Do 4–5 turns each, narrating what you see: 'round and round!'."
            ),
            "success_criteria": "Your child makes at least one mark on the paper.",
            "make_easier": "Help your child grip the crayon with your hand over theirs for the first mark.",
            "make_harder": "Draw a dotted line and ask your child to trace over it.",
            "group_play_line": "Two children draw on the same large sheet — one side each.",
            "what_to_avoid": "Avoid asking your child to draw a specific thing — free marks build grip and confidence.",
        },
        {
            "title": "Spoon Scoop Practice",
            "activity_family": "fork_spoon_use",
            "theme": "spoon scooping practice",
            "materials": "a spoon, a small bowl of cereal or soft food, and a second empty bowl",
            "instructions": (
                "Put a small bowl of cereal in front of your child and an empty bowl next to it. "
                "Say 'scoop and move!' "
                "Model scooping one spoonful from the full bowl to the empty one. "
                "Hand the spoon to your child and let them try. "
                "Aim for 4–5 scoops. Keep the portions small so spills are easy to manage."
            ),
            "success_criteria": "Your child scoops and moves at least one spoonful.",
            "make_easier": "Use a wide shallow spoon and large pieces — easier to catch and balance.",
            "make_harder": "Add a rule: carry it without spilling. Count any that make it across.",
            "group_play_line": "Two children take turns scooping — one scoops, the other counts the spoonfuls.",
            "what_to_avoid": "Avoid reacting strongly to spills — keep the tone light and keep going.",
        },
        {
            "title": "Clothespin Squeeze Helper",
            "activity_family": "pincer_grasp",
            "theme": "clothespin squeezing task",
            "materials": "4–6 wooden or plastic clothespins and a low basket or container edge",
            "instructions": (
                "Show your child how to squeeze a clothespin open and clip it to the basket edge. "
                "Do one yourself: squeeze — clip! "
                "Hand one to your child and say 'squeeze it!' "
                "Help only by guiding fingers into the right position on the first try. "
                "Clip 4–5 clothespins total, then unclip them together."
            ),
            "success_criteria": "Your child squeezes a clothespin open at least once.",
            "make_easier": "Use a wider-grip clothespin or spring-free clip — easier to open.",
            "make_harder": "Let your child unclip the clothespins too — squeeze open, pull off.",
            "group_play_line": "Two children take turns — one clips, the other unclips in a loop.",
            "what_to_avoid": "Avoid tiny spring clothespins — use large easy-grip ones only.",
        },
    ],
    # ── Receptive language / direction ────────────────────────────────────────
    "receptive_direction": [
        {
            "title": "Give Me Game",
            "activity_family": "receptive_directions_one_step",
            "theme": "one-step give-me direction",
            "materials": "4–5 small familiar objects on a tray (cup, spoon, block, toy car, sock)",
            "instructions": (
                "Lay 4–5 objects on a tray between you and your child. "
                "Say 'give me the cup' and hold out your hand. "
                "Wait 5 seconds. If your child doesn't respond, point gently to the cup and repeat. "
                "Once they hand it over, say 'thank you!' and put it back. "
                "Do 4–5 different objects."
            ),
            "success_criteria": "Your child picks up and hands over the correct object at least twice.",
            "make_easier": "Use only 2 objects and point to the correct one while saying its name.",
            "make_harder": "Name the object without pointing — wait the full 5 seconds before helping.",
            "group_play_line": "Two children take turns being the 'giver' — one asks, one fetches.",
            "what_to_avoid": "Avoid pointing before your child has had a moment to look.",
        },
        {
            "title": "Put It In the Box",
            "activity_family": "receptive_directions_one_step",
            "theme": "one-step put-it-in direction",
            "materials": "a small box or basket, 4–5 small toys",
            "instructions": (
                "Put the box and 4–5 toys on the floor between you. "
                "Say 'put the ball in the box' and wait. "
                "If your child picks up the right toy, nod and wait for them to put it in. "
                "Celebrate each success with a clap. "
                "Do 4–5 different objects."
            ),
            "success_criteria": "Your child puts the correct object in the box at least twice.",
            "make_easier": "Use one object only and point to it while saying 'put it in.'",
            "make_harder": "Say 'put the red block in the box' — adding a colour makes it a two-part direction.",
            "group_play_line": "Two children take turns: one says the instruction, the other follows it.",
            "what_to_avoid": "Avoid helping before the 5-second wait — give your child time to process.",
        },
        {
            "title": "Body Part Touch Game",
            "activity_family": "body_part_identification",
            "theme": "body part identification",
            "materials": "no materials needed",
            "instructions": (
                "Sit facing your child. "
                "Say 'touch your nose!' and touch your own nose as a model. "
                "Wait for your child to touch theirs. "
                "Try 4–5 body parts: nose, ears, head, belly, toes. "
                "Use a silly voice or song to keep it fun."
            ),
            "success_criteria": "Your child touches the correct body part at least 3 times.",
            "make_easier": "Name the part AND touch yours — let your child copy.",
            "make_harder": "Give the direction without touching yourself — just the words.",
            "group_play_line": "Two children follow together — call a body part and both touch at once.",
            "what_to_avoid": "Avoid doing it too fast — give your child 3 full seconds to respond.",
        },
        {
            "title": "Cleanup Direction Game",
            "activity_family": "receptive_directions_one_step",
            "theme": "one-item cleanup direction",
            "materials": "3–4 small toys or objects on the floor, a basket or bin",
            "instructions": (
                "Scatter 3–4 objects on the floor. "
                "Say 'put the car in the basket' and point to the basket. "
                "Wait for your child to pick up the car and put it in. "
                "Celebrate, then say the next direction. "
                "Do 3–4 one-item cleanup directions."
            ),
            "success_criteria": "Your child follows at least 2 one-step cleanup directions.",
            "make_easier": "Hold the basket close to the object — less distance to carry it.",
            "make_harder": "Say the direction, then turn away — let your child follow without your eye cue.",
            "group_play_line": "Two children each get their own basket — you call one direction and both try.",
            "what_to_avoid": "Avoid saying 'clean up everything' — one item per direction is the goal.",
        },
    ],
    # ── Ball / catch / gross motor ────────────────────────────────────────────
    "catch_ball": [
        {
            "title": "Soft Ball Fun",
            "activity_family": "catch_ball",
            "theme": "soft ball game",
            "materials": "soft ball or rolled-up socks",
            "instructions": (
                "Sit on the floor facing your child, close enough that a slow roll reaches easily. "
                "Say 'my turn' and roll the ball toward them. "
                "Wait — they can roll it back, push it, or just touch it. "
                "Say 'your turn!' and wait. Aim for 4–6 back-and-forth rolls."
            ),
            "success_criteria": "Your child rolls or pushes the ball back at least once.",
            "make_easier": "Sit closer and roll directly into their hands.",
            "make_harder": "Add a name rule: say each other's name before rolling.",
            "group_play_line": "With a third person, sit in a triangle and take turns rolling.",
            "what_to_avoid": "Avoid hard balls, long distances, or pressure to catch.",
        },
        {
            "title": "Roll the Ball Game",
            "activity_family": "catch_ball",
            "theme": "rolling game",
            "materials": "soft ball or rolled-up socks, clear floor space",
            "instructions": (
                "Sit across from your child with your legs apart forming a 'goal.' "
                "Roll the ball back and forth, keeping rolls slow and aimed right at them. "
                "Count each successful exchange: 'one… two… three!' "
                "See how many you can get in a row."
            ),
            "success_criteria": "Your child rolls or redirects the ball back at least twice in a row.",
            "make_easier": "Roll against a wall so the ball comes back without your child needing to aim.",
            "make_harder": "Move slightly farther apart and keep counting the streak.",
            "group_play_line": "A sibling sits in a triangle — each person rolls to a different person.",
            "what_to_avoid": "Avoid bouncing or throwing — rolling keeps control easy for everyone.",
        },
        {
            "title": "Basket Target Toss",
            "activity_family": "catch_ball",
            "theme": "basket target toss",
            "materials": "soft ball or beanbag, laundry basket or large bowl",
            "instructions": (
                "Place a laundry basket or large bowl on the floor about 1–2 steps away. "
                "Model tossing the ball underhand into the basket. "
                "Hand the ball to your child and say 'your turn — in the basket!' "
                "Move the basket closer if needed. Do 5–6 turns."
            ),
            "success_criteria": "Your child releases the ball in the direction of the basket at least once.",
            "make_easier": "Hold the basket directly in front of your child — just a drop-in.",
            "make_harder": "Move the basket one step farther and let your child aim on their own.",
            "group_play_line": "Two children take turns tossing — each gets their own basket.",
            "what_to_avoid": "Avoid hard balls or keeping score — the toss is the practice.",
        },
    ],
    # ── Time words / first-then ───────────────────────────────────────────────
    "time_words": [
        {
            "title": "First Crackers, Then Berries",
            "activity_family": "time_words_routine",
            "theme": "first/then snack routine",
            "materials": "a small plate, 2 crackers, 2–3 berries",
            "instructions": (
                "Make a tiny snack plate together. "
                "Say 'first crackers, then berries' and hold up one finger for each step. "
                "Let your child put 2 crackers on the plate, then 2–3 berries. "
                "Point to each step as you say it. When both steps are done, say 'finished!'"
            ),
            "success_criteria": "Your child places both the crackers and the berries in the right order.",
            "make_easier": "Do the first step yourself and let your child add only the berries.",
            "make_harder": "Ask 'what comes next?' before each step and wait for your child to tell you.",
            "group_play_line": "Two children share the job — one does crackers, one does berries.",
            "what_to_avoid": "Avoid giving both foods at once — the sequence is the whole point.",
        },
        {
            "title": "Table-Setting Steps",
            "activity_family": "time_words_routine",
            "theme": "table-setting first/then sequence",
            "materials": "plastic plate, spoon, and napkin — one set per place",
            "instructions": (
                "Before a meal or snack, say 'first plate, then spoon, then napkin!' "
                "Hold up a finger for each step. "
                "Hand your child the plate and say 'first — plate!' "
                "Once placed, hand the spoon: 'then — spoon!' Then the napkin. "
                "Celebrate: 'all done — table is ready!'"
            ),
            "success_criteria": "Your child places all three items in order with minimal prompting.",
            "make_easier": "Do only two steps: first plate, then spoon.",
            "make_harder": "Say the whole sequence once at the start, then let your child do it from memory.",
            "group_play_line": "Two children share the job — one does plates and napkins, one does spoons.",
            "what_to_avoid": "Avoid glass or sharp items — plastic only for this activity.",
        },
        {
            "title": "Backpack Ready Steps",
            "activity_family": "time_words_routine",
            "theme": "backpack packing first/then",
            "materials": "small backpack, a snack item, a water bottle or small toy",
            "instructions": (
                "Say 'backpack time — first snack, then water bottle, then zip!' "
                "Hold up a finger for each step. "
                "Hand your child the snack: 'first — snack in!' "
                "Then the bottle: 'then — bottle in!' "
                "Then guide them to the zip: 'now zip it!' "
                "Celebrate: 'bag is ready — let's go!'"
            ),
            "success_criteria": "Your child puts both items in the bag and attempts to zip it.",
            "make_easier": "Open the bag wide and have your child drop in just one item.",
            "make_harder": "Say the steps once, then let your child do all three without reminders.",
            "group_play_line": "A sibling does their own bag at the same time — who finishes first?",
            "what_to_avoid": "Avoid doing the zip for your child — guide their hand to the zip pull.",
        },
    ],
    # ── Counting ──────────────────────────────────────────────────────────────
    "counting": [
        {
            "title": "Count the Snacks",
            "activity_family": "counting_one_to_one",
            "theme": "snack counting",
            "materials": "3 crackers or berries on a plate and a small cup",
            "instructions": (
                "Put 3 crackers or berries on a plate. "
                "Touch each one and count slowly together: 'one… two… three.' "
                "Let your child move each piece into the cup as you count. "
                "When the plate is empty, say 'three! all done!'"
            ),
            "success_criteria": "Your child moves each piece into the cup while you count to three.",
            "make_easier": "Count just 2 pieces and hold the cup close.",
            "make_harder": "Let your child touch each piece and say the number themselves.",
            "group_play_line": "Two children take turns — one touches, the other counts.",
            "what_to_avoid": "Avoid counting faster than your child can move — one piece per number.",
        },
        {
            "title": "Block Tower Count",
            "activity_family": "counting_one_to_one",
            "theme": "block stacking and counting",
            "materials": "3 blocks or stacking cups",
            "instructions": (
                "Put 3 blocks on the floor. "
                "Pick up the first one, say 'one' and stack it. "
                "Hand your child the next block: 'two!' "
                "Then the last: 'three!' "
                "When the tower is done, count them together pointing to each."
            ),
            "success_criteria": "Your child stacks at least 2 blocks while counting along.",
            "make_easier": "Stack the first block yourself and let your child add just one more.",
            "make_harder": "Put out 5 blocks and count up to 5 together.",
            "group_play_line": "Two children take turns — one stacks while the other counts.",
            "what_to_avoid": "Avoid correcting the count mid-tower — finish building, then recount together.",
        },
        {
            "title": "Count and Drop",
            "activity_family": "counting_one_to_one",
            "theme": "counting objects into a container",
            "materials": "5 small objects (blocks, coins, or buttons) and a jar or cup",
            "instructions": (
                "Hold up each object one at a time. "
                "Say its number and drop it into the cup: 'one — plop! two — plop!' "
                "Let your child drop each object in after you count it. "
                "When all five are in, count them together by tapping the outside of the cup."
            ),
            "success_criteria": "Your child drops at least 3 objects into the cup while counting.",
            "make_easier": "Use only 3 objects and count together for each one.",
            "make_harder": "Count to 5 and then ask 'how many are in the cup?' Wait for the answer.",
            "group_play_line": "Two children take turns dropping — one drops, the other counts.",
            "what_to_avoid": "Avoid using small objects that could be a choking hazard for younger children.",
        },
    ],
    # ── Matching / sorting ────────────────────────────────────────────────────
    "matching": [
        {
            "title": "Sock Match Game",
            "activity_family": "matching_sorting",
            "theme": "sock matching",
            "materials": "4–6 clean socks in 2–3 colours or patterns",
            "instructions": (
                "Dump 4–6 socks on the floor. "
                "Pick up one and say 'I need the match — can you find it?' "
                "Wait for your child to pick up a sock. "
                "Help them press the pair together: 'match!' "
                "Aim for 2–3 pairs."
            ),
            "success_criteria": "Your child finds and brings over at least one matching sock.",
            "make_easier": "Lay out only 4 socks (2 obvious pairs) and point to the one that matches.",
            "make_harder": "Add a third pattern and let your child sort all three colours into their own pile.",
            "group_play_line": "Two children each hold one sock — call 'match!' and see if they're the same.",
            "what_to_avoid": "Avoid rushing — let your child look at both socks before deciding.",
        },
        {
            "title": "Color Sort Game",
            "activity_family": "color_shape_sorting",
            "theme": "colour sorting",
            "materials": "6–8 small objects in 2 colours (red and blue blocks, or coloured cups)",
            "instructions": (
                "Put two bowls or spots on the floor — one for each colour. "
                "Say 'red ones here, blue ones here!' and sort the first one yourself. "
                "Hand each object to your child and wait. "
                "If needed, point to the correct bowl. Do all 6–8 objects."
            ),
            "success_criteria": "Your child puts at least 3 objects in the correct colour bowl.",
            "make_easier": "Use just 4 objects (2 each) and keep the bowls very close together.",
            "make_harder": "Mix in a third colour and see if your child can make a third group.",
            "group_play_line": "Two children each sort their own half — see who finishes first.",
            "what_to_avoid": "Avoid correcting mid-sort — let your child finish each attempt.",
        },
        {
            "title": "Same or Different?",
            "activity_family": "matching_sorting",
            "theme": "same/different match",
            "materials": "pairs of matching household objects (2 spoons, 2 cups, 2 blocks of same colour)",
            "instructions": (
                "Put 4 objects on the floor — 2 matching pairs mixed together. "
                "Hold up one and say 'find the same!' "
                "Wait for your child to pick one up. "
                "Say 'same!' or 'different!' depending on the match. "
                "Do 4–5 rounds."
            ),
            "success_criteria": "Your child correctly matches at least 2 pairs.",
            "make_easier": "Make the pairs very obvious (same colour, same shape) and do the first one together.",
            "make_harder": "Add a pair that is nearly the same (same colour, different size) to make it trickier.",
            "group_play_line": "Two children each hold one item — call 'same!' if they match, 'different!' if not.",
            "what_to_avoid": "Avoid correcting wrong picks before your child has finished looking — wait for the full attempt.",
        },
    ],
}


# Deterministic fallback text  (V22)
# ---------------------------------------------------------------------------

def _v22_fallback_instructions(
    bucket: str,
    focus: str,
    fam: str,
    variant: int,
    week: int = 1,
) -> Dict[str, str]:
    # 1. Family-specific variants — 3 genuinely distinct cards per activity_family
    fam_cards = _FAMILY_VARIANTS.get(fam)
    if fam_cards:
        return dict(fam_cards[(variant - 1) % len(fam_cards)])

    # 2. Bucket-level variants — fallback for families not in _FAMILY_VARIANTS
    bucket_cards = _BUCKET_VARIANTS.get(bucket)
    if bucket_cards:
        return dict(bucket_cards[(variant - 1) % len(bucket_cards)])

    # 3. Generic fallback — safety net for all other buckets
    theme = _variant_theme(fam, variant, week)
    base = {
        "title": _bucket_title(bucket, theme),
        "theme": theme,
        "materials": f"items for {theme} (from around the home)",
        "instructions": (
            f"Set up a quick {theme} activity. "
            f"Show your child one small step and wait for them to try. "
            f"Celebrate any attempt and stop after 2–3 turns. "
            f"Goal: {focus}."
        ),
        "success_criteria": f"Your child tries at least once: {focus}.",
        "make_easier": "Break it into one single step, stay close, and accept any attempt.",
        "make_harder": "Add one more step or reduce your help by one level.",
        "group_play_line": (
            "With a sibling or friend, take turns — each person tries one step while the other watches."
        ),
        "what_to_avoid": (
            "Avoid pressure, repeated correction, or continuing after frustration or fatigue."
        ),
    }
    overrides = {
        "book_page": {
            "title": "Turn the Page Together",
            "materials": "board book or thick-page book",
            "instructions": (
                "Sit together with a thick-page book. Hold it steady, lift one page edge, "
                "and invite your child to push or pull one page over. "
                "One assisted page turn counts as success."
            ),
            "what_to_avoid": "Avoid thin paper pages, rushing, or turning pages for your child.",
        },
        "fork_spoon": {
            "title": _bucket_title("fork_spoon", theme),
            "materials": "child fork or spoon, soft safe food pieces or pretend food, plate",
            "instructions": (
                "Sit at a table with soft food or pretend food. "
                "Model one slow fork or spoon movement, then help your child stab or scoop one piece. "
                "Keep it supervised and brief."
            ),
            "what_to_avoid": "Avoid choking-risk foods, pressure to eat, or rushing.",
        },
        "dressing_on": {
            "title": _bucket_title("dressing_on", theme),
            "materials": "loose jacket, shirt, or pants",
            "instructions": (
                "Hold one loose sleeve or waistband open. Say a simple cue like 'arm in' "
                "and help your child push or pull one small clothing step. "
                "Any partial movement counts."
            ),
            "what_to_avoid": "Avoid tight clothing, multiple steps at once, or rushing.",
        },
        "dressing_off": {
            "title": _bucket_title("dressing_off", theme),
            "materials": "loose elastic-waist pants or jacket",
            "instructions": (
                "Start the removal movement for your child, then invite one small pull, "
                "push, or arm-out movement. Sitting is fine if balance is difficult."
            ),
            "what_to_avoid": "Avoid tight clothing or multiple items at once.",
        },
        "buttoning": {
            "title": "Button Challenge Game",
            "materials": "large button board or shirt with big buttons",
            "instructions": (
                "Show one large button or closure. Help your child pull apart, push through, "
                "or line up one closure step. One partial movement counts."
            ),
            "what_to_avoid": "Avoid small buttons, frustration, or requiring full completion.",
        },
        "catch_ball": {
            "title": _bucket_title("catch_ball", theme),
            "materials": "soft ball or rolled socks",
            "instructions": (
                "Sit close. Roll or gently pass a soft ball toward your child. "
                "Encourage looking at it and bringing hands toward it. Catching is not required."
            ),
            "what_to_avoid": "Avoid hard balls, long distances, or pressure to catch.",
        },
        "jump_prep": {
            "title": _bucket_title("jump_prep", theme),
            "materials": "clear flat floor, caregiver hand support",
            "instructions": (
                "On a clear flat floor, hold your child's hands or stay close. "
                "Model bending knees and standing tall. "
                "A knee bend, weight shift, or stand-up counts — do not require a jump."
            ),
            "what_to_avoid": "Avoid high surfaces, unsupported jumping, or speed.",
        },
        "expressive_word": {
            "title": _bucket_title("expressive_word", theme),
            "materials": "two favorite objects or pictures",
            "instructions": (
                f"Hold up two items related to {theme}. Pause and wait for your child "
                "to look, reach, point, or make a sound. Name it once and give it right away."
            ),
            "what_to_avoid": "Avoid asking 'say the word' repeatedly or withholding the item.",
        },
        "gesture": {
            "title": "Show Me What You Want",
            "materials": "two favorite objects or a clear container",
            "instructions": (
                "Put a favorite item in reach or a clear container. "
                "Pause and wait for your child to look, reach, point, gesture, or vocalize. "
                "Give the item right away and name it once."
            ),
            "what_to_avoid": "Avoid requiring a verbal word; accept any communication.",
        },
        "receptive_direction": {
            "title": _bucket_title("receptive_direction", theme),
            "materials": "one familiar toy and a basket or simple target",
            "instructions": (
                "Give one clear familiar direction like 'give me the [item]' or 'put it in.' "
                "Add a gesture only if needed. Celebrate any attempt."
            ),
            "what_to_avoid": "Avoid two-step directions or repeating the direction more than once.",
        },
        "social_turn": {
            "title": _bucket_title("social_turn", theme),
            "materials": "one favorite toy or no materials needed",
            "instructions": (
                "Use one toy or peekaboo. Say 'my turn,' take a brief turn, then 'your turn.' "
                "Keep turns very short and predictable. 2-3 back-and-forth exchanges."
            ),
            "what_to_avoid": "Avoid long turns, competition, or keeping score.",
        },
        "attention": {
            "title": _bucket_title("attention", theme),
            "materials": "3-5 simple pieces or a sticker card",
            "instructions": (
                "Choose a tiny task with a clear finish: 3 blocks or 3 stickers. "
                "Help your child stay with it until done, then stop."
            ),
            "what_to_avoid": "Avoid open-ended tasks or continuing past the clear finish.",
        },
    }
    if bucket in overrides:
        base.update(overrides[bucket])
    return base


# ---------------------------------------------------------------------------
# Parent-facing "why this helps" text — no bridge/clinical language
# ---------------------------------------------------------------------------

_DOMAIN_WHY: Dict[str, str] = {
    "movement_and_physical": (
        "Physical play builds strength, coordination, and body confidence — "
        "skills that support everything from dressing independently to playing with friends."
    ),
    "language_and_communication": (
        "Practising communication in small, playful moments builds the connection between "
        "hearing, understanding, and expressing — the foundation of language."
    ),
    "social_and_emotional": (
        "Small social moments teach your child how to connect, trust, and feel safe — "
        "building emotional skills one shared turn at a time."
    ),
    "cognitive": (
        "Play that involves thinking and exploring helps your child build attention, "
        "curiosity, and the ability to learn new things."
    ),
}

# Subdomain-specific "why this helps" text — overrides the domain-level text
# when the bridge's subdomain matches.  Used for common high-specificity
# concerns (e.g. ADHD / attention, emotional regulation) where the generic
# domain text does not land meaningfully for the parent.
_SUBDOMAIN_WHY: Dict[str, str] = {
    "attention_and_processing": (
        "Practising one small task to completion — with a clear start and a clear finish — "
        "helps your child build the ability to stay on task, follow through, and feel proud of finishing. "
        "Short, predictable routines with obvious end points are especially powerful for children "
        "who find it hard to focus or who struggle to complete activities."
    ),
    "emotional_regulation": (
        "Short, low-pressure turn-taking and waiting games help your child practise the pause "
        "between wanting something and getting it — a core skill for managing frustration and "
        "staying calm in everyday moments."
    ),
    "concepts_and_following_directions": (
        "Finishing short tasks — one clear start, one clear finish — builds the habit of "
        "staying focused, completing a routine, and starting the next step with confidence. "
        "These attention and task-completion skills underpin learning at home and at school."
    ),
    "expressive_language": (
        "Every time your child makes a choice, names something, or uses a word instead of "
        "pointing, they are practising expressive language — the building block of communication."
    ),
    "gestural_communication": (
        "Gestures, pointing, and reaching to communicate are the first language. "
        "Responding immediately when your child points or reaches teaches them that "
        "communication works — and encourages them to use it more."
    ),
    "gross_motor_mobility_and_coordination": (
        "Supported movement practice — even slow, small steps — builds the muscle patterns "
        "and body confidence your child needs for independent mobility."
    ),
    "social_engagement_and_joint_attention": (
        "Sharing attention with you on a toy, book, or activity is the foundation of "
        "communication and learning. Every moment of looking-together counts."
    ),
}


def _bucket_title(bucket: str, theme: str) -> str:
    """Return a playful, concrete activity title for a bucket+theme combination."""
    t = theme.lower().strip()
    if bucket == "jump_prep":
        if "frog" in t:        return "Frog Jump Game"
        if "sticker" in t:     return "Stomp the Sticker"
        if "squat" in t:       return "Squat & Reach"
        return "Move & Balance Game"
    if bucket == "expressive_word":
        if "snack" in t:       return "Snack Choice Words"
        if "photo" in t or "name" in t: return "Name the Faces"
        if "book" in t:        return "What's That?"
        if "toy" in t:         return "Pick Your Toy"
        return "Point and Name Game"
    if bucket == "receptive_direction":
        if "body" in t:        return "Touch Your Nose!"
        if "give" in t:        return "Give Me! Game"
        if "cleanup" in t:     return "Cleanup Helper"
        return "Do This! Game"
    if bucket == "social_turn":
        if "peekaboo" in t:    return "Peekaboo Together"
        if "copy" in t:        return "Copy Me!"
        return "My Turn, Your Turn!"
    if bucket == "attention":
        if "sticker" in t:     return "Sticker Finish Game"
        if "block" in t:       return "Build & Finish"
        return "Finish It! Game"
    if bucket == "gesture":
        return "Show Me What You Want"
    if bucket == "book_page":
        return "Turn the Page Together"
    if bucket == "fork_spoon":
        if "snack" in t or "meal" in t: return "Snack Time Practice"
        if "teddy" in t:       return "Feed the Teddy"
        return "Spoon & Fork Game"
    if bucket == "dressing_on":
        if "morning" in t:     return "Morning Clothes Game"
        if "dress" in t:       return "Dress-Up Star"
        return "Arms In! Game"
    if bucket == "dressing_off":
        if "bedtime" in t:     return "Bedtime Undress Game"
        if "teddy" in t:       return "Undress the Teddy"
        return "Pull It Off! Game"
    if bucket == "buttoning":
        return "Button Challenge Game"
    if bucket == "catch_ball":
        if "rolling" in t:     return "Roll the Ball Game"
        if "basket" in t:      return "Basket Target Toss"
        return "Soft Ball Fun"
    if bucket == "sound":
        if "animal" in t:      return "Animal Sound Game"
        if "song" in t:        return "Sing & Pause"
        return "Silly Sounds Together"
    if bucket == "sentence":
        if "photo" in t:       return "Photo Story Game"
        return "Let's Talk About It"
    if bucket == "counting":
        if "snack" in t:       return "Count the Snacks"
        if "block" in t:       return "Count the Blocks"
        return "Count Along Game"
    if bucket == "matching":
        if "sock" in t:        return "Sock Match Game"
        if "color" in t:       return "Color Sort Game"
        return "Same or Different?"
    if bucket == "routine":
        if "cleanup" in t:     return "Cleanup Helper"
        return "Helper's Job"
    if bucket == "action_label":
        if "family" in t:  return "Family Action Naming"
        if "puppet" in t:  return "Puppet Action Words"
        return "What Are They Doing?"
    if bucket == "function_question":
        return "What's It For? Game"
    if bucket == "conversation":
        return "Let's Chat Game"
    if bucket == "time_words":
        return "First and Then Game"
    if bucket == "letters":
        return "Letter Hunt Game"
    # Avoid doubling suffixes: if theme already ends in game/activity/time/practice, don't append "Game"
    import re as _re_t
    _title = theme.title()
    if _re_t.search(r'\b(game|activity|time|practice|session)\s*$', theme, _re_t.I):
        return _title
    return f"{_title} Game"


# ---------------------------------------------------------------------------
# Make one activity  (V22)
# ---------------------------------------------------------------------------

def _v22_make_activity(
    category_key: str,
    bridge: Dict[str, Any],
    activity_type: str,
    variant: int,
    state: Dict[str, Any],
    week: int = 1,
) -> Dict[str, Any]:
    fam = str(bridge.get("activity_family", "") or "")
    bucket = _family_bucket(fam, category_key)
    focus = _clean(bridge.get("bridge_step", "") or bridge.get("activity_focus_step", ""))
    cdc_goal = _clean(bridge.get("milestone", "") or bridge.get("cdc_milestone", ""))
    category_display = DOMAIN_CONFIG.get(category_key, {}).get("display", category_key)

    fallback = _v22_fallback_instructions(bucket, focus, fam, variant, week)
    llm_data = _v22_call_llm_activity_writer(state, category_key, bridge, variant, week)

    data = dict(fallback)
    if llm_data:
        for k, v in llm_data.items():
            if isinstance(v, str) and v.strip():
                data[k] = _clean(v)

    title = _clean(data.get("title", fallback["title"]))
    theme = _clean(data.get("theme", fallback["theme"]))
    instructions = _clean(data.get("instructions", fallback["instructions"]))
    success = _clean(data.get("success_criteria", fallback["success_criteria"]))
    easier = _clean(data.get("make_easier", fallback["make_easier"]))
    harder = _clean(data.get("make_harder", fallback["make_harder"]))
    group_play = _clean(data.get("group_play_line", fallback["group_play_line"]))
    avoid = _clean(data.get("what_to_avoid", fallback["what_to_avoid"]))
    materials = _clean(data.get("materials", fallback["materials"]))

    if activity_type == "easier_backup":
        title = f"Easier: {title}" if not title.startswith("Easier") else title
        instructions += " Simplify: use one item, model first, or accept the smallest response."
    elif activity_type == "harder_stretch":
        title = f"Stretch: {title}" if not title.startswith("Stretch") else title
        instructions += " Only try this if the main version is easy and enjoyable."

    # Parent-facing explanation — no bridge/CDC/clinical language.
    # Use subdomain-specific text when available (e.g. ADHD/attention, emotional regulation).
    # Fall back to domain-level text, then a safe generic string.
    subdomain = bridge.get("subdomain", "")
    why = (
        _SUBDOMAIN_WHY.get(subdomain)
        or _DOMAIN_WHY.get(
            category_key,
            "This activity supports your child's development through playful everyday practice.",
        )
    )

    return {
        # Parent-facing
        "title": title,
        "theme": theme,
        "domain": category_display,
        "duration_minutes": V22_PER_ACTIVITY_MIN,
        "why": why,
        "instructions": instructions,
        "success": success,
        "easier": easier,
        "harder": harder,
        "group_play": group_play,
        "avoid": avoid,
        "materials": materials,
        "feedback_options": ACTIVITY_FEEDBACK_OPTIONS,
        # Debug fields (hidden from parents by default)
        "_debug": {
            "activity_id": f"v22_{category_key}_{_norm(cdc_goal)[:24]}_b1_{activity_type}_{variant}",
            "subdomain": bridge.get("subdomain", "unspecified"),
            "milestone": cdc_goal,
            "bridge_step_1": focus,
            "activity_family": fam,
            "activity_type": activity_type,
            "planning_mode": bridge.get("planning_mode", "standard"),
            "bridge_step_number": bridge.get("bridge_step_number", 1),
            "previous_bridge_step": bridge.get("previous_bridge_step", ""),
            "previous_bridge_status": "not_used_initial_plan__feedback_fallback_only",
            "engine_version": ENGINE_VERSION,
            "llm_used": bool(llm_data),
            "variant": variant,
            "week": week,
        },
        # For validator
        "activity_family": fam,
        "category_key": category_key,
        "validation_warnings": [],
    }


# ---------------------------------------------------------------------------
# Uniquify titles
# ---------------------------------------------------------------------------

def _v22_title_key(title: str) -> str:
    return re.sub(r"\W+", "_", title.lower()).strip("_")


def _v22_uniquify_titles(activities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # No-op: _FAMILY_VARIANTS and _BUCKET_VARIANTS already produce distinct titles.
    # Appending "(variation N)" is not acceptable in parent-facing content.
    return activities


# ---------------------------------------------------------------------------
# generate_category_activity_bank  (V22)
# ---------------------------------------------------------------------------

_ADHD_OLDER_PREFERRED = re.compile(
    r"\b(helper|mission|checklist|first.*then|table.?set|snack prep|snack.*cup|"
    r"backpack|cleanup|clean.up|build|copy.*challenge|wait.*turn|turn.*wait|"
    r"block.*tower|count.*snack|snack.*count|sort|match|puzzle|story|draw|"
    r"name.*game|question|answer|idea)\b",
    re.I,
)

_ADHD_TODDLER_FEELING = re.compile(
    r"\b(peekaboo|peek.a.boo|name and wave|rolling ball|roll.*ball|"
    r"family photo names|paint choice|snack choice|copycat)\b",
    re.I,
)


def _apply_age_preference(
    activities: List[Dict[str, Any]],
    state: Dict[str, Any],
    category_key: str,
) -> List[Dict[str, Any]]:
    """For ADHD children 48+ months: float age-respectful cards to the front,
    soft-deprioritize toddler-feeling cards to the back.  Nothing is removed."""
    chrono = int((state.get("child") or {}).get("chronological_months", 0) or 0)
    diagnosis = str((state.get("child") or {}).get("diagnosis", "") or "").lower()
    if chrono < 48 or "adhd" not in diagnosis:
        return activities

    preferred, neutral, toddler = [], [], []
    for act in activities:
        title = act.get("title", "")
        instructions = act.get("instructions", "")
        combined = f"{title} {instructions}"
        if _ADHD_TODDLER_FEELING.search(combined):
            toddler.append(act)
        elif _ADHD_OLDER_PREFERRED.search(combined):
            preferred.append(act)
        else:
            neutral.append(act)

    return preferred + neutral + toddler


def generate_category_activity_bank(
    state: Dict[str, Any],
    category_key: str,
) -> Dict[str, Any]:
    """Generate the full activity bank for one category."""
    category_display = DOMAIN_CONFIG.get(category_key, {}).get(
        "display", category_key.replace("_", " ").title()
    )
    next_steps = select_next_milestones(
        state, category_key,
        max_milestones=V22_MAX_MILESTONES_PER_DOMAIN,
        min_milestones=V22_MIN_MILESTONES_PER_DOMAIN,
    )
    targets = next_steps.get("milestones", [])

    if not targets:
        bank = _empty_bank(category_key, category_display, next_steps)
        state.setdefault("activity_banks", {})[category_key] = bank
        return bank

    bridge_plan = build_bridge_plan_for_category(state, category_key, targets)
    active_bridges = bridge_plan.get("active_bridge_steps", [])
    planning_mode = bridge_plan.get("planning_mode", "standard")

    daily_time = int(state.get("child", {}).get("daily_time_min", 10) or 10)
    daily_slots = min(max(1, daily_time // V22_PER_ACTIVITY_MIN), V22_MAX_DAILY_ACTIVITIES)
    desired_week1_slots = V22_WEEK1_DAYS * daily_slots
    core_variants = max(2, math.ceil(desired_week1_slots / max(1, len(active_bridges))))
    core_variants = min(max(core_variants, 2), 7)

    raw_activities: List[Dict[str, Any]] = []
    # Global counter so successive bridges that share the same bucket/family
    # draw DIFFERENT cards from the pool instead of all starting at card[0].
    # Example: if two cognitive bridges both map to the "attention" bucket,
    # bridge 0 gets cards [0,1,2] and bridge 1 gets cards [3,4,5].
    _global_core_variant = 0

    for bridge in active_bridges:
        # Cap core_variants to the number of genuinely distinct cards available for
        # this bridge's activity_family/bucket.  Without this cap, variant N % 3 wraps
        # back to card 0 of a 3-card bucket, producing content-duplicate activities at
        # different list indices — which the index-based scheduler dedup cannot catch.
        fam_b = bridge.get("activity_family", "")
        bucket_b = _family_bucket(fam_b, category_key)
        fam_cards_b = _FAMILY_VARIANTS.get(fam_b, [])
        bucket_cards_b = _BUCKET_VARIANTS.get(bucket_b, [])
        if fam_cards_b:
            max_distinct_b = len(fam_cards_b)
        elif bucket_cards_b:
            max_distinct_b = len(bucket_cards_b)
        else:
            # Generic fallback: cap at the number of theme slots to avoid title cycling.
            # If there are no theme slots at all, produce only 1 card per bridge — otherwise
            # every variant would generate the same title (generic fallback returns one fixed
            # title for buckets without theme variants).
            bucket_themes = _FAMILY_THEMES.get(bucket_b, [])
            max_distinct_b = len(bucket_themes) if bucket_themes else 1
        effective_variants = max(1, min(core_variants, max_distinct_b))

        for _local_v in range(effective_variants):
            _global_core_variant += 1
            raw_activities.append(
                _v22_make_activity(category_key, bridge, "core", _global_core_variant, state, week=1)
            )
        # easier/harder use per-bridge local variants (Week 2 only, filtered out in Week 1)
        raw_activities.append(
            _v22_make_activity(category_key, bridge, "easier_backup", 1, state, week=1)
        )
        raw_activities.append(
            _v22_make_activity(category_key, bridge, "harder_stretch", 2, state, week=1)
        )

    raw_activities = _v22_uniquify_titles(raw_activities)
    valid_activities, blocked_activities = filter_valid_activities(raw_activities, category_key)

    # Apply safety constraints AFTER validation so the pass runs on every activity
    # regardless of whether it was LLM-generated or deterministic fallback.
    # This is the enforcement layer — activities containing jump/hop/climb/race are
    # replaced with safe alternatives for high-fall, mobility, and seizure profiles.
    valid_activities = apply_safety_constraints_to_activities(state, category_key, valid_activities)

    # Deduplicate by title AND by normalized title root.
    # "Squat and Reach" and "Supported Squat-and-Reach Game" share the same root
    # and should not both appear in the same bank.
    import re as _re

    def _norm_title_root(t: str) -> str:
        """Strip common adjective prefixes and game/activity suffixes, return core words."""
        t = t.lower()
        t = _re.sub(r"[^a-z0-9\s]", " ", t)
        # Remove common filler prefixes
        t = _re.sub(
            r"\b(supported|slow|quick|simple|easy|gentle|basic|little|tiny|short|fun|new|"
            r"my|your|our|a|the|an)\b",
            "",
            t,
        )
        # Remove common filler suffixes
        t = _re.sub(
            r"\b(game|activity|practice|challenge|time|session|version|exercise)\b",
            "",
            t,
        )
        return _re.sub(r"\s+", " ", t).strip()

    _seen_titles: set = set()
    _seen_roots: set = set()
    _deduped: List[Dict[str, Any]] = []
    for _a in valid_activities:
        _t = _a.get("title", "").strip().lower()
        _root = _norm_title_root(_t)
        if _t not in _seen_titles and _root not in _seen_roots:
            _seen_titles.add(_t)
            _seen_roots.add(_root)
            _deduped.append(_a)
    valid_activities = _deduped

    # Light age-respectful preference for ADHD 48–60m children:
    # prefer helper/checklist/routine/build cards, soft-deprioritize toddler-feeling ones.
    valid_activities = _apply_age_preference(valid_activities, state, category_key)

    warnings = list({w for a in raw_activities for w in a.get("validation_warnings", [])})

    bank = {
        "status": "ok" if valid_activities else "no_valid_activities",
        "version": ENGINE_VERSION,
        "category_key": category_key,
        "category": category_display,
        "planning_mode": planning_mode,
        "summary": next_steps.get("message", ""),
        "target_milestones": targets,
        "active_bridges": len(active_bridges),
        "activities": valid_activities,
        "blocked_activities": blocked_activities,
        "validation_warnings": warnings,
        "daily_slots": daily_slots,
        "core_variants_per_bridge": core_variants,
    }
    state.setdefault("activity_banks", {})[category_key] = bank
    return bank


def _empty_bank(
    category_key: str,
    category_display: str,
    next_steps: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "status": "no_targets",
        "version": ENGINE_VERSION,
        "category_key": category_key,
        "category": category_display,
        "planning_mode": next_steps.get("mode", "no_targets"),
        "summary": next_steps.get("message", "No target milestones found."),
        "target_milestones": [],
        "active_bridges": 0,
        "activities": [],
        "blocked_activities": [],
        "validation_warnings": [next_steps.get("message", "no_targets")],
        "daily_slots": 1,
        "core_variants_per_bridge": 0,
    }


def get_core_pool(bank: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return only 'core' type activities from a bank (used by scheduler)."""
    return [
        a for a in bank.get("activities", [])
        if a.get("_debug", {}).get("activity_type", "core") == "core"
    ]
