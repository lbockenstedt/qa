import asyncio
import json
import os
import ssl
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

# Local-dev escape hatch for a plaintext ws:// hub. Must be set explicitly;
# a configured wss:// URL is never downgraded by this variable.
INSECURE_WS_ENV = "QA_ALLOW_INSECURE_WS"

# The CA bundle has to reach TWO different clients, and they do not read the
# same thing. HubClient (below) builds its own ssl context from the path, but
# the spoke ALSO dials the hub through core's BaseControlPlane, and that one
# builds its context from LM_HUB_CA_CERT / LM_HUB_BUNDLE with verification
# gated behind LM_HUB_TLS_VERIFY (see core/src/messaging/control_plane.py
# _client_ssl_ctx). It never consults SSL_CERT_FILE, so setting that alone
# left the control-plane connection unpinned.
HUB_CA_ENV = "LM_HUB_CA_CERT"
HUB_TLS_VERIFY_ENV = "LM_HUB_TLS_VERIFY"


def _pin_hub_ca(ca_path):
    """Point every hub client at ``ca_path`` as the trust anchor.

    Uses assignment rather than ``setdefault``: an inherited LM_HUB_CA_CERT
    from the surrounding environment must not silently win over the path the
    operator passed on the command line. Also turns verification ON, since
    core defaults it OFF and would otherwise encrypt without authenticating
    the hub even though a CA was supplied.
    """
    if not ca_path:
        return
    os.environ[HUB_CA_ENV] = ca_path
    os.environ[HUB_TLS_VERIFY_ENV] = "1"
    # Kept for any stdlib client that builds a default context from the
    # environment; harmless, and no longer the only thing being set.
    os.environ.setdefault("SSL_CERT_FILE", ca_path)


class HubClient:
    """
    Dual-mode client for interacting with the Lab Manager Hub.
    Supports REST API for diagnostics and WebSocket for 'Ghost Tenant' interactions.
    """
    def __init__(self, hub_host: str, hub_port: int = 8000, ws_port: int = 8765,
                 spoke_id: Optional[str] = None, secret: Optional[str] = None,
                 insecure: bool = False, tls_ca_bundle: Optional[str] = None):
        self.hub_host = hub_host
        self.hub_port = hub_port
        self.ws_port = ws_port
        self.spoke_id = spoke_id
        self.secret = secret
        self.ws = None
        # True only when the caller resolved the configured hub URL scheme to
        # plaintext ws://. Never set based on this client's own defaults.
        self.insecure = insecure
        self.tls_ca_bundle = tls_ca_bundle

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

    def _build_ws_url(self) -> str:
        scheme = "ws" if self.insecure else "wss"
        return f"{scheme}://{self.hub_host}:{self.ws_port}"

    def _get_ssl_context(self) -> Optional[ssl.SSLContext]:
        """None for plaintext ws://; otherwise a verifying context (default
        trust store, or the configured CA bundle for a self-signed hub)."""
        if self.insecure:
            return None
        if self.tls_ca_bundle:
            return ssl.create_default_context(cafile=self.tls_ca_bundle)
        return ssl.create_default_context()

    async def connect(self):
        """Performs the authentication handshake and maintains the connection."""
        if not self.spoke_id or not self.secret:
            raise ValueError("spoke_id and secret are required for WebSocket connectivity.")

        # Refuse to send the shared secret over a plaintext connection unless
        # the operator has explicitly opted in for local development.
        if self.insecure:
            if os.getenv(INSECURE_WS_ENV) != "1":
                raise ConnectionError(
                    "Refusing to connect to hub over plaintext ws:// — this would "
                    f"send the shared secret in cleartext. Set {INSECURE_WS_ENV}=1 "
                    "to allow this for local development."
                )
            logger.warning(
                f"{INSECURE_WS_ENV}=1 is set — connecting over plaintext ws://. "
                "The shared secret will be sent unencrypted. Do not use in production."
            )

        url = self._build_ws_url()
        try:
            self.ws = await websockets.connect(url, ssl=self._get_ssl_context())

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
