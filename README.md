# Revit 2026 MCP Server

AI-driven BIM automation for Autodesk Revit 2026. The extension installs as a pyRevit ribbon button. When you click the **AI Builder** button, it opens a chat window inside Revit. Type a prompt like *"30-storey commercial office, 60×80m with central courtyard"* and Google Gemini plans the building, then the extension procedurally generates every level, wall, floor, column, lift core, and fire-escape staircase in the model.

The chat window is the primary UI. The extension also exposes the same tools via the Model Context Protocol on `http://localhost:8001/sse`.

---

## Workflow

From the moment you click **AI Builder** to a fully built model in Revit:

```
   ┌──────────────────────────────────────────────────────────────┐
   │  User opens Revit  ─►  clicks AI Builder button (pyRevit)    │
   │                              (script.py)                     │
   │                              │                               │
   │                              ▼                               │
   │                       Chat window opens                      │
   │                       User types a prompt                    │
   └──────────────────────────────┬───────────────────────────────┘
                                  │
                                  ▼
                    ┌─────────────────────────┐
                    │   Main agent (Gemini)   │   extracts intent + goal,
                    │   (dispatcher.py +      │   decides how to respond
                    │    main_agent.py)       │
                    └────────────┬────────────┘
                                 │
              ┌──────────────────┴──────────────────┐
              │ runs in parallel                    │
              ▼                                     ▼
   ┌────────────────────┐               ┌────────────────────────┐
   │  Sub-agent: RAG    │               │  Pass 1                │
   │  (sub_agent.py +   │               │  Lead Architect        │
   │   vertex_rag.py)   │               │  (Gemini, agent_       │
   │                    │               │   prompts.py)          │
   │  Vertex AI Search  │               │  → footprint, levels,  │
   │  + cached rules    │               │    massing concept     │
   │  (rag_rules_       │               │                        │
   │   cache.json)      │               │                        │
   └─────────┬──────────┘               └────────────┬───────────┘
             │                                       │
             └──────────── results join ─────────────┘
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │  Pass 2                 │   merges Pass 1 + RAG rules,
                    │  Core placement         │   places lifts / stairs /
                    │  (Gemini, agent_        │   fire lobby, outputs JSON
                    │   prompts.py)           │   building manifest
                    └────────────┬────────────┘
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │  QC validation pass     │   Gemini sanity-checks
                    │  (agent_prompts.py)     │   the manifest
                    └────────────┬────────────┘
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │  Revit Transactions     │   6-phase build:
                    │  (revit_workers.py)     │   levels → circulation →
                    │                         │   shell → structure →
                    │                         │   overrides → cleanup
                    └────────────┬────────────┘
                                 │
                                 ▼
                          Built model
                       (saved as an Option
                        in build_memory.py)
```

**Step by step**

1. **Open Revit and click *AI Builder*.** pyRevit loads the extension; `script.py` opens the chat window and starts the local server.
2. **Type a prompt** — e.g. *"30-storey commercial office, 60×80m with central courtyard"*.
3. **Main agent (Gemini)** reads the prompt and figures out *what* you want — build / edit / query / authority lookup — and *how* it should respond.
4. **In parallel**, the system fans out:
   - **Sub-agent (RAG)** pulls authority code rules (SCDF Fire Code, BS EN lift standards) from Vertex AI Search, with cached copies (`rag_rules_cache.json`) for repeat queries.
   - **Pass 1 (Lead Architect)** has Gemini sketch the high-level design — footprint shape, level count, massing concept.
5. **Both results return** and are merged into the prompt for the next step.
6. **Pass 2 (Core placement)** — Gemini places the vertical-circulation core (lifts, fire stairs, fire lobby) inside the footprint and emits a single JSON **building manifest** describing every element to build.
7. **QC pass** — a quick Gemini check validates the manifest before anything touches Revit.
8. **Revit Transactions** — `revit_workers.py` opens a `TransactionGroup` and builds the model in 6 phases: levels → vertical circulation → shell (walls + floors) → structure (columns) → per-floor overrides → cleanup.
9. **Result** — the built model appears in Revit, the manifest is saved as a named **Option** so you can roll back or branch revisions later.

> The two-pass split (Pass 1 architect → Pass 2 core placement) only runs for new builds. Edits, queries, and authority-code questions take shorter paths through the same dispatcher.

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

**Authority code questions** (when `RAG_ENABLED=true`)
- *"What's the minimum stair width per SCDF Table 2.2A?"*, *"What is the requirement for fire-fighting lift lobbies?"*, *"Show me clause 2.4 from the SCDF Fire Code"*, *"How do I count Fire Access Panels for office Building?"*
- The dispatcher routes these through Vertex AI RAG, retrieves the matching chunks, and Gemini summarises them with citations.

---

## Tools available to the LLM

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

## Where runtime files go

The server **never writes to the source tree**. All runtime state lives under `%APPDATA%\RevitMCP\`:

| Folder | Contents |
|---|---|
| `logs\` | `fastmcp_server.log`, `table_render_debug.log` |
| `cache\` | RAG chunk cache, RAG rules cache, last shell snapshot |
| `options\` | Build memory (`build_options.json`, or `build_options_<projectname>.json` per saved Revit project) |

Delete any of these to reset state or clear cache in chat window.

---

## Limitations & known caveats

- **Single typology.** Only `commercial_office` has full preset DNA today. Other building types fall back to office defaults. Residential, mixed-use, retail, healthcare, etc. would need new entries in `building_presets.json` plus matching planner logic. The chat will accept the prompt and try, but the resulting design DNA (floor heights, column spans, core ratios) will be commercial-office defaults.
- **Compliance is Singapore-flavoured.** Compliance defaults assume SCDF Fire Code 2023. Other jurisdictions need their own RAG corpus / compliance JSON.
- **No interior fitout.** Furniture, partitions, MEP runs, finishes, and lighting are out of scope.
- **No site context.** No terrain modelling, no neighbour-aware setbacks, no solar analysis.
- **Manifest size limits.** Gemini's 4000-character output budget caps how much detail the build manifest can carry. Very large or very intricate buildings may get truncated; the dispatcher retries on conflicts but won't paginate.
- **Conflict feedback is coarse.** When two core modules clash, the retry message names the zones but not their coordinates, so Gemini has limited info to fix the layout. Sometimes manual `move_staircase` / `edit_element` calls are needed.
- **One Revit document at a time.** State cache (`bridge`, `last_shell_state.json`) doesn't disambiguate across multiple open documents.
- **No undo across the full build.** Phase-level transactions are atomic but there's no global rollback if you change your mind mid-build — use *"rollback to option N"* to restore a prior saved state.
- **OR-Tools INFEASIBLE on tight courtyards.** When a footprint is near the minimum feasible size for the requested core, the constraint solver may return `None`. Workaround: enlarge the footprint or shrink the courtyard.
- **No rendering, materials, or visual styling.** All elements use Revit's default types unless you call low-level tools to change them.
- **No multi-user collaboration awareness.** The state cache assumes one Revit document at a time.
- **The chat window doesn't render images.** It's a text panel; floor plans / 3D previews live in Revit's normal views.
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
