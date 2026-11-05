"""QA test engine: the scenarios that check Lab Manager end to end.

Each scenario is an async check against the hub (REST and signed WebSocket), a
spoke command, AppBuilder or the WebUI. Progress and results are reported
through api_server so they appear in the QA API and log stream.
"""
# asyncio for timeouts, logging for output, json for raw messages.
import asyncio
import logging
import json
# httpx for AppBuilder calls; hub and WebUI clients; api_server for reporting.
import httpx
from hub_client import HubClient
from webui_client import WebUIClient
import api_server

# Log at INFO and use a named logger for the engine.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TestEngine")


class TestScenario:
    """A single test case associated with one or more module tags."""
    def __init__(self, name: str, test_func, modules: list = None):
        """Store the scenario's name, test coroutine and module tags."""
        # Name and the coroutine function that runs the check.
        self.name = name
        self.test_func = test_func
        # modules=None means the scenario runs in the full suite regardless of filter.
        # modules=["opnsense"] means it only runs when module filter matches.
        self.modules = [m.lower() for m in (modules or [])]

    def matches(self, module_filter: str = None) -> bool:
        """True if this scenario should run for the given module filter."""
        if not module_filter:
            return True  # no filter → always run
        # Untagged scenarios always run; tagged ones run when a tag contains the filter.
        return not self.modules or any(module_filter.lower() in m for m in self.modules)


class TestEngine:
    """
    Orchestrates QA scenarios against the Lab Manager ecosystem.
    Supports full-suite runs and targeted single-module runs triggered by AppBuilder.
    """
    def __init__(self, hub_host: str, spoke_id: str, secret: str, webui_creds: dict,
                 ab_url: str = None, insecure: bool = False, tls_ca_bundle: str = None):
        """Store connection details and create the WebUI and hub clients."""
        # Hub address and spoke credentials.
        self.hub_host = hub_host
        self.spoke_id = spoke_id
        self.secret = secret
        # WebUI client and login, AppBuilder URL (no trailing slash) and empty results.
        self.webui_client = WebUIClient(hub_host)
        self.creds = webui_creds
        self.ab_url = (ab_url or "").rstrip("/")
        self.results = []
        # Hub client used by the connectivity and spoke scenarios.
        self.hub_client = HubClient(hub_host, spoke_id=spoke_id, secret=secret,
                                     insecure=insecure, tls_ca_bundle=tls_ca_bundle)

    def _all_scenarios(self) -> list:
        """Return every scenario, grouped by tier, with its module tags."""
        return [
            # ── Tier 1: Connectivity & Security ──────────────────────────────
            TestScenario("Connectivity: Hub REST Status",   self.test_rest_status),
            TestScenario("Connectivity: WebSocket Handshake", self.test_ws_handshake),
            TestScenario("Security: Invalid Signature Rejection", self.test_security_invalid_sig),

            # ── Tier 2: Basic Feature Set ─────────────────────────────────────
            TestScenario("Feature: Spoke Version Query",    self.test_spoke_version),
            TestScenario("Feature: Config Update Round-Trip", self.test_config_update),

            # ── Tier 3: Spoke-Specific Functionality ──────────────────────────
            TestScenario("OPNsense: Rule Add/Delete",       self.test_opnsense_rules,   ["opnsense"]),
            TestScenario("NetBox: VM Documentation",        self.test_netbox_doc,        ["netbox"]),
            TestScenario("CPPM: Device Query",              self.test_cppm_query,        ["cppm"]),
            TestScenario("CS: Client Sim Trigger",          self.test_cs_simulation,     ["cs"]),
            TestScenario("PXMX: VM List",                   self.test_pxmx_vm_list,      ["pxmx"]),
            TestScenario("PXMX: Container Status",          self.test_pxmx_ct_status,    ["pxmx"]),
            TestScenario("LDAP: User Search",               self.test_ldap_user_search,  ["ldap"]),
            TestScenario("LDAP: Group Membership",          self.test_ldap_group_membership, ["ldap"]),

            # ── Tier 4: AppBuilder Pipeline ─────────────────────────────────────
            TestScenario("AppBuilder: Health Check",          self.test_ab_health,   ["ab"]),
            TestScenario("AppBuilder: Dashboard Reachable",   self.test_ab_dashboard, ["ab"]),
            TestScenario("AppBuilder: Provider Configured",   self.test_ab_providers, ["ab"]),

            # ── Tier 5: WebUI Smoke ───────────────────────────────────────────
            TestScenario("UI: Login & Dashboard",           self.test_webui_smoke),
        ]

    async def run_all(self):
        """Run the full suite."""
        return await self.run_module(None)

    async def run_module(self, module: str = None):
        """Run scenarios matching the given module filter (None = full suite)."""
        # Pick the matching scenarios and announce the run.
        scenarios = [s for s in self._all_scenarios() if s.matches(module)]
        label = f"module={module}" if module else "full suite"
        api_server.update_step(f"Starting QA ({label})", "RUNNING")
        api_server.log_event(f"Running {len(scenarios)} scenario(s) [{label}]", "INFO")
        # Start with no results.
        self.results = []

        # Run each scenario, reporting its start and result.
        for scenario in scenarios:
            api_server.update_step(scenario.name, "RUNNING")
            api_server.log_event(f"▶ {scenario.name}", "INFO")
            try:
                # The scenario returns True for pass and False for fail.
                success = await scenario.test_func()
                status = "PASS" if success else "FAIL"
            # An exception counts as a crash; record it and move on.
            except Exception as e:
                status = "CRASH"
                api_server.log_event(f"  ✗ Crashed: {e}", "ERROR")
                self.results.append({"name": scenario.name, "status": status, "error": str(e)})
                api_server.add_result(scenario.name, status, str(e))
                continue
            # Record the pass/fail result.
            self.results.append({"name": scenario.name, "status": status})
            api_server.add_result(scenario.name, status)

        # Close the hub connection and mark the run complete.
        await self.hub_client.disconnect()
        api_server.update_step("Suite Complete", "COMPLETED")
        # Log the pass count (as an error unless everything passed).
        passed = sum(1 for r in self.results if r["status"] == "PASS")
        api_server.log_event(f"Done — {passed}/{len(self.results)} passed", "SUCCESS" if passed == len(self.results) else "ERROR")
        return self.results

    # ── Tier 1 ────────────────────────────────────────────────────────────────

    async def test_rest_status(self) -> bool:
        """Pass if the hub's REST status endpoint reports success."""
        res = await self.hub_client.get_status()
        ok = res.get("status") == "SUCCESS"
        api_server.log_event(f"  Hub REST status: {'OK' if ok else 'FAIL'}", "INFO")
        return ok

    async def test_ws_handshake(self) -> bool:
        """Pass if the WebSocket handshake works and a heartbeat can be sent."""
        # Connect (mutual auth) and send a heartbeat.
        try:
            await self.hub_client.connect()
            await self.hub_client.send_command("HEARTBEAT", {})
            api_server.log_event("  WebSocket mutual auth OK", "SUCCESS")
            return True
        # Handshake or send failed.
        except Exception as e:
            api_server.log_event(f"  WS handshake failed: {e}", "ERROR")
            return False

    async def test_security_invalid_sig(self) -> bool:
        """Pass if the hub ignores a message with a forged signature."""
        # Connect if needed.
        if not self.hub_client.ws:
            await self.hub_client.connect()
        import json as _json
        # A get_version message with a fake signature.
        msg = {
            "header": {"message_id": "fake-id", "timestamp": 0,
                       "sender_id": self.hub_client.spoke_id, "destination_id": "hub"},
            "payload": {"type": "get_version", "data": {}},
            "signature": "totally-fake-signature",
        }
        # Send it raw, bypassing the client's signing.
        await self.hub_client.ws.send(_json.dumps(msg))
        # Any reply within one second means the hub accepted it.
        try:
            await asyncio.wait_for(self.hub_client.ws.recv(), timeout=1.0)
            api_server.log_event("  Security FAIL: Hub responded to invalid signature", "ERROR")
            return False
        # No reply: the hub correctly ignored it.
        except asyncio.TimeoutError:
            api_server.log_event("  Security OK: Hub ignored invalid signature", "SUCCESS")
            return True

    # ── Tier 2 ────────────────────────────────────────────────────────────────

    async def test_spoke_version(self) -> bool:
        """Pass if a version query over the WebSocket returns a version or success."""
        # Connect if needed, then send the query.
        if not self.hub_client.ws:
            await self.hub_client.connect()
        res = await self.hub_client.send_command("get_version", {})
        ok = "version" in res or res.get("status") == "SUCCESS"
        api_server.log_event(f"  Version: {'OK' if ok else 'FAIL'}", "INFO")
        return ok

    async def test_config_update(self) -> bool:
        """Pass if an appearance setting can be saved and read back."""
        # Save a test theme.
        test_cfg = {"appearance": {"theme": "dark_mode_test"}}
        update = await self.hub_client.request("POST", "/setup/appearance", data=test_cfg)
        if update.get("status") != "SUCCESS":
            return False
        # Read it back and compare.
        current = await self.hub_client.request("GET", "/setup/appearance")
        return current.get("theme") == "dark_mode_test"

    # ── Tier 3: OPNsense ──────────────────────────────────────────────────────

    async def test_opnsense_rules(self) -> bool:
        """Pass if a test firewall rule can be added and then deleted."""
        # Connect if needed.
        if not self.hub_client.ws:
            await self.hub_client.connect()
        # Add a simple pass rule for port 80.
        rule = {"rule": {"action": "pass", "protocol": "TCP",
                         "destination": "Port 80", "description": "QA Test Rule"}}
        res = await self.hub_client.send_command("OPNSENSE_ADD_RULE", rule)
        if res.get("status") != "SUCCESS":
            api_server.log_event(f"  Rule add failed: {res.get('message')}", "ERROR")
            return False
        # Delete the rule by the id returned.
        rule_id = res.get("rule_id")
        del_res = await self.hub_client.send_command("OPNSENSE_DEL_RULE", {"rule_id": rule_id})
        ok = del_res.get("status") == "SUCCESS"
        api_server.log_event(f"  OPNsense rule add/delete: {'OK' if ok else 'FAIL'}", "INFO")
        return ok

    # ── Tier 3: NetBox ────────────────────────────────────────────────────────

    async def test_netbox_doc(self) -> bool:
        """Pass if NetBox documents a test VM."""
        # Connect if needed, then send a test VM.
        if not self.hub_client.ws:
            await self.hub_client.connect()
        vm = {"name": "qa-test-vm", "cluster": "proxmox-1", "vcpus": 1, "ram": 1024}
        res = await self.hub_client.send_command("NETBOX_DOC_VM", vm)
        ok = res.get("status") == "SUCCESS"
        api_server.log_event(f"  NetBox VM doc: {'OK' if ok else 'FAIL'}", "INFO")
        return ok

    # ── Tier 3: CPPM ─────────────────────────────────────────────────────────

    async def test_cppm_query(self) -> bool:
        """Pass if a CPPM device lookup works (not found also counts)."""
        # Connect if needed, then look up a sample MAC address.
        if not self.hub_client.ws:
            await self.hub_client.connect()
        res = await self.hub_client.send_command("get_device", {"mac": "00:11:22:33:44:55"})
        # Pass on no error, or a not-found error.
        ok = "error" not in res or "ResourceNotFound" in res.get("error", "")
        api_server.log_event(f"  CPPM device query: {'OK' if ok else 'FAIL'}", "INFO")
        return ok

    # ── Tier 3: CS ───────────────────────────────────────────────────────────

    async def test_cs_simulation(self) -> bool:
        """Pass if the client simulator accepts an iteration trigger."""
        # Connect if needed, then trigger an iteration.
        if not self.hub_client.ws:
            await self.hub_client.connect()
        res = await self.hub_client.send_command("TRIGGER_ITERATION", {})
        ok = res.get("status") == "SUCCESS"
        api_server.log_event(f"  CS sim trigger: {'OK' if ok else 'FAIL'}", "INFO")
        return ok

    # ── Tier 3: PXMX ─────────────────────────────────────────────────────────

    async def test_pxmx_vm_list(self) -> bool:
        """Pass if Proxmox returns a list of VMs."""
        # Connect if needed, then list VMs.
        if not self.hub_client.ws:
            await self.hub_client.connect()
        res = await self.hub_client.send_command("PXMX_LIST_VMS", {})
        # Pass only when the reply includes a list of VMs.
        ok = res.get("status") == "SUCCESS" and isinstance(res.get("vms"), list)
        api_server.log_event(f"  PXMX VM list: {'OK — ' + str(len(res.get('vms',[]))) + ' VMs' if ok else 'FAIL'}", "INFO")
        return ok

    async def test_pxmx_ct_status(self) -> bool:
        """Pass if Proxmox lists its containers."""
        # Connect if needed, then list containers.
        if not self.hub_client.ws:
            await self.hub_client.connect()
        res = await self.hub_client.send_command("PXMX_LIST_CONTAINERS", {})
        ok = res.get("status") == "SUCCESS"
        api_server.log_event(f"  PXMX container status: {'OK' if ok else 'FAIL'}", "INFO")
        return ok

    # ── Tier 3: LDAP ─────────────────────────────────────────────────────────

    async def test_ldap_user_search(self) -> bool:
        """Pass if an LDAP user search succeeds."""
        # Connect if needed, then search for one user.
        if not self.hub_client.ws:
            await self.hub_client.connect()
        res = await self.hub_client.send_command("LDAP_SEARCH_USERS", {"filter": "(uid=*)", "limit": 1})
        ok = res.get("status") == "SUCCESS"
        api_server.log_event(f"  LDAP user search: {'OK' if ok else 'FAIL'}", "INFO")
        return ok

    async def test_ldap_group_membership(self) -> bool:
        """Pass if LDAP returns a list of groups."""
        # Connect if needed, then list groups.
        if not self.hub_client.ws:
            await self.hub_client.connect()
        res = await self.hub_client.send_command("LDAP_GET_GROUPS", {})
        # Pass only when the reply includes a list of groups.
        ok = res.get("status") == "SUCCESS" and isinstance(res.get("groups"), list)
        api_server.log_event(f"  LDAP group membership: {'OK' if ok else 'FAIL'}", "INFO")
        return ok

    # ── Tier 4: AppBuilder ─────────────────────────────────────────────────────

    async def _bf_get(self, path: str, timeout: float = 10) -> dict:
        """GET a JSON path from AppBuilder; errors come back as an ERROR dict."""
        # AppBuilder URL must be configured.
        if not self.ab_url:
            return {"status": "ERROR", "detail": "AB_URL not configured"}
        # Fetch the path and return its JSON body.
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.get(f"{self.ab_url}{path}")
                r.raise_for_status()
                return r.json()
        # Network or HTTP error.
        except Exception as e:
            return {"status": "ERROR", "detail": str(e)}

    async def test_ab_health(self) -> bool:
        """Pass if AppBuilder's state endpoint answers without an error."""
        res = await self._bf_get("/api/state")
        ok = "status" in res and res.get("status") != "ERROR"
        api_server.log_event(f"  AppBuilder health: {'OK' if ok else 'FAIL — ' + res.get('detail','')}", "INFO")
        return ok

    async def test_ab_dashboard(self) -> bool:
        """Pass if AppBuilder's dashboard returns HTTP 200 (skipped without a URL)."""
        # No AppBuilder URL: skip and count as passed.
        if not self.ab_url:
            api_server.log_event("  AppBuilder URL not set — skipping dashboard check", "INFO")
            return True
        # Load the dashboard page.
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(self.ab_url + "/")
                ok = r.status_code == 200
                api_server.log_event(f"  AppBuilder dashboard HTTP {r.status_code}: {'OK' if ok else 'FAIL'}", "INFO")
                return ok
        # Unreachable.
        except Exception as e:
            api_server.log_event(f"  AppBuilder dashboard unreachable: {e}", "ERROR")
            return False

    async def test_ab_providers(self) -> bool:
        """Pass if AppBuilder has at least one LLM provider configured."""
        # Configured if any LLM slot has a model set.
        res = await self._bf_get("/api/llm/config")
        configured = any(
            slot.get("model") for slot in (res.get("slots") or {}).values()
        ) if isinstance(res.get("slots"), dict) else False
        if not configured:
            # Fallback: check state for provider_N_configured flags
            state = await self._bf_get("/api/state")
            configured = any(state.get(f"provider_{n}_configured") for n in (1, 2, 3, 4))
        # Log and return the result.
        api_server.log_event(f"  AppBuilder providers: {'at least one configured' if configured else 'NONE configured'}", "INFO")
        return configured

    # ── Tier 5: WebUI Smoke ───────────────────────────────────────────────────

    async def test_webui_smoke(self) -> bool:
        """Pass if the WebUI login works and the dashboard appears."""
        # Start a headless browser.
        api_server.log_event("  Starting Playwright browser for WebUI smoke test", "INFO")
        await self.webui_client.start()
        # Log in; failure ends the test.
        try:
            if not await self.webui_client.login(self.creds["username"], self.creds["password"]):
                api_server.log_event("  WebUI login failed", "ERROR")
                return False
            # Look for the dashboard container or its heading.
            ok = (await self.webui_client.verify_element(".dashboard-container") or
                  await self.webui_client.verify_element("h1:has-text('Dashboard')"))
            api_server.log_event(f"  WebUI smoke: {'OK' if ok else 'FAIL'}", "INFO")
            return ok
        # Always close the browser.
        finally:
            await self.webui_client.stop()
