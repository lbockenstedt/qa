"""Entry point and control plane for the QA Auditor spoke.

Connects to the hub, runs the QA API in a background thread and wires the test
engine into both the spoke and the API.
"""
# Standard library: event loop, logging, CLI parsing, env vars and threads.
import asyncio
import logging
import argparse
import os
import threading
# uvicorn serves the FastAPI app; dotenv loads settings from a .env file.
import uvicorn
from pathlib import Path
from dotenv import load_dotenv

# Core control plane, hub TLS helpers, the QA spoke, test engine and API app.
from core.src.messaging.control_plane import BaseControlPlane
from hub_client import HUB_TLS_VERIFY_ENV, INSECURE_WS_ENV, _pin_hub_ca
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
            """Fallback logging setup: optional log file plus the console."""
            # Write to a file and the console only when a log file is given.
            handlers = ([logging.FileHandler(log_file), logging.StreamHandler()]
                        if log_file else None)
            # Apply the shared format, replacing any existing handlers.
            logging.basicConfig(level=default_level, force=True,
                                 format=_FMT, datefmt=_DFMT, handlers=handlers)


def _resolve_log_file(name: str):
    """Canonical /var/log/lm/<name>.log if writable, else a local logs/ fallback,
    else None (stderr only). So a file log always exists where it can."""
    # Try the system-wide log directory, checking the file can be opened for append.
    primary = f"/var/log/lm/{name}.log"
    try:
        os.makedirs("/var/log/lm", exist_ok=True)
        with open(primary, "a"):
            pass
        return primary
    # Not writable: use a logs/ folder next to this file instead.
    except OSError:
        try:
            local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
            os.makedirs(local, exist_ok=True)
            return os.path.join(local, f"{name}.log")
        # Neither works: log to stderr only.
        except OSError:
            return None


# Set up logging and the control plane's logger.
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
        """Pin the hub CA if given, normalise the hub URL, and store the QA settings."""
        # Trust the given CA bundle for the hub's certificate.
        if tls_ca_bundle:
            _pin_hub_ca(tls_ca_bundle)
        # A bare host gets the secure wss:// scheme.
        if hub_url and "://" not in hub_url:
            hub_url = f"wss://{hub_url.strip()}"
        # Base setup, then QA-specific settings (WebUI login defaults to admin/password).
        super().__init__(spoke_id, secret, hub_secret, hub_url)
        self.module_type = "qa"
        self.webui_creds = webui_creds or {"username": "admin", "password": "password"}
        self.ab_url = ab_url or ""
        self.api_port = api_port
        self.tls_ca_bundle = tls_ca_bundle

    def get_service_name(self) -> str:
        """Name of the systemd service that runs this spoke."""
        return "lm-qa"

    async def run(self):
        """Start the API thread, wire up the spoke and engine, then run the hub loop."""
        logger.info(f"Starting QA Auditor → {self.hub_url}")

        # Preserve the configured scheme before stripping it for the bare
        # hostname the WebSocket/REST clients expect. Only an explicit ws://
        # in self.hub_url marks the connection insecure — a missing hub_url
        # defaults to the secure wss:// path.
        insecure = bool(self.hub_url) and self.hub_url.strip().lower().startswith("ws://")
        raw_url = self.hub_url.strip() if self.hub_url else "localhost"
        without_scheme = raw_url.split("://", 1)[1] if "://" in raw_url else raw_url
        hub_host = without_scheme.split(":", 1)[0] or "localhost"

        # Guard the control plane's own WebSocket connection against cleartext secret exposure
        # before starting background services.
        if insecure and os.getenv(INSECURE_WS_ENV) != "1":
            raise ConnectionError(
                "Refusing to connect control plane to hub over plaintext ws:// — this would "
                f"send the shared secret in cleartext. Set {INSECURE_WS_ENV}=1 "
                "to allow this for local development."
            )
        # Without a CA bundle or verification enabled, warn that the hub cert is not checked.
        if not self.tls_ca_bundle and not insecure and os.getenv(HUB_TLS_VERIFY_ENV, "0").lower() not in ("1", "true", "yes"):
            logger.warning(
                "Connecting over wss:// without a CA certificate: core control plane "
                "connection will not verify the hub certificate (LM_HUB_TLS_VERIFY=0)."
            )

        # FastAPI sidecar in a background thread
        def _run_api():
            """Serve the FastAPI app with uvicorn (blocks this thread)."""
            cfg = uvicorn.Config(app, host="0.0.0.0", port=self.api_port, log_level="warning")
            uvicorn.Server(cfg).run()

        # Run it as a daemon thread so it stops with the process.
        t = threading.Thread(target=_run_api, daemon=True)
        t.start()
        logger.info(f"QA WebUI listening on :{self.api_port}")

        # Register the QA spoke with the control plane.
        qa_spoke = QASpoke(self.spoke_id, {})
        self.register_module("qa", qa_spoke)

        # Build the test engine with the hub connection details.
        engine = TestEngine(
            hub_host=hub_host,
            spoke_id=self.spoke_id,
            secret=self.secret,
            webui_creds=self.webui_creds,
            ab_url=self.ab_url,
            insecure=insecure,
            tls_ca_bundle=self.tls_ca_bundle,
        )
        # Give the engine to both the spoke and the API.
        await qa_spoke.set_engine(engine)
        set_engine(engine)

        # Run the hub connection loop.
        await super().run()


if __name__ == "__main__":
    # Load settings from .env before reading defaults from the environment.
    load_dotenv()

    # Command-line options, each defaulting to an environment variable.
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
    # Parse the command line.
    args = parser.parse_args()

    # Build the control plane and run it.
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
