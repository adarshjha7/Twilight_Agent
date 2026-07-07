from loguru import logger
from media.pdf_parser import extract_text_from_pdf
from llm.llm_client import chat_with_tools
from agent.tool_registry import registry

SYSTEM_PROMPT = """You are an AI agent for a company. You receive documents (bills, invoices, receipts, reports, etc.)
sent via WhatsApp. Based on the document content, choose the most appropriate tool to process it.

Rules:
- Always choose exactly one tool.
- If no tool fits, reply in plain text explaining why.
- Do not add explanations — just make the tool call."""

TEXT_SYSTEM_PROMPT = """You are an AI agent for a company. You receive plain text WhatsApp messages
containing commands or transaction details. Choose the most appropriate tool to handle the message.

Guidance:
- Messages about setting/updating an opening balance for a month → set_opening_balance
  (e.g. "set opening balance for June which is 5000", "opening balance of uday cash yes bank for may as 3434")
- If an opening-balance message is missing the month or the amount, STILL call
  set_opening_balance with whatever fields you found — the tool itself asks the
  user for anything missing. NEVER ask for missing details in plain text.
- Messages containing transaction details (amount paid, UPI reference, payee) → extract_petty_cash
- A text command IS processable on its own — it does NOT need an attached document or screenshot.

Rules:
- Always choose exactly one tool.
- Only reply in plain text if the message truly matches no tool (e.g. casual chit-chat).
- Do not add explanations — just make the tool call."""


async def run(file_path: str, media_type: str, message_id: str, caption: str = "", chat_name: str = "") -> dict:
    logger.info(f"[Agent] Processing {media_type} — message_id: {message_id} — group: {chat_name}")

    context = {
        "file_path": file_path,
        "media_type": media_type,
        "message_id": message_id,
        "caption": caption,
        "chat_name": chat_name,
    }

    if media_type == "image":
        # Images go directly to the vision model — no OCR, no tool selection needed
        tool = registry.get("extract_petty_cash")
        if not tool:
            raise ValueError("extract_petty_cash tool is not registered")
        logger.info("[Agent] Image — using vision model directly")
        result = await tool.execute({}, context)
        return {"success": True, "tool": "extract_petty_cash", "result": result}

    if media_type == "text":
        # Plain text messages: let LLM reason and pick the right tool
        text = caption
        context["text"] = text
        tools = registry.to_llm_tools()
        logger.info(f"[Agent] Text message — available tools: {registry.list_names()}")
        decision = await chat_with_tools(TEXT_SYSTEM_PROMPT, f"Message:\n{text}", tools)

        if "text" in decision:
            # The LLM sometimes answers in plain text (asking for a missing
            # month/amount) instead of calling the tool — and plain text never
            # reaches WhatsApp. If the message has opening-balance intent, force
            # the tool: it parses the raw text itself and replies asking for
            # whatever is missing. Substring checks tolerate typos like
            # "openig abalcen".
            lower = text.lower()
            if "bal" in lower and ("open" in lower or "ob" in lower.split()):
                tool = registry.get("set_opening_balance")
                if tool:
                    logger.info("[Agent] LLM gave plain text but opening-balance intent detected — forcing set_opening_balance")
                    result = await tool.execute({}, context)
                    return {"success": True, "tool": "set_opening_balance", "result": result}
            logger.warning(f"[Agent] LLM did not select a tool: {decision['text']}")
            return {"success": False, "reason": decision["text"]}

        tool_name = decision["tool_name"]
        tool = registry.get(tool_name)
        if not tool:
            raise ValueError(f"LLM selected unknown tool: '{tool_name}'. Registered: {registry.list_names()}")

        logger.info(f"[Agent] Executing tool: {tool_name}")
        result = await tool.execute(decision["tool_params"], context)
        return {"success": True, "tool": tool_name, "result": result}

    # PDFs: extract text then let LLM pick the right tool
    text = await extract_text_from_pdf(file_path)
    if not text or len(text) < 20:
        raise ValueError(f"Not enough text extracted from PDF ({len(text or '')} chars)")

    context["text"] = text

    tools = registry.to_llm_tools()
    logger.info(f"[Agent] Available tools: {registry.list_names()}")
    decision = await chat_with_tools(SYSTEM_PROMPT, f"Document content:\n---\n{text}\n---", tools)

    if "text" in decision:
        logger.warning(f"[Agent] LLM did not select a tool: {decision['text']}")
        return {"success": False, "reason": decision["text"]}

    tool_name = decision["tool_name"]
    tool = registry.get(tool_name)
    if not tool:
        raise ValueError(f"LLM selected unknown tool: '{tool_name}'. Registered: {registry.list_names()}")

    logger.info(f"[Agent] Executing tool: {tool_name}")
    result = await tool.execute(decision["tool_params"], context)
    logger.info(f"[Agent] Tool '{tool_name}' completed successfully")
    return {"success": True, "tool": tool_name, "result": result}
