"""
Streaming speech-to-text over Sarvam's realtime socket, with VAD signals.

WHY THIS REPLACES THE BATCH PATH
--------------------------------
The old path was: record → user clicks Stop → POST the whole WAV → wait.
Measured cost was 783-2442ms, and critically it did NOT scale with audio size
(2442ms on a 58KB clip, 890ms on 188KB). That is not compute — it is
per-request connection and queue overhead on the REST endpoint. You cannot
tune it away; you can only stop paying it every turn.

This module opens ONE socket for the whole session and streams mic audio into
it continuously. Transcription happens while the student is still speaking, so
the only latency left after they stop is the tail.

VAD IS THE BIGGER WIN
---------------------
`vad_signals` makes the server tell us when speech starts and stops:

    START_SPEECH → the student began talking. If the tutor is mid-sentence,
                   this is the barge-in trigger: stop playback NOW.
    END_SPEECH   → the student stopped. This replaces the Stop button.

The Stop button was never really a latency problem — it was an interaction
problem. Ixigo's Tara has no Stop button, and that absence is most of why it
feels alive. You just talk.

API SHAPES (verified against sarvamai SDK, not docs)
----------------------------------------------------
Response envelope: {"type": "data"|"events"|"error", "data": {...}}
  data   → .transcript, .language_code, .metrics
  events → .signal_type in ("START_SPEECH", "END_SPEECH")
  error  → .error, .code

Two traps, both the same shape as the TTS ones:
  - `vad_signals` and `flush_signal` take the STRING "true", not a bool.
  - `sample_rate` is a STRING on connect, but an INT on transcribe().

RECONNECT STRATEGY
------------------
Sarvam closes the socket with 1011 (internal error / keepalive ping timeout)
in two situations:
  1. The socket was idle too long (student not speaking, tutor is playing audio
     back — no PCM being fed, so Sarvam sees a dead connection).
  2. The session hit the server-side max duration (~5 min).

Three defences, applied in order:

  a. Silence keepalive — send 100ms of silence frames every ~3 seconds while
     the tutor is speaking. Keeps the TCP connection warm and prevents idle
     timeout. Call stt.keep_alive() from your ws.py tutor-playback loop.

  b. Proactive reconnect — at SESSION_MAX_SECONDS (4.5 min) we reconnect
     before Sarvam forces it. This covers the duration limit without waiting
     for an error.

  c. Reactive reconnect — if feed() gets a 1011/1006/keepalive error anyway
     (e.g. a genuine network hiccup), we tear down the dead socket, open a
     fresh one, and retry the current chunk once. The student never notices.
"""

import asyncio
import base64
import struct
import time
from typing import AsyncIterator, Optional

from sarvamai import AsyncSarvamAI

from config import get_settings

# ── Config ────────────────────────────────────────────────────────────────

STT_MODEL = "saaras:v3"          # "saaras:v4" is newer; try it once v3 is stable
SAMPLE_RATE = 16000

# codemix keeps English in Latin script and Indic words in Devanagari:
#   transcribe → "द बॉय रीड्स अ बुक"
#   codemix    → "The boy reads a book"
#
# Your tutor grades English translations, so transcribe mode was handing the
# LLM Devanagari-spelled English and asking it to guess the original words.
# This also does most of the work for step 4 (spoken Hinglish).
STT_MODE = "codemix"

# Pinned to Hindi, NOT "unknown".
#
# Auto-detect returned lang=kn-IN (Kannada) on a Hindi/Sanskrit session. Sanskrit
# read aloud shares enough phonetics with several Indic languages that per-
# utterance detection genuinely gets it wrong, and a wrong language means a
# wrong transcript — the student gets marked incorrect for a correct answer.
#
# Pinning costs nothing here because codemix already keeps English in Latin
# script within a Hindi stream, which is the only other language in play.
STT_LANGUAGE = "hi-IN"

# Raw 16-bit little-endian PCM — exactly what the browser's ScriptProcessor
# produces after Float32 → Int16 conversion. No WAV header needed on a stream.
INPUT_CODEC = "pcm_s16le"

# VAD tuning.
#
# START_SPEECH_VOLUME_THRESHOLD is the important one. Leaving it unset means
# NO volume filtering at all — Sarvam treats a conversation two rooms away as
# speech and fires START_SPEECH, which cancels whatever Vidya was saying.
#
# The value is dB below which audio is ignored. Speech into a laptop mic sits
# around -20 to -30 dB; background chatter is usually below -45. Start at -40
# and move it toward -30 if the room is still leaking in, or toward -50 if a
# soft-spoken student gets ignored.
START_SPEECH_VOLUME_THRESHOLD: Optional[str] = "-40"

# Minimum speech frames before a barge-in counts. A door slam or a single
# distant word is short; a student actually interrupting keeps going. Raising
# this trades a slightly later interruption for far fewer false ones.
INTERRUPT_MIN_SPEECH_FRAMES: Optional[str] = "8"

# Explicitly off: the high-sensitivity preset is the opposite of what a room
# with background noise needs.
HIGH_VAD_SENSITIVITY: Optional[str] = "false"

# Frames of silence before END_SPEECH. Left at the server default — raise it if
# the tutor cuts off students who pause to think mid-sentence.
NEGATIVE_FRAMES_COUNT: Optional[str] = None

# Proactive reconnect before Sarvam's server-side session limit.
# Set to 4.5 min so we reconnect cleanly before the ~5 min hard cutoff.
_SESSION_MAX_SECONDS = 270

# 100ms of silence at 16kHz = 1600 samples × 2 bytes (signed 16-bit LE).
# Used by keep_alive() to prevent idle-timeout while the tutor is speaking.
_SILENCE_FRAME = struct.pack("<" + "h" * 1600, *([0] * 1600))

# Error substrings that indicate a dead socket worth reconnecting.
_RECONNECT_SIGNALS = ("1011", "1006", "keepalive", "ping timeout", "connection closed")


class SarvamSTTStream:
    """One socket per SESSION. Not per turn — that's the whole point.

    Usage:
        stt = SarvamSTTStream()
        await stt.open()
        asyncio.create_task(consume(stt.events()))
        await stt.feed(pcm_bytes)     # called continuously from the ws loop
        await stt.keep_alive()        # call every ~3s while tutor is speaking
        ...
        await stt.close()
    """

    def __init__(self):
        settings = get_settings()
        self._client = AsyncSarvamAI(api_subscription_key=settings.sarvam_api_key)
        self._cm = None
        self._ws = None
        self._reader: Optional[asyncio.Task] = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self.speaking = False        # True between START_SPEECH and END_SPEECH
        self._session_start = 0.0
        self._reconnecting = False   # guard against concurrent reconnects

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def open(self):
        t0 = time.perf_counter()

        kwargs = dict(
            language_code=STT_LANGUAGE,
            model=STT_MODEL,
            mode=STT_MODE,
            sample_rate=str(SAMPLE_RATE),    # STRING here
            input_audio_codec=INPUT_CODEC,
            vad_signals="true",              # STRING, not True
            flush_signal="true",             # STRING, not True
        )
        if HIGH_VAD_SENSITIVITY is not None:
            kwargs["high_vad_sensitivity"] = HIGH_VAD_SENSITIVITY
        if NEGATIVE_FRAMES_COUNT is not None:
            kwargs["negative_frames_count"] = NEGATIVE_FRAMES_COUNT
        if START_SPEECH_VOLUME_THRESHOLD is not None:
            kwargs["start_speech_volume_threshold"] = START_SPEECH_VOLUME_THRESHOLD
        if INTERRUPT_MIN_SPEECH_FRAMES is not None:
            kwargs["interrupt_min_speech_frames"] = INTERRUPT_MIN_SPEECH_FRAMES

        self._cm = self._client.speech_to_text_streaming.connect(**kwargs)
        self._ws = await self._cm.__aenter__()
        self._session_start = time.perf_counter()

        self._reader = asyncio.create_task(self._read_loop())
        print(f"[STT] socket ready in {int((time.perf_counter()-t0)*1000)}ms "
              f"(model={STT_MODEL} mode={STT_MODE} "
              f"vol_thresh={START_SPEECH_VOLUME_THRESHOLD}dB "
              f"interrupt_frames={INTERRUPT_MIN_SPEECH_FRAMES})")

    async def _reconnect(self):
        """Tear down the dead socket and open a fresh one.

        Sets _reconnecting to block concurrent reconnect attempts — if two
        feed() calls race on the same dead socket we only want one reconnect.
        """
        if self._reconnecting:
            # Another coroutine is already reconnecting; wait for it to finish.
            while self._reconnecting:
                await asyncio.sleep(0.05)
            return

        self._reconnecting = True
        print("[STT] reconnecting...")
        try:
            if self._reader:
                self._reader.cancel()
                try:
                    await self._reader
                except (asyncio.CancelledError, Exception):
                    pass
                self._reader = None

            if self._cm:
                try:
                    await self._cm.__aexit__(None, None, None)
                except Exception:
                    pass
                self._cm = None
                self._ws = None

            self._closed = False
            await self.open()
            print("[STT] reconnected")
        except Exception as e:
            print(f"[STT] reconnect failed: {e}")
            await self._queue.put({"type": "error", "message": f"reconnect failed: {e}"})
        finally:
            self._reconnecting = False

    async def close(self):
        if self._closed:
            return
        self._closed = True

        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass

        if self._cm:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception as e:
                print(f"[STT] close error (ignored): {e}")

        await self._queue.put(None)   # unblock any consumer

    # ── Audio in ─────────────────────────────────────────────────────────

    async def feed(self, pcm: bytes):
        """Push one chunk of raw 16-bit PCM. Safe to call continuously.

        Handles three failure modes transparently:
          - Proactive reconnect when the session is near its max age.
          - Reactive reconnect on 1011/1006/keepalive errors.
          - One retry on the current chunk after a successful reconnect.
        """
        if self._closed or not self._ws or not pcm:
            return

        # (b) Proactive reconnect — before Sarvam forces a 1011.
        if time.perf_counter() - self._session_start > _SESSION_MAX_SECONDS:
            print("[STT] proactive reconnect (session age limit)")
            await self._reconnect()

        try:
            await self._ws.transcribe(
                audio=base64.b64encode(pcm).decode("ascii"),
                # Do NOT pass the codec here. This field is a strict
                # Literal['audio/wav'] and rejects anything else, even when
                # the stream is raw PCM. The actual format is declared once at
                # connect() via input_audio_codec; this per-message field is
                # vestigial and must always be left at its default.
                sample_rate=SAMPLE_RATE,     # INT here, unlike connect()
            )
        except Exception as e:
            err = str(e).lower()
            print(f"[STT] feed failed: {e}")

            # (c) Reactive reconnect on dead-socket errors.
            if any(sig in err for sig in _RECONNECT_SIGNALS):
                try:
                    await self._reconnect()
                    # Retry the chunk that failed — student shouldn't lose audio.
                    await self._ws.transcribe(
                        audio=base64.b64encode(pcm).decode("ascii"),
                        sample_rate=SAMPLE_RATE,
                    )
                except Exception as e2:
                    print(f"[STT] retry after reconnect failed: {e2}")
                    await self._queue.put({"type": "error", "message": str(e2)})
            else:
                await self._queue.put({"type": "error", "message": str(e)})

    async def keep_alive(self):
        """Send a silence frame to prevent idle-timeout while tutor is speaking.

        Call this from your ws.py playback loop every ~3 seconds:

            while tutor_is_playing:
                await asyncio.sleep(3)
                await stt.keep_alive()

        100ms of silence is enough to reset the server's idle timer without
        triggering VAD or affecting transcription quality.
        """
        await self.feed(_SILENCE_FRAME)

    async def flush(self):
        """Force the server to finalise whatever audio it is holding.

        Needed when VAD misses the end of an utterance — for example when the
        student trails off very quietly.
        """
        if self._closed or not self._ws:
            return
        try:
            await self._ws.flush()
        except Exception as e:
            print(f"[STT] flush failed: {e}")

    # ── Events out ───────────────────────────────────────────────────────

    async def events(self) -> AsyncIterator[dict]:
        """Yield normalised events until the socket closes.

        Emits:
            {"type": "speech_start"}
            {"type": "speech_end"}
            {"type": "transcript", "text": str, "language": str | None}
            {"type": "error", "message": str}
        """
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def _read_loop(self):
        try:
            while not self._closed:
                msg = await self._ws.recv()
                event = self._normalise(msg)
                if event:
                    await self._queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not self._closed:
                print(f"[STT] reader stopped: {e}")
                await self._queue.put({"type": "error", "message": str(e)})
                await self._queue.put(None)

    def _normalise(self, msg) -> Optional[dict]:
        """Flatten the SDK's tagged union into plain dicts.

        Keeping this translation in one place means ws.py never has to know
        Sarvam's message shapes — which is what makes swapping the STT
        provider later a single-file change.
        """
        kind = getattr(msg, "type", None)
        data = getattr(msg, "data", None)
        if data is None:
            return None

        if kind == "events":
            signal = getattr(data, "signal_type", None)
            if signal == "START_SPEECH":
                self.speaking = True
                return {"type": "speech_start"}
            if signal == "END_SPEECH":
                self.speaking = False
                return {"type": "speech_end"}
            return None

        if kind == "data":
            text = (getattr(data, "transcript", "") or "").strip()
            if not text:
                return None
            return {
                "type": "transcript",
                "text": text,
                "language": getattr(data, "language_code", None),
            }

        if kind == "error":
            return {
                "type": "error",
                "message": f"{getattr(data, 'code', '?')}: {getattr(data, 'error', '')}",
            }

        return None