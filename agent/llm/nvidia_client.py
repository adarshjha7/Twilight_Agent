"""
NVIDIA NIM free endpoints client (build.nvidia.com).

OpenAI-compatible /chat/completions API via httpx — no extra SDK needed.
Default model (vision + text/tools): nvidia/nemotron-3-nano-omni-30b-a3b-reasoning
— multimodal reasoning MoE, OCRBench v2 leader; thinking blocks are stripped in _extract_content.

Free tier: ~40 requests/minute per API key, no daily cap. Returns 429 when throttled.
"""

import asyncio
import base64
import io
import httpx
from loguru import logger
from config import config

# NVIDIA rejects inline base64 images larger than ~180KB.
# Keep a safety margin; b64 inflates bytes by ~33%.
_MAX_B64_CHARS = 170_000


def _prepare_image_b64(image_path: str) -> str:
    """Base64-encode the image, compressing with Pillow if it exceeds NVIDIA's inline limit."""
    with open(image_path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode("utf-8")
    if len(b64) <= _MAX_B64_CHARS:
        return b64

    from PIL import Image

    img = Image.open(io.BytesIO(raw))
    if img.mode != "RGB":
        img = img.convert("RGB")

    # Progressively shrink until the base64 payload fits
    for max_dim, quality in [(1280, 85), (1024, 80), (900, 75), (768, 70), (640, 65)]:
        scaled = img.copy()
        scaled.thumbnail((max_dim, max_dim))
        buf = io.BytesIO()
        scaled.save(buf, format="JPEG", quality=quality)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        if len(b64) <= _MAX_B64_CHARS:
            logger.info(f"[NVIDIA] Image compressed to {max_dim}px q{quality} ({len(b64)} b64 chars)")
            return b64

    logger.warning(f"[NVIDIA] Image still {len(b64)} b64 chars after max compression — sending anyway")
    return b64


async def _chat_completions(payload: dict) -> dict:
    """POST to /chat/completions with one retry on 429 (rate limit)."""
    if not config.nvidia_api_key:
        raise EnvironmentError("NVIDIA_API_KEY is not set in .env")

    headers = {
        "Authorization": f"Bearer {config.nvidia_api_key}",
        "Accept": "application/json",
    }
    url = f"{config.nvidia_base_url}/chat/completions"

    async with httpx.AsyncClient(timeout=config.nvidia_timeout) as client:
        for attempt in (1, 2):
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code == 429 and attempt == 1:
                logger.warning("[NVIDIA] 429 rate limited — retrying in 3s")
                await asyncio.sleep(3)
                continue
            resp.raise_for_status()
            return resp.json()


def _extract_content(data: dict) -> str:
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    # Reasoning models (e.g. Nemotron 3 Omni) may emit <think>...</think> blocks
    # before the answer — strip them so stray braces never confuse JSON parsing.
    import re
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


async def generate(prompt: str) -> str:
    """Plain text generation — used for PDF/text-based extraction."""
    logger.debug(f"[NVIDIA/generate] {len(prompt)} chars — model: {config.nvidia_chat_model}")
    data = await _chat_completions({
        "model": config.nvidia_chat_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 4096,
    })
    return _extract_content(data)


async def generate_from_image(prompt: str, image_path: str) -> str:
    """Vision extraction — sends the image to Nemotron Nano VL."""
    logger.info(f"[NVIDIA/vision] Sending image: {image_path} — model: {config.nvidia_vision_model}")
    image_b64 = _prepare_image_b64(image_path)
    data = await _chat_completions({
        "model": config.nvidia_vision_model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ],
        }],
        "temperature": 0.1,
        "max_tokens": 4096,
    })
    return _extract_content(data)


async def chat_with_tools(system_prompt: str, user_message: str, tools: list) -> dict:
    """
    Tool-calling chat. Same contract as llm_client.chat_with_tools:
    returns {"tool_name", "tool_params"} or {"text": str}.
    """
    logger.info(f"[NVIDIA/chat] {len(tools)} tools available — model: {config.nvidia_chat_model}")
    data = await _chat_completions({
        "model": config.nvidia_chat_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "tools": tools,
        "temperature": 0,
        "max_tokens": 4096,
    })

    msg = (data.get("choices") or [{}])[0].get("message", {})
    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        fn = tool_calls[0].get("function", {})
        tool_name = fn.get("name", "")
        args = fn.get("arguments", {})
        if isinstance(args, str):
            import json
            try:
                args = json.loads(args) if args else {}
            except json.JSONDecodeError:
                logger.warning(f"[NVIDIA/chat] Could not parse tool arguments: {args}")
                args = {}
        logger.info(f"[NVIDIA/chat] LLM selected tool: {tool_name}")
        return {"tool_name": tool_name, "tool_params": args}

    content = msg.get("content", "")
    logger.warning("[NVIDIA/chat] No tool selected — plain text response")
    return {"text": content or ""}
