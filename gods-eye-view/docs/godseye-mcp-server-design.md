# godSeye — MCP Server Architecture for Agentic UAV Mission Control

**Status:** Design proposal (v0.1-draft)
**Audience:** Systems engineers, harness/agent developers, God's Eye View integrators
**Scope:** The MCP (Model Context Protocol) server that exposes Microsoft AirSim UAV capabilities to an LLM agent harness, and bridges sim state into the God's Eye View Cesium globe.

---

## Table of Contents

1. [System Context & Component Overview](#1-system-context--component-overview)
2. [MCP Tool Catalog](#2-mcp-tool-catalog)
   - 2.1 Conventions & Common Types
   - 2.2 Flight Control
   - 2.3 Sensors
   - 2.4 Mission Primitives
   - 2.5 Analysis
   - 2.6 Sim Admin
3. [Resources, Prompts & Notifications](#3-resources-prompts--notifications)
4. [Transport Choice](#4-transport-choice)
5. [Safety Envelope](#5-safety-envelope)
6. [State Model: Missions, Tasks, Progress](#6-state-model-missions-tasks-progress)
7. [Telemetry Bridge to God's Eye View](#7-telemetry-bridge-to-gods-eye-view)
8. [Harness Skill Design](#8-harness-skill-design)
9. [Versioning & Extensibility](#9-versioning--extensibility)
10. [Appendix A: AirSim RPC Mapping](#appendix-a-airsim-rpc-mapping)
11. [Appendix B: Example Session](#appendix-b-example-session)

---

## 1. System Context & Component Overview

```
┌────────────────────────┐        MCP (Streamable HTTP)        ┌──────────────────────────┐
│  AGENT HARNESS (LLM)   │ ◄────────────────────────────────► │  godseye-mcp-server      │
│  skills: uav-recon,    │   tools / resources / prompts /    │  (Python, runs ON or     │
│  uav-sar, roe-policy   │   notifications (progress, events) │   NEXT TO the sim host)  │
└────────────────────────┘                                    └───────┬──────────┬───────┘
                                                                      │          │
                                                     msgpack-rpc      │          │ WebSocket
                                                     tcp/41451        │          │ /ws/telemetry
                                                                      ▼          ▼
                                                        ┌──────────────────┐   ┌──────────────────────┐
                                                        │  AirSim / Unreal │   │ God's Eye View       │
                                                        │  (multirotor,    │   │ (Cesium globe)       │
                                                        │  cameras, lidar) │   │ uav layer + HUD      │
                                                        └──────────────────┘   └──────────────────────┘
```

**Components**

| Component | Responsibility |
|---|---|
| `godseye-mcp-server` | The MCP server. Translates MCP tool calls into AirSim msgpack-rpc (`airsim.MultirotorClient`, port 41451). Owns the safety envelope, the mission/task state machine, the vision-analysis workers, and the telemetry fan-out. |
| Agent harness | MCP *client*. Runs the LLM agent + skills (Section 8). May be local or remote from the sim host — drives transport choice (Section 4). |
| AirSim / Unreal | Physics, sensors, rendering. Source of ground truth. |
| God's Eye View | Existing Cesium 3D globe app. A new **`uav` layer** subscribes to the telemetry bridge websocket and renders drones, targets, trails, mission overlays next to its existing aircraft/vessel/satellite layers. |

**Design principles**

1. **Thin on the wire, thick at the edge.** The MCP server is not a dumb RPC proxy: it enforces safety, manages async task lifecycles, and runs analysis pipelines — because the LLM must never be in the real-time control loop.
2. **Everything blocking is a task.** Any tool that may take >2 s (takeoff, goto, grid search, orbit) returns a `task_id` immediately and reports via MCP `notifications/progress`. The agent never holds a socket open waiting for a 90-second flight leg.
3. **Ground truth is a resource, reasoning is a tool.** Continuous state (telemetry, tracks, camera frames) is exposed as MCP *resources* with subscriptions; decisions and actions are *tools*; doctrine and reusable procedures are *prompts* and harness *skills*.
4. **Fail safe, not sorry.** Every mutating tool is validated against the safety envelope *server-side* (Section 5). Skill-level ROE is additive and can only be stricter.

---
