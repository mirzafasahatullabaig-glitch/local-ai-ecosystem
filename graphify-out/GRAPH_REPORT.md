# Graph Report - local-ai-ecosystem  (2026-10-23)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 472 nodes · 983 edges · 23 communities (11 shown, 12 thin omitted)
- Extraction: 93% EXTRACTED · 7% INFERRED · 0% AMBIGUOUS · INFERRED: 64 edges (avg confidence: 0.89)
- Token cost: 847 input · 183 output

## Graph Freshness
- Built from commit: `70592caa`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- Utility Functions
- App Initialization
- Test Utilities
- MQTT Command Bus
- Model Gateway
- Device Registry
- Request Routing
- TFT Display
- Dev Tools
- SQLite Persistence
- Dev Tests
- Documentation

## God Nodes (most connected - your core abstractions)
1. `create_app()` - 48 edges
2. `now_ms()` - 34 edges
3. `ModelGateway` - 31 edges
4. `SQLiteRepository` - 30 edges
5. `build_command()` - 16 edges
6. `DeviceRegistry` - 14 edges
7. `_app()` - 14 edges
8. `Command` - 13 edges
9. `Repository` - 13 edges
10. `Manifest` - 13 edges

## Surprising Connections (you probably didn't know these)
- `How a request is handled` --references--> `route()`  [INFERRED]
  README.md → backend/orchestrator/router.py
- `DeviceListener` --uses--> `Manifest`  [INFERRED]
  backend/messaging/bus.py → schemas/messages.py
- `FakeBus` --uses--> `Event`  [INFERRED]
  tests/test_api.py → schemas/messages.py
- `FakeBus` --uses--> `State`  [INFERRED]
  tests/test_api.py → schemas/messages.py
- `bus_client()` --uses--> `ModelGateway`  [INFERRED]
  tests/test_api.py → backend/model_gateway/gateway.py

## Import Cycles
- None detected.

## Communities (23 total, 12 thin omitted)

### Community 0 - "Utility Functions"
Cohesion: 0.05
Nodes (33): build_manifest(), camera_device(), _capture_image(), _do_action(), emit_event(), handle_command(), main(), on_connect() (+25 more)

### Community 1 - "App Initialization"
Cohesion: 0.06
Nodes (36): ActionRequest, AskRequest, _build_default_app(), _clean_image(), create_app(), _answer(), ask(), ask_stream() (+28 more)

### Community 2 - "Test Utilities"
Cohesion: 0.06
Nodes (32): bus_client(), _camera_setup(), send_command(), _capturing_app(), client(), handler(), FakeBus, _ndjson() (+24 more)

### Community 3 - "MQTT Command Bus"
Cohesion: 0.09
Nodes (19): DeviceBus, MqttDeviceBus, on_connect(), can_transition(), CapabilityParam, Command, device_topic(), Envelope (+11 more)

### Community 4 - "Model Gateway"
Cohesion: 0.08
Nodes (15): CallMetrics, GatewayError, GatewayResult, _metrics_from_response(), ModelGateway, _ns_to_ms(), make_gateway(), _model() (+7 more)

### Community 5 - "Device Registry"
Cohesion: 0.07
Nodes (18): DeviceRegistry, RegistrationError, Repository, API → broker (wired, verified 2026-10-21), Broker (on the Pi), Device agents (Phase 2), ESP32-S3 (`esp32_s3/eco_esp32_s3.ino`), Pi agent (`pi/pi_agent.py`) (+10 more)

### Community 6 - "Request Routing"
Cohesion: 0.06
Nodes (31): Decision, route(), Changelog, v2.10 — Pi TFT bot face, v2.11.0 — laptop brain, v2.12 — web panel, v2.13.0 — smarter model switching, v2.14.0 — vision and web speech (+23 more)

### Community 7 - "TFT Display"
Cohesion: 0.10
Nodes (16): blit(), blit_region(), camera_path(), command(), data(), draw_face(), emoji_text(), emoji_tile() (+8 more)

### Community 8 - "Dev Tools"
Cohesion: 0.11
Nodes (14): add_dev_routes(), dev_command(), dev_health(), dev_logs(), dev_state(), _cpu_percent(), DevCommand, _gpu() (+6 more)

### Community 9 - "SQLite Persistence"
Cohesion: 0.15
Nodes (7): SQLiteRepository, test_api_key_enforced(), test_panel_key_only_embedded_for_loopback(), test_device_roundtrip(), test_tasks(), test_thread_safe_writes(), test_upsert_is_idempotent()

### Community 10 - "Dev Tests"
Cohesion: 0.24
Nodes (10): _app(), test_dev_models_reports_vram(), test_forced_role_overrides_router(), test_health_shape(), test_logs_fixed_sources_only(), test_pages_send_csp_and_hide_key_from_lan(), test_raw_command_only_advertised_actions(), test_read_endpoints_need_the_key() (+2 more)

## Knowledge Gaps
- **12 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `create_app()` connect `App Initialization` to `Test Utilities`, `Model Gateway`, `Device Registry`, `Request Routing`, `Dev Tools`, `SQLite Persistence`, `Dev Tests`?**
  _High betweenness centrality (0.139) - this node is a cross-community bridge._
- **Are the 6 inferred relationships involving `create_app()` (e.g. with `require_key()` and `RegistrationError`) actually correct?**
  _`create_app()` has 6 INFERRED edges - model-reasoned connections that need verification._
- **Should `Utility Functions` be split into smaller, more focused modules?**
  _Cohesion score 0.053946053946053944 - nodes in this community are weakly interconnected._
- **Why does `route()` connect `Request Routing` to `App Initialization`?**
  _High betweenness centrality (0.132) - this node is a cross-community bridge._
- **Are the 3 inferred relationships involving `ModelGateway` (e.g. with `create_app()` and `bus_client()`) actually correct?**
  _`ModelGateway` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Should `App Initialization` be split into smaller, more focused modules?**
  _Cohesion score 0.06229508196721312 - nodes in this community are weakly interconnected._
- **Are the 3 inferred relationships involving `SQLiteRepository` (e.g. with `bus_client()` and `client()`) actually correct?**
  _`SQLiteRepository` has 3 INFERRED edges - model-reasoned connections that need verification._