# memory.md - Project Status & Progress

## Status: May 2026

### Completed
- [x] FastMCP server + Revit bridge (queue/Idling pattern)
- [x] Full 6-phase manifest executor (`revit_workers.py`) with TransactionGroup
- [x] Staircase generation (`staircase_logic.py`) — risers, multi-flight, landing shapes
- [x] Fire-fighting lift + lobby generation (`fire_safety_logic.py`) — BS EN 81-72/BS 9999
- [x] Passenger lift sizing (`lift_logic.py`) — BS EN 81-20, RTT-based count
- [x] Spatial collision detection (`spatial_registry.py`) — 3D AABB, 10mm tolerance
- [x] OR-Tools core module solver (`core_layout_engine.py`) — anchors fire lift / lobby / staircase around the passenger lift bank with snap-zone and footprint-hole awareness
- [x] Progress reporting via SSE (`progress_tracker.py`)
- [x] Extensible Storage state tracking (`state_manager.py`) + Comments fallback
- [x] Failure-handling preprocessors (`preprocessors.py`) — HideJoinFailures + NuclearJoinGuard
- [x] Gemini integration with Spatial Brain + Dispatcher + QC prompts (`agent_prompts.py`)
- [x] Vertex AI RAG retrieval for SCDF / NEA compliance rules (`rag/vertex_rag.py`)
- [x] Build memory / Options + Revisions store (`build_memory.py`)
- [x] Cancellation system with thread-safe flag (`cancel_manager.py`)
- [x] SVG → footprint multi-loop conversion for organic shells with courtyards (`svg_to_footprint.py`)
- [x] Tests consolidated into top-level `tests/` folder; runtime artifacts redirected to `%APPDATA%\RevitMCP\`

### Known Issues / Active Bugs
- **Conflict-retry feedback is thin:** `revit_workers.py` reports CONFLICT with zone names only (no coordinates/dimensions), so Gemini has limited info to reason about a fix
- **CLAUDE.md previously referenced a `core_planner.py`** — that module was renamed/replaced by `core_layout_engine.py`; older tests/docs may still mention it
- Intermittent OR-Tools INFEASIBLE returns for tight courtyard buildings (see `tests/test_placement_pipeline.py` scenarios)

### Next Steps
1. Improve CONFLICT description: pass coordinates / module bbox so Gemini can reason about spatial fixes
2. Investigate remaining INFEASIBLE cases when courtyard buildings are near the minimum feasible footprint
3. NEA COPEH RAG corpus integration for toilet layout compliance

### Future Features
- **Curtain Walls:** Procedural generation from building presets
- **Toilets:** Automated restroom core layout (NEA COPEH compliant)
- **Multi-typology presets:** Beyond `commercial_office` (residential, mixed-use)

### Architecture Notes
- All manifest dimensions in **millimeters**; Revit API uses **feet** (`utils.mm_to_ft`)
- `bridge.run_on_main_thread()` timeout: **1200s**
- Gemini model: `gemini-2.0-flash-exp` (override in `.env`)
- State cache TTL: 30s; force-refresh when prompt contains "create" or "delete"
- `nuclear_lockdown()` is called before every major shell/structure phase
- All runtime files (logs, caches, build memory) live under `%APPDATA%\RevitMCP\` — nothing is written to the source tree
