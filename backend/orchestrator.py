
"""
Orchestrator — decides what to do with each user turn.
 
Three modes depending on difficulty level and what the user said:
  - meaning   : explain the current sentence in Hindi/English
  - translate : user attempts a translation → check it, loop if wrong
  - grammar   : ask a grammar question (hard level only)
  - move_on   : advance to the next sentence
"""

from data.sentences import Sentence
import llm

#intent detection 
MEANING_KEYWORDS = [
    "meaning", "matlab", "arth", "kya matlab", "samjhao", "explain",
    "what does", "what is", "मतलब", "अर्थ", "समझाओ",
]
 
TRANSLATION_KEYWORDS = [
    "translate", "translation", "anuvad", "mera anuvad", "check",
    "is this right", "sahi hai", "अनुवाद", "सही है",
]
 
MOVE_ON_KEYWORDS = [
    "move on", "next", "agla", "aage", "done", "ok", "okay", "got it",
    "समझ गया", "आगे", "अगला",
]
 
def detect_intent(text: str) -> str:
    """Returns one of: 'meaning' | 'translate' 
    | 'move_on' | 'unknown'
    """
    t = text.lower()
    if any(k in t for k in MOVE_ON_KEYWORDS):
        return "move_on"
    if any(k in t for k in MEANING_KEYWORDS):
        return "meaning"
    if any(k in t for k in TRANSLATION_KEYWORDS):
        return "translate"
    return "unknown"


 
async def handle_meaning(sentence: Sentence, history: list[dict]) -> str:
    prompt = (
        f"The student is reading this Sanskrit sentence: '{sentence['sanskrit']}'\n"
        f"They want to know the meaning. "
        f"Hindi meaning: {sentence['meaning_hi']}. "
        f"English meaning: {sentence['meaning_en']}. "
        f"Explain it simply and encouragingly in 2-3 sentences."
    )
    return await llm.chat(prompt, history)
 

async def handle_translation_check(
    sentence: Sentence,
    user_translation: str,
    history: list[dict],
) -> tuple[str, bool]:
    """
    Returns (ai_response, is_correct).
    """
    prompt = (
        f"Sanskrit sentence: '{sentence['sanskrit']}'\n"
        f"Correct English translation: '{sentence['translation_en']}'\n"
        f"Student's translation: '{user_translation}'\n\n"
        f"Is the student's translation correct or close enough? "
        f"If yes, praise them and say 'move on'. "
        f"If no, gently point out what's wrong and ask them to try again. "
        f"Reply with RESULT:CORRECT or RESULT:RETRY on the first line, "
        f"then your spoken response on the next line."
    )
    raw = await llm.chat(prompt, history)
 
    # Parse the structured response
    lines = raw.strip().splitlines()
    is_correct = lines[0].strip().upper().startswith("RESULT:CORRECT") if lines else False
    spoken = "\n".join(lines[1:]).strip() if len(lines) > 1 else raw
 
    return spoken, is_correct
 
 
async def handle_grammar(sentence: Sentence, history: list[dict]) -> str:
    prompt = (
        f"Sanskrit sentence: '{sentence['sanskrit']}'\n"
        f"Grammar question to ask: '{sentence['grammar_note']}'\n"
        f"Ask the student this grammar question in a friendly way. "
        f"Keep it short."
    )
    return await llm.chat(prompt, history)
 
 
async def handle_grammar_answer(
    sentence: Sentence,
    user_answer: str,
    history: list[dict],
) -> tuple[str, bool]:
    prompt = (
        f"Sanskrit sentence: '{sentence['sanskrit']}'\n"
        f"Grammar question: '{sentence['grammar_note']}'\n"
        f"Student's answer: '{user_answer}'\n\n"
        f"Evaluate if the answer is correct. "
        f"Reply with RESULT:CORRECT or RESULT:RETRY on the first line, "
        f"then your spoken feedback on the next line."
    )
    raw = await llm.chat(prompt, history)
 
    lines = raw.strip().splitlines()
    is_correct = lines[0].strip().upper().startswith("RESULT:CORRECT") if lines else False
    spoken = "\n".join(lines[1:]).strip() if len(lines) > 1 else raw
 
    return spoken, is_correct
 
 
async def handle_unknown(sentence: Sentence, history: list[dict]) -> str:
    """Fallback — treat as a general question about the current sentence."""
    prompt = (
        f"The student is studying this Sanskrit sentence: '{sentence['sanskrit']}'\n"
        f"They said something unclear. Gently ask them what they'd like — "
        f"meaning, translation check, or to move to the next sentence."
    )
    return await llm.chat(prompt, history)
 
 
# ── Main entry point ──────────────────────────────────────────────────────────
 
async def process_turn(
    user_text: str,
    sentence: Sentence,
    level: str,
    history: list[dict],
    awaiting_grammar: bool = False,
    awaiting_translation: bool = False,
) -> dict:
    """
    Process one user turn.
 
    Returns a dict:
    {
        "response_text": str,        # what AI says (for TTS)
        "intent": str,               # detected intent
        "move_on": bool,             # should we advance to next sentence?
        "awaiting_grammar": bool,    # still waiting for grammar answer?
        "awaiting_translation": bool # still waiting for translation retry?
    }
    """
 
    # If we're in a loop waiting for a specific answer
    if awaiting_translation:
        spoken, correct = await handle_translation_check(sentence, user_text, history)
        return {
            "response_text": spoken,
            "intent": "translate",
            "move_on": False,
            "awaiting_grammar": False,
            "awaiting_translation": not correct,  # keep looping if wrong
        }
 
    if awaiting_grammar:
        spoken, correct = await handle_grammar_answer(sentence, user_text, history)
        if correct and level == "hard":
            # After grammar, suggest moving on
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
        # On intermediate/hard, check user's translation attempt
        spoken, correct = await handle_translation_check(sentence, user_text, history)
        return {
            "response_text": spoken,
            "intent": "translate",
            "move_on": False,
            "awaiting_grammar": False,
            "awaiting_translation": not correct,
        }
 
    # Hard level: if user seems to have read the sentence, trigger grammar
    if level == "hard" and intent == "unknown":
        spoken = await handle_grammar(sentence, history)
        return {
            "response_text": spoken,
            "intent": "grammar",
            "move_on": False,
            "awaiting_grammar": True,
            "awaiting_translation": False,
        }
 
    # Default fallback
    spoken = await handle_unknown(sentence, history)
    return {
        "response_text": spoken,
        "intent": "unknown",
        "move_on": False,
        "awaiting_grammar": False,
        "awaiting_translation": False,
    }