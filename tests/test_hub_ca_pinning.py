"""The hub CA bundle must reach BOTH hub clients, not just HubClient.

The QA spoke dials the hub twice: through HubClient (this repo) and through
core's BaseControlPlane. Only the first builds its ssl context from the path
we hold. BaseControlPlane builds its own from LM_HUB_CA_CERT / LM_HUB_BUNDLE,
gated on LM_HUB_TLS_VERIFY, and never consults SSL_CERT_FILE
(core/src/messaging/control_plane.py, _client_ssl_ctx).

So setting SSL_CERT_FILE alone left the control-plane connection unpinned --
silently, and precisely when the scheme had just been switched to wss://
against a self-signed hub. These tests pin the contract down.
"""
import os

import pytest

from hub_client import HUB_CA_ENV, HUB_TLS_VERIFY_ENV, _pin_hub_ca

_VARS = (HUB_CA_ENV, HUB_TLS_VERIFY_ENV, "SSL_CERT_FILE")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in _VARS:
        monkeypatch.delenv(v, raising=False)


def test_pins_the_ca_for_core_control_plane():
    _pin_hub_ca("/etc/ssl/hub-ca.pem")
    assert os.environ[HUB_CA_ENV] == "/etc/ssl/hub-ca.pem"


def test_enables_verification():
    """core defaults LM_HUB_TLS_VERIFY to 0, which encrypts without
    authenticating the hub. Supplying a CA has to turn verification on, or
    pinning it accomplishes nothing."""
    _pin_hub_ca("/etc/ssl/hub-ca.pem")
    assert os.environ[HUB_TLS_VERIFY_ENV] == "1"


def test_explicit_path_overrides_an_inherited_one(monkeypatch):
    """setdefault was the original bug: an inherited value silently won over
    the path the operator passed on the command line."""
    monkeypatch.setenv(HUB_CA_ENV, "/stale/inherited.pem")
    _pin_hub_ca("/etc/ssl/hub-ca.pem")
    assert os.environ[HUB_CA_ENV] == "/etc/ssl/hub-ca.pem"


def test_still_sets_ssl_cert_file_for_stdlib_clients():
    _pin_hub_ca("/etc/ssl/hub-ca.pem")
    assert os.environ["SSL_CERT_FILE"] == "/etc/ssl/hub-ca.pem"


def test_no_ca_is_a_no_op():
    """No CA must not flip verification on -- that would hard-fail every
    install that has no bundle to offer."""
    _pin_hub_ca("")
    for v in _VARS:
        assert v not in os.environ


def test_none_is_a_no_op():
    _pin_hub_ca(None)
    assert HUB_TLS_VERIFY_ENV not in os.environ


def test_entrypoints_use_the_helper_not_ssl_cert_file():
    """Neither entrypoint may go back to setting SSL_CERT_FILE on its own."""
    here = os.path.dirname(__file__)
    for name in ("control_plane.py", "main.py"):
        with open(os.path.join(here, "..", name), "r", encoding="utf-8") as f:
            src = f.read()
        assert "_pin_hub_ca(" in src, f"{name} must pin the CA via the helper"
        assert 'setdefault("SSL_CERT_FILE"' not in src, (
            f"{name} sets SSL_CERT_FILE directly, which core ignores")
