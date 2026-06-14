"""
Sarvam STT — converts audio bytes to text.
httpx is basically the modern version of Python's requests library.
base64 is a built-in Python module used to convert binary data (audio, images, PDFs, etc.) into text
Docs: https://docs.sarvam.ai/api-reference-docs/endpoints/speech-to-text
"""
import httpx
import base64
from config import get_settings


async def transcribe(audio_bytes: bytes, language_code: str = "hi-IN") -> str:
    settings = get_settings()

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{settings.sarvam_base_url}/speech-to-text",
            headers={"api-subscription-key": settings.sarvam_api_key},
            files={"file": ("audio.wav", audio_bytes, "audio/wav")},
            data={
                "model": "saarika:v2.5",
                "language_code": language_code,
            },
        )

    print(f"[STT] Status: {response.status_code}")
    print(f"[STT] Response: {response.text[:400]}")

    response.raise_for_status()
    data = response.json()
    return data.get("transcript", "")