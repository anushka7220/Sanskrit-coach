from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from config import get_settings
import session, ws
import pathlib
app = FastAPI(
    title="Sanskrit AI Tutor",
    description="Real-time Sanskrit learning assistant powered by Sarvam AI",
    version="0.1.0",
)

# Allow the frontend (any origin in dev, lock this down in prod)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(session.router)
app.include_router(ws.router)
 
@app.get("/health")
async def health():
    return {"status": "ok", "service": "sanskrit-ai-tutor"}

