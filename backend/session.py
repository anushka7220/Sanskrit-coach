from fastapi import APIRouter
from pydantic import BaseModel
from uuid import uuid4 #A UUID (Universally Unique Identifier) is a very long random ID that's extremely unlikely to ever repeat
from data.sentences import get_sentences, Level

router = APIRouter(prefix="/session", tags=["session"])

class StartRequest(BaseModel):
    level : Level

class StartResponse(BaseModel):
    session_id: str
    level: Level
    total_sentences: int
    first_sentence: dict

@router.post("/start", response_model=StartResponse)
async def start_session(body: StartRequest):
    """
    Create a new learning session.
    Returns session_id + the first sentence to display.
    """
    sentences = get_sentences(body.level)
    session_id = str(uuid4())
 
    return StartResponse(
        session_id=session_id,
        level=body.level,
        total_sentences=len(sentences),
        first_sentence=sentences[0],
    )