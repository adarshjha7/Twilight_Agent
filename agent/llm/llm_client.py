"""
LLM client dispatcher.

Public functions (generate, generate_from_image, chat_with_tools) call the
NVIDIA NIM free endpoints (nvidia_client.py), falling back to OpenRouter free
models (openrouter_client.py) if the NVIDIA call fails.

Tools import from this module — their code never changes when switching providers.
"""

from loguru import logger
from llm import nvidia_client, openrouter_client


async def generate(prompt: str) -> str:
    """Plain text generation — used for PDF/text-based extraction."""
    try:
        return await nvidia_client.generate(prompt)
    except Exception as err:
        logger.warning(f"[LLM] NVIDIA generate failed ({err}) — falling back to OpenRouter")
    return await openrouter_client.generate(prompt)


async def generate_from_image(prompt: str, image_path: str) -> str:
    """Vision extraction — NVIDIA with OpenRouter fallback."""
    try:
        return await nvidia_client.generate_from_image(prompt, image_path)
    except Exception as err:
        logger.warning(f"[LLM] NVIDIA vision failed ({err}) — falling back to OpenRouter")
    return await openrouter_client.generate_from_image(prompt, image_path)


async def chat_with_tools(system_prompt: str, user_message: str, tools: list) -> dict:
    """
    Sends a chat request with tool definitions.
    Returns {"tool_name": str, "tool_params": dict} if LLM picked a tool,
    or {"text": str} if it replied in plain text.
    """
    try:
        return await nvidia_client.chat_with_tools(system_prompt, user_message, tools)
    except Exception as err:
        logger.warning(f"[LLM] NVIDIA chat failed ({err}) — falling back to OpenRouter")
    return await openrouter_client.chat_with_tools(system_prompt, user_message, tools)
