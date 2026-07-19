import re
import httpx
import google.generativeai as genai
from config import get_settings

TUTOR_SYSTEM_PROMPT = """You are a friendly female Sanskrit AI tutor.
Your job is to help students read and understand Sanskrit sentences.
- Always respond in simple Hindi and English mixed (Hinglish is fine).
- You are FEMALE. When referring to yourself in Hindi, ALWAYS use feminine
  verb forms: कर रही हूं (not रहा), देख रही हूं (not रहा),
  कर सकती हूं (not सकता), बता सकती हूं (not सकता), समझा रही हूं (not रहा).
- Keep responses SHORT — 2-3 sentences max.
- Be encouraging and warm.
- When explaining meaning, give the Hindi meaning first, then English.
- When checking a translation, be specific about what's right and what's wrong.
- When asking grammar questions, give a hint if the student is struggling.
- Greet with नमस्ते ONLY on the very first message of a session. For all
  later replies, jump straight to the answer —you must NEVER start mid-conversation
  turns with नमस्ते or any greeting."""

def _extract_answer(message: dict) -> str:
    """Pull ONLY the final answer from a reasoning-model response.

    sarvam-105b returns chain-of-thought in `reasoning_content` and the
    real reply in `content`. We must never send reasoning to TTS, so we
    ignore reasoning_content entirely and strip any inline <think> blocks.
    """
    content = (message.get("content") or "").strip()

    # Some reasoning models embed thinking inline as <think>...</think>
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

    # Unterminated think block (ran out of tokens mid-thought)
    if "</think>" in content:
        content = content.split("</think>")[-1].strip()

    return content


async def _call_sarvam_llm(messages: list[dict]) -> str:
    settings = get_settings()

    # Remove system message — pass it as first user message for Sarvam
    filtered = [m for m in messages if m["role"] != "system"]
    system = next((m["content"] for m in messages if m["role"] == "system"), "")

    # Prepend system prompt to first user message
    if filtered and filtered[0]["role"] == "user":
        filtered[0] = {
            "role": "user",
            "content": f"{system}\n\n{filtered[0]['content']}",
        }

    payload = {
        "model": "sarvam-105b",
        "messages": filtered,
        # Reasoning eats tokens BEFORE the answer is written. 900 was too
        # tight, so on long turns the model never reached `content`.
        "max_tokens": 2048,
        "temperature": 0.7,
    }

    async with httpx.AsyncClient(timeout=45) as client:
        response = await client.post(
            "https://api.sarvam.ai/v1/chat/completions",
            headers={
                "api-subscription-key": settings.sarvam_api_key,
                "Content-Type": "application/json",
            },
            json=payload,
        )

    print(f"[Sarvam LLM] Status: {response.status_code}")
    if response.status_code != 200:
        print(f"[Sarvam LLM] Error: {response.text[:200]}")
        response.raise_for_status()

    data = response.json()
    message = data["choices"][0]["message"]
    answer = _extract_answer(message)

    # Empty answer = model spent its whole budget reasoning. Do NOT fall
    # back to reasoning_content — return "" so chat() routes to Gemini.
    if not answer:
        print("[Sarvam LLM] Empty content (reasoning-only) — will fall back")
        return ""

    print(f"[Sarvam LLM] Answer: {answer[:100]}")
    return answer


async def _call_gemini(messages: list[dict]) -> str:
    settings = get_settings()
    genai.configure(api_key=settings.gemini_api_key)
    model = genai.GenerativeModel("gemini-1.5-flash")

    prompt_parts = [TUTOR_SYSTEM_PROMPT, "\n\n"]
    for m in messages:
        if m["role"] == "system":
            continue
        role = "Student" if m["role"] == "user" else "Tutor"
        prompt_parts.append(f"{role}: {m['content']}\n")
    prompt_parts.append("Tutor:")

    response = model.generate_content("".join(prompt_parts))
    return (response.text or "").strip()


async def chat(user_message: str, history: list[dict] | None = None) -> str:
    messages = [{"role": "system", "content": TUTOR_SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    try:
        result = await _call_sarvam_llm(messages)
        if result:
            return result
        raise Exception("Empty response")
    except Exception as e:
        print(f"[LLM] Sarvam failed ({e}), falling back to Gemini")
        try:
            result = await _call_gemini(messages)
            if result:
                return result
            raise Exception("Empty response from Gemini")
        except Exception as e2:
            print(f"[LLM] Gemini also failed: {e2}")
            return "माफ करें, मुझे समझ नहीं आया। कृपया फिर से बोलें। (Sorry, please try again.)"