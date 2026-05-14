# -*- coding: utf-8 -*-
# NOTE: Do NOT import Autodesk.Revit.DB at module level.
# This module is loaded on the background Uvicorn thread. All DB access
# must stay INSIDE closures that run via mcp_event_handler (Revit main thread).
import json
from revit_mcp.gemini_client import client
from revit_mcp.agent_prompts import *
from revit_mcp.revit_workers import RevitWorkers
from revit_mcp.bridge import mcp_event_handler
from revit_mcp.utils import load_presets, load_compliance
from revit_mcp.build_memory import get_options_manager
from revit_mcp.cancel_manager import check_cancelled



class Orchestrator:
    # Disk-backed RAG cache key path.  Same %APPDATA%\Roaming\RevitMCP\cache
    # location used by sub_agent's chunk cache.  Persists synthesised rules
    # across server restarts so a "30-storey office" rebuild after a Revit
    # restart still hits cache and skips the ~50s RAG round-trip.
    _RAG_DISK_CACHE_FILE = "rag_rules_cache.json"

    def __init__(self):
        self.workers = None
        self.generator = None
        self._rag_cache = {}   # keyed by (building_type, storeys) → rag_rules dict
        self._load_rag_cache_from_disk()

    def _rag_cache_path(self):
        try:
            from revit_mcp.utils import get_appdata_path
            import os
            return os.path.join(get_appdata_path("cache"), self._RAG_DISK_CACHE_FILE)
        except Exception:
            return None

    def _load_rag_cache_from_disk(self):
        """Populate self._rag_cache from disk on startup.  Uses string keys
        on disk (json doesn't support tuple keys); convert back to tuples in
        memory.
        """
        try:
            import os, json as _j
            path = self._rag_cache_path()
            if not path or not os.path.isfile(path):
                return
            with open(path, "r", encoding="utf-8") as fh:
                _disk = _j.load(fh) or {}
            # Disk format: {"<type>::<storeys>": {rag_rules}}
            _restored = 0
            for k, v in _disk.items():
                try:
                    _btype, _storeys = k.split("::", 1)
                    _storeys_v = int(_storeys) if _storeys.isdigit() else _storeys
                    self._rag_cache[(_btype, _storeys_v)] = v
                    _restored += 1
                except Exception:
                    continue
            if _restored:
                client.log(f"[RAG] Restored {_restored} cached rule sets from disk: {path}")
        except Exception as _e:
            try: client.log(f"[RAG] Disk cache load failed: {_e}")
            except Exception: pass

    def _save_rag_cache_to_disk(self):
        """Write self._rag_cache to disk after a successful Vertex round-trip.
        Best-effort — failures are logged but don't break the build.
        """
        try:
            import os, json as _j
            path = self._rag_cache_path()
            if not path:
                return
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _disk = {}
            for (btype, storeys), rules in (self._rag_cache or {}).items():
                _disk[f"{btype}::{storeys}"] = rules
            with open(path, "w", encoding="utf-8") as fh:
                _j.dump(_disk, fh, indent=2, ensure_ascii=False)
        except Exception as _e:
            try: client.log(f"[RAG] Disk cache save failed: {_e}")
            except Exception: pass

    def run_full_stack(self, uiapp, user_prompt, tracker=None, history=None):
        # Do NOT access uiapp.ActiveUIDocument here (thread violation).
        # Simply pass uiapp or rely on the server's global _uiapp if preferred,
        # but here we pass it down.
        try:
            return self._orchestrate(uiapp, user_prompt, tracker, history=history)
        except RuntimeError as e:
            if "cancelled" in str(e).lower():
                self.log("Dispatcher: build cancelled by user.")
                return "Build stopped by user."
            raise

    def log(self, message):
        client.log(message)

    def _orchestrate(self, uiapp, user_prompt, tracker=None, history=None):

        # ── STEP 0: Multi-intent classification — routes everything downstream ──
        # classify_intent now returns {"intents": [...]} so multiple actions can be
        # dispatched from a single user message (e.g. "delete model and delete all options").
        history = history or []
        _recent = history[-6:] if len(history) > 6 else history
        _ctx_lines = []
        for turn in _recent:
            role = "User" if turn.get("is_user") else "Assistant"
            text = turn.get("text", "")[:300]  # trim very long turns
            _ctx_lines.append(f"{role}: {text}")
        conversation_context = "\n".join(_ctx_lines) if _ctx_lines else ""
        classified_wrapper = client.classify_intent(user_prompt, conversation_context=conversation_context)
        intents_list = (classified_wrapper or {}).get("intents", [])
        # Fallback: if classifier failed entirely, treat as unknown single intent
        if not intents_list:
            intents_list = [{"intent": None}]
        self.log("Dispatcher: classified={} → {} intent(s)".format(classified_wrapper, len(intents_list)))
        check_cancelled("after classify")

        prompt_lower = user_prompt.lower().strip()

        # ── Fast-path: if first intent is clarify or clear_chat, handle immediately ──
        first_intent = intents_list[0].get("intent") if intents_list else None
        if first_intent == "clarify":
            question = intents_list[0].get(
                "question",
                "Could you clarify what you'd like me to do? I can create or modify a building, look up authority code requirements, or answer questions about the current model."
            )
            self.log("Dispatcher: clarify intent — returning clarifying question.")
            return question

        if first_intent == "clear_chat":
            self.log("Dispatcher: clear_chat intent — advising user to clear conversation.")
            return "To clear the chat, use your AI client's built-in 'New conversation' or 'Clear chat' button. I don't have direct control over the conversation history from here."

        # ── Multi-intent loop: execute non-build intents first, collect results ──
        # Build/new_build intents are expensive — only one can run per call. If the
        # intents list contains a build alongside fast intents, run the fast ones first
        # and then fall through to the full build pipeline below.
        multi_results = []
        build_classified = None  # the build/new_build intent object, if any
        _has_build = any(it.get("intent") in ("build", "new_build") for it in intents_list)

        for it_obj in intents_list:
            it = it_obj.get("intent")

            if it == "clarify":
                # Clarify mid-list: surface the question and abort remaining intents
                multi_results.append(it_obj.get("question", "Could you clarify what you'd like me to do?"))
                break

            if it == "clear_chat":
                multi_results.append("To clear the chat, use your AI client's built-in 'New conversation' or 'Clear chat' button.")
                continue

            # Options / memory management
            opts_result, _ = self._try_intercept_options(prompt_lower, _classified=it_obj)
            if opts_result is not None:
                multi_results.append(opts_result)
                continue

            # Delete elements
            if it == "delete_elements":
                del_result = self._execute_delete(prompt_lower, classified=it_obj)
                if del_result is not None:
                    multi_results.append(del_result)
                    continue
                self.log("Dispatcher: delete_elements intent but no scope resolved — falling through to Gemini.")

            # Query
            if it == "query":
                self.log("Dispatcher: query intent — answering from BIM state.")
                multi_results.append(self._answer_query(uiapp, user_prompt, tracker, classified=it_obj))
                continue

            # Authority query
            if it == "authority_query":
                self.log("Dispatcher: authority_query intent — consulting SCDF RAG.")
                multi_results.append(self._answer_authority_query(user_prompt, tracker, history=history, classified=it_obj))
                continue

            # Build / new_build — defer to pipeline below (only first one is executed)
            if it in ("build", "new_build", None):
                if build_classified is None:
                    build_classified = it_obj
                continue

        # If all intents were fast (no build), return combined results now
        if not _has_build or build_classified is None:
            if multi_results:
                return "\n\n".join(str(r) for r in multi_results)
            # Nothing matched at all — fall through to build pipeline as a safe default
            build_classified = intents_list[0]

        # ── Build pipeline (single execution) ──
        # Prepend any fast-intent results so the user sees them alongside the build output.
        _fast_prefix = ("\n\n".join(str(r) for r in multi_results) + "\n\n") if multi_results else ""

        # Use the build intent object for temperature/thinking_budget
        classified = build_classified

        check_cancelled("before build pipeline")
        if tracker: tracker.start()
        if tracker:
            tracker.goal         = (classified or {}).get("goal", "")
            tracker.detail_level = (classified or {}).get("detail_level", "standard")
            tracker.tone         = (classified or {}).get("tone", "conversational")
        if tracker: tracker.set_status("Reading current BIM state from Revit...")
        self.log("Dispatcher: Gathering current BIM state...")

        # Load Building Presets
        presets = load_presets()
        presets_text = ""
        if presets:
            presets_text = "\nBUILDING PRESETS (DNA):\n" + json.dumps(presets, indent=2)

        # Load Authority Compliance Rules and inject into prompt
        _c_lift   = load_compliance("lift_engineering")
        _c_fire   = load_compliance("fire_safety")
        _c_struct = load_compliance("structural")
        compliance_text = ""

        # --- START RAG INTEGRATION ---
        # Only run RAG for build/new_build intents — queries, deletes, and option commands
        # don't need compliance rules.
        # Concurrency: when the build will run two-pass (new_build), Pass 1's
        # ~30s shell-only Gemini call doesn't actually need RAG content — the
        # shell decisions are geometry/typology, not compliance dimensions.  So
        # we kick RAG off in a daemon thread here and join it just before Pass 2
        # assembles its prompt.  The save_snap fast path returns nearly instant,
        # so the join is free in cached cases.  Single-pass / non-build intents
        # run RAG synchronously to preserve original behaviour.
        import time as _time
        import threading as _rag_threading
        rag_rules = None
        _stored_compliance_snapshot = None  # set when we reuse saved compliance
        _rag_future = None  # populated when we kick RAG off in a thread

        _build_intent_for_rag = (build_classified or {}).get("intent")
        _will_run_two_pass = (_build_intent_for_rag == "new_build")

        if _will_run_two_pass:
            # Concurrent path: launch the entire RAG pipeline in a background
            # thread.  Outputs are captured into _rag_holder; we resolve them
            # right before Pass 2 prompt assembly.
            _rag_holder = {"rag_rules": None, "snapshot": None, "done": _rag_threading.Event()}
            def _rag_worker():
                try:
                    _rr, _snap = self._run_rag_block(user_prompt, tracker, build_classified)
                    _rag_holder["rag_rules"] = _rr
                    _rag_holder["snapshot"] = _snap
                except Exception as _e:
                    self.log(f"[RAG] background worker crashed: {_e}")
                finally:
                    _rag_holder["done"].set()
            _rag_future = _rag_threading.Thread(target=_rag_worker, daemon=True)
            _rag_future.start()
            self.log("[RAG] launched in background (concurrent with Pass 1)")
        else:
            # Synchronous path: original behaviour.
            rag_rules, _stored_compliance_snapshot = self._run_rag_block(
                user_prompt, tracker, build_classified)
        # --- END RAG INTEGRATION ---
        # (legacy inline body extracted to Orchestrator._run_rag_block)

        # ── compliance_text builder ─────────────────────────────────────────
        # Re-callable so we can build a static-only version EARLY (before RAG
        # finishes — used for Pass 1, which doesn't need RAG content for shell
        # decisions) and a fully-merged version LATER once RAG is ready (used
        # for Pass 2 + single-pass).
        def _build_compliance_text(_rag_rules):
            if _stored_compliance_snapshot:
                return _stored_compliance_snapshot
            if not (_c_lift or _c_fire or _c_struct or _rag_rules):
                return ""
            _txt = "\nAUTHORITY COMPLIANCE RULES (MANDATORY — embed values used into manifest compliance_parameters):\n"
            if _c_lift:
                _txt += "## Lift Engineering — BS EN 81-20 / CIBSE Guide D:\n"
                _txt += json.dumps(_c_lift, indent=2) + "\n"
            # Merge dynamic RAG fire rules ON TOP of static file.
            # Static file supplies the known dimension values (riser, tread, flight width, etc.)
            # that the SCDF PDF table data may not be directly retrievable by Vertex.
            # RAG supplies specific clause numbers and any additional/more-specific rules.
            if _rag_rules or _c_fire:
                _txt += f"## Fire Safety ({_rag_rules.get('authority', 'SCDF') if _rag_rules else 'BS EN 81-72 / BS 9999'}):\n"
                merged = json.loads(json.dumps(_c_fire)) if _c_fire else {}
                if _rag_rules:
                    # Keys in these topics MUST come from RAG — always overwrite static values.
                    _RAG_AUTHORITATIVE_TOPICS = {
                        "staircase":        {"max_travel_distance_mm", "max_travel_distance_sprinklered_mm",
                                             "min_flight_width_mm", "min_landing_width_mm", "min_count"},
                        "fire_lift_lobby":  {"min_area_mm2", "min_width_mm", "min_depth_mm"},
                        "smoke_stop_lobby": {"min_area_mm2", "min_width_mm", "min_clear_depth_mm"},
                        "occupant_load":    {"occupant_load_factor_m2"},
                        "exit_width":       {"persons_per_unit_width", "exit_width_per_unit_mm"},
                        "corridor":         {"min_corridor_width_mm"},
                    }
                    for topic, vals in _rag_rules.get("rules", {}).items():
                        if topic not in merged:
                            merged[topic] = {}
                        authoritative_keys = _RAG_AUTHORITATIVE_TOPICS.get(topic, set())
                        for k, v in vals.items():
                            if k == "source":
                                merged[topic]["_source"] = v
                            elif isinstance(v, dict) and "dimension" in v:
                                dim = v["dimension"]
                                clause = v.get("clause")
                                if k in authoritative_keys or merged[topic].get(k) is None:
                                    merged[topic][k] = dim
                                if clause:
                                    merged[topic][k + "__clause"] = clause
                            else:
                                merged[topic][k] = v
                _txt += json.dumps(merged, indent=2) + "\n"
            if _c_struct:
                _txt += "## Structural — Wall Thicknesses:\n"
                _txt += json.dumps(_c_struct, indent=2) + "\n"
            return _txt

        compliance_text = _build_compliance_text(rag_rules)

        def gather_state():
            import Autodesk.Revit.DB as DB # type: ignore
            doc = uiapp.ActiveUIDocument.Document
            from revit_mcp.building_generator import get_model_registry # type: ignore
            registry = get_model_registry(doc)
            
            levels = []
            for l in DB.FilteredElementCollector(doc).OfClass(DB.Level):
                name = l.Name
                if name.startswith("AI Level") or name.startswith("AI_Level"):
                    levels.append(l)
            levels.sort(key=lambda x: x.Elevation)
            count = len(levels)
            
            from Autodesk.Revit.DB import UnitUtils, UnitTypeId # type: ignore
            height_str = ""
            overrides_str = ""
            
            if count >= 2:
                for i in range(count - 1):
                    diff_ft = levels[i+1].Elevation - levels[i].Elevation
                    h_mm = UnitUtils.ConvertFromInternalUnits(diff_ft, UnitTypeId.Millimeters)
                    height_str += f"L{i+1}:{h_mm:.0f} "
                    
                    # Also detect footprint overrides from walls
                    w_tag, l_tag = f"AI_Wall_L{i+1}_S", f"AI_Wall_L{i+1}_W"
                    if w_tag in registry and l_tag in registry:
                        w_wall, l_wall = doc.GetElement(registry[w_tag]), doc.GetElement(registry[l_tag])
                        if w_wall and l_wall and hasattr(w_wall.Location, "Curve") and hasattr(l_wall.Location, "Curve"):
                            w_mm = UnitUtils.ConvertFromInternalUnits(w_wall.Location.Curve.Length, UnitTypeId.Millimeters)
                            l_mm = UnitUtils.ConvertFromInternalUnits(l_wall.Location.Curve.Length, UnitTypeId.Millimeters)
                            overrides_str += f"L{i+1}:{w_mm:.0f}x{l_mm:.0f} "
            
            # Comprehensive Stats per Level
            per_floor_stats = {}
            for lvl in levels:
                l_id = lvl.Id
                level_filter = DB.ElementLevelFilter(l_id)
                
                def count_cat(category_bit):
                    return DB.FilteredElementCollector(doc).OfCategory(category_bit).WhereElementIsNotElementType().WherePasses(level_filter).GetElementCount()
                
                per_floor_stats[lvl.Name] = {
                    "walls": DB.FilteredElementCollector(doc).OfClass(DB.Wall).WherePasses(level_filter).GetElementCount(),
                    "floors": DB.FilteredElementCollector(doc).OfClass(DB.Floor).WherePasses(level_filter).GetElementCount(),
                    "doors": count_cat(DB.BuiltInCategory.OST_Doors),
                    "windows": count_cat(DB.BuiltInCategory.OST_Windows),
                    "columns": count_cat(DB.BuiltInCategory.OST_Columns) + count_cat(DB.BuiltInCategory.OST_StructuralColumns),
                }

            # Global Totals
            wall_count = DB.FilteredElementCollector(doc).OfClass(DB.Wall).GetElementCount()
            floor_count = DB.FilteredElementCollector(doc).OfClass(DB.Floor).GetElementCount()
            door_count = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Doors).WhereElementIsNotElementType().GetElementCount()
            win_count = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Windows).WhereElementIsNotElementType().GetElementCount()
            col_count = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Columns).WhereElementIsNotElementType().GetElementCount()
            scol_count = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_StructuralColumns).WhereElementIsNotElementType().GetElementCount()
            
            # Measure Column Span (preserving existing fix)
            col_span_mm = 10000 # Default fallback (10m)
            ai_cols = []
            col_collector = DB.FilteredElementCollector(doc).WhereElementIsNotElementType()
            from System.Collections.Generic import List
            cat_list = List[DB.BuiltInCategory]()
            cat_list.Add(DB.BuiltInCategory.OST_Columns)
            cat_list.Add(DB.BuiltInCategory.OST_StructuralColumns)
            col_collector.WherePasses(DB.ElementMulticategoryFilter(cat_list))
            
            for el in col_collector:
                p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                if not p: p = el.LookupParameter("Comments")
                if p and p.HasValue and p.AsString() and "AI_Col_L1_" in p.AsString():
                    ai_cols.append(el)
            
            if len(ai_cols) >= 2:
                pts = [c.Location.Point for c in ai_cols]
                pts.sort(key=lambda p: (round(p.X, 2), round(p.Y, 2)))
                for i in range(len(pts)-1):
                    p1, p2 = pts[i], pts[i+1]
                    dist_ft = p1.DistanceTo(p2)
                    if (abs(p1.X - p2.X) < 0.1 or abs(p1.Y - p2.Y) < 0.1) and dist_ft > 1.0:
                        col_span_mm = round(dist_ft * 304.8 / 100.0) * 100.0
                        break

            stats = {
                "levels": count,
                "total_stats": {
                    "walls": wall_count,
                    "floors": floor_count,
                    "doors": door_count,
                    "windows": win_count,
                    "columns": col_count + scol_count
                },
                "per_floor_breakdown": per_floor_stats,
                "current_column_span": col_span_mm
            }
            
            return count, height_str, overrides_str, stats

        # OPTIMIZATION: Cache BIM state with 120s TTL (30s was too aggressive)
        import time
        now = time.time()
        refresh_needed = True

        if hasattr(self, "_cached_state") and hasattr(self, "_cache_time"):
            age = now - self._cache_time
            force_refresh = any(x in user_prompt.lower() for x in ["create", "delete", "clear", "wipe"])
            if age < 120.0 and not force_refresh:
                refresh_needed = False
        
        if not refresh_needed:
            self.log("[{}] Dispatcher: Using cached BIM state (Age: {:.1f}s)".format(time.time(), now - self._cache_time))
            state_text = self._cached_state
        else:
            try:
                self.log("[{}] Dispatcher: Gathering fresh BIM state from Revit...".format(time.time()))
                cur_levels, cur_heights, cur_overrides, cur_stats = mcp_event_handler.run_on_main_thread(gather_state)
                self.log("BIM state gathered: {} levels found.".format(cur_levels))
                storeys = max(1, cur_levels - 1)
                state_text = f"CURRENT BIM STATE: {storeys} storeys. "
                if cur_heights: state_text += f"\nEXISTING HEIGHTS: {cur_heights}"
                if cur_overrides: state_text += f"\nEXISTING OVERRIDES: {cur_overrides}"
                if cur_stats:
                    state_text += f"\nPROJECT TOTALS: {json.dumps(cur_stats['total_stats'])}"
                    state_text += f"\nPER-FLOOR BREAKDOWN: {json.dumps(cur_stats['per_floor_breakdown'])}"
                    state_text += f"\nDETECTED COLUMN SPAN: {cur_stats['current_column_span']}mm"
                # Load persisted shell parametric state (shape, footprint_scale_overrides, etc.)
                # Skip if the user is creating a brand-new building — shell memory must not bleed across builds.
                _new_build_keywords = ["create", "build", "generate", "new building", "from scratch", "make a", "make me a"]
                _is_new_build = any(kw in user_prompt.lower() for kw in _new_build_keywords)
                # Allow the intent classifier to override shell-memory suppression.
                # When the classifier signals fresh_build=true it means Gemini read the
                # conversation context and determined the user wants a completely new form
                # (e.g. "try again" after a failed S-shape attempt).
                if not _is_new_build and (classified or {}).get("fresh_build"):
                    _is_new_build = True
                    self.log("Dispatcher: fresh_build=true from classifier — skipping shell memory.")
                try:
                    import os as _os
                    from revit_mcp.utils import get_appdata_path
                    _shell_path = _os.path.join(get_appdata_path("cache"), "last_shell_state.json")
                    if not _is_new_build and _os.path.exists(_shell_path):
                        with open(_shell_path) as _f:
                            _saved_shell = json.load(_f)
                        if _saved_shell:
                            state_text += f"\nEXISTING SHELL PARAMETERS: {json.dumps(_saved_shell)}"
                            state_text += (
                                "\nCRITICAL — SHELL MEMORY: The EXISTING SHELL PARAMETERS above define "
                                "the current building shape and per-floor scale pattern. You MUST carry "
                                "these values forward into your manifest unchanged UNLESS the user "
                                "explicitly asks to modify them. In particular, preserve 'shape', "
                                "'footprint_svg' (the organic form descriptor — copy it verbatim), "
                                "'footprint_scale_overrides' (extending or merging for new floors), "
                                "'width', and 'length'."
                            )
                    elif _is_new_build:
                        self.log("Dispatcher: New-build detected — skipping shell memory injection.")
                except Exception as _le:
                    self.log(f"Shell state load warning: {_le}")
                state_text += f"\nCRITICAL: Refer to PER-FLOOR BREAKDOWN for detailed queries. Preserve existing state unless asked to change."
                self._cached_state = state_text
                self._cache_time = now
            except Exception as e:
                self.log(f"Error gathering state: {e}")
                state_text = ""

        # 1. Generate Master Manifest (Fast-Track) with Agentic Loop
        check_cancelled("after BIM state gather")
        # Temperature + thinking_budget come from classify_intent — set per prompt, not hardcoded.
        temperature = float((classified or {}).get("temperature", 0.4))
        thinking_budget = int((classified or {}).get("thinking_budget", 16384))
        # Pass 2 (core placement) doesn't need the same reasoning budget as Pass 1
        # (shell architecture). Pass 2 is constraint-satisfaction — bank position
        # within an engine-computed valid range, cluster sides per directive.
        # Most of the architectural exploration already happened in Pass 1.
        # Cap Pass 2 at 4096 thinking tokens (was inheriting 16384 from new_build
        # classification). Saves ~20-30s per Pass 2 call. Conflict-retry attempts
        # also benefit since they go through the same Pass 2 path.
        pass2_thinking_budget = min(thinking_budget, 4096)
        self.log("Step 2: Requesting building plan from Gemini AI (model: {}, thinking_budget: {} pass1 / {} pass2, temperature: {})".format(
            client.model, thinking_budget, pass2_thinking_budget, temperature))
        creativity_label = "creative" if temperature >= 0.8 else ("balanced" if temperature >= 0.4 else "precise")
        self.log(f"Sending to Gemini AI ({client.model}) — {creativity_label} mode (temp={temperature})...")
        if tracker: tracker.set_status("Main agent: generating building manifest ({} mode)...".format(creativity_label))

        def _generate_with_heartbeat(prompt, phase_label, attempt_label="", thinking_budget_override=None):
            """Call client.generate_content() with a background thread that updates the
            tracker status every 10s so the user sees live elapsed time instead of silence.

            thinking_budget_override: when set, uses this budget instead of the
            classify_intent value. Pass 2 calls pass `pass2_thinking_budget` here.
            """
            import threading as _threading
            import time as _hb_time
            _result_holder = [None]
            _error_holder  = [None]
            _start = _hb_time.time()
            _budget = thinking_budget_override if thinking_budget_override is not None else thinking_budget

            def _worker():
                try:
                    _result_holder[0] = client.generate_content(
                        prompt, thinking_budget=_budget, temperature=temperature)
                except Exception as _e:
                    _error_holder[0] = _e

            _t = _threading.Thread(target=_worker, daemon=True)
            _t.start()
            _interval = 10
            while _t.is_alive():
                _t.join(timeout=_interval)
                if _t.is_alive():
                    _elapsed = int(_hb_time.time() - _start)
                    self.log("{}{} — waiting for Gemini ({:d}s elapsed)...".format(
                        phase_label, attempt_label, _elapsed))
                    if tracker:
                        tracker.set_status("{}{} — waiting for Gemini ({:d}s elapsed)...".format(
                            phase_label, attempt_label, _elapsed))
            if _error_holder[0] is not None:
                raise _error_holder[0]
            return _result_holder[0]

        # Build conversation history block for the main prompt.
        # Always include when there is history — Gemini uses it to:
        #   (a) understand what was previously attempted and what went wrong
        #   (b) avoid repeating mistakes from earlier turns
        #   (c) understand "try again" / "redo" in the context of a specific prior intent
        history_block = ""
        if history:
            _recent = history[-8:] if len(history) > 8 else history
            _lines = []
            for turn in _recent:
                role = "User" if turn.get("is_user") else "Assistant"
                text = (turn.get("text") or "")[:400]
                _lines.append(f"{role}: {text}")
            if _lines:
                history_block = (
                    "\n\n## CONVERSATION HISTORY (most recent first — use to understand intent and avoid repeating past mistakes)\n"
                    + "\n".join(reversed(_lines))
                    + "\n## END HISTORY\n"
                )

        _inferred_goal = (classified or {}).get("goal", "")
        _goal_block = ""
        if _inferred_goal:
            _goal_block = (
                "\n\n## INFERRED USER GOAL\n"
                "{}\n"
                "Let this goal inform your architectural decisions and the voice of your "
                "<architectural_intent> — speak directly to what the user is trying to achieve, "
                "not just what the manifest contains.\n"
            ).format(_inferred_goal)
        current_prompt = DISPATCHER_PROMPT + presets_text + compliance_text + "\n" + state_text + history_block + _goal_block + "\nUser Request: " + user_prompt

        # Save user goal for Pass 2 and all retry conflict blocks
        _user_goal = user_prompt
        self._last_user_goal = _user_goal

        # ── Two-pass branch: ALWAYS used for new_build intents ───────────────
        # Pass 1 generates the shell-only manifest; the engine runs OR-Tools
        # pre-analysis; Pass 2 adds core placement with computed position constraints.
        # This ensures Gemini always receives a FLOOR PLATE ANALYSIS + VALID
        # LIFTS.POSITION RANGE before placing the core — for both simple rectangular
        # buildings and complex footprints (L/U/H/courtyard etc.).
        _intent_for_twopass = (classified or {}).get("intent", "") if classified else ""
        _fp_complexity = (classified or {}).get("footprint_complexity", "simple")
        _run_two_pass = (_intent_for_twopass == "new_build")

        if _run_two_pass:
            try:
                from revit_mcp.agent_prompts import SHELL_ONLY_SYSTEM_PROMPT, CORE_PLACEMENT_SYSTEM_PROMPT
                from revit_mcp.revit_workers import pre_analyse_floor_plate
                self.log("[TwoPass] new_build intent (footprint_complexity={}) — running Pass 1 (shell only).".format(_fp_complexity))
                _pass1_prompt = (SHELL_ONLY_SYSTEM_PROMPT + "\n" + presets_text + compliance_text
                                 + "\n" + state_text + history_block
                                 + "\nUser Request: " + user_prompt)
                if tracker: tracker.set_status("Pass 1: generating floor plate geometry (shell only)...")
                _pass1_json = _generate_with_heartbeat(_pass1_prompt, "Pass 1: generating floor plate geometry")
                _pass1_manifest = None
                try:
                    import json as _p1json
                    _p1_block = _pass1_json
                    import re as _p1re
                    _p1_match = _p1re.search(r"```(?:json)?\s*(\{.*?\})\s*```", _p1_block, _p1re.DOTALL)
                    if _p1_match:
                        _pass1_manifest = _p1json.loads(_p1_match.group(1))
                    else:
                        _pass1_manifest = _p1json.loads(_p1_block)
                except Exception as _p1err:
                    self.log("[TwoPass] Pass 1 JSON parse failed: {} — falling back to single-pass.".format(_p1err))

                # Retry Pass 1 once when response had no JSON at all (Gemini wrote prose instead)
                if _pass1_manifest is None and "{" not in (_pass1_json or ""):
                    self.log("[TwoPass] Pass 1 no-JSON response — retrying with OUTPUT-ONLY instruction.")
                    _p1_retry_prompt = _pass1_prompt + (
                        "\n\n[SYSTEM: Your previous response contained no JSON manifest block. "
                        "OUTPUT THE SHELL-ONLY JSON MANIFEST NOW. "
                        "Start your response with ```json and end with ```. "
                        "No reasoning text. No explanation. Only the JSON manifest.]\n"
                    )
                    _p1_retry_raw = _generate_with_heartbeat(_p1_retry_prompt, "Pass 1 retry: shell geometry")
                    try:
                        import json as _p1rjson, re as _p1rre
                        _p1r_match = _p1rre.search(r"```(?:json)?\s*(\{.*?\})\s*```", _p1_retry_raw or "", _p1rre.DOTALL)
                        if _p1r_match:
                            _pass1_manifest = _p1rjson.loads(_p1r_match.group(1))
                        elif _p1_retry_raw:
                            _pass1_manifest = _p1rjson.loads(_p1_retry_raw)
                    except Exception as _p1rerr:
                        self.log("[TwoPass] Pass 1 retry also failed: {} — falling back to single-pass.".format(_p1rerr))

                if _pass1_manifest:
                    self.log("[TwoPass] Pass 1 succeeded — running engine core analysis.")
                    if tracker: tracker.set_status("Pass 1 complete — analysing floor plate for core placement...")
                    _cp_for_analysis = _pass1_manifest.get("compliance_parameters", {})
                    _analysis = pre_analyse_floor_plate(_pass1_manifest, compliance_overrides=_cp_for_analysis, user_goal=_user_goal)
                    # ── DIAG: log key floor-plate analysis numbers ──────────────
                    _fc_diag = _analysis.get("fire_cluster_elements", {})
                    _fp_diag = _analysis.get("floor_plate", {})
                    self.log("[TwoPass][DIAG] floor_plate analysis:"
                             " bank_depth={}mm bank_count={}"
                             " cluster_chain_depth={}mm"
                             " fp_outer={}".format(
                        _fp_diag.get("passenger_bank_total_depth_mm","?"),
                        _fp_diag.get("num_banks","?"),
                        _fc_diag.get("cluster_assembly_depth_mm","?"),
                        _analysis.get("outer_bbox_mm","?"),
                    ))
                    _void_diag = _analysis.get("void_bounds_mm")
                    if _void_diag:
                        self.log("[TwoPass][DIAG] void_bounds_mm={}".format(_void_diag))
                    _guide_diag = _analysis.get("placement_guide_bands")
                    if _guide_diag:
                        self.log("[TwoPass][DIAG] placement_guide_bands={}".format(_guide_diag))
                    # ────────────────────────────────────────────────────────────
                    # Concurrent RAG: if we kicked it off in a thread above,
                    # await it now (just before Pass 2 needs the merged compliance
                    # text) and rebuild compliance_text with the freshly-merged
                    # rules.  Pass 1 used the static-only compliance, which was
                    # fine because Pass 1 doesn't make decisions on RAG values.
                    if _rag_future is not None:
                        _t_join = _time.time()
                        if tracker: tracker.set_status("Awaiting authority code retrieval...")
                        # Generous deadline matching run_retrieve_rules' own 150s ceiling.
                        _rag_holder["done"].wait(timeout=160)
                        rag_rules = _rag_holder.get("rag_rules")
                        _stored_compliance_snapshot = _rag_holder.get("snapshot")
                        compliance_text = _build_compliance_text(rag_rules)
                        self.log("[RAG] joined background worker in {:.2f}s "
                                 "(rag_rules={}, snapshot={})".format(
                            _time.time() - _t_join,
                            "present" if rag_rules else "None",
                            "present" if _stored_compliance_snapshot else "None"))
                    _analysis_block = self._format_floor_plate_analysis(_analysis)
                    _shell_block = "\n\n## PASS 1 SHELL MANIFEST (do not change shell — add core placement only)\n```json\n{}\n```\n".format(
                        __import__("json").dumps(_pass1_manifest, indent=2))
                    current_prompt = (
                        CORE_PLACEMENT_SYSTEM_PROMPT
                        + "\n\n## USER GOAL (do not compromise under any circumstances)\n" + _user_goal
                        + "\n" + presets_text + compliance_text
                        + _shell_block
                        + "\n\n" + _analysis_block
                        + "\n\nNow produce the complete final manifest with core placement."
                    )
                    self.log("[TwoPass] Pass 2 prompt assembled ({} chars). Proceeding to retry loop.".format(len(current_prompt)))
                else:
                    self.log("[TwoPass] Pass 1 produced no manifest — falling back to single-pass.")
            except Exception as _tp_err:
                self.log("[TwoPass] Two-pass setup error: {} — falling back to single-pass.".format(_tp_err))

        # If RAG was launched concurrently and we never joined (either Pass 1
        # produced no manifest, OR an exception aborted the two-pass block), do
        # the join now so single-pass / retry-loop sees the merged compliance
        # instead of the static-only Pass-1 placeholder.
        if _rag_future is not None and not _rag_holder["done"].is_set():
            _t_join_fb = _time.time()
            if tracker: tracker.set_status("Awaiting authority code retrieval (fallback)...")
            _rag_holder["done"].wait(timeout=160)
            rag_rules = _rag_holder.get("rag_rules")
            _stored_compliance_snapshot = _rag_holder.get("snapshot")
            compliance_text = _build_compliance_text(rag_rules)
            current_prompt = (DISPATCHER_PROMPT + presets_text + compliance_text
                              + "\n" + state_text + history_block + _goal_block
                              + "\nUser Request: " + user_prompt)
            self.log("[RAG] joined background worker on two-pass fallback in "
                     "{:.2f}s; rebuilt single-pass current_prompt".format(
                         _time.time() - _t_join_fb))

        max_attempts = 3
        intent_text = None  # Captured from Gemini's <architectural_intent> for build_memory naming

        # Log the assembled prompt sections so the log shows exactly what the AI receives
        rag_source = "dynamic RAG ({})".format(rag_rules.get("authority", "?")) if rag_rules else ("saved snapshot" if _stored_compliance_snapshot else "static files")
        self.log(
            "=== PROMPT SECTIONS SENT TO AI ===\n"
            "[1] SYSTEM INSTRUCTION: {} chars\n"
            "[2] PRESETS: {} chars\n"
            "[3] COMPLIANCE ({}): {} chars — full content in dispatcher.py build_memory / RAG snapshot\n"
            "[4] BIM STATE:\n{}\n"
            "[5] HISTORY: {} turns\n"
            "[6] USER REQUEST: {}\n"
            "=== END PROMPT SECTIONS ===".format(
                len(DISPATCHER_PROMPT),
                len(presets_text),
                rag_source,
                len(compliance_text),
                state_text,
                len(_recent) if history else 0,
                user_prompt,
            )
        )

        for attempt in range(max_attempts):
            self.log(f"--- Orchestration Attempt {attempt + 1}/{max_attempts} ---")
            check_cancelled("attempt {}".format(attempt + 1))

            ai_start = time.time()
            _pass_label = "Pass 2: placing core" if _run_two_pass else "Generating manifest"
            _attempt_label = " (attempt {}/{})".format(attempt + 1, max_attempts) if max_attempts > 1 else ""
            if tracker: tracker.set_status("{}{} — sending to Gemini...".format(_pass_label, _attempt_label))
            # Pass 2 is constraint-satisfaction over engine-computed ranges, so it
            # uses a tighter thinking budget than Pass 1's architectural reasoning.
            # Single-pass builds (no Pass 1) keep the full classify_intent budget.
            _attempt_budget = pass2_thinking_budget if _run_two_pass else None
            manifest_json = _generate_with_heartbeat(
                current_prompt, _pass_label, _attempt_label,
                thinking_budget_override=_attempt_budget)
            ai_duration = time.time() - ai_start

            self.log("Manifest received from AI. (Time: {:.2f}s). Parsing...".format(ai_duration))
            if tracker: tracker.set_status("Manifest received in {:.0f}s — parsing building design...".format(ai_duration))

            # Stream Intent and Resolution Thoughts to UI; capture intent for naming
            intent_text = self._stream_narrative_to_user(manifest_json, tracker)
            if intent_text:
                self.log("Dispatcher: architectural_intent={}".format(repr(intent_text)))
            
            try:
                self.log("_orchestrate: manifest_json head={}".format(repr(manifest_json[:300])))
                manifest_str = self._extract_json(manifest_json)
                manifest = json.loads(manifest_str)
                
                # UNWRAP AI TOOL-CALL-STYLE WRAPPERS
                for wrapper in ["orchestrate_build", "edit_entire_building_dimensions"]:
                    if wrapper in manifest and len(manifest) == 1:
                        manifest = manifest[wrapper]
                        break
                
                # CHECK FOR QUERY RESPONSE (Natural Language only)
                if "response" in manifest and not any(k in manifest for k in ["project_setup", "levels", "shell"]):
                    self.log("Dispatcher: AI detected a QUESTION. Returning natural language response.")
                    return str(manifest["response"])

                # Log full manifest to runner/console for debugging
                self.log("=== BUILDING MANIFEST (compliance source: {}) ===\n{}\n=== END MANIFEST ===".format(
                    rag_source, json.dumps(manifest, indent=2)))
                # ── DIAG: log Gemini's core placement decision ──────────────────
                _lft = manifest.get("lifts", {})
                _sh_diag = manifest.get("shell", {})
                self.log("[DIAG][Gemini→Core] shell: width={}mm length={}mm".format(
                    _sh_diag.get("width","?"), _sh_diag.get("length","?")))
                self.log("[DIAG][Gemini→Core] lifts: count={} orientation={} position={} banks={}".format(
                    _lft.get("count","?"), _lft.get("orientation","?"),
                    _lft.get("position","?"), _lft.get("banks","(none)")))
                # ────────────────────────────────────────────────────────────────

                # Show manifest shell as a transient status (it will be superseded by build phases)
                if tracker:
                    _s2 = manifest.get("project_setup", {})
                    _sh2 = manifest.get("shell", {})
                    _typology = manifest.get("typology", "")
                    _desc = "{} storeys, {}mm × {}mm, {} lifts, {} stairs".format(
                        _s2.get("levels", "?"),
                        _sh2.get("width", "?"), _sh2.get("length", "?"),
                        manifest.get("lifts", {}).get("count", "?"),
                        manifest.get("staircases", {}).get("count", "?"),
                    )
                    tracker.set_status("Manifest: {} — {}".format(_typology or "building", _desc))

                # EXECUTE BUILD (Validate/Build)
                if tracker:
                    _s = manifest.get("project_setup", {})
                    _sh = manifest.get("shell", {})
                    _lvls = _s.get("levels", "?")
                    _w = _sh.get("width", 0)
                    _l = _sh.get("length", 0)
                    _fp = "{}m × {}m".format(int(_w/1000), int(_l/1000)) if _w and _l else "?"
                    _lifts = manifest.get("lifts", {}).get("count", "?")
                    _stairs = manifest.get("staircases", {}).get("count", "?")
                    tracker.set_status("Calling Revit API — building {} levels, footprint {}, {} lifts, {} staircases...".format(
                        _lvls, _fp, _lifts, _stairs))
                    tracker._last_manifest = manifest  # store for final report

                def main_action():
                    import Autodesk.Revit.DB as DB # type: ignore
                    doc = uiapp.ActiveUIDocument.Document
                    workers = RevitWorkers(doc, tracker=tracker)
                    return workers.execute_fast_manifest(manifest)

                check_cancelled("before Revit execution")
                self.log(f"Attempting build execution for Attempt {attempt + 1}...")
                results = mcp_event_handler.run_on_main_thread(main_action)
                
                # CHECK FOR CONFLICTS
                if isinstance(results, dict) and results.get("status") == "CONFLICT":
                    conflict_desc = results.get("description", "Unknown Spatial Conflict")
                    _cd_safe = conflict_desc.encode("ascii", "replace").decode("ascii")
                    # ── DIAG: log full conflict description ──────────────────────
                    self.log("[DIAG][CONFLICT] Attempt {} full description:\n{}".format(
                        attempt+1, _cd_safe))
                    # ────────────────────────────────────────────────────────────
                    if tracker:
                        tracker.report(f"### [Validation Failed] Attempt {attempt+1}\n{conflict_desc}")

                    if attempt < max_attempts - 1:
                        # Build a structured, information-rich retry block
                        _mi = results.get("manifest_inputs", {})
                        _layout = results.get("core_layout_summary", "")
                        _hints = results.get("resolution_hints", [])
                        _core_mm = results.get("core_total_mm", {})
                        _fd_list = results.get("ortools_failure_details", [])

                        _conflict_block = "[SPATIAL CONFLICT — ATTEMPT {} FAILED]\n\n".format(attempt + 1)
                        if getattr(self, "_last_user_goal", ""):
                            _conflict_block += "USER GOAL (do not compromise): {}\n\n".format(self._last_user_goal)
                        _conflict_block += "Engine reported: {}\n\n".format(conflict_desc)

                        if _layout:
                            _conflict_block += (
                                "Core zones computed by the engine for this attempt:\n"
                                "{}\n"
                                "  {:<30s}  {:>6} x {:>5} mm  (total core envelope)\n\n"
                            ).format(
                                _layout,
                                "— TOTAL CORE —",
                                _core_mm.get("width", "?"),
                                _core_mm.get("depth", "?"),
                            )

                        # Per-bank failure details: exact bank position, clearance vs. needed
                        if _fd_list:
                            _conflict_block += "Per-bank OR-Tools failure details:\n"
                            for _fd in _fd_list:
                                _bp = _fd.get("bank_pos_mm", ["?", "?"])
                                _ts = _fd.get("tried_side", "?")
                                _dn = _fd.get("cluster_d_needed", "?")
                                _ca = _fd.get("clearance_avail")
                                _vr = _fd.get("valid_range", "")
                                _conflict_block += (
                                    "  Bank set {}: position [{}, {}] mm\n"
                                    "    Tried {} face — cluster needs {}mm clearance, "
                                    "{}mm available.\n"
                                ).format(
                                    _fd.get("set_index", "?"),
                                    _bp[0], _bp[1], _ts, _dn,
                                    _ca if _ca is not None else "unknown",
                                )
                                if _vr:
                                    _conflict_block += "    Fix: {}\n".format(_vr)
                            _conflict_block += "\n"

                        if _mi:
                            _conflict_block += (
                                "Manifest values that drove this layout:\n"
                                "  lifts.count        = {}\n"
                                "  shell.width        = {:,} mm\n"
                                "  shell.length       = {:,} mm\n"
                                "  level_height       = {:,} mm\n\n"
                            ).format(
                                _mi.get("lift_count", "?"),
                                _mi.get("shell_width_mm", 0),
                                _mi.get("shell_length_mm", 0),
                                _mi.get("level_height_mm", 0),
                            )

                        if _hints:
                            _conflict_block += "Engine diagnosis:\n"
                            for _h in _hints:
                                _conflict_block += "  • {}\n".format(_h)
                            _conflict_block += "\n"

                        _all_bands_inf = results.get("all_bands_infeasible", False)
                        _min_bldg_mm   = results.get("min_building_mm", 0)
                        if _all_bands_inf and _min_bldg_mm:
                            _conflict_block += (
                                "BUILDING TOO SMALL — repositioning the core will NOT fix this.\n"
                                "You MUST choose ONE of:\n"
                                "  (a) Increase shell.length OR shell.width to ≥ {} mm, OR\n"
                                "  (b) Reduce lifts.count to decrease chain depth.\n"
                                "The floor plate geometry and void are in the PLACEMENT GUIDE above — "
                                "verify your new dimensions satisfy the band constraints before generating the manifest.\n\n"
                                "In <resolution_thoughts>, write ONE sentence naming the specific change — "
                                "e.g. 'Increased shell.length from 60000mm to {}mm so the south/north band is wide enough for the core chain.' "
                                "Then generate the corrected manifest."
                            ).format(_min_bldg_mm, _min_bldg_mm)
                        else:
                            _conflict_block += (
                                "You are the Lead Architect. The engine has given you its diagnosis above — "
                                "use it as a starting point, but you are not bound to follow it mechanically. "
                                "If you see a better solution (different typology, different core arrangement, "
                                "rethinking the floor count or shape), use your architectural judgement. "
                                "The only hard rules are: no two core zones may overlap, and all code-minimum "
                                "dimensions must be satisfied.\n\n"
                                "In <resolution_thoughts>, write ONE sentence naming the specific change you made "
                                "and your reasoning — e.g. 'Reduced lift count from 8 to 6 to shrink core width "
                                "from 9200mm to 7400mm, fitting within the 25000mm shell.' "
                                "Then generate the corrected manifest."
                            )

                        current_prompt += "\n\n" + _conflict_block
                        self.log(f"Retry prompt conflict block ({len(_conflict_block)} chars) appended for attempt {attempt+2}.")
                        continue
                    else:
                        self.log("Reached maximum orchestration attempts.")
                        return f"Failed to build after {max_attempts} attempts due to structural/spatial conflicts: {conflict_desc}"
                
                # Bail out if the workers returned a hard error (not a spatial conflict)
                if isinstance(results, dict) and results.get("error"):
                    err_msg = results.get("error", "Unknown build error")
                    self.log("Dispatcher: Build error — skipping memory save. Error: {}".format(err_msg))
                    if tracker:
                        return tracker.generate_final_report(base_summary="Build failed: {}".format(err_msg))
                    return "Build failed: {}".format(err_msg)

                # SUCCESS: invalidate BIM state cache so next prompt picks up the new shell
                self._cache_time = 0

                # Save to build memory (new option or revision based on current context)
                try:
                    build_duration = time.time() - ai_start
                    mgr = get_options_manager()
                    mgr._ensure_loaded()
                    manifest_to_save = dict(manifest)
                    if "shell" in manifest_to_save:
                        shell_copy = dict(manifest_to_save["shell"])
                        shell_copy.pop("footprint_points", None)  # always auto-derived, never store
                        shell_copy.pop("footprint_svg", None)      # stored in shell memory separately
                        manifest_to_save["shell"] = shell_copy
                    explicit_scratch = any(kw in prompt_lower for kw in ["from scratch", "brand new", "start over"])
                    cur_opt_id = mgr._data.get("current_option_id")
                    is_new_option = explicit_scratch or cur_opt_id is None

                    if not is_new_option:
                        # Compute diff against the current option's base or latest revision
                        cur_opt = mgr._find_option(cur_opt_id)
                        if cur_opt is not None:
                            parent_manifest = cur_opt["revisions"][-1]["manifest"] if cur_opt["revisions"] else cur_opt["manifest"]
                            diff = mgr.compute_diff_summary(parent_manifest, manifest_to_save)
                            if mgr.is_major_change(diff):
                                self.log("Dispatcher: Major change detected ({}) — saving as new option".format(
                                    diff.get("changed_keys")))
                                is_new_option = True

                    if is_new_option:
                        saved = mgr.save_new_option(manifest_to_save, intent_text=intent_text, duration_s=round(build_duration, 1),
                                                    rag_rules=rag_rules, compliance_snapshot=compliance_text)
                        self.log("Dispatcher: Saved new option {}".format(saved.get("id")))
                    else:
                        saved = mgr.save_revision(manifest_to_save, intent_text=intent_text, duration_s=round(build_duration, 1),
                                                   rag_rules=rag_rules, compliance_snapshot=compliance_text)
                        self.log("Dispatcher: Saved revision {} for option {}".format(
                            saved.get("id"), mgr._data.get("current_option_id")))
                except Exception as _mem_err:
                    self.log("Dispatcher: Build memory save warning: {}".format(_mem_err))

                # Return tracker report or summary (prepend any fast-intent results)
                if tracker:
                    tracker.analyze_manifest(manifest)
                    return _fast_prefix + tracker.generate_final_report(base_summary="Build Successful (Agentic Resolution Applied).")
                return _fast_prefix + "Build Completed successfully."

            except Exception as e:
                import traceback
                err = "Orchestration Error (Attempt {}): {}\n{}".format(attempt + 1, str(e), traceback.format_exc())
                self.log(err)
                if attempt == max_attempts - 1:
                    return _fast_prefix + err if _fast_prefix else err
                # If the failure was a missing JSON block (model produced prose instead),
                # give a laser-focused retry that forbids all prose output.
                if "Expecting value" in str(e) or "no JSON" in str(e).lower():
                    _raw_resp = manifest_json if isinstance(manifest_json, str) else ""
                    _no_json_brace = "{" not in _raw_resp
                    self.log("_orchestrate: no-JSON response detected (no_brace={}) — injecting OUTPUT-ONLY reprompt".format(
                        _no_json_brace))
                    _shell_adj_note = (
                        "\nREMINDER — if the core cannot fit in the current arm geometry, "
                        "you ARE permitted to widen the relevant arm (Rule 3 conditional exception). "
                        "Do NOT write an essay explaining why it cannot fit — widen the arm and output the manifest."
                    ) if _no_json_brace else ""
                    current_prompt += (
                        "\n\n[CRITICAL — PREVIOUS RESPONSE HAD NO JSON BLOCK]:\n"
                        "Your last response contained only prose/reasoning text — no ```json block was found.\n"
                        "THIS TIME: Output ONLY two things, nothing else:\n"
                        "1. <architectural_intent> block — 2 sentences MAX\n"
                        "2. The ```json\\n{...}\\n``` manifest block\n"
                        "Do NOT write any analysis, tables, bullet lists, or explanations outside these two blocks.\n"
                        "Use sparse footprint_scale_overrides (5-8 control points only, NOT one entry per floor)."
                        + _shell_adj_note
                    )
                else:
                    current_prompt += f"\n\n[ERROR IN PREVIOUS ATTEMPT]:\n{str(e)}\n\nPlease ensure you follow the JSON schema strictly."
                time.sleep(1)

    def _stream_narrative_to_user(self, text, tracker):
        """Extracts and streams <architectural_intent> and <resolution_thoughts> to the user.
        Returns the captured intent text (or None) for use in build_memory naming."""
        import re
        captured_intent = None

        intent_match = re.search(r"<architectural_intent>(.*?)</architectural_intent>", text, re.DOTALL)
        if intent_match:
            intent_text = intent_match.group(1).strip()
            if intent_text:
                if tracker:
                    tracker.report(f"**Architectural Intent:**\n{intent_text}", is_narrative=True)
                captured_intent = intent_text

        res_match = re.search(r"<resolution_thoughts>(.*?)</resolution_thoughts>", text, re.DOTALL)
        if res_match:
            res_text = res_match.group(1).strip()
            if res_text and tracker:
                tracker.report(f"**Conflict Resolution Logic:**\n{res_text}", is_narrative=True)

        return captured_intent

    def _is_complex_footprint(self, manifest):
        """Return True if the manifest describes a footprint that requires two-pass placement."""
        shell = manifest.get("shell", {})
        if shell.get("footprint_points"):           return True
        if shell.get("footprint_svg"):              return True
        if shell.get("footprint_holes"):            return True
        if shell.get("footprint_offset_overrides"): return True
        if manifest.get("volumes"):                 return True
        return False

    def _format_floor_plate_analysis(self, analysis):
        """Format pre_analyse_floor_plate() result into a text block for the Pass 2 prompt."""
        a = analysis
        fp = a.get("passenger_lift_elements", {})
        fc = a.get("fire_cluster_elements", {})
        fv = a.get("floor_plate_variation", {})
        b3 = a.get("building_form_3d", {})
        zones = a.get("solid_zones", [])
        vbounds = b3.get("void_boundaries_mm")
        area_m2 = a.get("floor_plate_area_mm2", 0) / 1e6

        _bbox = a.get("bounding_box_mm", [])
        _coord_note = ""
        if _bbox and len(_bbox) == 4 and (_bbox[0] < 0 or _bbox[1] < 0):
            _coord_note = (" COORDINATE SYSTEM: all coordinates below are centred on [0,0] "
                           "(bounding box [{},{},{},{}] mm). "
                           "Use these centred coordinates for lifts.position — do NOT use the "
                           "absolute coordinates from the SVG in the shell manifest.").format(
                               _bbox[0], _bbox[1], _bbox[2], _bbox[3])

        lines = [
            "## FLOOR PLATE ANALYSIS (engine-computed — use this to plan core placement)",
            "",
            "Shape: {} | Floor area: {:.0f} m²{}".format(
                a.get("shape_classification", "?"), area_m2, _coord_note),
            "",
            "### 3D Building Form",
            "Form type: {}".format(b3.get("form_type", "?")),
            b3.get("description", ""),
            "Floor plate variation: {}".format(fv.get("description", "uniform")),
            "Smallest plate: {} × {} mm (governs minimum clearance on all levels)".format(
                fv.get("smallest_plate_mm", ["?", "?"])[0],
                fv.get("smallest_plate_mm", ["?", "?"])[1]),
            "",
            "### Passenger Lift Bank (per bank — engine-computed)",
            "  Unit width per lift car: {} mm  →  bank length = N_lifts × {} mm".format(
                fp.get("lift_car_unit_w_mm", "?"), fp.get("lift_car_unit_w_mm", "?")),
            "  Bank depth (fixed, locked): {} mm  (both rows of cars + lobby + shaft walls combined)".format(
                fp.get("passenger_bank_total_depth_mm", "?")),
            "  Shaft wall thickness: {} mm  |  Car: {} mm wide × {} mm deep  |  Lobby gap: {} mm deep".format(
                fp.get("lift_wall_thickness_mm", "?"), fp.get("lift_car_w_mm", "?"),
                fp.get("lift_car_d_mm", "?"), fp.get("passenger_lobby_depth_mm", "?")),
            "  LOBBY OPEN FACES — passengers enter the lobby from the TWO ENDS of the long axis:",
            "    EW bank → lobby open faces = EAST end + WEST end → clusters MUST be 'north' or 'south'",
            "    NS bank → lobby open faces = NORTH end + SOUTH end → clusters MUST be 'east' or 'west'",
            "  RULE: Never place a cluster on a face that is a lobby open face. This is ABSOLUTE.",
            "  LOCKED ZONE: the full bank bounding box (length × depth above) — no fire cluster",
            "  element may overlap or enter it.",
            "",
            "### Fire Cluster Elements ({} clusters required)".format(fc.get("num_clusters", "?")),
            "  Fire lift shaft: {} mm wide × {} mm deep".format(
                fc.get("fire_lift_shaft_w_mm", "?"), fc.get("fire_lift_shaft_d_mm", "?")),
            "  Fire lift lobby (min): {} mm deep, {:.1f} m² area".format(
                fc.get("fire_lobby_min_depth_mm", "?"),
                fc.get("fire_lobby_min_area_mm2", 0) / 1e6),
            "  Smoke stop lobby (min): {} mm deep, {:.1f} m² area".format(
                fc.get("smoke_stop_lobby_min_depth_mm", "?"),
                fc.get("smoke_stop_lobby_min_area_mm2", 0) / 1e6),
            "  Staircase shaft: {} mm wide × {} mm deep (flight width: {} mm)".format(
                fc.get("staircase_shaft_w_mm", "?"), fc.get("staircase_shaft_d_mm", "?"),
                fc.get("staircase_flight_w_mm", "?")),
            "  CLUSTER ASSEMBLY DEPTH (fire_lift + lobby + staircase stacked perpendicular to bank face): {} mm".format(
                fc.get("cluster_assembly_depth_mm", "?")),
            "  → This is the FULL chain depth. For EW banks: the chain extends N or S by this amount.",
            "  → Verify: bank_face_to_obstacle_mm ≥ cluster_assembly_depth_mm.",
            "  → Verify: bank_face_Y + cluster_assembly_depth_mm ≤ fp_ymin (S cluster) or ≥ fp_ymax (N cluster).",
            "",
            "### Escape Staircases Required: {}".format(a.get("staircase_count_required", 2)),
            "### Default clearance from core to floor plate boundary: {} mm".format(
                a.get("minimum_clearance_mm", 6000)),
            "  (User intent overrides this default — if the user requests facade alignment, override.)",
        ]

        # Outer boundary coordinates for corner-check arithmetic
        _fp_poly = a.get("footprint_polygon_mm")
        _outer_bbox = a.get("bounding_box_mm", [])
        if _fp_poly and len(_fp_poly) >= 3:
            lines.append("")
            lines.append("### Building Boundary Coordinates (use for corner-check arithmetic)")
            lines.append("  Outer polygon vertices (mm): {}".format(
                ", ".join("[{},{}]".format(int(p[0]), int(p[1])) for p in _fp_poly)))
        elif _outer_bbox and len(_outer_bbox) == 4:
            lines.append("")
            lines.append("### Building Boundary Coordinates (use for corner-check arithmetic)")
            lines.append("  Outer bounding box (mm): xmin={} ymin={} xmax={} ymax={}".format(
                int(_outer_bbox[0]), int(_outer_bbox[1]), int(_outer_bbox[2]), int(_outer_bbox[3])))

        if vbounds:
            _void_xmin, _void_ymin, _void_xmax, _void_ymax = (
                int(vbounds[0]), int(vbounds[1]), int(vbounds[2]), int(vbounds[3]))
            lines.append("  Inner void (mm): xmin={} ymin={} xmax={} ymax={}".format(
                _void_xmin, _void_ymin, _void_xmax, _void_ymax))
            lines.append("  Void centre: [{}, {}] mm".format(
                int((_void_xmin + _void_xmax) / 2), int((_void_ymin + _void_ymax) / 2)))
            if _coord_note:
                lines.append("  NOTE: coordinates above are centred on [0,0] — use them directly for lifts.position.")
            # Placement guidance: compute ACTUAL valid bank_centre ranges for each band.
            # For each band, the bank must:
            #   (a) sit entirely outside the void (bank face clears void edge)
            #   (b) leave enough room for the fire cluster chain on the outer side
            # Valid ranges are [lo, hi]; if lo > hi the band is INFEASIBLE.
            _clust_d = fc.get("cluster_assembly_depth_mm", 0)
            _bank_d_half = int(fp.get("passenger_bank_total_depth_mm", 0)) // 2
            # When the manifest will produce TWO mirrored clusters on a single bank
            # (one outer, one inner), the inner cluster needs `cluster_d` clearance
            # too — otherwise the mirrored stair lands inside the void.  Default to
            # 2 (the most common case for courtyard typology) so the prompt range
            # is correct even before Gemini commits to a cluster count.  The post-
            # placement validator in fire_safety_logic catches drift if Gemini
            # actually emits a single-cluster directive.
            _num_clusters_reserve = max(int(fc.get("num_clusters", 2) or 2), 1)
            _inner_reserve = _clust_d if _num_clusters_reserve >= 2 else 0
            if _clust_d and _bank_d_half:
                # Footprint outer bounds (use outer_bbox when available, else void ± generous margin)
                if _outer_bbox and len(_outer_bbox) == 4:
                    _fp_xmin_pg, _fp_ymin_pg = int(_outer_bbox[0]), int(_outer_bbox[1])
                    _fp_xmax_pg, _fp_ymax_pg = int(_outer_bbox[2]), int(_outer_bbox[3])
                else:
                    # Fallback: pad void by a generous margin
                    _fp_xmin_pg = _void_xmin - 20000; _fp_xmax_pg = _void_xmax + 20000
                    _fp_ymin_pg = _void_ymin - 20000; _fp_ymax_pg = _void_ymax + 20000
                # South band: bank sits between fp_ymin and void_ymin
                #   Outer (south of bank): cluster_d reservation toward fp_ymin
                #   Inner (north of bank): cluster_d reservation toward void_ymin
                #     when 2 clusters — mirrored cluster lands here, must clear void
                _s_lo = _fp_ymin_pg + _clust_d + _bank_d_half
                _s_hi = _void_ymin - _bank_d_half - _inner_reserve
                # North band: bank sits between void_ymax and fp_ymax
                #   Outer (north of bank): cluster_d reservation toward fp_ymax
                #   Inner (south of bank): cluster_d reservation toward void_ymax
                _n_lo = _void_ymax + _bank_d_half + _inner_reserve
                _n_hi = _fp_ymax_pg - _clust_d - _bank_d_half
                # East band: bank between void_xmax and fp_xmax
                _e_lo = _void_xmax + _bank_d_half + _inner_reserve
                _e_hi = _fp_xmax_pg - _clust_d - _bank_d_half
                # West band: bank between fp_xmin and void_xmin
                _w_lo = _fp_xmin_pg + _clust_d + _bank_d_half
                _w_hi = _void_xmin - _bank_d_half - _inner_reserve

                lines.append("  PLACEMENT GUIDE — valid bank_centre ranges for EW bank (cluster goes N or S):")
                lines.append("    Cluster chain depth = {} mm (fire_lift + lobby + stair stacked)".format(_clust_d))
                lines.append("    Bank half-depth = {} mm".format(_bank_d_half))
                if _inner_reserve:
                    lines.append("    NOTE: assuming {} clusters per bank — both outer AND inner sides reserve "
                                 "{} mm clearance (the inner-side cluster is mirrored from the outer side and "
                                 "must not overlap the courtyard void)".format(
                                     _num_clusters_reserve, _clust_d))
                # Total clearance the band must hold:
                # outer cluster (cluster_d) + bank depth (2·bank_hd) + inner cluster
                # (inner_reserve, 0 for 1-cluster banks).
                _band_need = _clust_d + _bank_d_half * 2 + _inner_reserve
                _budget_label = "{}mm chain (outer) + {}mm bank{}".format(
                    _clust_d, _bank_d_half * 2,
                    " + {}mm chain (inner mirror)".format(_inner_reserve) if _inner_reserve else "")
                if _s_lo <= _s_hi:
                    lines.append("    South band — FEASIBLE: bank_centre_y in [{}, {}] mm  (bank south of void, cluster toward south wall)".format(
                        _s_lo, _s_hi))
                else:
                    lines.append("    South band — INFEASIBLE: need bank_cy in [{}, {}] mm but lo>hi "
                                 "(south band {}mm deep, need {} = {}mm total)".format(
                        _s_lo, _s_hi,
                        _void_ymin - _fp_ymin_pg, _budget_label, _band_need))
                if _n_lo <= _n_hi:
                    lines.append("    North band — FEASIBLE: bank_centre_y in [{}, {}] mm  (bank north of void, cluster toward north wall)".format(
                        _n_lo, _n_hi))
                else:
                    lines.append("    North band — INFEASIBLE: need bank_cy in [{}, {}] mm but lo>hi "
                                 "(north band {}mm deep, need {} = {}mm total)".format(
                        _n_lo, _n_hi,
                        _fp_ymax_pg - _void_ymax, _budget_label, _band_need))
                lines.append("  PLACEMENT GUIDE — valid bank_centre ranges for NS bank (cluster goes E or W):")
                if _e_lo <= _e_hi:
                    lines.append("    East band — FEASIBLE: bank_centre_x in [{}, {}] mm  (bank east of void, cluster toward east wall)".format(
                        _e_lo, _e_hi))
                else:
                    lines.append("    East band — INFEASIBLE: need bank_cx in [{}, {}] mm but lo>hi "
                                 "(east band {}mm wide, need {} = {}mm total)".format(
                        _e_lo, _e_hi,
                        _fp_xmax_pg - _void_xmax, _budget_label, _band_need))
                if _w_lo <= _w_hi:
                    lines.append("    West band — FEASIBLE: bank_centre_x in [{}, {}] mm  (bank west of void, cluster toward west wall)".format(
                        _w_lo, _w_hi))
                else:
                    lines.append("    West band — INFEASIBLE: need bank_cx in [{}, {}] mm but lo>hi "
                                 "(west band {}mm wide, need {} = {}mm total)".format(
                        _w_lo, _w_hi,
                        _void_xmin - _fp_xmin_pg, _budget_label, _band_need))
                _any_feasible = any([_s_lo <= _s_hi, _n_lo <= _n_hi, _e_lo <= _e_hi, _w_lo <= _w_hi])
                if not _any_feasible:
                    _min_band_sz = _band_need + 1000
                    _void_y_depth = _void_ymax - _void_ymin
                    _void_x_depth = _void_xmax - _void_xmin
                    _min_bldg_ew = int(_void_y_depth + 2 * _min_band_sz)
                    _min_bldg_ns = int(_void_x_depth + 2 * _min_band_sz)
                    _cur_ew = int(_fp_ymax_pg - _fp_ymin_pg)
                    _cur_ns = int(_fp_xmax_pg - _fp_xmin_pg)
                    lines.append("  *** ALL BANDS INFEASIBLE *** The courtyard bands are too narrow for this bank size.")
                    lines.append("  Minimum building size needed:")
                    lines.append("    shell.length ≥ {} mm  (EW bank, shortfall: {} mm)".format(
                        _min_bldg_ew, max(0, _min_bldg_ew - _cur_ew)))
                    lines.append("    shell.width  ≥ {} mm  (NS bank, shortfall: {} mm)".format(
                        _min_bldg_ns, max(0, _min_bldg_ns - _cur_ns)))
                    lines.append("  Fix options (choose one):")
                    lines.append("    1. Increase shell.length to ≥ {} mm (EW bank) OR shell.width to ≥ {} mm (NS bank).".format(
                        _min_bldg_ew, _min_bldg_ns))
                    lines.append("    2. Reduce lifts.count so chain depth decreases.")
                    lines.append("    3. Shrink the courtyard void (reduce footprint_holes dimensions).")
                    lines.append("  MANDATORY: Do NOT just move lifts.position — there is no valid position at the current building size.")
                lines.append("  CRITICAL: lifts.position MUST land inside one of the FEASIBLE ranges above. "
                             "Do NOT place the bank overlapping the void or in an INFEASIBLE band.")

        if zones:
            lines.append("")
            lines.append("### Solid Zones")
            for z in zones:
                lines.append("  {}: centroid {} mm — {} mm wide × {} mm deep".format(
                    z.get("label", "?"), z.get("centroid_mm", "?"),
                    z.get("available_w_mm", 0), z.get("available_d_mm", 0)))

            # Per-arm feasibility check: compute whether each arm is wide/deep enough
            # to fit the fire-safety cluster in EW or NS orientation.
            _bank_d  = fp.get("passenger_bank_total_depth_mm", 0) or 0
            _clust_d = fc.get("cluster_assembly_depth_mm", 0) or 0
            _corr    = 1200  # min corridor each side (mm)
            _min_span = _bank_d + _clust_d + 2 * _corr  # total depth needed perpendicular to bank
            if _bank_d and _clust_d:
                lines.append("")
                lines.append("### Arm Feasibility Check")
                lines.append(
                    "  Required perpendicular span = bank_depth({}) + cluster_depth({}) + 2×corridor({}) = {} mm".format(
                        _bank_d, _clust_d, _corr, _min_span))
                for z in zones:
                    _aw = z.get("available_w_mm", 0) or 0
                    _ad = z.get("available_d_mm", 0) or 0
                    _lbl = z.get("label", "?")
                    lines.append("  {} ({}mm wide × {}mm deep):".format(_lbl, _aw, _ad))
                    # EW bank: bank runs east-west, cluster extends N or S → depth is the constraint
                    _ew_ok = _ad >= _min_span
                    _ew_short = _min_span - _ad if not _ew_ok else 0
                    if _ew_ok:
                        lines.append("    EW bank (N/S cluster): arm {}mm deep ≥ {}mm needed → FEASIBLE".format(
                            _ad, _min_span))
                    else:
                        lines.append(
                            "    EW bank (N/S cluster): arm {}mm deep < {}mm needed → INFEASIBLE "
                            "(widen arm by min +{}mm, move concave corner outward by {}mm)".format(
                                _ad, _min_span, _ew_short, _ew_short))
                    # NS bank: bank runs north-south, cluster extends E or W → width is the constraint
                    _ns_ok = _aw >= _min_span
                    _ns_short = _min_span - _aw if not _ns_ok else 0
                    if _ns_ok:
                        lines.append("    NS bank (E/W cluster): arm {}mm wide ≥ {}mm needed → FEASIBLE".format(
                            _aw, _min_span))
                    else:
                        lines.append(
                            "    NS bank (E/W cluster): arm {}mm wide < {}mm needed → INFEASIBLE "
                            "(widen arm by min +{}mm, move concave corner outward by {}mm)".format(
                                _aw, _min_span, _ns_short, _ns_short))
                    if _ew_ok or _ns_ok:
                        _orient = []
                        if _ew_ok: _orient.append("EW bank")
                        if _ns_ok: _orient.append("NS bank")
                        lines.append("    VERDICT: {} feasible in this arm.".format(" and ".join(_orient)))
                    else:
                        lines.append(
                            "    VERDICT: NEITHER orientation fits — arm must be widened. "
                            "Minimum: widen depth by {}mm (for EW bank) OR widen width by {}mm (for NS bank). "
                            "Per Rule 3 conditional exception, you MAY widen this arm.".format(
                                _ew_short, _ns_short))

        # Courtyard multi-bank split guidance
        _csplit = a.get("courtyard_split_guidance", {})
        if _csplit.get("requires_separate_banks"):
            _mb = _csplit.get("main_bank", {})
            _sb = _csplit.get("secondary_bank", {})
            _bb_cs = a.get("bounding_box_mm", [0, 0, 0, 0])
            _cx = int((_bb_cs[0] + _bb_cs[2]) / 2)
            lines.append("")
            lines.append("### ⚠ COURTYARD MULTI-BANK CONSTRAINT (MANDATORY — Rule 9)")
            lines.append("  " + _csplit.get("reason", ""))
            lines.append("  You MUST use lifts.banks with exactly this structure:")
            lines.append('  "lifts": {')
            lines.append('    "banks": [')
            lines.append('      {')
            lines.append('        "count": {},'.format(_mb.get("count", "?")))
            lines.append('        "position": [{}, {}],'.format(_cx, _mb.get("position_y_mm", "?")))
            lines.append('        "orientation": "EW",')
            lines.append('        "clusters": [{{"side": "{}"}}]'.format(_mb.get("cluster_side", "south")))
            lines.append('      },')
            lines.append('      {')
            lines.append('        "count": {},'.format(_sb.get("count", 1)))
            lines.append('        "position": [{}, {}],'.format(_cx, _sb.get("position_y_mm", "?")))
            lines.append('        "orientation": "EW",')
            lines.append('        "clusters": [{{"side": "{}"}}]'.format(_sb.get("cluster_side", "north")))
            lines.append('      }')
            lines.append('    ]')
            lines.append('  }')
            lines.append("  Main bank (count={}, y={}): {} cluster outer face at y={} — {}mm from outer wall".format(
                _mb.get("count", "?"), _mb.get("position_y_mm", "?"),
                _mb.get("cluster_side", "?"), _mb.get("cluster_outer_y_mm", "?"),
                _mb.get("south_wall_clearance_mm", "?")))
            lines.append("  Secondary bank (count={}, y={}): {} cluster outer face at y={} — {}mm from outer wall".format(
                _sb.get("count", 1), _sb.get("position_y_mm", "?"),
                _sb.get("cluster_side", "?"), _sb.get("cluster_outer_y_mm", "?"),
                _sb.get("north_wall_clearance_mm", "?")))
            lines.append("  Use these x={}, y coordinates EXACTLY.".format(_cx))
            lines.append("  Both cluster lobby faces open AWAY from the void — no door faces the courtyard.")

        # ── Engine-Computed Core Sizes (arithmetic — no pre-solve OR-Tools call) ──
        # OR-Tools pre-computation removed: it ran against a dummy anchor and added
        # 5–20s per build. Module sizes below are from BS-standards arithmetic and
        # are what OR-Tools actually uses at build time.
        lines.append("")
        _fc_fl_d  = fc.get("fire_lift_shaft_d_mm", "?")
        _fc_fl_w  = fc.get("fire_lift_shaft_w_mm", "?")
        _fc_lb_d  = fc.get("fire_lobby_min_depth_mm", "?")
        _fc_sw    = fc.get("staircase_shaft_w_mm", "?")
        _fc_sd    = fc.get("staircase_shaft_d_mm", "?")
        _fc_cd    = fc.get("cluster_assembly_depth_mm", "?")
        _nc       = fc.get("num_clusters", "?")
        lines.append("### Engine Core Sizes ({} cluster(s) required)".format(_nc))
        lines.append("  Module sizes: fire lift {}×{}mm  |  fire lobby ≥{}mm deep  |  staircase {}×{}mm".format(
            _fc_fl_w, _fc_fl_d, _fc_lb_d, _fc_sw, _fc_sd))
        lines.append("  CLUSTER ASSEMBLY DEPTH (from bank face to cluster outer edge): {} mm".format(_fc_cd))
        lines.append("  This is how far each cluster extends away from the bank face.")

        # Valid bank position range from arithmetic (same formula as before, no OR-Tools needed)
        _pos_bbox = _outer_bbox if (_outer_bbox and len(_outer_bbox) == 4) else None
        _bank_half_d = int(fp.get("passenger_bank_total_depth_mm", 0)) // 2
        if _pos_bbox and _bank_half_d and isinstance(_fc_cd, int):
            _bx1, _by1, _bx2, _by2 = (int(_pos_bbox[0]), int(_pos_bbox[1]),
                                       int(_pos_bbox[2]), int(_pos_bbox[3]))
            lines.append("")
            lines.append("  ### VALID LIFTS.POSITION RANGE (arithmetic — MUST satisfy ALL constraints)")
            lines.append("  Building boundary: x=[{}, {}] mm  y=[{}, {}] mm".format(_bx1, _by1, _bx2, _by2))
            lines.append("  Bank half-depth: {} mm  |  Cluster assembly depth: {} mm".format(_bank_half_d, _fc_cd))
            lines.append("  NS bank (clusters E or W of bank):")
            lines.append("    bank_centre_x ≥ {} mm  (= {}+{})  [west edge clear]".format(
                _bx1 + _bank_half_d, _bx1, _bank_half_d))
            lines.append("    bank_centre_x ≤ {} mm  (= {}-{})  [east edge clear]".format(
                _bx2 - _bank_half_d, _bx2, _bank_half_d))
            lines.append("    East cluster: bank_centre_x ≤ {} mm  (= {}-{}-{})".format(
                _bx2 - _bank_half_d - _fc_cd, _bx2, _bank_half_d, _fc_cd))
            lines.append("    West cluster: bank_centre_x ≥ {} mm  (= {}+{}+{})".format(
                _bx1 + _bank_half_d + _fc_cd, _bx1, _bank_half_d, _fc_cd))
            lines.append("  EW bank (clusters N or S of bank):")
            lines.append("    bank_centre_y ≥ {} mm  (= {}+{})  [south edge clear]".format(
                _by1 + _bank_half_d, _by1, _bank_half_d))
            lines.append("    bank_centre_y ≤ {} mm  (= {}-{})  [north edge clear]".format(
                _by2 - _bank_half_d, _by2, _bank_half_d))
            lines.append("    North cluster: bank_centre_y ≤ {} mm  (= {}-{}-{})".format(
                _by2 - _bank_half_d - _fc_cd, _by2, _bank_half_d, _fc_cd))
            lines.append("    South cluster: bank_centre_y ≥ {} mm  (= {}+{}+{})".format(
                _by1 + _bank_half_d + _fc_cd, _by1, _bank_half_d, _fc_cd))
            lines.append("  CRITICAL: place bank so EVERY cluster fits within building boundary.")
            lines.append("  The engine will snap the bank by up to 2000mm if slightly outside — but")
            lines.append("  large violations will still cause a CONFLICT. Stay within the range above.")

        lines.append("")
        lines.append("END FLOOR PLATE ANALYSIS — verify every sub-element corner against boundary coords above before committing.")
        return "\n".join(lines)

    def _answer_query(self, uiapp, user_prompt, tracker=None, classified=None):
        """Handle query intent: gather BIM state + options list, then answer directly.
        Skips the full build pipeline (no RAG, no compliance, no manifest generation)."""
        import json as _json

        # Gather BIM state (reuse cache if fresh)
        import time as _time
        now = _time.time()
        if hasattr(self, "_cached_state") and hasattr(self, "_cache_time") and (now - self._cache_time) < 120.0:
            state_text = self._cached_state
        else:
            try:
                def _gather():
                    import Autodesk.Revit.DB as DB  # type: ignore
                    doc = uiapp.ActiveUIDocument.Document
                    levels = [l for l in DB.FilteredElementCollector(doc).OfClass(DB.Level)
                              if l.Name.startswith("AI Level") or l.Name.startswith("AI_Level")]
                    levels.sort(key=lambda x: x.Elevation)
                    count = len(levels)
                    storeys = max(0, count - 1)
                    wall_count = DB.FilteredElementCollector(doc).OfClass(DB.Wall).GetElementCount()
                    floor_count = DB.FilteredElementCollector(doc).OfClass(DB.Floor).GetElementCount()
                    col_count = (
                        DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Columns).WhereElementIsNotElementType().GetElementCount()
                        + DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_StructuralColumns).WhereElementIsNotElementType().GetElementCount()
                    )
                    return storeys, wall_count, floor_count, col_count
                storeys, walls, floors, cols = mcp_event_handler.run_on_main_thread(_gather)
                state_text = "CURRENT BIM STATE: {} storeys, {} walls, {} floors, {} columns.".format(storeys, walls, floors, cols)
            except Exception as _e:
                self.log("_answer_query: BIM state gather failed — {}".format(_e))
                state_text = "CURRENT BIM STATE: unavailable."

        # Include saved options summary for context (e.g. "which option am I on?")
        try:
            mgr = get_options_manager()
            options_summary = mgr.list_options()
        except Exception:
            options_summary = ""

        _goal       = (classified or {}).get("goal", "")
        _detail     = (classified or {}).get("detail_level", "standard")
        _tone       = (classified or {}).get("tone", "conversational")

        _format_instr = {
            "brief":    "Answer in 1-2 sentences. Be direct — no tables, no bullet lists.",
            "detailed": "Give a thorough breakdown. Use a table or bullet list where it adds clarity.",
            "standard": "Answer clearly and concisely. Add a table or bullets only if it genuinely helps.",
        }.get(_detail, "Answer clearly and concisely.")
        _tone_instr = "professional and precise" if _tone == "technical" else "warm and conversational"

        _context_lines = []
        if _goal:
            _context_lines.append("User's goal: {}".format(_goal))
        _context_lines.append("Response format: {}".format(_format_instr))
        _context_lines.append("Tone: {}".format(_tone_instr))
        _context_block = "\n".join(_context_lines) + "\n\n"

        query_prompt = (
            "You are a BIM assistant. Answer the user's question using only the information below.\n"
            "Do NOT produce a building manifest. Do NOT use JSON. Answer in plain text only.\n\n"
            "{}"
            "{}\n"
            "{}\n\n"
            "User question: {}\n\n"
            "Answer:"
        ).format(_context_block, state_text, options_summary, user_prompt)

        self.log("_answer_query: sending lightweight query to Gemini.")
        answer = client.generate_content(query_prompt, thinking_budget=0, temperature=0.1)
        # Strip any accidental JSON wrapping
        if answer and answer.strip().startswith("{"):
            try:
                parsed = _json.loads(answer.strip())
                if "response" in parsed:
                    answer = parsed["response"]
            except Exception:
                pass
        return answer.strip() if answer else "I could not answer that question."

    def _answer_authority_query(self, user_prompt, tracker=None, history=None, classified=None):
        """Handle authority_query intent: search the ENTIRE Vertex AI datastore (all authorities —
        SCDF, URA, LTA, NEA, NPARKS, PUB, etc.) and answer using only the retrieved excerpts."""
        import time as _time
        from revit_mcp.rag.vertex_rag import query_vertex_rag

        import re as _re
        t0 = _time.time()

        _goal   = (classified or {}).get("goal", "")
        _detail = (classified or {}).get("detail_level", "standard")
        _tone   = (classified or {}).get("tone", "technical")

        # Detect retry: if the current message is vague ("try again", "redo", "that's wrong" etc.)
        # and history contains a previous authority_query turn, reconstruct the actual question
        # and note what was wrong so the RAG search and Gemini answer can improve.
        _retry_note = ""
        _RETRY_PHRASES = {"try again", "try once more", "redo", "that's wrong", "incorrect", "wrong answer",
                          "that is wrong", "not correct", "try it again", "wrong information", "wrong info"}
        _is_retry = any(ph in user_prompt.lower() for ph in _RETRY_PHRASES) or len(user_prompt.strip().split()) <= 4
        if _is_retry and history:
            # Walk back through history to find the last authority question and answer pair
            _prev_question = None
            _prev_answer = None
            _complaint = user_prompt  # what the user said is wrong
            for turn in reversed(history):
                text = (turn.get("text") or "").strip()
                if not text:
                    continue
                if not turn.get("is_user") and _prev_answer is None:
                    _prev_answer = text[:600]  # last assistant answer
                elif turn.get("is_user") and _prev_question is None and text.lower() not in _RETRY_PHRASES:
                    _prev_question = text  # last real user question
                if _prev_question and _prev_answer:
                    break
            if _prev_question and _prev_answer:
                user_prompt = _prev_question
                _retry_note = (
                    "\n\nNOTE — THIS IS A RETRY: The user found the previous answer unsatisfactory.\n"
                    "Previous answer given:\n{}\n"
                    "User complaint: {}\n"
                    "Try to retrieve DIFFERENT or MORE SPECIFIC excerpts than before. "
                    "If the previous answer missed a table or clause, focus the search on that specifically."
                ).format(_prev_answer[:400], _complaint)
                self.log(f"[AuthorityQuery] retry detected — original question: '{user_prompt[:80]}', complaint: '{_complaint[:80]}'")

        self.log("_answer_authority_query: searching authority code library...")
        self.log(f"[AuthorityQuery] querying Vertex RAG — query='{user_prompt[:100]}'")

        # Detect which authority the user is asking about (default SCDF for table refs).
        # Used both to focus query expansion and to post-filter RAG results.
        _AUTHORITY_HINTS = {
            "scdf": "SCDF", "fire code": "SCDF", "fire safety": "SCDF",
            "ura": "URA", "master plan": "URA", "gross plot ratio": "URA",
            "lta": "LTA", "parking": "LTA", "railway": "LTA",
            "bca": "BCA", "accessibility": "BCA",
            "nea": "NEA", "nparks": "NPARKS", "pub": "PUB",
        }
        _prompt_lower = user_prompt.lower()
        detected_authority = next(
            (v for k, v in _AUTHORITY_HINTS.items() if k in _prompt_lower), None
        )
        # Tables like 2.2A, 2.2B are SCDF fire code tables — default to SCDF
        if not detected_authority and _re.search(r'table\s+\d+\.\d+', _prompt_lower):
            detected_authority = "SCDF"
        source_filter = detected_authority if detected_authority else None
        self.log(f"[AuthorityQuery] detected_authority={detected_authority} source_filter={source_filter}")

        # Ask Gemini to expand the user's query into 3 precise RAG search strings,
        # scoped to the detected authority so it doesn't invent cross-authority variants.
        authority_ctx = f"The user is asking about {detected_authority} codes. " if detected_authority else ""
        _goal_ctx = "User's goal: {}\n".format(_goal) if _goal else ""
        expansion_prompt = (
            "You are a search query expert for Singapore building authority codes.\n"
            "{auth}A user asked: \"{query}\"{retry}\n"
            "{goal}"
            "\nGenerate exactly 3 search queries to retrieve the most relevant content from a vector "
            "database of Singapore authority code PDFs. Rules:\n"
            "- All 3 queries must be about the SAME authority/document — do NOT invent variants for other agencies\n"
            "- Be specific: include the table/clause number AND its subject matter\n"
            "- For tables: state what the table contains (e.g. 'Table 2.2A maximum travel distance exit width occupancy type non-sprinklered sprinklered')\n"
            "- For clauses: include the clause number and the requirement topic\n"
            "- Vary phrasing across the 3 queries to maximise recall\n"
            "- Never include terms like 'amendment', 'circular date', 'effective date', 'clause status'\n\n"
            "Return ONLY a JSON array of 3 strings, no explanation:\n"
            "[\"query 1\", \"query 2\", \"query 3\"]"
        ).format(auth=authority_ctx, query=user_prompt, retry=_retry_note, goal=_goal_ctx)

        queries = [user_prompt]  # always keep original as fallback
        self.log(f"[AuthorityQuery] calling Gemini for query expansion...")
        try:
            expansion_raw = client.generate_content(expansion_prompt, thinking_budget=0, temperature=0.1)
            self.log(f"[AuthorityQuery] expansion raw: {expansion_raw[:300]!r}")
            match = _re.search(r'\[.*?\]', expansion_raw, _re.DOTALL)
            if match:
                import json as _json
                expanded = _json.loads(match.group())
                if isinstance(expanded, list):
                    queries = [q for q in expanded if isinstance(q, str) and q.strip()]
                    queries.append(user_prompt)
                    self.log(f"[AuthorityQuery] Gemini expanded to: {queries}")
            else:
                self.log(f"[AuthorityQuery] no JSON array in expansion response, using original")
        except Exception as e:
            self.log(f"[AuthorityQuery] query expansion failed ({e}), using original query")

        # Deduplicate while preserving order
        _seen_q = set()
        queries = [q for q in queries if not (_seen_q.add(q.lower().strip()) or q.lower().strip() in _seen_q - {q.lower().strip()})]

        # If the user explicitly named a table (e.g. "table 2.2a"), append a query phrased
        # like the chunk's own content header. Tables in the datastore open with lines such
        # as "TABLE 2.2A : DETERMINATION OF EXIT REQUIREMENT" — matching that string lifts
        # the grid-bearing chunk into the top results even when its metadata title is off
        # (e.g. titled after an adjacent clause). Only fires when a table ref is present,
        # so non-table queries are unaffected.
        _user_table_refs = _re.findall(r'table\s+(\d+\.\d+[a-z]?)', user_prompt.lower())
        if _user_table_refs:
            for _ref in _user_table_refs:
                _verbatim = f"TABLE {_ref.upper()} :"
                if _verbatim.lower() not in _seen_q:
                    queries.append(_verbatim)
                    _seen_q.add(_verbatim.lower())
            self.log(f"[AuthorityQuery] injected verbatim table-header queries for refs: {_user_table_refs}")

        self.log(f"[AuthorityQuery] firing {len(queries)} parallel queries: {queries}")
        all_results = []
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=len(queries)) as _pool:
            futures = {_pool.submit(query_vertex_rag, q, None, 10, source_filter): q for q in queries}
            try:
                for fut in _cf.as_completed(futures, timeout=60):
                    check_cancelled("RAG authority query")
                    try:
                        all_results.extend(fut.result())
                    except Exception as e:
                        self.log(f"[AuthorityQuery] sub-query error: {e}")
            except _cf.TimeoutError:
                # Harvest any futures that already finished before the timeout
                self.log("[AuthorityQuery] timeout — harvesting completed futures")
                for fut, q in futures.items():
                    if fut.done():
                        try:
                            all_results.extend(fut.result())
                        except Exception as e:
                            self.log(f"[AuthorityQuery] sub-query error ({q}): {e}")

        # Drop amendment-history chunks — they reference table names in a changelog
        # format but never contain the actual table data, and drown out real content.
        def _is_amendment_history(chunk):
            c = chunk.get("content", "")
            return ("Amendment Date" in c and "Effective Date" in c and "Clause Status" in c)

        # Partition: real content first, amendment history as fallback only
        real_chunks = [c for c in all_results if not _is_amendment_history(c)]
        amend_chunks = [c for c in all_results if _is_amendment_history(c)]
        ordered = real_chunks + amend_chunks

        # Deduplicate by content prefix, keep insertion order
        _seen_c = set()
        results = []
        for chunk in ordered:
            key = chunk.get("content", "")[:120]
            if key and key not in _seen_c:
                _seen_c.add(key)
                results.append(chunk)
        results = results[:15]  # cap at 15 unique chunks
        self.log(f"[AuthorityQuery] {len(results)} unique chunks from {len(queries)} queries in {_time.time()-t0:.2f}s")

        if not results:
            msg = "I could not find relevant excerpts in the authority code library for that question. The datastore may be unavailable — please try again."
            if tracker: tracker.report(msg)
            return msg

        # ── Pre-process chunks before assembling the Gemini context ───────────
        # 1. Detect table-rich chunks (count pipe-density per line, not just |-|).
        # 2. Stitch fragmented "(part N)" chunks of the same clause into one.
        # 3. Rank chunks by relevance to the user's prompt so most-useful go first.

        def _pipe_density(text: str) -> int:
            """Count consecutive lines with 3+ pipe separators — strong signal of a table."""
            lines = text.splitlines()
            run = best = 0
            for ln in lines:
                if ln.count("|") >= 3:
                    run += 1
                    if run > best: best = run
                else:
                    run = 0
            return best

        def _has_table(text: str) -> bool:
            return (
                "|-" in text or "| --- |" in text
                or _pipe_density(text) >= 3
                or "TABLE " in text.upper() and "|" in text
            )

        # Stitch (part N) fragments. Title pattern: "Clause X.Y.Z — Topic (part N)".
        # Group chunks whose title (sans "(part N)") matches; concatenate their content
        # in part-number order. Single-part clauses pass through unchanged.
        import re as _re_stitch
        _PART_RE = _re_stitch.compile(r"\s*\(part\s+(\d+)\)\s*$", _re_stitch.IGNORECASE)
        _groups: dict = {}
        _order: list = []
        for chunk in results:
            title = (chunk.get("metadata", {}).get("title") or "").strip()
            m = _PART_RE.search(title)
            if m:
                base_title = _PART_RE.sub("", title).strip()
                part_num = int(m.group(1))
            else:
                base_title = title
                part_num = 1
            key = base_title or chunk.get("content", "")[:60]
            if key not in _groups:
                _groups[key] = []
                _order.append(key)
            _groups[key].append((part_num, chunk))

        stitched_chunks = []
        for key in _order:
            parts = sorted(_groups[key], key=lambda pc: pc[0])
            if len(parts) == 1:
                stitched_chunks.append(parts[0][1])
                continue
            base = dict(parts[0][1])  # shallow copy of first chunk
            base = {**base, "metadata": dict(base.get("metadata", {}))}
            base["metadata"]["title"] = key  # drop "(part N)" suffix
            base["content"] = "\n".join(p[1].get("content", "") for p in parts)
            # Merge clause_refs across parts
            refs = []
            for _, p in parts:
                for r in p.get("metadata", {}).get("clause_refs", []) or []:
                    if r not in refs: refs.append(r)
            base["metadata"]["clause_refs"] = refs
            stitched_chunks.append(base)
            self.log(f"[AuthorityQuery] stitched {len(parts)} parts of '{key[:60]}' -> {len(base['content'])} chars")

        # Relevance rank: extract table refs and key terms from the user prompt,
        # boost chunks whose content/title hits them. Stable sort preserves order
        # within same score so original retrieval relevance is the tiebreaker.
        _table_refs = _re_stitch.findall(r'table\s+(\d+\.\d+[a-z]?)', user_prompt.lower())
        _query_terms = [w for w in _re_stitch.findall(r'\w{4,}', user_prompt.lower())
                        if w not in {"what", "show", "tell", "give", "from", "this", "that",
                                     "with", "have", "does", "should", "would", "could",
                                     "code", "rule", "table"}]

        def _score(chunk):
            content_lc = chunk.get("content", "").lower()
            text = (content_lc + " " + (chunk.get("metadata", {}).get("title", ""))).lower()
            score = 0
            for ref in _table_refs:
                # Grid-bearing chunks open with "TABLE 2.2A : ..." — give them the largest
                # boost so they outrank prose chunks that merely reference the table by name.
                # Gated on _table_refs so non-table queries see no behaviour change.
                if f"table {ref} :" in content_lc or f"table {ref}:" in content_lc:
                    score += 25
                if f"table {ref}" in text: score += 10
                elif ref in text:          score += 4
            # When the user asked about a specific table, prefer chunks that actually contain
            # tabular structure over prose. _has_table is defined in the enclosing scope.
            if _table_refs and _has_table(chunk.get("content", "")):
                score += 6
            for term in _query_terms:
                if term in text: score += 1
            return -score  # negative for ascending sort = highest score first

        ranked_chunks = sorted(stitched_chunks, key=_score)

        # Build chunks text for Gemini.
        chunks_text = ""
        sources_seen = []
        for chunk in ranked_chunks:
            meta        = chunk.get("metadata", {})
            title       = meta.get("title", "")
            page        = meta.get("page", "")
            clause_refs = meta.get("clause_refs", [])
            source_uri  = chunk.get("source_uri", "")
            raw_content = chunk.get("content", "")

            clean = raw_content.replace("_START_OF_TABLE_", "").replace("_END_OF_TABLE_", "").replace("TABLE_IN_MARKDOWN:", "").strip()

            # Stitched chunks are usually tables; give them a generous cap.
            has_table = _has_table(clean)
            cap = 12000 if has_table else 2000
            content = clean[:cap]

            refs_str = f" [refs: {', '.join(clause_refs)}]" if clause_refs else ""
            label    = f"{title} p.{page}" if page else title
            chunks_text += f"\n[{label}{refs_str}]\n{content}\n"
            self.log(f"[AuthorityQuery] chunk: label={label!r} has_table={has_table} len={len(content)} | {content.strip().splitlines()[0][:80]!r}")
            # Verbose dump: full content of any chunk that scored relevance points,
            # so we can see exactly what's reaching Gemini for table queries.
            if _table_refs and any(f"table {ref}" in clean.lower() or ref in clean.lower() for ref in _table_refs):
                _preview = clean[:1500].replace("\n", "\\n")
                self.log(f"[AuthorityQuery] >>> RELEVANT CHUNK FULL CONTENT ({label[:50]!r}, {len(clean)} chars):\n{_preview}")

            authority = source_uri.split("/")[-2] if "/" in source_uri else title
            if authority and authority not in sources_seen:
                sources_seen.append(authority)

        # Sources logged internally; raw chunk list not shown to user (too verbose)

        self.log(f"[AuthorityQuery] chunks assembled ({len(chunks_text)} chars), calling Gemini...")

        _fmt_map = {
            "brief":    "Answer in 2-3 sentences maximum. Lead with the direct answer; cite the supporting clause inline. No headings, no Key Takeaways section.",
            "detailed": "Give a thorough answer with full reasoning. Walk through the method step by step, show any formula or worked example, then attach a 'Supporting Clauses' section that quotes the relevant clauses/tables verbatim.",
            "standard": "Give a focused, direct answer to what the user actually asked. Use steps, a formula, or a calculation if that's what fits — not a wall of clauses. Cite supporting clauses inline.",
        }
        _tone_note = "Write in a precise, technical style." if _tone == "technical" else "Write in a clear, accessible style that a non-specialist can follow."
        _answer_preamble = ""
        if _goal:
            _answer_preamble += "**User's goal:** {}\n".format(_goal)
        _answer_preamble += "**Response format:** {} {}\n\n".format(_fmt_map.get(_detail, _fmt_map["standard"]), _tone_note)

        answer_prompt = (
            "You are a Singapore building authority code consultant with expertise across SCDF, URA, LTA, NEA, NPARKS, PUB, and other authorities.\n"
            "Your job is to ANSWER the user's question — not to dump excerpts. Read the retrieved clauses, understand what the user is actually trying to do, then give a direct, useful answer.{retry}\n\n"
            "{preamble}"
            "STEP 1 — UNDERSTAND THE INTENT\n"
            "Before writing anything, classify what the user is asking for:\n"
            "  • LOOKUP — \"what is the minimum X?\" → give the number/requirement, then cite the clause.\n"
            "  • HOW-TO — \"how do I count/calculate/measure X?\" → give the method as steps or a formula derived from the clauses. Do NOT just paste the clause and stop.\n"
            "  • CHECK — \"is N enough for situation Y?\" → apply the rule to their numbers and state pass/fail with reasoning.\n"
            "  • EXPLAIN — \"why does the code require X?\" → give the rationale and conditions in plain language.\n"
            "  • VERBATIM — \"show me Table X\" / \"quote clause Y\" → reproduce the table or clause faithfully.\n"
            "Match your response shape to the intent. A how-to answered as a list of clauses is a failed answer.\n\n"
            "STEP 2 — SYNTHESIZE, DON'T LIST\n"
            "- Lead with the direct answer (the steps, the formula, the number, the verdict).\n"
            "- If the user asked HOW to do something, derive a procedure from the rules: \"Step 1: measure X between Y and Z. Step 2: subtract any handrail projection. Step 3: compare against the table for your occupant load.\"\n"
            "- If a calculation is involved, show the formula and a worked example using the user's numbers when given.\n"
            "- Cite supporting clauses INLINE as evidence (e.g. \"...measured clear of handrails (**Clause 2.2.6**)\"), not as the main content.\n"
            "- Only reproduce a full table when the user asked for the table itself, or when the answer genuinely depends on multiple rows the user needs to see.\n\n"
            "READING THE EXCERPTS:\n"
            "- Tables in the excerpts may use pipe separators WITHOUT a `|---|` divider row. Treat any line with 3+ pipes as a table row.\n"
            "- A single excerpt may contain a table header followed by data rows further down — reconstruct the full Markdown table by adding a `|---|---|...` divider row between the header and the first data row.\n"
            "- The same table may also span multiple consecutive excerpts (e.g. header in one, rows in another). Merge them into one coherent Markdown table in your answer.\n"
            "- Cells that look mangled (extra blank cells, wrapped text, missing columns) are PDF→text artefacts. Reconstruct the most plausible row alignment based on column headers.\n"
            "- Ignore section headings like '# Two-way escape arrangement' that are PDF artefacts around the table.\n"
            "- If the excerpts genuinely do not contain enough to answer, say so explicitly — do NOT invent rules from outside knowledge.\n\n"
            "FORMATTING (response rendered as Markdown in a chat UI):\n"
            "1. Open with the direct answer — no preamble, no \"Based on the excerpts...\".\n"
            "2. Use numbered steps for procedures, **bold** for key values and clause numbers.\n"
            "3. Reproduce tables only when the intent is LOOKUP/VERBATIM and the table data is the answer.\n"
            "4. Footnote markers like `\\(^{{a}}\\)` are PDF artefacts — render as `(a)` or Unicode superscripts. Never emit literal `\\(`, `\\)`, `^`, or curly braces.\n\n"
            "Retrieved Excerpts:\n{}\n\n"
            "User question: {}\n\n"
            "Answer:"
        ).format(chunks_text, user_prompt, retry=_retry_note, preamble=_answer_preamble)

        answer = client.generate_content(answer_prompt, thinking_budget=0, temperature=0.1)
        self.log(f"[AuthorityQuery] Gemini answer len={len(answer) if answer else 0}")
        return answer.strip() if answer else "No answer could be generated from the retrieved excerpts."

    def _execute_delete(self, prompt_lower, classified=None):
        """Execute delete intent. Uses structured fields from classify_intent — no regex.
        classified: the dict returned by classify_intent (may be None on network failure).
        Returns a result string, or None to fall through to Gemini."""
        from revit_mcp import tool_logic as logic

        _tone = (classified or {}).get("tone", "conversational")
        _goal = (classified or {}).get("goal", "")
        _conv = _tone == "conversational"

        def _msg_all(count):
            if _conv:
                _fresh = any(w in _goal for w in ["fresh", "new", "start", "scratch", "clear", "rebuild"])
                _suffix = " — model is clear, ready for a fresh start." if _fresh else "."
                return "Done. Cleared {:,} element{}{}.".format(count, "s" if count != 1 else "", _suffix)
            return "Deleted {} elements from the model.".format(count)

        def _msg_cat(count, cat):
            if _conv:
                return "Done. Removed {:,} {} element{}.".format(count, cat, "s" if count != 1 else "")
            return "Deleted {} {} elements.".format(count, cat)

        def _msg_level(count, ls, le):
            _floor_label = "floors {}-{}".format(ls, le) if le and le != ls else "floor {}".format(ls)
            if _conv:
                return "Done. Cleared {:,} element{} from {}.".format(count, "s" if count != 1 else "", _floor_label)
            return "Deleted {} elements on floor(s) {}-{}.".format(count, ls, le or ls)

        # --- Classifier-driven path (primary) ---
        if classified is not None:
            scope       = classified.get("scope", "")
            category    = classified.get("category") or ""
            level_start = classified.get("level_start")
            level_end   = classified.get("level_end")

            if scope == "all":
                self.log("Dispatcher: Delete-all (scope=all) — clearing model.")
                result = mcp_event_handler.run_on_main_thread(logic.delete_all_elements_ui)
                self.log("Delete-all result: {}".format(result))
                return _msg_all(result.get("deleted_count", 0))

            if scope == "category" and category:
                params = {"category": category, "level_start": level_start, "level_end": level_end}
                desc = "category='{}'".format(category)
                if level_start is not None:
                    desc += ", floors {}-{}".format(level_start, level_end or level_start)
                self.log("Dispatcher: Delete category ({}).".format(desc))
                result = mcp_event_handler.run_on_main_thread(logic.delete_elements_by_filter_ui, params)
                self.log("Delete-category result: {}".format(result))
                if result.get("error"):
                    return "Delete failed: {}".format(result["error"])
                return _msg_cat(result.get("deleted_count", 0), category)

            if scope == "level" and level_start is not None:
                params = {"category": "", "level_start": level_start, "level_end": level_end}
                self.log("Dispatcher: Delete level (floors {}-{}).".format(level_start, level_end or level_start))
                result = mcp_event_handler.run_on_main_thread(logic.delete_elements_by_filter_ui, params)
                self.log("Delete-level result: {}".format(result))
                if result.get("error"):
                    return "Delete failed: {}".format(result["error"])
                return _msg_level(result.get("deleted_count", 0), level_start, level_end or level_start)

            # scope field missing or unrecognised — classifier returned delete_elements but no scope.
            # Treat as delete-all when no category/level, otherwise fall through to Gemini.
            if not category and level_start is None:
                self.log("Dispatcher: delete_elements with no scope/category — treating as delete-all.")
                result = mcp_event_handler.run_on_main_thread(logic.delete_all_elements_ui)
                return _msg_all(result.get("deleted_count", 0))

            self.log("Dispatcher: delete_elements — unresolved scope '{}', falling through to Gemini.".format(scope))
            return None

        # --- Regex fallback (only when classify_intent failed entirely) ---
        import re
        action_words = r"(?:delete|remove|clear|wipe|purge)"

        everything_patterns = [
            rf"{action_words}\s+(?:every\s*thing|all\b(?!\s+\w))",
            rf"{action_words}\s+(?:(?:the|all)\s+)?(?:model|building|project)",
            rf"clean\s+(?:the\s+)?(?:model|building|project)",
        ]
        for pat in everything_patterns:
            if re.search(pat, prompt_lower):
                result = mcp_event_handler.run_on_main_thread(logic.delete_all_elements_ui)
                return "Deleted {} elements from the model.".format(result.get("deleted_count", 0))

        category_names = r"(?:walls?|floors?|slabs?|columns?|doors?|windows?|roofs?|stairs?|staircases?|railings?|grids?|levels?)"
        partial = re.search(
            rf"{action_words}\s+(?:all\s+)?({category_names})(?:\s+(?:on|from|at)\s+(?:floor|level|storey)s?\s+(\d+)(?:\s*[-–to]+\s*(\d+))?)?",
            prompt_lower
        )
        if partial:
            cat = partial.group(1)
            ls  = int(partial.group(2)) if partial.group(2) else None
            le  = int(partial.group(3)) if partial.group(3) else None
            result = mcp_event_handler.run_on_main_thread(logic.delete_elements_by_filter_ui, {"category": cat, "level_start": ls, "level_end": le})
            if result.get("error"):
                return "Delete failed: {}".format(result["error"])
            return "Deleted {} {} elements.".format(result.get("deleted_count", 0), cat)

        level_only = re.search(
            rf"{action_words}\s+(?:everything|all)\s+(?:on|from|at)\s+(?:floor|level|storey)s?\s+(\d+)(?:\s*[-–to]+\s*(\d+))?",
            prompt_lower
        )
        if level_only:
            ls = int(level_only.group(1))
            le = int(level_only.group(2)) if level_only.group(2) else None
            result = mcp_event_handler.run_on_main_thread(logic.delete_elements_by_filter_ui, {"category": "", "level_start": ls, "level_end": le})
            if result.get("error"):
                return "Delete failed: {}".format(result["error"])
            return "Deleted {} elements on floor(s) {}-{}.".format(result.get("deleted_count", 0), ls, le or ls)

        return None

    def _try_intercept_options(self, prompt_lower, _classified=None):
        """Handle option/memory management commands using the pre-computed classification.

        _classified: the result already returned by classify_intent — never calls it again.
        Falls back to regex only when _classified is None (network/parse failure).
        Returns (result_string, classified_dict):
          - result_string is non-None when the prompt was handled (caller should return it).
          - classified_dict echoes _classified back to the caller.
        """
        import re
        mgr = get_options_manager()

        classified = _classified
        intent = classified.get("intent") if classified else None

        if intent == "list_options":
            self.log("Dispatcher: List-options intent.")
            _tone = (classified or {}).get("tone", "conversational")
            _list = mgr.list_options()
            if _tone == "conversational" and _list and not _list.startswith("No saved"):
                _list = "Here are your saved designs:\n\n" + _list
            return _list, classified

        if intent in ("use_option", "rollback"):
            opt_num = str(classified.get("option", ""))
            rev_num = str(classified.get("revision", "")) if classified.get("revision") else None
            if opt_num:
                self.log("Dispatcher: Use/rollback intent — option={}, revision={}".format(opt_num, rev_num))
                return self._execute_rollback(opt_num, rev_num), classified
            self.log("Dispatcher: {} intent missing option number — asking user to clarify.".format(intent))
            return ("Which option would you like to use?\n\n" + mgr.list_options()), classified

        if intent == "reorder_option":
            opt_num = str(classified.get("option", ""))
            tgt_pos = str(classified.get("tgt", ""))
            if opt_num and tgt_pos:
                self.log("Dispatcher: Reorder-option intent — option={} → position={}".format(opt_num, tgt_pos))
                ok, msg = mgr.reorder_option(opt_num, tgt_pos)
                return msg, classified
            self.log("Dispatcher: reorder_option intent missing option/target — asking user to clarify.")
            return ("Which option would you like to reorder, and to which position? "
                    "(e.g. 'move option 2 to position 1')\n\n" + mgr.list_options()), classified

        if intent == "delete_revision":
            opt_num = str(classified.get("option", ""))
            rev_num = str(classified.get("revision", ""))
            if opt_num and rev_num:
                self.log("Dispatcher: Delete-revision intent — option={}, rev={}".format(opt_num, rev_num))
                ok, msg = mgr.delete_revision(opt_num, rev_num)
                return msg, classified
            self.log("Dispatcher: delete_revision intent missing option/revision — asking user to clarify.")
            return ("Which revision would you like to delete? "
                    "Please specify both the option and revision number "
                    "(e.g. 'delete revision 2 of option 1').\n\n" + mgr.list_options()), classified

        if intent == "delete_all_options":
            self.log("Dispatcher: Delete-all-options intent.")
            count, msg = mgr.delete_all_options()
            return msg, classified

        if intent == "delete_option":
            opt_num = str(classified.get("option", ""))
            if opt_num:
                self.log("Dispatcher: Delete-option intent — option={}".format(opt_num))
                ok, msg = mgr.delete_option(opt_num)
                return msg, classified
            self.log("Dispatcher: delete_option intent missing option number — asking user to clarify.")
            return ("Which option would you like to delete? "
                    "(e.g. 'delete option 1')\n\n" + mgr.list_options()), classified

        if intent == "recreate_option":
            opt_num = str(classified.get("option", ""))
            rev_num = str(classified.get("revision", "")) if classified.get("revision") else None
            if opt_num:
                self.log("Dispatcher: Recreate-option intent — option={}, revision={}".format(opt_num, rev_num))
                return self._execute_rollback(opt_num, rev_num), classified
            self.log("Dispatcher: recreate_option intent missing option number — asking user to clarify.")
            return ("Which option would you like to recreate? "
                    "(e.g. 'recreate option 2')\n\n" + mgr.list_options()), classified

        if intent == "export_option":
            opt_num = str(classified.get("option", ""))
            rev_num = str(classified.get("revision", "")) if classified.get("revision") else None
            if not opt_num:
                mgr._ensure_loaded()
                current_opt_id = mgr._data.get("current_option_id") if mgr._data else None
                if current_opt_id:
                    m = re.search(r"(\d+)", current_opt_id)
                    opt_num = m.group(1) if m else current_opt_id
                    self.log("Dispatcher: Export-option — no option number given, resolved to current option {}".format(opt_num))
                else:
                    return "No active option found. Please build something first or specify an option number (e.g. 'export option 1').", classified
            if opt_num:
                self.log("Dispatcher: Export-option intent — option={}, rev={}".format(opt_num, rev_num))
                json_result = mgr.export_option_json(opt_num, rev_num)
                if json_result is None:
                    return "Option '{}' not found. Use 'list options' to see available options.".format(opt_num), classified
                return "Manifest JSON for option {}:\n```json\n{}\n```".format(opt_num, json_result), classified

        if intent == "move_to_revision":
            src_opt_num = str(classified.get("src_option", "")) or None
            tgt_opt_num = str(classified.get("tgt_option", ""))
            src_rev_num = str(classified.get("revision", "")) if classified.get("revision") else None
            if not src_opt_num:
                current_opt_id = mgr._data.get("current_option_id") if mgr._data else None
                if not current_opt_id:
                    mgr._ensure_loaded()
                    current_opt_id = mgr._data.get("current_option_id")
                if not current_opt_id:
                    return "No current option is active. Please specify which option to move.", classified
                m = re.search(r"(\d+)", current_opt_id)
                src_opt_num = m.group(1) if m else current_opt_id
            if tgt_opt_num:
                self.log("Dispatcher: Move-to-revision intent — src_opt={}, src_rev={}, tgt_opt={}".format(
                    src_opt_num, src_rev_num, tgt_opt_num))
                ok, msg = mgr.move_to_revision(src_opt_num, tgt_opt_num, src_rev_num)
                return msg, classified
            self.log("Dispatcher: move_to_revision intent missing target option — asking user to clarify.")
            return ("Which option should the revision be moved to? "
                    "(e.g. 'move revision 2 of option 1 to option 3')\n\n" + mgr.list_options()), classified

        if intent == "new_build":
            # Explicit "from scratch" / "start over" → clear context, fall through to Gemini
            explicit_scratch = re.search(
                r"\b(from\s+scratch|brand\s+new|start\s+over|start\s+fresh)\b", prompt_lower
            )
            if explicit_scratch:
                self.log("Dispatcher: Explicit scratch — clearing current option context.")
                mgr._ensure_loaded()
                mgr._data["current_option_id"] = None
                mgr._data["current_revision_id"] = None
                mgr._save()
                return None, classified  # fall through to Gemini
            # If the user described a specific building, build it directly — don't interrupt with the options menu.
            # Only show the options prompt for bare, unspecified requests (e.g. "create a building").
            is_specific = classified.get("specific", True)  # default True: when in doubt, build
            if not is_specific and mgr.has_options():
                prompt_str = mgr.get_new_build_prompt()
                if prompt_str:
                    self.log("Dispatcher: New-build intercepted (unspecified) — presenting saved options.")
                    return prompt_str, classified
            return None, classified  # fall through to Gemini

        # ── REGEX FALLBACK (classification failed or returned unhandled intent) ──
        # Only run when the intent is None (API failure or truly unclassified).
        # For known pass-through intents (build/query/delete_elements) skip regex.
        if intent is not None:
            return None, classified

        self.log("Dispatcher: classify_intent failed — falling back to regex.")

        list_patterns = [
            r"\b(list|show|display|what|get)\b.{0,30}\b(option|build|design|history|revision)s?\b",
            r"show\s+me\s+(my|all)?\s*(design|build|option|saved)",
            r"what\s+(design|build|option|revision)s?\s+(do|have)\s+i",
            r"what\s+have\s+i\s+(built|created|made)",
            r"how\s+many\s+(option|design|build|revision)s?",
            r"how\s+many\s+.{0,20}(option|design|build)s?\s+(are|do|have)",
        ]
        for pat in list_patterns:
            if re.search(pat, prompt_lower):
                self.log("Dispatcher (regex): List-options intent.")
                return mgr.list_options(), None

        rollback_match = re.search(
            r"(?:rollback|revert|restore|apply|go\s+back\s+to|go\s+to|switch\s+to|load|open|select|activate|set)\s+(?:to\s+)?(?:option|opt|design)[\s\-#]*(\d+)"
            r"(?:\s+(?:revision|rev|r)[\s\-#]*(\d+))?",
            prompt_lower
        )
        if rollback_match:
            self.log("Dispatcher (regex): Rollback intent.")
            return self._execute_rollback(rollback_match.group(1), rollback_match.group(2)), None

        reorder_match = re.search(
            r"(?:"
            r"(?:reorder|move|set|make|place)\s+(?:option|opt)[\s\-#]*(\d+)\s+"
            r"(?:to\s+(?:position|pos|slot|number|no\.?|#)?\s*|as\s+(?:option|opt)[\s\-#]*)(\d+)"
            r"|"
            r"(?:make|set)\s+(?:option|opt)[\s\-#]*(\d+)\s+(?:the\s+)?(?:first|1st)"
            r")",
            prompt_lower
        )
        if reorder_match:
            src = reorder_match.group(1) or reorder_match.group(3)
            tgt = reorder_match.group(2) or "1"
            self.log("Dispatcher (regex): Reorder-option intent.")
            ok, msg = mgr.reorder_option(src, tgt)
            return msg, None

        del_rev = re.search(
            r"(?:delete|remove)\s+(?:option|opt)[\s\-#]*(\d+)\s+(?:revision|rev|r)[\s\-#]*(\d+)",
            prompt_lower
        )
        if del_rev:
            ok, msg = mgr.delete_revision(del_rev.group(1), del_rev.group(2))
            return msg, None

        del_all = re.search(r"(?:delete|remove|clear|purge|wipe)\s+all\s+(?:option|design|build)s?", prompt_lower)
        if del_all:
            count, msg = mgr.delete_all_options()
            return msg, None

        del_opt = re.search(r"(?:delete|remove|purge)\s+(?:option|opt|design)[\s\-#]*(\d+)", prompt_lower)
        if del_opt:
            ok, msg = mgr.delete_option(del_opt.group(1))
            return msg, None

        use_match = re.search(
            r"\b(?:use|option|opt)[\s\-#]*(\d+)(?:\s+(?:revision|rev|r)[\s\-#]*(\d+))?",
            prompt_lower
        )
        if use_match:
            self.log("Dispatcher (regex): Use-option intent.")
            return self._execute_rollback(use_match.group(1), use_match.group(2)), None

        new_build_keywords = [r"\bcreate\b", r"\bbuild\b", r"\bgenerate\b", r"\bnew\s+building\b", r"\bmake\s+(a|me)\b"]
        if any(re.search(kw, prompt_lower) for kw in new_build_keywords):
            explicit_scratch = re.search(r"\b(from\s+scratch|brand\s+new|start\s+over|start\s+fresh)\b", prompt_lower)
            if explicit_scratch:
                mgr._ensure_loaded()
                mgr._data["current_option_id"] = None
                mgr._data["current_revision_id"] = None
                mgr._save()
                return None, None
            # In the regex fallback (Gemini classification failed), check if the prompt looks like a
            # bare unspecified request before showing the options menu. A prompt with any numbers,
            # typology words, or aesthetic descriptors is treated as specific → fall through to build.
            _specific_signals = [
                r"\d+\s*stor", r"\d+\s*floor", r"\d+\s*level",  # storey count
                r"\boffice\b", r"\bresidential\b", r"\bhotel\b", r"\bmixed.use\b", r"\bretail\b",  # typology
                r"\brandom", r"\borganic\b", r"\bcurv", r"\bmodern\b", r"\bsleek\b",  # aesthetics
                r"\btaper", r"\bcantilever\b", r"\bhourglass\b", r"\bsetback\b",  # form
            ]
            is_specific_regex = any(re.search(s, prompt_lower) for s in _specific_signals)
            if not is_specific_regex and mgr.has_options():
                prompt_str = mgr.get_new_build_prompt()
                if prompt_str:
                    return prompt_str, None

        return None, None

    def _execute_rollback(self, opt_num, rev_num=None):
        """Execute a rollback: delete all Revit elements, then re-apply the saved manifest.
        Returns a result string."""
        from revit_mcp import tool_logic as logic
        mgr = get_options_manager()

        manifest, resolved_opt_id, resolved_rev_id = mgr.get_manifest_for_rollback(opt_num, rev_num)
        if manifest is None:
            return "Option '{}'{} not found. Use 'list options' to see available options.".format(
                opt_num, " revision '{}'".format(rev_num) if rev_num else ""
            )

        self.log("Dispatcher: Rolling back to {} / {}".format(resolved_opt_id, resolved_rev_id))

        # Step 1: Delete all current Revit elements
        deleted_count = 0
        try:
            delete_result = mcp_event_handler.run_on_main_thread(logic.delete_all_elements_ui)
            if isinstance(delete_result, dict):
                deleted_count = delete_result.get("deleted_count", 0)
            self.log("Rollback: Deleted {} elements.".format(deleted_count))
        except Exception as e:
            self.log("Rollback: delete_all failed: {}".format(e))
            return "Rollback failed during model cleanup: {}".format(e)

        # Step 2: Re-apply the saved manifest via execute_fast_manifest
        try:
            def rollback_action():
                import Autodesk.Revit.DB as DB  # type: ignore  # noqa
                from revit_mcp.server import _get_revit_app
                uiapp = _get_revit_app()
                doc = uiapp.ActiveUIDocument.Document
                workers = RevitWorkers(doc, tracker=None)
                return workers.execute_fast_manifest(manifest)

            results = mcp_event_handler.run_on_main_thread(rollback_action)

            if isinstance(results, dict) and results.get("error"):
                return "Rollback build failed: {}".format(results["error"])

        except Exception as e:
            self.log("Rollback: execute_fast_manifest failed: {}".format(e))
            return "Rollback build failed: {}".format(e)

        # Step 3: Update current state pointer in build_options.json
        mgr.apply_rollback_state(resolved_opt_id, resolved_rev_id)

        # Step 4: Invalidate BIM cache
        self._cache_time = 0

        rev_label = " revision {}".format(resolved_rev_id) if resolved_rev_id else " (base)"
        return "Rolled back to option {}{} successfully. {} elements cleared, manifest re-applied.".format(
            resolved_opt_id, rev_label, deleted_count
        )

    def _report_design_parameters(self, manifest, presets, tracker, rag_source=None):
        """Format and stream design parameters + compliance numbers to the chat UI."""
        if not tracker:
            return

        typology = manifest.get("typology", "default")
        preset   = presets.get(typology) or presets.get("default") or {}
        cp       = manifest.get("compliance_parameters", {})
        setup    = manifest.get("project_setup", {})
        shell    = manifest.get("shell", {})
        lifts    = manifest.get("lifts", {})
        stairs   = manifest.get("staircases", {})

        lines = []

        # ── Typology ──────────────────────────────────────────────────────────
        lines.append("**Typology:** `{}`".format(typology))

        # ── Manifest shell ────────────────────────────────────────────────────
        lines.append("\n**Manifest — Building Shell:**")
        lvls = setup.get("levels", "?")
        lh   = setup.get("level_height", "?")
        shell_rows = [("Levels", str(lvls)), ("Typical level height", "{}mm".format(lh))]
        if shell.get("width") and shell.get("length"):
            shell_rows.append(("Footprint", "{}mm × {}mm".format(shell["width"], shell["length"])))
        if shell.get("column_spacing"):
            shell_rows.append(("Column grid", "{}mm".format(shell["column_spacing"])))
        if lifts.get("count"):
            shell_rows.append(("Lifts", str(lifts["count"])))
        if stairs.get("count"):
            shell_rows.append(("Staircases", str(stairs["count"])))
        lines.append("| Parameter | Value |")
        lines.append("|-----------|-------|")
        for _param, _val in shell_rows:
            lines.append("| {} | {} |".format(_param, _val))

        # ── Design parameters from preset ─────────────────────────────────────
        bd  = preset.get("building_defaults", {})
        cl  = preset.get("core_logic", {})
        pr  = preset.get("program_requirements", {})
        col = preset.get("column_logic", {})
        if bd or cl or pr or col:
            lines.append("\n**Design Parameters (preset: `{}`):**".format(typology))
            dp_rows = []
            if bd.get("typical_floor_height"):
                dp_rows.append(("Typical floor height", "{}mm".format(bd["typical_floor_height"])))
            if bd.get("first_storey_floor_height"):
                dp_rows.append(("Ground floor height", "{}mm".format(bd["first_storey_floor_height"])))
            if bd.get("clear_ceiling_height"):
                dp_rows.append(("Clear ceiling height", "{}mm".format(bd["clear_ceiling_height"])))
            if col.get("span"):
                dp_rows.append(("Column span range", "{}–{}mm".format(col["span"][0], col["span"][1])))
            if col.get("offset_from_edge"):
                dp_rows.append(("Column offset from edge", "{}mm".format(col["offset_from_edge"])))
            if pr.get("minimum_distance_facade_to_core"):
                dp_rows.append(("Min facade-to-core depth", "{}mm".format(pr["minimum_distance_facade_to_core"])))
            if pr.get("core_area_ratio"):
                lo, hi = pr["core_area_ratio"]
                dp_rows.append(("Core area ratio", "{:.0f}–{:.0f}%".format(lo * 100, hi * 100)))
            if pr.get("occupancy_load_factor"):
                dp_rows.append(("Occupancy load factor", "{} m²/person".format(pr["occupancy_load_factor"])))
            if cl.get("lift_waiting_time"):
                dp_rows.append(("Target lift waiting time", "{}s".format(cl["lift_waiting_time"])))
            if cl.get("lift_lobby_width"):
                dp_rows.append(("Lift lobby width", "{}mm".format(cl["lift_lobby_width"])))
            if cl.get("lift_shaft_size"):
                sz = cl["lift_shaft_size"]
                dp_rows.append(("Lift shaft size", "{}×{}mm".format(sz[0], sz[1])))
            if cl.get("fire_lobby_std_depth"):
                dp_rows.append(("Fire lobby std depth", "{}mm".format(cl["fire_lobby_std_depth"])))
            sc = cl.get("staircase_spec", {})
            if sc:
                dp_rows.append(("Staircase riser", "{}mm".format(sc.get("riser", "?"))))
                dp_rows.append(("Staircase tread", "{}mm".format(sc.get("tread", "?"))))
                dp_rows.append(("Staircase flight width", "{}mm".format(sc.get("width_of_flight", "?"))))
                dp_rows.append(("Staircase landing width", "{}mm".format(sc.get("landing_width", "?"))))
            if dp_rows:
                lines.append("| Parameter | Value |")
                lines.append("|-----------|-------|")
                for _param, _val in dp_rows:
                    lines.append("| {} | {} |".format(_param, _val))

        # ── Authority compliance parameters (embedded in manifest) ─────────────
        if cp:
            src_label = " [source: {}]".format(rag_source) if rag_source else ""
            lines.append("\n**Authority Compliance Parameters (manifest `compliance_parameters`{}):**".format(src_label))
            _labels = {
                "max_travel_distance_mm":    ("Max travel distance",  "mm"),
                "stair_riser_mm":            ("Stair riser",          "mm"),
                "stair_tread_mm":            ("Stair tread",          "mm"),
                "stair_flight_width_mm":     ("Stair flight width",   "mm"),
                "stair_landing_width_mm":    ("Stair landing width",  "mm"),
                "stair_headroom_mm":         ("Stair headroom",       "mm"),
                "stair_overrun_mm":          ("Stair overrun",        "mm"),
                "fire_lobby_min_area_mm2":   ("Fire lobby min area",  "m²"),
                "smoke_lobby_min_area_mm2":  ("Smoke lobby min area", "m²"),
                "smoke_lobby_min_depth_mm":  ("Smoke lobby min depth","mm"),
                "fire_lift_car_size_mm":     ("Fire lift car size",   "mm"),
                "lift_wall_thickness_mm":    ("Lift shaft wall",      "mm"),
                "std_wall_thickness_mm":     ("Std wall thickness",   "mm"),
                "lift_speed_m_s":            ("Lift speed",           "m/s"),
                "lift_door_time_s":          ("Lift door time",       "s"),
                "lift_transfer_time_s":      ("Lift transfer time",   "s"),
                "lift_peak_demand_fraction": ("Peak demand fraction", ""),
                "lift_interval_s":           ("Lift interval period", "s"),
                "lift_occupants_per_lift":   ("Occupants per lift",   ""),
            }
            lines.append("| Parameter | Value |")
            lines.append("|-----------|-------|")
            for k, v in cp.items():
                label, unit = _labels.get(k, (k, ""))
                if unit == "m²" and isinstance(v, (int, float)):
                    display = "{:.1f} m²".format(v / 1_000_000)
                elif unit:
                    display = "{} {}".format(v, unit)
                else:
                    display = str(v)
                lines.append("| {} | {} |".format(label, display))
        else:
            lines.append("\n*No `compliance_parameters` block in manifest — Gemini did not embed compliance values.*")

        tracker.report("\n".join(lines))

    def _trim_to_balanced_braces(self, s):
        """Return the substring from the first '{' to the matching closing '}', or None.

        Honours strings and escapes so braces inside string literals don't throw off the
        depth counter. Used by _extract_json to drop trailing text after the JSON object.
        """
        start = s.find("{")
        if start == -1:
            return None
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(s)):
            ch = s[i]
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start:i+1].strip()
        return None

    def _extract_json(self, text):
        # Log tail of response to diagnose extraction failures
        self.log("_extract_json: text len={}, tail={}".format(
            len(text), repr(text[-200:]) if len(text) > 200 else repr(text)))

        # Only match explicit ```json fences (case-insensitive).
        # Do NOT match bare ``` fences — they may contain ASCII diagrams or tables.
        import re as _re
        fence_match = _re.search(r"```[Jj][Ss][Oo][Nn]\s*\n([\s\S]*?)```", text)
        if fence_match:
            candidate = fence_match.group(1).strip()
            if candidate and candidate.startswith("{"):
                # Trim any trailing non-JSON text after the closing brace (e.g. when
                # Gemini puts an <architectural_intent> block inside the fence). Run a
                # brace-depth scan to find the real end of the JSON object.
                trimmed = self._trim_to_balanced_braces(candidate)
                if trimmed is not None and trimmed != candidate:
                    self.log("_extract_json: trimmed trailing text inside fence ({} -> {} chars)".format(
                        len(candidate), len(trimmed)))
                    return trimmed
                self.log("_extract_json: extracted via ```json fence ({} chars)".format(len(candidate)))
                return candidate

        # Robustness: Remove common hallucinated wrappers
        data = text.strip()
        if data.startswith("orchestrate_build(") and data.endswith(")"):
            data = data[len("orchestrate_build("):-1].strip()
        if data.startswith("edit_entire_building_dimensions(") and data.endswith(")"):
            data = data[len("edit_entire_building_dimensions("):-1].strip()

        # Final Fallback: depth-counter brace search so trailing text after the JSON
        # object does not cause json.loads "Extra data" errors (rfind grabbed too far).
        try:
            start = data.find("{")
            if start != -1:
                depth = 0
                end = -1
                in_string = False
                escape_next = False
                for i in range(start, len(data)):
                    ch = data[i]
                    if escape_next:
                        escape_next = False
                        continue
                    if ch == "\\" and in_string:
                        escape_next = True
                        continue
                    if ch == '"':
                        in_string = not in_string
                        continue
                    if in_string:
                        continue
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end = i
                            break
                if end != -1:
                    candidate = data[start:end+1].strip()
                    self.log("_extract_json: extracted via brace-depth search ({} chars)".format(len(candidate)))
                    return candidate
        except Exception:
            pass

        self.log("_extract_json: FAILED — no JSON found in response")
        return data.strip()

    def _run_rag_block(self, user_prompt, tracker, build_classified):
        """Run the entire RAG retrieval pipeline (saved-snap fast path,
        in-session cache hit, Vertex retrieval + retry validation, persist
        to disk).  Returns ``(rag_rules, _stored_compliance_snapshot)``.

        Extracted from inline ``_orchestrate`` so it can be invoked either
        synchronously (single-pass builds) or in a daemon thread concurrent
        with Pass 1 (two-pass new_build).  Body is the original inline code
        with ``rag_rules`` / ``_stored_compliance_snapshot`` returned at the
        end instead of left as outer locals.
        """
        import time as _time
        rag_rules = None
        _stored_compliance_snapshot = None  # set when we reuse saved compliance
        _build_intent_name = (build_classified or {}).get("intent")
        _needs_rag = _build_intent_name in ("build", "new_build", None)  # None = classification failed, be safe
        try:
            from revit_mcp.config import RAG_ENABLED
            self.log(f"[RAG] RAG_ENABLED={RAG_ENABLED}")
            if RAG_ENABLED:
                if not _needs_rag:
                    self.log("[RAG] Skipping — intent is '{}', not a design operation".format(_build_intent_name))
                else:
                    # Check if the current option already has saved compliance — skip RAG if so
                    _saved_rag, _saved_snap = None, None
                    try:
                        _mgr_early = get_options_manager()
                        _mgr_early._ensure_loaded()
                        _cur_opt = _mgr_early._data.get("current_option_id")
                        _cur_rev = _mgr_early._data.get("current_revision_id")
                        if _cur_opt:
                            _saved_rag, _saved_snap = _mgr_early.get_cached_compliance(_cur_opt, _cur_rev)
                    except Exception as _ce:
                        self.log(f"[RAG] Compliance cache lookup failed: {_ce}")

                    # Detect large storey change — compliance must be re-fetched
                    _large_storey_change = False
                    if _saved_snap:
                        import re as _re2
                        _storey_nums = _re2.findall(r'\b(\d+)\s*stor', user_prompt.lower())
                        if _storey_nums:
                            try:
                                _cur_opt_obj = _mgr_early._find_option(_cur_opt) if _cur_opt else None
                                _cur_levels = (_cur_opt_obj or {}).get("manifest", {}).get(
                                    "project_setup", {}).get("levels", 0) if _cur_opt_obj else 0
                                _req_levels = int(_storey_nums[-1])
                                if abs(_req_levels - _cur_levels) >= 10:
                                    _large_storey_change = True
                                    self.log(f"[RAG] Large storey change detected ({_cur_levels} → {_req_levels}) — invalidating compliance cache")
                            except Exception:
                                pass

                    if _saved_snap and not _large_storey_change:
                        self.log(f"[RAG] Using saved compliance from option {_cur_opt} — skipping RAG")
                        if tracker: tracker.set_status("Using saved authority codes from previous build...")
                        rag_rules = _saved_rag
                        _stored_compliance_snapshot = _saved_snap
                        if tracker and rag_rules:
                            from revit_mcp.agents.sub_agent import format_rules_for_display
                            _table = format_rules_for_display(rag_rules)
                            if _table:
                                tracker.report(
                                    "📋 **Authority codes (reused from previous build option):**\n\n" + _table,
                                    is_narrative=True,
                                )
                            else:
                                tracker.report("📋 Authority codes reused from previous build option.", is_narrative=True)
                    else:
                        from revit_mcp.agents.main_agent import extract_intent, enrich_intent
                        from revit_mcp.agents.sub_agent import run_retrieve_rules
                        self.log("[RAG] Extracting building intent (regex)...")
                        intent = extract_intent(user_prompt)
                        self.log(f"[RAG] Intent extracted: {intent}")
                        if tracker: tracker.set_status("Sub-agent: classifying building occupancy and scope...")
                        intent = enrich_intent(intent, user_prompt, log_fn=lambda m: self.log(f"[RAG] {m}"))
                        self.log(f"[RAG] Intent enriched: pg={intent.get('purpose_group')} occupancy={intent.get('occupancy_class')} band={intent.get('height_band')} sprinklered={intent.get('sprinklered')} exclude={intent.get('exclude_pg')}")
                        cache_key = (intent.get("building_type"), intent.get("storeys"))
                        if cache_key in self._rag_cache:
                            rag_rules = self._rag_cache[cache_key]
                            self.log(f"[RAG] Cache hit for {cache_key} — reusing rules")
                            if tracker: tracker.set_status("Authority codes loaded from cache...")
                            if tracker and rag_rules:
                                from revit_mcp.agents.sub_agent import format_rules_for_display
                                _table = format_rules_for_display(rag_rules)
                                if _table:
                                    tracker.report(
                                        "📋 **Authority codes (cached from earlier this session):**\n\n" + _table,
                                        is_narrative=True,
                                    )
                                else:
                                    tracker.report("📋 Authority codes loaded from in-session cache.", is_narrative=True)
                        else:
                            _btype = intent.get("building_type", "building")
                            _nstoreys = intent.get("storeys", "?")
                            _topics = intent.get("topics", [])
                            if tracker: tracker.set_status(
                                "Sub-agent: querying authority code library ({}, {} storeys)...".format(_btype, _nstoreys))
                            self.log(f"[RAG] Building intent: {_btype}, {_nstoreys} storeys, topics: {_topics}")
                            _t_rag = _time.time()
                            self.log("[RAG] Calling run_retrieve_rules...")
                            rag_rules = run_retrieve_rules(
                                intent,
                                report=None,
                                set_status=tracker.set_status if tracker else None,
                            )
                            self.log(f"[RAG] run_retrieve_rules returned in {_time.time()-_t_rag:.2f}s — result={rag_rules}")

                            # ── RAG VALIDATION + RETRY ──────────────────────────────────────────
                            # Keys that MUST be present before passing compliance to Gemini.
                            # Structured as {topic: [required_keys]} so we know which topics to retry.
                            _REQUIRED_RAG_KEYS = {
                                "staircase":        ["min_count", "min_flight_width_mm", "max_travel_distance_mm"],
                                "occupant_load":    ["occupant_load_factor_m2"],
                                "exit_width":       ["persons_per_unit_width", "exit_width_per_unit_mm"],
                                "travel_distance":  ["max_travel_distance_mm"],
                                "corridor":         ["min_corridor_width_mm"],
                                "smoke_stop_lobby": ["min_area_mm2", "min_width_mm"],
                            }
                            if intent.get("storeys", 1) > 4:
                                _REQUIRED_RAG_KEYS["fire_lift_lobby"] = ["min_area_mm2", "min_width_mm"]

                            _retrieved_rules = (rag_rules or {}).get("rules", {})
                            _missing_topics = []
                            for _topic, _keys in _REQUIRED_RAG_KEYS.items():
                                _topic_data = _retrieved_rules.get(_topic, {})
                                for _key in _keys:
                                    _val = _topic_data.get(_key)
                                    _has_val = (
                                        _val is not None
                                        and (not isinstance(_val, dict) or _val.get("dimension") is not None)
                                    )
                                    if not _has_val:
                                        if _topic not in _missing_topics:
                                            _missing_topics.append(_topic)
                                        self.log(f"[RAG] Missing required key: {_topic}.{_key}")

                            if _missing_topics:
                                self.log(f"[RAG] Retrying {len(_missing_topics)} topic(s) with missing keys: {_missing_topics}")
                                _retry_intent = dict(intent, topics=_missing_topics)
                                _t_retry = _time.time()
                                _retry_rules = run_retrieve_rules(_retry_intent)
                                self.log(f"[RAG] Retry done in {_time.time()-_t_retry:.2f}s — result={_retry_rules}")

                                # Merge retry results — STRICTLY scoped:
                                # 1. Only consider topics in _missing_topics (don't introduce new top-level topics).
                                # 2. Only fill keys that are in _REQUIRED_RAG_KEYS for that topic AND were missing.
                                # 3. Never overwrite a key that already has a valid value — that protects against
                                #    the retry returning a less-strict variant (e.g. staircase.min_count flipping 2→1).
                                if rag_rules is None:
                                    rag_rules = {"authority": (_retry_rules or {}).get("authority", "SCDF"), "rules": {}}
                                if _retry_rules and _retry_rules.get("rules"):
                                    _retry_topics = _retry_rules.get("rules", {})
                                    _retry_added = []
                                    _retry_skipped_topics = [t for t in _retry_topics if t not in _missing_topics]
                                    if _retry_skipped_topics:
                                        self.log(f"[RAG] Retry returned out-of-scope topics, ignoring: {_retry_skipped_topics}")
                                    for _rt in _missing_topics:
                                        _rv = _retry_topics.get(_rt)
                                        if not _rv:
                                            continue
                                        _existing_topic = rag_rules["rules"].setdefault(_rt, {})
                                        # Only the required keys for this topic — synthesis may surface other
                                        # keys but they aren't authoritative enough to merge in via retry.
                                        _required_for_topic = set(_REQUIRED_RAG_KEYS.get(_rt, []))
                                        for _rk in _required_for_topic:
                                            _rval = _rv.get(_rk)
                                            _is_present = (
                                                _rval is not None
                                                and (not isinstance(_rval, dict) or _rval.get("dimension") is not None)
                                            )
                                            if not _is_present:
                                                continue
                                            _existing_val = _existing_topic.get(_rk)
                                            _existing_present = (
                                                _existing_val is not None
                                                and (not isinstance(_existing_val, dict) or _existing_val.get("dimension") is not None)
                                            )
                                            if _existing_present:
                                                # Don't clobber an already-valid value — first synthesis wins.
                                                continue
                                            _existing_topic[_rk] = _rval
                                            _retry_added.append(f"{_rt}.{_rk}")
                                        # Also merge "source" if missing
                                        if _rv.get("source") and not _existing_topic.get("source"):
                                            _existing_topic["source"] = _rv["source"]
                                    self.log(f"[RAG] Retry merge filled {len(_retry_added)} missing keys: {_retry_added}")

                                # Cross-topic mirroring: Table 2.2A's max travel distance is a building-wide
                                # value retrieved under the travel_distance topic but the build pipeline also
                                # expects it under staircase.max_travel_distance_mm. Mirror it across when the
                                # source-of-truth topic has a value and the destination doesn't.
                                _post_rules = (rag_rules or {}).get("rules", {})
                                _MIRROR_RULES = [
                                    ("travel_distance", "max_travel_distance_mm",            "staircase", "max_travel_distance_mm"),
                                    ("travel_distance", "max_travel_distance_sprinklered_mm", "staircase", "max_travel_distance_sprinklered_mm"),
                                ]
                                for _src_t, _src_k, _dst_t, _dst_k in _MIRROR_RULES:
                                    _src_val = _post_rules.get(_src_t, {}).get(_src_k)
                                    _src_present = (
                                        _src_val is not None
                                        and (not isinstance(_src_val, dict) or _src_val.get("dimension") is not None)
                                    )
                                    if not _src_present:
                                        continue
                                    _dst_topic = _post_rules.setdefault(_dst_t, {})
                                    _dst_val = _dst_topic.get(_dst_k)
                                    _dst_present = (
                                        _dst_val is not None
                                        and (not isinstance(_dst_val, dict) or _dst_val.get("dimension") is not None)
                                    )
                                    if not _dst_present:
                                        _dst_topic[_dst_k] = _src_val
                                        self.log(f"[RAG] Mirrored {_src_t}.{_src_k} -> {_dst_t}.{_dst_k}")

                                # Log final state of required keys after retry
                                _final_rules = (rag_rules or {}).get("rules", {})
                                _still_missing = []
                                for _topic, _keys in _REQUIRED_RAG_KEYS.items():
                                    _topic_data = _final_rules.get(_topic, {})
                                    for _key in _keys:
                                        _val = _topic_data.get(_key)
                                        _has_val = (
                                            _val is not None
                                            and (not isinstance(_val, dict) or _val.get("dimension") is not None)
                                        )
                                        if not _has_val:
                                            _still_missing.append(f"{_topic}.{_key}")
                                if _still_missing:
                                    self.log(f"[RAG] After retry, still missing: {_still_missing} — Gemini will use static fallback for these")
                                else:
                                    self.log("[RAG] All required keys present after retry ✓")
                            # ── END RAG VALIDATION + RETRY ─────────────────────────────────────

                            if rag_rules:
                                topics_found = list(rag_rules.get("rules", {}).keys())
                                self.log(f"[RAG] Rules retrieved for topics: {topics_found}")
                                self._rag_cache[cache_key] = rag_rules
                                self.log(f"[RAG] Cached rules under key {cache_key}")
                                # Persist to disk so the cache survives Revit
                                # restarts.  Best-effort — failure here doesn't
                                # break the build (in-memory cache still works).
                                self._save_rag_cache_to_disk()
                                if tracker:
                                    from revit_mcp.agents.sub_agent import format_rules_for_display
                                    _table = format_rules_for_display(rag_rules)
                                    if _table:
                                        tracker.report("✅ **Authority codes resolved:**\n\n" + _table, is_narrative=True)
                                    else:
                                        tracker.report("✅ Authority codes resolved.", is_narrative=True)
        except Exception as _rag_err:
            self.log(f"[RAG] FAILED — {type(_rag_err).__name__}: {_rag_err}")
            rag_rules = None
        return rag_rules, _stored_compliance_snapshot


orchestrator = Orchestrator()
