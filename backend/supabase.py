#db client(not used heavily yet, ready for when you persist session)

from supabase import create_client, Client 
from config import get_settings

_client: Client | None = None

def get_supabase() -> Client:
    global _client
    if _client is None:
        s = get_settings()
        _client = create_client(s.supabase_url,s.supabase_anon_key)
    return _client
    

    