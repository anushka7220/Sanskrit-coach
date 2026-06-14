"""
Sarvam TTS — converts text to speech audio bytes.
Docs: https://docs.sarvam.ai/api-reference-docs/endpoints/text-to-speech
"""

import httpx
import base64
from config import get_settings


async def synthesize(text: str, language_code: str = "hi-IN") -> bytes:
    settings = get_settings()

    payload = {
        "text": text,
        "target_language_code": language_code,
        "speaker": "anand",
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