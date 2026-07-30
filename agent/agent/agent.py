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

# The "Maintenance Payments and Bills" WhatsApp group is dedicated to bills —
# routed straight to its own tool for both images and PDFs (no LLM tool
# selection needed, since there's only one tool), and text is ignored
# entirely (bills only ever arrive as image/PDF). Adding this tool never
# changes what any other monitored group's messages resolve to.
_MAINTENANCE_CHAT_KEYWORD = "maintenance"
_MAINTENANCE_TOOL_NAME = "extract_maintenance_bill"


def _is_maintenance_chat(chat_name: str) -> bool:
    return _MAINTENANCE_CHAT_KEYWORD in (chat_name or "").lower()


def _non_maintenance_tools() -> list[dict]:
    """Every registered tool except extract_maintenance_bill. Used for every
    non-maintenance chat's LLM tool-selection call (text messages, PDFs) so
    the maintenance tool is never even offered as a candidate there — mirrors
    the maintenance chat only ever seeing its own tool, in the other
    direction. Without this, registry.to_llm_tools() would hand the LLM every
    tool including this one, since they all share one global registry."""
    return [t for t in registry.to_llm_tools() if t["function"]["name"] != _MAINTENANCE_TOOL_NAME]


async def run(file_path: str, media_type: str, message_id: str, caption: str = "", chat_name: str = "") -> dict:
    logger.info(f"[Agent] Processing {media_type} — message_id: {message_id} — group: {chat_name}")

    context = {
        "file_path": file_path,
        "media_type": media_type,
        "message_id": message_id,
        "caption": caption,
        "chat_name": chat_name,
    }
    maintenance_chat = _is_maintenance_chat(chat_name)

    if media_type == "image" or (media_type == "pdf" and maintenance_chat):
        # Images always go directly to the model — no OCR, no tool selection
        # needed. PDFs in the maintenance group do too: the maintenance tool
        # sends Gemini the raw PDF bytes directly (matching the real backend's
        # extractBillJson()), so there's nothing to flatten through pdfplumber
        # first, and no ambiguity to resolve since this group only ever uses
        # one tool. PDFs in every other group still go through the pdfplumber
        # + tool-selection pipeline below, unchanged.
        tool_name = _MAINTENANCE_TOOL_NAME if maintenance_chat else "extract_petty_cash"
        tool = registry.get(tool_name)
        if not tool:
            raise ValueError(f"{tool_name} tool is not registered")
        logger.info(f"[Agent] {media_type} — using {tool_name} directly (no tool-selection LLM call)")
        result = await tool.execute({}, context)
        return {"success": True, "tool": tool_name, "result": result}

    if media_type == "text":
        # Maintenance bills only ever arrive as an image or PDF — a text
        # message in that group is ordinary chat (mentions, banter, etc.),
        # never bill data, so it's ignored outright rather than run through
        # the bill-extraction prompt (which would otherwise return a mostly
        # empty/garbage JSON for a chat message and fail on the missing
        # grand_total). No tool call, no reply, no reaction — quiet skip.
        if maintenance_chat:
            logger.info("[Agent] Text message in maintenance group — ignored (bills only arrive as image/PDF)")
            return {"success": False, "reason": "text messages are ignored in the maintenance group"}

        # Plain text messages (any group except maintenance, handled above):
        # let LLM reason and pick the right tool. Excludes the maintenance
        # tool from the candidates — it's never a valid choice outside its
        # own group (see _non_maintenance_tools).
        text = caption
        context["text"] = text
        tools = _non_maintenance_tools()
        logger.info(f"[Agent] Text message — available tools: {[t['function']['name'] for t in tools]}")
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

    # PDFs (non-maintenance groups only — see the shortcut above): extract
    # text then let LLM pick the right tool
    text = await extract_text_from_pdf(file_path)
    if not text or len(text) < 20:
        raise ValueError(f"Not enough text extracted from PDF ({len(text or '')} chars)")

    context["text"] = text

    # Same exclusion as the text branch — the maintenance tool is never a
    # candidate for a PDF in a non-maintenance group.
    tools = _non_maintenance_tools()
    logger.info(f"[Agent] Available tools: {[t['function']['name'] for t in tools]}")
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
