"""
genex_core/config.py
--------------------
All domain configs, answer scores, subdomain keyword maps, and safety keyword maps.
Extracted verbatim from genex_interview_activity_v11.ipynb — no logic changed.
"""

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
    "physical": "movement_and_physical",
    "motor": "movement_and_physical",
    "gross motor": "movement_and_physical",
    "social and emotional": "social_and_emotional",
    "social and emotial": "social_and_emotional",
    "social_emotional": "social_and_emotional",
    "social": "social_and_emotional",
    "language and communication": "language_and_communication",
    "language": "language_and_communication",
    "speech": "language_and_communication",
    "speech and language": "language_and_communication",
    "cognitive": "cognitive",
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
# Subdomain keyword map (concern router)
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
    ],
    "receptive_language": [
        r"understands well",
        r"good comprehension",
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
    ],
    "play_and_symbolic_social_play": [
        r"pretend play",
        r"limited pretend play",
        r"symbolic play",
        r"repetitive play",
        r"lines up toys",
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
        r"adhd",
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
        r"run",
        r"jump",
        r"walker",
        r"mobility",
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
        r"grasp",
        r"beads?",
        r"string",
        r"\bfork\b",
        r"crayon",
        r"pencil",
        r"hand use",
    ],
    "self_help_motor_skills": [
        r"self-care",
        r"dress",
        r"clothes",
        r"buttons?",
        r"zippers?",
        r"utensil",
        r"\bspoon\b",
    ],
    "adaptive_feeding_cues": [
        r"slow feeding",
        r"feeding",
        r"picky eating",
        r"oral motor",
        r"open mouth",
        r"close lips",
    ],
    "attention_and_processing": [
        r"attention",
        r"short attention span",
        r"\bfocus\b",
        r"processing",
    ],
    "concepts_and_following_directions": [
        r"follow(s|) directions",
        r"one-step",
        r"two-step",
        r"concepts?",
        r"colors?",
        r"letters?",
        r"numbers?",
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

POSITIVE_ROUTING_HINTS = [
    "good eye contact",
    "good comprehension",
    "understands well",
    "good cognition",
    "socially engaged",
    "strong language skills",
    "strong language",
    "very verbal",
]

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
        r"cerebral palsy",
        r"ataxia",
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
        r"adhd",
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
