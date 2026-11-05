"""HTTP and WebSocket API for the QA service.

Exposes run status, logs and a run trigger over REST, streams log history over a
WebSocket, and keeps the shared state of the current test session.
"""
# FastAPI app, request types and response helpers.
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks, Query
from fastapi.responses import FileResponse, JSONResponse
# asyncio and threading run the test engine without blocking requests.
import asyncio
import logging
import threading
# Type hints only.
from typing import Optional, List

# Log at INFO and use a named logger for the API.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("QA_API")

# The web application.
app = FastAPI(title="AppBuilder QA Service")

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

class TestSession:
    """State of the current (or last) QA run, shared between the API and the engine."""
    def __init__(self):
        """Start idle with no logs or results."""
        # Logs, results, current step, overall status and optional module filter.
        self.logs: list = []
        self.results: list = []
        self.current_step: str = "Idle"
        self.status: str = "IDLE"   # IDLE | RUNNING | COMPLETED | FAILED
        self.module_filter: Optional[str] = None
        # Guards the state, which the engine thread updates while the API reads it.
        self._lock = threading.Lock()

    def reset(self, module: Optional[str] = None):
        """Reset test session state and prepare for a new run."""
        # Clear previous output and mark the run as started.
        with self._lock:
            self.logs = []
            self.results = []
            self.current_step = "Starting"
            self.status = "RUNNING"
            self.module_filter = module

    def log(self, message: str, level: str = "INFO"):
        """Add a log entry to the session and the service log."""
        # Store the entry under the lock, then also write it to the service log.
        entry = {"level": level, "message": message}
        with self._lock:
            self.logs.append(entry)
        logger.info("[%s] %s", level, message)

    def step(self, name: str, status: str = "RUNNING"):
        """Record the current step and, if it is final, the run's overall status."""
        # Update the step; COMPLETED and FAILED also set the run status.
        with self._lock:
            self.current_step = name
            if status in ("COMPLETED", "FAILED"):
                self.status = status
        # Log the step change.
        self.log(f"Step: {name} → {status}", "INFO")

    def add_result(self, name: str, status: str, error: str = None):
        """Record a test scenario execution result."""
        # Store the result under the lock.
        with self._lock:
            self.results.append({"name": name, "status": status, "error": error})
        # Log it as SUCCESS for a pass, otherwise ERROR.
        level = "SUCCESS" if status == "PASS" else "ERROR"
        self.log(f"{name}: {status}" + (f" — {error}" if error else ""), level)


# The single shared session.
session = TestSession()

# Keep backward-compatible module-level helpers used by qa_engine.py
def log_event(message: str, level: str = "INFO"):
    """Log a message to the shared session."""
    session.log(message, level)

def update_step(step_name: str, status: str = "RUNNING"):
    """Record a step change on the shared session."""
    session.step(step_name, status)

def add_result(name: str, status: str, error: str = None):
    """Record a test result on the shared session."""
    session.add_result(name, status, error)


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def read_index():
    """Serve the WebUI page, or a JSON summary if it cannot be served."""
    # Serve the bundled WebUI page.
    try:
        return FileResponse("webui/index.html")
    # Fall back to a small JSON status.
    except Exception:
        return JSONResponse({"service": "QA API", "status": session.status})


@app.get("/api/status")
async def get_status(module: Optional[str] = Query(None)):
    """Return the run status, current step and results, optionally filtered by module."""
    # Copy the results under the lock.
    with session._lock:
        results = list(session.results)
    # Keep only results whose name contains the module, case-insensitively.
    if module:
        results = [r for r in results if module.lower() in r["name"].lower()]
    # Count passes for the summary.
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
    """Return a copy of all log entries from the current run."""
    # Copy under the lock so the list cannot change while it is returned.
    with session._lock:
        return list(session.logs)


@app.post("/api/run")
async def trigger_run(request_data: dict = None, background_tasks: BackgroundTasks = None):
    """Trigger a QA run, optionally scoped to a specific module.

    Body (optional JSON): {"module": "opnsense"}
    If module is omitted, the full suite runs.
    Called by AppBuilder after applying a fix to verify nothing regressed.
    """
    # Refuse to start a second run while one is in progress.
    if session.status == "RUNNING":
        return JSONResponse(
            {"status": "error", "detail": "A QA run is already in progress."},
            status_code=409,
        )

    # Optional module filter from the request body.
    module = None
    if request_data and isinstance(request_data, dict):
        module = request_data.get("module")

    # Reset the session for the new run and log what was requested.
    session.reset(module=module)
    logger.info("QA run triggered via API%s", f" (module={module})" if module else " (full suite)")

    # The actual test engine is injected at startup via set_engine().
    # If it's available, run in a background thread so this endpoint returns immediately.
    if _engine_ref[0] is not None:
        def _run():
            """Run the engine on its own event loop in this thread."""
            # A fresh event loop for this thread.
            import asyncio as _asyncio
            loop = _asyncio.new_event_loop()
            _asyncio.set_event_loop(loop)
            # Run the requested module (or the full suite), then close the loop.
            try:
                loop.run_until_complete(_engine_ref[0].run_module(module))
            finally:
                loop.close()
        # Start the run in the background and return straight away.
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return {"status": "started", "module": module}

    # No engine yet: the service is still starting.
    return JSONResponse(
        {"status": "error", "detail": "TestEngine not initialised — QA service starting up."},
        status_code=503,
    )


@app.get("/api/health")
async def health():
    """Liveness check that also reports the QA run status."""
    return {"status": "ok", "qa_status": session.status}


# ---------------------------------------------------------------------------
# WebSocket log stream
# ---------------------------------------------------------------------------

# Open log-stream WebSocket connections.
_ws_clients: List[WebSocket] = []

@app.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket):
    """Send the log history to a WebSocket client, then hold the connection open."""
    # Accept the connection and remember it.
    await websocket.accept()
    _ws_clients.append(websocket)
    # Send a copy of the log history.
    try:
        with session._lock:
            history = list(session.logs)
        await websocket.send_json({"type": "history", "data": history})
        # Keep the connection open until the client goes away.
        while True:
            await asyncio.sleep(1)
    # Disconnects and errors simply end the stream.
    except (WebSocketDisconnect, Exception):
        pass
    # Always forget the client.
    finally:
        _ws_clients.remove(websocket)


# Engine reference — injected from main.py after startup
_engine_ref: list = [None]

def set_engine(engine):
    """Register the test engine that /api/run uses."""
    # Store the engine for /api/run.
    _engine_ref[0] = engine
    logger.info("TestEngine registered with API server")
