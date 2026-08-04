"""
Unsafe-conversation detection, safe responses, and internal alerting.

WHAT THIS IS FOR
----------------
A Sanskrit tutor is not a counsellor, a doctor, or a crisis service, and it
must not behave like one. But students say things to a patient voice that they
don't say to people, and some of those things need a human, quickly.

So this module does three narrow jobs:
  1. Notice when a conversation has moved somewhere unsafe.
  2. Say something warm, short, and NOT advice, that points at a real person.
  3. Tell the team.

DESIGN DECISION: THE REPLY IS NOT GENERATED
-------------------------------------------
Everywhere else in this app the LLM writes the reply. Here it doesn't, and
that's deliberate.

A model improvising around self-harm or a medical emergency can produce
advice it isn't qualified to give, invent a resource, or say something that
lands badly — and you'd have no way to know in advance what a student heard.
Fixed strings are auditable: you can read exactly what gets said, and it's the
same every time. Predictability beats fluency in the one place where being
wrong is expensive.

It also removes latency and any chance of a provider failure at the worst
possible moment.

WHAT THIS IS NOT
----------------
Keyword detection has false negatives, and STT mistakes make that worse. This
is a floor, not a ceiling — it catches plain statements, not indirect ones.
Treat every alert as real; do not treat silence as safety.
"""

import asyncio
import platform
import re
import shutil
import subprocess
import threading
import time
from typing import Optional

# ── Normalisation ─────────────────────────────────────────────────────────────
# Devanagari writes the same sound several ways, and STT picks whichever it
# likes: मर जाऊँ / मर जाऊं / मर जाउ are one phrase and three strings. Writing a
# pattern per variant is a losing game — the next variant is always the one
# that gets missed, and a missed match here matters.
#
# So both the input AND the patterns get flattened first: nasal marks dropped,
# long vowels folded onto short. That is lossy for reading and exactly right
# for matching.
_NASALS = dict.fromkeys(map(ord, "\u0901\u0902\u093C"), None)   # ँ ं ़
_VOWEL_FOLD = str.maketrans({
    "ऊ": "उ", "ई": "इ", "आ": "अ", "ऐ": "ए", "औ": "ओ", "ऋ": "र",
    "ू": "ु", "ी": "ि", "ै": "े", "ौ": "ो", "ा": "",
})


def _norm(text: str) -> str:
    return text.translate(_NASALS).translate(_VOWEL_FOLD).lower()

# ── Categories ────────────────────────────────────────────────────────────────
# Ordered by how urgently a human is needed. detect() returns the first match
# in this order, so a message containing several signals resolves to the most
# serious one rather than whichever pattern happened to be listed first.

_PATTERNS: list[tuple[str, list[str]]] = [
    ("self_harm", [
        r"\bkill myself\b", r"\bend (my|it) (life|all)\b", r"\bsuicide\b",
        r"\bwant to die\b", r"\bdon'?t want to live\b", r"\bno reason to live\b",
        r"\bhurt myself\b", r"\bharm myself\b", r"\bcut myself\b",
        r"\bmar\s*jau", r"\bmarna chah", r"\bjeena nahi\b", r"\bjina nahi\b",
        # Written against NORMALISED text — no nasal marks, long vowels folded.
        # "मर जाऊँ", "मर जाऊं" and "मर जाउ" all arrive here as "मर जउ".
        r"मर\s*ज", r"आत्मह", r"जिन नहि चहत", r"जिन नहि",
        r"खुद को (मर|नुकसन)", r"अपने को मर",
        r"जन दे", r"जन देन",
    ]),
    ("medical_emergency", [
        r"\bcan'?t breathe\b", r"\bchest pain\b", r"\bbleeding\b",
        r"\bunconscious\b", r"\boverdose\b", r"\bpoison\b",
        r"\bsaans nahi\b", r"सस नहि", r"सिने मे दर्द",
        r"\bbehosh\b", r"बेहोश", r"खुन बह", r"\bzeher\b", r"जहर",
    ]),
    ("abuse_or_danger", [
        r"\bhits? me\b", r"\bbeats? me\b", r"\bhurting me\b",
        r"\bafraid (of|to go) home\b", r"\bnot safe at home\b",
        r"\btouch(ed|es|ing) me\b", r"\babus(e|ed|ing) me\b",
        r"\bmar(ta|ti) (hai|hain) mujhe\b", r"\bmujhe mar(ta|ti)\b",
        r"मुझे मरत", r"घर पर सुरक्षित नहि", r"डर लगत ह घर",
        r"गलत तरिके से छु",
    ]),
    ("harm_to_others", [
        r"\bkill (him|her|them|someone)\b", r"\bhurt (him|her|them|someone)\b",
        r"\bmar dung", r"मर दुंग", r"मर दुग",
    ]),
]

_COMPILED = [
    (name, [re.compile(p, re.IGNORECASE) for p in pats])
    for name, pats in _PATTERNS
]


# ── Responses ─────────────────────────────────────────────────────────────────
# Rules these are written to, and that any edit must keep:
#   - No advice. Not medical, not psychological, not "try this".
#   - Never name a method, and never ask for detail. Asking a distressed
#     student to elaborate to a tutor keeps them talking to the wrong listener.
#   - Point at a specific, reachable human or number.
#   - Short. This is spoken aloud, and length dilutes it.
#   - Warm, and honest about what she is: not equipped for this.
#
# Numbers are Indian national services. Verify them before you ship, and
# localise if your students aren't in India.
#   112   — all-emergency
#   14416 — Tele-MANAS, government mental health support
#   1098  — Childline, for anyone under 18

RESPONSES: dict[str, str] = {
    "self_harm": (
        "मैं यहीं रुक रही हूँ। मैं एक tutor हूँ, इसमें आपकी सही मदद नहीं कर सकती। "
        "अभी किसी भरोसेमंद इंसान से बात कीजिए, या Tele-MANAS — one four four one six."
    ),
    "medical_emergency": (
        "मैं tutor हूँ, doctor नहीं। "
        "अभी किसी बड़े को बुलाइए या one one two पर call कीजिए, देर मत कीजिए।"
    ),
    "abuse_or_danger": (
        "अच्छा किया जो आपने बताया। यह मेरे बस की बात नहीं है। "
        "किसी भरोसेमंद बड़े से बात कीजिए, या Childline — one zero nine eight."
    ),
    "harm_to_others": (
        "मैं यहाँ रुक रही हूँ, इसमें मैं मदद नहीं कर सकती। "
        "आज ही किसी भरोसेमंद बड़े से बात कीजिए। ख़तरा हो तो one one two."
    ),
}

# What a human reading the alert needs to see at a glance.
SEVERITY = {
    "self_harm": "CRITICAL",
    "medical_emergency": "CRITICAL",
    "abuse_or_danger": "HIGH",
    "harm_to_others": "HIGH",
}


def detect(text: str) -> Optional[str]:
    """Return a category name, or None.

    Deliberately returns only the category — never the matched phrase. Nothing
    downstream needs to know which words fired, and passing them around invites
    them into prompts or logs where a student's exact words don't belong.
    """
    if not text:
        return None
    flat = _norm(text)
    for name, patterns in _COMPILED:
        for p in patterns:
            if p.search(flat):
                return name
    return None


def response_for(category: str) -> str:
    return RESPONSES.get(category, RESPONSES["self_harm"])


# ── Alerting ──────────────────────────────────────────────────────────────────

# How insistent the audible alert is.
#
# One system sound is about a second — long enough to notice if you happen to
# be looking at the terminal, and easy to miss if you aren't. An alert you can
# miss isn't doing its job, so it repeats.
#
# Raise ALERT_REPEATS if you work with the terminal in the background; lower it
# if it's more annoying than useful during development.
ALERT_REPEATS = 15
ALERT_GAP_SECONDS = 0.45

# macOS options that carry: Submarine, Sosumi, Basso, Hero, Glass.
MAC_ALERT_SOUND = "/System/Library/Sounds/Submarine.aiff"


def _alert_sound_command() -> Optional[list[str]]:
    """The command that plays one alert, or None if there isn't one."""
    system = platform.system()
    if system == "Darwin" and shutil.which("afplay"):
        return ["afplay", MAC_ALERT_SOUND]
    if system == "Linux" and shutil.which("paplay"):
        return ["paplay",
                "/usr/share/sounds/freedesktop/stereo/dialog-warning.oga"]
    return None


def _sound_worker() -> None:
    """Play the alert repeatedly. Runs on its own thread.

    Each repeat WAITS for the previous one, otherwise six copies start at once
    and you get a single loud smear instead of an insistent beep. That waiting
    is exactly why this can't run on the event loop.
    """
    cmd = _alert_sound_command()
    for i in range(ALERT_REPEATS):
        try:
            if cmd:
                subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=5)
            elif platform.system() == "Windows":
                import winsound
                winsound.MessageBeep(winsound.MB_ICONHAND)
            else:
                # Terminal bell. Works nearly everywhere, needs no audio stack.
                print("\a", end="", flush=True)
        except Exception:
            # A missing player must not silence the rest of the alert, and the
            # log is the real record anyway.
            try:
                print("\a", end="", flush=True)
            except Exception:
                pass
        if i < ALERT_REPEATS - 1:
            time.sleep(ALERT_GAP_SECONDS)


def _play_alert_sound() -> None:
    """Kick off the audible alert and return immediately.

    Daemon thread so it can never hold the process open, and never delays the
    turn it was triggered from.
    """
    try:
        threading.Thread(target=_sound_worker, daemon=True).start()
    except Exception:
        try:
            print("\a", end="", flush=True)
        except Exception:
            pass


def alert_team(category: str, session_id: str = "?", student: Optional[dict] = None,
               transcript: str = "") -> None:
    """Log the incident and make a noise. Never raises.

    Synchronous and fast by design — it's called from an async path and must
    not be something a turn can await or fail on. Sound playback is spawned,
    not waited for.
    """
    try:
        sev = SEVERITY.get(category, "HIGH")
        name = (student or {}).get("name") or "(unnamed)"
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")

        bar = "!" * 68
        print(f"\n{bar}")
        print(f"  SAFETY ALERT — {sev}")
        print(f"  category   : {category}")
        print(f"  session    : {session_id}")
        print(f"  student    : {name}")
        print(f"  time       : {stamp}")
        # The student's own words are included because a human reviewing this
        # needs them, and only here — they are never sent to a model.
        print(f"  transcript : {transcript[:300]}")
        print(f"  action     : tutoring paused; fixed safe response spoken")
        print(f"{bar}\n", flush=True)

        _play_alert_sound()
    except Exception as e:
        # Even the alerter failing must not break the turn.
        print(f"[safety] alert_team failed: {e}", flush=True)


async def alert_team_async(*args, **kwargs) -> None:
    """Off-thread wrapper, so printing and process spawning can't stall the
    event loop while audio is still streaming to other parts of the app."""
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: alert_team(*args, **kwargs))