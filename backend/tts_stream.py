# backend/tts_stream.py
import asyncio
import base64
from sarvamai import AsyncSarvamAI, AudioOutput, EventResponse

SAMPLE_RATE = 24000

class SarvamTTSStream:
    """One instance per assistant turn. Owns a Sarvam TTS websocket."""

    def __init__(self, api_key: str, speaker: str = "shubh", language: str = "hi-IN"):
        self._client = AsyncSarvamAI(api_subscription_key=api_key)
        self.speaker = speaker
        self.language = language
        self.audio_out: asyncio.Queue = asyncio.Queue()
        self._cm = None
        self._ws = None
        self._reader = None

    async def open(self):
        self._cm = self._client.text_to_speech_streaming.connect(
            model="bulbul:v3",
            send_completion_event=True,
        )
        self._ws = await self._cm.__aenter__()
        await self._ws.configure(
            target_language_code=self.language,
            speaker=self.speaker,
            pace=0.9,
            min_buffer_size=30,
            max_chunk_length=120,
            output_audio_codec="pcm",
        )
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        try:
            async for msg in self._ws:
                if isinstance(msg, AudioOutput):
                    await self.audio_out.put(base64.b64decode(msg.data.audio))
                elif isinstance(msg, EventResponse):
                    if msg.data.event_type == "final":
                        await self.audio_out.put(None)   # sentinel
                        break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[tts] reader died: {e}")
            await self.audio_out.put(None)

    async def say(self, text: str):
        await self._ws.convert(text)

    async def finish(self):
        await self._ws.flush()

    async def close(self):
        if self._reader:
            self._reader.cancel()
        try:
            await self._cm.__aexit__(None, None, None)
        except Exception:
            pass