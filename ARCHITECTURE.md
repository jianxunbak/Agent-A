# ARCHITECTURE.md — Revit 2026 MCP Server

## Overview

**Revit 2026 MCP Server** is an AI-driven BIM automation system. It integrates Google Gemini with Autodesk Revit via a FastMCP (Model Context Protocol) server that exposes 40+ tools. An LLM uses those tools to procedurally generate and modify building designs.

---

## Directory Structure

```
revit-MCP/                              # Git repo root
├── .gitignore
├── GeminiMCP.extension/
│   ├── .env                            # GEMINI_API_KEY etc. (not committed)
│   ├── .env.example
│   ├── service-account.json            # Vertex RAG credentials (not committed)
│   ├── extension.json                  # pyRevit manifest
│   ├── AI Builder.tab/
│   │   └── AI Builder.panel/
│   │       └── Start Server.pushbutton/
│   │           └── script.py           # Revit ribbon → start_mcp_server()
│   ├── revit_mcp/                      # Core Python package
│   │   ├── server.py                   # FastMCP server, tool registration
│   │   ├── bridge.py                   # Thread-safe queue to Revit main thread
│   │   ├── runner.py                   # Uvicorn launcher + Idling event handler
│   │   ├── tool_logic.py               # All tool implementations
│   │   ├── tool_definitions.py         # MCP schema declarations
│   │   ├── dispatcher.py               # Orchestrator: state → Gemini → manifest → execute
│   │   ├── gemini_client.py            # Gemini API wrapper
│   │   ├── building_generator.py       # Manifest sync engine, element registry
│   │   ├── revit_workers.py            # 6-phase manifest executor (TransactionGroup)
│   │   ├── state_manager.py            # Extensible Storage metadata
│   │   ├── utils.py                    # Unit conversion, AppData paths, helpers
│   │   ├── agent_prompts.py            # Gemini system prompts
│   │   ├── lift_logic.py               # Passenger lift sizing (BS EN 81-20)
│   │   ├── staircase_logic.py          # Fire-escape staircase generation
│   │   ├── fire_safety_logic.py        # Fire-fighting lift + lobby (BS EN 81-72)
│   │   ├── core_layout_engine.py       # OR-Tools constraint solver for core modules
│   │   ├── spatial_registry.py         # 3D AABB collision detection
│   │   ├── preprocessors.py            # Revit failure-handling preprocessors
│   │   ├── progress_tracker.py         # SSE build-progress reporting
│   │   ├── build_memory.py             # Build Options/Revisions store
│   │   ├── cancel_manager.py           # Global cancellation flag
│   │   ├── config.py                   # Static config constants
│   │   ├── svg_to_footprint.py         # SVG path → footprint loops + holes
│   │   ├── building_presets.json       # Architectural DNA (commercial_office)
│   │   ├── compliance_fire_safety.json
│   │   ├── compliance_lift_engineering.json
│   │   ├── compliance_structural.json
│   │   ├── agents/
│   │   │   ├── main_agent.py           # Intent extraction (regex)
│   │   │   └── sub_agent.py            # Multi-intent dispatch + chunk cache
│   │   └── rag/
│   │       ├── vertex_rag.py           # Vertex AI RAG corpus retrieval
│   │       └── query_builder.py        # Intent → RAG topic queries
│   └── lib/                            # Bundled deps (no pip install)

%APPDATA%\RevitMCP\                     # Runtime artifacts (outside source tree)
├── logs/
│   ├── fastmcp_server.log
│   └── table_render_debug.log
├── cache/
│   ├── chunk_cache.json                # RAG chunk cache (offline fallback)
│   ├── rag_rules_cache.json            # Synthesised RAG rules
│   └── last_shell_state.json           # Shell snapshot for diff/edit
└── options/
    └── build_options[_<projectstem>].json  # Build memory (Options + Revisions)

revit-MCP/tests/                        # Sibling of GeminiMCP.extension/
├── __init__.py                         # Adds revit_mcp to sys.path
├── conftest.py
└── test_*.py                           # 14 pure-Python test modules
```

---

## Threading Model (Critical)

Revit's API is **single-threaded and main-thread-only**. All Revit API calls must cross from the async Uvicorn thread to Revit's event loop via `bridge.py`.

```
Uvicorn thread (async)
  └─ bridge.run_on_main_thread(fn)         ← BLOCKS here (1200s timeout)
       │
       │   queue.Queue (thread-safe)
       ▼
Revit main thread  [Idling event fires every ~100ms]
  └─ bridge.pump_commands(uiapp)
       └─ executes fn inside a Transaction
       └─ sets threading.Event → unblocks caller
```

**Never** call Revit API directly from FastMCP handlers or async contexts.

---

## Request Flows

### Simple Tool Call

```
LLM / User
  → FastMCP tool (server.py)
  → tool_logic.<fn>_ui(params)
  → bridge.run_on_main_thread(lambda)
  → Revit main thread executes in Transaction
  → JSON result returned to LLM
```

### Orchestrated Build (`orchestrate_build`)

```
orchestrate_build(prompt)
  → dispatcher.py
      ├─ classify_intent()                              # multi-intent, may dispatch >1
      ├─ gather_state()                                 # BIM scan, cached 30s
      ├─ rag/vertex_rag.retrieve_rules()                # RAG compliance lookup
      ├─ gemini_client.generate_content()               # Spatial Brain prompt + state + RAG rules
      ├─ extract JSON manifest
      ├─ QC validation prompt
      └─ revit_workers.execute_fast_manifest(manifest)
           ├─ Phase 1: Levels
           ├─ Phase 2: Vertical circulation (lifts + stairs + fire safety)
           ├─ Phase 3: Shell (walls + floors)
           ├─ Phase 4: Structure (columns + grids)
           ├─ Phase 5: Granular overrides (per-floor dims)
           └─ Phase 6: Cleanup (delete stale elements)
       (on CONFLICT: append description to prompt, retry up to 3 attempts)
       (on success: build_memory.save_new_option() → %APPDATA%\RevitMCP\options\)
```

---

## 6-Phase Build Execution (`revit_workers.py`)

All phases run inside a single `TransactionGroup`. Each phase opens its own `Transaction`.

| Phase | Contents | Key actions |
|-------|----------|-------------|
| 1 | Levels | Create/reuse Level objects, track elevations |
| 2 | Vertical circulation | Lifts (lift_logic), stairs (staircase_logic), fire lifts (fire_safety_logic); core_layout_engine OR-Tools solver places modules; calls `nuclear_lockdown()` |
| 3 | Shell | Walls + floors (including footprint holes); auto-void at core; calls `nuclear_lockdown()` |
| 4 | Structure | Columns on grid intersection points (polygon-aware infill) |
| 5 | Granular overrides | Floor-specific width/length/cantilever changes |
| 6 | Cleanup | Delete obsolete AI elements from registry |

All phases attach `HideJoinFailuresPreprocessor` or `NuclearJoinGuard` via `setup_failure_handling()` to suppress non-critical join/overlap warnings.

---

## State Tracking (`state_manager.py`)

Every AI-generated element is tagged with **Extensible Storage**:

```
Schema GUID: B6D5A8C1-F8B4-406F-9D6A-7E5C4B4C1234
Fields:
  AI_ID          (String)  — e.g. "AI_Wall_L1_S"
  GeometryHash   (String)  — geometry fingerprint
  SchemaVersion  (Int32)   — format version
```

A fallback **Comments parameter** is also set (same `AI_ID`) for fast scans without schema lookup.

`building_generator.get_model_registry(doc)` returns `{ai_id → ElementId}` — used to decide create vs. reuse.

---

## Runtime State Locations

All runtime files live under `%APPDATA%\RevitMCP\` (resolved by `utils.get_appdata_path(subfolder)`). Nothing is written to the source tree.

| Path | Contents | Writer |
|------|----------|--------|
| `logs\fastmcp_server.log` | Main server log | `utils.get_log_path()` |
| `logs\table_render_debug.log` | Table-render debug | `Start Server.pushbutton/script.py` |
| `cache\chunk_cache.json` | RAG chunk cache | `agents/sub_agent.py` |
| `cache\rag_rules_cache.json` | Synthesised RAG rules | `dispatcher.py` |
| `cache\last_shell_state.json` | Shell snapshot | `revit_workers.py` |
| `options\build_options.json` | Build memory (no project) | `build_memory.py` |
| `options\build_options_<stem>.json` | Build memory (saved project) | `build_memory.py` |

---

## Building Manifest Schema

Gemini produces (and `revit_workers.py` consumes) this JSON manifest. **All dimensions are millimeters.**

```json
{
  "project_setup": {
    "levels": 10,
    "level_height": 3500,
    "height_overrides": { "1": 5000, "10": "random" }
  },
  "shell": {
    "width": 30000,
    "length": 50000,
    "column_spacing": 10000,
    "parapet_height": 1100,
    "cantilever_depth": 0,
    "force_global_dimensions": false,
    "footprint_points": [],
    "footprint_holes": [],
    "footprint_svg": null,
    "footprint_rotation_overrides": {},
    "footprint_scale_overrides": {},
    "footprint_offset_overrides": {},
    "floor_overrides": {
      "4": { "width": 40000, "cantilever_depth": 2000 }
    }
  },
  "lifts": {
    "count": "random",
    "position": [0, 0],
    "occupancy_density": 0.1
  },
  "staircases": { "count": 2 },
  "walls": [],
  "floors": [],
  "columns": [],
  "registry_intent": "optional narrative"
}
```

`"random"` values are resolved by `utils.get_random_dim()` (±20% variation).

---

## Key Modules

### `bridge.py` — Thread Bridge
- `run_on_main_thread(fn, *args)` — queues work, blocks caller (1200s timeout); polls `cancel_manager.is_cancelled()` every 0.5s
- `pump_commands(uiapp)` — called from Revit Idling event; drains queue
- Logs latency warning if item waits >1.0s in queue

### `dispatcher.py` — Orchestrator
- `Orchestrator.run_full_stack(uiapp, prompt, tracker)` — full build pipeline
- Loads commercial_office preset DNA before each call
- State cache: 30s TTL; force-refresh when prompt contains "create" or "delete"
- Calls QC prompt validation after manifest generation
- Persists synthesised RAG rules to `%APPDATA%\RevitMCP\cache\rag_rules_cache.json` so they survive Revit restarts

### `gemini_client.py` — AI Client
- Persistent `httpx.Client` (120s timeout)
- Temperature: 0.1 (deterministic JSON output)
- Default model: `gemini-2.0-flash-exp` (overridden by `.env`)
- `chat()` → routes to Orchestrator; `generate_content()` → raw text generation

### `building_generator.py` — Sync Engine
- `BuildingSystem.sync_manifest(manifest)` — 3-phase sync (levels, shell, columns)
- `_expand_high_level_manifest()` — converts `project_setup`/`shell` fields into explicit walls/floors
- `get_model_registry(doc)` — fast scan via Extensible Storage + Comments

### `tool_logic.py` — Tool Implementations
- Every `_ui()` function validates args, builds a lambda, calls `bridge.run_on_main_thread()`
- `get_doc_info_ui()` returns full BIM state: levels, boundaries, core map, occupancy (3D), obstructions
- Element creation functions use `state_manager.set_ai_metadata()` to tag output
- `setup_failure_handling(t, use_nuclear=True)` attached to every transaction

### `utils.py` — Utilities
| Function | Purpose |
|----------|---------|
| `mm_to_ft(mm)` / `ft_to_mm(ft)` | Convert between mm and Revit internal feet |
| `get_random_dim(val, base)` | ±20% variation for `"random"` values |
| `nuclear_lockdown(doc)` | Disjoint ALL walls — run before major structural ops |
| `disallow_joins(wall)` | Disable AutoJoin + room-bounding on a wall |
| `setup_failure_handling(t, nuclear)` | Attach preprocessor to transaction |
| `find_level(doc, name_or_id)` | Lookup level by name or ElementId |
| `find_type_symbol(doc, bip, name)` | Lookup family type symbol |
| `get_appdata_path(subfolder)` | Resolve / create `%APPDATA%\RevitMCP\<subfolder>` |
| `get_log_path()` | Cached path to `%APPDATA%\RevitMCP\logs\fastmcp_server.log` |
| `load_presets()` | Load `building_presets.json` (next to module) |
| `load_compliance(name)` | Load `compliance_<name>.json` (next to module) |

---

## Vertical Circulation Subsystem

### Passenger Lifts (`lift_logic.py`)
- Sizing: BS EN 81-20 compliance
- RTT-based count calculation
- Compliance constants from `compliance_lift_engineering.json` and `compliance_structural.json`
- Max 12 lifts per block; back-to-back layout for larger buildings

### Fire-Escape Staircases (`staircase_logic.py`)
- Min 2 per building; positioned at N/S ends of lift core
- Riser: 150mm, Tread: 300mm, Flight width: 1500mm (defaults; per-typology in `building_presets.json`)
- Multi-flight for tall floors (2, 4, 6, 8 flights as needed)
- Polygon-aware travel-distance check; supports footprint holes

### Fire-Fighting Lifts (`fire_safety_logic.py`)
- BS EN 81-72 / BS 9999 compliant
- Compliance constants from `compliance_fire_safety.json` and `compliance_structural.json`
- Polygon-aware perimeter stair placement; rotation-aware geometry for EW vs NS attachment

---

## Spatial Integrity

### `core_layout_engine.py` — OR-Tools Solver
- Places fire-lift, fire-lobby, and staircase modules around the passenger lift bank ("anchor")
- Inputs: anchor bounds, module sizes, footprint polygon + holes, allowed sides, snap zone
- Constraints: stay inside footprint, avoid holes, respect chain ordering (fire lift → lobby → stair), keep anchor inside snap zone
- Returns module bboxes and `attach_side`, or `None` (INFEASIBLE)

### `spatial_registry.py` — 3D Collision Detection
- AABB overlap check with 10mm tolerance (touching edges allowed)
- `reserve(space_id, bbox, tags)` — claim 3D volume or raise on conflict
- `get_occupancy_map()` — full inventory for `get_doc_info_ui()`

---

## RAG Compliance System

`rag/vertex_rag.py` retrieves building code rules from a Vertex AI RAG corpus.
`rag/query_builder.py` maps building intent (type + storey count) to topic queries.
Results are merged into the Gemini prompt as an `AUTHORITY COMPLIANCE RULES` block.
Cancel-aware: HTTP calls poll `cancel_manager.is_cancelled()` every 0.5s.

The chunk cache lives in `%APPDATA%\RevitMCP\cache\chunk_cache.json` and is loaded at `agents/sub_agent.py` import time. It survives Revit restarts.

---

## Build Memory / Options

`build_memory.py` persists every successful build manifest as a named **Option** with **Revisions** for edits. Files live in `%APPDATA%\RevitMCP\options\` — one file per saved Revit project (named after the project's basename) plus a fallback `build_options.json` for unsaved documents.

The dispatcher reads this to understand which option is currently active, enabling "redo option A" style commands.

---

## Failure Handling (`preprocessors.py`)

Two `IFailuresPreprocessor` implementations suppress non-critical Revit errors:

| Class | Behaviour |
|-------|-----------|
| `HideJoinFailuresPreprocessor` | Suppresses join/overlap warnings selectively |
| `NuclearJoinGuard` | Deletes ALL warnings unconditionally (used in nuclear ops) |

Suppressed failures include: `AttemptedJoinFailed`, `WallsOverlap`, `FloorsOverlap`, `CurvesOverlap`, `DuplicateValue`, `RoomBoundaryLinesOverlap`.

---

## Cancellation (`cancel_manager.py`)

Global thread-safe flag (`request_cancel()`, `is_cancelled()`, `clear_cancel()`, `check_cancelled()`).
Polled by:
- `bridge.run_on_main_thread()` — every 0.5s while waiting for the main thread
- `revit_workers._process_*()` — between every level inside the build phases
- `rag/vertex_rag._do_search()` — every 0.5s during HTTP polls
- `agents/sub_agent.run_retrieve_rules()` — every 0.5s

`orchestrate_build` calls `clear_cancel()` at the start of every build and converts `asyncio.CancelledError` into `request_cancel()` for downstream consumers.

---

## Progress Reporting (`progress_tracker.py`)

- `BuildProgressTracker.start()` — begins async poller on Uvicorn event loop
- `report(msg)` — queues status update; sent via `ctx.info()` over SSE
- `analyze_manifest(manifest)` — estimates element counts + build time before execution
- `generate_final_report()` — summary with duration, element counts, design adjustments

---

## Building Presets (`building_presets.json`)

Currently: `commercial_office` DNA only.

Key fields:
- `typical_floor_height`: 4200mm; first storey: 8400mm
- `column_logic.span`: 12,000–15,000mm; offset_from_edge: 500mm
- `program_requirements.core_area_ratio`: 20–25%
- `program_requirements.minimum_distance_facade_to_core`: 12,000mm
- `core_logic.lift_waiting_time`: 25s target interval
- `core_logic.fire_safety.max_travel_distance`: 60,000mm

---

## Gemini Integration (`agent_prompts.py`)

| Prompt | Role |
|--------|------|
| `SPATIAL_BRAIN_SYSTEM_INSTRUCTION` | Lead Architect — form resolution + 4-step core planning |
| `DISPATCHER_PROMPT` | Routes question vs. build, validates manifest conflicts |
| `QC_PROMPT` | Manifest schema validation after generation |

Core planning protocol (Steps 0–4 inside Spatial Brain):
0. Form resolution — interpret prompt into footprint shape, rotations, scales, offsets, SVG, or footprint_points
1. Space Inventory — list all core spaces with min dimensions
2. Boundary Planning — assign non-overlapping rectangular zones
3. Efficiency Check — core = 20–25% of floor area
4. Commit — emit JSON manifest

---

## Dependencies

All bundled in `GeminiMCP.extension/lib/` — no pip install required. Runs in Revit's embedded CPython 3.12.

| Package | Use |
|---------|-----|
| `fastmcp` | MCP protocol server |
| `uvicorn` | ASGI server (port 8001) |
| `httpx` | Persistent HTTP session to Gemini and Vertex |
| `pydantic` | Data validation |
| `anyio` | Async I/O |
| `ortools` | Constraint solver in `core_layout_engine.py` |

---

## Entry Points

1. **Revit ribbon button** → `AI Builder.tab/AI Builder.panel/Start Server.pushbutton/script.py` → `runner.start_mcp_server()`
   - Checks if port 8001 already in use (re-links to existing server if so)
   - Spawns Uvicorn on background thread
   - Registers `bridge.idling_handler` on `UIApplication.Idling`

2. **MCP client** (Claude Desktop / any MCP host) connects to `http://localhost:8001/sse`

---

## Unit Convention

| Context | Unit |
|---------|------|
| Manifest JSON | **millimeters** |
| Revit API internal | **feet** |
| Conversion | `utils.mm_to_ft()` / `utils.ft_to_mm()` |
