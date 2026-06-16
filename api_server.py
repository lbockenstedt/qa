from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import asyncio
import json
import logging
from typing import List

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("QA_API")

app = FastAPI()

# Store for active test sessions and their logs
class TestSession:
    def __init__(self):
        self.logs = []
        self.results = []
        self.current_step = "Idle"
        self.status = "IDLE" # IDLE, RUNNING, COMPLETED, FAILED

session = TestSession()

@app.get("/")
async def read_index():
    return FileResponse("webui/index.html")

@app.get("/api/status")
async def get_status():
    return {
        "status": session.status,
        "current_step": session.current_step,
        "results": session.results
    }

@app.get("/api/logs")
async def get_logs():
    return session.logs

@app.websocket("/ws/logs")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    # Send current logs first
    await websocket.send_json({"type": "history", "data": session.logs})

    # Keep the connection open to push new logs
    # We'll use a simple loop and a queue in the actual implementation
    # For now, just keep it open.
    try:
        while True:
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        logger.info("WebUI disconnected")

def log_event(message: str, level: str = "INFO"):
    """Called by the TestEngine to push logs to the UI."""
    log_entry = {"level": level, "message": message}
    session.logs.append(log_entry)
    # In a real implementation, we'd push this via the WebSocket list
    logger.info(f"[{level}] {message}")

def update_step(step_name: str, status: str = "RUNNING"):
    session.current_step = step_name
    session.status = status
    log_event(f"Transitioned to step: {step_name}", "INFO")

def add_result(name: str, status: str, error: str = None):
    session.results.append({"name": name, "status": status, "error": error})
    log_event(f"Scenario {name} finished: {status}", "SUCCESS" if status == "PASS" else "ERROR")
