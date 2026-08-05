"""
Sarvam TTS — streaming (WebSocket) with a REST fallback.

Two ways to synthesize:

  1. SarvamTTSStream  — persistent WebSocket, one instance per assistant turn.
                        Text goes in progressively, PCM audio comes back
                        progressively. This is the low-latency path.

  2. synthesize()     — the old REST call. Kept as a safety net so a demo
                        never goes silent if the socket fails.

Audio format note
-----------------
The streaming socket supports **MP3 only** — the SDK docstring for
`output_audio_codec` says so explicitly, and sending "pcm" returns a 422
from the server. So chunks arrive as MP3 frames and the browser decodes
each one with `decodeAudioData` before scheduling it.

This costs a decode step per chunk versus raw PCM, but it's what the API
allows. The client still schedules decoded buffers on an explicit timeline,
so playback stays gapless and remains cancellable for barge-in.

The REST fallback returns a self-describing WAV, so the client needs
handlers for both message types.

Docs: https://docs.sarvam.ai/api-reference-docs/api-guides-tutorials/text-to-speech/streaming-api/web-socket
"""

import asyncio
import base64
import time
from typing import AsyncIterator, Optional

import httpx

from config import get_settings
import prosody

# ── Voice / model config ──────────────────────────────────────────────────
TTS_MODEL = "bulbul:v3"
TTS_SPEAKER = "ishita"
TTS_LANGUAGE = "hi-IN"
TTS_PACE = 0.9

# Pronunciation dictionary ID. bulbul:v3 only.
#
# This is the single biggest remaining quality lever and it is currently
# unused. TTS guesses at Sanskrit — गच्छति, पठति, बालकः — because they aren't
# Hindi words, and no amount of pace or punctuation shaping fixes a
# mispronounced word. A dictionary is the only thing that does.
#
# Build one with client.pronunciation_dictionary (see the SDK), add the
# Sanskrit vocabulary from data/sentences.py, and put its ID here.
TTS_DICT_ID: str | None = None

# bulbul:v3's native sample rate. Supported values: 8000, 16000, 22050, 24000.
SAMPLE_RATE = 24000

# MP3 bitrate for streamed chunks. Higher = better quality, more bandwidth.
MP3_BITRATE = "128k"

# Sarvam's server-side buffer. Valid range 30–200. Low = lower time-to-first-
# audio, at the cost of slightly less prosodic context per synthesis unit.
MIN_BUFFER_SIZE = 30
MAX_CHUNK_LENGTH = 120

# REST-only: Sarvam rejects requests over this length with a 400.
MAX_TTS_CHARS = 2500


# ══════════════════════════════════════════════════════════════════════════
#  Streaming TTS
# ══════════════════════════════════════════════════════════════════════════

class SarvamTTSStream:
    """One instance per assistant turn. Owns a Sarvam TTS WebSocket.

    Usage:
        tts = SarvamTTSStream()
        await tts.open()
        # ... consumer task drains tts.audio_out ...
        await tts.say("नमस्ते!")
        await tts.say("आज हम एक वाक्य पढ़ेंगे।")
        await tts.finish()          # flush — server will emit the 'final' event
        await tts.close()

    audio_out yields raw PCM byte chunks, then a single None sentinel when
    synthesis is complete (or has failed).
    """

    def __init__(
        self,
        speaker: str = TTS_SPEAKER,
        language: str = TTS_LANGUAGE,
        pace: float = TTS_PACE,
    ):
        settings = get_settings()
        self.speaker = speaker
        self.language = language
        self.pace = pace
        self.audio_out: asyncio.Queue = asyncio.Queue()

        # Imported lazily so a missing `sarvamai` install surfaces as a clean
        # error at turn time (and falls back to REST) rather than at import.
        from sarvamai import AsyncSarvamAI

        self._client = AsyncSarvamAI(api_subscription_key=settings.sarvam_api_key)
        self._cm = None
        self._ws = None
        self._reader: Optional[asyncio.Task] = None
        self._closed = False

    async def open(self):
        # A fresh websocket + configure() handshake runs on every single turn,
        # so its cost is a floor under time-to-first-audio that no model change
        # can touch. Measure it before optimising anything upstream.
        t_open = time.perf_counter()

        # NOTE: send_completion_event takes the STRING 'true', not a bool.
        # Passing True means the 'final' event never fires and the reader
        # loop hangs until timeout.
        self._cm = self._client.text_to_speech_streaming.connect(
            model=TTS_MODEL,
            send_completion_event="true",
        )
        self._ws = await self._cm.__aenter__()

        try:
            await self._ws.configure(
                target_language_code=self.language,
                speaker=self.speaker,
                pace=self.pace,
                speech_sample_rate=SAMPLE_RATE,
                min_buffer_size=MIN_BUFFER_SIZE,
                max_chunk_length=MAX_CHUNK_LENGTH,
                # Normalises English words and numeric entities. This text is
                # Hinglish with digits in it — helpline numbers, sentence
                # counts — and without this they get read as raw characters.
                # The REST fallback already set it; the socket didn't, so the
                # two paths sounded different.
                enable_preprocessing=True,
                dict_id=TTS_DICT_ID,
                # MP3 ONLY. The SDK docstring is explicit: "currently supports
                # MP3 only". Sending "pcm" gets a 422 from the server. This is
                # why the client can't use a raw-PCM scheduler.
                output_audio_codec="mp3",
                output_audio_bitrate=MP3_BITRATE,
            )
        except TypeError as e:
            # SDK version doesn't accept one of these kwargs. Don't silently
            # fall back to a different codec — the client scheduler assumes
            # PCM. Bail so the caller uses the REST path instead.
            await self.close()
            raise RuntimeError(
                f"[TTS] configure() rejected a parameter ({e}). "
                f"Upgrade the sarvamai SDK, or drop to a raw websockets client."
            ) from e

        self._reader = asyncio.create_task(self._read_loop())
        print(f"[TTS] socket ready in {int((time.perf_counter()-t_open)*1000)}ms")

    async def _read_loop(self):
        from sarvamai import AudioOutput, EventResponse, ErrorResponse

        first = True
        try:
            async for msg in self._ws:
                if first:
                    # One-time shape check. Delete once streaming is confirmed.
                    print(f"[TTS] first msg type={type(msg).__name__}")
                    first = False
                if isinstance(msg, AudioOutput):
                    await self.audio_out.put(base64.b64decode(msg.data.audio))
                elif isinstance(msg, ErrorResponse):
                    # Sarvam-side error (bad speaker, bad codec, etc). Without
                    # this branch it's silently ignored and you get n/a audio
                    # with no clue why.
                    print(f"[TTS] server error: {getattr(msg, 'data', msg)}")
                    break
                elif isinstance(msg, EventResponse):
                    if getattr(msg.data, "event_type", None) == "final":
                        break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[TTS] reader loop ended: {e}")
        finally:
            await self.audio_out.put(None)

    async def set_pace(self, pace: float):
        """Change delivery speed mid-utterance.

        The SDK docstring is explicit that a config message can be sent at any
        point in the socket's life, and that it flushes whatever is buffered
        before applying. That's exactly what we want at a chunk boundary — the
        previous sentence finishes at its own pace, the next one starts at the
        new one.

        Nothing else in this app used that. It's the only way to vary delivery
        within a turn, since bulbul has no SSML.
        """
        if abs(pace - self.pace) < 0.02:
            return          # not worth a round trip
        self.pace = pace
        try:
            await self._ws.configure(
                target_language_code=self.language,
                speaker=self.speaker,
                pace=pace,
                speech_sample_rate=SAMPLE_RATE,
                min_buffer_size=MIN_BUFFER_SIZE,
                max_chunk_length=MAX_CHUNK_LENGTH,
                # A config update REPLACES the config — omitting these here
                # would silently reset preprocessing and the dictionary the
                # moment the pace changed mid-sentence.
                enable_preprocessing=True,
                dict_id=TTS_DICT_ID,
                output_audio_codec="mp3",
                output_audio_bitrate=MP3_BITRATE,
            )
        except Exception as e:
            # Delivery is a nicety; losing the turn over it is not acceptable.
            print(f"[TTS] pace change ignored: {e}")

    async def say(self, text: str):
        """Push a speakable chunk. Returns as soon as it's sent — audio comes
        back asynchronously on audio_out."""
        if text.strip():
            await self._ws.convert(text)

    async def finish(self):
        """Flush the server buffer. Triggers the 'final' completion event."""
        await self._ws.flush()

    async def close(self):
        if self._closed:
            return
        self._closed = True
        if self._reader and not self._reader.done():
            self._reader.cancel()
        if self._cm is not None:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════
#  Text shaping — what actually gets spoken, and in what size pieces
# ══════════════════════════════════════════════════════════════════════════

class SpokenTextFilter:
    """Strips '(English gloss)' spans from a *token stream*.

    Your old regex version couldn't survive streaming — an opening paren and
    its closer routinely arrive in different tokens. This tracks depth
    character by character across the whole stream, so the tutor speaks Hindi
    only while the full text still renders on screen.
    """

    def __init__(self):
        self._depth = 0

    def feed(self, text: str) -> str:
        out = []
        for ch in text:
            if ch == "(":
                self._depth += 1
            elif ch == ")":
                if self._depth:
                    self._depth -= 1
            elif self._depth == 0:
                out.append(ch)
        return "".join(out)


# Devanagari danda first — Gemini uses it constantly when writing Hindi, and
# a chunker without it will simply never fire on a Devanagari-only reply.
_HARD = "।.?!\n"
_SOFT = ",;:—"

# Chunk sizes ramp up, and the first value is NOT arbitrary — it matches
# MIN_BUFFER_SIZE.
#
# It used to be 12. That bought nothing: the server buffers until
# MIN_BUFFER_SIZE characters have arrived before it synthesises anything, so a
# 12-character chunk just sat there waiting for the next one. We paid for it
# twice — no latency saved, and bulbul got a fragment of a clause to build an
# intonation contour from, which is why the opening words always sounded
# flattest.
#
# Prosody needs a whole phrase. If you lower MIN_BUFFER_SIZE, lower this with
# it; below the server's threshold the chunk is invisible.
_CHUNK_RAMP = [MIN_BUFFER_SIZE, 60, 100]


def _min_len_for(idx: int) -> int:
    return _CHUNK_RAMP[min(idx, len(_CHUNK_RAMP) - 1)]


# If the model writes a long run with no punctuation at all, the chunker would
# otherwise hold everything until the stream ends and the whole streaming win
# evaporates. Past this length, cut at the last space instead.
_FORCE_CUT = 180


def _find_cut(buf: str, min_len: int, chars: str) -> Optional[int]:
    for i, ch in enumerate(buf):
        if i + 1 >= min_len and ch in chars:
            return i + 1

    # Punctuation never arrived — fall back to a word boundary.
    if len(buf) >= _FORCE_CUT:
        space = buf.rfind(" ", 0, _FORCE_CUT)
        return space + 1 if space > min_len else _FORCE_CUT
    return None


import re

# Sarvam rejects any chunk that contains no character from a supported
# language: "Text must contain at least one character from the allowed
# languages." An aggressive first cut can easily produce a chunk that is pure
# punctuation, digits or whitespace — and because one bad chunk kills the whole
# socket, that costs the entire turn. Length alone is not a safe cut criterion;
# the chunk has to actually contain a letter.
_SPEAKABLE = re.compile(r"[\u0900-\u097FA-Za-z]")


def _is_speakable(text: str) -> bool:
    return bool(_SPEAKABLE.search(text))


async def pipe_text_to_tts(token_iter: AsyncIterator[str], tts: SarvamTTSStream):
    """Consume LLM tokens, cut them into speakable chunks, feed the socket.

    Applies SpokenTextFilter internally, so pass raw tokens — the caller can
    still forward the unfiltered text to the UI.
    """
    spoken = SpokenTextFilter()
    buf = ""
    idx = 0

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
                # Punctuation-only fragment. Don't send it and don't drop it —
                # glue it onto the next chunk so nothing is lost from the reply.
                buf = piece + buf
                break
            # Shape punctuation and pick a pace for THIS sentence. Both are
            # deterministic string work — no model call, no latency.
            spoken_text, pace = prosody.shape(piece)
            await tts.set_pace(pace)
            await tts.say(spoken_text)
            idx += 1

    tail = buf.strip()
    if tail and _is_speakable(tail):
        spoken_text, pace = prosody.shape(tail)
        await tts.set_pace(pace)
        await tts.say(spoken_text)

    await tts.finish()


async def single_shot(text: str) -> AsyncIterator[str]:
    """Adapter so fixed strings (greetings, praise) use the same pipeline."""
    yield text


# ══════════════════════════════════════════════════════════════════════════
#  REST fallback
# ══════════════════════════════════════════════════════════════════════════

def _prepare_tts_text(text: str) -> str:
    """Trim TTS input to Sarvam's hard limit, cutting at a sentence end."""
    text = (text or "").strip()
    if len(text) <= MAX_TTS_CHARS:
        return text

    clipped = text[:MAX_TTS_CHARS]
    for sep in ("।", ".", "!", "?", "\n"):
        i = clipped.rfind(sep)
        if i > 0:
            return clipped[: i + 1].strip()
    return clipped.strip()


async def synthesize(text: str, language_code: str = TTS_LANGUAGE) -> bytes:
    """One-shot REST synthesis. Returns WAV bytes."""
    settings = get_settings()

    text = _prepare_tts_text(text)
    if not text:
        raise ValueError("[TTS] Empty text after preparation — nothing to synthesize")

    payload = {
        "text": text,
        "target_language_code": language_code,
        "speaker": TTS_SPEAKER,
        "model": TTS_MODEL,
        "pace": TTS_PACE,
        "speech_sample_rate": SAMPLE_RATE,
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
    return base64.b64decode(data["audios"][0])