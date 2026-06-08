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
from genex_core.safety import ensure_safety_profile, format_safety_constraints_for_prompt
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
        "materials": "simple household items",
        "instructions": (
            f"Choose a short activity using {theme}. "
            f"Model the small step once: {focus}. "
            f"Invite your child to try one turn with as much help as needed. "
            f"Celebrate any attempt and stop after 2-3 turns."
        ),
        "success_criteria": f"Any calm attempt at: {focus}.",
        "make_easier": "Use one item, model first, shorten the turn, or accept a smaller response.",
        "make_harder": "Only if easy and enjoyable: add one small step or reduce support slightly.",
        "group_play_line": (
            "With another child: one person models, one supports, your child takes one short turn."
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
        return "What Are They Doing?"
    if bucket == "function_question":
        return "What's It For? Game"
    if bucket == "conversation":
        return "Let's Chat Game"
    if bucket == "time_words":
        return "First and Then Game"
    if bucket == "letters":
        return "Letter Hunt Game"
    return f"{theme.title()} Game"


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
    # Full domain context is shown in _build_why_helps() in app.py if why is blank.
    why = _DOMAIN_WHY.get(
        category_key,
        "This activity supports your child's development through playful everyday practice.",
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

        for variant in range(1, effective_variants + 1):
            raw_activities.append(
                _v22_make_activity(category_key, bridge, "core", variant, state, week=1)
            )
        raw_activities.append(
            _v22_make_activity(category_key, bridge, "easier_backup", 1, state, week=1)
        )
        raw_activities.append(
            _v22_make_activity(category_key, bridge, "harder_stretch", 2, state, week=1)
        )

    raw_activities = _v22_uniquify_titles(raw_activities)
    valid_activities, blocked_activities = filter_valid_activities(raw_activities, category_key)

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
