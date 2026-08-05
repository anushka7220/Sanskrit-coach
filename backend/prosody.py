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
   after an opening discourse marker, around a quoted Sanskrit phrase, and
   after mid-sentence setup clauses like यानी / मतलब / इसलिए.

3. TONE PER SENTENCE
   `pace` can be changed mid-turn — the SDK docstring is explicit that a new
   config message can be sent at any time and flushes the buffer first. Nobody
   was using this. A correction wants to be slower than praise; a Sanskrit
   quote wants to be slower than either. Same voice, different delivery.
"""

import re

# ── Voice modulation per kind of sentence ─────────────────────────────────────
# (speed, stability, style).
#
# Tuned for a calm, grounded friend — not a radio presenter.
#
#   question   : slightly quicker, stability pulled up so pitch moves but
#                doesn't wobble; style kept modest
#   correction : slowest, highest stability — steady and kind, not dramatic
#   praise     : pulled way back — warm but flat, never gushing or excited
#                style 0.18 (was 0.38) is the key change; kills the exclamation
#                energy that made her sound like a game show host
#   calm       : scripted lines (greeting, transition, announcements) —
#                high stability, low style so they never sound theatrical
VOICE = {
    "default":    (0.90, 0.55, 0.20),
    "question":   (0.93, 0.50, 0.35),
    "correction": (0.84, 0.72, 0.10),
    "praise":     (0.90, 0.58, 0.18),
    "calm":       (0.88, 0.68, 0.12),
}

# ── Fillers ───────────────────────────────────────────────────────────────────
# Disabled — LLM prompt now controls voice character. Prosody fillers caused
# double openers and clashed with the "no filler at sentence start" rule.
_FILLERS_BY_KIND = {}
_THINKING_FILLER = "हम्म, "   # kept for reference, not used

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

# Praise markers — kept minimal. "सही है" removed because it matched too many
# normal sentences and caused over-excited delivery on plain statements.
_PRAISE_MARKERS = [
    "बिल्कुल सही", "अच्छा किया", "excellent",
]

# Openers a teacher pauses after (sentence-initial).
_OPENERS = ["तो", "अच्छा", "देखिए", "हाँ तो", "ठीक है", "सुनिए", "अब"]

# Mid-sentence setup clauses that deserve a breath after them.
# e.g. "राम वन जाते हैं यानी वे घर छोड़ देते हैं।"
#   → "राम वन जाते हैं यानी, वे घर छोड़ देते हैं।"
_MID_PAUSE_MARKERS = ["जैसे कि", "यानी", "मतलब", "इसलिए", "क्योंकि"]

_DEVANAGARI = re.compile(r"[\u0900-\u097F]")


def _has(text: str, needles: list[str]) -> bool:
    return any(n in text for n in needles)


def is_question(text: str) -> bool:
    """True if this should be spoken with a rising contour.

    Does NOT rely on a '?' already being present — that's the bug it
    exists to fix.
    """
    t = text.strip()
    if t.endswith("?"):
        return True
    if _has(t, _Q_WORDS):
        return True
    # Tag questions: "...चलें।", "...है ना।"
    # The tail alone is not enough — "ठीक है।" is a statement; it only
    # becomes interrogative when attached to something longer.
    stripped = t.rstrip("।.!? ")
    for tail in _Q_TAIL:
        if stripped.endswith(tail) and len(stripped) > len(tail) + 2:
            return True
    return False


def classify(text: str) -> str:
    """One of: praise | correction | question | default."""
    if _has(text, _PRAISE_MARKERS):
        return "praise"
    if _has(text, _CORRECTION_MARKERS):
        return "correction"
    if is_question(text):
        return "question"
    return "default"


def shape(text: str, is_first_chunk: bool = False,
          force_kind: str | None = None) -> tuple[str, tuple[float, float, float]]:
    """Return (text to speak, (speed, stability, style)).

    `force_kind` overrides classification — used for scripted lines (greeting,
    transitions) that should always be delivered calm, regardless of whether
    they happen to contain a question mark or a praise word.
    """
    t = text.strip()
    if not t:
        return t, VOICE["default"]

    kind = force_kind or classify(t)

    # ── 0. Pre-pause before corrections ──────────────────────────────────
    # A real teacher pauses before "नहीं" — the silence signals something's
    # coming. A leading comma gives TTS that small breath.
    if kind == "correction" and not t.startswith(","):
        t = ", " + t

    # ── 1. Question punctuation ──────────────────────────────────────────
    if is_question(t):
        t = re.sub(r"[।\.]+\s*$", "?", t)
        if not t.endswith("?"):
            t += "?"

    # ── 2. Pauses ────────────────────────────────────────────────────────

    # 2a. Sanskrit / quoted phrases — space before, comma after
    has_quote = "'" in t
    t = re.sub(r"\s*'([^']+)'\s*", r" '\1', ", t)

    # 2b. Sentence-initial openers
    if not has_quote:
        for op in _OPENERS:
            if t.startswith(op) and not t[len(op):].lstrip().startswith(","):
                rest = t[len(op):].lstrip()
                if rest:
                    t = f"{op}, {rest}"
                break

    # 2c. Mid-sentence setup clauses (यानी, मतलब, इसलिए, …)
    for marker in _MID_PAUSE_MARKERS:
        # Add comma after marker only when one isn't already there
        t = re.sub(rf"({re.escape(marker)})\s+(?!,)", rf"\1, ", t)

    # 2d. Cleanup: collapse double commas, strip comma before terminal punct
    t = re.sub(r"\s*,\s*,\s*", ", ", t)
    t = re.sub(r",\s*([।\.\?!])", r"\1", t)
    t = re.sub(r"^\s*,\s*", "", t)       # strip leading comma left by cleanup
    t = re.sub(r"\s+", " ", t).strip()

    # ── 3. Filler ────────────────────────────────────────────────────────
    # Disabled — LLM prompt handles voice character. Prosody fillers caused
    # double openers and clashed with the no-filler-at-sentence-start rule.
    # if is_first_chunk and kind in _FILLERS_BY_KIND:
    #     ...

    return t, VOICE[kind]