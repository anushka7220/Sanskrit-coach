"""
Sarvam TTS — converts text to speech audio bytes.
Docs: https://docs.sarvam.ai/api-reference-docs/endpoints/text-to-speech
"""

import httpx
import base64
from config import get_settings

# Sarvam TTS rejects any request whose text exceeds 2500 chars (400 error).
MAX_TTS_CHARS = 2500


def _prepare_tts_text(text: str) -> str:
    """Trim TTS input to Sarvam's hard limit, cutting at a sentence end.

    A tutor reply should never be this long — this is a safety net so a
    misbehaving LLM turn can't 400 the whole synthesis call.
    """
    text = (text or "").strip()
    if len(text) <= MAX_TTS_CHARS:
        return text

    clipped = text[:MAX_TTS_CHARS]
    # Prefer to end on a clean boundary (Devanagari danda, then punctuation)
    for sep in ("।", ".", "!", "?", "\n"):
        i = clipped.rfind(sep)
        if i > 0:
            return clipped[: i + 1].strip()
    return clipped.strip()


async def synthesize(text: str, language_code: str = "hi-IN") -> bytes:
    settings = get_settings()

    text = _prepare_tts_text(text)
    if not text:
        raise ValueError("[TTS] Empty text after preparation — nothing to synthesize")

    payload = {
        "text": text,
        "target_language_code": language_code,
        "speaker": "ishita",
        "model": "bulbul:v3",
        "pace": 0.9,
        "speech_sample_rate": 22050,
        "enable_preprocessing": True,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{settings.sarvam_base_url}/text-to-speech",
            headers={
                "api-subscription-key": settings.sarvam_api_key,
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if response.status_code != 200:
        print(f"[TTS] Raw error: {response.text}")
        response.raise_for_status()

    data = response.json()
    audio_b64 = data["audios"][0]
    return base64.b64decode(audio_b64)