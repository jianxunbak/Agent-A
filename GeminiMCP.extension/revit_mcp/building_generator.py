# -*- coding: utf-8 -*-
try:
    import clr
    clr.AddReference('RevitAPI')
    from Autodesk.Revit.DB import * # type: ignore
    import System
except:
    pass

import math
from revit_mcp.state_manager import state_manager

from revit_mcp.utils import safe_num, mm_to_ft, load_presets
from . import lift_logic
from . import staircase_logic
from . import fire_safety_logic
from .spatial_registry import SpatialRegistry

def get_model_registry(doc, zone_bbox=None):
    """
    ULTRA-HIGH-SPEED SCAN: Search for AI-managed elements using Extensible Storage filters.
    """
    import Autodesk.Revit.DB as DB # type: ignore
    from .state_manager import state_manager
    registry = {}
    
    # 1. Primary Scan: Extensible Storage Filter (Very fast)
    try:
        schema = state_manager.get_schema()
        es_filter = DB.ExtensibleStorage.ExtensibleStorageFilter(schema.GUID)
        collector = DB.FilteredElementCollector(doc).WherePasses(es_filter)
        
        if zone_bbox:
            buffer = mm_to_ft(1000)
            outline = DB.Outline(
                DB.XYZ(zone_bbox.Min.X - buffer, zone_bbox.Min.Y - buffer, zone_bbox.Min.Z - buffer),
                DB.XYZ(zone_bbox.Max.X + buffer, zone_bbox.Max.Y + buffer, zone_bbox.Max.Z + buffer)
            )
            collector.WherePasses(DB.BoundingBoxIntersectsFilter(outline))

        for el in collector:
            metadata = state_manager.get_ai_metadata(el)
            if metadata:
                registry[metadata['ai_id']] = el.Id
    except Exception:
        pass

    # 2. Comment Tag Scan: RevitWorkers sets Comments (not ES) on walls/columns/floors,
    #    so this scan must ALWAYS run to find those elements during edits.
    cats = [DB.BuiltInCategory.OST_Walls, DB.BuiltInCategory.OST_Floors, DB.BuiltInCategory.OST_Levels,
            DB.BuiltInCategory.OST_Grids, DB.BuiltInCategory.OST_Columns, DB.BuiltInCategory.OST_StructuralColumns]
    net_cats = System.Collections.Generic.List[DB.BuiltInCategory]()
    for c in cats: net_cats.Add(c)
    filter = DB.ElementMulticategoryFilter(net_cats)
    col = DB.FilteredElementCollector(doc).WherePasses(filter).WhereElementIsNotElementType()
    for el in col:
        p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if not p: p = el.LookupParameter("Comments")
        if p and p.HasValue:
            val = p.AsString()
            if val.startswith("AI_") and val not in registry:
                registry[val] = el.Id
    
    return registry
class BuildingSystem:
    def __init__(self, doc):
        self.doc = doc
        self.spatial_registry = SpatialRegistry()

    def sync_manifest(self, manifest):
        """State-aware manifest execution with high-speed transaction batching."""
        # Immediate self-log for the worker
        def worker_log(msg):
            from .runner import log
            log("[BuildingSystem] {}".format(msg))
            
        worker_log("Starting sync_manifest execution...")
        import Autodesk.Revit.DB as DB # type: ignore
        from revit_mcp.building_generator import get_model_registry
        from revit_mcp.utils import mm_to_ft, safe_num, setup_failure_handling, nuclear_lockdown
        
        # 0. NUCLEAR LOCKDOWN: Forcefully disjoint all walls before any sync moves
        nuclear_lockdown(self.doc)
        
        results = []
        self.registry = get_model_registry(self.doc)
        
        tg = DB.TransactionGroup(self.doc, "AI Sync: Building Manifest")
        tg.Start()
        
        try:
            # --- PRE-PROCESSING: High-Level to Low-Level expansion ---
            if "project_setup" in manifest or "shell" in manifest:
                worker_log("Expanding high-level architectural manifest...")
                manifest = self._expand_high_level_manifest(manifest)
            
            # --- PHASE 1: LEVELS ---
            worker_log("PHASE 1: Syncing Levels...")
            t = DB.Transaction(self.doc, "AI Sync: Levels")
            t.Start()
            from revit_mcp.utils import setup_failure_handling
            setup_failure_handling(t, use_nuclear=True)
            level_map = {} # AI_ID -> Revit Level
            levels_data = manifest.get('levels', [])
            for l_data in levels_data:
                lvl = self._sync_level(l_data)
                level_map[l_data['id']] = lvl
                results.append({"type": "level", "id": l_data['id'], "revit_id": str(lvl.Id.Value)})
            t.Commit()
            worker_log("Levels synced: {}.".format(len(level_map)))
            
            # --- PHASE 2: SHELL (Walls & Floors) ---
            worker_log("PHASE 2: Syncing Shell (Walls & Floors)...")
            t = DB.Transaction(self.doc, "AI Sync: Shell")
            t.Start()
            from revit_mcp.utils import setup_failure_handling
            setup_failure_handling(t, use_nuclear=True)
            walls_count = 0
            for w_data in manifest.get('walls', []):
                wall = self._sync_wall(w_data, level_map)
                results.append({"type": "wall", "id": w_data['id'], "revit_id": str(wall.Id.Value)})
                walls_count += 1
                
            floors_count = 0
            for f_data in manifest.get('floors', []):
                floor = self._sync_floor(f_data, level_map)
                results.append({"type": "floor", "id": f_data['id'], "revit_id": str(floor.Id.Value)})
                floors_count += 1
                
            t.Commit()
            worker_log("Shell synced: {} walls, {} floors.".format(walls_count, floors_count))
            
            # --- PHASE 3: COLUMNS ---
            worker_log("PHASE 3: Syncing Columns...")
            t = DB.Transaction(self.doc, "AI Sync: Columns")
            t.Start()
            from revit_mcp.utils import setup_failure_handling
            setup_failure_handling(t, use_nuclear=True)
            cols_count = 0
            for c_data in manifest.get('columns', []):
                col = self._sync_column(c_data, level_map)
                if col:
                    results.append({"type": "column", "id": c_data['id'], "revit_id": str(col.Id.Value)})
                    cols_count += 1
            t.Commit()
            worker_log("Columns synced: {}.".format(cols_count))
            
            tg.Assimilate()
            worker_log("sync_manifest SUCCESS.")
            return {"status": "Success", "elements": results}
            
        except Exception as e:
            import traceback
            err_trace = traceback.format_exc()
            worker_log("sync_manifest FAILED: {}\n{}".format(str(e), err_trace))
            tg.RollBack()
            return {"status": "Error", "message": str(e)}

    def _sync_level(self, data):
        import Autodesk.Revit.DB as DB # type: ignore
        ai_id = data['id']
        elev = mm_to_ft(data['elevation'])
        name = data.get('name', ai_id)
        
        lvl = None
        if ai_id in self.registry:
            lvl = self.doc.GetElement(self.registry[ai_id])
            
        if not lvl:
            lvl = DB.Level.Create(self.doc, elev)
            state_manager.set_ai_metadata(lvl, ai_id)
        
        if lvl.Elevation != elev: lvl.Elevation = elev
        if lvl.Name != name:
            try: lvl.Name = name
            except: pass
            
        return lvl

    def _sync_wall(self, data, level_map):
        import Autodesk.Revit.DB as DB # type: ignore
        ai_id = data['id']
        start = data['start'] # [x, y, z]
        end = data['end']     # [x, y, z]
        
        p1 = DB.XYZ(mm_to_ft(start[0]), mm_to_ft(start[1]), mm_to_ft(start[2]))
        p2 = DB.XYZ(mm_to_ft(end[0]), mm_to_ft(end[1]), mm_to_ft(end[2]))
        line = DB.Line.CreateBound(p1, p2)
        
        level = level_map.get(data.get('level_id'))
        if not level:
            level = DB.FilteredElementCollector(self.doc).OfClass(DB.Level).FirstElement()
            
        wall = None
        if ai_id in self.registry:
            wall = self.doc.GetElement(self.registry[ai_id])
            
            # Update existing
            from revit_mcp.utils import disallow_joins
            # PRE-MOVE LOCK
            disallow_joins(wall)
            wall.Location.Curve = line
            # POST-MOVE RE-ENFORCE
            disallow_joins(wall)
        else:
            # Create new
            wall = DB.Wall.Create(self.doc, line, level.Id, False)
            from revit_mcp.utils import disallow_joins
            # POST-CREATION LOCK
            disallow_joins(wall)
            state_manager.set_ai_metadata(wall, ai_id)
            
        # Set Type if specified
        if data.get('type'):
            wt = self._find_type(DB.BuiltInCategory.OST_Walls, data['type'])
            if wt: wall.WallType = wt
            
        # Set Height if specified (for overruns, etc.)
        if data.get('height'):
            h_ft = mm_to_ft(data['height'])
            # Disconnect from top level if height is literal
            p_top = wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE)
            if p_top: p_top.Set(DB.ElementId.InvalidElementId)
            p_h = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
            if p_h: p_h.Set(h_ft)
            
        return wall

    @staticmethod
    def _build_curve_loop(pts):
        """Build a DB.CurveLoop from a points list.
        Each point is either [x_mm, y_mm] (straight segment to next)
        or [x_mm, y_mm, {"mid_x": mx, "mid_y": my}] (arc segment to next).
        """
        import Autodesk.Revit.DB as DB # type: ignore
        loop = DB.CurveLoop()
        n = len(pts)
        for i in range(n):
            raw1 = pts[i]
            raw2 = pts[(i + 1) % n]
            p1 = DB.XYZ(mm_to_ft(raw1[0]), mm_to_ft(raw1[1]), 0)
            p2 = DB.XYZ(mm_to_ft(raw2[0]), mm_to_ft(raw2[1]), 0)
            arc_data = raw1[2] if len(raw1) > 2 else None
            if arc_data and isinstance(arc_data, dict):
                pm = DB.XYZ(mm_to_ft(arc_data['mid_x']), mm_to_ft(arc_data['mid_y']), 0)
                loop.Append(DB.Arc.Create(p1, p2, pm))
            else:
                loop.Append(DB.Line.CreateBound(p1, p2))
        return loop

    def _sync_floor(self, data, level_map):
        import Autodesk.Revit.DB as DB # type: ignore
        import System.Collections.Generic as Generic # type: ignore
        ai_id = data['id']
        points = data['points'] # [[x,y], [x,y], ...] or [[x,y,{mid}], ...]
        level = level_map.get(data.get('level_id'))

        loops = Generic.List[DB.CurveLoop]()
        loops.Add(self._build_curve_loop(points)) # Outer boundary

        if 'voids' in data:
            for void_pts in data['voids']:
                loops.Add(self._build_curve_loop(void_pts))
                
        floor = None
        
        if ai_id in self.registry:
            # Floors are tricky to update geometry (requires Sketch edit), 
            # for the AI manifest, we delete and recreate for stability if geometry changes.
            existing = self.doc.GetElement(self.registry[ai_id])
            if existing: self.doc.Delete(existing.Id)
            
        ft = DB.FilteredElementCollector(self.doc).OfClass(DB.FloorType).FirstElement()
        if data.get('type'):
            ft_match = self._find_type(DB.BuiltInCategory.OST_Floors, data['type'])
            if ft_match: ft = ft_match
            
        floor = DB.Floor.Create(self.doc, loops, ft.Id, level.Id)
        state_manager.set_ai_metadata(floor, ai_id)
        return floor

    def _sync_column(self, data, level_map):
        import Autodesk.Revit.DB as DB # type: ignore
        ai_id = data['id']
        loc_raw = data['location'] # [x, y, z]
        p = DB.XYZ(mm_to_ft(loc_raw[0]), mm_to_ft(loc_raw[1]), mm_to_ft(loc_raw[2]))
        
        level = level_map.get(data.get('level_id'))
        top_level = level_map.get(data.get('top_level_id'))
        
        # Find structural column symbol
        symbol = self._find_type(DB.BuiltInCategory.OST_StructuralColumns, data.get('type', "Column"))
        if not symbol:
            symbol = DB.FilteredElementCollector(self.doc).OfCategory(DB.BuiltInCategory.OST_StructuralColumns).OfClass(DB.FamilySymbol).FirstElement()
        
        if not symbol: return None
        if not symbol.IsActive: symbol.Activate()
        
        col = None
        if ai_id in self.registry:
            col = self.doc.GetElement(self.registry[ai_id])
            
        if col and isinstance(col, DB.FamilyInstance):
            col.Location.Point = p
        else:
            col = self.doc.Create.NewFamilyInstance(p, symbol, level, DB.Structure.StructuralType.Column)
            state_manager.set_ai_metadata(col, ai_id)
            
        # Set Top Level
        if top_level:
            p_top = col.get_Parameter(DB.BuiltInParameter.FAMILY_TOP_LEVEL_PARAM)
            if p_top: p_top.Set(top_level.Id)
            
        return col
            
    def _synthesize_structural_grid(self, target_dim, span_range, min_offset):
        """
        Synthesises an optimal structural grid span for the given building dimension.

        The 1/3 cantilever rule governs the PERIMETER overhang (facade → first
        column = min_offset), which is always satisfied for typical values.
        It does NOT govern the inner zone between the last bay column and the
        building centre — in a central-core building that zone is the core, and
        no structural columns are placed there.

        Strategy (in priority order):
          1. Find n such that a span in [min_s, max_s] produces an edge overhang
             (half_w − n·span) between min_offset and span/2 (generous inner rule).
          2. Use the largest span in range that fits n+1 intervals.
          3. Fall back to the DNA midpoint span — the actual column placement code
             (get_grid_offsets_mm) divides each region into equal sub-spans and
             always produces a valid grid regardless.

        Returns: (final_dim, final_span)  — never returns CONFLICT.
        """
        if isinstance(span_range, (int, float)):
            min_s, max_s = float(span_range), float(span_range)
        elif isinstance(span_range, list) and len(span_range) >= 2:
            min_s, max_s = float(span_range[0]), float(span_range[1])
        else:
            min_s, max_s = 10000.0, 12000.0

        mid_s = (min_s + max_s) / 2.0
        target_half = target_dim / 2.0

        # Try increasing n (number of full spans from centre outward) and find a
        # span that keeps the perimeter overhang within [min_offset, span/2].
        for n in range(1, 20):
            # Span range that satisfies: min_offset ≤ half_w − n·s ≤ s/2
            # → half_w / (n + 0.5) ≤ s ≤ (half_w − min_offset) / n
            s_lo = target_half / (n + 0.5)
            s_hi = (target_half - min_offset) / n if n > 0 else max_s
            lo = max(min_s, s_lo)
            hi = min(max_s, s_hi)
            if lo <= hi + 1.0:
                return target_dim, hi

        # Fallback: DNA midpoint — get_grid_offsets_mm handles the rest.
        return target_dim, mid_s

    def _expand_high_level_manifest(self, manifest):
        """Converts Architectural Intent (Storeys/Shell) into concrete element lists."""
        setup = manifest.get("project_setup", {})
        shell = manifest.get("shell", {})

        # --- SVG FOOTPRINT EXPANSION ---
        # If Gemini provided a footprint_svg string, convert it to footprint_points
        # (and optionally footprint_holes for multi-subpath courtyard/void SVGs).
        # footprint_svg takes priority over any manually specified footprint_points.
        svg_path = shell.get("footprint_svg")
        if svg_path:
            try:
                from revit_mcp.svg_to_footprint import svg_path_to_multiloop
                multi = svg_path_to_multiloop(svg_path)
                shell = dict(shell)          # shallow copy so we don't mutate the original
                shell["footprint_points"] = multi["outer"]
                if multi["holes"]:
                    shell["footprint_holes"] = multi["holes"]
                shell.pop("footprint_svg", None)
                manifest = dict(manifest)
                manifest["shell"] = shell
                self.log("[SVG] footprint_svg converted: {} outer vertices, {} hole(s)".format(
                    len(multi["outer"]), len(multi["holes"])))
            except Exception as _svg_err:
                self.log("[SVG] footprint_svg conversion FAILED: {} — building will be rectangular".format(_svg_err))

        # --- RESET SPATIAL REGISTRY ---
        self.spatial_registry.clear()
        
        # --- PRESET LOADING ---
        presets = load_presets()
        typology = (manifest.get("typology") or setup.get("typology", "")).lower().replace(" ", "_")
        preset = presets.get(typology) or presets.get("default") or presets.get("commercial_office", {})
        
        num_storeys = int(safe_num(setup.get("levels", 1), 1))
        
        # Default Heights from Presets
        p_defaults = preset.get("building_defaults", {})
        base_height = safe_num(setup.get("level_height", p_defaults.get("typical_floor_height", 4000)), 4000.0)
        height_overrides = setup.get("height_overrides", {})
        
        # Apply Preset Height Overrides if not explicitly overridden by user
        if "1" not in height_overrides and "first_storey_floor_height" in p_defaults:
            height_overrides["1"] = p_defaults["first_storey_floor_height"]
        if str(num_storeys) not in height_overrides and "last_floor_height" in p_defaults:
            height_overrides[str(num_storeys)] = p_defaults["last_floor_height"]
        
        self.log("Building sync: {} storeys @ {}mm (Last: {}mm)".format(
            num_storeys, base_height, height_overrides.get(str(num_storeys), base_height)))
            
        # --- STRUCTURAL SYNTHESIS (1/3 RULE) ---
        p_col_logic = preset.get("column_logic", {})
        dna_span = p_col_logic.get("span", [12000, 15000])
        dna_offset = safe_num(p_col_logic.get("offset_from_edge", 1000), 1000)
        
        width = safe_num(shell.get("width", 50000), 50000.0)
        length = safe_num(shell.get("length", 50000), 50000.0)
        
        # Area Goal Adjustment
        area_goal = safe_num(setup.get("typical_floor_area", preset.get("building_identity", {}).get("typical_floor_area", 0)), 0)
        if area_goal > 0 and width == 50000 and length == 50000:
            side = math.sqrt(area_goal * 1000000.0)
            width, length = side, side
            
        # Synthesis Run (enforce 1/3 rule)
        res_w = self._synthesize_structural_grid(width, dna_span, dna_offset)
        if isinstance(res_w, dict) and res_w.get("status") == "CONFLICT":
            return res_w
        width, synth_span_w = res_w
        
        res_l = self._synthesize_structural_grid(length, dna_span, dna_offset)
        if isinstance(res_l, dict) and res_l.get("status") == "CONFLICT":
            return res_l
        length, synth_span_l = res_l
        
        # Use simple average or W-span for the global col_span? 
        # Usually buildings have a square/regular grid. We'll prioritize W-span but keep logic robust.
        synth_col_span = (synth_span_w + synth_span_l) / 2.0
            
        floor_overrides = shell.get("floor_overrides", {})
        
        new_levels = []
        new_walls = []
        new_floors = []
        
        current_elev = 0.0
        all_floor_dims = []
        # Step 3a: Extracting Staircase Shafts (HOT RELOADED ONCE)
        from revit_mcp import staircase_logic
        from . import fire_safety_logic
        import importlib
        importlib.reload(staircase_logic)
        importlib.reload(fire_safety_logic)
        
        # --- PHASE 1: LEVELS & FLOORS ---
        for i in range(num_storeys + 1):
            is_roof = (i == num_storeys)
            level_idx = i + 1
            lvl_id = "AI_Level_{}".format(level_idx)
            lvl_name = "AI Level {}".format(level_idx)
            new_levels.append({"id": lvl_id, "name": lvl_name, "elevation": current_elev})
            
            if not is_roof:
                is_top = (i == num_storeys - 1)
                raw_height = safe_num(height_overrides.get(str(level_idx), base_height), base_height)
                req_height = staircase_logic.adjust_storey_height(raw_height, base_height, is_top_floor=is_top)
                
                f_w = width; f_l = length
                if str(level_idx) in floor_overrides:
                    ovr = floor_overrides[str(level_idx)]
                    f_w = safe_num(ovr.get("width", f_w), f_w)
                    f_l = safe_num(ovr.get("length", f_l), f_l)
                
                hw, hl = f_w / 2.0, f_l / 2.0
                f_points = [[-hw, -hl], [hw, -hl], [hw, hl], [-hw, hl]]
                
                # --- SPATIAL RESERVATION: Shell Volume ---
                if i == 0:
                    self.spatial_registry.reserve("Building_Shell_{}".format(level_idx), (-hw, -hl, 0, hw, hl, current_elev + req_height), tags=["Shell"])
                new_floors.append({"id": "AI_Floor_{}".format(level_idx), "level_id": lvl_id, "points": f_points})
                all_floor_dims.append((f_w, f_l))
            # --- PHASE 2: CORE GENERATION ---
        lifts_config = manifest.get("lifts", {})
        lift_walls, core_bounds_list = [], []
        f_center_x, f_center_y = 0.0, 0.0
        num_lifts = 0
        lobby_w = 3000
        l1_floor_search = [f for f in new_floors if f.get("id") == "AI_Floor_1"]
        l1_floor = l1_floor_search[0] if l1_floor_search else (new_floors[0] if new_floors else None)
        if l1_floor:
            l1_pts = l1_floor.get("points", [])
            if len(l1_pts) >= 3:
                f_center_x = (min(p[0] for p in l1_pts) + max(p[0] for p in l1_pts)) / 2.0
                f_center_y = (min(p[1] for p in l1_pts) + max(p[1] for p in l1_pts)) / 2.0

        # --- PHASE 2: CORE GENERATION (Unified) ---
        num_lifts = 0
        p_core_logic = preset.get("core_logic", {})
        lobby_w = p_core_logic.get("lift_lobby_width", 3000)
        
        if shell.get("include_lifts") or num_storeys >= 3 or manifest.get("lifts"):
            from . import lift_logic
            lifts_config = manifest.get("lifts", {})
            num_lifts = lifts_config.get("count")
            # Occupancy from floor area (m²) × density (persons/m²) × storeys
            _fw = safe_num(shell.get("width", 30000), 30000)
            _fl = safe_num(shell.get("length", 50000), 50000)
            _occ_density = safe_num(lifts_config.get("occupancy_density", 0.1), 0.1)
            _total_occ = max(100, (_fw * _fl / 1e6) * _occ_density * num_storeys)
            _auto_lifts = lift_logic.calculate_lift_requirements(num_storeys, base_height, _total_occ, 25.0)
            if num_lifts is None or num_lifts == "random":
                num_lifts = _auto_lifts
            else:
                # Cap Gemini-specified count against demand-based calculation
                num_lifts = min(int(num_lifts), _auto_lifts)
            num_lifts = int(num_lifts)
            lobby_w = lifts_config.get("lobby_width", lobby_w)

        # Initial core center based on floor center
        center_pos = [f_center_x, f_center_y]
        all_voids = []
        exposed_stair_info = []  # populated after fire-safety manifest generation
        # Calculate Unified Core (Passenger Lifts + Fire Safety Sets)
        preset_fs = preset.get("core_logic", {}).get("fire_safety", {})
        stair_spec = preset_fs.get("staircase_spec", {}).copy()
        m_stair_config = manifest.get("staircases", {})
        if "spec" in m_stair_config:
            for k in ["riser", "tread", "width_of_flight", "landing_width"]:
                if k in m_stair_config["spec"]:
                    stair_spec[k] = safe_num(m_stair_config["spec"][k], stair_spec.get(k))

        # Initial core center based on floor center
        center_pos = [f_center_x, f_center_y]
        
        # 1. First, determine positions
        safety_sets = fire_safety_logic.calculate_fire_safety_requirements(
            all_floor_dims, center_pos, None, 
            base_height, preset_fs, num_lifts, lobby_w
        )
        
        # 2. Generate the Unified Manifest
        unified_man = fire_safety_logic.generate_fire_safety_manifest(
            safety_sets, new_levels, stair_spec, base_height, preset_fs, None, num_lifts, lobby_w,
            all_floor_dims=all_floor_dims,
            manifest_lifts=manifest.get("lifts"),
            center_pos=center_pos,
        )
        
        if isinstance(unified_man, dict) and unified_man.get("status") == "CONFLICT":
            return unified_man

        # 2b. Exposed staircase floor-widening (Step 2 of design intent)
        # If any perimeter staircases extend outside smaller floor plates, optionally
        # widen those floors to enclose the stairs — controlled by the manifest flag
        # `enclose_exposed_stairs` (default: True unless user sets it to False).
        # The AI can set this to false to leave stairs exposed as a design choice.
        enclose_exposed = manifest.get("enclose_exposed_stairs", True)
        exposed_stair_info = unified_man.get("exposed_stair_info", [])
        if exposed_stair_info and enclose_exposed:
            for exposure in exposed_stair_info:
                for lvl_exp in exposure["exposed_levels"]:
                    lvl_key = str(lvl_exp["level_index"])
                    cur_w, cur_l = all_floor_dims[lvl_exp["level_index"] - 1]
                    new_w = max(cur_w, lvl_exp["min_width_to_enclose"])
                    new_l = max(cur_l, lvl_exp["min_length_to_enclose"])
                    if new_w == cur_w and new_l == cur_l:
                        continue  # already large enough; no change needed
                    # Update floor_overrides so Phase 3 shell walls use new dims
                    ovr = floor_overrides.get(lvl_key, {})
                    ovr["width"] = new_w
                    ovr["length"] = new_l
                    floor_overrides[lvl_key] = ovr
                    # Patch all_floor_dims so column logic sees updated dims
                    all_floor_dims[lvl_exp["level_index"] - 1] = (new_w, new_l)
                    # Patch the floor slab points already assembled in new_floors
                    for fl in new_floors:
                        if fl.get("id") == "AI_Floor_{}".format(lvl_key):
                            hw2, hl2 = new_w / 2.0, new_l / 2.0
                            fl["points"] = [[-hw2, -hl2], [hw2, -hl2], [hw2, hl2], [-hw2, hl2]]
                            break

        # 3. Precise Mental Model Reservation
        core_bounds_list = unified_man.get("core_bounds", [])
        sub_bounds = unified_man.get("sub_boundaries", [])
        
        if sub_bounds:
            for sb in sub_bounds:
                res_id = sb["id"]
                rect = sb["rect"]
                # Use current_elev as a quick approximation for the built volume height
                res, conflict = self.spatial_registry.reserve(res_id, (rect[0], rect[1], 0, rect[2], rect[3], base_height))
                if not res and "Shell" not in str(conflict):
                     return {
                        "status": "CONFLICT",
                        "type": "GEOMETRIC_INTERFERENCE",
                        "description": "Spatial Conflict: '{}' overlaps with '{}'".format(res_id, conflict)
                    }
        else:
            # Fallback for core boundaries
            for i, cb in enumerate(core_bounds_list):
                res_id = "Passenger_Core" if i == 0 and num_lifts > 0 else "Safety_Set_{}".format(i)
                res, conflict = self.spatial_registry.reserve(res_id, (cb[0], cb[1], 0, cb[2], cb[3], base_height))
                if not res and "Shell" not in str(conflict):
                    return {"status": "CONFLICT", "type": "GEOMETRIC_INTERFERENCE", "description": "Core element {} overlaps with: {}".format(res_id, conflict)}

        new_walls.extend(unified_man.get("walls", []))
        new_floors.extend(unified_man.get("floors", []))
        all_voids.extend(unified_man.get("voids", []))
        
        # --- PHASE 3: SHELL WALLS WITH CURBING ---
        for i in range(num_storeys):
            level_idx = i + 1; lvl_id = "AI_Level_{}".format(level_idx); raw_h = safe_num(height_overrides.get(str(level_idx), base_height), base_height); is_top = (i == num_storeys - 1)
            req_h = staircase_logic.adjust_storey_height(raw_h, base_height, is_top_floor=is_top)
            f_w, f_l = width, length
            if str(level_idx) in floor_overrides:
                ovr = floor_overrides[str(level_idx)]; f_w = safe_num(ovr.get("width", f_w), f_w); f_l = safe_num(ovr.get("length", f_l), f_l)
            hw, hl = f_w/2.0, f_l/2.0
            pts = [[-hw, -hl], [hw, -hl], [hw, hl], [-hw, hl]]; tags = ["S", "E", "N", "W"]
            for j in range(4):
                segments = [[pts[j], pts[(j+1)%4]]]
                for cb in core_bounds_list:
                    new_segs = []
                    for seg in segments: new_segs.extend(self._curb_wall_segment(seg[0], seg[1], cb))
                    segments = new_segs
                for s_idx, seg in enumerate(segments):
                    new_walls.append({"id": "AI_Wall_L{}_{}_{}".format(level_idx, tags[j], s_idx), "level_id": lvl_id, "start": [seg[0][0], seg[0][1], 0], "end": [seg[1][0], seg[1][1], 0], "height": req_h})

        new_walls.extend(lift_walls)
        # Combined Core Bounds for Column logic
        core_bounds = (min(c[0] for c in core_bounds_list), min(c[1] for c in core_bounds_list), max(c[2] for c in core_bounds_list), max(c[3] for c in core_bounds_list)) if core_bounds_list else None

        # --- COLUMN GENERATION ---
        # Core-aware grid: grid lines at core edges, building edges (with setback),
        # and equally spaced between them. Core is structural — no columns inside.
        new_columns = []
        # Use synthesis span (already optimized for building dimensions)
        col_span = synth_col_span
        if col_span % 300 != 0: col_span = round(col_span / 300.0) * 300.0

        col_offset = dna_offset  # offset_from_edge in mm

        center_only = shell.get("columns_center_only", False) or "center area" in str(shell).lower()

        def get_grid_offsets_mm(dim_mm, span_mm, core_min_mm=None, core_max_mm=None):
            """Core-aware grid: positions at building edges, core edges, and
            equally spaced between each edge and its adjacent core wall (all mm)."""
            half_dim = dim_mm / 2.0
            edge_pos = half_dim - col_offset
            edge_neg = -edge_pos
            offsets = set()

            if core_min_mm is not None and core_max_mm is not None and (core_max_mm - core_min_mm) > 1.0:
                # Always include core boundary positions
                offsets.add(round(core_min_mm, 1))
                offsets.add(round(core_max_mm, 1))

                # LEFT REGION: edge_neg to core_min (both endpoints, equally spaced)
                left_dist = core_min_mm - edge_neg
                s_left = span_mm
                if left_dist > 1.0:
                    n_spans = max(1, int(math.ceil(left_dist / span_mm - 0.001)))
                    s_left = left_dist / n_spans
                    for i in range(n_spans + 1):
                        offsets.add(round(edge_neg + i * s_left, 1))
                else:
                    offsets.add(round(edge_neg, 1))

                # RIGHT REGION: core_max to edge_pos (both endpoints, equally spaced)
                right_dist = edge_pos - core_max_mm
                s_right = span_mm
                if right_dist > 1.0:
                    n_spans = max(1, int(math.ceil(right_dist / span_mm - 0.001)))
                    s_right = right_dist / n_spans
                    for i in range(n_spans + 1):
                        offsets.add(round(core_max_mm + i * s_right, 1))
                else:
                    offsets.add(round(edge_pos, 1))

                # THROUGH-CORE: interior positions only (for perpendicular axis)
                core_dist = core_max_mm - core_min_mm
                if core_dist > 1.0:
                    avg_span = (s_left + s_right) / 2.0
                    n_core = max(2, int(math.ceil(core_dist / avg_span - 0.001)))
                    s_core = core_dist / n_core
                    for i in range(1, n_core):
                        offsets.add(round(core_min_mm + i * s_core, 1))
            else:
                # NO CORE: edge columns + equally spaced between
                offsets.add(round(edge_neg, 1))
                offsets.add(round(edge_pos, 1))
                full_dist = edge_pos - edge_neg
                if full_dist > 1.0:
                    n_spans = max(1, int(math.ceil(full_dist / span_mm - 0.001)))
                    s = full_dist / n_spans
                    for i in range(n_spans + 1):
                        offsets.add(round(edge_neg + i * s, 1))

            return sorted(offsets)

        # Extract core bounds per axis for grid computation
        if core_bounds:
            x_core_min, x_core_max = core_bounds[0], core_bounds[2]
            y_core_min, y_core_max = core_bounds[1], core_bounds[3]
        else:
            x_core_min = x_core_max = y_core_min = y_core_max = None

        x_offsets = get_grid_offsets_mm(width, synth_span_w, x_core_min, x_core_max)
        y_offsets = get_grid_offsets_mm(length, synth_span_l, y_core_min, y_core_max)

        if core_bounds:
            anchor_x = (core_bounds[0] + core_bounds[2]) / 2.0
            anchor_y = (core_bounds[1] + core_bounds[3]) / 2.0
        else:
            anchor_x = f_center_x
            anchor_y = f_center_y

        if center_only:
            half_w = width / 2.0
            half_l = length / 2.0
            x_offsets = [o for o in x_offsets if abs(o) < half_w - 1.0]
            y_offsets = [o for o in y_offsets if abs(o) < half_l - 1.0]

        for k in range(num_storeys):
            lvl_id = "AI_Level_{}".format(k+1)
            top_lvl_id = "AI_Level_{}".format(k+2)

            for ox_mm in x_offsets:
                for oy_mm in y_offsets:
                    # Cull columns strictly inside core footprint (core is structural)
                    if core_bounds:
                        if (core_bounds[0] < ox_mm < core_bounds[2]) and \
                           (core_bounds[1] < oy_mm < core_bounds[3]):
                            continue

                    # Calculate IX/IY relative to anchor
                    ix = int(round((ox_mm - anchor_x) / synth_span_w))
                    iy = int(round((oy_mm - anchor_y) / synth_span_l))
                    col_id = "AI_Col_L{}_GX{}_GY{}".format(k+1, ix, iy)
                    new_columns.append({
                        "id": col_id,
                        "level_id": lvl_id,
                        "top_level_id": top_lvl_id,
                        "location": [ox_mm, oy_mm, 0],
                        "type": shell.get("column_type", "")
                    })

        # --- PHASE 4: UNIVERSAL ASSEMBLY VALIDATION & GRANULAR SPACES ---
        # If the manifest contains custom "spaces", validate them
        custom_spaces = manifest.get("spaces", [])
        for space in custom_spaces:
            sid = space.get("id")
            bbox_raw = space.get("bbox") # [x1, y1, z1, x2, y2, z2]
            if not sid or not bbox_raw: continue
            
            # Validation: Must have walls and floors
            is_valid, err = self.spatial_registry.validate_assembly(sid, space)
            if not is_valid:
                return {"status": "CONFLICT", "type": "ASSEMBLY_INCOMPLETE", "description": err}
                
            # Reservation
            res, conflict = self.spatial_registry.reserve(sid, tuple(bbox_raw), tags=space.get("tags", []))
            if not res:
                return {"status": "CONFLICT", "type": "GEOMETRIC_INTERFERENCE", "description": "Space {} overlaps with: {}".format(sid, conflict)}
            
            # Extract elements from space and add to manifest
            new_walls.extend(space.get("walls", []))
            new_floors.extend(space.get("floors", []))
            if "columns" in space: new_columns.extend(space["columns"])

        # --- SPATIAL SUMMARY LOGGING ---
        occupancy = self.spatial_registry.get_occupancy_map()
        self.log("Spatial Clearinghouse: {} volumes reserved (Cores: {}, Custom: {}). Zero conflicts detected.".format(
            len(occupancy),
            len([o for o in occupancy if "Core" in o['id'] or "Safety" in o['id']]),
            len([o for o in occupancy if "Shell" not in o['id'] and "Core" not in o['id'] and "Safety" not in o['id']])
        ))

        result = {"levels": new_levels, "walls": new_walls, "floors": new_floors, "columns": new_columns, "core_bounds": core_bounds_list, "voids": all_voids}
        if exposed_stair_info:
            result["exposed_stair_info"] = exposed_stair_info
        return result

    def _curb_wall_segment(self, p1, p2, bounds, tolerance=100.0):
        xmin, ymin, xmax, ymax = bounds
        xmin -= tolerance; ymin -= tolerance; xmax += tolerance; ymax += tolerance
        def is_in(p): return xmin <= p[0] <= xmax and ymin <= p[1] <= ymax
        if is_in(p1) and is_in(p2): return []
        is_horiz = abs(p1[1] - p2[1]) < 0.1
        is_vert = abs(p1[0] - p2[0]) < 0.1
        if is_horiz:
            if not (ymin <= p1[1] <= ymax): return [[p1, p2]]
            w_min, w_max = (p1[0], p2[0]) if p1[0] < p2[0] else (p2[0], p1[0])
            if w_max < xmin or w_min > xmax: return [[p1, p2]]
            results = []
            if w_min < xmin: results.append([[w_min, p1[1]], [xmin, p1[1]]])
            if w_max > xmax: results.append([[xmax, p1[1]], [w_max, p1[1]]])
            return results
        elif is_vert:
            if not (xmin <= p1[0] <= xmax): return [[p1, p2]]
            w_min, w_max = (p1[1], p2[1]) if p1[1] < p2[1] else (p2[1], p1[1])
            if w_max < ymin or w_min > ymax: return [[p1, p2]]
            results = []
            if w_min < ymin: results.append([[p1[0], w_min], [p1[0], ymin]])
            if w_max > ymax: results.append([[p1[0], ymax], [p1[0], w_max]])
            return results
        return [[p1, p2]]

    def _find_type(self, category_bip, name):
        import Autodesk.Revit.DB as DB # type: ignore
        cl = DB.FilteredElementCollector(self.doc).OfCategory(category_bip).OfClass(DB.ElementType)
        for t in cl:
            if name.lower() in t.Name.lower(): return t
        return None

# Example Usage (not to be run directly as a script but used by the MCP)
# generator = BuildingSystem(doc, 5000, 8000, 3500)
# generator.generate()

