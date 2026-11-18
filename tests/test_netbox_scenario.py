"""The NetBox scenario must only read: QA may share another environment's
NetBox read-only (hub System → Sync → Read-only NetBox/Zabbix)."""
import asyncio

from qa_engine import TestEngine


class _Hub:
    """Records the commands a scenario sends; answers with a site list."""

    ws = True

    def __init__(self):
        """Start with no commands sent."""
        self.sent = []

    async def send_command(self, command_type, data, timeout=5.0):
        """Record the command and reply with an empty site list."""
        self.sent.append(command_type)
        return {"status": "SUCCESS", "sites": []}


def test_netbox_scenario_reads_the_site_list_and_writes_nothing():
    engine = TestEngine.__new__(TestEngine)
    engine.hub_client = _Hub()
    assert asyncio.run(engine.test_netbox_sites()) is True
    assert engine.hub_client.sent == ["NETBOX_GET_SITES"]
