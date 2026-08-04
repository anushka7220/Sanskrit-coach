# list_models.py  — project root mein, wahin jahan config.py hai
from google import genai
from config import get_settings

client = genai.Client(api_key=get_settings().gemini_api_key)

for m in client.models.list():
    if "generateContent" in getattr(m, "supported_actions", []):
        print(m.name)

stream = client.models.generate_content_stream(
    model="gemini-3.6-flash",
    contents="Say hello"
)

for chunk in stream:
    print(repr(chunk.text))