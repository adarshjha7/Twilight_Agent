import sys
import os
import re

sys.path.insert(0, os.path.dirname(__file__))

from pathlib import Path
import json
from datetime import datetime
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from loguru import logger
from config import config

# ─── Register tools ───────────────────────────────────────────────────────────
from agent.tool_registry import registry
from tools.petty_cash_extractor_tool import PettyCashExtractorTool
from tools.set_opening_balance_tool import SetOpeningBalanceTool
from tools.maintenance_bill_extractor_tool import MaintenanceBillExtractorTool

registry.register(PettyCashExtractorTool())
registry.register(SetOpeningBalanceTool())
registry.register(MaintenanceBillExtractorTool())
# ─────────────────────────────────────────────────────────────────────────────

from agent.agent import run as agent_run

app = FastAPI(title="Agent AI", version="1.0.0")

# Logging setup
logger.remove()
logger.add(sys.stderr, level=config.log_level)
os.makedirs(os.path.join(os.path.dirname(__file__), '..', 'logs'), exist_ok=True)
logger.add(os.path.join(os.path.dirname(__file__), '..', 'logs', 'agent.log'), level=config.log_level, rotation="10 MB", retention=5)
logger.add(os.path.join(os.path.dirname(__file__), '..', 'logs', 'error.log'), level="ERROR")

# Serve frontend
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

# ─── Connected WebSocket clients ──────────────────────────────────────────────
connected_clients: list[WebSocket] = []


async def broadcast(data: dict):
    for ws in list(connected_clients):
        try:
            await ws.send_json(data)
        except Exception:
            connected_clients.remove(ws)


@app.get("/")
async def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    connected_clients.append(ws)
    logger.info(f"[WS] Client connected — {len(connected_clients)} total")
    try:
        while True:
            await ws.receive_text()  # keep connection alive
    except WebSocketDisconnect:
        connected_clients.remove(ws)
        logger.info("[WS] Client disconnected")


# ─── Process WhatsApp media ───────────────────────────────────────────────────
class ProcessRequest(BaseModel):
    file_path: str = ""
    media_type: str
    message_id: str
    caption: str = ""
    chat_name: str = ""


@app.post("/process")
async def process(req: ProcessRequest):
    logger.info(f"[API] /process — {req.media_type}  message_id={req.message_id}")
    try:
        outcome = await agent_run(
            file_path=req.file_path,
            media_type=req.media_type,
            message_id=req.message_id,
            caption=req.caption,
            chat_name=req.chat_name,
        )

        # Broadcast to chat UI
        if outcome.get("success"):
            await broadcast({
                "type": "wa_image",
                "chat": req.chat_name,
                "result": outcome.get("result"),
            })

        return outcome
    except Exception as err:
        logger.error(f"[API] Error: {err}")
        raise HTTPException(status_code=500, detail=str(err))


# ─── Chat with LLM ────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str


@app.post("/chat")
async def chat(req: ChatRequest):
    logger.info(f"[Chat] User: {req.message[:80]}")
    try:
        # Routed through the provider dispatcher (NVIDIA → OpenRouter fallback)
        from llm.llm_client import generate
        reply = await generate(
            "You are a helpful AI assistant for a company.\n\n"
            f"User: {req.message}\n\nAssistant:"
        )
        logger.info(f"[Chat] Reply: {reply[:80]}")
        return {"reply": reply}
    except Exception as err:
        logger.error(f"[Chat] Error: {err}")
        raise HTTPException(status_code=500, detail=str(err))


@app.get("/health")
async def health():
    return {"status": "ok", "tools": registry.list_names()}


if __name__ == "__main__":
    import uvicorn
    # /process and /chat have no authentication — the gateway reaches this
    # service via localhost, so never bind to 0.0.0.0 in production.
    # Set AGENT_BIND_HOST=0.0.0.0 explicitly only for LAN debugging.
    uvicorn.run(app, host=os.getenv("AGENT_BIND_HOST", "127.0.0.1"), port=int(os.getenv("AGENT_PORT", "8000")))
