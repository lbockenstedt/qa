from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks, Query
from fastapi.responses import FileResponse, JSONResponse
import asyncio
import logging
import threading
from typing import Optional, List

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("QA_API")

app = FastAPI(title="BugFixer QA Service")

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

class TestSession:
    def __init__(self):
        self.logs: list = []
        self.results: list = []
        self.current_step: str = "Idle"
        self.status: str = "IDLE"   # IDLE | RUNNING | COMPLETED | FAILED
        self.module_filter: Optional[str] = None
        self._lock = threading.Lock()

    def reset(self, module: Optional[str] = None):
        with self._lock:
            self.logs = []
            self.results = []
            self.current_step = "Starting"
            self.status = "RUNNING"
            self.module_filter = module

    def log(self, message: str, level: str = "INFO"):
        entry = {"level": level, "message": message}
        with self._lock:
            self.logs.append(entry)
        logger.info("[%s] %s", level, message)

    def step(self, name: str, status: str = "RUNNING"):
        with self._lock:
            self.current_step = name
            if status in ("COMPLETED", "FAILED"):
                self.status = status
        self.log(f"Step: {name} → {status}", "INFO")

    def add_result(self, name: str, status: str, error: str = None):
        with self._lock:
            self.results.append({"name": name, "status": status, "error": error})
        level = "SUCCESS" if status == "PASS" else "ERROR"
        self.log(f"{name}: {status}" + (f" — {error}" if error else ""), level)


session = TestSession()

# Keep backward-compatible module-level helpers used by test_engine.py
def log_event(message: str, level: str = "INFO"):
    session.log(message, level)

def update_step(step_name: str, status: str = "RUNNING"):
    session.step(step_name, status)

def add_result(name: str, status: str, error: str = None):
    session.add_result(name, status, error)


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def read_index():
    try:
        return FileResponse("webui/index.html")
    except Exception:
        return JSONResponse({"service": "QA API", "status": session.status})


@app.get("/api/status")
async def get_status(module: Optional[str] = Query(None)):
    with session._lock:
        results = list(session.results)
    if module:
        results = [r for r in results if module.lower() in r["name"].lower()]
    passed = sum(1 for r in results if r.get("status") == "PASS")
    return {
        "status": session.status,
        "current_step": session.current_step,
        "module_filter": session.module_filter,
        "results": results,
        "summary": f"{passed}/{len(results)} passed" if results else "no results",
    }


@app.get("/api/logs")
async def get_logs():
    with session._lock:
        return list(session.logs)


@app.post("/api/run")
async def trigger_run(request_data: dict = None, background_tasks: BackgroundTasks = None):
    """Trigger a QA run, optionally scoped to a specific module.

    Body (optional JSON): {"module": "opnsense"}
    If module is omitted, the full suite runs.
    Called by BugFixer after applying a fix to verify nothing regressed.
    """
    if session.status == "RUNNING":
        return JSONResponse(
            {"status": "error", "detail": "A QA run is already in progress."},
            status_code=409,
        )

    module = None
    if request_data and isinstance(request_data, dict):
        module = request_data.get("module")

    session.reset(module=module)
    logger.info("QA run triggered via API%s", f" (module={module})" if module else " (full suite)")

    # The actual test engine is injected at startup via set_engine().
    # If it's available, run in a background thread so this endpoint returns immediately.
    if _engine_ref[0] is not None:
        def _run():
            import asyncio as _asyncio
            loop = _asyncio.new_event_loop()
            _asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_engine_ref[0].run_module(module))
            finally:
                loop.close()
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return {"status": "started", "module": module}

    return JSONResponse(
        {"status": "error", "detail": "TestEngine not initialised — QA service starting up."},
        status_code=503,
    )


@app.get("/api/health")
async def health():
    return {"status": "ok", "qa_status": session.status}


# ---------------------------------------------------------------------------
# WebSocket log stream
# ---------------------------------------------------------------------------

_ws_clients: List[WebSocket] = []

@app.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket):
    await websocket.accept()
    _ws_clients.append(websocket)
    try:
        with session._lock:
            history = list(session.logs)
        await websocket.send_json({"type": "history", "data": history})
        while True:
            await asyncio.sleep(1)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _ws_clients.remove(websocket)


# Engine reference — injected from main.py after startup
_engine_ref: list = [None]

def set_engine(engine):
    _engine_ref[0] = engine
    logger.info("TestEngine registered with API server")
