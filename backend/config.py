from pydantic_settings import BaseSettings
from functools import lru_cache
from pathlib import Path

class Settings(BaseSettings):
    gemini_api_key: str
    sarvam_api_key: str
    supabase_url: str
    supabase_anon_key: str
    sarvam_base_url: str = "https://api.sarvam.ai"

    class Config:
        env_file = Path(__file__).parent / ".env"

@lru_cache
def get_settings() -> Settings:
    return Settings()