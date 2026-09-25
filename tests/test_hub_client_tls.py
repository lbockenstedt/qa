"""Cover the transport-security guard in HubClient.connect().

Prior to this fix, HubClient always dialled ws:// and sent {"secret": ...} in
cleartext before ever checking the hub's HMAC proof -- anyone on the network
path, or a fake hub, could capture the shared secret. These tests pin: a
plaintext connection is refused (and the secret never sent) unless an explicit
opt-in env var is set, and a normal (insecure=False) connection uses wss://
with certificate verification.

Uses asyncio.run() directly rather than @pytest.mark.asyncio: CI installs only
`pytest` + `pytest-timeout` (see .github/workflows/ci.yml), no asyncio plugin,
so an asyncio marker would silently skip awaiting the coroutine and the test
would pass regardless of what connect() actually does.
"""
import asyncio
import ssl
from unittest.mock import AsyncMock, patch

from hub_client import HubClient, INSECURE_WS_ENV


def _client(insecure=False, tls_ca_bundle=None):
    return HubClient("hub.example.com", spoke_id="qa-1", secret="s3cret",
                      insecure=insecure, tls_ca_bundle=tls_ca_bundle)


def test_insecure_ws_without_opt_in_refuses_and_never_sends_secret(monkeypatch):
    monkeypatch.delenv(INSECURE_WS_ENV, raising=False)
    client = _client(insecure=True)

    with patch("hub_client.websockets.connect", new_callable=AsyncMock) as mock_connect:
        try:
            asyncio.run(client.connect())
            raised = False
        except ConnectionError:
            raised = True

    assert raised, "connect() must refuse a plaintext hub without the opt-in"
    mock_connect.assert_not_called()
    assert client.ws is None


def test_insecure_ws_with_opt_in_connects_and_warns(monkeypatch, caplog):
    monkeypatch.setenv(INSECURE_WS_ENV, "1")
    client = _client(insecure=True)

    mock_ws = AsyncMock()
    mock_ws.recv.return_value = '{"status": "HUB_VERIFIED", "challenge": "", "signature": ""}'
    with patch("hub_client.websockets.connect", new_callable=AsyncMock, return_value=mock_ws) as mock_connect:
        with patch("hmac.compare_digest", return_value=True):
            with caplog.at_level("WARNING"):
                asyncio.run(client.connect())

    args, kwargs = mock_connect.call_args
    assert args[0].startswith("ws://")
    assert kwargs.get("ssl") is None
    mock_ws.send.assert_any_call('{"spoke_id": "qa-1", "secret": "s3cret"}')
    assert any(INSECURE_WS_ENV in record.message for record in caplog.records
               if record.levelname == "WARNING")


def test_secure_default_connects_wss_with_verification(monkeypatch):
    monkeypatch.delenv(INSECURE_WS_ENV, raising=False)
    client = _client(insecure=False)

    mock_ws = AsyncMock()
    mock_ws.recv.return_value = '{"status": "HUB_VERIFIED", "challenge": "", "signature": ""}'
    with patch("hub_client.websockets.connect", new_callable=AsyncMock, return_value=mock_ws) as mock_connect:
        with patch("hmac.compare_digest", return_value=True):
            asyncio.run(client.connect())

    args, kwargs = mock_connect.call_args
    assert args[0].startswith("wss://")
    assert isinstance(kwargs.get("ssl"), ssl.SSLContext)
    assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
    mock_ws.send.assert_any_call('{"spoke_id": "qa-1", "secret": "s3cret"}')
