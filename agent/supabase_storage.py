"""
Supabase Storage access — separate from db.py because Storage is a different
REST surface (object storage, not PostgREST) with its own upload semantics.
Uses the service-role key directly, same as db.py.
"""

import mimetypes
import httpx
from config import config

_STORAGE_BASE = f"{config.supabase_url}/storage/v1"
_HEADERS = {
    "apikey": config.supabase_service_role_key,
    "Authorization": f"Bearer {config.supabase_service_role_key}",
}


async def upload_file(local_path: str, bucket: str, remote_path: str) -> str:
    """Upload a local file to a Supabase Storage bucket, overwriting any object
    already at remote_path, and return its public URL."""
    content_type = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
    with open(local_path, "rb") as f:
        data = f.read()
    headers = {**_HEADERS, "Content-Type": content_type, "x-upsert": "true"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{_STORAGE_BASE}/object/{bucket}/{remote_path}", headers=headers, content=data)
        r.raise_for_status()
    return f"{_STORAGE_BASE}/object/public/{bucket}/{remote_path}"
