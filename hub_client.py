"""Client the QA engine uses to talk to the Lab Manager hub.

Calls the hub's REST API for status and diagnostics, and can join the hub over a
signed, mutually authenticated WebSocket to send commands like a real spoke.
"""
# Standard library: event loop, JSON, env vars, TLS, ids, time and HMAC signing.
import asyncio
import json
import os
import ssl
import uuid
import time
import hmac
import hashlib
import logging
# httpx for REST calls; websockets for the hub connection.
import httpx
import websockets
# Type hints only.
from typing import Dict, Any, Optional

# Log at INFO and use a named logger for the client.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("HubClient")

# Local-dev escape hatch for a plaintext ws:// hub. Must be set explicitly;
# a configured wss:// URL is never downgraded by this variable.
INSECURE_WS_ENV = "QA_ALLOW_INSECURE_WS"

# The CA bundle has to reach TWO different clients, and they do not read the
# same thing. HubClient (below) builds its own ssl context from the path, but
# the spoke ALSO dials the hub through core's BaseControlPlane, and that one
# builds its context from LM_HUB_CA_CERT / LM_HUB_CA_BUNDLE with verification
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
    # Nothing to pin.
    if not ca_path:
        return
    # Set the CA path and turn verification on.
    os.environ[HUB_CA_ENV] = ca_path
    os.environ[HUB_TLS_VERIFY_ENV] = "1"


class HubClient:
    """
    Dual-mode client for interacting with the Lab Manager Hub.
    Supports REST API for diagnostics and WebSocket for 'Ghost Tenant' interactions.
    """
    def __init__(self, hub_host: str, hub_port: int = 8000, ws_port: int = 8765,
                 spoke_id: Optional[str] = None, secret: Optional[str] = None,
                 insecure: bool = False, tls_ca_bundle: Optional[str] = None):
        """Store the hub address, ports, spoke credentials and TLS settings."""
        # Hub address, ports and spoke credentials; no WebSocket until connect().
        self.hub_host = hub_host
        self.hub_port = hub_port
        self.ws_port = ws_port
        self.spoke_id = spoke_id
        self.secret = secret
        self.ws = None
        # True only when the caller resolved the configured hub URL scheme to
        # plaintext ws://. Never set based on this client's own defaults.
        self.insecure = insecure
        # Optional CA bundle for a self-signed hub certificate.
        self.tls_ca_bundle = tls_ca_bundle

    # --- REST API Methods ---

    async def request(self, method: str, endpoint: str, data: Dict = None) -> Dict[str, Any]:
        """Send a JSON REST request to the hub; errors come back as an ERROR dict."""
        # Build the hub REST URL.
        url = f"http://{self.hub_host}:{self.hub_port}{endpoint}"
        # Send the request with a 5s timeout and return the JSON body.
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.request(method, url, json=data)
                resp.raise_for_status()
                return resp.json()
        # Network or HTTP error: log it and return an ERROR dict.
        except Exception as e:
            logger.error(f"REST API Error ({endpoint}): {e}")
            return {"status": "ERROR", "message": str(e)}

    async def get_status(self):
        """Fetch the hub's /status."""
        return await self.request("GET", "/status")

    async def get_diagnostics(self):
        """Fetch the hub's setup diagnostics."""
        return await self.request("GET", "/setup/diagnostics")

    # --- WebSocket / Ghost Tenant Methods ---

    def _sign_message(self, message_dict: Dict[str, Any]) -> str:
        """Implements the HMAC-SHA256 signing required by the Hub."""
        # Exclude signature from the data being signed
        data = {k: v for k, v in message_dict.items() if k != "signature"}
        # Ensure deterministic JSON representation
        message_bytes = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
        # HMAC-SHA256 with the shared secret, as a hex string.
        return hmac.new(self.secret.encode(), message_bytes, hashlib.sha256).hexdigest()

    def _build_ws_url(self) -> str:
        """Build the hub WebSocket URL, plaintext only when insecure is set."""
        scheme = "ws" if self.insecure else "wss"
        return f"{scheme}://{self.hub_host}:{self.ws_port}"

    def _get_ssl_context(self) -> Optional[ssl.SSLContext]:
        """None for plaintext ws://; otherwise a verifying context (default
        trust store, or the configured CA bundle for a self-signed hub)."""
        # Plaintext: no TLS context.
        if self.insecure:
            return None
        # Verify against the given CA bundle, or the system trust store.
        if self.tls_ca_bundle:
            return ssl.create_default_context(cafile=self.tls_ca_bundle)
        return ssl.create_default_context()

    async def connect(self):
        """Performs the authentication handshake and maintains the connection."""
        # Both a spoke id and a secret are required.
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
            # Opted in: warn loudly.
            logger.warning(
                f"{INSECURE_WS_ENV}=1 is set — connecting over plaintext ws://. "
                "The shared secret will be sent unencrypted. Do not use in production."
            )

        # Open the WebSocket (with TLS unless insecure).
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

            # The hub proves its identity by signing a challenge with the shared secret.
            if proof.get("status") == "HUB_VERIFIED":
                challenge = proof.get("challenge")
                signature = proof.get("signature")

                # Verify Hub's identity
                expected_sig = hmac.new(self.secret.encode(), challenge.encode(), hashlib.sha256).hexdigest()
                # Signature matches: confirm to the hub.
                if hmac.compare_digest(expected_sig, signature):
                    logger.info("Hub identity verified.")
                    await self.ws.send(json.dumps({"status": "HUB_OK"}))
                # Signature mismatch: close and fail.
                else:
                    await self.ws.close(1008, "Hub identity mismatch")
                    raise ConnectionError("Hub identity verification failed.")
            # No proof sent: close and fail.
            else:
                await self.ws.close(1008, "Hub did not provide verification proof")
                raise ConnectionError("Hub failed to provide mutual authentication proof.")

            logger.info(f"Connected and authenticated as {self.spoke_id}")
        # Any failure: log it, drop the connection and re-raise.
        except Exception as e:
            logger.error(f"WebSocket Connection Failed: {e}")
            self.ws = None
            raise

    async def send_command(self, command_type: str, data: Dict[str, Any], timeout: float = 5.0) -> Dict[str, Any]:
        """Sends a signed command and waits for a response."""
        # Must be connected first.
        if not self.ws:
            raise ConnectionError("Not connected to Hub. Call connect() first.")

        # Build the message with a unique id, sender, destination and payload.
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

        # Sign it and send compact JSON.
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
        # No reply in time.
        except asyncio.TimeoutError:
            return {"status": "ERROR", "message": "Timed out waiting for response from Hub"}
        # Any other receive or parse error.
        except Exception as e:
            return {"status": "ERROR", "message": f"Exception during command: {str(e)}"}

    async def disconnect(self):
        """Close the hub WebSocket if it is open."""
        # Close and forget the connection.
        if self.ws:
            await self.ws.close()
            self.ws = None
            logger.info("Disconnected from Hub.")
