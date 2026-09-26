"""Category rules, tone management, taboo filters, and conversational intent detection."""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple


# Category-specific taboo words that must never be used
CATEGORY_TABOOS: Dict[str, List[str]] = {
    "dentists": [
        "guaranteed",
        "100% safe",
        "completely cure",
        "miracle",
        "best in city",
        "doctor approved",
    ],
    "salons": [
        "guaranteed glow",
        "permanent results",
        "instant transformation",
        "miracle",
        "best in city",
    ],
    "restaurants": [
        "best food in city",
        "guaranteed packed house",
        "miracle marketing",
        "viral guarantee",
    ],
    "gyms": [
        "guaranteed weight loss",
        "shred in 7 days",
        "miracle transformation",
        "fastest results",
    ],
    "pharmacies": [
        "miracle cure",
        "guaranteed result",
        "100% safe",
        "best price",
    ],
}

# AI cliches to avoid
AI_CLICHES = [
    "based on our advanced analysis",
    "based on our analysis",
    "our ai has identified",
    "our ai",
    "leverage this opportunity",
    "optimize your business performance",
    "optimize your business",
    "unlock growth",
    "unlock your potential",
    "strategic advantage",
]

# Patterns for WhatsApp business canned auto-replies
AUTO_REPLY_PATTERNS = [
    r"thank\s+you\s+for\s+contacting",
    r"our\s+team\s+will\s+respond",
    r"will\s+respond\s+shortly",
    r"will\s+get\s+back\s+to\s+you",
    r"automated\s+(assistant|reply|message|response)",
    r"auto[\s-]?reply",
    r"canned\s+response",
    r"shukriya.*hamari\s+team",
    r"currently\s+unavailable",
    r"leave\s+a\s+message",
]

# Patterns for opt-out or hostile intent
OPT_OUT_PATTERNS = [
    r"\bstop\b",
    r"\bunsubscribe\b",
    r"don['’]?t\s+message",
    r"dont\s+message",
    r"stop\s+(messaging|sending|bothering)",
    r"not\s+interested",
    r"useless\s+spam",
    r"\bspam\b",
    r"leave\s+me\s+alone",
    r"why\s+are\s+you\s+bothering",
    r"band\s+karo",
    r"mat\s+bhejo",
    r"not\s+needed",
]

# Patterns for explicit merchant acceptance / commitment
INTENT_COMMIT_PATTERNS = [
    r"ok\s+let['’]?s\s+do\s+it",
    r"let['’]?s\s+do\s+it",
    r"what['’]?s\s+next",
    r"whats\s+next",
    r"yes\s+please",
    r"yes\s+send",
    r"yes\s*,?\s*send\s+me",
    r"pull\s+the\s+abstract",
    r"draft\s+the\s+patient[-\s]?ed\s+whatsapp",
    r"patient[-\s]?ed\s+whatsapp",
    r"\bproceed\b",
    r"\bconfirm\b",
    r"go\s+ahead",
    r"send\s+(it|the\s+abstract|the\s+draft|the\s+post)",
    r"draft\s+it",
    r"draft\s+the\s+patient",
    r"sounds\s+good",
    r"haan\s+bhejo",
    r"chalo\s+karo",
    r"make\s+it\s+live",
]

# Patterns for off-topic inquiries
OFF_TOPIC_PATTERNS = [
    r"\bgst\b",
    r"income\s+tax",
    r"tax\s+filing",
    r"ca\s+firm",
    r"accounting",
    r"business\s+loan",
    r"filing\s+this\s+month",
]

# Patterns for "later / busy"
LATER_PATTERNS = [
    r"busy\s+right\s+now",
    r"call\s+(me\s+)?later",
    r"message\s+later",
    r"later\s+please",
    r"maybe\s+later",
    r"check\s+back\s+later",
    r"some\s+other\s+time",
    r"not\s+interested\s+right\s+now",
    r"not\s+right\s+now",
    r"tomorrow",
    r"baad\s+mein",
]


def format_salutation(
    category_slug: str, owner_first_name: str, biz_name: str = "", is_hindi: bool = False
) -> str:
    """Formats category-appropriate salutation."""
    first = owner_first_name.strip() if owner_first_name else ""

    if category_slug == "dentists":
        if first:
            if first.lower().startswith("dr."):
                return first
            elif first.lower().startswith("dr"):
                return f"Dr. {first[2:].strip()}"
            return f"Dr. {first}"
        return "Doctor"

    if category_slug == "pharmacies":
        if is_hindi:
            return f"Namaste {first}" if first else "Namaste"
        return f"Hi {first}" if first else "Hi there"

    if category_slug == "gyms":
        return f"Hi {first}" if first else "Coach"

    if first:
        return f"Hi {first}"
    if biz_name:
        return f"Hi {biz_name} team"
    return "Hi there"


def is_auto_reply(message: str) -> bool:
    """Checks if message matches canned auto-reply signatures."""
    msg = message.lower().strip()
    return any(re.search(pat, msg) for pat in AUTO_REPLY_PATTERNS)


def is_opt_out(message: str) -> bool:
    """Checks if merchant requested opt-out or expressed hostility."""
    msg = message.lower().strip()
    return any(re.search(pat, msg) for pat in OPT_OUT_PATTERNS)


def is_intent_commit(message: str) -> bool:
    """Checks if merchant gave explicit green-light / commitment to proceed."""
    msg = message.lower().strip()
    return any(re.search(pat, msg) for pat in INTENT_COMMIT_PATTERNS)


def is_off_topic(message: str) -> bool:
    """Checks if message asks an off-topic question (e.g. GST filing)."""
    msg = message.lower().strip()
    return any(re.search(pat, msg) for pat in OFF_TOPIC_PATTERNS)


def is_later_request(message: str) -> bool:
    """Checks if merchant asked to follow up later."""
    msg = message.lower().strip()
    return any(re.search(pat, msg) for pat in LATER_PATTERNS)


def sanitize_message(text: str, category_slug: str) -> str:
    """Ensures message is clean:

    - Removes taboos for the category
    - Removes corporate AI cliches
    - Removes any forbidden external URLs
    - Normalizes spacing
    """
    clean = text

    # Remove URLs (Meta WhatsApp template penalty & brief penalty)
    clean = re.sub(r"https?://\S+", "", clean)

    # Check taboos
    taboos = CATEGORY_TABOOS.get(category_slug, [])
    for taboo in taboos:
        # Case insensitive replacement
        pattern = re.compile(re.escape(taboo), re.IGNORECASE)
        clean = pattern.sub("", clean)

    # Check AI cliches
    for cliche in AI_CLICHES:
        pattern = re.compile(re.escape(cliche), re.IGNORECASE)
        clean = pattern.sub("", clean)

    # Clean double spaces and awkward punctuation
    clean = re.sub(r"\s+", " ", clean)
    clean = re.sub(r"\s+([.,?!])", r"\1", clean)
    return clean.strip()
