---
summary: "Automated QA auditor and fleet integration test spoke. Repo: qa."
keywords: [qa, testing, automation, spoke, hub, verification, test_engine]
---

# qa — QA Auditor Spoke

The **QA module** wraps an automated integration test engine that exercises the Lab Manager control plane, REST APIs, and connected spoke subsystems.

## Module Identification
- **Module type:** `qa`
- **Default Port:** `8080` (REST API / WebSocket logs)
- **Hub Link:** WebSocket to Hub on port 443/8765

## Core Subsystems
1. **`qa_spoke.py`**: Implementation of `BaseSpoke`. Translates hub control-plane commands into test executions and provides status reporting.
2. **`qa_engine.py`**: The `TestEngine` class that executes test scenarios across three capability tiers.
3. **`api_server.py`**: FastAPI server exposing endpoints for external automation tools (such as AppBuilder) to trigger runs and stream logs.
4. **`control_plane.py`**: Spoke process runner maintaining resilient hub connection with auto-reconnect and HMAC authentication.
