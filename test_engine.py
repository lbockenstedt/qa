import asyncio
import logging
from hub_client import HubClient
from webui_client import WebUIClient
import api_server

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TestEngine")

class TestScenario:
    """Defines a single test case with a name and a function to execute."""
    def __init__(self, name: str, test_func):
        self.name = name
        self.test_func = test_func

class TestEngine:
    """
    Orchestrates the execution of QA scenarios against the Lab Manager ecosystem.
    Reports progress and results to the QA WebUI.
    """
    def __init__(self, hub_host: str, spoke_id: str, secret: str, webui_creds: dict):
        self.hub_host = hub_host
        self.spoke_id = spoke_id
        self.secret = secret
        self.webui_client = WebUIClient(hub_host)
        self.creds = webui_creds
        self.results = []
        self.hub_client = HubClient(hub_host, spoke_id=spoke_id, secret=secret)

    async def run_all(self):
        # We group tests into "Tiers"
        scenarios = [
            # Tier 1: Connectivity & Security
            TestScenario("Connectivity: REST Status", self.test_rest_status),
            TestScenario("Connectivity: WebSocket Handshake", self.test_ws_handshake),
            TestScenario("Security: Invalid Signature Rejection", self.test_security_invalid_sig),

            # Tier 2: Basic Feature Set
            TestScenario("Feature: Spoke Version Query", self.test_spoke_version),
            TestScenario("Feature: Configuration Update", self.test_config_update),

            # Tier 3: Deep Spoke Functionality (Based on API_SPECS)
            TestScenario("Spoke: OPNsense Rule Management", self.test_opnsense_rules),
            TestScenario("Spoke: NetBox VM Documentation", self.test_netbox_doc),
            TestScenario("Spoke: CPPM Device Query", self.test_cppm_query),
            TestScenario("Spoke: Client Sim Trigger", self.test_cs_simulation),

            # Tier 4: WebUI
            TestScenario("UI: Smoke Test (Login & Dashboard)", self.test_webui_smoke),
        ]

        api_server.update_step("Starting QA Suite", "RUNNING")
        api_server.log_event(f"Launching {len(scenarios)} test scenarios across 4 tiers...", "INFO")

        for scenario in scenarios:
            api_server.update_step(scenario.name, "RUNNING")
            api_server.log_event(f"Executing: {scenario.name}", "INFO")

            try:
                success = await scenario.test_func()
                status = "PASS" if success else "FAIL"
                self.results.append({"name": scenario.name, "status": status})
                api_server.add_result(scenario.name, status)
            except Exception as e:
                api_server.log_event(f"Scenario {scenario.name} crashed: {e}", "ERROR")
                self.results.append({"name": scenario.name, "status": "CRASH", "error": str(e)})
                api_server.add_result(scenario.name, "CRASH", str(e))

        await self.hub_client.disconnect()
        await self.webui_client.stop()

        api_server.update_step("Suite Complete", "COMPLETED")
        api_server.log_event("All tests finished. Final report generated.", "SUCCESS")

        return self.results

    # --- Scenario Implementations ---

    async def test_rest_status(self) -> bool:
        api_server.log_event("Querying Hub /status endpoint...", "INFO")
        res = await self.hub_client.get_status()
        success = res.get("status") == "SUCCESS"
        api_server.log_event(f"REST status check: {'SUCCESS' if success else 'FAILED'}", "INFO")
        return success

    async def test_ws_handshake(self) -> bool:
        try:
            api_server.log_event("Initiating WebSocket handshake...", "INFO")
            await self.hub_client.connect()
            await self.hub_client.send_command("HEARTBEAT", {})
            api_server.log_event("WebSocket mutual authentication successful.", "SUCCESS")
            return True
        except Exception as e:
            api_server.log_event(f"WS Handshake failed: {e}", "ERROR")
            return False

    async def test_security_invalid_sig(self) -> bool:
        """Verifies that the Hub rejects messages with an invalid signature."""
        if not self.hub_client.ws:
            await self.hub_client.connect()

        api_server.log_event("Sending malformed signature to test Hub security...", "INFO")

        # Manually construct a message with a fake signature
        msg_id = "fake-id"
        message = {
            "header": {"message_id": msg_id, "timestamp": 0, "sender_id": self.hub_client.spoke_id, "destination_id": "hub"},
            "payload": {"type": "get_version", "data": {}},
            "signature": "totally-fake-signature"
        }

        await self.hub_client.ws.send(json.dumps(message))
        # The Hub should simply drop this message (silent ignore)
        # We verify this by ensuring we don't receive a response within a short window
        try:
            await asyncio.wait_for(self.hub_client.ws.recv(), timeout=1.0)
            api_server.log_event("Security Failure: Hub responded to invalid signature!", "ERROR")
            return False
        except asyncio.TimeoutError:
            api_server.log_event("Security Success: Hub correctly ignored invalid signature.", "SUCCESS")
            return True

    async def test_spoke_version(self) -> bool:
        if not self.hub_client.ws:
            await self.hub_client.connect()

        api_server.log_event("Requesting spoke version...", "INFO")
        res = await self.hub_client.send_command("get_version", {})
        success = "version" in res or res.get("status") == "SUCCESS"
        api_server.log_event(f"Version query result: {'SUCCESS' if success else 'FAILED'}", "INFO")
        return success

    async def test_config_update(self) -> bool:
        api_server.log_event("Testing config persistence via REST API...", "INFO")
        test_config = {"appearance": {"theme": "dark_mode_test"}}
        update_res = await self.hub_client.request("POST", "/setup/appearance", data=test_config)

        if update_res.get("status") != "SUCCESS":
            api_server.log_event("Failed to update appearance config.", "ERROR")
            return False

        current_config = await self.hub_client.request("GET", "/setup/appearance")
        success = current_config.get("theme") == "dark_mode_test"
        api_server.log_event(f"Config verification: {'SUCCESS' if success else 'FAILED'}", "INFO")
        return success

    # --- Deep Feature Tests (Existing Spoke functionality) ---

    async def test_opnsense_rules(self) -> bool:
        if not self.hub_client.ws:
            await self.hub_client.connect()

        api_server.log_event("Testing OPNsense rule management...", "INFO")
        rule_data = {"rule": {"action": "pass", "protocol": "TCP", "destination": "Port 80", "description": "QA Test Rule"}}

        res = await self.hub_client.send_command("OPNSENSE_ADD_RULE", rule_data)
        if res.get("status") != "SUCCESS":
            api_server.log_event(f"Failed to add rule: {res.get('message')}", "ERROR")
            return False

        rule_id = res.get("rule_id")
        api_server.log_event(f"Rule added successfully (ID: {rule_id}). Now deleting...", "INFO")

        del_res = await self.hub_client.send_command("OPNSENSE_DEL_RULE", {"rule_id": rule_id})
        success = del_res.get("status") == "SUCCESS"
        api_server.log_event(f"Rule deletion: {'SUCCESS' if success else 'FAILED'}", "INFO")
        return success

    async def test_netbox_doc(self) -> bool:
        if not self.hub_client.ws:
            await self.hub_client.connect()

        api_server.log_event("Testing NetBox VM documentation...", "INFO")
        vm_data = {"name": "qa-test-vm", "cluster": "proxmox-1", "vcpus": 1, "ram": 1024}

        res = await self.hub_client.send_command("NETBOX_DOC_VM", vm_data)
        success = res.get("status") == "SUCCESS"
        api_server.log_event(f"VM Documentation: {'SUCCESS' if success else 'FAILED'}", "INFO")
        return success

    async def test_cppm_query(self) -> bool:
        if not self.hub_client.ws:
            await self.hub_client.connect()

        api_server.log_event("Testing CPPM device query...", "INFO")
        # Use a known MAC or a generic one for testing
        res = await self.hub_client.send_command("get_device", {"mac": "00:11:22:33:44:55"})

        # We accept either a valid device object or a "not found" error,
        # as long as the CPPM spoke actually responded.
        success = "error" not in res or "ResourceNotFound" in res.get("error", "")
        api_server.log_event(f"CPPM Query Response: {'SUCCESS' if success else 'FAILED'}", "INFO")
        return success

    async def test_cs_simulation(self) -> bool:
        if not self.hub_client.ws:
            await self.hub_client.connect()

        api_server.log_event("Testing Client Sim trigger...", "INFO")
        res = await self.hub_client.send_command("TRIGGER_ITERATION", {})
        success = res.get("status") == "SUCCESS"
        api_server.log_event(f"CS Simulation Trigger: {'SUCCESS' if success else 'FAILED'}", "INFO")
        return success

    async def test_webui_smoke(self) -> bool:
        api_server.log_event("Launching Playwright browser for WebUI smoke test...", "INFO")
        await self.webui_client.start()

        api_server.log_event("Attempting login...", "INFO")
        if await self.webui_client.login(self.creds['username'], self.creds['password']):
            api_server.log_event("WebUI Login successful.", "SUCCESS")

            api_server.log_event("Verifying dashboard layout...", "INFO")
            if await self.webui_client.verify_element(".dashboard-container") or \
               await self.webui_client.verify_element("h1:has-text('Dashboard')"):

                api_server.log_event("Navigating to Tenants menu...", "INFO")
                if await self.webui_client.navigate_to("Tenants"):
                    api_server.log_event("UI Navigation verified.", "SUCCESS")
                    return True

        api_server.log_event("WebUI smoke test failed.", "ERROR")
        return False
