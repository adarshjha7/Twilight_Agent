"""
OpenRouter client (openrouter.ai) — fallback provider when NVIDIA fails.

OpenAI-compatible /chat/completions API via httpx.
Default model (vision + text/tools): nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free
— same model as the NVIDIA primary, so extraction behavior is identical on fallback.

Free tier: 20 req/min, 50 req/day shared across ALL :free models (1000/day if
$10 credits ever purchased). Failed requests count toward the daily quota, so
this client does NOT retry on 429 — quota is precious here.
"""

import json
import httpx
from loguru import logger
from config import config
from llm.nvidia_client import _prepare_image_b64


def _model_payload(model_csv: str) -> dict:
    """
    Supports comma-separated model lists. With multiple models, OpenRouter
    server-side falls back to the next model when the first is unavailable
    (e.g. Omni's NVIDIA-hosted free endpoint is degraded).
    """
    models = [m.strip() for m in model_csv.split(",") if m.strip()]
    if len(models) > 1:
        return {"model": models[0], "models": models}
    return {"model": models[0]}


async def _chat_completions(payload: dict) -> dict:
    if not config.openrouter_api_key:
        raise EnvironmentError("OPENROUTER_API_KEY is not set in .env")

    headers = {
        "Authorization": f"Bearer {config.openrouter_api_key}",
        "X-Title": "Agent AI WhatsApp",
    }
    url = f"{config.openrouter_base_url}/chat/completions"

    async with httpx.AsyncClient(timeout=config.openrouter_timeout) as client:
        resp = await client.post(url, json=payload, headers=headers)
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
    logger.debug(f"[OpenRouter/generate] {len(prompt)} chars — model: {config.openrouter_chat_model}")
    data = await _chat_completions({
        **_model_payload(config.openrouter_chat_model),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 4096,
    })
    return _extract_content(data)


async def generate_from_image(prompt: str, image_path: str) -> str:
    """Vision extraction — sends the image to the OpenRouter vision model."""
    logger.info(f"[OpenRouter/vision] Sending image: {image_path} — model: {config.openrouter_vision_model}")
    image_b64 = _prepare_image_b64(image_path)
    data = await _chat_completions({
        **_model_payload(config.openrouter_vision_model),
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
    logger.info(f"[OpenRouter/chat] {len(tools)} tools available — model: {config.openrouter_chat_model}")
    data = await _chat_completions({
        **_model_payload(config.openrouter_chat_model),
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
            try:
                args = json.loads(args) if args else {}
            except json.JSONDecodeError:
                logger.warning(f"[OpenRouter/chat] Could not parse tool arguments: {args}")
                args = {}
        logger.info(f"[OpenRouter/chat] LLM selected tool: {tool_name}")
        return {"tool_name": tool_name, "tool_params": args}

    content = msg.get("content", "")
    logger.warning("[OpenRouter/chat] No tool selected — plain text response")
    return {"text": content or ""}
