import asyncio
import os
import socket
import ssl
import time

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = "gemini-3.5-flash-lite"

HOST = "generativelanguage.googleapis.com"


async def test():
    print("=" * 60)
    print("Gemini Latency Debug")
    print("=" * 60)

    # ---------------- DNS ----------------

    t = time.perf_counter()
    ip = socket.gethostbyname(HOST)
    print(f"DNS lookup         : {(time.perf_counter()-t)*1000:.0f} ms ({ip})")

    # ---------------- TCP ----------------

    t = time.perf_counter()
    sock = socket.create_connection((HOST, 443))
    print(f"TCP connect        : {(time.perf_counter()-t)*1000:.0f} ms")

    # ---------------- TLS ----------------

    t = time.perf_counter()
    ctx = ssl.create_default_context()
    ssock = ctx.wrap_socket(sock, server_hostname=HOST)
    print(f"TLS handshake      : {(time.perf_counter()-t)*1000:.0f} ms")

    ssock.close()

    # ---------------- Gemini client ----------------

    t = time.perf_counter()
    client = genai.Client(api_key=API_KEY)
    print(f"Client creation    : {(time.perf_counter()-t)*1000:.0f} ms")

    print()

    print("Calling Gemini...")

    t0 = time.perf_counter()

    stream = await client.aio.models.generate_content_stream(
        model=MODEL,
        contents="Hello",
        config=types.GenerateContentConfig(
            max_output_tokens=20,
            temperature=0,
            thinking_config=types.ThinkingConfig(
                thinking_level="MINIMAL"
            ),
        ),
    )

    print(f"Stream object      : {(time.perf_counter()-t0)*1000:.0f} ms")

    first = None
    total = ""

    async for chunk in stream:

        if chunk.text:

            if first is None:
                first = time.perf_counter()
                print(f"First token        : {(first-t0)*1000:.0f} ms")

            total += chunk.text

    end = time.perf_counter()

    print(f"Finished           : {(end-t0)*1000:.0f} ms")
    print()
    print("Response:")
    print(total)


asyncio.run(test())