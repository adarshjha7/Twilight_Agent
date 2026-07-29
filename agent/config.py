import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), '..', '.env'))


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"Missing required env var: {key}")
    return val


class Config:
    # NVIDIA NIM free cloud endpoints, falling back to OpenRouter on error.
    nvidia_api_key: str = os.getenv("NVIDIA_API_KEY", "")
    nvidia_base_url: str = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
    nvidia_vision_model: str = os.getenv("NVIDIA_VISION_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning")
    nvidia_chat_model: str = os.getenv("NVIDIA_CHAT_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning")
    nvidia_timeout: int = int(os.getenv("NVIDIA_TIMEOUT_MS", "90000")) // 1000

    # OpenRouter — fallback when NVIDIA fails (free tier: 20 req/min, 50 req/day shared pool)
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_base_url: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    openrouter_vision_model: str = os.getenv("OPENROUTER_VISION_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free")
    openrouter_chat_model: str = os.getenv("OPENROUTER_CHAT_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free")
    openrouter_timeout: int = int(os.getenv("OPENROUTER_TIMEOUT_MS", "90000")) // 1000

    # Gemini — used only by the maintenance bill extractor, to match the real
    # backend's extractBillJson() (same model/prompt/JSON-mode as the web
    # "Upload Bill" dialog). Optional at startup — only required when that
    # tool actually runs, checked there rather than here, so the rest of the
    # agent (petty cash, etc.) works without it.
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    gemini_timeout: int = int(os.getenv("GEMINI_TIMEOUT_MS", "60000")) // 1000

    storage_dir: str = os.getenv("STORAGE_DIR", "./storage")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    supabase_url: str = _require("SUPABASE_URL")
    supabase_service_role_key: str = _require("SUPABASE_SERVICE_ROLE_KEY")


config = Config()
