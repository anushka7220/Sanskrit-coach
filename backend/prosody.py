"""
Prosody shaping — the layer between what the LLM wrote and what the voice says.

WHY THIS EXISTS
---------------
bulbul has no SSML. No <break>, no <emphasis>, no pitch contour. Everything a
listener hears as intonation comes from two things:

  1. The punctuation in the text.
  2. The `pace` set on the socket.

That's the whole instrument. So "she reads questions as statements", "she
doesn't pause enough" and "her tone doesn't match the sentence" are not three
model problems — they're three text problems and one config problem, and all
four are fixable here, deterministically, with no extra latency.

THE THREE FIXES

1. QUESTIONS THAT SOUND LIKE QUESTIONS
   Hindi questions are routinely written with a danda: "क्या आप तैयार हैं।"
   TTS reads a danda as a full stop, so the pitch falls and it lands as a
   statement. The interrogative is marked by the question WORD, not the
   punctuation — so we detect the word and fix the punctuation.

2. PAUSES
   A comma is the only pause instruction available. The model writes long
   clauses without them, so we insert them where a teacher would breathe:
   after an opening discourse marker, and around a quoted Sanskrit phrase.

3. TONE PER SENTENCE
   `pace` can be changed mid-turn — the SDK docstring is explicit that a new
   config message can be sent at any time and flushes the buffer first. Nobody
   was using this. A correction wants to be slower than praise; a Sanskrit
   quote wants to be slower than either. Same voice, different delivery.
"""

import re

# ── Pace per kind of sentence ─────────────────────────────────────────────────
# Deliberately a narrow band. Below ~0.8 she sounds sedated; above ~1.05 she
# sounds like she's rushing you out of the room. The differences want to be
# felt, not noticed.
PACE = {
    "default":    0.92,
    "question":   0.95,   # slightly brisker; the rise carries the intent
    "correction": 0.86,   # slow. being told you're wrong needs room
    "praise":     1.00,   # brisk and light
    "sanskrit":   0.80,   # slowest. every syllable has to be hearable
}

# Question words. Presence of any of these makes a sentence interrogative
# regardless of how it was punctuated.
_Q_WORDS = [
    "क्या", "कौन", "कैसे", "कैसा", "कैसी", "क्यों", "कब", "कहाँ", "कहां",
    "कितना", "कितनी", "कितने", "किसने", "किसका", "बताइए क्या",
]
_Q_TAIL = ["ना", "ठीक है", "है ना", "हैं ना", "चलें", "करें", "बताऊँ", "बताऊं"]

_CORRECTION_MARKERS = [
    "नहीं", "गलत", "फिर से", "दोबारा", "ध्यान", "not quite", "try again",
]
_PRAISE_MARKERS = [
    "बहुत बढ़िया", "शाबाश", "बिल्कुल सही", "सही है", "अच्छा किया", "excellent",
]

# Openers a teacher pauses after. Written without the comma because the model
# often omits it — that's the whole point.
_OPENERS = ["तो", "अच्छा", "देखिए", "हाँ तो", "ठीक है", "सुनिए", "अब"]

_DEVANAGARI = re.compile(r"[\u0900-\u097F]")


def _has(text: str, needles: list[str]) -> bool:
    return any(n in text for n in needles)


def is_question(text: str) -> bool:
    """True if this should be spoken with a rising contour.

    Note it does NOT rely on a '?' already being present — that's the bug it
    exists to fix.
    """
    t = text.strip()
    if t.endswith("?"):
        return True
    if _has(t, _Q_WORDS):
        return True
    # Tag questions: "...चलें।", "...है ना।"
    #
    # The tail alone is not one. "ठीक है।" is a statement meaning "fine";
    # "आपको समझ आया, ठीक है।" is a question. So the tail has to be attached to
    # something, not be the whole sentence.
    stripped = t.rstrip("।.!? ")
    for tail in _Q_TAIL:
        if stripped.endswith(tail) and len(stripped) > len(tail) + 2:
            return True
    return False


def shape(text: str) -> tuple[str, float]:
    """Return (text to speak, pace to speak it at).

    Pure and cheap — string work on one chunk. It runs between the sentence
    chunker and the socket, so it costs nothing measurable.
    """
    t = text.strip()
    if not t:
        return t, PACE["default"]

    # ── 1. Question punctuation ──────────────────────────────────────────
    if is_question(t):
        # A danda or full stop at the end of a question flattens the contour.
        t = re.sub(r"[।\.]+\s*$", "?", t)
        if not t.endswith("?"):
            t += "?"

    # ── 2. Pauses ────────────────────────────────────────────────────────
    # A quoted Sanskrit phrase needs air on both sides, or it runs into the
    # Hindi around it and stops being hearable as a separate thing.
    has_quote = "'" in t
    t = re.sub(r"\s*'([^']+)'\s*", r", '\1', ", t)

    # Comma after an opening marker, if the model didn't write one — but not
    # when a quote is already going to add pauses a few words later. Three
    # pauses in the first four words reads as hesitant, not deliberate.
    if not has_quote:
        for op in _OPENERS:
            if t.startswith(op) and not t[len(op):].lstrip().startswith(","):
                rest = t[len(op):].lstrip()
                if rest:
                    t = f"{op}, {rest}"
                break

    t = re.sub(r"\s*,\s*,\s*", ", ", t)       # collapse doubles we just made
    t = re.sub(r",\s*([।\.\?!])", r"\1", t)   # no comma right before an end mark
    t = re.sub(r"^\s*,\s*", "", t)            # never open on a comma
    t = re.sub(r"\s+", " ", t).strip()

    # ── 3. Pace ──────────────────────────────────────────────────────────
    if _has(t, _PRAISE_MARKERS):
        pace = PACE["praise"]
    elif _has(t, _CORRECTION_MARKERS):
        pace = PACE["correction"]
    elif is_question(t):
        pace = PACE["question"]
    else:
        pace = PACE["default"]

    # NOTE: there was a script-ratio check here that slowed any mostly-
    # Devanagari chunk to `sanskrit` pace, on the theory that it was the
    # Sanskrit line being read out. It was wrong: Hindi is Devanagari too, so
    # it fired on almost every sentence and flattened all four paces into one.
    #
    # Script can't separate Hindi from Sanskrit. If you want the Sanskrit line
    # itself read slowly, pass PACE["sanskrit"] explicitly from the caller that
    # already knows it's announcing a sentence — don't try to infer it here.

    return t, pace