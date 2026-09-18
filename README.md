# eye-in-the-sky

An agentic UAV **ISR** (intelligence, surveillance, reconnaissance) mission simulation. An LLM harness
loads a skill, connects over MCP, and flies reconnaissance, target-identification and threat-assessment
missions over **real-world terrain and data**, while an operator watches live on a photorealistic 3D globe.

**ISR-only.** There are no kinetic tools anywhere in the system and none may be added. It observes,
classifies and reports; command authority stays with the operator.

```
  agentic harness  (LLM + the godseye-uav skill)
            │  MCP · Streamable HTTP · Bearer
  ┌─────────▼──────────────────────────────┐
  │ godseye-mcp-server        :8791/mcp    │  45 tools · 8 resources
  │   safety envelope · FIFO queue · fuel  │  geofence · BINGO · lost-link
  │   └─ telemetry bridge     :8790        │  /snapshot /mission-overlay /camera /events
  └────┬──────────────────────────┬────────┘
       │ msgpack-rpc :41451       │ REST + SSE (read-only)
  ┌────▼─────────────┐   ┌────────▼──────────────┐
  │ AirSim  or  the  │   │ God's Eye View  :4173 │  the command center
  │ built-in fake    │   │ (Cesium 3D globe)     │
  └──────────────────┘   └───────────────────────┘
```

## Quick start (no GPU, no Unreal)

```bash
./scripts/setup.sh --with-ui
cd godseye && ./scripts/demo_laptop.sh
```

`setup.sh` creates the venv, installs the package, and fetches the pinned AirSim PythonClient — that
last part is **required**: `import airsim` is a runtime dependency and the client is a pinned checkout
of `microsoft/airsim`, not the PyPI release. `demo_laptop.sh` then boots the fake sim, the MCP server,
the bridge and the UI, and flies a scripted recon mission you can watch at <http://localhost:4173>.

| | |
|---|---|
| MCP server | `http://127.0.0.1:8791/mcp` — the **only** command path; Bearer auth |
| Telemetry bridge | `http://127.0.0.1:8790` — read-only feeds |
| Command center | `http://localhost:4173` — what the operator watches |

## Layout

| Path | What |
|---|---|
| `godseye/` | the simulation: MCP server, telemetry bridge, safety envelope, mission doctrine, intel, geo, fake AirSim, tests |
| `godseye/.agents/skills/godseye-uav/` | **the harness skill** — mission patterns, sensor doctrine, safety contract, SALUTE/INTREP templates |
| `gods-eye-view/` | the command-center UI. **Vendored third-party project** (MIT, © 2026 Bilawal Sidhu) plus this project's UAV layer |
| `scripts/setup.sh` | makes a fresh clone runnable |

Start with [`godseye/README.md`](godseye/README.md) for the full picture,
[`godseye/TOOL_CONTRACT.md`](godseye/TOOL_CONTRACT.md) for the authoritative tool catalog, and
[`godseye/PLAN.md`](godseye/PLAN.md) for the design and its numbered findings (M1–M22 / T1–T9).

## Connecting a harness

`godseye/.mcp.json` wires the server up for clients that read that format:

```json
{
  "mcpServers": {
    "godseye-uav": {
      "type": "http",
      "url": "http://127.0.0.1:8791/mcp",
      "headers": { "Authorization": "Bearer ${GODSEYE_MCP_TOKEN:-dev-token}" }
    }
  }
}
```

The skill's non-negotiable workflow is **task → plan → dry-run → execute → monitor → report**. The
dry-run returns waypoints, fuel estimate and the BINGO gate result *without flying anything*, and a
rejected gate means re-plan — never work around it.

## What makes it realistic

- **Real terrain** drives AGL, line-of-sight and a geofence floor. Over the Fordow ridge the aircraft
  reports `alt_agl_m = −42.2` where a launch-datum figure would say `+39.9`.
- **Real weather** drives the wind vector that feeds the fuel model; **real air traffic** (OpenSky)
  gives airspace contacts to deconflict; **real mapped installations** can seed an order of battle.
- Every such value carries provenance — `alt_agl_is_real`, `los_is_measured`, `traffic_is_real`,
  `wind_known` — because a feed that is down must never be mistaken for a clear reading.
- **Safety is enforced server-side**: geofence, ceiling, min-AGL, max speed, and BINGO fuel. Reaching
  BINGO forces a return-to-home that **cannot be cancelled**. A harness's ROE may only be *stricter*.

Tests: **1,004 Python** (zero GPU, against the fake AirSim) and **4,283 JavaScript**.

```bash
cd godseye && .venv/bin/python -m pytest tests -q
cd gods-eye-view && npm test
```

## Licence

This project is Apache-2.0 (`LICENSE`, `NOTICE`). It bundles and derives from third-party software and
data under their own terms — Microsoft AirSim (MIT, the `geo.py` port), God's Eye View (MIT), and
several datasets, **two of which are non-commercial**. Read
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) before making this repository public or using it
commercially.
