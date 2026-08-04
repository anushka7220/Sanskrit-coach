"""
FAQ retrieval — the tutor answering questions about herself.

Students ask things that aren't about the Sanskrit sentence at all: how this
works, whether she can hear them properly, how to change level. Before this,
those fell into the generic fallback and got a plausible-sounding invented
answer, which is the worst outcome — the tutor confidently telling you the app
does something it doesn't.

WHY LEXICAL AND NOT EMBEDDINGS
------------------------------
Embedding the query would mean an extra network round trip in front of every
single turn, to pick between ten documents. This app spent a long time getting
time-to-first-audio from 17s down to ~500ms; spending 300-500ms of that to
choose from a list this short is a bad trade.

The same rule the intent router follows applies here: retrieval is free,
generation is what you pay for. If the FAQ ever grows past ~50 entries, or
starts needing paraphrase matching this can't do, that's the point to swap
`retrieve()` for a real vector search. Nothing outside this module needs to
change when you do — that's why the interface is one function.

MATCHING
--------
Scoring is keyword hits plus token overlap, normalised. Keywords carry most of
the weight because the useful signal in "level kaise badlun" is 'level' and
'badlun', not the shared stopwords. Hindi, English and romanised forms all sit
in the same keyword list, since STT output mixes scripts.
"""

import re
from typing import Optional

# Anything below this is treated as "not an FAQ question" and falls through to
# the normal fallback. Set deliberately high: answering a Sanskrit question
# with an FAQ entry is far more jarring than missing an FAQ.
MATCH_THRESHOLD = 0.34

_TOKEN_RE = re.compile(r"[\u0900-\u097FA-Za-z]+")

# Words that appear in almost every question and carry no signal.
_STOP = {
    "kya", "hai", "ho", "hain", "main", "me", "mein", "ka", "ki", "ke", "ko",
    "is", "the", "a", "an", "to", "do", "you", "i", "can", "what", "how",
    "क्या", "है", "हो", "हैं", "मैं", "में", "का", "की", "के", "को", "और",
}


def _tokens(text: str) -> set[str]:
    return {t for t in (w.lower() for w in _TOKEN_RE.findall(text)) if t not in _STOP}


# ── The FAQ ───────────────────────────────────────────────────────────────────
# `answer` is the ground truth, not the spoken reply. Vidya rewrites it in her
# own voice — a canned string read aloud sounds like a phone menu, and the
# answer still has to agree with the student's gender and name.
FAQ: list[dict] = [
    {
        "id": "who_are_you",
        "q": "तुम कौन हो? Who are you?",
        "keywords": ["कौन", "kaun", "who", "naam", "नाम", "name", "tum", "तुम",
                     "yourself", "परिचय"],
        "answer": "I am Vidya, an AI Sanskrit tutor. I listen to you read "
                  "Sanskrit aloud, explain meanings, check your translations "
                  "and ask grammar questions.",
    },
    {
        "id": "how_it_works",
        "q": "यह कैसे काम करता है? How does this work?",
        "keywords": ["काम", "kaam", "work", "works", "kaise", "कैसे", "how",
                     "process", "तरीका", "use", "इस्तेमाल"],
        "answer": "You read the Sanskrit sentence aloud. I hear you, tell you "
                  "what it means, and can check your Hindi or English "
                  "translation. When you're ready we move to the next sentence.",
    },
    {
        "id": "pronunciation",
        "q": "क्या तुम मेरा उच्चारण ठीक कर सकती हो? Can you correct my pronunciation?",
        "keywords": ["उच्चारण", "uchcharan", "pronunciation", "pronounce",
                     "बोलना", "galat", "गलत", "sahi", "सही", "correct"],
        "answer": "Yes. Read the sentence aloud and I'll tell you what I "
                  "heard, and where it differed from the correct reading.",
    },
    {
        "id": "change_level",
        "q": "लेवल कैसे बदलूँ? How do I change the level?",
        "keywords": ["लेवल", "level", "badal", "बदल", "change", "easy", "hard",
                     "आसान", "कठिन", "मुश्किल", "difficulty"],
        "answer": "Just say it out loud — 'level change karo hard' or 'easy "
                  "level pe le chalo'. There are three levels: easy, "
                  "intermediate and hard.",
    },
    {
        "id": "levels_meaning",
        "q": "तीनों लेवल में क्या फर्क है? What's the difference between levels?",
        "keywords": ["फर्क", "fark", "difference", "levels", "level", "लेवल",
                     "intermediate", "beginner", "advanced", "अंतर",
                     "teeno", "तीनों", "three"],
        "answer": "Easy is reading and meaning. Intermediate adds translation "
                  "checking. Hard adds grammar questions on top of that.",
    },
    {
        "id": "interrupt",
        "q": "क्या मैं तुम्हें बीच में रोक सकती हूँ? Can I interrupt you?",
        "keywords": ["रोक", "rok", "interrupt", "बीच", "beech", "stop",
                     "चुप", "chup", "wait", "रुको"],
        "answer": "Yes, just start speaking and I'll stop. You don't need to "
                  "press anything — there's no stop button, I'm always "
                  "listening.",
    },
    {
        "id": "language",
        "q": "क्या मैं हिंदी में बोल सकती हूँ? Can I speak in Hindi?",
        "keywords": ["हिंदी", "hindi", "english", "अंग्रेजी", "भाषा", "bhasha",
                     "language", "bol", "बोल", "speak"],
        "answer": "Yes. Speak in Hindi, English or a mix of both — I "
                  "understand all three, and I'll reply the same way.",
    },
    {
        "id": "wrong_answer",
        "q": "अगर मैं गलत बोलूँ तो? What if I get it wrong?",
        "keywords": ["गलत", "galat", "wrong", "mistake", "गलती", "galti",
                     "fail", "error", "incorrect", "bolun", "boloon", "bolu",
                     "बोलूँ", "बोलूं"],
        "answer": "Nothing bad happens. I'll tell you gently what was off and "
                  "let you try again as many times as you like.",
    },
    {
        "id": "progress_saved",
        "q": "क्या मेरी प्रोग्रेस सेव होती है? Is my progress saved?",
        "keywords": ["प्रोग्रेस", "progress", "save", "सेव", "यादगार", "yaad",
                     "remember", "history", "record"],
        "answer": "Your name is remembered on this device, but your lesson "
                  "progress isn't saved yet — if you reload, the session "
                  "starts fresh.",
    },
    {
        "id": "how_long",
        "q": "संस्कृत सीखने में कितना समय लगेगा? How long will it take to learn Sanskrit?",
        "keywords": ["समय", "samay", "time", "कितना", "kitna", "long", "days",
                     "महीने", "सीखने", "seekhne", "learn", "duration"],
        "answer": "That depends on you. Reading Devanagari aloud comfortably "
                  "takes a few weeks of regular practice; grammar takes "
                  "longer. Short daily sessions beat long rare ones.",
    },
]

# Precompute so retrieval is pure set arithmetic at request time.
for _e in FAQ:
    _e["_q_tokens"] = _tokens(_e["q"])
    _e["_kw"] = {k.lower() for k in _e["keywords"]}


def retrieve(text: str) -> Optional[dict]:
    """Best-matching FAQ entry, or None if nothing is close enough.

    Returning None is the important half: it's what keeps a Sanskrit question
    from being answered out of the FAQ.
    """
    q = _tokens(text)
    if not q:
        return None

    best, best_score = None, 0.0
    for entry in FAQ:
        # Keyword hits dominate — they're the words that actually distinguish
        # one question from another.
        kw_hits = len(q & entry["_kw"])
        overlap = len(q & entry["_q_tokens"])

        score = (kw_hits * 1.0 + overlap * 0.35) / max(3, len(q))
        if score > best_score:
            best, best_score = entry, score

    if best_score < MATCH_THRESHOLD:
        return None

    return {**best, "score": round(best_score, 3)}