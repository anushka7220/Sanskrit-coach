"""
List the voices your ElevenLabs account can actually use via the API.

The default voice_id in tts.py is a library voice, which free accounts can't
use through the API — that's the 402 you saw. This prints the voices tied to
your account, which are the ones that will work. Pick a female one and put its
ID in TTS_VOICE_ID.

    python list_voices.py
"""

import asyncio
from elevenlabs import AsyncElevenLabs
from config import get_settings


async def main():
    client = AsyncElevenLabs(api_key=get_settings().elevenlabs_api_key)
    resp = await client.voices.get_all()

    print(f"\n{len(resp.voices)} voice(s) available on this account:\n")
    for v in resp.voices:
        labels = getattr(v, "labels", {}) or {}
        gender = labels.get("gender", "?")
        accent = labels.get("accent", "")
        desc = labels.get("description", "")
        cat = getattr(v, "category", "?")
        print(f"  {v.voice_id}")
        print(f"      {v.name}  |  {gender}  {accent}  {desc}  [{cat}]")
    print()
    print("Pick a female voice and set TTS_VOICE_ID in tts.py to its id above.")
    print("category 'premade' works on free tier; 'professional'/'cloned' may not.")


if __name__ == "__main__":
    asyncio.run(main())