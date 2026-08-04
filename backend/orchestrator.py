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

Two notes on why this was easier than expected:

1. Every branch that sets move_on=True uses a hardcoded string and never calls
   the LLM. So move_on is fully resolved before generation starts — it can be
   emitted immediately and never needs to be inferred from streamed text.

2. The RESULT:CORRECT/RETRY marker is instructed to come first, which is the
   best possible place for it. _stream_checked() holds back the head of the
   stream until the marker resolves (typically ~15 characters), strips it, and
   releases everything after it. The old lenient search-anywhere parse can't
   work on a stream, so the prompts below now insist the marker leads.

The handle_* coroutines are gone — they only ever built a prompt and called
llm.chat. They're now _prompt_* functions, and the LLM call happens once, in
one place.
"""

import re
from typing import AsyncIterator

from data.sentences import Sentence
import llm
import faq
import safety

# ── intent detection (unchanged) ──────────────────────────────────────────────
MEANING_KEYWORDS = [
    "meaning", "matlab", "arth", "samjhao", "samjha", "explain",
    "what does", "what is", "मतलब", "अर्थ", "समझाओ", "समझा", "मीनिंग", "एक्सप्लेन",
]

TRANSLATION_KEYWORDS = [
    # "sahi hai" / "सही है" used to be here and had to go: it just means
    # "is it right", which is equally a question about pronunciation, grammar
    # or the app itself. It was routing "mera uchcharan sahi hai kya" to the
    # translation branch. A student who is mid-translation is already caught
    # by the awaiting_translation loop, so nothing is lost by removing it.
    "translate", "translation", "anuvad", "check",
    "अनुवाद", "ट्रांसलेशन", "ट्रांसलेट", "चेक",
]

MOVE_ON_KEYWORDS = [
    # Bare "chalo" / "चलो" / "aage" / "आगे" were here and had to go. They are
    # ordinary Hindi filler — "अच्छा चलो, इसका मतलब बताओ" is a request for the
    # meaning, not to skip the sentence. So is "समझ गई", which means "I
    # understood", not "move me on".
    #
    # Everything here has to be unambiguous on its own.
    "move on", "next sentence", "next vaakya", "agla vaakya", "agle vaakya",
    "aage badho", "aage badhte", "aage chalo", "aage chaliye",
    "नेक्स्ट", "अगला वाक्य", "अगले वाक्य", "आगे बढ़", "आगे चलो", "आगे चलिए",
    "next karo", "अगला करो",
]

# Explicit advance words that escape any waiting loop. Kept to unambiguous
# tokens so a translation attempt can't accidentally trigger it. Bare "next"
# was removed: "next sentence ka meaning batao" was escaping the loop instead
# of being answered.
ESCAPE_MOVE_ON = [
    "move on", "next sentence", "नेक्स्ट", "अगला वाक्य", "अगले वाक्य",
    "आगे बढ़", "agla vaakya", "agle vaakya", "aage badho",
]

MOVE_ON_LINE = "बहुत अच्छे! अगला वाक्य शुरू करते हैं। (Great! Let's start the next sentence.)"

# ── Level switching by voice ──────────────────────────────────────────────────
# Detected with keywords, not an LLM call. Routing has to stay free: adding a
# classification round trip here would put ~1.3s in front of every single turn
# just to catch the rare one that asks to switch level.
LEVEL_WORDS = {
    "easy": ["easy", "आसान", "सरल", "beginner", "बिगिनर", "इजी", "सिंपल", "simple",
             "aasan", "asaan", "saral"],
    "intermediate": ["intermediate", "medium", "मध्यम", "इंटरमीडिएट", "मीडियम",
                     "madhyam"],
    "hard": ["hard", "difficult", "advanced", "कठिन", "मुश्किल", "हार्ड", "एडवांस",
             "kathin", "mushkil"],
}

# A level word ALONE is not enough — "yes this is hard" is a comment about the
# sentence, not a request. Requiring an intent word too costs the occasional
# missed switch, which is far better than silently wiping a student's progress
# because they said something was difficult.
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
    """Return the requested level, or None.

    Returns None when the request names the level the student is already on —
    resetting their progress to announce "you're already on hard" would be a
    strange thing to do.
    """
    t = text.lower()
    if not _has_keyword(t, LEVEL_CHANGE_INTENT):
        return None
    for level, words in LEVEL_WORDS.items():
        if _has_keyword(t, words):
            return None if level == current_level else level
    return None


def _has_keyword(text: str, keywords: list[str]) -> bool:
    """Whole-token match so short tokens like 'ok' don't match inside 'shlok'."""
    for k in keywords:
        if re.search(rf"(?<!\w){re.escape(k)}(?!\w)", text):
            return True
    return False


def detect_intent(text: str) -> str:
    """Returns one of: 'meaning' | 'translate' | 'move_on' | 'unknown'.

    Order matters and move_on goes LAST. It used to be first, so any sentence
    that happened to contain an advance word skipped ahead — a student asking
    "चलो इसका मतलब बताओ" got moved to the next sentence instead of an answer.
    A request for something specific always beats a request to move on.
    """
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
    """Which language does the student want to translate INTO?

    The intent detector only answers "is this a translation request", which
    threw away the half of the sentence that mattered. Saying "I want to check
    the Hindi translation" matched on 'check' and then got answered with a
    hardcoded request for an English one.

    Defaults to English because that's the original flow and every sentence
    has translation_en; Hindi falls back to meaning_hi.
    """
    t = text.lower()
    if _has_keyword(t, HINDI_TARGET_KEYWORDS):
        return "hi"
    if _has_keyword(t, ENGLISH_TARGET_KEYWORDS):
        return "en"
    return "en"


def _reference_translation(sentence: Sentence, lang: str) -> str:
    """The correct answer to grade against, per target language."""
    if lang == "hi":
        # There's no translation_hi field; meaning_hi is the Hindi rendering.
        return sentence.get("meaning_hi") or sentence["translation_en"]
    return sentence["translation_en"]


# ── prompts ───────────────────────────────────────────────────────────────────

def _prompt_meaning(sentence: Sentence) -> str:
    return (
        f"The student is reading this Sanskrit sentence: '{sentence['sanskrit']}'\n"
        f"They want to know the meaning. "
        f"Hindi meaning: {sentence['meaning_hi']}. "
        f"English meaning: {sentence['meaning_en']}. "
        f"Explain it simply and encouragingly in 2-3 sentences."
    )


def _prompt_translation_request(sentence: Sentence, lang: str = "en") -> str:
    """User asked to CHECK a translation but hasn't given one yet — invite it."""
    lang_name = "Hindi" if lang == "hi" else "English"
    return (
        f"The student wants to translate this Sanskrit sentence: '{sentence['sanskrit']}'.\n"
        f"They have NOT given their translation yet — they only asked to check one.\n"
        f"They asked specifically about the {lang_name} translation, so invite a "
        f"{lang_name} one — do not ask for any other language.\n"
        f"Warmly invite them, in 1-2 short sentences, to say their {lang_name} "
        f"translation now. Do NOT translate it for them."
    )


def _prompt_translation_check(sentence: Sentence, user_translation: str,
                              lang: str = "en") -> str:
    lang_name = "Hindi" if lang == "hi" else "English"
    return (
        f"Sanskrit sentence: '{sentence['sanskrit']}'\n"
        f"Correct {lang_name} translation: '{_reference_translation(sentence, lang)}'\n"
        f"Student's translation: '{user_translation}'\n\n"
        f"Is the student's translation correct or close enough?\n"
        f"Begin your reply with EXACTLY one of these as the very first "
        f"characters — nothing before it, not even a greeting:\n"
        f"  RESULT:CORRECT  — their translation is right or close enough\n"
        f"  RESULT:RETRY    — they attempted a translation but it's wrong\n"
        f"  RESULT:OFFTOPIC — they did NOT attempt a translation at all "
        f"(they asked you something, made a comment, requested something, "
        f"or changed the subject)\n"
        f"Then, on the next line, your warm spoken feedback.\n"
        f"For OFFTOPIC: actually respond to what they said in 1-2 short "
        f"sentences — do not grade it, do not pretend it was a translation — "
        f"then lightly invite them to try the {lang_name} translation. "
        f"If they asked for something you can't do as a voice tutor, say so "
        f"warmly and move on. Never recite song lyrics.\n"
        f"For RETRY: gently say what's off and ask them to try again."
    )


def _prompt_grammar(sentence: Sentence) -> str:
    return (
        f"Sanskrit sentence: '{sentence['sanskrit']}'\n"
        f"Grammar question to ask: '{sentence['grammar_note']}'\n"
        f"Ask the student this grammar question in a friendly way. Keep it short."
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
        f"  RESULT:OFFTOPIC — they did NOT answer the question at all "
        f"(they asked you something, commented, or changed the subject)\n"
        f"Then, on the next line, your warm spoken feedback.\n"
        f"For OFFTOPIC: respond to what they actually said in 1-2 short "
        f"sentences, then lightly re-ask the grammar question. Never recite "
        f"song lyrics."
    )


def _prompt_faq(sentence: Sentence, user_text: str, entry: dict) -> str:
    """Answer a question about the tutor itself, grounded in a retrieved fact.

    The grounding is the point. Without it the fallback would happily invent
    an answer — telling a student their progress is saved when it isn't, or
    describing a feature that doesn't exist. A confidently wrong tutor is
    worse than one that says it doesn't know.
    """
    return (
        f"You are Vidya, a warm Sanskrit tutor speaking aloud.\n"
        f"The student asked: '{user_text}'\n\n"
        f"Here is the correct information:\n"
        f"{entry['answer']}\n\n"
        f"Say this back to them in your own voice, in 1-2 SHORT sentences, "
        f"in Hindi with English words where natural.\n"
        f"- Do NOT add anything the information above doesn't say. If they "
        f"asked something it doesn't cover, answer only the part it does and "
        f"say plainly that you're not sure about the rest.\n"
        f"- Do not read it out like a manual. This is a conversation.\n"
        f"- Then, in the same breath, offer to get back to the sentence "
        f"'{sentence['sanskrit']}'."
    )


def _prompt_unknown(sentence: Sentence, user_text: str = "") -> str:
    """Anything that isn't meaning / translate / move_on.

    This used to ignore user_text entirely and just re-read the menu, so
    "Vidya gaana suna do" got answered with "would you like the meaning or a
    translation check?". The student asked a real question and got a form.

    Answering costs no extra latency: this branch already makes exactly one
    LLM call, and it's the same call either way. Only the prompt changes.
    """
    return (
        f"You are Vidya, a warm Sanskrit tutor. The student is currently "
        f"studying: '{sentence['sanskrit']}'\n"
        f"The student said: '{user_text}'\n\n"
        f"This isn't a request about the sentence's meaning, a translation "
        f"check, or moving on. Respond naturally and warmly to what they "
        f"actually said, in 1-2 SHORT sentences, then gently offer to "
        f"continue with the sentence.\n"
        f"- If it's small talk or a personal question, answer it briefly and "
        f"stay in character as their tutor.\n"
        f"- If they ask for something you can't do as a voice tutor (sing a "
        f"song, play music), say so lightly and without apologising twice. "
        f"Never recite song lyrics.\n"
        f"- If they ask a general knowledge question, answer it in one "
        f"sentence.\n"
        f"- If you genuinely can't tell what they meant, THEN ask what "
        f"they'd like.\n"
        f"Keep it short — every extra sentence is extra seconds of speech."
    )


# ── streaming helpers ─────────────────────────────────────────────────────────

_RESULT_RE = re.compile(
    r"RESULT\s*:?\s*(CORRECT|RETRY|INCORRECT|WRONG|OFFTOPIC)", re.IGNORECASE
)


def _normalise_result(raw: str) -> str:
    """Collapse the model's marker into one of three states.

    OFFTOPIC is what breaks the sticky-loop bug. Once awaiting_translation was
    set, this branch ran ahead of intent detection, so ANY utterance got graded
    as a translation attempt — "तुम इसको हँसकर बता सकती हो" included. No keyword
    list can fix that, because the set of things a student might say instead of
    answering is unbounded.

    The model already understands the difference, and we're already paying for
    the call, so we ask it as part of the same request. Costs nothing extra.
    """
    r = raw.upper()
    if r == "CORRECT":
        return "CORRECT"
    if r == "OFFTOPIC":
        return "OFFTOPIC"
    return "RETRY"

# How far into the stream we'll wait for the marker before giving up on it.
# The prompt demands it lead, so this normally resolves inside ~15 chars.
# Every character here is added directly to time-to-first-audio, so keep it tight.
_MARKER_SCAN_CHARS = 48


def _strip_marker(text: str) -> str:
    return _RESULT_RE.sub("", text, count=1).strip().strip("-").strip()


async def _stream_checked(prompt: str, history: list[dict],
                          student: dict | None = None):
    """Stream a RESULT-marked reply.

    Yields ("token", str) for speakable text, then exactly one
    ("result", "CORRECT" | "RETRY" | "OFFTOPIC") once the marker is resolved.

    Holds back the head of the stream until the marker is found so the marker
    text itself is never spoken — the old approach of regexing the finished
    string obviously can't work here.
    """
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
                # Model ignored the instruction. Treat as RETRY (the safe
                # default — ESCAPE_MOVE_ON still lets the student break out)
                # and release the buffered text so audio isn't stuck.
                resolved = "RETRY"
                yield ("result", resolved)
                yield ("token", head)
            continue
        yield ("token", token)

    if resolved is None:
        # Whole reply was shorter than the scan window.
        match = _RESULT_RE.search(head)
        resolved = _normalise_result(match.group(1)) if match else "RETRY"
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

    # An explicit "next"/"move on" always escapes a waiting loop, so a stuck
    # RESULT parse can never soft-lock the session by voice. No LLM call —
    # audio for this turn starts as fast as TTS can synthesize.
    # Level changes are checked FIRST — before the awaiting_* loops — so a
    # student stuck mid-translation can still switch out by voice. Hardcoded
    # reply, no LLM call, so audio starts as fast as TTS can synthesize.
    # ── SAFETY: checked before everything else ────────────────────────────
    # Above level changes, above the escape word, above the awaiting_* loops.
    # If a student in the middle of a translation says something unsafe, that
    # loop must not get to answer first — and it would, since it runs ahead of
    # intent detection.
    #
    # No LLM call. The reply is a fixed string, so what the student hears is
    # auditable and identical every time. A model improvising here could give
    # medical advice it isn't qualified to give.
    unsafe = safety.detect(user_text)
    print(f"[safety] check: {unsafe or 'clear'} | {user_text[:80]}")
    if unsafe:
        # Meta FIRST, before the text. ws.py needs to know this is a safety
        # turn before a single audio chunk goes out, so it can mute the mic —
        # the alert beep plays out of the same speakers the mic is listening
        # to, and without this it trips VAD and barges Vidya out of her own
        # safety message. That happened: she cut off at "मदद नहीं कर सकती".
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

    # ── mid-loop: waiting for a translation attempt ────────────────────────
    if awaiting_translation:
        # awaiting_translation is "hi" or "en" when it came from the translate
        # branch, but may be a plain True from older state or a manual call.
        target = awaiting_translation if isinstance(awaiting_translation, str) else "en"
        result = "RETRY"
        async for kind, value in _stream_checked(
            _prompt_translation_check(sentence, user_text, target), history, student
        ):
            if kind == "result":
                result = value
            else:
                yield {"type": "token", "text": value}
        yield {
            "type": "meta", "intent": "translate", "move_on": False,
            "awaiting_grammar": False,
            # OFFTOPIC keeps the loop open in the same language: the student
            # asked something else, they didn't fail and they didn't finish.
            "awaiting_translation": False if result == "CORRECT" else target,
        }
        return

    # ── mid-loop: waiting for a grammar answer ────────────────────────────
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
        if correct and level == "hard":
            yield {"type": "token", "text": " अब अगले वाक्य पर चलते हैं? (Move on?)"}
        yield {
            "type": "meta", "intent": "grammar", "move_on": False,
            "awaiting_grammar": not correct, "awaiting_translation": False,
        }
        return

    intent = detect_intent(user_text)

    # ── keyword move-on (also LLM-free) ───────────────────────────────────
    if intent == "move_on":
        yield {"type": "token", "text": MOVE_ON_LINE}
        yield {
            "type": "meta", "intent": "move_on", "move_on": True,
            "awaiting_grammar": False, "awaiting_translation": False,
        }
        return

    # ── meaning ───────────────────────────────────────────────────────────
    if intent == "meaning":
        async for event in _stream_plain(_prompt_meaning(sentence), history, student):
            yield event
        yield {
            "type": "meta", "intent": "meaning", "move_on": False,
            "awaiting_grammar": False, "awaiting_translation": False,
        }
        return

    # ── translate: FIRST turn is a request to begin, NOT the translation ──
    if intent == "translate":
        target = detect_target_language(user_text)
        async for event in _stream_plain(
            _prompt_translation_request(sentence, target), history, student
        ):
            yield event
        yield {
            "type": "meta", "intent": "translate", "move_on": False,
            "awaiting_grammar": False,
            # Carries the language, not just "yes". Both "hi" and "en" are
            # truthy, so every existing `if awaiting_translation:` check keeps
            # working and ws.py needs no change.
            "awaiting_translation": target,
        }
        return

    # ── hard level: unclear utterance after reading → grammar question ────
    if level == "hard" and intent == "unknown":
        async for event in _stream_plain(_prompt_grammar(sentence), history, student):
            yield event
        yield {
            "type": "meta", "intent": "grammar", "move_on": False,
            "awaiting_grammar": True, "awaiting_translation": False,
        }
        return

    # ── fallback ──────────────────────────────────────────────────────────
    # FAQ is checked HERE and not earlier on purpose. By this point
    # detect_intent has already claimed anything about meaning, translation or
    # moving on, so a lesson question can never be answered out of the FAQ —
    # "इसका हिंदी अनुवाद क्या है" is routed as `translate` long before this.
    #
    # Retrieval costs nothing: it's set arithmetic over ten entries. The reply
    # is the same single LLM call the fallback was already making.
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
    """Batch wrapper. Same return shape as before — kept so nothing else
    breaks, but ws.py should use process_turn_stream()."""
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
        # Dropping this here would make level switching silently stop working
        # for any caller on the batch path.
        "change_level": meta.get("change_level"),
    }