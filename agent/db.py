"""
Supabase data access via direct PostgREST calls (httpx).

The supabase-py client's requests trigger Cloudflare 1101 'Worker threw
exception' errors on this project, while identical raw REST calls succeed —
so we talk to PostgREST directly. Service-role key bypasses RLS.
"""

import httpx
from loguru import logger
from config import config

_BASE = f"{config.supabase_url}/rest/v1"
_HEADERS = {
    "apikey": config.supabase_service_role_key,
    "Authorization": f"Bearer {config.supabase_service_role_key}",
    "Content-Type": "application/json",
}
_TIMEOUT = 30


# ── Delete guardrail ──────────────────────────────────────────────────────────
# The agent may read (GET), create (POST) and update (PATCH) — never delete.
# Enforced as an httpx event hook on every client this module creates, so the
# check runs on the raw outgoing request: no tool or future code path that goes
# through db.py can issue a DELETE, and RPC calls (which could run arbitrary
# SQL server-side, including deletes) are blocked too.

class ForbiddenDbActionError(Exception):
    """Raised when the agent attempts a database action it is not allowed."""


_ALLOWED_METHODS = {"GET", "POST", "PATCH", "HEAD"}


async def _guard_request(request: httpx.Request):
    method = request.method.upper()
    if method not in _ALLOWED_METHODS:
        logger.error(f"[DB GUARD] Blocked {method} {request.url}")
        raise ForbiddenDbActionError(
            f"Blocked {method} to {request.url.path} — the agent is not allowed to delete data"
        )
    if "/rpc/" in request.url.path:
        logger.error(f"[DB GUARD] Blocked RPC call {request.url}")
        raise ForbiddenDbActionError(
            f"Blocked RPC call to {request.url.path} — the agent may not run server-side functions"
        )


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_TIMEOUT, event_hooks={"request": [_guard_request]})


class _Result:
    """Mimics supabase-py's response shape (.data) so callers don't change."""
    def __init__(self, data):
        self.data = data


async def query(table: str, filters: dict = None, select: str = "*") -> _Result:
    """SELECT rows from a table with optional eq filters."""
    params = {"select": select}
    for col, val in (filters or {}).items():
        params[col] = f"eq.{val}"
    async with _client() as client:
        r = await client.get(f"{_BASE}/{table}", headers=_HEADERS, params=params)
        r.raise_for_status()
        return _Result(r.json())


async def ilike_query(table: str, column: str, pattern: str, select: str = "*") -> _Result:
    """SELECT rows where column ILIKE pattern (use % wildcards)."""
    # PostgREST accepts '*' as the ILIKE wildcard. A literal '%' must never be
    # sent raw — an unencoded trailing '%' is invalid URL syntax and makes
    # Supabase's Cloudflare worker throw (error 1101).
    params = {"select": select, column: f"ilike.{pattern.replace('%', '*')}"}
    async with _client() as client:
        r = await client.get(f"{_BASE}/{table}", headers=_HEADERS, params=params)
        r.raise_for_status()
        return _Result(r.json())


async def insert(table: str, data: dict) -> _Result:
    """INSERT a single row and return the created record."""
    headers = {**_HEADERS, "Prefer": "return=representation"}
    async with _client() as client:
        r = await client.post(f"{_BASE}/{table}", headers=headers, json=data)
        r.raise_for_status()
        return _Result(r.json())


async def upsert(table: str, data: dict, on_conflict: str) -> _Result:
    """UPSERT a single row, updating on conflict columns."""
    headers = {**_HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"}
    async with _client() as client:
        r = await client.post(
            f"{_BASE}/{table}",
            headers=headers,
            params={"on_conflict": on_conflict},
            json=data,
        )
        r.raise_for_status()
        return _Result(r.json())


async def insert_ignore(table: str, data, on_conflict: str) -> _Result:
    """INSERT row(s), silently skipping any that violate on_conflict (ON CONFLICT DO NOTHING)."""
    headers = {**_HEADERS, "Prefer": "resolution=ignore-duplicates,return=representation"}
    async with _client() as client:
        r = await client.post(
            f"{_BASE}/{table}",
            headers=headers,
            params={"on_conflict": on_conflict},
            json=data,
        )
        r.raise_for_status()
        return _Result(r.json())


async def update(table: str, filters: dict, data: dict) -> _Result:
    """PATCH rows matching filters. Filter values must be pre-formatted PostgREST
    operators (e.g. {"vehicle_number": "eq.MH12AB1234", "current_odometer": "lt.5000"})."""
    headers = {**_HEADERS, "Prefer": "return=representation"}
    async with _client() as client:
        r = await client.patch(f"{_BASE}/{table}", headers=headers, params=filters, json=data)
        r.raise_for_status()
        return _Result(r.json())
