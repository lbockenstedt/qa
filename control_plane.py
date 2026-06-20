import asyncio
import logging
import argparse
import os
import threading
import uvicorn
from pathlib import Path
from dotenv import load_dotenv

from core.src.messaging.control_plane import BaseControlPlane
from qa_spoke import QASpoke
from test_engine import TestEngine
from api_server import app, set_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("QAControlPlane")


class QAControlPlane(BaseControlPlane):
    """
    Control plane for the QA Auditor spoke.

    Runs the LM spoke WebSocket loop alongside a FastAPI sidecar that exposes
    test results over HTTP/WebSocket (port 8090 by default, avoids conflict
    with the hub's port 8000).
    """

    def __init__(self, spoke_id: str, secret: str, hub_secret: str = None,
                 hub_url: str = None, webui_creds: dict = None,
                 bugfixer_url: str = None, api_port: int = 8090):
        super().__init__(spoke_id, secret, hub_secret, hub_url)
        self.module_type = "qa"
        self.webui_creds = webui_creds or {"username": "admin", "password": "password"}
        self.bugfixer_url = bugfixer_url or ""
        self.api_port = api_port

    def get_service_name(self) -> str:
        return "lm-qa"

    async def run(self):
        logger.info(f"Starting QA Auditor → {self.hub_url}")

        # FastAPI sidecar in a background thread
        def _run_api():
            cfg = uvicorn.Config(app, host="0.0.0.0", port=self.api_port, log_level="warning")
            uvicorn.Server(cfg).run()

        t = threading.Thread(target=_run_api, daemon=True)
        t.start()
        logger.info(f"QA WebUI listening on :{self.api_port}")

        hub_host = (self.hub_url.replace("wss://", "").replace("ws://", "").split(":")[0]
                    if self.hub_url else "localhost")

        qa_spoke = QASpoke(self.spoke_id, {})
        self.register_module("qa", qa_spoke)

        engine = TestEngine(
            hub_host=hub_host,
            spoke_id=self.spoke_id,
            secret=self.secret,
            webui_creds=self.webui_creds,
            bugfixer_url=self.bugfixer_url,
        )
        await qa_spoke.set_engine(engine)
        set_engine(engine)

        await super().run()


if __name__ == "__main__":
    load_dotenv()

    parser = argparse.ArgumentParser(description="Lab Manager QA Auditor Spoke")
    parser.add_argument("--id",         default=os.getenv("SPOKE_ID", "qa-spoke-1"))
    parser.add_argument("--secret",     default=os.getenv("SPOKE_SECRET", ""))
    parser.add_argument("--hub-secret", default=os.getenv("HUB_SECRET", ""))
    parser.add_argument("--hub",        default=os.getenv("HUB_URL", "ws://localhost:8765"))
    parser.add_argument("--user",       default=os.getenv("LM_USER", "admin"))
    parser.add_argument("--password",   default=os.getenv("LM_PASSWORD", "password"))
    parser.add_argument("--bugfixer",   default=os.getenv("BUGFIXER_URL", ""))
    parser.add_argument("--api-port",   type=int, default=int(os.getenv("QA_API_PORT", "8090")))
    args = parser.parse_args()

    cp = QAControlPlane(
        spoke_id=args.id,
        secret=args.secret,
        hub_secret=args.hub_secret,
        hub_url=args.hub,
        webui_creds={"username": args.user, "password": args.password},
        bugfixer_url=args.bugfixer,
        api_port=args.api_port,
    )
    asyncio.run(cp.run())
