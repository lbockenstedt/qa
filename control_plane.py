import asyncio
import logging
import argparse
import os
import threading
import uvicorn
from pathlib import Path
from dotenv import load_dotenv

from core.src.messaging.control_plane import BaseControlPlane
from hub_client import INSECURE_WS_ENV
from qa_spoke import QASpoke
from qa_engine import TestEngine
from api_server import app, set_engine

# Shared logging setup + a canonical local file log (contract req 6 +
# normalization). configure_logging gives the standard format + LOG_LEVEL env;
# log_file makes a Python FileHandler own /var/log/lm/qa.log so the file exists
# regardless of the systemd unit's stderr redirect, with a local fallback for a
# hand-run (non-root). Mirrors pxmx get_log_path.
try:
    from logging_setup import configure_logging
except ImportError:
    try:
        from core.src.logging_setup import configure_logging
    except ImportError:
        _FMT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        _DFMT = '%Y-%m-%d %H:%M:%S'
        def configure_logging(default_level=logging.INFO, *, log_file=None, **_):
            handlers = ([logging.FileHandler(log_file), logging.StreamHandler()]
                        if log_file else None)
            logging.basicConfig(level=default_level, force=True,
                                 format=_FMT, datefmt=_DFMT, handlers=handlers)


def _resolve_log_file(name: str):
    """Canonical /var/log/lm/<name>.log if writable, else a local logs/ fallback,
    else None (stderr only). So a file log always exists where it can."""
    primary = f"/var/log/lm/{name}.log"
    try:
        os.makedirs("/var/log/lm", exist_ok=True)
        with open(primary, "a"):
            pass
        return primary
    except OSError:
        try:
            local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
            os.makedirs(local, exist_ok=True)
            return os.path.join(local, f"{name}.log")
        except OSError:
            return None


configure_logging(log_file=_resolve_log_file("qa"))
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
                 ab_url: str = None, api_port: int = 8090, tls_ca_bundle: str = None):
        super().__init__(spoke_id, secret, hub_secret, hub_url)
        self.module_type = "qa"
        self.webui_creds = webui_creds or {"username": "admin", "password": "password"}
        self.ab_url = ab_url or ""
        self.api_port = api_port
        self.tls_ca_bundle = tls_ca_bundle

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

        # Preserve the configured scheme before stripping it for the bare
        # hostname the WebSocket/REST clients expect. Only an explicit ws://
        # in self.hub_url marks the connection insecure — a missing hub_url
        # defaults to the secure wss:// path.
        insecure = bool(self.hub_url) and self.hub_url.strip().lower().startswith("ws://")
        hub_host = (self.hub_url.replace("wss://", "").replace("ws://", "").split(":")[0]
                    if self.hub_url else "localhost")

        # Guard the control plane's own WebSocket connection against cleartext secret exposure
        if insecure and os.getenv(INSECURE_WS_ENV) != "1":
            raise ConnectionError(
                "Refusing to connect control plane to hub over plaintext ws:// — this would "
                f"send the shared secret in cleartext. Set {INSECURE_WS_ENV}=1 "
                "to allow this for local development."
            )
        if self.tls_ca_bundle:
            os.environ.setdefault("SSL_CERT_FILE", self.tls_ca_bundle)

        qa_spoke = QASpoke(self.spoke_id, {})
        self.register_module("qa", qa_spoke)

        engine = TestEngine(
            hub_host=hub_host,
            spoke_id=self.spoke_id,
            secret=self.secret,
            webui_creds=self.webui_creds,
            ab_url=self.ab_url,
            insecure=insecure,
            tls_ca_bundle=self.tls_ca_bundle,
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
    parser.add_argument("--hub",        default=os.getenv("HUB_URL", "wss://localhost:8765"))
    parser.add_argument("--user",       default=os.getenv("LM_USER", "admin"))
    parser.add_argument("--password",   default=os.getenv("LM_PASSWORD", "password"))
    parser.add_argument("--ab",   default=os.getenv("AB_URL", ""))
    parser.add_argument("--api-port",   type=int, default=int(os.getenv("QA_API_PORT", "8090")))
    parser.add_argument("--tls-ca-cert", default=os.getenv("QA_HUB_CA_CERT"),
                         help="CA bundle for a self-signed hub certificate")
    args = parser.parse_args()

    cp = QAControlPlane(
        spoke_id=args.id,
        secret=args.secret,
        hub_secret=args.hub_secret,
        hub_url=args.hub,
        webui_creds={"username": args.user, "password": args.password},
        ab_url=args.ab,
        api_port=args.api_port,
        tls_ca_bundle=args.tls_ca_cert,
    )
    asyncio.run(cp.run())
