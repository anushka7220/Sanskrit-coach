# backend/chunker.py
HARD = "।.?!\n"
SOFT = ",;:—"

def _find_cut(buf: str, min_len: int, chars: str):
    for i, ch in enumerate(buf):
        if i + 1 >= min_len and ch in chars:
            return i + 1
    return None

async def pipe_llm_to_tts(token_iter, tts):
    """Consume LLM tokens, push speakable chunks into the TTS socket."""
    buf = ""
    first = True
    async for token in token_iter:
        buf += token
        while True:
            cut = _find_cut(
                buf,
                min_len=25 if first else 60,
                chars=HARD + SOFT if first else HARD,
            )
            if cut is None:
                break
            piece = buf[:cut].strip()
            if piece:
                await tts.say(piece)
                first = False
            buf = buf[cut:]
    if buf.strip():
        await tts.say(buf.strip())
    await tts.finish()