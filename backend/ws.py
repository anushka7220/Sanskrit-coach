"""
WebSocket handler — the real-time conversation loop.

WHAT CHANGED IN STEP 3 (and why the shape of this file had to change)
---------------------------------------------------------------------
The old loop was strictly sequential:

    receive message → transcribe → run_turn (blocks) → receive next message

That is fine when the client hands you one complete WAV per turn. It is
impossible once audio streams continuously, because `await run_turn(...)`
stops the receive loop — so while the tutor is speaking, the server is deaf.
Barge-in cannot exist in that design.

So the turn is now a *task*, not an awaited call:

    receive loop  ─── mic_chunk ──▶ STT socket (always, even mid-turn)
         │
    stt events  ─── START_SPEECH ──▶ cancel turn task, tell client to stop
                └── transcript   ──▶ start a new turn task

Three things run concurrently: the receive loop, the STT event consumer, and
at most one turn task. The receive loop never blocks on a turn again.

BARGE-IN IS CANCELLATION, NOT MUTING
------------------------------------
When the student interrupts, we cancel the turn task outright. `_speak_stream`
closes the TTS socket in a `finally`, so cancellation releases it cleanly and
the LLM stream is abandoned mid-flight. Muting instead would leave the tutor
"talking" invisibly, burning tokens and TTS credit, and the next turn would
queue behind it.

`turn_id` guards on the client discard any audio still in flight from the
cancelled turn.

ACOUSTIC ECHO — READ THIS
-------------------------
Mic audio is fed to STT *while the tutor is speaking*. That is required for
barge-in, and it means the tutor's own voice can come back through the mic,
trigger START_SPEECH, and cancel its own turn.

The client MUST request the mic with echoCancellation enabled:

    getUserMedia({ audio: { echoCancellation: true,
                            noiseSuppression: true,
                            autoGainControl: true } })

If you still see self-interruption, that is the first thing to check — not
the VAD thresholds.

Client → Server
  { "type": "init",        "level": "easy", "sentence_index": 0 }
  { "type": "mic_chunk",   "data": "<base64 pcm16 @16k>" }   # continuous
  { "type": "mic_stop" }                                     # flush STT
  { "type": "audio_chunk", "data": "<base64 wav>" }          # legacy batch
  { "type": "text",        "text": "what does this mean?" }
  { "type": "move_on" }

Server → Client
  { "type": "turn_start",     "turn_id": "..." }
  { "type": "ai_text_delta",  "turn_id": "...", "text": "..." }
  { "type": "ai_text",        "turn_id": "...", "text": "..." }
  { "type": "audio_chunk",    "turn_id": "...", "data": "<base64 MP3>" }
  { "type": "audio_end",      "turn_id": "..." }
  { "type": "ai_audio",       "turn_id": "...", "data": "<base64 WAV>" }  # fallback
  { "type": "transcript",     "text": "..." }
  { "type": "speech_start" }                                 # VAD, for UI
  { "type": "speech_end" }                                   # VAD, for UI
  { "type": "barge_in" }                                     # stop playback NOW
  { "type": "next_sentence",  "sentence": {...}, "index": N }
  { "type": "session_complete" }
  { "type": "error",          "message": "..." }
"""

import asyncio
import base64
import json
import time
import uuid
from typing import AsyncIterator, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from data.sentences import get_sentences, Level
import stt, tts, orchestrator, llm, safety
from stt_stream import SarvamSTTStream

router = APIRouter(tags=["websocket"])

# If the 'final' completion event never arrives we don't want a turn to hang
# the session forever.
AUDIO_DRAIN_TIMEOUT = 30.0

# How long the classroom is noisy on its own before Vidya cuts in.
#
# This no longer has to be matched against the chatter length. The chatter now
# runs until she speaks and then ducks under her, so overlap is guaranteed —
# this only controls how long the room murmurs before she does.
#
# Lower = she cuts in sooner over louder noise. Higher = the room runs longer
# first. Her voice actually starts ~0.8s after this, since speak() has to open
# a TTS socket and wait for its first chunk.
CHATTER_CUE_SECONDS = 1.0


async def _orchestrate_stream(**kwargs) -> AsyncIterator[dict]:
    """Adapter: streaming orchestrator if available, otherwise wrap the
    existing batch one so this file works unchanged today."""
    streamer = getattr(orchestrator, "process_turn_stream", None)
    if streamer is not None:
        async for event in streamer(**kwargs):
            yield event
        return

    result = await orchestrator.process_turn(**kwargs)
    yield {"type": "token", "text": result["response_text"]}
    yield {
        "type": "meta",
        "move_on": result.get("move_on", False),
        "awaiting_grammar": result.get("awaiting_grammar", False),
        "awaiting_translation": result.get("awaiting_translation", False),
        "change_level": result.get("change_level"),
    }


@router.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()

    # Session state
    level: Level = "easy"
    sentences = []
    sentence_index = 0
    history: list[dict] = []
    # Name and gender, set at init. Gender is not decorative: Hindi verbs
    # agree with the person addressed, so without it Vidya says
    # "आप जानना चाहते हैं" to every student regardless.
    student: dict = {"name": "", "gender": "neutral"}
    awaiting_grammar = False
    awaiting_translation = False

    # Step 3 state
    stt_stream: Optional[SarvamSTTStream] = None
    stt_consumer: Optional[asyncio.Task] = None
    turn_task: Optional[asyncio.Task] = None
    speech_started_at: float = 0.0

    # ── Endpointing ───────────────────────────────────────────────────────
    # Sarvam emits a transcript per detected utterance *segment*, not per turn.
    # A student who pauses to think mid-sentence produces several segments, so
    # firing a turn on every transcript starts a reply the student hasn't
    # finished asking for — which they then talk over, which registers as
    # barge-in and cancels it. That's the "cancelled after 0 chars" loop.
    #
    # So transcripts accumulate, and the turn only fires once the student has
    # been quiet for ENDPOINT_DELAY. Any new speech resets the timer.
    #
    # Tuning: too low and it cuts off thinkers; too high and the tutor feels
    # sluggish. This is the single most demo-visible number in the file.
    ENDPOINT_DELAY = 0.7

    pending_text: list[str] = []
    endpoint_task: Optional[asyncio.Task] = None
    speech_ended_at: float = 0.0

    # What the in-flight turn is answering, and whether it has said anything
    # yet. Both are needed to tell a real interruption from the student simply
    # still talking — and to avoid throwing their words away when it's the
    # latter.
    turn_info: dict = {"text": "", "spoke": False}

    # Diagnostics for the mic path.
    mic_chunks = 0
    mic_bytes = 0

    # websocket.send_text is not safe to call from two tasks at once, and we
    # now have three of them. One lock keeps frames from interleaving.
    send_lock = asyncio.Lock()

    async def send(payload: dict):
        async with send_lock:
            try:
                await websocket.send_text(json.dumps(payload))
            except Exception:
                # Client vanished mid-turn. Let the main loop's disconnect
                # handler deal with it rather than raising inside a task.
                pass

    # ── Audio out ─────────────────────────────────────────────────────────

    async def _pump_audio(stream: "tts.SarvamTTSStream", turn_id: str, marks: dict):
        """Drain audio chunks from the TTS socket straight to the client."""
        while True:
            chunk = await stream.audio_out.get()
            if chunk is None:
                break
            if "first_audio" not in marks:
                marks["first_audio"] = time.perf_counter()
            await send({
                "type": "audio_chunk",
                "turn_id": turn_id,
                "data": base64.b64encode(chunk).decode(),
            })
        await send({"type": "audio_end", "turn_id": turn_id})

    async def _speak_stream(token_iter: AsyncIterator[str], turn_id: str, marks: dict,
                            calm: bool = False):
        """Run one streamed utterance end to end. Raises on socket failure so
        the caller can fall back to REST.

        The `finally` is what makes barge-in safe: when this task is cancelled
        the TTS socket still gets closed.
        """
        stream = tts.SarvamTTSStream()
        await stream.open()
        pump = asyncio.create_task(_pump_audio(stream, turn_id, marks))
        try:
            await tts.pipe_text_to_tts(token_iter, stream, calm=calm)
            await asyncio.wait_for(pump, timeout=AUDIO_DRAIN_TIMEOUT)
        finally:
            if not pump.done():
                pump.cancel()
            await stream.close()

    async def speak(text: str, calm: bool = True):
        """Fixed strings — greetings, praise, sentence announcements.

        calm defaults True: these are scripted lines and should be delivered
        steadily. The excited question/praise modulation is for the LLM's own
        replies, not for "अगला वाक्य" every single time.
        """
        turn_id = uuid.uuid4().hex
        await send({"type": "turn_start", "turn_id": turn_id})
        await send({"type": "ai_text", "turn_id": turn_id, "text": text})
        try:
            await _speak_stream(tts.single_shot(text), turn_id, {}, calm=calm)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[TTS] stream failed, falling back to REST: {e}")
            await _speak_rest(text, turn_id)

    async def _speak_rest(text: str, turn_id: str):
        """Last-resort one-shot synthesis so a demo never goes silent."""
        try:
            spoken = tts.SpokenTextFilter().feed(text)
            if not spoken.strip():
                return
            audio_bytes = await tts.synthesize(spoken)
            await send({
                "type": "ai_audio",
                "turn_id": turn_id,
                "data": base64.b64encode(audio_bytes).decode(),
            })
        except Exception as e:
            print(f"[TTS] REST fallback also failed: {e}")

    # ── One assistant turn ────────────────────────────────────────────────

    async def run_turn(user_text: str, current: dict, t_input: float) -> dict:
        """Stream the orchestrator's reply as text + audio."""
        nonlocal awaiting_grammar, awaiting_translation

        turn_id = uuid.uuid4().hex
        await send({"type": "turn_start", "turn_id": turn_id})

        parts: list[str] = []
        meta: dict = {}
        marks: dict = {"input_ready": t_input}

        async def tokens() -> AsyncIterator[str]:
            async for event in _orchestrate_stream(
                user_text=user_text,
                sentence=current,
                level=level,
                history=history,
                awaiting_grammar=awaiting_grammar,
                awaiting_translation=awaiting_translation,
                student=student,
            ):
                if event.get("type") == "token":
                    text = event.get("text", "")
                    if not text:
                        continue
                    if "first_token" not in marks:
                        marks["first_token"] = time.perf_counter()
                        turn_info["spoke"] = True
                    parts.append(text)
                    await send({
                        "type": "ai_text_delta",
                        "turn_id": turn_id,
                        "text": text,
                    })
                    yield text
                elif event.get("type") == "meta":
                    meta.update(event)
                    if event.get("safety"):
                        # Sent before any audio. The client mutes the mic so
                        # the alert beep — which comes out of the same speakers
                        # the mic is listening to — cannot trigger VAD and
                        # barge Vidya out of her own safety message.
                        await send({"type": "safety_hold", "turn_id": turn_id})

        full_text = ""
        try:
            await _speak_stream(tokens(), turn_id, marks)
            full_text = "".join(parts)
        except asyncio.CancelledError:
            # Barge-in. Record what the tutor actually managed to say so the
            # history reflects the conversation the student really heard.
            partial = "".join(parts).strip()
            if partial:
                history.append({"role": "user", "content": user_text})
                history.append({"role": "assistant",
                                "content": partial + " [interrupted]"})
            print(f"[turn] cancelled after {len(partial)} chars")
            raise
        except Exception as e:
            print(f"[TTS] stream failed mid-turn, falling back to REST: {e}")
            if not parts:
                async for event in _orchestrate_stream(
                    user_text=user_text,
                    sentence=current,
                    level=level,
                    history=history,
                    awaiting_grammar=awaiting_grammar,
                    awaiting_translation=awaiting_translation,
                    student=student,
                ):
                    if event.get("type") == "token":
                        parts.append(event.get("text", ""))
                    elif event.get("type") == "meta":
                        meta.update(event)
            full_text = "".join(parts)
            await send({"type": "ai_text", "turn_id": turn_id, "text": full_text})
            await _speak_rest(full_text, turn_id)

        await send({"type": "ai_text", "turn_id": turn_id, "text": full_text})
        _log_latency(marks)

        awaiting_grammar = meta.get("awaiting_grammar", False)
        awaiting_translation = meta.get("awaiting_translation", False)

        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": full_text})

        return meta

    def _log_latency(marks: dict):
        """t0 is now the moment the transcript landed, so these numbers are
        comparable to the batch runs — but note STT no longer sits in front of
        them as a separate serial cost."""
        t0 = marks.get("input_ready")
        if not t0:
            return
        ft = marks.get("first_token")
        fa = marks.get("first_audio")
        llm = f"{(ft - t0) * 1000:.0f}ms" if ft else "n/a"
        audio = f"{(fa - t0) * 1000:.0f}ms" if fa else "n/a"
        print(f"[latency] first_token={llm}  first_audio={audio}")

    # ── Turn scheduling ───────────────────────────────────────────────────

    async def cancel_turn():
        """Stop the in-flight turn and wait for its cleanup to finish.

        Awaiting the cancelled task matters: it's what guarantees the TTS
        socket is closed before the next turn opens a new one.
        """
        nonlocal turn_task
        task, turn_task = turn_task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # ── Idle re-engagement ────────────────────────────────────────────────
    # After a turn ends, silence is ambiguous: the student may be thinking, or
    # may have quietly lost the thread. A human tutor checks in. Two nudges
    # only — a tutor that keeps prompting into an empty room is worse than one
    # that waits, and the student may simply have walked away.
    IDLE_FIRST = 14.0     # seconds of silence before the first check-in
    IDLE_SECOND = 25.0    # and before the second
    IDLE_LINES = [
        "कुछ और पूछना चाहती हैं? मैं यहीं हूँ।",
        # "warna" was written in Latin here and bulbul read it as an English
        # word. Hindi words go in Devanagari; only genuinely English words
        # (translation, level, check) stay in Latin.
        "कोई सवाल हो तो बताइए, नहीं तो हम अगले वाक्य पर चल सकते हैं।",
    ]

    idle_task: Optional[asyncio.Task] = None
    idle_count = 0

    async def _cancel_idle():
        nonlocal idle_task
        task, idle_task = idle_task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _idle_loop():
        nonlocal idle_count
        try:
            for delay, line in zip((IDLE_FIRST, IDLE_SECOND), IDLE_LINES):
                await asyncio.sleep(delay)
                # Don't talk over a turn that started while we were waiting.
                if turn_task and not turn_task.done():
                    return
                idle_count += 1
                print(f"[idle] nudge {idle_count}")
                await speak(line)
        except asyncio.CancelledError:
            return

    def start_idle_timer():
        nonlocal idle_task, idle_count
        if stt_stream is None:
            return          # no mic path — nobody is there to nudge
        idle_count = 0
        idle_task = asyncio.create_task(_idle_loop())

    async def apply_level_change(new_level: str):
        """Switch levels mid-session, the way asking a human tutor would."""
        nonlocal level, sentences, sentence_index, history
        nonlocal awaiting_grammar, awaiting_translation

        level = new_level
        sentences = get_sentences(new_level)
        sentence_index = 0
        history = []
        awaiting_grammar = False
        awaiting_translation = False

        if not sentences:
            await send({"type": "error",
                        "message": f"No sentences found for level {new_level}."})
            return

        # The client shows the level badge and drives its own progress list, so
        # it has to be told explicitly — it can't infer this from next_sentence.
        # `total` matters because levels have different sentence counts and the
        # old total would linger otherwise.
        await send({"type": "level_changed", "level": new_level,
                    "total": len(sentences)})
        await send({
            "type": "next_sentence",
            "sentence": sentences[0],
            "index": 0,
        })
        print(f"[level] switched to {new_level}")

    async def _turn_and_advance(user_text: str, current: dict, t_input: float):
        try:
            meta = await run_turn(user_text, current, t_input)

            if meta.get("safety"):
                # Alert AFTER run_turn returns — at that point _speak_stream
                # has finished and all audio chunks have been sent to the
                # client. The old placement fired the alert while Vidya was
                # still speaking, the beep came through the speakers, the mic
                # heard it, VAD fired, and Vidya barge-in'd herself.
                #
                # A small extra delay lets the client's MediaSource buffer
                # drain. 2s is conservative; safety responses are ~7-11s
                # spoken, and we'd rather the beep arrives a beat late than
                # cuts her off.
                await asyncio.sleep(2.0)
                await safety.alert_team_async(
                    meta["safety"], session_id=session_id,
                    student=student, transcript=user_text)
                # No advance, no idle nudge. The lesson stops.
                return

            if meta.get("change_level"):
                await apply_level_change(meta["change_level"])
            elif meta.get("move_on"):
                await advance_sentence()
            start_idle_timer()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[turn] failed: {e}")
            await send({"type": "error", "message": str(e)})

    async def start_turn(user_text: str, t_input: float):
        """Replace any in-flight turn with a new one."""
        nonlocal turn_task
        if not sentences:
            await send({"type": "error",
                        "message": "Session not initialised. Send 'init' first."})
            return
        # After the last sentence, sentence_index == len(sentences) and a turn
        # here would crash on sentences[sentence_index]. This happens when the
        # student speaks after session_complete — the endpoint timer can still
        # fire if speech overlapped the completion message.
        if sentence_index >= len(sentences):
            print(f"[turn] ignoring — session already complete (index={sentence_index})")
            return
        await cancel_turn()
        await _cancel_idle()
        current = sentences[sentence_index]
        turn_info["text"] = user_text
        turn_info["spoke"] = False
        turn_task = asyncio.create_task(
            _turn_and_advance(user_text, current, t_input)
        )

    # ── STT event consumer ────────────────────────────────────────────────

    async def _cancel_endpoint():
        nonlocal endpoint_task
        task, endpoint_task = endpoint_task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _fire_after_silence():
        """Wait out the endpoint window, then run the accumulated utterance."""
        try:
            await asyncio.sleep(ENDPOINT_DELAY)
        except asyncio.CancelledError:
            return   # student resumed — this utterance isn't over

        text = " ".join(pending_text).strip()
        pending_text.clear()
        if text:
            await start_turn(text, time.perf_counter())

    async def consume_stt():
        """Translate VAD signals and transcripts into turn control.

        This is the whole of step 3's behaviour change: the student's voice,
        not a button, decides when a turn begins and when the tutor shuts up.
        """
        nonlocal speech_started_at, speech_ended_at, endpoint_task

        async for ev in stt_stream.events():
            kind = ev.get("type")

            if kind == "speech_start":
                speech_started_at = time.perf_counter()
                print("[vad] START_SPEECH")
                await send({"type": "speech_start"})

                # They're here. Nothing to check in about.
                await _cancel_idle()

                # The student is still going — don't let a queued turn fire
                # underneath them.
                await _cancel_endpoint()

                # A genuine interruption only exists if a turn is actually
                # running. With endpointing in place this no longer fires on
                # the student's own continued speech.
                if turn_task and not turn_task.done():
                    if turn_info["spoke"]:
                        print("[barge-in] student interrupted — cancelling turn")
                        await send({"type": "barge_in"})
                        await cancel_turn()
                    else:
                        # The turn hasn't produced a word yet — usually it is
                        # still opening its TTS socket (~500ms). Nothing is
                        # playing, so there is nothing to barge in on: the
                        # student simply hasn't finished talking.
                        #
                        # Put its text back at the front of the queue. Without
                        # this, cancelling drops what they already said and the
                        # tutor answers half a question.
                        print("[turn] pre-empted before speaking — requeuing")
                        await cancel_turn()
                        if turn_info["text"]:
                            pending_text.insert(0, turn_info["text"])
                            turn_info["text"] = ""

            elif kind == "speech_end":
                # THIS is the baseline for tail latency. Measuring from
                # speech_start conflated "how long the student talked" with
                # "how long we waited", and transcription now runs *during*
                # speech, so only the post-speech remainder is real latency.
                speech_ended_at = time.perf_counter()
                await send({"type": "speech_end"})

            elif kind == "transcript":
                text = ev.get("text", "").strip()
                if not text:
                    continue
                if speech_started_at:
                    spoke = int((time.perf_counter() - speech_started_at) * 1000)
                    tail = (int((time.perf_counter() - speech_ended_at) * 1000)
                            if speech_ended_at else -1)
                    print(f"[latency] stt_tail={tail}ms spoke={spoke}ms "
                          f"lang={ev.get('language')}")
                    speech_started_at = 0.0
                    speech_ended_at = 0.0

                # Show it immediately — the student should see they were heard
                # even though the reply waits for the endpoint window.
                pending_text.append(text)
                await send({"type": "transcript",
                            "text": " ".join(pending_text)})

                await _cancel_endpoint()
                endpoint_task = asyncio.create_task(_fire_after_silence())

            elif kind == "error":
                print(f"[STT] {ev.get('message')}")

    async def ensure_stt():
        """Open the STT socket once per session, lazily.

        If it fails we do NOT kill the session — the legacy `audio_chunk`
        batch path still works, so the app degrades to the old behaviour
        instead of going dead.
        """
        nonlocal stt_stream, stt_consumer
        if stt_stream is not None:
            return True
        try:
            stream = SarvamSTTStream()
            await stream.open()
            stt_stream = stream
            stt_consumer = asyncio.create_task(consume_stt())
            return True
        except Exception as e:
            print(f"[STT] streaming unavailable, staying on batch path: {e}")
            return False

    async def advance_sentence():
        """Move to the next sentence, or end the session."""
        nonlocal sentence_index, awaiting_grammar, awaiting_translation, history

        sentence_index += 1
        awaiting_grammar = False
        awaiting_translation = False
        history = []  # fresh context per sentence

        if sentence_index >= len(sentences):
            await speak("शाबाश! आपने सभी वाक्य पूरे कर लिए! "
                        "(Well done! You've completed all sentences!)")
            await send({"type": "session_complete"})
            return None

        next_s = sentences[sentence_index]
        print(f"[transition] → sentence {sentence_index}"
              f"{' (chatter + classroom ritual)' if sentence_index == 1 else ''}")
        await send({
            "type": "next_sentence",
            "sentence": next_s,
            "index": sentence_index,
        })

        # The client starts its chatter burst the moment next_sentence lands.
        # Wait it out before speaking: first_audio has been as low as 535ms and
        # the burst runs ~600ms, so without this the settling line sometimes
        # starts underneath the noise it is supposed to be settling.
        # The classroom ritual is a ONE-TIME beat, tied to the same transition
        # that plays the chatter. Repeating "क्लास... शांत हो जाइए" at every
        # sentence stops being a moment and becomes a tic — and the student
        # sits through the wait each time for a sound that isn't playing.
        #
        # Applies to every level: the ritual belongs to the second sentence,
        # not to a difficulty.
        if sentence_index == 1:
            # Let the chatter finish, then speak. Sequential and simple:
            # playback starts the moment audio arrives, so this one number
            # controls the whole beat. It is deliberately a little less than
            # the burst length, because speak() spends ~0.5-0.9s opening a TTS
            # socket and waiting for the first chunk.
            await asyncio.sleep(CHATTER_CUE_SECONDS)

            # Fixed string rather than generated: a ritual needs the same beat
            # every time, and an LLM would re-word it. The commas are the
            # pauses — bulbul has no SSML.
            who = student.get("name") or ""
            call = "क्लास... शांत हो जाइए।"
            follow = (f" हाँ {who}, बताइए — इसका अर्थ बताऊँ या translation check करें?"
                      if who else
                      " हाँ, बताइए — इसका अर्थ बताऊँ या translation check करें?")
            await speak(call + follow)
        else:
            await speak(f"अगला वाक्य — {next_s['sanskrit']}")

        return next_s

    # ── Main loop ─────────────────────────────────────────────────────────

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type")

            # ── MIC CHUNK (hot path) ──────────────────────────────────────
            # Deliberately first: this fires many times per second and must
            # never queue behind anything.
            if msg_type == "mic_chunk":
                if stt_stream is not None:
                    pcm = base64.b64decode(msg.get("data", ""))
                    mic_chunks += 1
                    mic_bytes += len(pcm)
                    # Confirms audio is actually arriving. Without this, "VAD
                    # never fired" and "no audio ever reached the server" look
                    # identical in the logs.
                    if mic_chunks == 1:
                        print(f"[mic] first chunk: {len(pcm)} bytes")
                    elif mic_chunks % 100 == 0:
                        print(f"[mic] {mic_chunks} chunks, "
                              f"{mic_bytes/1024:.0f}KB, "
                              f"~{mic_bytes/2/16000:.1f}s of audio @16k")
                    await stt_stream.feed(pcm)
                elif mic_chunks == 0:
                    mic_chunks = 1
                    print("[mic] receiving audio but STT socket is not open")
                continue

            if msg_type == "mic_stop":
                if stt_stream is not None:
                    await stt_stream.flush()
                continue

            # ── HEARTBEAT ─────────────────────────────────────────────────
            # Cheap liveness proof. Mic traffic only shows the client is alive;
            # this is how the client learns the server still is.
            if msg_type == "ping":
                await send({"type": "pong"})
                continue

            # ── CLIENT-INITIATED BARGE-IN ─────────────────────────────────
            # The browser detected the student talking over audible speech.
            # It knows things this side cannot: TTS runs faster than realtime,
            # so a turn can be "done" here while seconds of it are still
            # queued in the browser. Trust the client on this.
            if msg_type == "barge_in":
                print("[barge-in] client reported interruption")
                await _cancel_endpoint()
                await _cancel_idle()
                if turn_task and not turn_task.done():
                    await cancel_turn()
                    # Nothing was audible from the server's point of view but
                    # the student was mid-sentence — keep their words.
                    if not turn_info["spoke"] and turn_info["text"]:
                        pending_text.insert(0, turn_info["text"])
                        turn_info["text"] = ""
                continue

            # ── INIT ──────────────────────────────────────────────────────
            if msg_type == "init":
                level = msg.get("level", "easy")
                sentence_index = msg.get("sentence_index", 0)
                # A reconnect resumes mid-lesson; a fresh start doesn't. Logged
                # so recovery is visible in the server log — code 1001 comes
                # from the browser, so this is the only side that shows whether
                # the client actually came back.
                print(f"[WS] init session={session_id} level={level} "
                      f"index={sentence_index} "
                      f"({'RECONNECT' if sentence_index > 0 else 'fresh'})")
                student = {
                    "name": (msg.get("name") or "").strip()[:40],
                    "gender": msg.get("gender") or "neutral",
                }
                sentences = get_sentences(level)

                if not sentences:
                    await send({"type": "error",
                                "message": "No sentences found for this level."})
                    continue

                await ensure_stt()

                current = sentences[sentence_index]
                await send({
                    "type": "next_sentence",
                    "sentence": current,
                    "index": sentence_index,
                })

                who = f" {student['name']}" if student.get("name") else ""
                greeting = (
                    f"नमस्ते{who}! आज हम '{current['sanskrit']}' वाक्य पढ़ेंगे। "
                    f"(Namaste! Today we'll read '{current['transliteration']}'. "
                    f"Start reading when you're ready!)"
                )
                # The greeting is a fixed string and takes seconds to speak, so
                # the LLM's cold start is hidden entirely behind it. Fire and
                # forget — nothing should wait on a warmup.
                asyncio.create_task(llm.prewarm())
                await speak(greeting)

            # ── AUDIO CHUNK (legacy batch fallback) ───────────────────────
            elif msg_type == "audio_chunk":
                audio_bytes = base64.b64decode(msg.get("data", ""))

                t_stt = time.perf_counter()
                try:
                    transcript = await stt.transcribe(audio_bytes)
                    print(f"[latency] stt={int((time.perf_counter()-t_stt)*1000)}ms "
                          f"audio={len(audio_bytes)/1024:.0f}KB")
                except Exception as e:
                    await send({"type": "error", "message": f"STT failed: {e}"})
                    continue

                if not transcript.strip():
                    continue

                await send({"type": "transcript", "text": transcript})
                await start_turn(transcript, time.perf_counter())

            # ── TEXT MESSAGE ──────────────────────────────────────────────
            elif msg_type == "text":
                user_text = msg.get("text", "")
                if user_text.strip():
                    await start_turn(user_text, time.perf_counter())

            # ── MANUAL MOVE ON ────────────────────────────────────────────
            elif msg_type == "move_on":
                await cancel_turn()
                # advance_sentence() now speaks the classroom line itself, so
                # announcing again here would say it twice.
                await advance_sentence()

    except WebSocketDisconnect as e:
        # code 1000 = normal (tab closed / navigated away)
        # code 1001 = going away (refresh)
        # code 1006 = abnormal — no close frame. Usually a client-side JS crash
        #             or a proxy/timeout killing the connection.
        print(f"[WS] Session {session_id} disconnected "
              f"(code={getattr(e, 'code', '?')} reason={getattr(e, 'reason', '')!r})")
    except Exception as e:
        print(f"[WS] Unexpected error in session {session_id}: {e}")
        try:
            await send({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        # Three concurrent things are running; none of them may outlive the
        # socket, or a cancelled session keeps streaming audio into the void.
        await cancel_turn()
        await _cancel_endpoint()
        await _cancel_idle()
        if stt_consumer and not stt_consumer.done():
            stt_consumer.cancel()
            try:
                await stt_consumer
            except (asyncio.CancelledError, Exception):
                pass
        if stt_stream is not None:
            await stt_stream.close()