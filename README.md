# qa — Fleet Automated Quality Assurance & Testing Spoke (Lab Manager Module)

The **QA Auditor Spoke** (`module_type = "qa"`) provides autonomous and on-demand integration testing across the entire Lab Manager fleet. It validates core hub functionality, WebSocket protocol contracts, security signing, and spoke-specific operational capabilities.

See [`docs/qa.md`](docs/qa.md) for the full architecture reference and [`lm/docs/architecture-topology.md`](../lm/docs/architecture-topology.md) for fleet topology.

## Architecture

The QA spoke comprises four core components:
1. **Spoke Coordinator (`qa_spoke.py`):** Handles WebSocket commands from the Lab Manager hub (`QA_RUN_TESTS`, `QA_GET_LAST_RESULTS`) and reports telemetry.
2. **REST API Server (`api_server.py`):** A standalone FastAPI application exposing test session control, live streaming logs over WebSockets (`/ws/logs`), and test execution triggers (`/run`).
3. **Test Engine (`qa_engine.py`):** Multi-tier test orchestrator with support for targeted module filtering and structured pass/fail assertion reporting.
4. **Control Plane (`control_plane.py`):** Daemon entrypoint managing the hub WebSocket connection, HMAC message signing, and API server lifecycle.

## Test Tiers & Capabilities

The QA test suite is organized into three progressive execution tiers:
- **Tier 1 (Connectivity & Security):** Hub REST status verification, WebSocket authentication handshake, and invalid cryptographic signature rejection.
- **Tier 2 (Basic Fleet Protocol):** Spoke version querying, and configuration update (`UPDATE_CONFIG`) round-trip verification.
- **Tier 3 (Spoke-Specific Feature Validation):**
  - **OPNsense (`opnsense`):** Firewall rule creation, verification, and deletion.
  - **NetBox (`netbox`):** IPAM VM documentation and sync verification.
  - **ClearPass (`cppm`):** Device database queries and session tracking.
  - **Client Simulator (`cs`):** Automated client scenario triggering and simulation execution.
  - **Proxmox (`pxmx`):** Virtual machine and LXC container inventory enumeration.

## Spoke Commands Reference

The QA spoke listens for and responds to the following control plane commands:

| Command | Payload | Response | Description |
| :--- | :--- | :--- | :--- |
| `QA_RUN_TESTS` | `{"module": "<optional_filter>"}` | `{"status": "SUCCESS", "summary": "...", "results": [...]}` | Executes full or filtered QA test suite |
| `QA_GET_LAST_RESULTS` | `{}` | `{"status": "SUCCESS", "results": [...]}` | Retrieves cached results from the most recent test run |
| `get_status` | `{}` | `{"spoke_id": "...", "status": "ONLINE", "last_run_summary": "..."}` | Queries spoke operational status and run summary |

## REST API Endpoints

The QA REST API runs by default on port `8080`:

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/health` | Service health status and current engine state |
| `GET` | `/session` | Current test session state, logs, and results |
| `POST` | `/run` | Triggers a test run (accepts optional `?module=` filter) |
| `GET` | `/ws/logs` | WebSocket stream for live test execution logs |

<!-- INSTALLERS:START -->
## Installation

Every installer in this repo, with every flag and environment variable it accepts.
Installers are idempotent — re-running one updates code and preserves credentials.

### QA auditor spoke — `install_qa.sh`

```bash
curl -sSL https://raw.githubusercontent.com/lbockenstedt/qa/main/install_qa.sh \
  | sudo bash -s -- --hub wss://LM_HUB_IP:8765
```

| Flag | Purpose |
| :--- | :--- |
| `--hub URL` | Hub WebSocket URL, default `wss://localhost:8765`. **Pass a full `ws://`/`wss://` URL** — this installer does not normalize a bare hostname. A `ws://` URL is **refused** unless `--insecure-ws` is passed. |
| `--tls-ca-cert PATH` | CA bundle used to verify a self-signed hub certificate. Written to `.env` as `QA_HUB_CA_CERT`. |
| `--insecure-ws` | Allow a plaintext `ws://` hub URL. Sends the spoke secret unencrypted — lab use only. |
| `--id`, `--name` | Pin the spoke id. |
| `--secret` | Pre-shared spoke secret. |
| `--hub-secret` | Hub PSK for auto-approval. |
| `--admin-token` | Hub admin token. |
| `--user` | LM username the QA runner logs in as. |
| `--password` | Password for that user. |
| `--ab URL` | AppBuilder base URL, for filing what QA finds. |
| `--api-port` | Port the QA API listens on. |
| `--all-prereqs` | Accepted and ignored. |

> **Upgrading from a pre-TLS install.** The spoke now connects over `wss://`
> with certificate verification and **refuses to send its secret over plaintext
> `ws://`**, so an existing install pointed at a plaintext hub will fail to
> connect until you either point it at a TLS listener or re-run the installer
> with `--insecure-ws`. For the fleet's self-signed hub certificate, pass
> `--tls-ca-cert /path/to/ca.pem` — set it at install time rather than by
> hand-editing `.env`.

**Environment overrides:** `SPOKE_ID`, `HUB_SECRET`, `ADMIN_TOKEN`, `LM_USER`,
`LM_PASSWORD`, `AB_URL`, `QA_API_PORT`.
<!-- INSTALLERS:END -->
