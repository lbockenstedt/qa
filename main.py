import asyncio
import argparse
import logging
import threading
import httpx
import uvicorn
from fastapi import FastAPI
from core.src.messaging.control_plane import BaseControlPlane
from qa_spoke import QASpoke
from test_engine import TestEngine
from api_server import app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("QA_Main")

def run_api_server():
    """Runs the FastAPI server in a background thread."""
    config = uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="info")
    server = uvicorn.Server(config)
    asyncio.run(server.serve())

async def fetch_first_secret(hub_host: str, spoke_id: str):
    """Fetches the initial onboarding secret from the Hub API."""
    url = f"http://{hub_host}:8000/setup/generate-secret"
    payload = {"spoke_id": spoke_id}
    logger.info(f"Requesting first_secret from Hub at {url} for {spoke_id}...")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            secret = data.get("secret")
            if not secret:
                raise ValueError("Hub response did not contain a secret")
            logger.info("Successfully retrieved first_secret from Hub.")
            return secret
    except Exception as e:
        logger.error(f"Failed to fetch first_secret: {e}")
        return None

async def main():
    parser = argparse.ArgumentParser(description="Lab Manager QA Auditor (Managed Spoke)")
    parser.add_argument("--hub", default="localhost", help="Hub hostname")
    parser.add_argument("--spoke-id", required=True, help="Spoke ID for authentication")
    parser.add_argument("--secret", help="Shared secret (optional, will fetch from Hub if missing)")
    parser.add_argument("--user", default="admin", help="WebUI username")
    parser.add_argument("--password", default="password", help="WebUI password")

    args = parser.parse_args()

    # 1. Handle Secret Onboarding
    secret = args.secret
    if not secret:
        secret = await fetch_first_secret(args.hub, args.spoke_id)
        if not secret:
            logger.error("Could not obtain secret. Exiting.")
            return

    # 2. Start the QA WebUI Server as a sidecar
    api_thread = threading.Thread(target=run_api_server, daemon=True)
    api_thread.start()
    logger.info("QA WebUI Server started on port 8080")

    # 3. Setup the Control Plane
    # We use the BaseControlPlane to handle Hub connectivity and authentication
    plane = BaseControlPlane(
        spoke_id=args.spoke_id,
        secret=secret,
        hub_url=f"ws://{args.hub}:8765"
    )

    # 4. Create and Register the QA Spoke
    qa_spoke = QASpoke(args.spoke_id, config={})
    plane.register_module("qa", qa_spoke)

    # 5. Link the TestEngine
    # The TestEngine needs a HubClient to actually perform the tests.
    # We instantiate it and inject it into the spoke.
    webui_creds = {"username": args.user, "password": args.password}
    engine = TestEngine(
        hub_host=args.hub,
        spoke_id=args.spoke_id,
        secret=secret,
        webui_creds=webui_creds
    )
    await qa_spoke.set_engine(engine)

    logger.info(f"QA Auditor launched as managed spoke: {args.spoke_id}")
    try:
        await plane.run()
    except Exception as e:
        logger.error(f"Control Plane crashed: {e}")

if __name__ == "__main__":
    asyncio.run(main())
