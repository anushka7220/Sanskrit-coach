# संस्कृत Coach

A real-time voice tutor for Sanskrit — read aloud, be heard, be corrected kindly.

Built as a production voice agent with streaming STT, LLM, and TTS running in parallel, not sequentially. The student talks to Vidya, an AI tutor who listens continuously, responds in natural Hinglish, and can be interrupted mid-sentence.

**Live:** [sanskrit-coach.vercel.app](https://sanskrit-coach.vercel.app)

---

## What It Does

The student sees a Sanskrit sentence on screen. They read it aloud, ask about its meaning, request a translation check, or ask grammar questions — all by voice. Vidya listens, responds, and advances through the lesson.

Three difficulty levels: Easy (reading + meaning), Intermediate (+ translation checking), Hard (+ grammar questions).

---

## Architecture

```
Browser (vanilla JS)
  ├─ Mic → 16kHz PCM chunks → WebSocket → Server
  ├─ MediaSource ← MP3 audio chunks ← WebSocket ← Server
  └─ UI: karaoke sentences, speech cloud, animated tutor SVG

Server (FastAPI + WebSocket)
  ├─ STT: Sarvam saaras:v3 (streaming WebSocket, not batch)
  ├─ LLM: Gemini Flash-Lite (primary) / Sarvam 30b (fallback)
  ├─ TTS: ElevenLabs eleven_flash_v2_5 (streaming HTTP)
  ├─ Orchestrator: intent detection, FAQ, safety, level changes
  └─ Prosody: deterministic text shaping between LLM and TTS
```

### Why Streaming Matters

The old pipeline was sequential: record → stop → transcribe → generate → synthesize → play. Each step waited for the previous one. Time from student finishing their sentence to hearing Vidya's first word: **17 seconds**.

The new pipeline is parallel:

- **STT** runs continuously while the student speaks (not after)
- **LLM** streams tokens as they're generated (not after the full reply)
- **TTS** synthesizes sentence-by-sentence as tokens arrive (not after the full text)
- **Playback** starts on the first audio chunk (not after full synthesis)

Result: **~500ms** time-to-first-audio on a warm turn.

### Three Concurrent Tasks

The WebSocket handler runs three things at once:

1. **Receive loop** — always listening for mic chunks, never blocked
2. **STT consumer** — translates VAD signals and transcripts into turn control
3. **Turn task** — one at a time, cancelled on barge-in

The receive loop never blocks on a turn. That's what makes barge-in possible — if the loop awaited `run_turn()`, the server would be deaf while Vidya speaks.

---

## Key Design Decisions

### Barge-in Is Client-Authoritative

TTS generates faster than realtime. By the time the server finishes a turn, the browser may still have 8 seconds of audio buffered. The server sees no running turn and won't cancel anything — but Vidya is very much still talking.

Only the browser knows whether sound is actually coming out of the speaker. So barge-in lives in the client: `isPlaying()` reads the `<audio>` element directly, and if the student speaks while it's playing, the client sends `barge_in` to the server.

### Barge-in Is Cancellation, Not Muting

When interrupted, the turn task is cancelled outright. The LLM stream is abandoned, the TTS connection is closed in a `finally` block. Muting would leave the tutor "talking" invisibly, burning tokens and TTS credit.

### The Turn Is a Task, Not an Awaited Call

```python
# Old: server is deaf while tutor speaks
await run_turn(text)

# New: receive loop stays free
turn_task = asyncio.create_task(_turn_and_advance(text))
```

### No LLM Call in the Safety Layer

When a student says something indicating self-harm, abuse, or a medical emergency, the response is a fixed string — not generated. A model improvising around a crisis could give medical advice, invent a resource, or say something harmful. Fixed strings are auditable: you know exactly what the student will hear, every time.

### Retrieval Is Free, Generation Costs

FAQ retrieval uses lexical keyword matching over 10 in-memory entries, not embeddings. An embedding call would add 300-500ms to every turn to choose from a list of ten. The same principle applies to intent detection and safety detection — all keyword-based, all zero-latency.

### Naturalness Is Decided in the Text-Shaping Layer

The TTS model (whether Sarvam's bulbul or ElevenLabs) has no SSML. No `<break>`, no `<emphasis>`, no pitch contour control. Everything a listener hears as intonation comes from punctuation and voice settings.

So "questions sound like statements" is a punctuation problem (danda → question mark), "not enough pauses" is a comma problem, and "wrong tone" is a voice-settings problem. All three are fixed deterministically in `prosody.py`, with no extra latency.

---

## Features

### Voice Interaction
- Continuous mic streaming (no stop button — VAD decides turn boundaries)
- Endpointing with 0.7s silence window (avoids mid-thought interruptions)
- Echo cancellation required (tutor's voice would otherwise trigger VAD)
- Pre-empted turn text requeue (half-questions aren't lost on cancellation)

### Teaching
- Three levels: easy, intermediate, hard
- Voice-activated level switching ("level hard kar do")
- Meaning explanation, translation checking, grammar questions
- Intent detection: meaning / translate / move_on / FAQ / safety
- Idle re-engagement: two nudges (14s, 25s) then silence

### Prosody & Voice Modulation
- Per-chunk voice settings: speed, stability, style vary by sentence type
- Question detection by word (not punctuation) — fixes danda-as-statement
- Comma insertion after openers and around quoted Sanskrit
- Mid-sentence pause markers (यानी, मतलब, इसलिए)
- Pre-pause before corrections
- Calm mode for scripted lines (greeting, transitions)
- `previous_text` for continuous intonation across chunk boundaries

### Safety
- Keyword detection with Devanagari normalization (ँ/ं/़ folding)
- Four categories: self_harm, medical_emergency, abuse_or_danger, harm_to_others
- Fixed responses with Indian helpline numbers (112, 14416, 1098)
- Mic suppression during safety response (alert beep can't trigger VAD)
- Audible server-side alert: repeating system sound, 6×0.45s gap
- Lesson stops after safety trigger (no idle nudge, no advance)

### FAQ
- 10 in-memory entries covering tutorship questions
- Lexical retrieval: keyword hits + token overlap, normalized
- Checked in fallback branch only (lesson questions can never match)
- Ground-truth answers rewritten by LLM in Vidya's voice

### Classroom Ambience
- Chatter burst on sentence 1→2 transition only
- Chatter runs until Vidya's first audio, then ducks to 22% and fades
- Mic suppressed during cue (crowd voices would trigger VAD)
- 12s watchdog prevents stuck-muted mic

### Recovery & Persistence
- Heartbeat ping/pong (15s interval, 45s server silence → force reconnect)
- `visibilitychange` + `pageshow` handlers for tab freeze recovery
- `sessionStorage` persistence: reload resumes the lesson, not the landing
- `localStorage` profile: name + gender asked once, remembered across sessions
- `beforeunload` handler removed (it fired on tab freeze and killed reconnect)

---

## Project Structure

```
sanskrit-coach/
├── backend/
│   ├── main.py              # FastAPI app, CORS, router registration
│   ├── config.py            # Pydantic Settings, .env loading
│   ├── ws.py                # WebSocket handler, turn lifecycle, barge-in
│   ├── orchestrator.py      # Intent routing, safety, FAQ, LLM orchestration
│   ├── llm.py               # Gemini/Sarvam streaming with failover
│   ├── tts.py               # ElevenLabs streaming TTS
│   ├── tts_sarvam.py        # Sarvam TTS (backup)
│   ├── stt.py               # Sarvam batch STT (fallback)
│   ├── stt_stream.py        # Sarvam streaming STT + VAD
│   ├── prosody.py           # Text shaping: punctuation, pauses, classification
│   ├── safety.py            # Unsafe conversation detection + alerting
│   ├── faq.py               # In-memory FAQ retrieval
│   ├── session.py           # Session creation endpoint
│   ├── data/
│   │   └── sentences.py     # Sanskrit sentences by level
│   ├── requirements.txt
│   ├── Procfile
│   └── .env                 # API keys (not committed)
│
└── frontend/
    ├── index.html            # Single-file app: UI + JS + CSS
    └── ambience/
        └── classroom-chatter.mp3
```

---

## Setup

### Prerequisites

- Python 3.11+
- API keys: Gemini, Sarvam AI, ElevenLabs

### Backend

```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create `.env`:

```env
GEMINI_API_KEY=your-key
SARVAM_API_KEY=your-key
ELEVENLABS_API_KEY=your-key
```

Run:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

### Frontend

Open `frontend/index.html` in a browser, or serve it:

```bash
cd frontend
python -m http.server 3000
```

For production, set `RAILWAY_URL` in `index.html` to your deployed backend URL.

---

## Deployment

### Backend → Render

1. Push to GitHub
2. Render → New Web Service → select repo
3. Root Directory: `backend`
4. Build Command: `pip install -r requirements.txt`
5. Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
6. Add environment variables (all three API keys)

### Frontend → Vercel

1. Set `RAILWAY_URL` in `index.html` to Render URL
2. Push to GitHub
3. Vercel → New Project → select repo
4. Root Directory: `frontend`
5. Framework Preset: Other

---

## Latency Journey

| Milestone | First Audio |
|-----------|------------|
| Starting point (batch everything) | 17,000ms |
| Streaming LLM + sentence chunking | ~4,000ms |
| Gemini thinking_level=MINIMAL | ~1,700ms |
| Streaming STT (parallel with speech) | ~800ms |
| Chunk ramp alignment + preprocessing | ~535ms |

The single biggest fix was `thinking_level="MINIMAL"`. Gemini 3.x models think by default — leaving it unset doesn't mean "no thinking", it means the model's own default, which is thinking ON. That's why TTFT swung between 522ms and 7010ms on identical code.

---

## VAD Tuning

```python
START_SPEECH_VOLUME_THRESHOLD = "-40"   # dB below which = not speech
INTERRUPT_MIN_SPEECH_FRAMES = "8"       # frames before barge-in counts
HIGH_VAD_SENSITIVITY = "false"          # noisy room needs low sensitivity
```

The volume threshold was unset by default, meaning no filtering at all — distant conversations triggered turns. `-40dB` is the starting point; move toward `-30` if background still leaks, toward `-50` if a soft-spoken student gets ignored.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | Vanilla JS, Web Audio API, MediaSource |
| Backend | FastAPI, WebSocket, asyncio |
| STT | Sarvam saaras:v3 (streaming) |
| LLM | Gemini Flash-Lite (primary), Sarvam 30b (fallback) |
| TTS | ElevenLabs eleven_flash_v2_5 |
| Hosting | Vercel (frontend), Render (backend) |

---

## Known Limitations

- **STT false negatives on safety**: keyword detection catches direct statements, not indirect ones. Treat every alert as real; do not treat silence as safety.
- **No pronunciation dictionary yet**: `dict_id` slot is wired but empty. Sanskrit words like गच्छति get TTS's best guess, which isn't always right.
- **Render free tier sleeps after 15min**: first request after inactivity takes ~30s to wake.
- **No progress persistence**: lesson position survives a reload (sessionStorage) but not a new session. The student starts fresh each time.

---

## License

MIT
