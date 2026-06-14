"""
LLM service — tries Sarvam first, falls back to Gemini.
"""

import traceback
import httpx
import google.generativeai as genai

from config import get_settings


TUTOR_SYSTEM_PROMPT = """
You are a friendly Sanskrit AI tutor.

Your job is to help students read and understand Sanskrit sentences.

- Always respond in simple Hindi and English mixed (Hinglish is fine).
- Keep responses SHORT — 2-3 sentences max.
- Be encouraging and warm.
- When explaining meaning, give the Hindi meaning first, then English.
- When checking a translation, be specific about what's right and what's wrong.
- When asking grammar questions, give a hint if the student is struggling.
"""


async def _call_sarvam_llm(messages: list[dict]) -> str:
    """
    Call Sarvam Chat API.
    """

    settings = get_settings()

    print("[SARVAM] API key loaded:", bool(settings.sarvam_api_key))

    payload = {
        "model": "sarvam-m",
        "messages": messages,
        "max_tokens": 300,
        "temperature": 0.7,
    }

    headers = {
        "Authorization": f"Bearer {settings.sarvam_api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{settings.sarvam_base_url}/v1/chat/completions",
            json=payload,
            headers=headers,
        )

    print("[SARVAM] Status:", response.status_code)

    response.raise_for_status()

    data = response.json()

    print("[SARVAM] Response:", data)

    return data["choices"][0]["message"]["content"]


async def _call_gemini(messages: list[dict]) -> str:
    """
    Gemini fallback.
    """

    settings = get_settings()

    print("[GEMINI] API key loaded:", bool(settings.gemini_api_key))

    genai.configure(api_key=settings.gemini_api_key)

    model = genai.GenerativeModel("gemini-2.5-flash")

    prompt_parts = [TUTOR_SYSTEM_PROMPT, "\n\n"]

    for m in messages:
        role = "Student" if m["role"] == "user" else "Tutor"
        prompt_parts.append(f"{role}: {m['content']}\n")

    prompt_parts.append("Tutor:")

    prompt = "".join(prompt_parts)

    response = model.generate_content(prompt)

    return response.text


async def chat(
    user_message: str,
    history: list[dict] | None = None,
) -> str:

    messages = [
        {
            "role": "system",
            "content": TUTOR_SYSTEM_PROMPT,
        }
    ]

    if history:
        messages.extend(history)

    messages.append(
        {
            "role": "user",
            "content": user_message,
        }
    )

    try:
        result = await _call_sarvam_llm(messages)

        if result and result.strip():
            return result

        raise Exception("Empty response from Sarvam")

    except Exception as e:
        print("\n========== SARVAM FAILED ==========")
        traceback.print_exc()
        print("===================================\n")

        try:
            result = await _call_gemini(messages)

            if result and result.strip():
                return result

            raise Exception("Empty response from Gemini")

        except Exception as e2:
            print("\n========== GEMINI FAILED ==========")
            traceback.print_exc()
            print("===================================\n")

            return (
                "माफ करें, मुझे समझ नहीं आया। "
                "कृपया फिर से बोलें। "
                "(Sorry, I didn't understand. Please try again.)"
            )
