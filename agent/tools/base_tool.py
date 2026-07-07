from abc import ABC, abstractmethod


class BaseTool(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Unique snake_case identifier shown to the LLM."""

    @property
    @abstractmethod
    def description(self) -> str:
        """One-sentence description the LLM uses to decide when to call this tool."""

    @property
    def parameters(self) -> dict:
        """JSON Schema for params the LLM can pass. Override to add params."""
        return {"type": "object", "properties": {}, "required": []}

    @abstractmethod
    async def execute(self, params: dict, context: dict):
        """
        params  — values the LLM passed when selecting this tool
        context — { text, file_path, media_type, message_id }
        """
