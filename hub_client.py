import asyncio
import json
import uuid
import time
import hmac
import hashlib
import logging
import httpx
import websockets
from typing import Dict, Any, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("HubClient")

class HubClient:
    """
    Dual-mode client for interacting with the Lab Manager Hub.
    Supports REST API for diagnostics and WebSocket for 'Ghost Tenant' interactions.
    """
    def __init__(self, hub_host: str, hub_port: int = 8000, ws_port: int = 8765,
                 spoke_id: Optional[str] = None, secret: Optional[str] = None):
        self.hub_host = hub_host
        self.hub_port = hub_port
        self.ws_port = ws_port
        self.spoke_id = spoke_id
        self.secret = secret
        self.ws = None

    # --- REST API Methods ---

    async def request(self, method: str, endpoint: str, data: Dict = None) -> Dict[str, Any]:
        url = f"http://{self.hub_host}:{self.hub_port}{endpoint}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.request(method, url, json=data)
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            logger.error(f"REST API Error ({endpoint}): {e}")
            return {"status": "ERROR", "message": str(e)}

    async def get_status(self):
        return await self.request("GET", "/status")

    async def get_diagnostics(self):
        return await self.request("GET", "/setup/diagnostics")

    # --- WebSocket / Ghost Tenant Methods ---

    def _sign_message(self, message_dict: Dict[str, Any]) -> str:
        """Implements the HMAC-SHA256 signing required by the Hub."""
        # Exclude signature from the data being signed
        data = {k: v for k, v in message_dict.items() if k != "signature"}
        # Ensure deterministic JSON representation
        message_bytes = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
        return hmac.new(self.secret.encode(), message_bytes, hashlib.sha256).hexdigest()

    async def connect(self):
        """Performs the authentication handshake and maintains the connection."""
        if not self.spoke_id or not self.secret:
            raise ValueError("spoke_id and secret are required for WebSocket connectivity.")

        url = f"ws://{self.hub_host}:{self.ws_port}"
        try:
            self.ws = await websockets.connect(url)

            # 1. Send Authentication Request
            auth_req = {
                "spoke_id": self.spoke_id,
                "secret": self.secret
            }
            await self.ws.send(json.dumps(auth_req))
            logger.info(f"Sent auth request for {self.spoke_id}...")

            # 2. Mutual Authentication (Hub Identity Proof)
            proof_json = await asyncio.wait_for(self.ws.recv(), timeout=5.0)
            proof = json.loads(proof_json)

            if proof.get("status") == "HUB_VERIFIED":
                challenge = proof.get("challenge")
                signature = proof.get("signature")

                # Verify Hub's identity
                expected_sig = hmac.new(self.secret.encode(), challenge.encode(), hashlib.sha256).hexdigest()
                if hmac.compare_digest(expected_sig, signature):
                    logger.info("Hub identity verified.")
                    await self.ws.send(json.dumps({"status": "HUB_OK"}))
                else:
                    await self.ws.close(1008, "Hub identity mismatch")
                    raise ConnectionError("Hub identity verification failed.")
            else:
                await self.ws.close(1008, "Hub did not provide verification proof")
                raise ConnectionError("Hub failed to provide mutual authentication proof.")

            logger.info(f"Connected and authenticated as {self.spoke_id}")
        except Exception as e:
            logger.error(f"WebSocket Connection Failed: {e}")
            self.ws = None
            raise

    async def send_command(self, command_type: str, data: Dict[str, Any], timeout: float = 5.0) -> Dict[str, Any]:
        """Sends a signed command and waits for a response."""
        if not self.ws:
            raise ConnectionError("Not connected to Hub. Call connect() first.")

        msg_id = str(uuid.uuid4())
        message = {
            "header": {
                "message_id": msg_id,
                "timestamp": time.time(),
                "sender_id": self.spoke_id,
                "destination_id": "hub"
            },
            "payload": {
                "type": command_type,
                "data": data
            }
        }

        message["signature"] = self._sign_message(message)
        await self.ws.send(json.dumps(message, separators=(',', ':')))

        # Wait for response
        try:
            resp_json = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            resp = json.loads(resp_json)

            # Verify response correlation (if available)
            if resp.get("correlation_id") == msg_id or resp.get("header", {}).get("message_id") == msg_id:
                return resp

            # If we got a different message (like a heartbeat or asynchronous event),
            # in a real client we'd have a queue. For this QA tool, we'll just return the raw response.
            return resp
        except asyncio.TimeoutError:
            return {"status": "ERROR", "message": "Timed out waiting for response from Hub"}
        except Exception as e:
            return {"status": "ERROR", "message": f"Exception during command: {str(e)}"}

    async def disconnect(self):
        if self.ws:
            await self.ws.close()
            self.ws = None
            logger.info("Disconnected from Hub.")
