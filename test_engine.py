import asyncio
import logging
import json
import httpx
from hub_client import HubClient
from webui_client import WebUIClient
import api_server

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TestEngine")


class TestScenario:
    """A single test case associated with one or more module tags."""
    def __init__(self, name: str, test_func, modules: list = None):
        self.name = name
        self.test_func = test_func
        # modules=None means the scenario runs in the full suite regardless of filter.
        # modules=["opnsense"] means it only runs when module filter matches.
        self.modules = [m.lower() for m in (modules or [])]

    def matches(self, module_filter: str = None) -> bool:
        if not module_filter:
            return True  # no filter → always run
        return not self.modules or any(module_filter.lower() in m for m in self.modules)


class TestEngine:
    """
    Orchestrates QA scenarios against the Lab Manager ecosystem.
    Supports full-suite runs and targeted single-module runs triggered by BugFixer.
    """
    def __init__(self, hub_host: str, spoke_id: str, secret: str, webui_creds: dict,
                 bugfixer_url: str = None):
        self.hub_host = hub_host
        self.spoke_id = spoke_id
        self.secret = secret
        self.webui_client = WebUIClient(hub_host)
        self.creds = webui_creds
        self.bugfixer_url = (bugfixer_url or "").rstrip("/")
        self.results = []
        self.hub_client = HubClient(hub_host, spoke_id=spoke_id, secret=secret)

    def _all_scenarios(self) -> list:
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

            # ── Tier 4: BugFixer Pipeline ─────────────────────────────────────
            TestScenario("BugFixer: Health Check",          self.test_bugfixer_health,   ["bugfixer"]),
            TestScenario("BugFixer: Dashboard Reachable",   self.test_bugfixer_dashboard, ["bugfixer"]),
            TestScenario("BugFixer: Provider Configured",   self.test_bugfixer_providers, ["bugfixer"]),

            # ── Tier 5: WebUI Smoke ───────────────────────────────────────────
            TestScenario("UI: Login & Dashboard",           self.test_webui_smoke),
        ]

    async def run_all(self):
        return await self.run_module(None)

    async def run_module(self, module: str = None):
        """Run scenarios matching the given module filter (None = full suite)."""
        scenarios = [s for s in self._all_scenarios() if s.matches(module)]
        label = f"module={module}" if module else "full suite"
        api_server.update_step(f"Starting QA ({label})", "RUNNING")
        api_server.log_event(f"Running {len(scenarios)} scenario(s) [{label}]", "INFO")
        self.results = []

        for scenario in scenarios:
            api_server.update_step(scenario.name, "RUNNING")
            api_server.log_event(f"▶ {scenario.name}", "INFO")
            try:
                success = await scenario.test_func()
                status = "PASS" if success else "FAIL"
            except Exception as e:
                status = "CRASH"
                api_server.log_event(f"  ✗ Crashed: {e}", "ERROR")
                self.results.append({"name": scenario.name, "status": status, "error": str(e)})
                api_server.add_result(scenario.name, status, str(e))
                continue
            self.results.append({"name": scenario.name, "status": status})
            api_server.add_result(scenario.name, status)

        await self.hub_client.disconnect()
        api_server.update_step("Suite Complete", "COMPLETED")
        passed = sum(1 for r in self.results if r["status"] == "PASS")
        api_server.log_event(f"Done — {passed}/{len(self.results)} passed", "SUCCESS" if passed == len(self.results) else "ERROR")
        return self.results

    # ── Tier 1 ────────────────────────────────────────────────────────────────

    async def test_rest_status(self) -> bool:
        res = await self.hub_client.get_status()
        ok = res.get("status") == "SUCCESS"
        api_server.log_event(f"  Hub REST status: {'OK' if ok else 'FAIL'}", "INFO")
        return ok

    async def test_ws_handshake(self) -> bool:
        try:
            await self.hub_client.connect()
            await self.hub_client.send_command("HEARTBEAT", {})
            api_server.log_event("  WebSocket mutual auth OK", "SUCCESS")
            return True
        except Exception as e:
            api_server.log_event(f"  WS handshake failed: {e}", "ERROR")
            return False

    async def test_security_invalid_sig(self) -> bool:
        if not self.hub_client.ws:
            await self.hub_client.connect()
        import json as _json
        msg = {
            "header": {"message_id": "fake-id", "timestamp": 0,
                       "sender_id": self.hub_client.spoke_id, "destination_id": "hub"},
            "payload": {"type": "get_version", "data": {}},
            "signature": "totally-fake-signature",
        }
        await self.hub_client.ws.send(_json.dumps(msg))
        try:
            await asyncio.wait_for(self.hub_client.ws.recv(), timeout=1.0)
            api_server.log_event("  Security FAIL: Hub responded to invalid signature", "ERROR")
            return False
        except asyncio.TimeoutError:
            api_server.log_event("  Security OK: Hub ignored invalid signature", "SUCCESS")
            return True

    # ── Tier 2 ────────────────────────────────────────────────────────────────

    async def test_spoke_version(self) -> bool:
        if not self.hub_client.ws:
            await self.hub_client.connect()
        res = await self.hub_client.send_command("get_version", {})
        ok = "version" in res or res.get("status") == "SUCCESS"
        api_server.log_event(f"  Version: {'OK' if ok else 'FAIL'}", "INFO")
        return ok

    async def test_config_update(self) -> bool:
        test_cfg = {"appearance": {"theme": "dark_mode_test"}}
        update = await self.hub_client.request("POST", "/setup/appearance", data=test_cfg)
        if update.get("status") != "SUCCESS":
            return False
        current = await self.hub_client.request("GET", "/setup/appearance")
        return current.get("theme") == "dark_mode_test"

    # ── Tier 3: OPNsense ──────────────────────────────────────────────────────

    async def test_opnsense_rules(self) -> bool:
        if not self.hub_client.ws:
            await self.hub_client.connect()
        rule = {"rule": {"action": "pass", "protocol": "TCP",
                         "destination": "Port 80", "description": "QA Test Rule"}}
        res = await self.hub_client.send_command("OPNSENSE_ADD_RULE", rule)
        if res.get("status") != "SUCCESS":
            api_server.log_event(f"  Rule add failed: {res.get('message')}", "ERROR")
            return False
        rule_id = res.get("rule_id")
        del_res = await self.hub_client.send_command("OPNSENSE_DEL_RULE", {"rule_id": rule_id})
        ok = del_res.get("status") == "SUCCESS"
        api_server.log_event(f"  OPNsense rule add/delete: {'OK' if ok else 'FAIL'}", "INFO")
        return ok

    # ── Tier 3: NetBox ────────────────────────────────────────────────────────

    async def test_netbox_doc(self) -> bool:
        if not self.hub_client.ws:
            await self.hub_client.connect()
        vm = {"name": "qa-test-vm", "cluster": "proxmox-1", "vcpus": 1, "ram": 1024}
        res = await self.hub_client.send_command("NETBOX_DOC_VM", vm)
        ok = res.get("status") == "SUCCESS"
        api_server.log_event(f"  NetBox VM doc: {'OK' if ok else 'FAIL'}", "INFO")
        return ok

    # ── Tier 3: CPPM ─────────────────────────────────────────────────────────

    async def test_cppm_query(self) -> bool:
        if not self.hub_client.ws:
            await self.hub_client.connect()
        res = await self.hub_client.send_command("get_device", {"mac": "00:11:22:33:44:55"})
        ok = "error" not in res or "ResourceNotFound" in res.get("error", "")
        api_server.log_event(f"  CPPM device query: {'OK' if ok else 'FAIL'}", "INFO")
        return ok

    # ── Tier 3: CS ───────────────────────────────────────────────────────────

    async def test_cs_simulation(self) -> bool:
        if not self.hub_client.ws:
            await self.hub_client.connect()
        res = await self.hub_client.send_command("TRIGGER_ITERATION", {})
        ok = res.get("status") == "SUCCESS"
        api_server.log_event(f"  CS sim trigger: {'OK' if ok else 'FAIL'}", "INFO")
        return ok

    # ── Tier 3: PXMX ─────────────────────────────────────────────────────────

    async def test_pxmx_vm_list(self) -> bool:
        if not self.hub_client.ws:
            await self.hub_client.connect()
        res = await self.hub_client.send_command("PXMX_LIST_VMS", {})
        ok = res.get("status") == "SUCCESS" and isinstance(res.get("vms"), list)
        api_server.log_event(f"  PXMX VM list: {'OK — ' + str(len(res.get('vms',[]))) + ' VMs' if ok else 'FAIL'}", "INFO")
        return ok

    async def test_pxmx_ct_status(self) -> bool:
        if not self.hub_client.ws:
            await self.hub_client.connect()
        res = await self.hub_client.send_command("PXMX_LIST_CONTAINERS", {})
        ok = res.get("status") == "SUCCESS"
        api_server.log_event(f"  PXMX container status: {'OK' if ok else 'FAIL'}", "INFO")
        return ok

    # ── Tier 3: LDAP ─────────────────────────────────────────────────────────

    async def test_ldap_user_search(self) -> bool:
        if not self.hub_client.ws:
            await self.hub_client.connect()
        res = await self.hub_client.send_command("LDAP_SEARCH_USERS", {"filter": "(uid=*)", "limit": 1})
        ok = res.get("status") == "SUCCESS"
        api_server.log_event(f"  LDAP user search: {'OK' if ok else 'FAIL'}", "INFO")
        return ok

    async def test_ldap_group_membership(self) -> bool:
        if not self.hub_client.ws:
            await self.hub_client.connect()
        res = await self.hub_client.send_command("LDAP_GET_GROUPS", {})
        ok = res.get("status") == "SUCCESS" and isinstance(res.get("groups"), list)
        api_server.log_event(f"  LDAP group membership: {'OK' if ok else 'FAIL'}", "INFO")
        return ok

    # ── Tier 4: BugFixer ─────────────────────────────────────────────────────

    async def _bf_get(self, path: str, timeout: float = 10) -> dict:
        if not self.bugfixer_url:
            return {"status": "ERROR", "detail": "BUGFIXER_URL not configured"}
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.get(f"{self.bugfixer_url}{path}")
                r.raise_for_status()
                return r.json()
        except Exception as e:
            return {"status": "ERROR", "detail": str(e)}

    async def test_bugfixer_health(self) -> bool:
        res = await self._bf_get("/api/state")
        ok = "status" in res and res.get("status") != "ERROR"
        api_server.log_event(f"  BugFixer health: {'OK' if ok else 'FAIL — ' + res.get('detail','')}", "INFO")
        return ok

    async def test_bugfixer_dashboard(self) -> bool:
        if not self.bugfixer_url:
            api_server.log_event("  BugFixer URL not set — skipping dashboard check", "INFO")
            return True
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(self.bugfixer_url + "/")
                ok = r.status_code == 200
                api_server.log_event(f"  BugFixer dashboard HTTP {r.status_code}: {'OK' if ok else 'FAIL'}", "INFO")
                return ok
        except Exception as e:
            api_server.log_event(f"  BugFixer dashboard unreachable: {e}", "ERROR")
            return False

    async def test_bugfixer_providers(self) -> bool:
        res = await self._bf_get("/api/llm/config")
        configured = any(
            slot.get("model") for slot in (res.get("slots") or {}).values()
        ) if isinstance(res.get("slots"), dict) else False
        if not configured:
            # Fallback: check state for provider_N_configured flags
            state = await self._bf_get("/api/state")
            configured = any(state.get(f"provider_{n}_configured") for n in (1, 2, 3, 4))
        api_server.log_event(f"  BugFixer providers: {'at least one configured' if configured else 'NONE configured'}", "INFO")
        return configured

    # ── Tier 5: WebUI Smoke ───────────────────────────────────────────────────

    async def test_webui_smoke(self) -> bool:
        api_server.log_event("  Starting Playwright browser for WebUI smoke test", "INFO")
        await self.webui_client.start()
        try:
            if not await self.webui_client.login(self.creds["username"], self.creds["password"]):
                api_server.log_event("  WebUI login failed", "ERROR")
                return False
            ok = (await self.webui_client.verify_element(".dashboard-container") or
                  await self.webui_client.verify_element("h1:has-text('Dashboard')"))
            api_server.log_event(f"  WebUI smoke: {'OK' if ok else 'FAIL'}", "INFO")
            return ok
        finally:
            await self.webui_client.stop()
