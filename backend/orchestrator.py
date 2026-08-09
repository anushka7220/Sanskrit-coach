"""
Orchestrator — decides what to do with each user turn.

Modes:
  - meaning   : explain the current sentence in Hindi/English
  - translate : user asks to check → invite translation → check → loop if wrong
  - grammar   : ask a grammar question (hard level only)
  - move_on   : advance to the next sentence

STREAMING CONTRACT
------------------
`process_turn_stream()` is now the primary entry point. It yields:

    {"type": "token", "text": "..."}     # zero or more, in order
    {"type": "meta",  "intent": str,
                      "move_on": bool,
                      "awaiting_grammar": bool,
                      "awaiting_translation": bool}

`process_turn()` is kept as a batch wrapper over the same generator, so there
is exactly one copy of the routing logic.

PROMPTS — WRITTEN IN VIDYA'S SITUATION, NOT AS FORMAT INSTRUCTIONS
-------------------------------------------------------------------
The system prompt (llm.py) defines who Vidya is — warm, calm, Hinglish,
female forms, short replies. The prompts here only need to tell her what
situation she's in and what the student just did. She figures out how to say
it.

Old pattern: "Explain it simply and encouragingly in 2-3 sentences."
New pattern: "The student asked what this means. Help them understand."

The difference: old prompts described output format. New prompts describe
the moment. Vidya's voice comes from the system prompt, not from adjectives
like "warmly" repeated in every prompt.
"""

import re
from typing import AsyncIterator

from data.sentences import Sentence
import llm
import faq
import safety

# ── intent detection ──────────────────────────────────────────────────────────
MEANING_KEYWORDS = [
    "meaning", "matlab", "arth", "samjhao", "samjha", "explain",
    "what does", "what is", "मतलब", "अर्थ", "समझाओ", "समझा", "मीनिंग", "एक्सप्लेन",
]

TRANSLATION_KEYWORDS = [
    "translate", "translation", "anuvad", "check",
    "अनुवाद", "ट्रांसलेशन", "ट्रांसलेट", "चेक",
]

MOVE_ON_KEYWORDS = [
    "move on", "next sentence", "next vaakya", "agla vaakya", "agle vaakya",
    "aage badho", "aage badhte", "aage chalo", "aage chaliye",
    "नेक्स्ट", "अगला वाक्य", "अगले वाक्य", "आगे बढ़", "आगे चलो", "आगे चलिए",
    "next karo", "अगला करो",
]

ESCAPE_MOVE_ON = [
    "move on", "next sentence", "नेक्स्ट", "अगला वाक्य", "अगले वाक्य",
    "आगे बढ़", "agla vaakya", "agle vaakya", "aage badho",
]

MOVE_ON_LINE = "nice! agla sentence shuru karte hain."
# ── Level switching ───────────────────────────────────────────────────────────
LEVEL_WORDS = {
    "easy": ["easy", "आसान", "सरल", "beginner", "बिगिनर", "इजी", "सिंपल", "simple",
             "aasan", "asaan", "saral"],
    "intermediate": ["intermediate", "medium", "मध्यम", "इंटरमीडिएट", "मीडियम",
                     "madhyam"],
    "hard": ["hard", "difficult", "advanced", "कठिन", "मुश्किल", "हार्ड", "एडवांस",
             "kathin", "mushkil"],
}

LEVEL_CHANGE_INTENT = [
    "level", "लेवल", "बदल", "change", "switch", "कर दो", "करो", "चाहिए",
    "le chalo", "ले चलो", "पर चलो", "pe chalo", "shift", "जाओ", "करा दो",
    "chahiye", "badal", "karo", "kar do",
]

LEVEL_CHANGE_LINES = {
    "easy": "ठीक है, easy level पर चलते हैं।",
    "intermediate": "ठीक है, intermediate level पर चलते हैं।",
    "hard": "ठीक है, hard level पर चलते हैं।",
}


def detect_level_change(text: str, current_level: str) -> str | None:
    t = text.lower()
    if not _has_keyword(t, LEVEL_CHANGE_INTENT):
        return None
    for level, words in LEVEL_WORDS.items():
        if _has_keyword(t, words):
            return None if level == current_level else level
    return None


def _has_keyword(text: str, keywords: list[str]) -> bool:
    for k in keywords:
        if re.search(rf"(?<!\w){re.escape(k)}(?!\w)", text):
            return True
    return False


def detect_intent(text: str) -> str:
    t = text.lower()
    if _has_keyword(t, MEANING_KEYWORDS):
        return "meaning"
    if _has_keyword(t, TRANSLATION_KEYWORDS):
        return "translate"
    if _has_keyword(t, MOVE_ON_KEYWORDS):
        return "move_on"
    return "unknown"


HINDI_TARGET_KEYWORDS = [
    "hindi", "हिंदी", "हिन्दी", "hindi mein", "हिंदी में",
]
ENGLISH_TARGET_KEYWORDS = [
    "english", "अंग्रेजी", "अंग्रेज़ी", "इंग्लिश", "angrezi",
]


def detect_target_language(text: str) -> str:
    t = text.lower()
    if _has_keyword(t, HINDI_TARGET_KEYWORDS):
        return "hi"
    if _has_keyword(t, ENGLISH_TARGET_KEYWORDS):
        return "en"
    return "en"


def _reference_translation(sentence: Sentence, lang: str) -> str:
    if lang == "hi":
        return sentence.get("meaning_hi") or sentence["translation_en"]
    return sentence["translation_en"]


# ── prompts ───────────────────────────────────────────────────────────────────
# Written as situation context, not format instructions.
# Vidya's voice comes from the system prompt in llm.py — these prompts only
# tell her what's happening right now.
def _prompt_meaning(sentence: Sentence) -> str:
    return (
        f"The student is reading: '{sentence['sanskrit']}'\n"
        f"They want to know what it means.\n\n"
        f"Hindi meaning: {sentence['meaning_hi']}\n"
        f"English meaning: {sentence['meaning_en']}\n\n"
        f"Explain it casually — like you're telling a friend, not teaching a class. "
        f"Start with the Hindi meaning, then the English. "
        f"If there's something interesting about the sentence (a word root, "
        f"a connection to modern Hindi), mention it briefly. "
        f"2-3 sentences max. Use Hinglish naturally."
    )


def _prompt_translation_request(sentence: Sentence, lang: str = "en") -> str:
    lang_name = "Hindi" if lang == "hi" else "English"
    return (
        f"The student is studying: '{sentence['sanskrit']}'\n"
        f"They want to check their {lang_name} translation, but haven't "
        f"given it yet.\n\n"
        f"Invite them to say their {lang_name} translation — casual and curious, "
        f"like a friend who genuinely wants to hear what they came up with. "
        f"Use tum, not aap. One or two sentences only. "
        f"Do NOT translate it for them."
    )


def _prompt_translation_check(sentence: Sentence, user_translation: str,
                              lang: str = "en") -> str:
    lang_name = "Hindi" if lang == "hi" else "English"
    return (
        f"Sanskrit sentence: '{sentence['sanskrit']}'\n"
        f"Correct {lang_name} translation: '{_reference_translation(sentence, lang)}'\n"
        f"What the student said: '{user_translation}'\n\n"
        f"Begin your reply with EXACTLY one of these as the very first "
        f"characters — nothing before it, not even a greeting:\n"
        f"  RESULT:CORRECT  — their translation is right or close enough\n"
        f"  RESULT:RETRY    — they tried but got something wrong\n"
        f"  RESULT:OFFTOPIC — they didn't attempt a translation at all\n\n"
        f"Then, on the next line, respond as Vidya would:\n"
        f"- CORRECT: react like a friend who's genuinely pleased — brief and real. "
        f"NEVER say बहुत बढ़िया or शाबाश. Something like 'haan yahi tha!' or "
        f"'ekdum sahi.' then move on naturally. Use tum forms.\n"
        f"- RETRY: tell them exactly what's off, lightly — like a heads-up not a "
        f"correction. One specific thing to fix. Then invite them to try again. "
        f"Use tum: 'tum phir se try karo'.\n"
        f"- OFFTOPIC: respond to what they said in 1-2 sentences, "
        f"then lightly bring them back to the {lang_name} translation."
    )


def _prompt_grammar(sentence: Sentence) -> str:
    return (
        f"Sanskrit sentence: '{sentence['sanskrit']}'\n"
        f"Grammar point to explore: '{sentence['grammar_note']}'\n\n"
        f"Ask the student about this grammar point like a curious friend — "
        f"not an exam question. Something you noticed and want to see if they "
        f"caught it too. Give a small hint if it feels hard on its own. "
        f"Keep it short and casual. Use tum, not aap."
    )


def _prompt_grammar_answer(sentence: Sentence, user_answer: str) -> str:
    return (
        f"Sanskrit sentence: '{sentence['sanskrit']}'\n"
        f"Grammar question: '{sentence['grammar_note']}'\n"
        f"Student's answer: '{user_answer}'\n\n"
        f"Begin your reply with EXACTLY one of these as the very first "
        f"characters — nothing before it:\n"
        f"  RESULT:CORRECT  — their answer is right or close enough\n"
        f"  RESULT:RETRY    — they answered but got it wrong\n"
        f"  RESULT:OFFTOPIC — they didn't answer the grammar question at all\n\n"
        f"Then respond as Vidya:\n"
        f"- CORRECT: react naturally — 'haan! yahi hai.' then briefly say why "
        f"it works. No शाबाश, no बहुत बढ़िया. Friend energy, not teacher energy. "
        f"Use tum forms.\n"
        f"- RETRY: give a clearer hint, not the answer yet. Ask them to try "
        f"once more. Use tum: 'ek baar aur try karo'.\n"
        f"- OFFTOPIC: respond to what they actually said, then gently re-ask "
        f"the grammar question. Use tum throughout."
    )


def _prompt_faq(sentence: Sentence, user_text: str, entry: dict) -> str:
    return (
        f"The student asked: '{user_text}'\n\n"
        f"Here is the correct information to base your answer on:\n"
        f"{entry['answer']}\n\n"
        f"Answer as Vidya — casual Hinglish, 1-2 short sentences. "
        f"Use tum, not aap. Don't add anything the information above doesn't say. "
        f"If their question goes beyond what's covered, answer the part you "
        f"can and say plainly you're not sure about the rest.\n"
        f"Then briefly offer to get back to '{sentence['sanskrit']}'."
    )


def _prompt_unknown(sentence: Sentence, user_text: str = "") -> str:
    return (
        f"The student is studying: '{sentence['sanskrit']}'\n"
        f"They said: '{user_text}'\n\n"
        f"This isn't about meaning, translation, or moving on. "
        f"Respond to what they actually said — the way a friend would mid-conversation. "
        f"Natural, brief, use tum not aap, then bring them back to the lesson.\n\n"
        f"If it's small talk, play along for one sentence then return.\n"
        f"If it's something you genuinely can't do, say so lightly — once — "
        f"without over-apologising.\n"
        f"If it's a one-word or unclear thing, just ask them casually what they meant."
    )
# ── streaming helpers ─────────────────────────────────────────────────────────

_RESULT_RE = re.compile(
    r"RESULT\s*:?\s*(CORRECT|RETRY|INCORRECT|WRONG|OFFTOPIC)", re.IGNORECASE
)


def _normalise_result(raw: str) -> str:
    r = raw.upper()
    if r == "CORRECT":
        return "CORRECT"
    if r == "OFFTOPIC":
        return "OFFTOPIC"
    return "RETRY"


_MARKER_SCAN_CHARS = 48


def _strip_marker(text: str) -> str:
    return _RESULT_RE.sub("", text, count=1).strip().strip("-").strip()


async def _stream_checked(prompt: str, history: list[dict],
                          student: dict | None = None):
    head = ""
    resolved: str | None = None

    async for token in llm.stream_chat(prompt, history, student):
        if resolved is None:
            head += token
            match = _RESULT_RE.search(head)
            if match:
                resolved = _normalise_result(match.group(1))
                yield ("result", resolved)
                rest = _strip_marker(head)
                if rest:
                    yield ("token", rest)
            elif len(head) >= _MARKER_SCAN_CHARS:
                # No marker in the opening — the model just started talking
                # instead of classifying. That almost always means the student
                # said something conversational, not a wrong answer. Defaulting
                # to RETRY here was trapping people in the loop; OFFTOPIC
                # releases it and lets the reply through as normal conversation.
                resolved = "OFFTOPIC"
                yield ("result", resolved)
                yield ("token", head)
            continue
        yield ("token", token)

    if resolved is None:
        match = _RESULT_RE.search(head)
        # Same reasoning for a short reply that ended before the scan window:
        # no marker means uncertain, and uncertain should not trap the student.
        resolved = _normalise_result(match.group(1)) if match else "OFFTOPIC"
        yield ("result", resolved)
        rest = _strip_marker(head) if match else head.strip()
        if rest:
            yield ("token", rest)


async def _stream_plain(prompt: str, history: list[dict],
                        student: dict | None = None) -> AsyncIterator[dict]:
    async for token in llm.stream_chat(prompt, history, student):
        yield {"type": "token", "text": token}


# ── main entry point ──────────────────────────────────────────────────────────

async def process_turn_stream(
    user_text: str,
    sentence: Sentence,
    level: str,
    history: list[dict],
    awaiting_grammar: bool = False,
    awaiting_translation: bool = False,
    student: dict | None = None,
) -> AsyncIterator[dict]:

    unsafe = safety.detect(user_text)
    print(f"[safety] check: {unsafe or 'clear'} | {user_text[:80]}")
    if unsafe:
        yield {
            "type": "meta", "intent": "safety", "move_on": False,
            "awaiting_grammar": False, "awaiting_translation": False,
            "safety": unsafe,
        }
        yield {"type": "token", "text": safety.response_for(unsafe)}
        return

    new_level = detect_level_change(user_text, level)
    if new_level:
        print(f"[level] request detected: {level} → {new_level}")
        yield {"type": "token", "text": LEVEL_CHANGE_LINES[new_level]}
        yield {
            "type": "meta", "intent": "change_level", "move_on": False,
            "awaiting_grammar": False, "awaiting_translation": False,
            "change_level": new_level,
        }
        return

    if _has_keyword(user_text.lower(), ESCAPE_MOVE_ON):
        yield {"type": "token", "text": MOVE_ON_LINE}
        yield {
            "type": "meta", "intent": "move_on", "move_on": True,
            "awaiting_grammar": False, "awaiting_translation": False,
        }
        return

    if awaiting_translation:
        target = awaiting_translation if isinstance(awaiting_translation, str) else "en"
        result = "RETRY"
        async for kind, value in _stream_checked(
            _prompt_translation_check(sentence, user_text, target), history, student
        ):
            if kind == "result":
                result = value
            else:
                yield {"type": "token", "text": value}
        # OFFTOPIC means the student wasn't attempting a translation at all —
        # they said something conversational. Keeping awaiting_translation set
        # in that case traps them: their normal reply gets graded as a wrong
        # translation over and over. Only RETRY should keep the loop open.
        still_waiting = target if result == "RETRY" else False
        yield {
            "type": "meta", "intent": "translate", "move_on": False,
            "awaiting_grammar": False,
            "awaiting_translation": still_waiting,
        }
        return

    if awaiting_grammar:
        result = "RETRY"
        async for kind, value in _stream_checked(
            _prompt_grammar_answer(sentence, user_text), history, student
        ):
            if kind == "result":
                result = value
            else:
                yield {"type": "token", "text": value}
        correct = result == "CORRECT"
        # Same fix as translation: OFFTOPIC (the student was just talking, not
        # answering the grammar question) must release the loop. Previously
        # `not correct` kept awaiting_grammar=True on OFFTOPIC, so a plain
        # remark like "haan notice kiya maine" was rejected as a wrong answer
        # and the student got stuck on "firse try karo".
        still_waiting = result == "RETRY"
        # No "Move on?" line here any more. The orchestrator can't see whether
        # this is the last sentence, so it would offer to advance past the end
        # — the student says yes, the session completes, and the tutor is still
        # mid-offer. Advancement is the Next button / an explicit move_on, both
        # of which the client now guards once the session is over.
        yield {
            "type": "meta", "intent": "grammar", "move_on": False,
            "awaiting_grammar": still_waiting, "awaiting_translation": False,
        }
        return

    intent = detect_intent(user_text)

    if intent == "move_on":
        yield {"type": "token", "text": MOVE_ON_LINE}
        yield {
            "type": "meta", "intent": "move_on", "move_on": True,
            "awaiting_grammar": False, "awaiting_translation": False,
        }
        return

    if intent == "meaning":
        async for event in _stream_plain(_prompt_meaning(sentence), history, student):
            yield event
        yield {
            "type": "meta", "intent": "meaning", "move_on": False,
            "awaiting_grammar": False, "awaiting_translation": False,
        }
        return

    if intent == "translate":
        target = detect_target_language(user_text)
        async for event in _stream_plain(
            _prompt_translation_request(sentence, target), history, student
        ):
            yield event
        yield {
            "type": "meta", "intent": "translate", "move_on": False,
            "awaiting_grammar": False,
            "awaiting_translation": target,
        }
        return

    if level == "hard" and intent == "unknown":
        async for event in _stream_plain(_prompt_grammar(sentence), history, student):
            yield event
        yield {
            "type": "meta", "intent": "grammar", "move_on": False,
            "awaiting_grammar": True, "awaiting_translation": False,
        }
        return

    faq_hit = faq.retrieve(user_text)
    if faq_hit:
        print(f"[faq] {faq_hit['id']} (score {faq_hit['score']})")
        async for event in _stream_plain(
            _prompt_faq(sentence, user_text, faq_hit), history, student
        ):
            yield event
        yield {
            "type": "meta", "intent": "faq", "move_on": False,
            "awaiting_grammar": False, "awaiting_translation": False,
        }
        return

    async for event in _stream_plain(_prompt_unknown(sentence, user_text), history, student):
        yield event
    yield {
        "type": "meta", "intent": "unknown", "move_on": False,
        "awaiting_grammar": False, "awaiting_translation": False,
    }


async def process_turn(
    user_text: str,
    sentence: Sentence,
    level: str,
    history: list[dict],
    awaiting_grammar: bool = False,
    awaiting_translation: bool = False,
) -> dict:
    """Batch wrapper — kept so nothing else breaks."""
    parts: list[str] = []
    meta: dict = {}

    async for event in process_turn_stream(
        user_text=user_text,
        sentence=sentence,
        level=level,
        history=history,
        awaiting_grammar=awaiting_grammar,
        awaiting_translation=awaiting_translation,
    ):
        if event["type"] == "token":
            parts.append(event["text"])
        elif event["type"] == "meta":
            meta = event

    return {
        "response_text": "".join(parts).strip(),
        "intent": meta.get("intent", "unknown"),
        "move_on": meta.get("move_on", False),
        "awaiting_grammar": meta.get("awaiting_grammar", False),
        "awaiting_translation": meta.get("awaiting_translation", False),
        "change_level": meta.get("change_level"),
    }