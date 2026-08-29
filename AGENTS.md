# AGENTS.md — `qa`

**QA auditor module.** Drives the LM WebUI as a real user, and files what it finds into AppBuilder.

- **Repo:** `github.com/lbockenstedt/qa`
- **Module type:** `module_type = "qa"`
- **Canonical docs:** [`lm/docs/agents-and-skills.md`](../lm/docs/agents-and-skills.md) *(in the `lm` repo — the master registry)*
- **Fleet map:** [`../AGENTS.md`](../AGENTS.md) *(only present in a side-by-side checkout)*

## Context

This repo is **one of 16** that make up **Lab Manager (LM)** — a hub-and-spoke
"single pane of glass" orchestrator for lab/datacenter infrastructure. One hub (the `lm`
repo) runs the control plane, REST API and WebUI. Every other repo is a **spoke** wrapping
exactly one external system and dialling the hub over a WebSocket on port 443.

Read [`lm/docs/architecture-topology.md`](../lm/docs/architecture-topology.md) — a verbatim
copy also lives in this repo's `docs/` — before making structural changes.

## Layout

Flat, unlike the other spokes — no `src/`. `qa_spoke.py` (spoke), `control_plane.py`,
`qa_engine.py` (the audit logic), `api_server.py`, `hub_client.py`, `webui_client.py`
(logs into and drives the WebUI), `main.py`, `webui/`. Hub-side counterpart: `lm/qa/`.

## qa-specific gotchas

- **No `VERSION` file** — the only repo in the fleet without one.
- **`--hub` needs a full `ws://`/`wss://` URL** (default `ws://localhost:8765`); this installer does not normalise a bare hostname.
- It **logs in as a real LM user** (`--user`/`--password`) and needs `--admin-token`. Treat those as credentials.
- **`--ab URL` wires it to AppBuilder** (the `ab` repo) so findings become filed issues — which `ab` may then try to auto-fix. Be aware of that loop before pointing it at a live fleet.
- Two installers: `install_qa.sh` and `deploy_qa.sh`.

## Fleet conventions (identical in every LM repo)

- **Python 3.11**, FastAPI + `websockets` + `asyncio`. WebUI is dependency-free vanilla JS — **no npm build step exists anywhere in this project**.
- **`VERSION` is `MAJOR.NN` and branch-owned.** A bot bumps the last segment. **Never bump it by hand.** Promotion carries code only.
- **Branching: `dev -> qa -> main`.** `qa` and `main` need a PR; `ci.yml` is the required check. Direct pushes to `dev` are allowed.
- **CI runs one pytest process per component.** Components share top-level module names (`control_plane.py` exists in most repos) and collide in a single process.
- **Installers are idempotent** — re-running updates code and preserves credentials. Common flags: `--hub` (bare hostname is normalised to `wss://...:443`), `--id`/`--name`, `--secret`, `--hub-secret`, `--all-prereqs`.
- **Transport:** WebSocket on 443, mailbox pattern, **push-ack-retry — no fire-and-forget**. Heartbeat 30s; yellow at >=120s, red at >=300s. Hub queues 24h for offline spokes.
- **TLS:** encrypted but **verify-OFF by default** (self-signed hub cert). Verification is opt-in at install time via `--tls-verify` / `--tls-ca-cert` — never by hand-editing `.env`.
- **Heavy lifting belongs in the spoke, not the hub.** The hub is transport, state, policy and UI. See `lm/docs/architecture-spoke-heavy-lifting.md`.
- **API-first:** every operation exposes an API; the WebUI only ever calls that API.
- **Atomic transactions:** a mid-chain failure rolls back every preceding step and reports a before/after diff. No zombie resources.
- **Multitenancy is not optional:** isolation rides on Proxmox labels + NetBox tenant IDs. New resources carry tenant context.

## Rules

1. **One repo per change.** Cross-repo work is separate PRs, and the wire contract must stay backward-compatible because the two sides deploy independently.
2. **Read the canonical doc first** (linked above) — it is usually more current than this repo's README.
3. **Never hand-edit `VERSION`.**
4. **Check you are editing the live path,** not a preserved legacy one.
5. Match surrounding style. Comment only what needs clarifying.
