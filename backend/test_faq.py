"""
Test the FAQ layer without talking to it.

Run from the backend folder:

    python test_faq.py

This checks the two things that actually matter, and they are not the same:

  1. FAQ questions reach the FAQ.
  2. Lesson questions DON'T.

The second is the one that breaks quietly. A tutor that misses an FAQ just
falls back to a generic reply — mildly disappointing. A tutor that answers
"इसका अनुवाद बताओ" out of the FAQ has ignored the actual lesson, and you'd
only notice by using it.

No API calls, no audio, no server. Retrieval and routing are pure functions,
so they can be tested in milliseconds instead of by speaking into a mic.
"""

import faq
from orchestrator import detect_intent, detect_level_change


def route(text: str) -> str:
    """Mirror the real order in process_turn_stream.

    Level change first, then intent, and only an `unknown` intent ever reaches
    the FAQ. Getting this order wrong in the test would hide exactly the bugs
    the test exists to catch.
    """
    if detect_level_change(text, "easy"):
        return "level_change"

    intent = detect_intent(text)
    if intent != "unknown":
        return intent

    hit = faq.retrieve(text)
    return f"faq:{hit['id']}" if hit else "unknown"


FAQ_CASES = [
    ("तुम कौन हो",                          "faq:who_are_you"),
    ("yeh kaise kaam karta hai",             "faq:how_it_works"),
    ("kya main tumhe beech me rok sakti hu", "faq:interrupt"),
    ("agar main galat bolun to",             "faq:wrong_answer"),
    ("kya meri progress save hoti hai",      "faq:progress_saved"),
    ("sanskrit seekhne me kitna samay lagega","faq:how_long"),
    ("kya main hindi me bol sakti hu",       "faq:language"),
    ("mera uchcharan sahi hai kya",          "faq:pronunciation"),
    ("teeno level me kya fark hai",          "faq:levels_meaning"),
    ("तुम्हारा नाम क्या है",                  "faq:who_are_you"),
]

# These must NEVER be answered from the FAQ.
LESSON_CASES = [
    ("इसका मतलब बताओ",                       "meaning"),
    ("इसका हिंदी अनुवाद क्या है",             "translate"),
    ("रामः वनं गच्छति",                      "unknown"),
    ("अगले वाक्य पर चलो",                    "move_on"),
    ("बालकः पुस्तकं पठति इसका अनुवाद करो",    "translate"),
    ("level change karo hard",               "level_change"),
    ("अच्छा चलो, इसका मतलब बताओ",             "meaning"),
]


def run(title, cases):
    print(f"\n{title}")
    print("-" * len(title))
    failures = 0
    for text, want in cases:
        got = route(text)
        ok = got == want
        failures += not ok
        print(f"  {'ok ' if ok else 'BAD'}  {got:24} want={want:24} | {text}")
    return failures


if __name__ == "__main__":
    f = 0
    f += run("FAQ questions should reach the FAQ", FAQ_CASES)
    f += run("Lesson questions must NOT reach the FAQ", LESSON_CASES)

    print()
    if f:
        print(f"{f} failing case(s).")
        print("If an FAQ case missed: add the words you actually said to that")
        print("entry's `keywords` — don't lower MATCH_THRESHOLD, that loosens")
        print("every entry at once and starts eating lesson questions.")
    else:
        print("All routing correct.")