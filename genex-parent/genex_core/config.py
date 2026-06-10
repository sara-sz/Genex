"""
genex_core/config.py
--------------------
All domain configs, answer scores, subdomain keyword maps, safety keyword maps,
V22 follow-up schemas, performance barrier scoring, and model configuration.

Updated for Genex Brain V22.
"""

import os

# ------------------------------------------------------------------
# Engine version
# ------------------------------------------------------------------
ENGINE_VERSION = "v22"
APP_VERSION = "parent-copilot-v0.4-v22-staging"

# ------------------------------------------------------------------
# Model configuration  (read from environment — no hardcoded defaults)
# If env var is not set, downstream code uses deterministic fallback
# and logs a warning.
# ------------------------------------------------------------------
ACTIVITY_MODEL: str = os.environ.get("ACTIVITY_MODEL", "").strip()
CONCERN_ROUTER_MODEL: str = os.environ.get("CONCERN_ROUTER_MODEL", "").strip()

# ------------------------------------------------------------------
# V22 schedule constants
# ------------------------------------------------------------------
V22_CYCLE_DAYS = 14           # total plan window (Week 1 + Week 2)
V22_WEEK1_DAYS = 7            # unique activity days
V22_PER_ACTIVITY_MIN = 5      # minutes per activity slot
V22_MAX_DAILY_ACTIVITIES = 3  # hard cap regardless of daily time
V22_MAX_MILESTONES_PER_DOMAIN = 5
V22_MIN_MILESTONES_PER_DOMAIN = 1

# ------------------------------------------------------------------
# Domain config
# ------------------------------------------------------------------
DOMAIN_CONFIG = {
    "movement_and_physical": {
        "display": "Movement / Physical",
        "short": "motor",
    },
    "social_and_emotional": {
        "display": "Social / Emotional",
        "short": "social_emotional",
    },
    "language_and_communication": {
        "display": "Language / Communication",
        "short": "language_communication",
    },
    "cognitive": {
        "display": "Cognitive / Adaptive",
        "short": "cognitive",
    },
}

ALIAS_TO_CATEGORY = {
    "movement and physical": "movement_and_physical",
    "movement/physical": "movement_and_physical",
    "physical": "movement_and_physical",
    "motor": "movement_and_physical",
    "gross motor": "movement_and_physical",
    "social and emotional": "social_and_emotional",
    "social and emotial": "social_and_emotional",
    "social/emotional": "social_and_emotional",
    "social_emotional": "social_and_emotional",
    "social": "social_and_emotional",
    "language and communication": "language_and_communication",
    "language/communication": "language_and_communication",
    "language": "language_and_communication",
    "speech": "language_and_communication",
    "speech and language": "language_and_communication",
    "cognitive": "cognitive",
    "cognitive / adaptive": "cognitive",
    "cognitive/adaptive": "cognitive",
    "adaptive": "cognitive",
}

# ------------------------------------------------------------------
# Answer scoring
# ------------------------------------------------------------------
ANSWER_SCORES = {
    "yes": 1.0,
    "sometimes": 0.4,
    "with_help": 0.2,
    "no": 0.0,
    "not_sure": 0.1,
}

VALID_ANSWERS = set(ANSWER_SCORES.keys())
VALID_PARENT_ANSWERS = {"yes", "sometimes", "with_help", "no", "not_sure"}

# ------------------------------------------------------------------
# V22 follow-up schemas
# Shown to parent after "sometimes" or "with_help" answers to
# capture performance-barrier context without changing the score.
# ------------------------------------------------------------------
FOLLOWUP_SCHEMAS = {
    "sometimes": {
        "prompt": "Why only sometimes? Choose the best fit:",
        "choices": [
            ("not_consistent_yet", "can do it, but not consistently yet"),
            ("distracted", "can do it, but gets distracted"),
            ("upset_or_refuses", "can do it, but gets upset or refuses"),
            ("only_some_situations", "can do it, but only in some situations"),
            ("not_sure", "not sure"),
        ],
    },
    "with_help": {
        "prompt": "What kind of help is usually needed?",
        "choices": [
            ("physical_help", "physical help / hands-on support"),
            ("reminders_prompting", "reminders or step-by-step prompting"),
            ("emotional_support", "encouragement or emotional support"),
            ("showing_first", "showing first / demonstrating"),
            ("not_sure", "not sure"),
        ],
    },
    "no": {
        "prompt": "Can you tell us a little more about the 'no'? Choose the best fit:",
        "choices": [
            ("not_able_yet", "not able yet"),
            ("does_not_do_even_when_we_try", "does not do it even when we try"),
            ("upset_or_refuses", "gets upset or refuses"),
            ("distracted_before_doing", "gets distracted before doing it"),
            ("not_sure_why", "not sure why"),
        ],
    },
}

FOLLOWUP_LABEL_TO_KEY = {
    answer_norm: {label.lower(): key for key, label in schema["choices"]}
    for answer_norm, schema in FOLLOWUP_SCHEMAS.items()
}

# ------------------------------------------------------------------
# V22 performance barrier scoring
# Maps (answer_norm, followup_key) → skill_ability + scoring adjustment
# ------------------------------------------------------------------
PERFORMANCE_BARRIER_SCORING = {
    ("yes", ""): {
        "skill_ability": "yes",
        "performance_barrier": "none",
        "scoring_norm_answer": "yes",
    },
    ("not_sure", ""): {
        "skill_ability": "unclear",
        "performance_barrier": "unclear",
        "scoring_norm_answer": "not_sure",
    },
    ("sometimes", "not_consistent_yet"): {
        "skill_ability": "emerging",
        "performance_barrier": "skill_emerging",
        "scoring_norm_answer": "sometimes",
    },
    ("sometimes", "distracted"): {
        "skill_ability": "yes",
        "performance_barrier": "distractibility",
        "scoring_norm_answer": "yes",
    },
    ("sometimes", "upset_or_refuses"): {
        "skill_ability": "yes",
        "performance_barrier": "emotional_dysregulation_refusal",
        "scoring_norm_answer": "yes",
    },
    ("sometimes", "only_some_situations"): {
        "skill_ability": "emerging",
        "performance_barrier": "situational_inconsistency",
        "scoring_norm_answer": "sometimes",
    },
    ("sometimes", "not_sure"): {
        "skill_ability": "unclear",
        "performance_barrier": "unclear",
        "scoring_norm_answer": "sometimes",
    },
    ("with_help", "physical_help"): {
        "skill_ability": "emerging",
        "performance_barrier": "physical_help",
        "scoring_norm_answer": "with_help",
    },
    ("with_help", "reminders_prompting"): {
        "skill_ability": "yes",
        "performance_barrier": "needs_prompting",
        "scoring_norm_answer": "yes",
    },
    ("with_help", "emotional_support"): {
        "skill_ability": "yes",
        "performance_barrier": "emotional_support",
        "scoring_norm_answer": "yes",
    },
    ("with_help", "showing_first"): {
        "skill_ability": "yes",
        "performance_barrier": "needs_demonstration",
        "scoring_norm_answer": "yes",
    },
    ("with_help", "not_sure"): {
        "skill_ability": "unclear",
        "performance_barrier": "unclear",
        "scoring_norm_answer": "with_help",
    },
    ("no", "not_able_yet"): {
        "skill_ability": "no",
        "performance_barrier": "not_able_yet",
        "scoring_norm_answer": "no",
    },
    ("no", "does_not_do_even_when_we_try"): {
        "skill_ability": "no",
        "performance_barrier": "persistent_failure",
        "scoring_norm_answer": "no",
    },
    ("no", "upset_or_refuses"): {
        "skill_ability": "yes",
        "performance_barrier": "emotional_dysregulation_refusal",
        "scoring_norm_answer": "yes",
    },
    ("no", "distracted_before_doing"): {
        "skill_ability": "yes",
        "performance_barrier": "distractibility",
        "scoring_norm_answer": "yes",
    },
    ("no", "not_sure_why"): {
        "skill_ability": "unclear",
        "performance_barrier": "unclear",
        "scoring_norm_answer": "not_sure",
    },
}

BEHAVIORAL_PERFORMANCE_BARRIERS = {
    "distractibility",
    "needs_prompting",
    "needs_demonstration",
    "emotional_support",
    "emotional_dysregulation_refusal",
    "situational_inconsistency",
}

# ------------------------------------------------------------------
# V22 activity feedback options
# ------------------------------------------------------------------
ACTIVITY_FEEDBACK_OPTIONS = {
    "difficulty": ["too_hard", "just_right", "too_easy"],
    "performance": ["done_independently", "done_with_help", "couldnt_do_it"],
    "engagement": ["enjoyed_it", "resisted_it", "didnt_like_it"],
}

# ------------------------------------------------------------------
# Motor emerging subdomain weights
# ------------------------------------------------------------------
MOTOR_EMERGING_SUBDOMAINS = {
    "postural_control_and_transitions",
    "gross_motor_mobility_and_coordination",
    "fine_motor_hand_use",
}

GENERAL_EMERGING_PARTIAL_WEIGHT = 0.45
GENERAL_EMERGING_NO_PENALTY = 0.40

MOTOR_EMERGING_PARTIAL_WEIGHT = 0.70
MOTOR_EMERGING_NO_PENALTY = 0.25

# ------------------------------------------------------------------
# Subdomain keyword map (concern router — deterministic layer)
# ------------------------------------------------------------------
SUBDOMAIN_KEYWORD_MAP = {
    "speech_intelligibility": [
        r"\bapraxia\b",
        r"\bchildhood apraxia of speech\b",
        r"\bcas\b",
        r"hard to understand",
        r"unclear speech",
        r"inconsistent sounds",
        r"speech output",
        r"speech hard to understand",
        r"articulation",
        r"motor speech",
        r"intelligib",
        r"can't understand",
        r"cannot understand",
    ],
    "expressive_language": [
        r"no words",
        r"few words",
        r"limited speech",
        r"limited words",
        r"only\s*~?\d+\s*words",
        r"very limited speech",
        r"speech delay",
        r"late talk",
        r"two[- ]word",
        r"phrases",
        r"naming",
        r"expressive",
        r"not talking",
        r"doesn't talk",
        r"does not talk",
        r"speech regression",   # Dravet / neurological regression
        r"language regression",
        r"lost words",
        r"lost speech",
        r"losing words",
        r"word loss",
    ],
    "receptive_language": [
        r"comprehension",
        r"doesn't understand",
        r"does not understand",
        r"follows? simple directions",
        r"follows? directions",
        r"commands",
        r"receptive",
    ],
    "gestural_communication": [
        r"uses gestures",
        r"gesture",
        r"pointing",
        r"\bpoints?\b",
        r"signs?",
        r"communicat",
    ],
    "early_vocalization_and_babbling": [
        r"limited babbling",
        r"\bbabbl",
        r"\bcoo",
        r"vocal",
        r"raspberr",
        r"squeal",
        r"makes sounds",
    ],
    "conversation_narrative": [
        r"conversation",
        r"answers questions",
        r"tell(s)? (a )?story",
        r"narrative",
    ],
    "social_engagement_and_joint_attention": [
        r"eye contact",
        r"limited eye contact",
        r"joint attention",
        r"shared attention",
        r"responds? to name",
        r"responds? to her name",
        r"responds? to his name",
        r"socially engaged",
        r"good eye contact",
    ],
    "peer_interaction_and_social_rules": [
        r"\bpeer",
        r"friends?",
        r"play with children",
        r"plays with other children",
        r"takes? turns?",
        r"interrupts",
        r"social but rigid",
        r"social rules",
        r"other kids",
        r"birthday part",
        r"preschool social",
        r"join (other|kids|children)",
        r"problem around other",
    ],
    "play_and_symbolic_social_play": [
        r"pretend play",
        r"limited pretend play",
        r"symbolic play",
        r"repetitive play",
        r"lines up toys",
        r"imaginative play",
    ],
    "emotional_regulation": [
        r"tantrum",
        r"meltdown",
        r"big emotional reactions",
        r"gets frustrated",
        r"frustrat",
        r"emotional regulation",
        r"self-regulation",
        r"impulsive",
        r"very active",
        r"hyperactive",
        r"\badhd\b",
        r"lack of focus",
        r"focus problem",
        r"can't focus",
        r"attention problem",
    ],
    "attachment_and_separation": [
        r"clingy",
        r"separation",
        r"drop off",
        r"leave(s|) the room",
        r"when you leave",
    ],
    "empathy_and_prosocial_behavior": [
        r"empathy",
        r"notices when others are hurt",
        r"kind to others",
        r"hurt or upset",
    ],
    "gross_motor_mobility_and_coordination": [
        r"not walking",
        r"walking",
        r"frequent falls",
        r"stairs",
        r"balance",
        r"clumsy gait",
        r"\bgait\b",
        r"\brun\b",
        r"\brunning\b",          # "wobbly running" — Chang profile
        r"\bjump\b",
        r"\bjumping\b",          # "not yet jumping" — Chang profile
        r"not yet jumping",
        r"not yet walking",
        r"not yet running",
        r"wobbly",               # "wobbly running", "wobbly gait"
        r"walker",
        r"mobility",
        r"keeping up",
        r"trouble keeping up",
        r"difficulty with stairs",
        r"cerebral palsy",
        r"\bcp\b",
        r"physical therapy",     # explicit OT/PT routing
        r"\bpt delay\b",
        r"pt delay",
        r"\bphysio\b",
        r"gross motor delay",
    ],
    "postural_control_and_transitions": [
        r"not sitting",
        r"sitting independently",
        r"sit independently",
        r"head control",
        r"hypotonia",
        r"posture",
        r"rolling",
        r"get to sitting",
        r"pushes up",
    ],
    "fine_motor_hand_use": [
        r"fine motor",
        r"fine motor delay",
        r"grasp",
        r"beads?",
        r"string",
        r"\bfork\b",
        r"crayon",
        r"pencil",
        r"hand use",
        r"hand skills",
        r"manipulation",
        r"occupational therapy",
        r"\bot delay\b",
        r"ot delay",
        r"scissor",
        r"cutting",
        r"grip difficulty",
        r"handwriting",
    ],
    "self_help_motor_skills": [
        r"self-care",
        r"self care",
        r"\bdress",
        r"clothes",
        r"buttons?",
        r"zippers?",
        r"utensil",
        r"\bspoon\b",
        r"dressing",
        r"undressing",
        r"putting on",
        r"taking off",
    ],
    "adaptive_feeding_cues": [
        r"slow feeding",
        r"feeding",
        r"picky eating",
        r"oral motor",
        r"open mouth",
        r"close lips",
        r"\beat\b",
        r"mealtime",
    ],
    "attention_and_processing": [
        r"\battention\b",
        r"short attention span",
        r"\bfocus\b",
        r"processing",
        r"lack of focus",
        r"can't focus",
        r"distractib",
        r"\badhd\b",
        r"stays on task",
        r"task completion",
    ],
    "concepts_and_following_directions": [
        r"follow(s|) directions",
        r"one-step",
        r"two-step",
        r"concepts?",
        r"colors?",
        r"letters?",
        r"numbers?",
        r"developmental delay",  # Chang / global delay profiles
        r"global delay",
        r"globally delayed",
        r"learning delay",
        r"learning difficulties",
        r"developmental regression",
        r"cognitive delay",
    ],
    "exploration_and_object_use": [
        r"explore",
        r"object use",
        r"toy use",
        r"puts things in (his|her|their) mouth",
    ],
    "imitation_and_play_skills": [
        r"imitat",
        r"\bcopy",
        r"copies",
        r"play skills",
    ],
    "object_permanence_and_problem_solving": [
        r"problem solving",
        r"finds?",
        r"hides?",
        r"object permanence",
    ],
    "pre_academic_skills": [
        r"letters?",
        r"count(ing)?",
        r"colors?",
        r"pre-academic",
    ],
    "safety_awareness": [
        r"safety",
        r"danger",
        r"unsafe",
    ],
}

# ------------------------------------------------------------------
# Positive routing hints  (suppress over-routing into cognitive domain
# when parent explicitly says cognition is a strength)
# ------------------------------------------------------------------
POSITIVE_ROUTING_HINTS = [
    "good eye contact",
    "good comprehension",
    "understands well",
    "good cognition",
    "socially engaged",
    "strong language skills",
    "strong language",
    "very verbal",
    "very bright",
    "bright child",
    "smart",
    "great understanding",
    # Social strength hints — suppress social/emotional domain over-routing
    "social is good",
    "social is strong",
    "social skills are good",
    "social skills are strong",
    "good at social",
    "good socially",
    "socially strong",
    "social is okay",
    "social seems fine",
    "no social concerns",
    "social is not a concern",
]

# Weight applied to domain scores when a positive hint is present for that domain
POSITIVE_HINT_WEIGHT_MULTIPLIER = 0.18

# ------------------------------------------------------------------
# Safety keyword map
# ------------------------------------------------------------------
SAFETY_KEYWORD_MAP = {
    "falls_balance_gait": [
        r"frequent falls",
        r"fall",
        r"difficulty with stairs",
        r"stairs",
        r"clumsy gait",
        r"balance",
        r"walker",
        r"unsteady",
        r"unstable",
        r"unstable gait",
        r"unstable walk",
        r"cerebral palsy",
        r"\bcp\b",
        r"ataxia",
        r"wobbly",
        r"down syndrome",
        r"down's syndrome",
        r"trisomy.?21",
        r"\btrisomy\b",
        r"\bdowns?\b",
        r"not jumping",
        r"not hopping",
        r"can'?t jump",
        r"gross motor delay",
        r"\bpt delay\b",
        r"physical therapy",
        r"\bphysio\b",
    ],
    "postural_low_tone_fatigue": [
        r"hypotonia",
        r"low muscle tone",
        r"low tone",
        r"tires quickly",
        r"fatigue",
        r"weak",
        r"not sitting independently",
        r"not walking",
        r"down syndrome",
        r"down's syndrome",
        r"trisomy.?21",
        r"\btrisomy\b",
        r"\bdowns?\b",
    ],
    "fine_motor_or_coordination": [
        r"fine motor",
        r"grip",
        r"coordination",
        r"clumsy hand",
        r"uses a fork",
        r"beads",
        r"crayon",
        r"pencil",
    ],
    "feeding_or_oral_motor": [
        r"feeding",
        r"slow feeding",
        r"chew",
        r"swallow",
        r"chok",
        r"drool",
        r"oral motor",
        r"oral-motor",
    ],
    "regulation_frustration": [
        r"tantrum",
        r"frustrat",
        r"difficulty with transitions",
        r"transitions",
        r"rigid",
        r"big emotional reactions",
        r"impuls",
        r"short attention",
        r"attention",
        r"\badhd\b",
    ],
    "sensory_sensitivity": [
        r"sensory",
        r"sensitive",
        r"picky eating",
        r"texture",
        r"noise",
        r"overwhelm",
    ],
    "mobility_equipment_support": [
        r"walker",
        r"wheelchair",
        r"braces",
        r"orthotic",
        r"uses support",
    ],
    "seizure_or_medical_monitoring": [
        r"seizure",
        r"epilep",
        r"drop attack",
        r"dravet",
        r"medical frag",
    ],
    "high_activity_open_space_risk": [
        r"very active",
        r"bolts",
        r"runs off",
        r"elopes",
        r"unsafe climbing",
    ],
}

SAFETY_CONSTRAINT_TEMPLATES = {
    "falls_balance_gait": (
        "Keep activities ground-level and closely supervised. "
        "Avoid high surfaces, jumping from heights, or tasks that assume stable balance without support."
    ),
    "postural_low_tone_fatigue": (
        "Prefer short bouts with rest breaks, supported positioning, and lower-endurance tasks. "
        "Avoid long sustained postures beyond the child's tolerance."
    ),
    "fine_motor_or_coordination": (
        "Adapt hand demands with larger tools/items, stabilizing support, and slower pacing "
        "rather than precision-heavy expectations."
    ),
    "feeding_or_oral_motor": (
        "Keep feeding or oral-motor tasks upright and closely supervised. "
        "Avoid choking-risk foods or unsafe oral-motor suggestions."
    ),
    "regulation_frustration": (
        "Keep tasks short, predictable, and easy to start. "
        "Build in transitions, choices, and stop before escalation or overload."
    ),
    "sensory_sensitivity": (
        "Use low-clutter, lower-noise materials when possible and introduce textures/sounds gradually."
    ),
    "mobility_equipment_support": (
        "Adapt tasks for walker/braces/other supports and do not assume unsupported stairs, standing, or walking."
    ),
    "seizure_or_medical_monitoring": (
        "Avoid activities that would be unsafe if sudden loss of awareness or control occurred. "
        "Keep supervision explicit and setups low risk."
    ),
    "high_activity_open_space_risk": (
        "Use contained spaces, clear physical boundaries, and active supervision. "
        "Avoid tasks that depend on open unsafe spaces or long waiting."
    ),
}

# ------------------------------------------------------------------
# Language scoring tracks
# ------------------------------------------------------------------
LANGUAGE_SCORING_TRACKS = {
    "expressive_speech": {
        "subdomains": {
            "expressive_language",
            "speech_intelligibility",
            "early_vocalization_and_babbling",
            "conversation_narrative",
        }
    },
    "receptive": {
        "subdomains": {"receptive_language"}
    },
    "gesture": {
        "subdomains": {"gestural_communication"}
    },
}
