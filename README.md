# Revit 2026 MCP Server

AI-driven BIM automation for Autodesk Revit 2026. The extension installs as a pyRevit ribbon button. When you click the **AI Builder** button, it opens a chat window inside Revit — type a prompt like *"30-storey commercial office, 60×80m with central courtyard"* and Google Gemini plans the building, then the extension procedurally generates every level, wall, floor, column, lift core, and fire-escape staircase in the model.

The chat window is the primary UI. The extension also exposes the same tools via the Model Context Protocol on `http://localhost:8001/sse`.

---

## Workflow

The system has four moving parts: a **WPF chat window** running on Revit's UI thread, a **FastMCP/Uvicorn server** running on a background thread, a **dispatcher** that talks to Gemini and assembles a JSON building manifest, and a **6-phase worker** that executes that manifest inside Revit transactions. The four diagrams below zoom in from the top-level loop to the threading bridge that holds it all together.

### 1. End-to-end flow (high level)

```
┌──────────────┐   prompt    ┌────────────┐   classified    ┌────────────┐
│ Chat window  │ ──────────► │ dispatcher │ ──────────────► │   Gemini   │
│  (in Revit)  │             │  .py       │                 │    API     │
└──────────────┘             └────────────┘ ◄─────────────  └────────────┘
       ▲                          │           JSON manifest
       │                          ▼
       │                    ┌────────────┐   Transactions   ┌────────────┐
       │  result text       │ revit_     │ ───────────────► │ Revit 2026 │
       └────────────────────│ workers.py │                  │   model    │
                            └────────────┘ ◄─────────────── └────────────┘
                                              built elements
```

1. User clicks **AI Builder → Start Server**. `script.py` boots the chat window (WPF) and launches Uvicorn on `localhost:8001`.
2. User types a prompt; the chat panel forwards it to `gemini_client.client.chat_with_orchestrator()` which calls `orchestrator.run_full_stack(uiapp, prompt, history)` in `dispatcher.py`.
3. The dispatcher classifies intent, gathers BIM state, runs RAG (if enabled), and asks Gemini for a JSON building manifest.
4. `revit_workers.execute_fast_manifest()` consumes the manifest and creates levels, walls, floors, columns, lifts, and stairs across six transactional phases.
5. The chat window streams progress (via `progress_tracker.py` SSE) and displays the final result.

### 2. Dispatcher detail (what happens between prompt and manifest)

```
                user_prompt + history
                        │
                        ▼
        ┌────────────────────────────────┐
        │ classify_intent (Gemini call)  │── multi-intent: clarify / query /
        │  → {intents: [...]}            │   authority_query / build / new_build /
        └────────────────────────────────┘   delete / clear_chat / rollback
                        │
        ┌───────────────┴────────────────┐
        │                                │
   fast intents                    build / new_build
   (clarify, query,                       │
    authority, delete,                    ▼
    rollback)                   ┌──────────────────┐
        │                       │ gather BIM state │  cached 30 s; force-refresh
        │                       │ (main thread)    │  on "create"/"delete"/"clear"
        │                       └──────────────────┘
        │                                │
        │                                ▼
        │                       ┌──────────────────┐
        │                       │ RAG block        │  Vertex AI Search
        │                       │ (if RAG_ENABLED) │  → rag_rules_cache.json
        │                       └──────────────────┘
        │                                │
        │                                ▼
        │                       ┌──────────────────┐
        │                       │ Build Gemini     │  DISPATCHER_PROMPT +
        │                       │ prompt           │  presets + compliance +
        │                       └──────────────────┘  state + history + RAG
        │                                │
        │                                ▼
        │                       ┌──────────────────┐
        │                       │ Gemini → JSON    │  4000-char output budget
        │                       │ manifest         │  retry on malformed JSON (×3)
        │                       └──────────────────┘
        │                                │
        │                                ▼
        │                       ┌──────────────────┐
        │                       │ QC validation    │  second Gemini pass checks
        │                       │ pass             │  manifest sanity
        │                       └──────────────────┘
        │                                │
        │                                ▼
        │                       ┌──────────────────┐    CONFLICT
        │                       │ revit_workers    │ ──────────────┐
        │                       │ .execute_fast_   │               │
        │                       │ manifest()       │               │
        │                       └──────────────────┘               │
        │                                │                         │
        │                                │ OK                      │
        │                                ▼                         │
        │                       ┌──────────────────┐               │
        │                       │ build_memory →   │               │
        │                       │ save Option/Rev  │               │
        │                       └──────────────────┘               │
        │                                │                         │
        ▼                                ▼                         ▼
   text reply                       text reply              append conflict
   to chat window                   to chat window          → retry (≤3 attempts)
```

1. **Classify** — `client.classify_intent()` returns `{"intents": [...]}`. Multiple intents per message are supported (e.g. *"clear the model and tell me about SCDF Table 2.2A"*).
2. **Fast-path** — `clarify`, `clear_chat`, `query`, `authority_query`, `delete`, `rollback` complete without touching the build pipeline.
3. **State gather** — `gather_state()` runs on Revit's main thread, returns levels / heights / overrides / element counts. Cached 30 s; invalidated by `create`/`delete`/`clear`/`wipe` keywords.
4. **RAG (optional)** — when `RAG_ENABLED=true`, `vertex_rag.py` retrieves authority rules keyed by `(building_type, storeys)`. Result cached on disk in `rag_rules_cache.json`.
5. **Prompt assembly** — `DISPATCHER_PROMPT` + presets JSON + compliance JSON + current state + chat history + RAG block + user prompt.
6. **Manifest generation** — Gemini at temperature 0.1, 4000-char output budget. Up to 3 retries if the response isn't valid JSON.
7. **QC pass** — second Gemini call validates the manifest before execution.
8. **Execute** — `revit_workers.execute_fast_manifest()` runs the 6-phase build (next diagram).
9. **Conflict retry** — if a phase returns `{"status": "CONFLICT", "description": "..."}`, the description is appended to the prompt and Gemini is re-invoked. Max 3 attempts.
10. **Persist** — successful manifest saved to `build_options.json` as a named **Option**. Subsequent edits become **Revisions** of that option.

### 3. The 6-phase build (inside `revit_workers.py`)

```
                 ┌──────────────────────── TransactionGroup ────────────────────────┐
                 │                                                                  │
   manifest ───► │  Phase 1   Phase 2          Phase 3    Phase 4    Phase 5    P6  │ ───► built
                 │  ┌──────┐ ┌──────────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌──┐ │     model
                 │  │Levels│ │Vert. circul. │ │ Shell  │ │Structure│ │Override│ │Cl│ │
                 │  └──────┘ │ • passenger  │ │ • walls│ │• columns│ │• per-  │ │ea│ │
                 │           │   lifts      │ │ • floors│ │  on grid│ │  floor │ │nu│ │
                 │           │ • fire stairs│ │        │ │        │ │  width │ │p │ │
                 │           │ • fire lifts │ │        │ │        │ │  /len  │ │  │ │
                 │           │ • fire lobby │ │        │ │        │ │        │ │  │ │
                 │           └──────────────┘ └────────┘ └────────┘ └────────┘ └──┘ │
                 │                                                                  │
                 │  each phase = its own Transaction with IFailuresPreprocessor     │
                 └──────────────────────────────────────────────────────────────────┘
```

1. **Levels** — create or reuse Revit `Level` objects matching `project_setup.levels` and `level_height`/`height_overrides`.
2. **Vertical circulation** — `core_layout_engine.py` (OR-Tools CP-SAT) places fire stair / fire lobby / fire lift / passenger lift modules around an anchor; `lift_logic.py` sizes passenger lifts (BS EN 81-20), `staircase_logic.py` builds fire-escape stairs (150 mm riser / 300 mm tread, min 2/building), `fire_safety_logic.py` adds fire-fighting lifts and lobbies (BS EN 81-72, BS 9999). `nuclear_lockdown(doc)` runs first to disjoint walls. Returns `CONFLICT` if `spatial_registry.py` detects an AABB collision.
3. **Shell** — exterior walls and floors (rectangular, polygon, or SVG-path footprint, with optional courtyard hole). `nuclear_lockdown()` again before this phase.
4. **Structure** — columns placed on the column-spacing grid.
5. **Granular overrides** — per-floor width / length / cantilever changes from `floor_overrides`.
6. **Cleanup** — delete obsolete AI-managed elements that aren't in the new manifest (identified via Extensible Storage `AI_ID`).

A `CONFLICT` from any phase aborts the TransactionGroup and bubbles back to step 9 of diagram 2.

### 4. Threading bridge (why none of this deadlocks)

```
   ┌─────────────────── Revit main thread (single, STA) ───────────────────┐
   │                                                                       │
   │   UIApplication.Idling ──► pump_commands(uiapp) ──► drain queue       │
   │   ▲                                ▲                     │            │
   │   │  (also Win32 PostMessage      │                     │            │
   │   │   + DispatcherTimer fallback) │                     ▼            │
   │   │                                │              run lambdas in     │
   │   │  WPF Chat Window               │              Revit Transactions │
   │   │  (XAML, on UI thread)          │                                 │
   │   └────────────────────────────────┴─────────────────────────────────┘
                       ▲                         ▲
                       │ result                  │ blocks 1200 s max
                       │                         │
   ─────────────────── │ ─── thread boundary ─── │ ───────────────────────
                       │                         │
   ┌─────────────────  │ ──── Uvicorn / FastMCP background thread ───────┐
   │                   │                         │                       │
   │   server.py  ──►  tool_logic.py  ──►  bridge.run_on_main_thread(fn)│
   │   (FastMCP            │                                             │
   │    tool handler)      │  enqueues fn + Event                        │
   │                       │  waits on Event                             │
   └───────────────────────┼─────────────────────────────────────────────┘
                           │
                  same bridge is used by
                  dispatcher.py for state-gather
                  and main-thread tool calls
```

1. Revit's API is **single-threaded** — calls are only legal on the UI thread, inside an Idling event or an `ExternalEvent`.
2. Uvicorn / FastMCP runs on a **background thread**, so it cannot call Revit directly.
3. `bridge.run_on_main_thread(fn)` enqueues `fn` plus a `threading.Event`, then blocks the caller (1200 s timeout).
4. Three wake strategies push Revit to drain the queue: registered `UIApplication.Idling` handler, Win32 `PostMessage` + WPF `DispatcherTimer` (fallback), and Idling-only (last resort, requires user mouse activity).
5. `pump_commands(uiapp)` runs on the main thread, executes each queued lambda inside a fresh `Transaction`, stores the return value, and signals the Event — unblocking the background caller with the result.
6. **Rule:** every Revit DB / UI call inside `tool_logic.py`, `dispatcher.py`, and `revit_workers.py` is wrapped in `mcp_event_handler.run_on_main_thread(...)`. Direct DB access from a background context is a bug.

---

## What the chat window can do

Type natural-language prompts into the Revit chat panel. The dispatcher classifies intent and routes to the right handler.

**Building generation**
- *"Create a 20-storey office, 60×40m, central courtyard 20×20m"* — full procedural build (levels, shell, columns, lift core, fire stairs, fire-fighting lobbies)
- *"Make it L-shaped"*, *"add a 15° twist toward the top"*, *"taper the upper floors"* — non-rectangular shells, polygon footprints, SVG paths, courtyards (footprint holes)
- *"Apply SCDF fire code"* — when `RAG_ENABLED=true`, real building-code rules (fire safety, lift engineering) are pulled from Vertex AI and merged into the prompt

**Editing existing builds**
- *"Make floor 5 wider by 5m"* — per-floor overrides
- *"Increase first storey height to 6m"* — level-specific overrides
- *"Regenerate staircases"* — heal stairs after manual level-height edits

**Build memory (Options & Revisions)**
- *"List options"*, *"rollback to selected option"*
- Every successful build is saved as a named **Option**; subsequent edits become **Revisions** of that option
- Stored per-Revit-project at `%APPDATA%\RevitMCP\options\build_options_<project>.json`

**Queries about the current model**
- *"What's the current floor count?"*, *"How many walls are AI-managed?"*, *"List columns on level 3"*

**Authority code questions** (when `RAG_ENABLED=true`)
- *"What's the minimum stair width per SCDF Table 2.2A?"*, *"What is the requirement for fire-fighting lift lobbies?"*, *"Show me clause 2.4 from the SCDF Fire Code"*, *"How do I count Fire Access Panels for office Building?"*
- The dispatcher routes these through Vertex AI RAG, retrieves the matching chunks, and Gemini summarises them with citations.

## What the chat window can't do (yet)

- **Only one building typology is wired up: `commercial_office`.** Residential, mixed-use, retail, healthcare, etc. would need new entries in `building_presets.json` plus matching planner logic. The chat will accept the prompt and try, but the resulting design DNA (floor heights, column spans, core ratios) will be commercial-office defaults.
- **No furniture, MEP, or interior fitout.** The system generates the shell + structure + vertical circulation. Doors and windows are placed only when you explicitly ask via low-level tools.
- **Compliance is Singapore-flavoured.** RAG defaults reference SCDF Fire Code; lift logic uses BS EN 81-20/72 and BS 9999. Other jurisdictions would need their own rules in the RAG corpus or in the compliance JSON files.
- **No rendering, materials, or visual styling.** All elements use Revit's default types unless you call low-level tools to change them.
- **No multi-user collaboration awareness.** The state cache assumes one Revit document at a time.
- **No undo across the build pipeline.** Each phase is its own Transaction inside a TransactionGroup, but if a build fails halfway, you'll need to delete partial output manually (or use *"clear all AI elements"*).
- **No site context.** No terrain, no surrounding buildings, no setbacks computed from a site polygon.
- **Curtain walls, roofs, ramps, stairs other than fire-escape — not generated automatically.**
- **The chat window doesn't render images.** It's a text panel; floor plans / 3D previews live in Revit's normal views.

---

## What the codebase can do (tools available to the LLM)

The MCP server registers ~45 tools. Categories:

| Category | Examples |
|---|---|
| **Orchestration** | `orchestrate_build`, `cancel_build`, `regenerate_staircases`, `sync_building_manifest` |
| **Walls / floors / columns** | `create_wall`, `create_arc_wall`, `create_floor`, `create_polygon_floor`, `create_column`, `edit_wall`, `edit_column` |
| **Hosted elements** | `create_door`, `create_window`, `edit_hosted_element` |
| **Levels / grids** | `create_level`, `create_grid`, `create_arc_grid`, `edit_grid`, `query_levels` |
| **Family types** | `duplicate_family_type`, `place_family_instance`, `list_family_types`, `query_types`, `edit_type` |
| **Generic edits** | `move_element`, `move_staircase`, `set_parameter`, `get_parameters`, `edit_element` |
| **Deletion** | `delete_walls`, `delete_element`, `delete_elements_by_filter`, `delete_all_elements` |
| **Inspection** | `get_document_info`, `get_element_details`, `list_elements`, `get_building_metrics` |
| **Build memory** | `list_build_options`, `rollback_to_option`, `export_option_to_json` |
| **Health** | `heartbeat`, `check_bridge_health` |

These are the same tools the in-Revit chat window calls under the hood. They're also what an external MCP client would see if you connect one.

---

## How to use it

### 1. Prerequisites

- **Autodesk Revit 2026** on Windows.
- **pyRevit** installed and configured (the extension uses pyRevit's ribbon and bundled CPython).
- A **Google Gemini API key** ([Google AI Studio](https://aistudio.google.com/app/apikey)).
- *(Optional)* A Google Cloud project with Vertex AI Search / Discovery Engine enabled and a service-account JSON key — only needed if you want code-aware RAG retrieval.
- *(Optional)* An external MCP client (Claude Desktop, Cursor, etc.) — only if you want to drive Revit from outside the in-Revit chat window.

### 2. Install the extension

Clone this repo into your pyRevit extensions folder (or symlink it):

```powershell
cd "$env:APPDATA\pyRevit\Extensions"
git clone <this-repo-url> revit-MCP
```

(If you clone elsewhere, point pyRevit at that folder via *pyRevit Settings → Custom Extension Directories*.)

Restart Revit. The **AI Builder** tab should appear on the ribbon.

### 3. Configure environment

Inside `GeminiMCP.extension/`:

```powershell
cd "$env:APPDATA\pyRevit\Extensions\revit-MCP\GeminiMCP.extension"
copy .env.example .env
copy service-account.example.json service-account.json   # only if RAG_ENABLED=true
```

Edit `.env` (see *Required configuration* below).

### 4. Run a build

In Revit: click **AI Builder → Start Server**. A chat window opens. Type a prompt and press Send.

That's it for normal use.

### 5. (Optional) Connect an external MCP client

If you also want to drive Revit from Claude Desktop or another MCP client app, point it at `http://localhost:8001/sse`. Example Claude Desktop config:

```json
{
  "mcpServers": {
    "revit": {
      "url": "http://localhost:8001/sse"
    }
  }
}
```

The external client will see all ~45 tools listed above. Skip this step if you only use the in-Revit chat window.

---

## Required configuration

### `.env` (in `GeminiMCP.extension/`)

```
GEMINI_API_KEY=your_api_key_here          # required — from Google AI Studio
GEMINI_MODEL=gemini-2.0-flash-exp         # required — any current Gemini model id

# --- Vertex AI RAG (optional) -----------------------------------------------
RAG_ENABLED=false                          # set to true to enable code-aware retrieval
GOOGLE_CLOUD_PROJECT=your-project-id       # GCP project containing the RAG datastore
GOOGLE_CLOUD_LOCATION=global               # e.g. global, us, eu
VERTEX_DATASTORE_ID=your-data-store-id     # Discovery Engine data-store ID
```

If `RAG_ENABLED=false` (the default), everything works — the server just skips compliance lookup and uses static rules from `compliance_*.json`. With RAG disabled you can ignore the GCP fields and the service-account file entirely.

### `service-account.json` (in `GeminiMCP.extension/`) — only if `RAG_ENABLED=true`

JSON key for a Google Cloud service account with the **Discovery Engine Viewer** role on your RAG datastore.

1. [Google Cloud Console](https://console.cloud.google.com/) → IAM & Admin → Service Accounts.
2. Create a service account with the *Discovery Engine Viewer* role.
3. Create a JSON key, download it.
4. Save it as `GeminiMCP.extension/service-account.json`.

`service-account.example.json` is committed as a template — copy it, fill in your values, rename. **Never commit your real key.**

⚠️ **If you have ever committed a real key**, rotate it immediately in GCP and rewrite git history (`git filter-repo` or BFG). `.gitignore` only prevents *future* commits.

---

## Repo layout

```
revit-MCP/                              # Git repo root
├── README.md                            (you are here)
├── CLAUDE.md                            # Notes for AI coding agents working on this repo
├── ARCHITECTURE.md                      # Detailed module-by-module architecture
├── MEMORY.md                            # Project status / known issues
├── .gitignore
├── GeminiMCP.extension/                 # The pyRevit extension
│   ├── extension.json
│   ├── .env.example
│   ├── service-account.example.json
│   ├── AI Builder.tab/.../Start Server.pushbutton/script.py   # Ribbon button + chat window
│   ├── revit_mcp/                       # Core Python package — all server logic
│   ├── lib/                             # Bundled deps (no pip install required)
│   └── (ignored) .env, service-account.json
└── tests/                               # Pure-Python tests, no Revit needed
    ├── __init__.py
    └── test_*.py
```

Detailed architecture (threading model, build phases, manifest schema, prompt structure) lives in [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Where runtime files go

The server **never writes to the source tree**. All runtime state lives under `%APPDATA%\RevitMCP\`:

| Folder | Contents |
|---|---|
| `logs\` | `fastmcp_server.log`, `table_render_debug.log` |
| `cache\` | RAG chunk cache, RAG rules cache, last shell snapshot |
| `options\` | Build memory (`build_options.json`, or `build_options_<projectname>.json` per saved Revit project) |

Delete any of these to reset state.

---

## Running the tests

Pure-Python tests (no live Revit needed):

```powershell
cd path\to\revit-MCP
py -3 -m unittest tests.test_landing_shapes tests.test_staircase_logic tests.test_polygon_travel
```

`tests/__init__.py` adds `GeminiMCP.extension/` to `sys.path` so imports resolve.

---

## Limitations & known caveats

- **Single typology.** Only `commercial_office` has full preset DNA today. Other building types fall back to office defaults.
- **Singapore code bias.** Compliance defaults assume SCDF Fire Code + Singapore lift practice. Other jurisdictions need their own RAG corpus / compliance JSON.
- **No interior fitout.** Furniture, partitions, MEP runs, finishes, and lighting are out of scope.
- **No site context.** No terrain modelling, no neighbour-aware setbacks, no solar analysis.
- **No automatic rendering or material assignment.** Elements use Revit defaults.
- **Manifest size limits.** Gemini's 4000-character output budget caps how much detail the build manifest can carry. Very large or very intricate buildings may get truncated; the dispatcher retries on conflicts but won't paginate.
- **Conflict feedback is coarse.** When two core modules clash, the retry message names the zones but not their coordinates, so Gemini has limited info to fix the layout. Sometimes manual `move_staircase` / `edit_element` calls are needed.
- **One Revit document at a time.** State cache (`bridge`, `last_shell_state.json`) doesn't disambiguate across multiple open documents.
- **No undo across the full build.** Phase-level transactions are atomic but there's no global rollback if you change your mind mid-build — use *"rollback to option N"* to restore a prior saved state.
- **OR-Tools INFEASIBLE on tight courtyards.** When a footprint is near the minimum feasible size for the requested core, the constraint solver may return `None`. Workaround: enlarge the footprint or shrink the courtyard.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Ribbon button missing | Verify pyRevit sees the extension folder; restart Revit. |
| Chat window blank / errors | Check `%APPDATA%\RevitMCP\logs\fastmcp_server.log`. Most failures are import errors on first run — re-click *Start Server* to retry. |
| Port 8001 already in use | The server detects this and re-attaches to the existing instance. If that fails, free the port and click *Start Server* again. |
| "Build cancelled by user" | You clicked stop. Just send another prompt — cancellation is per-build, not per-server. |
| Gemini returns malformed JSON | The dispatcher retries up to 3 times. If it keeps failing, check `GEMINI_MODEL` — older models produce less reliable structured output. |
| RAG calls hang | Network issue or wrong datastore id. Set `RAG_ENABLED=false` to disable while you debug. |
| `ImportError: revit_mcp` when running tests | Run from the `revit-MCP/` folder, not from inside `tests/`. |
| Stale state after a failed build | Delete `%APPDATA%\RevitMCP\cache\last_shell_state.json`. |
| Build half-finished, model in weird state | Ask the chat: *"delete all AI elements"*. Or rollback to the last saved option. |

---

## License & credits

- Built on the **Model Context Protocol** ([modelcontextprotocol.io](https://modelcontextprotocol.io)).
- Compliance rules retrieval via **Google Vertex AI Search**.
- LLM: **Google Gemini**.
- Constraint solver: **Google OR-Tools** (CP-SAT).
- Revit integration via **pyRevit**.
- A large share of the heavy lifting on this codebase — architecture, refactoring, debugging, docs — was done with **Claude Code** (Anthropic) as a pair-programming collaborator.
