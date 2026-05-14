# -*- coding: utf-8 -*-
import os
import math
import re
import json

def load_presets():
    try:
        preset_path = os.path.join(os.path.dirname(__file__), "building_presets.json")
        if os.path.exists(preset_path):
            with open(preset_path, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def load_compliance(name):
    """Load compliance_{name}.json from same directory as building_presets.json."""
    try:
        path = os.path.join(os.path.dirname(__file__), "compliance_{}.json".format(name))
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

_cached_log_path = None

def get_appdata_path(subfolder):
    """Return %APPDATA%\\RevitMCP\\<subfolder>, creating directories if needed."""
    base = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "RevitMCP", subfolder)
    os.makedirs(base, exist_ok=True)
    return base

def get_log_path():
    global _cached_log_path
    if _cached_log_path:
        return _cached_log_path
    _cached_log_path = os.path.join(get_appdata_path("logs"), "fastmcp_server.log")
    return _cached_log_path

def safe_num(val, default=0):
    try:
        if val is None: return float(default) if default is not None else None
        if isinstance(val, (int, float)): return float(val)
        if isinstance(val, str):
            m = re.findall(r"[-+]?\d*\.\d+|\d+", val)
            if m: return float(m[0])
        return float(default) if default is not None else None
    except:
        return float(default) if default is not None else None

def get_random_dim(val, base, variation=0.2):
    import random
    if isinstance(val, str) and "random" in val.lower():
        v = float(base)
        return v * (1.0 + random.uniform(-variation, variation))
    return safe_num(val, base)

def mm_to_ft(mm):
    import Autodesk.Revit.DB as DB # type: ignore
    return DB.UnitUtils.ConvertToInternalUnits(safe_num(mm), DB.UnitTypeId.Millimeters)

def ft_to_mm(ft):
    import Autodesk.Revit.DB as DB # type: ignore
    return DB.UnitUtils.ConvertFromInternalUnits(float(ft), DB.UnitTypeId.Millimeters)

def sqmm_to_sqft(sqmm):
    import Autodesk.Revit.DB as DB # type: ignore
    return DB.UnitUtils.ConvertToInternalUnits(float(sqmm), DB.UnitTypeId.SquareMillimeters)

def safe_set_comment(element, tag):
    import Autodesk.Revit.DB as DB # type: ignore
    p = element.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
    if not p:
        p = element.LookupParameter("Comments")
    if p:
        p.Set(tag)
    return p

def get_bip(name):
    import Autodesk.Revit.DB as DB # type: ignore
    try: return getattr(DB.BuiltInParameter, name)
    except: return None

def find_level(doc, level_id_or_name=None):
    import Autodesk.Revit.DB as DB # type: ignore
    cl = DB.FilteredElementCollector(doc).OfClass(DB.Level)
    if not level_id_or_name:
        return cl.FirstElement()
    try:
        eid = DB.ElementId(int(level_id_or_name))
        lvl = doc.GetElement(eid)
        if isinstance(lvl, DB.Level): return lvl
    except: pass
    for lvl in cl:
        if level_id_or_name.lower() in lvl.Name.lower():
            return lvl
    return cl.FirstElement()

def find_type_symbol(doc, category_bip, type_name=None):
    import Autodesk.Revit.DB as DB # type: ignore
    cl = DB.FilteredElementCollector(doc).OfCategory(category_bip).OfClass(DB.ElementType)
    if not type_name:
        return cl.FirstElement()
    for sym in cl:
        if sym.Name.lower() == type_name.lower():
            return sym
    for sym in cl:
        if type_name.lower() in sym.Name.lower():
            return sym
    return cl.FirstElement()

def set_params_batch(element, params_dict):
    import Autodesk.Revit.DB as DB # type: ignore
    for p_name, p_val in params_dict.items():
        param = element.LookupParameter(p_name)
        if not param:
            bip = get_bip(p_name)
            if bip: param = element.get_Parameter(bip)
        
        if param and not param.IsReadOnly:
            if param.StorageType == DB.StorageType.Double:
                low = p_name.lower()
                if any(x in low for x in ["width", "height", "depth", "thickness", "length", "radius", "diameter", "offset", "sill", "elevation"]):
                    param.Set(mm_to_ft(float(p_val)))
                else:
                    param.Set(float(p_val))
            elif param.StorageType == DB.StorageType.Integer: param.Set(int(p_val))
            elif param.StorageType == DB.StorageType.String: param.Set(str(p_val))
            elif param.StorageType == DB.StorageType.ElementId: param.Set(DB.ElementId(int(p_val)))

def get_location_line_param(wall):
    import Autodesk.Revit.DB as DB # type: ignore
    bip = getattr(DB.BuiltInParameter, "WALL_KEY_REF_PARAM", None)
    if bip:
        p = wall.get_Parameter(bip)
        if p: return p
    return wall.LookupParameter("Location Line")

def nuclear_lockdown(doc):
    """
    CRITICAL PERFORMANCE GUARD:
    Forcefully disjoints EVERY wall in the entire project before a major operation.
    This prevents 'Neighbor Healing' and accidental joins to user walls.
    """
    import Autodesk.Revit.DB as DB # type: ignore
    all_walls = DB.FilteredElementCollector(doc).OfClass(DB.Wall).WhereElementIsNotElementType().ToElements()
    if len(all_walls) == 0: return
    
    t = DB.Transaction(doc, "BIM: Nuclear Lockdown")
    t.Start()
    setup_failure_handling(t, use_nuclear=True)
    count = 0
    for wall in all_walls:
        if wall and isinstance(wall, DB.Wall):
            disallow_joins(wall)
            count += 1
    t.Commit()
    t.Dispose()
    return count

def setup_failure_handling(transaction, use_nuclear=False):
    import Autodesk.Revit.DB as DB # type: ignore
    try:
        from revit_mcp.preprocessors import HideJoinFailuresPreprocessor, NuclearJoinGuard
        doc = transaction.GetDocument()
        if use_nuclear:
            preprocessor = NuclearJoinGuard(doc)
        else:
            preprocessor = HideJoinFailuresPreprocessor(doc)
        options = transaction.GetFailureHandlingOptions()
        options.SetFailuresPreprocessor(preprocessor)
        transaction.SetFailureHandlingOptions(options)
    except Exception:
        options = transaction.GetFailureHandlingOptions()
        options.SetClearAfterRollback(True)
        options.SetForcedModalHandling(False)
        transaction.SetFailureHandlingOptions(options)

def disallow_joins(wall):
    import Autodesk.Revit.DB as DB # type: ignore
    if not wall or not isinstance(wall, DB.Wall): return
    if hasattr(wall, "SetAllowAutoJoin"):
        wall.SetAllowAutoJoin(False)
    try:
        DB.WallUtils.DisallowWallJoinAtEnd(wall, 0)
        DB.WallUtils.DisallowWallJoinAtEnd(wall, 1)
    except: pass
    try:
        rb_param = wall.get_Parameter(DB.BuiltInParameter.WALL_ATTR_ROOM_BOUNDING)
        if rb_param and not rb_param.IsReadOnly: rb_param.Set(0)
    except: pass
    try:
        ana_param = wall.get_Parameter(DB.BuiltInParameter.STRUCTURAL_ANALYZABLE)
        if ana_param and not ana_param.IsReadOnly: ana_param.Set(0)
    except: pass
    try:
        top_attach = wall.get_Parameter(DB.BuiltInParameter.WALL_TOP_IS_ATTACHED)
        if top_attach and not top_attach.IsReadOnly: top_attach.Set(0)
        base_attach = wall.get_Parameter(DB.BuiltInParameter.WALL_BOTTOM_IS_ATTACHED)
        if base_attach and not base_attach.IsReadOnly: base_attach.Set(0)
    except: pass
    try:
        wt = wall.WallType
        p = wt.get_Parameter(DB.BuiltInParameter.WALL_DEFAULT_JOIN_TYPE)
        if p and not p.IsReadOnly: p.Set(1) # 1 = Miter
    except: pass
