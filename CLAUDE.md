# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **Revit 2026 MCP Server** — an AI-driven BIM automation system that integrates Google Gemini with Autodesk Revit to procedurally generate and modify building designs. The MCP server exposes 40+ tools that an LLM can call to create/edit Revit elements.

## Configuration

Copy `GeminiMCP.extension/.env.example` to `GeminiMCP.extension/.env` and set:
```
GEMINI_API_KEY=your_key_here
GEMINI_MODEL=gemini-2.0-flash-exp
```

`service-account.json` (Vertex AI RAG credentials) also lives in `GeminiMCP.extension/`.

## Runtime State Locations

All runtime artifacts (logs, caches, build memory) are written to **`%APPDATA%\RevitMCP\`** — never inside the source tree. The path is resolved by `utils.get_appdata_path(subfolder)`.

| Subfolder | Contents | Written by |
|---|---|---|
| `logs/` | `fastmcp_server.log`, `table_render_debug.log` | `utils.get_log_path()`, `Start Server.pushbutton/script.py` |
| `cache/` | `chunk_cache.json`, `rag_rules_cache.json`, `last_shell_state.json` | `agents/sub_agent.py`, `dispatcher.py`, `revit_workers.py` |
| `options/` | `build_options.json` (or `build_options_<projectstem>.json`) | `build_memory.py` |

## Testing

All test files live in `tests/` (sibling of `GeminiMCP.extension/`). Run from this folder (`revit-MCP/`):

```bash
python -m unittest tests.test_landing_shapes
python -m unittest tests.test_staircase_logic
python -m unittest tests.test_polygon_travel
python -m unittest tests.test_core_integration
python -m unittest tests.test_fire_safety_compliance
```

`tests/__init__.py` adds `GeminiMCP.extension/` to `sys.path` so imports like `from revit_mcp.staircase_logic import …` resolve. None of these tests require a live Revit instance — they exercise pure-Python logic only.

## Architecture

### Threading Model (Critical)

Revit's API is **single-threaded and main-thread-only**. All Revit API calls must go through `bridge.py`:

- `bridge.py` maintains a work queue consumed on Revit's main thread via an `Idling` event handler registered in `runner.py`
- `tool_logic.py` functions call `bridge.run_on_main_thread(fn)` — this blocks the caller until Revit executes `fn` on the main thread (1200s timeout)
- **Never** call Revit API directly from FastMCP tool handlers or async contexts

### Request Flow

```
User/LLM → FastMCP tool call (server.py)
         → tool_logic.py (validates args, builds lambda)
         → bridge.run_on_main_thread() [blocks here]
         → Revit main thread executes lambda inside Transaction
         → Returns result
```

For the orchestrated build path:
```
orchestrate_build(prompt) → dispatcher.py
  → classify_intent() — multi-intent classification (may dispatch 2+ intents)
  → Gather BIM state (cached 30s; force-refresh on "create"/"delete")
  → RAG compliance lookup (rag/vertex_rag.py → chunk_cache fallback in %APPDATA%\RevitMCP\cache\)
  → Send state + RAG rules + prompt to Gemini (gemini_client.py)
  → Extract JSON manifest from response
  → QC validation prompt
  → revit_workers.py (6-phase execution)
  → On CONFLICT result: append conflict description to prompt, retry (max 3 attempts)
  → Save successful manifest to build_memory.py (Options/Revisions in %APPDATA%\RevitMCP\options\)
```

### 6-Phase Build Execution (`revit_workers.py`)

All manifest execution runs in a `TransactionGroup` with phase-level `Transaction`s:
1. Levels
2. Vertical circulation (lifts/stairs via `lift_logic.py`, `staircase_logic.py`, `fire_safety_logic.py`) — returns `{"status": "CONFLICT", ...}` on spatial collision; this bubbles back to `dispatcher.py` to trigger a Gemini retry
3. Shell (walls, floors — calls `nuclear_lockdown()` first)
4. Structure (columns, grids)
5. Granular overrides (floor-specific dimension changes)
6. Cleanup (delete stale AI elements)

Phases 2.5 and 2.9 are pre-cleanup sub-steps inside phase 2 (staircase cleanup, bulk AI element deletion) — they don't have their own transactions but run between the main phases.

### State Tracking

`state_manager.py` stores metadata on every AI-generated element using Revit's **Extensible Storage** (schema GUID: `B6D5A8C1-F8B4-406F-9D6A-7E5C4B4C1234`). Fields: `AI_ID`, `GeometryHash`, `SchemaVersion`. A Comments parameter fallback is also written for fast registry scans. This enables detecting which elements are AI-managed vs user-created.

### Key Utilities

- **`utils.nuclear_lockdown(doc)`** — Disjoints all walls in the model before structural operations; prevents "can't edit attached wall" errors
- **`utils.mm_to_ft(mm)`** / **`utils.ft_to_mm(ft)`** — All Revit API calls use feet internally; all manifest JSON uses mm
- **`utils.get_random_dim(value, base)`** — Applies ±20% variation when manifest specifies `"random"` for a dimension
- **`utils.setup_failure_handling(t, use_nuclear)`** — Attaches `IFailuresPreprocessor` to suppress join/overlap warnings
- **`utils.get_appdata_path(subfolder)`** — Returns/creates `%APPDATA%\RevitMCP\<subfolder>`; the single source of truth for runtime file locations
- **`utils.get_log_path()`** — Returns `%APPDATA%\RevitMCP\logs\fastmcp_server.log` (cached)

### Building Manifest Schema

The JSON manifest produced by Gemini and consumed by `revit_workers.py`. **All dimensions are millimeters.**
```json
{
  "project_setup": {
    "levels": 10,
    "level_height": 3500,
    "height_overrides": { "1": 5000 }
  },
  "shell": {
    "width": 30000, "length": 50000,
    "column_spacing": 10000, "parapet_height": 1100,
    "floor_overrides": { "4": { "width": 40000 } }
  },
  "lifts": { "count": "random", "occupancy_density": 0.1 },
  "staircases": { "count": 2 }
}
```

### Vertical Circulation Subsystem

- **`lift_logic.py`** — Passenger lift sizing per BS EN 81-20; RTT-based count; max 12 lifts/block. Reads `compliance_lift_engineering.json` and `compliance_structural.json`.
- **`staircase_logic.py`** — Fire-escape stairs; min 2 per building; 150mm riser, 300mm tread. Reads `compliance_fire_safety.json` and `compliance_structural.json`.
- **`fire_safety_logic.py`** — Fire-fighting lifts + lobbies per BS EN 81-72/BS 9999. Reads `compliance_fire_safety.json` and `compliance_structural.json`.

### Spatial Integrity

- **`core_layout_engine.py`** — OR-Tools-based constraint solver for placing fire lift, lobby, and staircase modules around an anchor (the passenger lift bank). Returns module bounds or `None` if infeasible.
- **`spatial_registry.py`** — 3D AABB collision detection; 10mm tolerance for shared walls.

### Conflict Retry Loop

When `revit_workers.py` returns `{"status": "CONFLICT", "description": "..."}`, `dispatcher.py` appends the description to the Gemini prompt and retries up to `max_attempts` (default 3). The conflict message currently contains only the names of the conflicting spatial zones (e.g. `"'Core_Set_2' overlaps with 'FireLift_North'"`), not coordinates or dimensions — so the AI has limited information to reason about the fix.

### RAG Compliance System

`rag/vertex_rag.py` retrieves building code rules from a Vertex AI RAG corpus. `rag/query_builder.py` maps building intent (type + storey count) to topic queries. Results are merged into the Gemini prompt as an `AUTHORITY COMPLIANCE RULES` block. The chunk cache lives in `%APPDATA%\RevitMCP\cache\chunk_cache.json` and survives Revit restarts (loaded by `agents/sub_agent.py` at import time).

### Build Memory / Options

`build_memory.py` persists every successful build manifest to `%APPDATA%\RevitMCP\options\build_options.json` (or `build_options_<projectstem>.json` for saved Revit projects) as a named **Option** with **Revisions** for edits. The dispatcher reads this to understand which option is currently active, enabling "redo option A" style commands.

### Gemini Prompt Architecture

All system prompts are in `agent_prompts.py`. The `SPATIAL_BRAIN_SYSTEM_INSTRUCTION` defines the core planning protocol (Steps 0–4: form resolution → space inventory → boundary planning → efficiency check). `DISPATCHER_PROMPT` extends it with output format rules and a strict 4000-character budget. The `<resolution_thoughts>` block in Gemini responses is currently not parsed — it's for human debugging only.

### Failure Handling

`preprocessors.py` provides two `IFailuresPreprocessor` implementations:
- `HideJoinFailuresPreprocessor` — Selective suppression of join/overlap warnings
- `NuclearJoinGuard` — Deletes ALL warnings unconditionally

### Dependencies

All Python dependencies are pre-bundled in `GeminiMCP.extension/lib/` — do not install via pip. The extension runs in Revit's embedded Python 3.12 environment. Key packages: `fastmcp`, `uvicorn`, `httpx`, `pydantic`, `anyio`, `ortools`.

## File Map

| File | Role |
|------|------|
| `server.py` | FastMCP server — tool registration and schema |
| `tool_logic.py` | Tool implementations — each calls `bridge.run_on_main_thread()` |
| `tool_definitions.py` | MCP tool schema declarations (separate from implementations) |
| `bridge.py` | Thread-safe queue between async server and Revit main thread |
| `runner.py` | Uvicorn launcher + Revit Idling event handler registration |
| `dispatcher.py` | Orchestrator: BIM state → Gemini → manifest → execution |
| `gemini_client.py` | Gemini API wrapper (httpx persistent session, temp 0.1) |
| `building_generator.py` | Manifest sync engine, element registry management |
| `revit_workers.py` | 6-phase manifest executor with TransactionGroup |
| `agent_prompts.py` | All Gemini system prompts (Spatial Brain, Dispatcher, QC) |
| `state_manager.py` | Extensible Storage read/write for AI element metadata |
| `lift_logic.py` | Passenger lift sizing + placement (BS EN 81-20) |
| `staircase_logic.py` | Fire-escape staircase generation |
| `fire_safety_logic.py` | Fire-fighting lift + lobby generation (BS EN 81-72) |
| `core_layout_engine.py` | OR-Tools constraint solver for core module placement |
| `spatial_registry.py` | 3D AABB collision detection |
| `preprocessors.py` | Revit IFailuresPreprocessor implementations |
| `progress_tracker.py` | Build progress reporting via SSE |
| `build_memory.py` | Options/Revisions store — persists manifests to AppData |
| `cancel_manager.py` | Global cancellation flag checked between build phases |
| `config.py` | Static config constants |
| `svg_to_footprint.py` | Converts SVG path strings to Revit curve arrays for organic footprints |
| `utils.py` | Unit conversion, AppData paths, log path, `nuclear_lockdown`, helpers |
| `agents/main_agent.py` | Intent extraction from user prompt (regex, no LLM) |
| `agents/sub_agent.py` | Sub-query dispatch for multi-intent prompts; chunk cache loader |
| `rag/vertex_rag.py` | Vertex AI RAG corpus retrieval for compliance rules |
| `rag/query_builder.py` | Maps building intent → RAG topic queries |
| `building_presets.json` | Architectural DNA templates (currently: `commercial_office`) |
| `compliance_fire_safety.json` | Fire-safety compliance constants |
| `compliance_lift_engineering.json` | Lift sizing/speed/wall-thickness constants |
| `compliance_structural.json` | Structural compliance constants |
