"""
Orchestrator — decides what to do with each user turn.

Modes:
  - meaning   : explain the current sentence in Hindi/English
  - translate : user asks to check → invite translation → check → loop if wrong
  - grammar   : ask a grammar question (hard level only)
  - move_on   : advance to the next sentence
"""

import re
from data.sentences import Sentence
import llm

# ── intent detection ──────────────────────────────────────────────────────────
MEANING_KEYWORDS = [
    "meaning", "matlab", "arth", "samjhao", "samjha", "explain",
    "what does", "what is", "मतलब", "अर्थ", "समझाओ", "समझा", "मीनिंग", "एक्सप्लेन",
]

TRANSLATION_KEYWORDS = [
    "translate", "translation", "anuvad", "check", "sahi hai",
    "अनुवाद", "सही है", "ट्रांसलेशन", "ट्रांसलेट", "चेक",
]

MOVE_ON_KEYWORDS = [
    "move on", "next", "agla", "agle", "aage", "chalo", "chaliye", "done", "got it",
    "नेक्स्ट", "अगला", "अगले", "आगे", "चलो", "चलिए", "आगे बढ़", "समझ गया", "समझ गयी",
]

# Explicit advance words that escape any waiting loop. Kept to unambiguous
# tokens so a translation attempt can't accidentally trigger it.
ESCAPE_MOVE_ON = ["move on", "next", "नेक्स्ट", "agla", "agle", "अगला", "अगले", "आगे बढ़"]

def _has_keyword(text: str, keywords: list[str]) -> bool:
    """Whole-token match so short tokens like 'ok' don't match inside 'shlok'."""
    for k in keywords:
        if re.search(rf"(?<!\w){re.escape(k)}(?!\w)", text):
            return True
    return False


def detect_intent(text: str) -> str:
    """Returns one of: 'meaning' | 'translate' | 'move_on' | 'unknown'."""
    t = text.lower()
    if _has_keyword(t, MOVE_ON_KEYWORDS):
        return "move_on"
    if _has_keyword(t, MEANING_KEYWORDS):
        return "meaning"
    if _has_keyword(t, TRANSLATION_KEYWORDS):
        return "translate"
    return "unknown"


def _parse_result(raw: str) -> tuple[str, bool]:
    """Pull RESULT:CORRECT/RETRY from anywhere in the reply and strip it.

    The conversational system prompt makes the model drop or move the marker,
    so we search leniently instead of trusting it to sit on line 1.
    """
    m = re.search(r"RESULT\s*:?\s*(CORRECT|RETRY|INCORRECT|WRONG)", raw, re.IGNORECASE)
    is_correct = bool(m) and m.group(1).upper() == "CORRECT"
    spoken = re.sub(
        r"RESULT\s*:?\s*(CORRECT|RETRY|INCORRECT|WRONG)", "", raw, flags=re.IGNORECASE
    ).strip().strip("-").strip()
    return (spoken or raw.strip()), is_correct


# ── handlers ──────────────────────────────────────────────────────────────────
async def handle_meaning(sentence: Sentence, history: list[dict]) -> str:
    prompt = (
        f"The student is reading this Sanskrit sentence: '{sentence['sanskrit']}'\n"
        f"They want to know the meaning. "
        f"Hindi meaning: {sentence['meaning_hi']}. "
        f"English meaning: {sentence['meaning_en']}. "
        f"Explain it simply and encouragingly in 2-3 sentences."
    )
    return await llm.chat(prompt, history)


async def handle_translation_request(sentence: Sentence, history: list[dict]) -> str:
    """User asked to CHECK a translation but hasn't given one yet — invite it."""
    prompt = (
        f"The student wants to translate this Sanskrit sentence: '{sentence['sanskrit']}'.\n"
        f"They have NOT given their translation yet — they only asked to check one.\n"
        f"Warmly invite them, in 1-2 short sentences, to say their English translation now. "
        f"Do NOT translate it for them."
    )
    return await llm.chat(prompt, history)


async def handle_translation_check(
    sentence: Sentence, user_translation: str, history: list[dict]
) -> tuple[str, bool]:
    """Returns (spoken_response, is_correct)."""
    prompt = (
        f"Sanskrit sentence: '{sentence['sanskrit']}'\n"
        f"Correct English translation: '{sentence['translation_en']}'\n"
        f"Student's translation: '{user_translation}'\n\n"
        f"Is the student's translation correct or close enough?\n"
        f"Start your reply with the token RESULT:CORRECT or RESULT:RETRY, then your "
        f"warm spoken feedback. If wrong, gently say what's off and ask them to try again."
    )
    raw = await llm.chat(prompt, history)
    return _parse_result(raw)


async def handle_grammar(sentence: Sentence, history: list[dict]) -> str:
    prompt = (
        f"Sanskrit sentence: '{sentence['sanskrit']}'\n"
        f"Grammar question to ask: '{sentence['grammar_note']}'\n"
        f"Ask the student this grammar question in a friendly way. Keep it short."
    )
    return await llm.chat(prompt, history)


async def handle_grammar_answer(
    sentence: Sentence, user_answer: str, history: list[dict]
) -> tuple[str, bool]:
    prompt = (
        f"Sanskrit sentence: '{sentence['sanskrit']}'\n"
        f"Grammar question: '{sentence['grammar_note']}'\n"
        f"Student's answer: '{user_answer}'\n\n"
        f"Start your reply with the token RESULT:CORRECT or RESULT:RETRY, then your "
        f"warm spoken feedback."
    )
    raw = await llm.chat(prompt, history)
    return _parse_result(raw)


async def handle_unknown(sentence: Sentence, history: list[dict]) -> str:
    prompt = (
        f"The student is studying this Sanskrit sentence: '{sentence['sanskrit']}'\n"
        f"They said something unclear. Gently ask them what they'd like — "
        f"meaning, translation check, or to move to the next sentence."
    )
    return await llm.chat(prompt, history)


# ── main entry point ──────────────────────────────────────────────────────────
async def process_turn(
    user_text: str,
    sentence: Sentence,
    level: str,
    history: list[dict],
    awaiting_grammar: bool = False,
    awaiting_translation: bool = False,
) -> dict:
    # An explicit "next"/"move on" always escapes a waiting loop, so a stuck
    # RESULT parse can never soft-lock the session by voice.
    if _has_keyword(user_text.lower(), ESCAPE_MOVE_ON):
        return {
            "response_text": "बहुत अच्छे! अगला वाक्य शुरू करते हैं। (Great! Let's move on.)",
            "intent": "move_on",
            "move_on": True,
            "awaiting_grammar": False,
            "awaiting_translation": False,
        }

    # We're mid-loop, waiting for a specific answer.
    if awaiting_translation:
        spoken, correct = await handle_translation_check(sentence, user_text, history)
        return {
            "response_text": spoken,
            "intent": "translate",
            "move_on": False,
            "awaiting_grammar": False,
            "awaiting_translation": not correct,  # keep looping only if wrong
        }

    if awaiting_grammar:
        spoken, correct = await handle_grammar_answer(sentence, user_text, history)
        if correct and level == "hard":
            spoken += " अब अगले वाक्य पर चलते हैं? (Move on?)"
        return {
            "response_text": spoken,
            "intent": "grammar",
            "move_on": False,
            "awaiting_grammar": not correct,
            "awaiting_translation": False,
        }

    intent = detect_intent(user_text)

    if intent == "move_on":
        return {
            "response_text": "बहुत अच्छे! अगला वाक्य शुरू करते हैं। (Great! Let's start the next sentence.)",
            "intent": "move_on",
            "move_on": True,
            "awaiting_grammar": False,
            "awaiting_translation": False,
        }

    if intent == "meaning":
        spoken = await handle_meaning(sentence, history)
        return {
            "response_text": spoken,
            "intent": "meaning",
            "move_on": False,
            "awaiting_grammar": False,
            "awaiting_translation": False,
        }

    if intent == "translate":
        # FIRST translate turn = a request to begin, NOT the translation itself.
        # Invite the attempt and wait for it on the next turn.
        spoken = await handle_translation_request(sentence, history)
        return {
            "response_text": spoken,
            "intent": "translate",
            "move_on": False,
            "awaiting_grammar": False,
            "awaiting_translation": True,
        }

    # Hard level: an unclear utterance after reading triggers a grammar question.
    if level == "hard" and intent == "unknown":
        spoken = await handle_grammar(sentence, history)
        return {
            "response_text": spoken,
            "intent": "grammar",
            "move_on": False,
            "awaiting_grammar": True,
            "awaiting_translation": False,
        }

    spoken = await handle_unknown(sentence, history)
    return {
        "response_text": spoken,
        "intent": "unknown",
        "move_on": False,
        "awaiting_grammar": False,
        "awaiting_translation": False,
    }