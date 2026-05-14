# -*- coding: utf-8 -*-
import json
import time
import math
import os
from revit_mcp.state_manager import state_manager
from revit_mcp.utils import (
    mm_to_ft, ft_to_mm, get_bip, 
    find_level, find_type_symbol, set_params_batch
)

# Application access (initialized by server.py)
_get_revit_app = None

def initialize(get_app_func):
    global _get_revit_app
    _get_revit_app = get_app_func

# --- UI LOGIC HELPERS ---

def get_doc_info_ui():
    uiapp = _get_revit_app()
    if not uiapp or not uiapp.ActiveUIDocument:
        return {"error": "No active document."}
    doc = uiapp.ActiveUIDocument.Document
    
    import Autodesk.Revit.DB as DB # type: ignore
    from revit_mcp.building_generator import get_model_registry # type: ignore
    
    registry = get_model_registry(doc)
    
    # 1. Collect Floor boundaries
    floors = DB.FilteredElementCollector(doc).OfClass(DB.Floor).WhereElementIsNotElementType()
    floor_boundaries = []
    for f in floors:
        bb = f.get_BoundingBox(None)
        if bb:
            floor_boundaries.append({
                "id": str(f.Id.Value),
                "level": doc.GetElement(f.LevelId).Name if f.LevelId != DB.ElementId.InvalidElementId else "Unknown",
                "bounds": [ft_to_mm(bb.Min.X), ft_to_mm(bb.Min.Y), ft_to_mm(bb.Max.X), ft_to_mm(bb.Max.Y)]
            })
            
    # 2. Collect Core locations (Lifts and Stairs)
    core_bounds = []
    for ai_id, eid in registry.items():
        if "Lift" in ai_id or "Stair" in ai_id:
            el = doc.GetElement(eid)
            if el:
                bb = el.get_BoundingBox(None)
                if bb:
                    core_bounds.append([ft_to_mm(bb.Min.X), ft_to_mm(bb.Min.Y), ft_to_mm(bb.Max.X), ft_to_mm(bb.Max.Y)])
                    
    # Simplify core bounds to a single bounding area if possible, or keep list
    unified_core = None
    if core_bounds:
        xmin = min(b[0] for b in core_bounds)
        ymin = min(b[1] for b in core_bounds)
        xmax = max(b[2] for b in core_bounds)
        ymax = max(b[3] for b in core_bounds)
        unified_core = [xmin, ymin, xmax, ymax]

    # 3. Detect "Leftover" or Unmanaged Objects (Flying all over)
    unmanaged_obstructions = []
    if floor_boundaries:
        import System.Collections.Generic as Generic # type: ignore
        min_x = min(f["bounds"][0] for f in floor_boundaries) - 5000
        min_y = min(f["bounds"][1] for f in floor_boundaries) - 5000
        max_x = max(f["bounds"][2] for f in floor_boundaries) + 5000
        max_y = max(f["bounds"][3] for f in floor_boundaries) + 5000
        
        scan_cats = [
            DB.BuiltInCategory.OST_Walls, DB.BuiltInCategory.OST_Floors,
            DB.BuiltInCategory.OST_StructuralColumns, DB.BuiltInCategory.OST_Columns,
            DB.BuiltInCategory.OST_Windows, DB.BuiltInCategory.OST_Doors,
            DB.BuiltInCategory.OST_GenericModel
        ]
        
        cat_list = Generic.List[DB.BuiltInCategory]()
        for c in scan_cats: cat_list.Add(c)
        
        provider = DB.ElementMulticategoryFilter(cat_list)
        others = DB.FilteredElementCollector(doc).WherePasses(provider).WhereElementIsNotElementType().ToElements()
        
        for el in others:
            if str(el.Id.Value) in [str(v) for v in registry.values()]: continue
            p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if p and p.HasValue and p.AsString().startswith("AI_"): continue
            
            bb = el.get_BoundingBox(None)
            if bb:
                ex1, ey1 = ft_to_mm(bb.Min.X), ft_to_mm(bb.Min.Y)
                ex2, ey2 = ft_to_mm(bb.Max.X), ft_to_mm(bb.Max.Y)
                if not (ex2 < min_x or ex1 > max_x or ey2 < min_y or ey1 > max_y):
                    unmanaged_obstructions.append({
                        "id": str(el.Id.Value),
                        "category": el.Category.Name,
                        "bounds": [ex1, ey1, ex2, ey2]
                    })

    # 4. Generate 3D Occupancy Map
    occupancy_map = []
    
    # Process Cores (Lifts/Stairs)
    for ai_id, eid in registry.items():
        if "Lift" in ai_id or "Stair" in ai_id or "SafetySet" in ai_id:
            el = doc.GetElement(eid)
            if el:
                bb = el.get_BoundingBox(None)
                if bb:
                    occupancy_map.append({
                        "id": ai_id,
                        "type": "CORE",
                        "bbox": [ft_to_mm(bb.Min.X), ft_to_mm(bb.Min.Y), ft_to_mm(bb.Min.Z),
                                 ft_to_mm(bb.Max.X), ft_to_mm(bb.Max.Y), ft_to_mm(bb.Max.Z)]
                    })

    # Process unmanaged obstructions in 3D
    for obs in unmanaged_obstructions:
        occupancy_map.append({
            "id": obs["id"],
            "type": "OBSTRUCTION",
            "category": obs["category"],
            "bbox": [obs["bounds"][0], obs["bounds"][1], 0, obs["bounds"][2], obs["bounds"][3], 4000] # Default height if unknown
        })

    return {
        "title": doc.Title,
        "path": doc.PathName or "Unsaved Document",
        "vision_3d": {
            "occupancy_map": occupancy_map[:30], # Top 30 for token efficiency
            "floor_boundaries": floor_boundaries,
            "overall_core_bounds": unified_core
        },
        "system_status": "Spatial Clearinghouse ACTIVE. Global Geometric Constraints enforced."
    }

def create_wall_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app()
    doc = uiapp.ActiveUIDocument.Document
    
    sx = params.get('start_x', 0)
    sy = params.get('start_y', 0)
    p1 = DB.XYZ(mm_to_ft(sx), mm_to_ft(sy), 0)
    
    ex = params.get('end_x')
    ey = params.get('end_y')
    
    if ex is not None and ey is not None:
        p2 = DB.XYZ(mm_to_ft(ex), mm_to_ft(ey), 0)
    else:
        l_mm = params.get('length_mm', 5000)
        p2 = DB.XYZ(mm_to_ft(sx + l_mm), mm_to_ft(sy), 0)
    
    try:
        line = DB.Line.CreateBound(p1, p2)
    except Exception as e:
        dist_ft = p1.DistanceTo(p2)
        msg = "Failed to create wall curve: {}. Target length: {}mm. Revit min is ~1.6mm.".format(str(e), ft_to_mm(dist_ft))
        return {"error": msg}
    
    level = find_level(doc, params.get('level_name') or params.get('level_id'))
    
    from revit_mcp.utils import nuclear_lockdown
    nuclear_lockdown(doc)
    
    t = DB.Transaction(doc, "MCP: Create Wall")
    t.Start()
    from revit_mcp.utils import setup_failure_handling
    setup_failure_handling(t, use_nuclear=True)
    new_wall = DB.Wall.Create(doc, line, level.Id, False)
    state_manager.set_ai_metadata(new_wall, params.get('ai_id') or "AI_Wall_{}".format(new_wall.Id.Value))
    from revit_mcp.utils import disallow_joins
    # POST-CREATION LOCK
    disallow_joins(new_wall)
    
    param = new_wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
    if param: param.Set(mm_to_ft(params.get('height_mm', 3000)))
    
    thickness_mm = params.get('thickness_mm')
    if thickness_mm:
        wt = next((w for w in DB.FilteredElementCollector(doc).OfClass(DB.WallType) if str(int(thickness_mm)) in w.Name), None)
        if wt: new_wall.WallType = wt
                
    t.Commit()
    return {"success": True, "wall_id": str(new_wall.Id.Value)}

def create_arc_wall_ui(params):
    """Create a curved wall along an arc defined by start, end, and mid points (all in mm)."""
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app()
    doc = uiapp.ActiveUIDocument.Document

    p1 = DB.XYZ(mm_to_ft(params['start_x']), mm_to_ft(params['start_y']), 0)
    p2 = DB.XYZ(mm_to_ft(params['end_x']),   mm_to_ft(params['end_y']),   0)
    pm = DB.XYZ(mm_to_ft(params['mid_x']),   mm_to_ft(params['mid_y']),   0)

    try:
        arc = DB.Arc.Create(p1, p2, pm)
    except Exception as e:
        return {"error": "Failed to create arc curve: {}. Ensure the three points are not collinear.".format(str(e))}

    level = find_level(doc, params.get('level_name') or params.get('level_id'))

    from revit_mcp.utils import nuclear_lockdown, setup_failure_handling, disallow_joins
    nuclear_lockdown(doc)

    t = DB.Transaction(doc, "MCP: Create Arc Wall")
    t.Start()
    setup_failure_handling(t, use_nuclear=True)
    new_wall = DB.Wall.Create(doc, arc, level.Id, False)
    state_manager.set_ai_metadata(new_wall, params.get('ai_id') or "AI_Wall_{}".format(new_wall.Id.Value))
    disallow_joins(new_wall)

    param = new_wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
    if param: param.Set(mm_to_ft(params.get('height_mm', 3000)))

    thickness_mm = params.get('thickness_mm')
    if thickness_mm:
        wt = next((w for w in DB.FilteredElementCollector(doc).OfClass(DB.WallType) if str(int(thickness_mm)) in w.Name), None)
        if wt: new_wall.WallType = wt

    t.Commit()
    return {"success": True, "wall_id": str(new_wall.Id.Value)}

def get_element_details_ui(element_id_val):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app()
    doc = uiapp.ActiveUIDocument.Document
    try:
        eid = DB.ElementId(int(element_id_val))
        el = doc.GetElement(eid)
        if not el: return {"error": "Not found"}
        
        info = {"id": element_id_val, "name": el.Name, "category": el.Category.Name if el.Category else "None", "geometry": []}
        
        if isinstance(el, DB.Grid):
            c = el.Curve
            info["geometry"].append({"type": "grid_line", "start": {"x": ft_to_mm(c.GetEndPoint(0).X), "y": ft_to_mm(c.GetEndPoint(0).Y)}, "end": {"x": ft_to_mm(c.GetEndPoint(1).X), "y": ft_to_mm(c.GetEndPoint(1).Y)}})
        elif isinstance(el, DB.Level):
            info["elevation_mm"] = ft_to_mm(el.Elevation)

        loc = el.Location
        if loc and hasattr(loc, "Curve") and isinstance(loc.Curve, DB.Line):
            c = loc.Curve
            info["geometry"].append({"type": "line", "start": {"x": ft_to_mm(c.GetEndPoint(0).X), "y": ft_to_mm(c.GetEndPoint(0).Y)}, "end": {"x": ft_to_mm(c.GetEndPoint(1).X), "y": ft_to_mm(c.GetEndPoint(1).Y)}})
        elif loc and hasattr(loc, "Point"):
            p = loc.Point
            info["point"] = {"x": ft_to_mm(p.X), "y": ft_to_mm(p.Y), "z": ft_to_mm(p.Z)}

        if isinstance(el, DB.Floor):
            seen_lines = set()
            for g in el.get_Geometry(DB.Options()):
                if isinstance(g, DB.Solid):
                    for edge in g.Edges:
                        c = edge.AsCurve()
                        if isinstance(c, DB.Line):
                            p1, p2 = c.GetEndPoint(0), c.GetEndPoint(1)
                            if abs(p1.X - p2.X) < 0.001 and abs(p1.Y - p2.Y) < 0.001: continue
                            line_key = tuple(sorted([(round(p1.X,3), round(p1.Y,3)), (round(p2.X,3), round(p2.Y,3))]))
                            if line_key in seen_lines: continue
                            seen_lines.add(line_key)
                            info["geometry"].append({"type": "line", "start": {"x": ft_to_mm(p1.X), "y": ft_to_mm(p1.Y)}, "end": {"x": ft_to_mm(p2.X), "y": ft_to_mm(p2.Y)}})
        return info
    except Exception as e: return {"error": str(e)}

def delete_walls_ui():
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    walls = DB.FilteredElementCollector(doc).OfClass(DB.Wall).ToElementIds()
    if walls.Count == 0: return {"success": True, "message": "No walls."}
    t = DB.Transaction(doc, "MCP: Delete Walls"); t.Start(); doc.Delete(walls); t.Commit()
    return {"success": True}

def delete_all_elements_ui():
    """Delete all AI-generated elements from the model, or all model elements if requested."""
    import Autodesk.Revit.DB as DB # type: ignore
    from revit_mcp.utils import setup_failure_handling
    uiapp = _get_revit_app()
    doc = uiapp.ActiveUIDocument.Document

    deleted_count = 0
    errors = []

    # Collect all deletable model element categories
    categories = [
        DB.BuiltInCategory.OST_Walls,
        DB.BuiltInCategory.OST_Floors,
        DB.BuiltInCategory.OST_Columns,
        DB.BuiltInCategory.OST_StructuralColumns,
        DB.BuiltInCategory.OST_Doors,
        DB.BuiltInCategory.OST_Windows,
        DB.BuiltInCategory.OST_Roofs,
        DB.BuiltInCategory.OST_Stairs,
        DB.BuiltInCategory.OST_StairsRailing,
        DB.BuiltInCategory.OST_Grids,
    ]

    tg = DB.TransactionGroup(doc, "MCP: Delete All Elements")
    tg.Start()

    # Phase 1: Delete all model elements by category
    t = DB.Transaction(doc, "MCP: Delete Model Elements")
    t.Start()
    setup_failure_handling(t, use_nuclear=True)

    for cat in categories:
        try:
            ids = DB.FilteredElementCollector(doc).OfCategory(cat).WhereElementIsNotElementType().ToElementIds()
            if ids.Count > 0:
                doc.Delete(ids)
                deleted_count += ids.Count
        except Exception as e:
            errors.append("Category {}: {}".format(cat, str(e)))

    t.Commit()
    t.Dispose()

    # Phase 2: Delete AI levels and their associated views
    t = DB.Transaction(doc, "MCP: Delete AI Levels")
    t.Start()
    setup_failure_handling(t, use_nuclear=True)

    ai_levels = []
    for lvl in DB.FilteredElementCollector(doc).OfClass(DB.Level):
        name = lvl.Name
        if name.startswith("AI Level") or name.startswith("AI_Level") or name.startswith("AI "):
            ai_levels.append(lvl)

    # Delete associated floor plan views first
    all_views = DB.FilteredElementCollector(doc).OfClass(DB.View).ToElements()
    for lvl in ai_levels:
        for v in all_views:
            try:
                if hasattr(v, "GenLevel") and v.GenLevel and v.GenLevel.Id == lvl.Id:
                    if v.Pinned: v.Pinned = False
                    doc.Delete(v.Id)
            except: pass

    # Delete the levels themselves
    for lvl in ai_levels:
        try:
            if lvl.Pinned: lvl.Pinned = False
            doc.Delete(lvl.Id)
            deleted_count += 1
        except Exception as e:
            errors.append("Level {}: {}".format(lvl.Name, str(e)))

    t.Commit()
    t.Dispose()
    tg.Assimilate()

    result = {"success": True, "deleted_count": deleted_count}
    if errors:
        result["warnings"] = errors
    return result

def delete_elements_by_filter_ui(params):
    """Delete elements filtered by category and/or level range."""
    import Autodesk.Revit.DB as DB # type: ignore
    from revit_mcp.utils import setup_failure_handling
    uiapp = _get_revit_app()
    doc = uiapp.ActiveUIDocument.Document

    category = (params.get("category") or "").lower().strip()
    level_start = params.get("level_start")  # int or None
    level_end = params.get("level_end")      # int or None (defaults to level_start if not set)

    if level_start is not None and level_end is None:
        level_end = level_start

    # Map user-friendly category names to Revit BuiltInCategory values
    CATEGORY_MAP = {
        "walls":              [DB.BuiltInCategory.OST_Walls],
        "wall":               [DB.BuiltInCategory.OST_Walls],
        "floors":             [DB.BuiltInCategory.OST_Floors],
        "floor":              [DB.BuiltInCategory.OST_Floors],
        "slabs":              [DB.BuiltInCategory.OST_Floors],
        "columns":            [DB.BuiltInCategory.OST_Columns, DB.BuiltInCategory.OST_StructuralColumns],
        "column":             [DB.BuiltInCategory.OST_Columns, DB.BuiltInCategory.OST_StructuralColumns],
        "doors":              [DB.BuiltInCategory.OST_Doors],
        "door":               [DB.BuiltInCategory.OST_Doors],
        "windows":            [DB.BuiltInCategory.OST_Windows],
        "window":             [DB.BuiltInCategory.OST_Windows],
        "roofs":              [DB.BuiltInCategory.OST_Roofs],
        "roof":               [DB.BuiltInCategory.OST_Roofs],
        "stairs":             [DB.BuiltInCategory.OST_Stairs, DB.BuiltInCategory.OST_StairsRailing],
        "stair":              [DB.BuiltInCategory.OST_Stairs, DB.BuiltInCategory.OST_StairsRailing],
        "staircases":         [DB.BuiltInCategory.OST_Stairs, DB.BuiltInCategory.OST_StairsRailing],
        "railings":           [DB.BuiltInCategory.OST_StairsRailing],
        "grids":              [DB.BuiltInCategory.OST_Grids],
        "grid":               [DB.BuiltInCategory.OST_Grids],
        "levels":             [],  # special handling below
        "level":              [],
    }

    # Determine which categories to delete
    if category and category in CATEGORY_MAP:
        target_cats = CATEGORY_MAP[category]
    elif category:
        return {"error": "Unknown category '{}'. Valid: {}".format(category, ", ".join(sorted(set(CATEGORY_MAP.keys()))))}
    else:
        # No category specified = all model element categories
        target_cats = [
            DB.BuiltInCategory.OST_Walls, DB.BuiltInCategory.OST_Floors,
            DB.BuiltInCategory.OST_Columns, DB.BuiltInCategory.OST_StructuralColumns,
            DB.BuiltInCategory.OST_Doors, DB.BuiltInCategory.OST_Windows,
            DB.BuiltInCategory.OST_Roofs, DB.BuiltInCategory.OST_Stairs,
            DB.BuiltInCategory.OST_StairsRailing, DB.BuiltInCategory.OST_Grids,
        ]

    # Resolve level filter: find AI levels matching the requested range
    target_level_ids = set()
    if level_start is not None:
        ai_levels = []
        for lvl in DB.FilteredElementCollector(doc).OfClass(DB.Level):
            ai_levels.append(lvl)
        ai_levels.sort(key=lambda x: x.Elevation)

        for i, lvl in enumerate(ai_levels):
            floor_num = i + 1  # 1-based floor number
            if level_start <= floor_num <= level_end:
                target_level_ids.add(lvl.Id)

        if not target_level_ids:
            return {"error": "No levels found matching floor range {}-{}".format(level_start, level_end)}

    deleted_count = 0
    errors = []

    tg = DB.TransactionGroup(doc, "MCP: Delete Filtered Elements")
    tg.Start()

    # Delete model elements
    if target_cats:
        t = DB.Transaction(doc, "MCP: Delete Filtered")
        t.Start()
        setup_failure_handling(t, use_nuclear=True)

        for cat in target_cats:
            try:
                collector = DB.FilteredElementCollector(doc).OfCategory(cat).WhereElementIsNotElementType()
                if target_level_ids:
                    # Filter by level — check each element
                    ids_to_delete = []
                    for el in collector:
                        el_level_id = None
                        if hasattr(el, "LevelId"):
                            el_level_id = el.LevelId
                        elif hasattr(el, "Level") and el.Level:
                            el_level_id = el.Level.Id
                        else:
                            p = el.get_Parameter(DB.BuiltInParameter.INSTANCE_REFERENCE_LEVEL_PARAM)
                            if p and p.HasValue:
                                el_level_id = DB.ElementId(p.AsElementId().Value)
                        if el_level_id and el_level_id in target_level_ids:
                            ids_to_delete.append(el.Id)
                    if ids_to_delete:
                        from System.Collections.Generic import List
                        id_list = List[DB.ElementId]()
                        for eid in ids_to_delete:
                            id_list.Add(eid)
                        doc.Delete(id_list)
                        deleted_count += len(ids_to_delete)
                else:
                    ids = collector.ToElementIds()
                    if ids.Count > 0:
                        doc.Delete(ids)
                        deleted_count += ids.Count
            except Exception as e:
                errors.append("Category {}: {}".format(cat, str(e)))

        t.Commit()
        t.Dispose()

    # Delete levels themselves if category is "levels"
    if category in ("levels", "level"):
        t = DB.Transaction(doc, "MCP: Delete Levels")
        t.Start()
        setup_failure_handling(t, use_nuclear=True)

        all_levels = list(DB.FilteredElementCollector(doc).OfClass(DB.Level))
        all_levels.sort(key=lambda x: x.Elevation)

        levels_to_delete = []
        if level_start is not None:
            for i, lvl in enumerate(all_levels):
                if level_start <= (i + 1) <= level_end:
                    levels_to_delete.append(lvl)
        else:
            # Delete only AI levels when no range specified
            for lvl in all_levels:
                name = lvl.Name
                if name.startswith("AI Level") or name.startswith("AI_Level") or name.startswith("AI "):
                    levels_to_delete.append(lvl)

        # Delete associated views first
        all_views = DB.FilteredElementCollector(doc).OfClass(DB.View).ToElements()
        for lvl in levels_to_delete:
            for v in all_views:
                try:
                    if hasattr(v, "GenLevel") and v.GenLevel and v.GenLevel.Id == lvl.Id:
                        if v.Pinned: v.Pinned = False
                        doc.Delete(v.Id)
                except: pass

        for lvl in levels_to_delete:
            try:
                if lvl.Pinned: lvl.Pinned = False
                doc.Delete(lvl.Id)
                deleted_count += 1
            except Exception as e:
                errors.append("Level {}: {}".format(lvl.Name, str(e)))

        t.Commit()
        t.Dispose()

    tg.Assimilate()

    result = {"success": True, "deleted_count": deleted_count}
    if category:
        result["filter_category"] = category
    if level_start is not None:
        result["filter_levels"] = "{}-{}".format(level_start, level_end)
    if errors:
        result["warnings"] = errors
    return result

def edit_wall_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    wall = doc.GetElement(DB.ElementId(int(params['wall_id'])))
    t = DB.Transaction(doc, "MCP: Edit Wall"); t.Start()
    if params.get('type_name'):
        wt = find_type_symbol(doc, DB.BuiltInCategory.OST_Walls, params['type_name'])
        if wt: wall.WallType = wt
    if params.get('height_mm') is not None:
        p = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
        if p: p.Set(mm_to_ft(float(params['height_mm'])))
    if params.get('length_mm') is not None:
        lc = wall.Location
        if hasattr(lc, "Curve") and isinstance(lc.Curve, DB.Line):
            old_line = lc.Curve
            new_end = old_line.GetEndPoint(0) + old_line.Direction.Normalize() * mm_to_ft(float(params['length_mm']))
            try: lc.Curve = DB.Line.CreateBound(old_line.GetEndPoint(0), new_end)
            except Exception as e: return {"error": "Failed to resize: " + str(e)}
    t.Commit()
    return {"success": True}

def move_element_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    eid = DB.ElementId(int(params['element_id']))
    dx, dy, dz = params.get('dx_mm', 0), params.get('dy_mm', 0), params.get('dz_mm', 0)
    direction, dist = params.get('direction', '').lower(), params.get('distance_mm', 0)
    if direction == 'north': dy += dist
    elif direction == 'south': dy -= dist
    elif direction == 'east': dx += dist
    elif direction == 'west': dx -= dist
    t = DB.Transaction(doc, "MCP: Move"); t.Start()
    DB.ElementTransformUtils.MoveElement(doc, eid, DB.XYZ(mm_to_ft(dx), mm_to_ft(dy), mm_to_ft(dz)))
    t.Commit()
    return {"success": True}

def move_staircase_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    import math
    uiapp = _get_revit_app()
    doc = uiapp.ActiveUIDocument.Document
    from revit_mcp.building_generator import get_model_registry
    from revit_mcp.utils import mm_to_ft, ft_to_mm

    stair_idx = params['stair_idx']
    target_x = params['target_x_mm']
    target_y = params['target_y_mm']

    registry = get_model_registry(doc)
    
    stair_elements = {}
    floor_min_x, floor_min_y = float('inf'), float('inf')
    floor_max_x, floor_max_y = float('-inf'), float('-inf')

    for ai_id, eid in registry.items():
        if ai_id.startswith("AI_Stair_"):
            parts = ai_id.split("_")
            try:
                idx = int(parts[2])
                stair_elements.setdefault(idx, []).append(eid)
            except: pass
        elif ai_id.startswith("AI_Floor_"):
            try:
                el = doc.GetElement(eid)
                if el and hasattr(el, "get_BoundingBox"):
                    bb = el.get_BoundingBox(None)
                    if bb:
                        floor_min_x = min(floor_min_x, ft_to_mm(bb.Min.X))
                        floor_min_y = min(floor_min_y, ft_to_mm(bb.Min.Y))
                        floor_max_x = max(floor_max_x, ft_to_mm(bb.Max.X))
                        floor_max_y = max(floor_max_y, ft_to_mm(bb.Max.Y))
            except: pass

    if stair_idx not in stair_elements:
        return {"error": "Staircore {} not found in model registry.".format(stair_idx)}

    if floor_min_x == float('inf'):
        return {"error": "Could not determine floor boundaries."}

    floor_w = floor_max_x - floor_min_x
    floor_l = floor_max_y - floor_min_y
    f_center_x = (floor_min_x + floor_max_x) / 2.0
    f_center_y = (floor_min_y + floor_max_y) / 2.0

    current_positions = {}
    for idx, eids in stair_elements.items():
        min_x, min_y, max_x, max_y = float('inf'), float('inf'), float('-inf'), float('-inf')
        for eid in eids:
            try:
                el = doc.GetElement(eid)
                if el and hasattr(el, "get_BoundingBox"):
                    bb = el.get_BoundingBox(None)
                    if bb:
                        min_x = min(min_x, ft_to_mm(bb.Min.X))
                        min_y = min(min_y, ft_to_mm(bb.Min.Y))
                        max_x = max(max_x, ft_to_mm(bb.Max.X))
                        max_y = max(max_y, ft_to_mm(bb.Max.Y))
            except: pass
        if min_x != float('inf'):
            current_positions[idx] = ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)
        else:
            current_positions[idx] = (f_center_x, f_center_y)

    curr_x, curr_y = current_positions[stair_idx]
    dx = target_x - curr_x
    dy = target_y - curr_y

    from revit_mcp.staircase_logic import _check_travel_distance
    
    def evaluate_positions(test_x, test_y):
        positions_rel = []
        for idx, (cx, cy) in current_positions.items():
            if idx == stair_idx:
                px, py = test_x, test_y
            else:
                px, py = cx, cy
            positions_rel.append((px - f_center_x, py - f_center_y))
        return _check_travel_distance(positions_rel, floor_w, floor_l, 60000)

    is_valid = evaluate_positions(target_x, target_y)

    if not is_valid:
        best_cand = None
        min_dist = float('inf')
        step = 1000 
        srch_range = 20000 
        
        for ring in range(1, (srch_range // step) + 1):
            offset = ring * step
            points_to_test = [
                (target_x + offset, target_y),
                (target_x - offset, target_y),
                (target_x, target_y + offset),
                (target_x, target_y - offset),
                (target_x + offset, target_y + offset),
                (target_x - offset, target_y + offset),
                (target_x + offset, target_y - offset),
                (target_x - offset, target_y - offset),
            ]
            for pt_x, pt_y in points_to_test:
                if pt_x < floor_min_x or pt_x > floor_max_x or pt_y < floor_min_y or pt_y > floor_max_y:
                    continue
                if evaluate_positions(pt_x, pt_y):
                    d = math.sqrt((pt_x - target_x)**2 + (pt_y - target_y)**2)
                    if d < min_dist:
                        min_dist = d
                        best_cand = (pt_x, pt_y)
            if best_cand: 
                break
                
        if best_cand:
            return {
                "error": "The requested location violates the 60m fire safety travel rule.",
                "complies": False,
                "suggested_x": best_cand[0],
                "suggested_y": best_cand[1]
            }
        else:
            return {
                "error": "The requested location violates the 60m fire safety travel rule, and no valid nearby location could be suggested.",
                "complies": False
            }

    t = DB.Transaction(doc, "MCP: Move Staircase {}".format(stair_idx))
    t.Start()
    from System.Collections.Generic import List
    ids = List[DB.ElementId]()
    for eid in stair_elements[stair_idx]:
        ids.Add(eid)
    
    DB.ElementTransformUtils.MoveElements(doc, ids, DB.XYZ(mm_to_ft(dx), mm_to_ft(dy), 0.0))
    t.Commit()

    return {"success": True, "complies": True, "message": "Staircore {} moved successfully.".format(stair_idx)}

def create_floor_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    import System.Collections.Generic as Generic # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    w, l = mm_to_ft(params.get('width_mm', 5000)), mm_to_ft(params.get('length_mm', 5000))
    x, y = mm_to_ft(params.get('center_x', 0)), mm_to_ft(params.get('center_y', 0))
    try:
        p1, p2, p3, p4 = DB.XYZ(x-w/2, y-l/2, 0), DB.XYZ(x+w/2, y-l/2, 0), DB.XYZ(x+w/2, y+l/2, 0), DB.XYZ(x-w/2, y+l/2, 0)
        profile = DB.CurveLoop()
        profile.Append(DB.Line.CreateBound(p1, p2)); profile.Append(DB.Line.CreateBound(p2, p3))
        profile.Append(DB.Line.CreateBound(p3, p4)); profile.Append(DB.Line.CreateBound(p4, p1))
        loops = Generic.List[DB.CurveLoop](); loops.Add(profile)
        lvl = find_level(doc, params.get('level_name') or params.get('level_id'))
        ftype = DB.FilteredElementCollector(doc).OfClass(DB.FloorType).FirstElement()
        t = DB.Transaction(doc, "MCP: Create Floor"); t.Start()
        from revit_mcp.utils import setup_failure_handling
        setup_failure_handling(t, use_nuclear=True)
        new_floor = DB.Floor.Create(doc, loops, ftype.Id, lvl.Id)
        state_manager.set_ai_metadata(new_floor, params.get('ai_id') or "AI_Floor_{}".format(new_floor.Id.Value))
        t.Commit()
        return {"success": True, "floor_id": str(new_floor.Id.Value)}
    except Exception as e: return {"error": str(e)}

def create_column_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    level, symbol = find_level(doc, params.get('level_name')), find_type_symbol(doc, DB.BuiltInCategory.OST_Columns, params.get('type_name'))
    if not symbol.IsActive:
        t = DB.Transaction(doc, "Load symbol"); t.Start(); symbol.Activate(); t.Commit()
    p = DB.XYZ(mm_to_ft(params.get('x', 0)), mm_to_ft(params.get('y', 0)), level.Elevation)
    t = DB.Transaction(doc, "MCP: Column"); t.Start()
    inst = doc.Create.NewFamilyInstance(p, symbol, level, DB.Structure.StructuralType.NonStructural)
    rot = params.get('rotation_degrees', 0)
    if rot != 0: DB.ElementTransformUtils.RotateElement(doc, inst.Id, DB.Line.CreateBound(p, p+DB.XYZ.BasisZ), rot * (math.pi / 180.0))
    t.Commit()
    return {"success": True, "id": str(inst.Id.Value)}

def create_hosted_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    category = DB.BuiltInCategory.OST_Doors if params['category'] == 'door' else DB.BuiltInCategory.OST_Windows
    symbol, wall = find_type_symbol(doc, category, params.get('type_name')), doc.GetElement(DB.ElementId(int(params['wall_id'])))
    if not symbol.IsActive:
        t = DB.Transaction(doc, "Activate"); t.Start(); symbol.Activate(); t.Commit()
    line = wall.Location.Curve
    p = line.GetEndPoint(0) + line.Direction.Normalize() * mm_to_ft(params.get('offset_mm', 1000))
    t = DB.Transaction(doc, "MCP: Hosted"); t.Start()
    inst = doc.Create.NewFamilyInstance(p, symbol, wall, doc.GetElement(wall.LevelId), DB.Structure.StructuralType.NonStructural)
    sill = params.get('sill_height_mm')
    if sill is not None:
        p_sill = inst.get_Parameter(DB.BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
        if p_sill: p_sill.Set(mm_to_ft(sill))
    t.Commit()
    return {"success": True, "id": str(inst.Id.Value)}

def edit_column_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    inst = doc.GetElement(DB.ElementId(int(params['column_id'])))
    t = DB.Transaction(doc, "MCP: Edit Column"); t.Start()
    if params.get('type_name'):
        symbol = find_type_symbol(doc, DB.BuiltInCategory.OST_Columns, params['type_name'])
        if symbol: inst.Symbol = symbol
    if params.get('x') is not None or params.get('y') is not None:
        loc = inst.Location
        old_p = loc.Point
        new_x, new_y = mm_to_ft(params.get('x', old_p.X*304.8)), mm_to_ft(params.get('y', old_p.Y*304.8))
        loc.Point = DB.XYZ(new_x, new_y, old_p.Z)
    if params.get('rotation_degrees') is not None:
        p = inst.Location.Point
        DB.ElementTransformUtils.RotateElement(doc, inst.Id, DB.Line.CreateBound(p, p+DB.XYZ.BasisZ), params['rotation_degrees']*(math.pi/180.0))
    t.Commit()
    return {"success": True}

def edit_hosted_element_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    inst = doc.GetElement(DB.ElementId(int(params['element_id'])))
    t = DB.Transaction(doc, "MCP: Edit Hosted"); t.Start()
    if params.get('type_name'):
        symbol = find_type_symbol(doc, inst.Category.BuiltInCategory, params['type_name'])
        if symbol: inst.Symbol = symbol
    if params.get('offset_mm') is not None and inst.Host:
        line = inst.Host.Location.Curve
        p_new = line.GetEndPoint(0) + line.Direction.Normalize()*mm_to_ft(params['offset_mm'])
        DB.ElementTransformUtils.MoveElement(doc, inst.Id, p_new - inst.Location.Point)
    if params.get('sill_height_mm') is not None:
        p_sill = inst.get_Parameter(DB.BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
        if p_sill: p_sill.Set(mm_to_ft(params['sill_height_mm']))
    t.Commit()
    return {"success": True}

def duplicate_family_type_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    cat_map = {"door": DB.BuiltInCategory.OST_Doors, "window": DB.BuiltInCategory.OST_Windows, "column": DB.BuiltInCategory.OST_Columns, "wall": DB.BuiltInCategory.OST_Walls, "floor": DB.BuiltInCategory.OST_Floors}
    source = find_type_symbol(doc, cat_map.get(params['category'].lower(), DB.BuiltInCategory.OST_Doors), params.get('source_type_name'))
    if not source: return {"error": "Source not found"}
    t = DB.Transaction(doc, "MCP: Create Type"); t.Start()
    new_type = source.Duplicate(params['new_name'])
    p_dict = params.get('parameters', {})
    if params['category'].lower() in ["wall", "floor"] and "thickness_mm" in p_dict:
        cs = new_type.GetCompoundStructure()
        if cs:
            layers = cs.GetLayers()
            for i in range(layers.Count):
                if layers[i].Function == DB.MaterialFunctionAssignment.Structure:
                    layers[i].Width = mm_to_ft(float(p_dict["thickness_mm"])); break
            cs.SetLayers(layers); new_type.SetCompoundStructure(cs)
    set_params_batch(new_type, p_dict)
    t.Commit()
    return {"success": True, "id": str(new_type.Id.Value)}

def place_family_instance_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    symbol = next((s for s in DB.FilteredElementCollector(doc).OfClass(DB.FamilySymbol) if s.Name.lower() == params['type_name'].lower()), None)
    if not symbol: return {"error": "Symbol not found"}
    level = find_level(doc, params.get('level_name'))
    p = DB.XYZ(mm_to_ft(params.get('x', 0)), mm_to_ft(params.get('y', 0)), mm_to_ft(params.get('z', 0)) + level.Elevation)
    t = DB.Transaction(doc, "MCP: Place Family"); t.Start()
    if not symbol.IsActive: symbol.Activate()
    inst = doc.Create.NewFamilyInstance(p, symbol, level, DB.Structure.StructuralType.NonStructural)
    rot = params.get('rotation', 0)
    if rot != 0: DB.ElementTransformUtils.RotateElement(doc, inst.Id, DB.Line.CreateBound(p, p+DB.XYZ.BasisZ), rot * (math.pi / 180.0))
    set_params_batch(inst, params.get('parameters', {}))
    t.Commit()
    return {"success": True, "id": str(inst.Id.Value)}

def edit_element_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    el = doc.GetElement(DB.ElementId(int(params['element_id'])))
    t = DB.Transaction(doc, "MCP: Edit Element"); t.Start()
    if params.get('type_name'):
        new_sym = find_type_symbol(doc, el.Category.BuiltInCategory, params['type_name'])
        if new_sym:
            if hasattr(el, 'Symbol'): el.Symbol = new_sym
            elif isinstance(el, DB.Wall): el.WallType = new_sym
            elif isinstance(el, DB.Floor): el.FloorType = new_sym
    if any(params.get(p) is not None for p in ["x", "y", "z"]) and hasattr(el.Location, 'Point'):
        p = el.Location.Point
        el.Location.Point = DB.XYZ(mm_to_ft(params.get('x', p.X*304.8)), mm_to_ft(params.get('y', p.Y*304.8)), mm_to_ft(params.get('z', p.Z*304.8)))
    if params.get('rotation_degrees') is not None:
        p = el.Location.Point if hasattr(el.Location, 'Point') else DB.XYZ.Zero
        DB.ElementTransformUtils.RotateElement(doc, el.Id, DB.Line.CreateBound(p, p+DB.XYZ.BasisZ), params['rotation_degrees'] * (math.pi / 180.0))
    set_params_batch(el, params.get('parameters', {}))
    t.Commit()
    return {"success": True}

def edit_type_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    etype = doc.GetElement(DB.ElementId(int(params['type_id']))) if params.get('type_id') else next((t for t in DB.FilteredElementCollector(doc).OfClass(DB.ElementType) if params['type_name'].lower() in t.Name.lower()), None)
    if not etype: return {"error": "Type not found"}
    t = DB.Transaction(doc, "MCP: Edit Type"); t.Start(); set_params_batch(etype, params.get('parameters', {})); t.Commit()
    return {"success": True}

def regenerate_staircases_ui():
    import Autodesk.Revit.DB as DB # type: ignore
    from revit_mcp.revit_workers import RevitWorkers
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    workers = RevitWorkers(doc)
    try:
        res = workers.regenerate_staircases_only()
        return res
    except Exception as e:
        import traceback
        return {"status": "Error", "message": "Failed: {}\n{}".format(str(e), traceback.format_exc())}

def get_building_metrics_ui():
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    lvls = sorted([l for l in DB.FilteredElementCollector(doc).OfClass(DB.Level) if "AI" in l.Name], key=lambda x: x.Elevation)
    if not lvls: return 0, 0, True
    count = len(lvls)
    if count < 2: return count, 0, True
    h = DB.UnitUtils.ConvertFromInternalUnits(lvls[1].Elevation - lvls[0].Elevation, DB.UnitTypeId.Millimeters)
    consistent = all(abs(DB.UnitUtils.ConvertFromInternalUnits(lvls[i].Elevation - lvls[i-1].Elevation, DB.UnitTypeId.Millimeters) - h) < 1.0 for i in range(1, count))
    return count, h, consistent

def list_elements_ui(category):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    cl = DB.FilteredElementCollector(doc)
    cat = category.lower()
    if "wall" in cat: cl.OfClass(DB.Wall)
    elif "floor" in cat: cl.OfClass(DB.Floor)
    elif "level" in cat: cl.OfClass(DB.Level)
    elif "grid" in cat: cl.OfClass(DB.Grid)
    elif "door" in cat: cl.OfCategory(DB.BuiltInCategory.OST_Doors).OfClass(DB.FamilyInstance)
    elif "window" in cat: cl.OfCategory(DB.BuiltInCategory.OST_Windows).OfClass(DB.FamilyInstance)
    elif "column" in cat: cl.OfCategory(DB.BuiltInCategory.OST_Columns).OfClass(DB.FamilyInstance)
    return [{"id": str(el.Id.Value), "name": el.Name} for el in cl]

def query_types_ui(category):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    cl = DB.FilteredElementCollector(doc)
    cat = category.lower()
    if "wall" in cat: cl.OfClass(DB.WallType)
    elif "floor" in cat: cl.OfClass(DB.FloorType)
    elif "door" in cat: cl.OfCategory(DB.BuiltInCategory.OST_Doors).OfClass(DB.FamilySymbol)
    elif "window" in cat: cl.OfCategory(DB.BuiltInCategory.OST_Windows).OfClass(DB.FamilySymbol)
    elif "column" in cat: cl.OfCategory(DB.BuiltInCategory.OST_Columns).OfClass(DB.FamilySymbol)
    return [{"id": str(t.Id.Value), "name": t.Name} for t in cl]

def create_polygon_floor_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    import System.Collections.Generic as Generic # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    points = params['points_mm']
    loop = DB.CurveLoop()
    for i in range(len(points)):
        seg_start = points[i]
        seg_end   = points[(i + 1) % len(points)]
        p1 = DB.XYZ(mm_to_ft(seg_start['x']), mm_to_ft(seg_start['y']), 0)
        p2 = DB.XYZ(mm_to_ft(seg_end['x']),   mm_to_ft(seg_end['y']),   0)
        if seg_start.get('mid'):
            mid = seg_start['mid']
            pm  = DB.XYZ(mm_to_ft(mid['x']), mm_to_ft(mid['y']), 0)
            loop.Append(DB.Arc.Create(p1, p2, pm))
        else:
            loop.Append(DB.Line.CreateBound(p1, p2))
    loops = Generic.List[DB.CurveLoop](); loops.Add(loop)
    lvl = find_level(doc, params.get('level_name') or params.get('level_id'))
    ftype = DB.FilteredElementCollector(doc).OfClass(DB.FloorType).FirstElement()
    t = DB.Transaction(doc, "MCP: Poly Floor"); t.Start()
    from revit_mcp.utils import setup_failure_handling
    setup_failure_handling(t, use_nuclear=True)
    nf = DB.Floor.Create(doc, loops, ftype.Id, lvl.Id); t.Commit()
    return {"success": True, "id": str(nf.Id.Value)}

def create_level_ui(elevation_mm):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    t = DB.Transaction(doc, "MCP: Level"); t.Start(); lvl = DB.Level.Create(doc, mm_to_ft(elevation_mm)); t.Commit()
    return {"id": str(lvl.Id.Value), "name": lvl.Name}

def create_grid_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    p1, p2 = DB.XYZ(mm_to_ft(params['x1']), mm_to_ft(params['y1']), 0), DB.XYZ(mm_to_ft(params['x2']), mm_to_ft(params['y2']), 0)
    line = DB.Line.CreateBound(p1, p2)
    t = DB.Transaction(doc, "MCP: Grid"); t.Start(); grid = DB.Grid.Create(doc, line); t.Commit()
    return {"id": str(grid.Id.Value), "name": grid.Name}

def create_arc_grid_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    p1, p2, pm = DB.XYZ(mm_to_ft(params['start_x']), mm_to_ft(params['start_y']), 0), DB.XYZ(mm_to_ft(params['end_x']), mm_to_ft(params['end_y']), 0), DB.XYZ(mm_to_ft(params['mid_x']), mm_to_ft(params['mid_y']), 0)
    t = DB.Transaction(doc, "MCP: Arc Grid"); t.Start(); grid = DB.Grid.Create(doc, DB.Arc.Create(p1, p2, pm)); t.Commit()
    return {"id": str(grid.Id.Value), "name": grid.Name}

def edit_grid_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    grid = doc.GetElement(DB.ElementId(int(params['grid_id'])))
    t = DB.Transaction(doc, "MCP: Edit Grid"); t.Start()
    if 'name' in params: grid.Name = params['name']
    if 'type_name' in params:
        gt = next((g for g in DB.FilteredElementCollector(doc).OfClass(DB.GridType) if params['type_name'].lower() in g.Name.lower()), None)
        if gt: grid.GridType = gt
    t.Commit()
    return {"success": True}

def set_parameter_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    el = doc.GetElement(DB.ElementId(int(params['element_id'])))
    t = DB.Transaction(doc, "MCP: Set Param"); t.Start()
    bip = get_bip(params['parameter_name'])
    param = el.get_Parameter(bip) if bip else el.LookupParameter(params['parameter_name'])
    if param:
        val = params['value']
        if param.StorageType == DB.StorageType.Double: param.Set(float(val))
        elif param.StorageType == DB.StorageType.Integer: param.Set(int(val))
        elif param.StorageType == DB.StorageType.String: param.Set(str(val))
    t.Commit()
    return {"success": True}

def get_building_presets_ui():
    try:
        current_dir = os.path.dirname(__file__)
        file_path = os.path.join(current_dir, "building_presets.json")
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"error": str(e)}
