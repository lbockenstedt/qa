# qa — QA auditor (LM module)

<!-- INSTALLERS:START -->
## Installation

Every installer in this repo, with every flag and environment variable it accepts.
Installers are idempotent — re-running one updates code and preserves credentials.

### QA auditor spoke — `install_qa.sh`

```bash
curl -sSL https://raw.githubusercontent.com/lbockenstedt/qa/main/install_qa.sh \
  | sudo bash -s -- --hub ws://LM_HUB_IP:8765
```

| Flag | Purpose |
| :--- | :--- |
| `--hub URL` | Hub WebSocket URL, default `ws://localhost:8765`. **Pass a full `ws://`/`wss://` URL** — this installer does not normalize a bare hostname. |
| `--id`, `--name` | Pin the spoke id. |
| `--secret` | Pre-shared spoke secret. |
| `--hub-secret` | Hub PSK for auto-approval. |
| `--admin-token` | Hub admin token. |
| `--user` | LM username the QA runner logs in as. |
| `--password` | Password for that user. |
| `--bugfixer URL` | BugFixer base URL, for filing what QA finds. |
| `--api-port` | Port the QA API listens on. |
| `--all-prereqs` | Accepted and ignored. |

**Environment overrides:** `SPOKE_ID`, `HUB_SECRET`, `ADMIN_TOKEN`, `LM_USER`,
`LM_PASSWORD`, `BUGFIXER_URL`, `QA_API_PORT`.
<!-- INSTALLERS:END -->
