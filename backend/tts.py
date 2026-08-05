"""
TTS layer — ElevenLabs streaming.

DROP-IN REPLACEMENT FOR tts_sarvam.py
-------------------------------------
The public surface is identical:

    SarvamTTSStream  → ElevenLabsTTSStream   (same interface)
    pipe_text_to_tts → unchanged
    single_shot      → unchanged
    synthesize       → unchanged
    SpokenTextFilter → unchanged

ws.py imports `tts.SarvamTTSStream` by name, so the class is aliased at the
bottom: `SarvamTTSStream = ElevenLabsTTSStream`. Nothing else needs to change.

WHY THIS IS SIMPLER
-------------------
Sarvam required: open a websocket → configure() → send text chunks → read
audio messages → watch for a 'final' event → handle ErrorResponse. Five
failure modes, three of which were string-vs-bool traps in the SDK.

ElevenLabs: call convert() with text, iterate the AsyncIterator[bytes].
That's it. The SDK does all the HTTP/SSE work internally. No socket to manage,
no configure, no completion events.

VOICE / MODEL SELECTION
-----------------------
Model: eleven_flash_v2_5  — lowest latency, Hindi supported, <75ms TTFB.
       eleven_multilingual_v2 — better quality, higher latency.

Voice: pick a voice_id from the ElevenLabs voice library.

API key: set ELEVENLABS_API_KEY in your .env file.
"""

import asyncio
import base64
import re
import time
from typing import AsyncIterator, Optional

from elevenlabs import AsyncElevenLabs
from elevenlabs.types import VoiceSettings

from config import get_settings
import prosody

# ── Voice / model config ──────────────────────────────────────────────────────

# eleven_flash_v2_5: fastest, <75ms TTFB, Hindi supported.
# eleven_multilingual_v2: highest quality, ~2-3x slower.
TTS_MODEL = "eleven_flash_v2_5"

# Output format. Must match the client's MediaSource codec.
# mp3_24000_48 = MP3 at 24kHz, 48kbps.
TTS_OUTPUT_FORMAT = "mp3_24000_48"

# Pick from the ElevenLabs voice library. Replace with the voice_id you chose.
TTS_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"   # "Sarah" — warm, clear female

# Hindi. ElevenLabs auto-detects, but pinning avoids occasional misdetect
# on short Hinglish phrases.
TTS_LANGUAGE = "hi"

# Voice tuning knobs.
#
# stability: 0.0-1.0. Lower = more expressive. Higher = more consistent.
# similarity_boost: 0.0-1.0. How closely to match the original voice sample.
# style: 0.0-1.0. Expressiveness. Keep low for a tutor.
# speed: 0.5-2.0. Same concept as Sarvam's pace.
DEFAULT_VOICE_SETTINGS = VoiceSettings(
    stability=0.5,
    similarity_boost=0.75,
    style=0.3,
    speed=0.92,
)

# Text normalization: 'auto', 'on', 'off'.
TTS_TEXT_NORMALIZATION = "on"

# ── Client ────────────────────────────────────────────────────────────────────

_client: Optional[AsyncElevenLabs] = None


def _get_client() -> AsyncElevenLabs:
    global _client
    if _client is None:
        _client = AsyncElevenLabs(
            api_key=get_settings().elevenlabs_api_key,
        )
    return _client


def _settings_for(speed: float, stability: float, style: float) -> VoiceSettings:
    """Full per-chunk voice settings.

    ElevenLabs modulation is stability + style, not just speed:
      stability  low  = more expressive, varied intonation
                 high = flatter, more consistent
      style      high = more emotional/dramatic delivery

    A question wants lower stability so the pitch actually rises. A correction
    wants higher stability so it lands as calm and clear, not theatrical.
    That's real voice modulation — the thing Sarvam's single `pace` knob
    couldn't do.
    """
    return VoiceSettings(
        stability=stability,
        similarity_boost=DEFAULT_VOICE_SETTINGS.similarity_boost,
        style=style,
        speed=speed,
    )


# ── Spoken-text filter ────────────────────────────────────────────────────────

class SpokenTextFilter:
    """Strip parenthetical English glosses so only the spoken part reaches TTS.

    LLM writes: "राम वन जाते हैं। (Ram goes to the forest.)"
    TTS should say: "राम वन जाते हैं।"
    """

    _PAREN = re.compile(r"\s*\([^)]*\)")

    def __init__(self):
        self._buf = ""

    def feed(self, text: str) -> str:
        self._buf += text
        out = self._PAREN.sub("", self._buf)
        idx = self._buf.rfind("(")
        if idx != -1 and ")" not in self._buf[idx:]:
            clean = self._PAREN.sub("", self._buf[:idx])
            self._buf = self._buf[idx:]
            return clean
        self._buf = ""
        return out


# ── Streaming TTS class ──────────────────────────────────────────────────────

class ElevenLabsTTSStream:
    """One instance per turn.

    Interface is identical to the Sarvam version so ws.py doesn't change:
        stream = ElevenLabsTTSStream()
        await stream.open()
        stream.audio_out             # asyncio.Queue of bytes | None
        await stream.say(text)
        await stream.finish()
        await stream.close()
    """

    def __init__(self):
        self.audio_out: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._voice = (DEFAULT_VOICE_SETTINGS.speed, DEFAULT_VOICE_SETTINGS.stability, DEFAULT_VOICE_SETTINGS.style)
        # Chunks are synthesised strictly in order. The previous design fired
        # each say() as an independent task, so several HTTP requests raced and
        # whichever returned first got queued first — that's why line two
        # sometimes played before line one, and why boundaries clicked. A
        # single worker draining a queue guarantees order.
        self._work: asyncio.Queue = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None

    async def open(self):
        """Warm the HTTP connection pool and start the ordered worker."""
        t0 = time.perf_counter()
        try:
            _get_client()
        except Exception as e:
            print(f"[TTS] client init failed: {e}")
        self._worker = asyncio.create_task(self._run_worker())
        print(f"[TTS] ready in {int((time.perf_counter() - t0) * 1000)}ms")

    async def _run_worker(self):
        """Pull queued items and synthesise them one at a time, in order.

        Tracks the previous chunk's text and hands it to the next call as
        `previous_text`. That's the fix for choppy boundaries: ElevenLabs uses
        it to keep intonation continuous across chunks instead of restarting
        the contour cold each time, which is what made it sound cut-cut."""
        prev_text = ""
        while True:
            item = await self._work.get()
            if item is None:
                break
            text, voice = item
            if not self._closed:
                await self._stream_chunk(text, voice, prev_text)
                prev_text = text
        await self.audio_out.put(None)

    async def set_voice(self, settings: tuple[float, float, float]):
        """Set (speed, stability, style) for the next say()."""
        self._voice = settings

    async def say(self, text: str):
        """Queue one chunk for synthesis. The worker speaks them in order."""
        if self._closed or not text.strip():
            return
        await self._work.put((text, self._voice))

    async def _stream_chunk(self, text: str, voice: tuple, prev_text: str = ""):
        first = True
        speed, stability, style = voice
        try:
            client = _get_client()
            settings = _settings_for(speed, stability, style)
            async for chunk in client.text_to_speech.convert(
                voice_id=TTS_VOICE_ID,
                text=text,
                model_id=TTS_MODEL,
                output_format=TTS_OUTPUT_FORMAT,
                language_code=TTS_LANGUAGE,
                voice_settings=settings,
                apply_text_normalization=TTS_TEXT_NORMALIZATION,
                # Continuity across chunk boundaries — stops the choppy restart.
                previous_text=prev_text or None,
            ):
                if chunk and not self._closed:
                    if first:
                        first = False
                        print(f"[TTS] first msg (speed={speed:.2f})")
                    await self.audio_out.put(chunk)
        except Exception as e:
            print(f"[TTS] chunk synthesis failed: {e}")

    async def finish(self):
        """Signal no more chunks; the worker drains what's queued, in order."""
        await self._work.put(None)

    async def close(self):
        """Stop the worker and unblock the reader."""
        self._closed = True
        if self._worker and not self._worker.done():
            await self._work.put(None)
            try:
                await asyncio.wait_for(self._worker, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._worker.cancel()
        try:
            self.audio_out.put_nowait(None)
        except asyncio.QueueFull:
            pass


# ── Alias for ws.py compatibility ─────────────────────────────────────────────
SarvamTTSStream = ElevenLabsTTSStream


# ── Sentence chunker + prosody ────────────────────────────────────────────────

_SPEAKABLE = re.compile(r"[\u0900-\u097FA-Za-z]")


def _is_speakable(text: str) -> bool:
    return bool(_SPEAKABLE.search(text))


_HARD = "।.?!\n"
_SOFT = ",;:—"

MIN_BUFFER_SIZE = 30
MAX_CHUNK_LENGTH = 120

_CHUNK_RAMP = [MIN_BUFFER_SIZE, 60, 100]


def _min_len_for(idx: int) -> int:
    return _CHUNK_RAMP[min(idx, len(_CHUNK_RAMP) - 1)]


_FORCE_CUT = 180


def _find_cut(buf: str, min_len: int, chars: str) -> Optional[int]:
    for i, ch in enumerate(buf):
        if i + 1 >= min_len and ch in chars:
            return i + 1
    if len(buf) >= _FORCE_CUT:
        space = buf.rfind(" ", 0, _FORCE_CUT)
        return space + 1 if space > min_len else _FORCE_CUT
    return None


async def pipe_text_to_tts(token_iter: AsyncIterator[str], tts_stream: ElevenLabsTTSStream,
                           calm: bool = False):
    """Accumulate tokens into sentence-sized chunks, shape them, and speak.

    `calm=True` forces the steady scripted-line delivery — used for greetings,
    the classroom transition, and announcements, which should never come out
    with the excited intonation a question or praise word would trigger.
    """
    spoken = SpokenTextFilter()
    buf = ""
    idx = 0
    force = "calm" if calm else None

    async for token in token_iter:
        buf += spoken.feed(token)
        while True:
            cut = _find_cut(
                buf,
                min_len=_min_len_for(idx),
                chars=(_HARD + _SOFT) if idx == 0 else _HARD,
            )
            if cut is None:
                break
            piece = buf[:cut].strip()
            buf = buf[cut:]
            if not piece:
                continue
            if not _is_speakable(piece):
                buf = piece + buf
                break
            # Fillers only on real explanations, never on scripted calm lines.
            spoken_text, voice = prosody.shape(
                piece, is_first_chunk=(idx == 0 and not calm), force_kind=force)
            await tts_stream.set_voice(voice)
            await tts_stream.say(spoken_text)
            idx += 1

    tail = buf.strip()
    if tail and _is_speakable(tail):
        spoken_text, voice = prosody.shape(
            tail, is_first_chunk=(idx == 0 and not calm), force_kind=force)
        await tts_stream.set_voice(voice)
        await tts_stream.say(spoken_text)

    await tts_stream.finish()


async def single_shot(text: str) -> AsyncIterator[str]:
    """Wrap a fixed string as a one-token stream for pipe_text_to_tts."""
    yield text


async def synthesize(text: str) -> bytes:
    """One-shot synthesis returning a complete audio blob. REST fallback."""
    client = _get_client()
    chunks = []
    async for chunk in client.text_to_speech.convert(
        voice_id=TTS_VOICE_ID,
        text=text,
        model_id=TTS_MODEL,
        output_format=TTS_OUTPUT_FORMAT,
        language_code=TTS_LANGUAGE,
        voice_settings=DEFAULT_VOICE_SETTINGS,
        apply_text_normalization=TTS_TEXT_NORMALIZATION,
    ):
        if chunk:
            chunks.append(chunk)
    return b"".join(chunks)