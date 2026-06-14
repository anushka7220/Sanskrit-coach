# Sanskrit Coach — AI Sanskrit Tutor

A real-time Sanskrit learning assistant powered by Sarvam AI (STT + TTS), Gemini LLM, FastAPI, and WebSockets.

---

## Project structure

```
sanskrit-coach/
├── .venv/                   # virtual environment (arm64 M1)
├── .env                     # secrets — never commit this
├── requirements.txt
├── readme.md
│
├── backend/
│   ├── main.py              # FastAPI app entry point
│   ├── config.py            # env + settings (pydantic-settings)
│   ├── supabase.py          # Supabase client singleton
│   ├── session.py           # POST /session/start router
│   ├── ws.py                # WebSocket /ws/{session_id} router
│   ├── stt.py               # Sarvam STT wrapper
│   ├── tts.py               # Sarvam TTS wrapper (bulbul:v3, speaker: anand)
│   ├── llm.py               # Gemini LLM (Sarvam LLM fallback)
│   ├── orchestrator.py      # turn logic: meaning / translate / grammar
│   └── data/
│       ├── __init__.py
│       └── sentences.py     # hardcoded Sanskrit sentences by level
│
└── frontend/
    └── index.html           # single-file UI (served via python http.server)
```

---

## Setup (macOS M1)

```bash
# 1. create virtual environment (arm64 native)
python3 -m venv .venv
source .venv/bin/activate

# 2. install dependencies
pip install -r requirements.txt

# 3. fill in your keys
nano backend/.env
```

---

## Environment variables

`backend/.env`:

```
SARVAM_API_KEY=your_sarvam_key_here
GEMINI_API_KEY=your_gemini_key_here
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_ANON_KEY=your_supabase_anon_key
```

| Key | Where to get it |
|-----|----------------|
| `SARVAM_API_KEY` | https://dashboard.sarvam.ai |
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey |
| `SUPABASE_URL` + `SUPABASE_ANON_KEY` | https://supabase.com → project settings → API |

---

## Running locally

Two terminals, both must be running:

**Terminal 1 — Backend**
```bash
source .venv/bin/activate
cd backend
uvicorn main:app --reload --port 8000
```

**Terminal 2 — Frontend**
```bash
cd frontend
python3 -m http.server 3000
```

Then open **http://localhost:3000**

API docs: **http://localhost:8000/docs**

---

## API overview

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| POST | `/session/start` | Create session, returns session_id + first sentence |
| WS | `/ws/{session_id}` | Real-time audio + text exchange |

### WebSocket message protocol

**Client → Server**
```json
{ "type": "init",        "level": "easy", "sentence_index": 0 }
{ "type": "audio_chunk", "data": "<base64 WAV>" }
{ "type": "text",        "text": "what does this mean?" }
{ "type": "move_on" }
```

**Server → Client**
```json
{ "type": "next_sentence", "sentence": {...}, "index": 0 }
{ "type": "transcript",    "text": "user speech text" }
{ "type": "ai_text",       "text": "tutor response text" }
{ "type": "ai_audio",      "data": "<base64 WAV>" }
{ "type": "session_complete" }
{ "type": "error",         "message": "..." }
```

---

## Difficulty levels

| Level | What happens |
|-------|-------------|
| Easy | Read sentence → ask meaning → AI explains in Hindi/English |
| Intermediate | Read sentence → attempt translation → AI checks and loops if wrong |
| Hard | Read sentence → attempt translation → AI asks grammar question |

---

## Sarvam AI notes (important — API quirks)

- **TTS model**: `bulbul:v3` — do NOT pass `pitch` or `loudness` (unsupported, returns 400)
- **TTS speaker**: use `anand` (v3 compatible) — `meera` and `anushka` are v1 only
- **STT model**: `saarika:v2.5`
- **STT input**: must be WAV format — browser records webm, frontend converts to WAV via AudioContext before sending
- **LLM**: Sarvam LLM endpoint is `https://api.sarvam.ai/v1/chat/completions`, model `sarvam-m`
- **Auth header**: `api-subscription-key` (not `Authorization: Bearer`)

---

## Known issues / gotchas

- Gemini API keys expire — regenerate at https://aistudio.google.com/apikey if LLM stops working
- Chrome blocks AudioContext until a user gesture — always click Start before audio will play
- `.env` must be inside `backend/` folder (not project root) since uvicorn runs from there
- `config.py` uses `Path(__file__).parent / ".env"` to always find the right `.env` regardless of where uvicorn is launched from