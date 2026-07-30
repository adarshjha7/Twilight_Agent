"""
Gemini REST client for maintenance bill extraction only.

Mirrors the real backend's extractBillJson()
(twilight-fleetzen-backend/src/maintenance/maintenance.service.ts) so a bill
sent over WhatsApp is parsed by the exact same model, prompt, and forced-JSON
response mode as the web "Upload Bill" dialog's auto-fill — for identical
extraction quality between the two entry points. Petty cash and every other
tool keep using llm_client's NVIDIA→OpenRouter chain; this client is not part
of that dispatcher on purpose.
"""

import base64
import mimetypes
import httpx
from loguru import logger
from config import config

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


async def _call_gemini(prompt: str, file_path: str | None, api_key: str) -> httpx.Response:
    parts = [{"text": prompt}]
    if file_path:
        mime_type = mimetypes.guess_type(file_path)[0] or "application/pdf"
        with open(file_path, "rb") as f:
            data_b64 = base64.b64encode(f.read()).decode("utf-8")
        parts.append({"inline_data": {"mime_type": mime_type, "data": data_b64}})

    url = _ENDPOINT.format(model=config.gemini_model)
    headers = {"Content-Type": "application/json", "X-goog-api-key": api_key}
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseMimeType": "application/json"},
    }

    async with httpx.AsyncClient(timeout=config.gemini_timeout) as client:
        return await client.post(url, headers=headers, json=body)


async def extract_bill_json(prompt: str, file_path: str | None = None) -> str:
    """Calls Gemini with the extraction prompt and, if given, an attached file
    sent as inline_data (base64) — an image or PDF, exactly as the backend
    passes it, never pre-flattened through OCR/text-extraction first, so
    Gemini sees the bill the same way it does via the web upload dialog.
    Returns the raw response text (already JSON, since generationConfig
    forces responseMimeType to application/json — same as the backend)."""
    if not config.gemini_api_key:
        raise EnvironmentError("GEMINI_API_KEY is not set in .env")

    resp = await _call_gemini(prompt, file_path, config.gemini_api_key)

    # Free-tier quota exhausted on the primary key (a busy day of bills) —
    # retry once on the fallback key before giving up.
    if resp.status_code == 429 and config.gemini_api_key_fallback:
        logger.warning("[Gemini] Primary key quota reached — retrying with fallback key")
        resp = await _call_gemini(prompt, file_path, config.gemini_api_key_fallback)

    if resp.status_code != 200:
        # Same friendly-429 handling as the backend — Gemini's free-tier quota
        # error body is a deeply nested details/links/quotaDimensions blob,
        # not worth surfacing raw.
        if resp.status_code == 429:
            raise RuntimeError("Gemini automatic parsing limit reached — please try again later.")
        detail = resp.text
        try:
            detail = resp.json().get("error", {}).get("message", detail)
        except Exception:
            pass
        raise RuntimeError(f"Gemini API error ({resp.status_code}): {detail}")

    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        text = None
    if not text:
        raise RuntimeError("Gemini returned no extractable content for this bill")

    logger.info(f"[Gemini] Extracted {len(text)} chars using model {config.gemini_model}")
    return text
