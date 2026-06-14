"""
WebSocket handler — the real-time conversation loop.

Client → Server message types:
  { "type": "init",        "level": "easy", "sentence_index": 0 }
  { "type": "audio_chunk", "data": "<base64 wav>" }
  { "type": "text",        "text": "what does this mean?" }
  { "type": "move_on" }

Server → Client message types:
  { "type": "transcript",    "text": "..." }
  { "type": "ai_text",       "text": "..." }
  { "type": "ai_audio",      "data": "<base64 wav>" }
  { "type": "next_sentence", "sentence": {...}, "index": N }
  { "type": "session_complete" }
  { "type": "error",         "message": "..." }
"""

import base64
import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from data.sentences import get_sentences, Level
import stt, tts, orchestrator

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()

    # Session state
    level: Level = "easy"
    sentences = []
    sentence_index = 0
    history: list[dict] = []
    awaiting_grammar = False
    awaiting_translation = False

    async def send(payload: dict):
        await websocket.send_text(json.dumps(payload))

    async def speak(text: str, lang: str = "hi-IN"):
        """Convert text to speech and send audio + text to client."""
        await send({"type": "ai_text", "text": text})
        try:
            audio_bytes = await tts.synthesize(text, language_code=lang)
            audio_b64 = base64.b64encode(audio_bytes).decode()
            await send({"type": "ai_audio", "data": audio_b64})
        except Exception as e:
            print(f"[TTS] Error: {e}")

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type")

            # ── INIT ──────────────────────────────────────────────────────────
            if msg_type == "init":
                level = msg.get("level", "easy")
                sentence_index = msg.get("sentence_index", 0)
                sentences = get_sentences(level)

                if not sentences:
                    await send({"type": "error", "message": "No sentences found for this level."})
                    continue

                current = sentences[sentence_index]
                await send({
                    "type": "next_sentence",
                    "sentence": current,
                    "index": sentence_index,
                })

                greeting = (
                    f"नमस्ते! आज हम '{current['sanskrit']}' वाक्य पढ़ेंगे। "
                    f"(Namaste! Today we'll read '{current['transliteration']}'. "
                    f"Start reading when you're ready!)"
                )
                await speak(greeting)

            # ── AUDIO CHUNK ───────────────────────────────────────────────────
            elif msg_type == "audio_chunk":
                audio_b64 = msg.get("data", "")
                audio_bytes = base64.b64decode(audio_b64)

                try:
                    transcript = await stt.transcribe(audio_bytes)
                except Exception as e:
                    await send({"type": "error", "message": f"STT failed: {e}"})
                    continue

                if not transcript.strip():
                    continue

                await send({"type": "transcript", "text": transcript})

                # Process through orchestrator
                current = sentences[sentence_index]
                result = await orchestrator.process_turn(
                    user_text=transcript,
                    sentence=current,
                    level=level,
                    history=history,
                    awaiting_grammar=awaiting_grammar,
                    awaiting_translation=awaiting_translation,
                )

                awaiting_grammar = result["awaiting_grammar"]
                awaiting_translation = result["awaiting_translation"]

                # Update conversation history
                history.append({"role": "user", "content": transcript})
                history.append({"role": "assistant", "content": result["response_text"]})

                await speak(result["response_text"])

                if result["move_on"]:
                    sentence_index += 1
                    if sentence_index >= len(sentences):
                        await speak("शाबाश! आपने सभी वाक्य पूरे कर लिए! (Well done! You've completed all sentences!)")
                        await send({"type": "session_complete"})
                    else:
                        next_s = sentences[sentence_index]
                        awaiting_grammar = False
                        awaiting_translation = False
                        history = []  # fresh context per sentence
                        await send({
                            "type": "next_sentence",
                            "sentence": next_s,
                            "index": sentence_index,
                        })

            # ── TEXT MESSAGE ──────────────────────────────────────────────────
            elif msg_type == "text":
                user_text = msg.get("text", "")
                if not user_text.strip():
                    continue

                current = sentences[sentence_index] if sentences else None
                if not current:
                    await send({"type": "error", "message": "Session not initialised. Send 'init' first."})
                    continue

                result = await orchestrator.process_turn(
                    user_text=user_text,
                    sentence=current,
                    level=level,
                    history=history,
                    awaiting_grammar=awaiting_grammar,
                    awaiting_translation=awaiting_translation,
                )

                awaiting_grammar = result["awaiting_grammar"]
                awaiting_translation = result["awaiting_translation"]

                history.append({"role": "user", "content": user_text})
                history.append({"role": "assistant", "content": result["response_text"]})

                await speak(result["response_text"])

                if result["move_on"]:
                    sentence_index += 1
                    if sentence_index >= len(sentences):
                        await speak("शाबाश! आपने सभी वाक्य पूरे कर लिए!")
                        await send({"type": "session_complete"})
                    else:
                        next_s = sentences[sentence_index]
                        awaiting_grammar = False
                        awaiting_translation = False
                        history = []
                        await send({
                            "type": "next_sentence",
                            "sentence": next_s,
                            "index": sentence_index,
                        })

            # ── MANUAL MOVE ON ────────────────────────────────────────────────
            elif msg_type == "move_on":
                sentence_index += 1
                awaiting_grammar = False
                awaiting_translation = False
                history = []

                if sentence_index >= len(sentences):
                    await speak("शाबाश! सभी वाक्य पूरे हो गए!")
                    await send({"type": "session_complete"})
                else:
                    next_s = sentences[sentence_index]
                    await send({
                        "type": "next_sentence",
                        "sentence": next_s,
                        "index": sentence_index,
                    })
                    await speak(f"अगला वाक्य: {next_s['sanskrit']}")

    except WebSocketDisconnect:
        print(f"[WS] Session {session_id} disconnected")
    except Exception as e:
        print(f"[WS] Unexpected error in session {session_id}: {e}")
        try:
            await send({"type": "error", "message": str(e)})
        except Exception:
            pass