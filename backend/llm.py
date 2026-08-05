"""
LLM layer — streaming, async, with provider failover.

Exposes:
    stream_chat(user_message, history) -> AsyncIterator[str]   # primary API
    chat(user_message, history)        -> str                  # compat wrapper

WHAT CHANGED AND WHY
--------------------
1. Provider order is flipped. Gemini Flash is now primary; Sarvam is the
   fallback. `sarvam-105b` is a *reasoning* model — it writes chain-of-thought
   before it writes the answer, and you throw that away. That's fine for a
   batch pipeline and fatal for a streaming one: nothing arrives until the
   thinking is done, so there's nothing to stream. Flip PROVIDER_ORDER back
   once you've confirmed a non-reasoning Sarvam model (or a thinking-level
   parameter) that keeps time-to-first-token low.

2. Gemini is no longer synchronous. `google.generativeai`'s
   `generate_content()` blocks the event loop — which in the old turn-based
   design was merely slow, but now would stall the TTS audio pump mid-
   utterance and cause playback underruns. Uses `google-genai`'s async client.

       pip uninstall google-generativeai
       pip install google-genai

3. Failover is commit-based. Once the first token has been handed to TTS,
   audio is already playing and we can't take it back — so a mid-stream
   failure ends the turn rather than restarting on the other provider.
   Failover only happens before anything has been emitted.
"""

import json
from typing import AsyncIterator, Callable

import httpx

from config import get_settings

# ── Provider config ───────────────────────────────────────────────────────

# Order matters. First entry is primary; later entries are tried only if an
# earlier one fails *before emitting its first token*.
#
# Gemini primary. Sarvam stays as a safety net, but note it only fires if
# Gemini dies BEFORE its first token — and a sarvam-105b turn costs ~16s, so
# treat any fallback in the logs as an incident, not a normal path.
PROVIDER_ORDER = ["gemini", "sarvam"]

# Flash-Lite is the latency tier: fewer parameters, lower time-to-first-token.
# For a 2-3 sentence spoken tutor reply there is little the bigger Flash buys
# you, and TTFT is the metric that decides whether the tutor feels alive.
# Revert to "gemini-3.6-flash" if replies get noticeably worse — grammar
# explanation is the place a lite model tends to slip first.
GEMINI_MODEL = "gemini-3.5-flash-lite"

# THE fix for 4-7s time-to-first-token.
#
# 3.x models control reasoning with thinking *levels*, not budgets. Leaving
# this unset does NOT mean "no thinking" — it means the model falls back to
# its own default, which is thinking ON. That default is exactly why first
# token swung between 522ms and 7010ms on identical code: short inputs skipped
# thinking, anything else spent seconds on it before emitting a single token.
#
# This is the same class of bug as Sarvam's reasoning_effort. Both models
# think by default; both have to be told not to.
#
# Valid: "MINIMAL" | "LOW" | "MEDIUM" | "HIGH" | None (None = model default).
GEMINI_THINKING_LEVEL: str | None = "MINIMAL"

# Check https://docs.sarvam.ai/api-reference-docs/getting-started/models — if a
# non-reasoning model is available on your key, use it here. Otherwise look at
# the chat-completion "adjust the model's thinking level" control. Streaming a
# reasoning model is close to pointless.
# sarvam-30b is Sarvam's voice-pipeline model (powers Samvaad, built for
# multilingual voice calls); 105b is the reasoning/agent flagship. For a
# 2-3 sentence spoken tutor reply, 30b is the right call.
SARVAM_MODEL = "sarvam-30b"
SARVAM_CHAT_URL = "https://api.sarvam.ai/v1/chat/completions"

MAX_TOKENS = 300     # kept for compatibility; see per-provider values below

# sarvam-105b is a reasoning model: it spends tokens on chain-of-thought
# BEFORE it writes `content`. Your original code found 900 too tight — the
# model never reached the answer. A 300-token cap is why you saw
# "sarvam returned nothing". Sarvam gets a large budget; Gemini gets a small
# one (small budget = latency cap on a non-reasoning model).
# THE fix for "returned reasoning only". Sarvam chat models have thinking mode
# ON by default, so the whole token budget gets eaten by chain-of-thought and
# `content` comes back empty. None disables reasoning entirely — required for
# any latency-sensitive path, and mandatory for voice.
# Other values: "low" | "medium" | "high".
SARVAM_REASONING_EFFORT = None

# Safe to keep small ONLY because thinking is now MINIMAL. With thinking on, a
# low cap gets eaten by chain-of-thought and you get an empty reply — that was
# the Sarvam failure. Devanagari tokenises heavily, so 400 ≈ 3 Hindi sentences.
# This doubles as a latency ceiling: the model cannot ramble.
GEMINI_MAX_TOKENS = 400
SARVAM_MAX_TOKENS = 2048

# Every turn resends the whole history, so prompt tokens grow without bound and
# time-to-first-token creeps up over a session. A tutor turn only needs recent
# context. 6 = 3 exchanges.
MAX_HISTORY_MESSAGES = 6

# sarvam-105b's SSE streaming is unverified and previously returned nothing.
# False = call the batch endpoint and emit the whole reply as one chunk. The
# TTS layer still cuts it into sentences, so audio starts after sentence one
# instead of after the whole reply — you keep most of the win.
# Flip to True to test real token streaming.
SARVAM_STREAMING = False

TEMPERATURE = 0.5

FALLBACK_LINE = "माफ करें, मुझे समझ नहीं आया। कृपया फिर से बोलें। (Sorry, please try again.)"

TUTOR_SYSTEM_PROMPT = """
You are Vidya, a warm, friendly female Sanskrit teacher who teaches spoken Sanskrit to beginners.

Your goal is to help students understand Sanskrit naturally, like a chill friend sitting beside them who happens to know Sanskrit well.
Tone is always calm and flat — like you're just talking, not performing.
Never sound enthusiastic or like you're trying to hype the student up.
do not write sankrit words in enlish or hinglish, they must be in sankrit.
do not write complex hindi words in enlish, they must be in hindi.

========================
LANGUAGE
========================

- Speak in Hinglish — mix Hindi and English the way two friends talk who grew up speaking both.
- Hindi words MUST always be written in Devanagari.
- English words MUST always be written in Latin script.
- Never write Hindi in Roman letters.

Correct:
"इसका matlab है, Rama goes to the forest."

Wrong:
"Kya aap samajh gaye?"

========================
PERSONALITY
========================

- You are calm, cheerful and encouraging.
- Talk like a friend who knows Sanskrit — not a teacher giving marks.
- Sound human, not like a chatbot.
- Speak naturally as if talking to one friend.
- Never sound robotic or overly formal.
- You react the way a friend would — briefly, genuinely, then move on.
- If something is wrong, say it directly but lightly — not as a correction, as a heads-up.
NEVER start a reply with dekhiye,

NEVER use these words: बहुत बढ़िया, शाबाश, वाह, बहुत अच्छे।

Encouragement should feel offhand and real:
  - haan yahi hai!
  - ekdum sahi.
  - haan bilkul.
  - haan! yahi tha.
  - almost — bas ek cheez.
  - karib ho, thoda sa.
  - are haan, yahi!
  - nice, yahi chahiye tha.
  - correct hai yaar.

========================
FEMALE SPEECH
========================

You are FEMALE.

Always use feminine forms for yourself:

✓ समझा रही हूं
✓ बता सकती हूं
✓ देख रही हूं
✓ कर सकती हूं

Never use masculine forms.

========================
ADDRESSING THE STUDENT
========================

Always address the student as "tum" — never "aap".
Use "tumhara", "tumne", "tum karo" — never "aapka", "aapne", "aap karein".
Verb forms with tum: "tum samjhe?", "tumne sahi kaha", "tum try karo".

========================
SPOKEN STYLE
========================

Your replies are spoken aloud.

Write exactly how a good friend would naturally speak — casual, warm, real.

Do NOT start replies with filler openers like: अच्छा, देखो, तो, ठीक है, are, dekhiye.
Jump straight into what you want to say.
Only use a filler mid-sentence if it sounds completely natural — never as an opener.

Use these SPARINGLY — one per reply maximum, only when it sounds natural.
Never use them when directly confirming or correcting.

Instead be direct:
✓ haan yahi hai.
✓ almost sahi hai.
✓ ek word change karna hoga.

Use commas where a person would naturally pause.

Avoid long written-style sentences.

========================
LENGTH
========================

Keep replies short.

Usually 2–3 sentences.

Only give longer explanations if the student explicitly asks.

========================
TEACHING
========================

When explaining meaning:

1. Hindi meaning first.
2. English meaning second.

When checking translations:

- Tell exactly what is correct.
- Tell exactly what should change.
- If the student mixes Hindi and English naturally, accept it if the meaning is correct.

When teaching grammar:

- Give hints before giving the full answer.
- Help the student think.

If the student pauses or becomes silent:

Ask one short, warm follow-up question to continue.

Example:
"agla word padho."
"इसका meaning batana chahoge?"
"ek baar phir try karo?"

========================
BOUNDARIES
========================

Only help with Sanskrit learning.

Do not answer questions about:
- Maths
- Science
- History
- Geography
- Politics
- Religion
- Programming
- General knowledge
- Any topic unrelated to Sanskrit

Politely guide the student back to Sanskrit.

========================
CONVERSATION
========================

Do not greet in every response.

Say hello only at the beginning of a new conversation.

Do not repeat the student's name unless greeting them.

Speak like you remember the ongoing conversation.
Always use "tum" with the student — friendly and warm, never formal "aap".

========================
PUNCTUATION
========================

Every sentence must end with:

- ।
or
- .

Never leave sentences unfinished because the text is converted directly into speech.
"""
def build_system_prompt(student: dict | None = None) -> str:
    """Attach the student's name and gender to the tutor persona.
 
    Kept as a function rather than mutating a module constant: one process
    serves many concurrent sessions, and a global would leak one student's
    name into another's lesson.
    """
    if not student:
        return TUTOR_SYSTEM_PROMPT
 
    extra = []
 
    name = (student.get("name") or "").strip()
    if name:
        extra.append(
            f"- The student's name is {name}. Use it plainly — say '{name}', "
            f"never '{name} जी' or any honorific suffix. Use it occasionally "
            f"and warmly (greeting them, praising them) but NOT in every "
            f"sentence, which sounds robotic."
        )
 
    gender = (student.get("gender") or "neutral").lower()
    if gender == "female":
        extra.append(
            "- The student is FEMALE. Every Hindi verb and adjective you "
            "address to her must take feminine forms: आप जानना चाहती हैं "
            "(not चाहते), आपने सही कहा, आप पढ़ रही हैं (not रहे), "
            "आप तैयार हैं तो बताइए."
        )
    elif gender == "male":
        extra.append(
            "- The student is MALE. Every Hindi verb and adjective you address "
            "to him must take masculine forms: आप जानना चाहते हैं (not चाहती), "
            "आप पढ़ रहे हैं (not रही)."
        )
    else:
        extra.append(
            "- The student's gender is unknown. Avoid gendered Hindi verb "
            "forms when addressing them — prefer neutral phrasings like "
            "'क्या हम आगे बढ़ें?', 'बहुत बढ़िया!', 'अब यह वाक्य देखिए' over "
            "constructions that force चाहते/चाहती."
        )
 
    return TUTOR_SYSTEM_PROMPT + "\n" + "\n".join(extra)
 
 
# ── Streaming think-block filter ──────────────────────────────────────────
 
class _ThinkFilter:
    """Drops <think>...</think> spans from a token stream.
 
    The batch version could regex the whole string. Streaming can't — the tags
    routinely split across tokens — so this walks a small carry buffer.
    """
 
    _OPEN = "<think>"
    _CLOSE = "</think>"
 
    def __init__(self):
        self._buf = ""
        self._inside = False
 
    def feed(self, text: str) -> str:
        self._buf += text
        out = []
        while self._buf:
            if self._inside:
                idx = self._buf.find(self._CLOSE)
                if idx == -1:
                    # Hold back only enough to catch a split closing tag.
                    self._buf = self._buf[-len(self._CLOSE):]
                    break
                self._buf = self._buf[idx + len(self._CLOSE):]
                self._inside = False
                continue
 
            idx = self._buf.find(self._OPEN)
            if idx == -1:
                # Emit everything except a possible partial opening tag.
                keep = len(self._OPEN) - 1
                if len(self._buf) > keep:
                    out.append(self._buf[:-keep] if keep else self._buf)
                    self._buf = self._buf[-keep:] if keep else ""
                break
            out.append(self._buf[:idx])
            self._buf = self._buf[idx + len(self._OPEN):]
            self._inside = True
        return "".join(out)
 
    def flush(self) -> str:
        """Emit anything held back once the stream ends."""
        if self._inside:
            self._buf = ""
            return ""
        tail, self._buf = self._buf, ""
        return tail
 
 
# ── Gemini ────────────────────────────────────────────────────────────────
 
_gemini_client = None
 
 
def _get_gemini():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        _gemini_client = genai.Client(api_key=get_settings().gemini_api_key)
    return _gemini_client
 
 
async def _stream_gemini(messages: list[dict]) -> AsyncIterator[str]:
    from google.genai import types
 
    contents = []
    system_text = TUTOR_SYSTEM_PROMPT
    for m in messages:
        if m["role"] == "system":
            # Use the system message that was actually built for this session
            # rather than the module constant — that constant has no idea who
            # the student is.
            system_text = m["content"]
            continue
        role = "user" if m["role"] == "user" else "model"
        contents.append(types.Content(role=role, parts=[types.Part(text=m["content"])]))
 
    cfg_kwargs = dict(
        system_instruction=system_text,
        temperature=TEMPERATURE,
        max_output_tokens=GEMINI_MAX_TOKENS,
    )
    if GEMINI_THINKING_LEVEL is not None:
        cfg_kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_level=GEMINI_THINKING_LEVEL
        )
 
    stream = await _get_gemini().aio.models.generate_content_stream(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(**cfg_kwargs),
    )
 
    async for chunk in stream:
        text = getattr(chunk, "text", None)
        if text:
            yield text
 
        # Definitive check that thinking_level actually took effect. Same
        # trick as Sarvam's token log: if thoughts > 0, the model is still
        # reasoning and that is where time-to-first-token is going.
        usage = getattr(chunk, "usage_metadata", None)
        if usage is not None:
            thoughts = getattr(usage, "thoughts_token_count", None)
            if thoughts:
                print(f"[Gemini] thoughts={thoughts} "
                      f"output={getattr(usage, 'candidates_token_count', None)} "
                      f"prompt={getattr(usage, 'prompt_token_count', None)}")
 
 
# ── Sarvam ────────────────────────────────────────────────────────────────
 
async def _stream_sarvam(messages: list[dict]) -> AsyncIterator[str]:
    """Router: batch (reliable) or true SSE streaming (unverified)."""
    if SARVAM_STREAMING:
        async for token in _stream_sarvam_sse(messages):
            yield token
    else:
        text = await _call_sarvam_batch(messages)
        if text:
            yield text
 
 
def _prepare_sarvam_messages(messages: list[dict]) -> list[dict]:
    """Strip the system role and fold the prompt into the CURRENT user turn.
 
    Your original prepended to messages[0], which is the *oldest* history entry
    once a conversation gets going — so on later turns the instructions ended
    up buried behind several exchanges.
    """
    convo = [dict(m) for m in messages if m["role"] != "system"]
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    if convo and system:
        convo[-1]["content"] = f"{system}\n\n{convo[-1]['content']}"
    return convo
 
 
def _extract_answer(message: dict) -> str:
    """Pull ONLY the final answer from a reasoning-model response.
 
    sarvam-105b returns chain-of-thought in `reasoning_content` and the real
    reply in `content`. Reasoning must never reach TTS, so we ignore
    reasoning_content entirely and strip any inline <think> blocks.
    """
    import re
 
    content = (message.get("content") or "").strip()
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    if "</think>" in content:
        content = content.split("</think>")[-1].strip()
    return content
 
 
async def _call_sarvam_batch(messages: list[dict]) -> str:
    """One-shot (non-streaming) Sarvam call. This is your original working
    path — kept because a reasoning model has nothing useful to stream."""
    settings = get_settings()
 
    payload = {
        "model": SARVAM_MODEL,
        "messages": _prepare_sarvam_messages(messages),
        "max_tokens": SARVAM_MAX_TOKENS,
        "temperature": TEMPERATURE,
        # Explicit null disables thinking mode. Must be sent as JSON null,
        # which is what Python None serialises to.
        "reasoning_effort": SARVAM_REASONING_EFFORT,
    }
 
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            SARVAM_CHAT_URL,
            headers={
                "api-subscription-key": settings.sarvam_api_key,
                "Content-Type": "application/json",
            },
            json=payload,
        )
 
    if response.status_code != 200:
        raise RuntimeError(f"Sarvam {response.status_code}: {response.text[:300]}")
 
    data = response.json()
 
    # Token accounting tells you instantly whether reasoning is still on.
    usage = data.get("usage") or {}
    if usage:
        print(f"[Sarvam] tokens: {usage}")
 
    answer = _extract_answer(data["choices"][0]["message"])
 
    if not answer:
        reasoning = (data["choices"][0]["message"].get("reasoning_content") or "")
        raise RuntimeError(
            "Sarvam returned reasoning only — no content "
            f"(reasoning chars={len(reasoning)}; is reasoning_effort actually null?)"
        )
 
    return answer
 
 
async def _stream_sarvam_sse(messages: list[dict]) -> AsyncIterator[str]:
    settings = get_settings()
 
    payload = {
        "model": SARVAM_MODEL,
        "messages": _prepare_sarvam_messages(messages),
        "max_tokens": SARVAM_MAX_TOKENS,
        "temperature": TEMPERATURE,
        "reasoning_effort": SARVAM_REASONING_EFFORT,
        "stream": True,
    }
 
    think = _ThinkFilter()
 
    async with httpx.AsyncClient(timeout=60) as client:
        async with client.stream(
            "POST",
            SARVAM_CHAT_URL,
            headers={
                "api-subscription-key": settings.sarvam_api_key,
                "Content-Type": "application/json",
            },
            json=payload,
        ) as response:
            if response.status_code != 200:
                body = (await response.aread()).decode(errors="replace")
                raise RuntimeError(f"Sarvam {response.status_code}: {body[:200]}")
 
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
 
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
 
                # Deliberately ignore `reasoning_content`. It must never reach
                # TTS — that was the whole point of _extract_answer().
                piece = delta.get("content")
                if piece:
                    cleaned = think.feed(piece)
                    if cleaned:
                        yield cleaned
 
    tail = think.flush()
    if tail:
        yield tail
 
 
_PROVIDERS: dict[str, Callable[[list[dict]], AsyncIterator[str]]] = {
    "gemini": _stream_gemini,
    "sarvam": _stream_sarvam,
}
 
 
# ── Public API ────────────────────────────────────────────────────────────
 
# Set False to rule prewarm out entirely. It runs as a detached task during the
# greeting and touches nothing the mic or STT path uses, so it should not be
# able to affect them — but a one-line off switch is cheaper than arguing.
LLM_PREWARM = True
 
 
async def prewarm() -> None:
    """Pay the cold-start cost before the student's first turn.
 
    The first Gemini call in a process does far more than generate: it builds
    the client, resolves DNS, completes a TLS handshake and negotiates HTTP/2.
    That is why turn one feels sluggish and every turn after it is fine.
 
    Call this while the greeting is being spoken — the greeting is a fixed
    string that takes several seconds to say, so the warmup is free.
    Failures are ignored on purpose: this is an optimisation, and a broken
    prewarm must never stop a session from starting.
    """
    try:
        if not LLM_PREWARM:
            return
        from google.genai import types
        await _get_gemini().aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=[types.Content(role="user", parts=[types.Part(text="hi")])],
            config=types.GenerateContentConfig(max_output_tokens=1),
        )
        print("[LLM] prewarm ok")
    except Exception as e:
        print(f"[LLM] prewarm skipped: {e}")
 
 
def _build_messages(user_message: str, history: list[dict] | None,
                    student: dict | None = None) -> list[dict]:
    messages = [{"role": "system", "content": build_system_prompt(student)}]
    if history:
        messages.extend(history[-MAX_HISTORY_MESSAGES:])
    messages.append({"role": "user", "content": user_message})
    return messages
 
 
async def stream_chat(
    user_message: str,
    history: list[dict] | None = None,
    student: dict | None = None,
) -> AsyncIterator[str]:
    """Yield reply text incrementally, failing over between providers.
 
    Failover only fires *before* the first token. After that the audio is
    already in the user's ears and restarting would double-speak the turn.
 
    `student` is optional and passed per call rather than held on the module,
    because one process serves many sessions at once.
    """
    messages = _build_messages(user_message, history, student)
 
    for name in PROVIDER_ORDER:
        gen = _PROVIDERS[name](messages)
        emitted = False
        try:
            async for token in gen:
                if not token:
                    continue
                emitted = True
                yield token
        except Exception as e:
            if emitted:
                print(f"[LLM] {name} failed mid-stream — turn truncated: {e}")
                return
            print(f"[LLM] {name} failed before first token ({e}) — trying next")
            continue
        finally:
            await gen.aclose()
 
        if emitted:
            return
        print(f"[LLM] {name} returned nothing — trying next")
 
    print("[LLM] all providers failed")
    yield FALLBACK_LINE
 
 
async def chat(user_message: str, history: list[dict] | None = None) -> str:
    """Non-streaming compatibility wrapper. Prefer stream_chat()."""
    parts = [t async for t in stream_chat(user_message, history)]
    return "".join(parts).strip()