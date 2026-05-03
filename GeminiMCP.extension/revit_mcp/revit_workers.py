# -*- coding: utf-8 -*-
# NOTE: Do NOT import Autodesk.Revit.DB at module level.
# Each method does its own local import on the correct thread.
from revit_mcp.gemini_client import client
from revit_mcp.bridge import mcp_event_handler
from revit_mcp.utils import (
    safe_num, mm_to_ft, sqmm_to_sqft, 
    safe_set_comment, get_location_line_param,
    get_random_dim, load_presets, ft_to_mm, disallow_joins
)
from . import lift_logic
from . import staircase_logic
from . import fire_safety_logic
import math


def _merge_void_rects(rects):
    """Merge OVERLAPPING axis-aligned rectangles into non-overlapping ones.
    Each rect is (x1, y1, x2, y2).
    Uses a small 1mm overlap threshold to avoid merging rectangles that are 
    merely touching (which happens often with clipped perimeter voids).
    """
    if len(rects) <= 1:
        return list(rects)
    
    # Use a small epsilon (1mm in feet) to ensure we ONLY merge if they actually overlap.
    # 1mm is approx 0.00328 ft.
    eps = 0.00328
    
    merged = list(rects)
    changed = True
    while changed:
        changed = False
        out = []
        used = [False] * len(merged)
        for i in range(len(merged)):
            if used[i]:
                continue
            ax1, ay1, ax2, ay2 = merged[i]
            for j in range(i + 1, len(merged)):
                if used[j]:
                    continue
                bx1, by1, bx2, by2 = merged[j]
                # Check actual OVERLAP (not just touching)
                if ax1 < bx2 - eps and ax2 > bx1 + eps and ay1 < by2 - eps and ay2 > by1 + eps:
                    ax1 = min(ax1, bx1)
                    ay1 = min(ay1, by1)
                    ax2 = max(ax2, bx2)
                    ay2 = max(ay2, by2)
                    used[j] = True
                    changed = True
            out.append((ax1, ay1, ax2, ay2))
            used[i] = True
        merged = out
    return merged


def _make_circle_footprint(radius_mm, n=48):
    """Generate an n-vertex polygon approximating a circle (CCW winding).
    No arc mid-points — polygon floors work reliably in Revit; walls with many
    short straight segments look smooth at architectural scale."""
    pts = []
    for i in range(n):
        angle = math.pi / 2.0 + 2.0 * math.pi * i / n  # start north, go CCW
        pts.append([radius_mm * math.cos(angle), radius_mm * math.sin(angle)])
    return pts


def _make_ellipse_footprint(rx_mm, ry_mm, n=48):
    """Generate an n-vertex polygon approximating an ellipse (CCW winding)."""
    pts = []
    for i in range(n):
        angle = math.pi / 2.0 + 2.0 * math.pi * i / n  # start north, go CCW
        pts.append([rx_mm * math.cos(angle), ry_mm * math.sin(angle)])
    return pts


def _expand_shape_shorthand(shell):
    """Expand shell['shape'] into shell['footprint_points'] if not already present.
    Supports 'circle', 'ellipse', 'oval'. Modifies shell in-place and returns it.

    Also auto-promotes a plain rectangular shell to footprint_points when
    footprint_scale_overrides or footprint_offset_overrides are present — this
    ensures the organic per-level scale/offset pipeline is used instead of the
    static rectangular wall path, which ignores those keys entirely.
    """
    shape = str(shell.get("shape", "")).lower().strip()
    if not shape or shell.get("footprint_points"):
        # May still need promotion for scale/offset overrides
        pass
    else:
        w = safe_num(shell.get("width"), 30000)
        l = safe_num(shell.get("length"), w)
        if shape == "circle":
            r = max(w, l) / 2.0
            shell["footprint_points"] = _make_circle_footprint(r)
        elif shape in ("ellipse", "oval"):
            shell["footprint_points"] = _make_ellipse_footprint(w / 2.0, l / 2.0)

    # Promote rectangular shell → footprint_points when organic overrides are present.
    # footprint_scale_overrides / footprint_offset_overrides / footprint_rotation_overrides
    # are only processed by the organic wall/slab pipeline, which requires footprint_points.
    # Without this promotion a rectangular building with any of these overrides would
    # silently fall through to the static 4-wall path and produce a uniform tower.
    if not shell.get("footprint_points") and (
        shell.get("footprint_scale_overrides")
        or shell.get("footprint_offset_overrides")
        or shell.get("footprint_rotation_overrides")
    ):
        w = safe_num(shell.get("width"), 30000)
        l = safe_num(shell.get("length"), w)
        hw, hl = w / 2.0, l / 2.0
        # CCW rectangle centred on origin
        shell["footprint_points"] = [
            [-hw, -hl],
            [ hw, -hl],
            [ hw,  hl],
            [-hw,  hl],
        ]
        try:
            from revit_mcp.gemini_client import client as _cl
            _cl.log("[ShapePromotion] Rectangular shell promoted to footprint_points ({}x{}mm) for organic scale/offset overrides.".format(w, l))
        except Exception:
            pass

    return shell


def _get_interpolated_rotation(rotation_overrides, level_1based):
    """Return the rotation in degrees for a given 1-based level index,
    linearly interpolating between sparse control-point keys.
    Levels outside the key range clamp to the nearest endpoint.
    Example: {"1": 0, "10": 90, "20": 180} → 45° at level 5."""
    if not rotation_overrides:
        return 0.0
    pts = {}
    for k, v in rotation_overrides.items():
        try:
            pts[int(k)] = float(v)
        except (ValueError, TypeError):
            pass
    if not pts:
        return 0.0
    sorted_keys = sorted(pts.keys())
    if level_1based <= sorted_keys[0]:
        return pts[sorted_keys[0]]
    if level_1based >= sorted_keys[-1]:
        return pts[sorted_keys[-1]]
    lo = max(k for k in sorted_keys if k <= level_1based)
    hi = min(k for k in sorted_keys if k > level_1based)
    t = (level_1based - lo) / float(hi - lo)
    return pts[lo] + t * (pts[hi] - pts[lo])


def _get_interpolated_scale(scale_overrides, level_1based):
    """Return the scale for a given 1-based level index, linearly interpolating
    between sparse control-point keys (e.g. {"1":1.1,"10":0.7,"20":1.1}).
    Levels outside the key range clamp to the nearest endpoint."""
    if not scale_overrides:
        return 1.0
    # Normalise all keys to int
    pts = {}
    for k, v in scale_overrides.items():
        try:
            pts[int(k)] = float(v)
        except (ValueError, TypeError):
            pass
    if not pts:
        return 1.0
    if level_1based in pts:
        return pts[level_1based]
    sorted_keys = sorted(pts.keys())
    if level_1based <= sorted_keys[0]:
        return pts[sorted_keys[0]]
    if level_1based >= sorted_keys[-1]:
        return pts[sorted_keys[-1]]
    # Find bracketing keys and lerp
    lo = max(k for k in sorted_keys if k < level_1based)
    hi = min(k for k in sorted_keys if k > level_1based)
    t = (level_1based - lo) / float(hi - lo)
    return pts[lo] + t * (pts[hi] - pts[lo])


def _scale_footprint(pts, scale, offset_x=0.0, offset_y=0.0, rotation_deg=0.0):
    """Scale footprint_points about [0,0], rotate, then apply an XY offset (all in mm).

    Order: scale → rotate → offset.
    rotation_deg rotates the (already-scaled) footprint about the origin before
    the centroid offset is applied. This produces a true per-level rotation for
    twist/screw forms without changing the footprint size.
    offset_x / offset_y shift the rotated footprint so the form can drift
    asymmetrically as it rises — positive X moves east, positive Y moves north.
    Arc mid-points are transformed identically to vertices.
    """
    import math as _math
    cos_r = _math.cos(_math.radians(rotation_deg))
    sin_r = _math.sin(_math.radians(rotation_deg))

    def _transform(x, y):
        xs = x * scale
        ys = y * scale
        xr = xs * cos_r - ys * sin_r
        yr = xs * sin_r + ys * cos_r
        return xr + offset_x, yr + offset_y

    result = []
    for pt in pts:
        rx, ry = _transform(pt[0], pt[1])
        new_pt = [rx, ry]
        if len(pt) > 2 and isinstance(pt[2], dict):
            ad = pt[2]
            mx, my = _transform(ad.get("mid_x", 0), ad.get("mid_y", 0))
            new_pt.append({"mid_x": mx, "mid_y": my})
        result.append(new_pt)
    return result


def _build_volume_map(manifest, num_levels):
    """Return a dict {level_1based: volume_dict} from manifest["volumes"].

    Each volume entry:
      {"id": "vol_base", "levels": [1, 5], "width": 40000, "length": 40000,
       "offset_x": 0, "offset_y": 0, "rotation_deg": 0,
       "footprint_points": [...]}   # optional — overrides width/length box

    Levels assigned to MULTIPLE volumes use the LAST one listed (higher in
    the array wins, so Gemini can layer corrections easily).
    """
    volumes = manifest.get("volumes")
    if not volumes:
        return {}
    vol_map = {}
    for vol in volumes:
        try:
            lo, hi = int(vol["levels"][0]), int(vol["levels"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        for lvl in range(lo, hi + 1):
            if 1 <= lvl <= num_levels:
                vol_map[lvl] = vol
    return vol_map


def _rotate_pts(pts_2d, angle_rad, cx=0.0, cy=0.0):
    """Rotate a list of [x, y] points about (cx, cy) by angle_rad.
    Points may have an optional arc-mid dict as a third element — it is also rotated.
    """
    cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
    result = []
    for pt in pts_2d:
        dx, dy = pt[0] - cx, pt[1] - cy
        rx = cx + dx * cos_a - dy * sin_a
        ry = cy + dx * sin_a + dy * cos_a
        new_pt = [rx, ry]
        if len(pt) > 2 and isinstance(pt[2], dict):
            ad = pt[2]
            mdx, mdy = ad.get("mid_x", cx) - cx, ad.get("mid_y", cy) - cy
            new_pt.append({
                "mid_x": cx + mdx * cos_a - mdy * sin_a,
                "mid_y": cy + mdx * sin_a + mdy * cos_a,
            })
        result.append(new_pt)
    return result


def _poly_area_mm2(pts):
    """Shoelace formula — returns polygon area in mm²."""
    n = len(pts)
    return abs(sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1]
                   for i in range(n))) / 2.0


def _normalise_footprint_holes(holes):
    """Ensure footprint_holes is [[polygon1_pts], [polygon2_pts], ...].

    Gemini may output a flat list [[x,y],[x,y],...] for a single-hole courtyard.
    The engine always expects a list-of-polygons [[[x,y],...], [[x,y],...]].
    """
    if not holes:
        return []
    # Already nested: first element is a list of [x,y] lists
    first = holes[0]
    if isinstance(first, (list, tuple)) and first and isinstance(first[0], (list, tuple)):
        return holes
    # Flat list of [x,y] points — wrap in outer list as a single-hole polygon
    if isinstance(first, (list, tuple)) and len(first) == 2 and isinstance(first[0], (int, float)):
        return [holes]
    return holes


def pre_analyse_floor_plate(manifest, compliance_overrides=None, user_goal=None):
    """Pure Python — no Revit API.

    Analyses a Pass-1 manifest (shell geometry + lift count only) and returns
    a structured dict with individual element dimensions and building geometry
    description for injection into the Pass-2 Gemini prompt.
    """
    import math as _pa_math

    setup       = manifest.get("project_setup", {})
    shell       = manifest.get("shell", {})
    lifts_cfg   = manifest.get("lifts", {})
    typology    = (manifest.get("typology") or setup.get("typology", "")).lower().replace(" ", "_")
    presets     = load_presets()
    preset      = presets.get(typology) or presets.get("default") or presets.get("commercial_office", {})

    # ── Pre-expand SVG footprint so classification / area use real polygon ───
    _pa_svg = shell.get("footprint_svg")
    if _pa_svg and not shell.get("footprint_points"):
        try:
            from revit_mcp.svg_to_footprint import svg_path_to_multiloop as _pa_s2m
            _pa_ml = _pa_s2m(_pa_svg)
            shell = dict(shell)
            shell["footprint_points"] = _pa_ml["outer"]
            _pa_off = _pa_ml.get("offset", [0.0, 0.0])
            if _pa_ml.get("holes"):
                shell["footprint_holes"] = _pa_ml["holes"]
            elif shell.get("footprint_holes"):
                _pa_raw_holes = _normalise_footprint_holes(shell["footprint_holes"])
                if _pa_off[0] != 0.0 or _pa_off[1] != 0.0:
                    _pa_raw_holes = [
                        [[p[0] - _pa_off[0], p[1] - _pa_off[1]] for p in h]
                        for h in _pa_raw_holes
                    ]
                shell["footprint_holes"] = _pa_raw_holes
        except Exception:
            pass  # SVG parse fail — fall back to width/length estimates

    # ── Typical floor height ─────────────────────────────────────────────────
    _preset_typical = safe_num(preset.get("floor_heights", {}).get("typical", 0), 0)
    _manifest_h     = safe_num(setup.get("level_height", 0), 0)
    typical_h_mm    = _manifest_h if _manifest_h > 0 else (_preset_typical if _preset_typical > 0 else 4000)

    num_storeys = max(1, safe_num(setup.get("levels", 5), 5))

    # ── Lift count ───────────────────────────────────────────────────────────
    num_lifts = lifts_cfg.get("count")
    if isinstance(num_lifts, dict):
        num_lifts = num_lifts.get("passenger", num_lifts.get("total", num_lifts.get("count", None)))
    _fw = safe_num(shell.get("width", 30000), 30000)
    _fl = safe_num(shell.get("length", 50000), 50000)
    fp_pts  = shell.get("footprint_points")
    if fp_pts and fp_pts[0] and isinstance(fp_pts[0][0], (list, tuple)):
        fp_pts = fp_pts[0]
    fp_holes = _normalise_footprint_holes(shell.get("footprint_holes", []))
    if fp_pts and len(fp_pts) >= 3:
        _floor_area_mm2 = _poly_area_mm2(fp_pts)
    else:
        _floor_area_mm2 = _fw * _fl
    _NFA_RATIO   = 0.60
    _occ_density = safe_num(lifts_cfg.get("occupancy_density", 0.067), 0.067)
    _total_occ   = max(100, (_floor_area_mm2 / 1e6) * _NFA_RATIO * _occ_density * num_storeys)
    _auto_lifts  = lift_logic.calculate_lift_requirements(num_storeys, typical_h_mm, _total_occ)
    if num_lifts is None or num_lifts == "random":
        num_lifts = _auto_lifts
    else:
        try:
            _manifest_n = int(num_lifts)
            if _manifest_n >= max(2, int(_auto_lifts * 0.8)):
                num_lifts = _manifest_n
            else:
                num_lifts = _auto_lifts
        except (TypeError, ValueError):
            num_lifts = _auto_lifts
    num_lifts = int(num_lifts)

    lobby_w = safe_num(lifts_cfg.get("lobby_width",
                       preset.get("core_logic", {}).get("lift_lobby_width", 3000)), 3000)

    # ── Compliance overrides ─────────────────────────────────────────────────
    cp = compliance_overrides or manifest.get("compliance_parameters", {})
    _cp_overrides = {}
    if cp.get("stair_riser_mm"):        _cp_overrides["max_riser_mm"]              = int(cp["stair_riser_mm"])
    if cp.get("stair_tread_mm"):        _cp_overrides["min_tread_mm"]              = int(cp["stair_tread_mm"])
    if cp.get("stair_headroom_mm"):     _cp_overrides["min_headroom_mm"]           = int(cp["stair_headroom_mm"])
    if cp.get("stair_overrun_mm"):      _cp_overrides["overrun_height_mm"]         = int(cp["stair_overrun_mm"])
    if cp.get("max_travel_distance_mm"):_cp_overrides["max_travel_distance_mm"]    = int(cp["max_travel_distance_mm"])

    # ── Passenger lift element dimensions ────────────────────────────────────
    _lift_internal = tuple(preset.get("core_logic", {}).get("lift_internal_size", [2500, 2500]))
    _lift_wall_t   = int(safe_num(preset.get("core_logic", {}).get("lift_wall_thickness", 200), 200))
    layout         = lift_logic.get_total_core_layout(num_lifts, internal_size=_lift_internal, lobby_width=lobby_w)
    _lift_car_w    = _lift_internal[0]
    _lift_car_d    = _lift_internal[1]
    _psgr_lobby_w  = layout["total_w"]
    _psgr_lobby_d  = lobby_w
    _psgr_lobby_a  = int(_psgr_lobby_w * _psgr_lobby_d)

    # Per-lift unit width: car + one wall thickness (walls are shared between adjacent cars,
    # so the marginal width added by each car is car_w + wall_t, not car_w + 2*wall_t).
    _lift_unit_w = _lift_car_w + _lift_wall_t

    passenger_lift_elements = {
        "num_lifts":              num_lifts,
        "lifts_per_block":        layout["lifts_per_block"],
        "num_blocks":             layout["num_blocks"],
        "lift_car_w_mm":          _lift_car_w,
        "lift_car_d_mm":          _lift_car_d,
        "lift_wall_thickness_mm": _lift_wall_t,
        "lift_car_unit_w_mm":     _lift_unit_w,
        "passenger_lobby_width_mm":  int(_psgr_lobby_w),
        "passenger_lobby_depth_mm":  int(_psgr_lobby_d),
        "passenger_lobby_area_mm2":  _psgr_lobby_a,
        "passenger_bank_total_depth_mm": int(layout["total_d"]),
    }

    # ── Fire cluster element dimensions ──────────────────────────────────────
    preset_fs   = preset.get("core_logic", {}).get("fire_safety", {})
    stair_spec  = preset_fs.get("staircase_spec", {}).copy()
    _lift_bounds_pre = (
        -layout["total_w"] / 2.0, -layout["total_d"] / 2.0,
         layout["total_w"] / 2.0,  layout["total_d"] / 2.0,
    )
    _floor_dims_stub = [(_fw, _fl)]
    try:
        _safety_sets = fire_safety_logic.calculate_fire_safety_requirements(
            _floor_dims_stub, [0.0, 0.0], _lift_bounds_pre,
            typical_h_mm, preset_fs, num_lifts, lobby_w,
            compliance_overrides=_cp_overrides,
            footprint_pts=fp_pts, footprint_holes=fp_holes,
            orientation="NS"
        )
        num_clusters = max(len(_safety_sets), 1)
    except Exception:
        num_clusters = 2

    # Individual fire cluster element sizes (from FSC constants / compliance JSON)
    from revit_mcp.fire_safety_logic import _FSC, _STC, _FL_CAR_SIZE, _FL_SHAFT_D, _WALL_THICKNESS
    _fl_car_w = int(safe_num(cp.get("fire_lift_car_width_mm",  _FSC.get("fire_lift", {}).get("car_size_mm", 2500)), 2500))
    _fl_car_d = int(safe_num(cp.get("fire_lift_car_depth_mm",  _FL_CAR_SIZE), _FL_CAR_SIZE))
    _fl_shaft_w = _fl_car_w + 2 * _WALL_THICKNESS
    _fl_shaft_d = _FL_SHAFT_D
    _fire_lobby_d   = int(safe_num(cp.get("fire_lobby_min_depth_mm",
                          _FSC.get("fire_lift", {}).get("lobby_min_depth_mm", 1800)), 1800))
    _fire_lobby_a   = int(safe_num(cp.get("fire_lobby_min_area_mm2",
                          _FSC.get("fire_lift", {}).get("lobby_min_area_mm2", 5400000)), 5400000))
    _smoke_d = int(safe_num(cp.get("smoke_stop_lobby_min_depth_mm",
                   _FSC.get("perimeter_staircase", {}).get("smoke_stop_lobby_clear_depth_mm", 2000)), 2000))
    _smoke_a = int(safe_num(cp.get("smoke_stop_lobby_min_area_mm2", 3000000), 3000000))
    _sw_nat, _sd_nat = staircase_logic.get_shaft_dimensions(typical_h_mm, stair_spec)
    _st_flight_w = int(stair_spec.get("width_of_flight",
                        safe_num(_FSC.get("staircase", {}).get("width_of_flight_mm", 1200), 1200)))
    _st_landing_d = int(stair_spec.get("landing_width",
                         safe_num(_FSC.get("staircase", {}).get("landing_width_mm", 1200), 1200)))

    # Compact rectangle: all 3 modules side-by-side parallel to bank face.
    # Perpendicular depth = deepest single module = staircase depth (sd_nat).
    _cluster_d_p   = int(_sd_nat)

    fire_cluster_elements = {
        "num_clusters":                  num_clusters,
        "fire_lift_car_w_mm":            _fl_car_w,
        "fire_lift_car_d_mm":            _fl_car_d,
        "fire_lift_shaft_w_mm":          int(_fl_shaft_w),
        "fire_lift_shaft_d_mm":          int(_fl_shaft_d),
        "fire_lobby_min_depth_mm":       _fire_lobby_d,
        "fire_lobby_min_area_mm2":       _fire_lobby_a,
        "smoke_stop_lobby_min_depth_mm": _smoke_d,
        "smoke_stop_lobby_min_area_mm2": _smoke_a,
        "staircase_flight_w_mm":         _st_flight_w,
        "staircase_landing_d_mm":        _st_landing_d,
        "staircase_shaft_w_mm":          int(_sw_nat),
        "staircase_shaft_d_mm":          int(_sd_nat),
        "cluster_assembly_depth_mm":     _cluster_d_p,
    }

    staircase_count_required = max(num_clusters, 2)

    # ── Classify footprint shape ─────────────────────────────────────────────
    _has_pts   = bool(fp_pts and len(fp_pts) >= 3)
    _has_svg   = bool(shell.get("footprint_svg"))
    _has_holes = bool(fp_holes)
    _has_vols  = bool(manifest.get("volumes"))
    _shape_str = shell.get("shape", "")

    if _shape_str in ("circle", "ellipse"):
        shape_classification = _shape_str
    elif _has_pts and _has_holes:
        shape_classification = "courtyard"
    elif _has_pts:
        # Count concave vertices to classify irregular polygons
        n = len(fp_pts)
        _concave = 0
        for _vi in range(n):
            ax, ay = fp_pts[(_vi - 1) % n]
            bx, by = fp_pts[_vi]
            cx, cy = fp_pts[(_vi + 1) % n]
            _cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
            if _cross < 0:
                _concave += 1
        if _concave <= 2:
            shape_classification = "L-shape"
        elif _concave <= 4:
            shape_classification = "U-shape or T-shape"
        else:
            shape_classification = "H-shape or complex"
    elif _has_svg:
        shape_classification = "organic_svg"
    elif _has_holes:
        shape_classification = "courtyard"
    elif _has_vols:
        shape_classification = "volumes_composite"
    else:
        shape_classification = "rectangular"

    # ── Floor plate variation analysis ───────────────────────────────────────
    _w_base = safe_num(shell.get("width", 30000), 30000)
    _l_base = safe_num(shell.get("length", 50000), 50000)
    overrides = shell.get("floor_overrides", {})
    scale_ovr = shell.get("footprint_scale_overrides", {})
    _all_ws = [_w_base]; _all_ls = [_l_base]
    for _k, _ov in overrides.items():
        _all_ws.append(safe_num(_ov.get("width", _w_base), _w_base))
        _all_ls.append(safe_num(_ov.get("length", _l_base), _l_base))
    for _k, _sc in scale_ovr.items():
        try:
            _s = float(_sc)
            _all_ws.append(_w_base * _s); _all_ls.append(_l_base * _s)
        except (TypeError, ValueError):
            pass
    _min_w, _max_w = int(min(_all_ws)), int(max(_all_ws))
    _min_l, _max_l = int(min(_all_ls)), int(max(_all_ls))
    _is_uniform    = (_min_w == _max_w) and (_min_l == _max_l) and not overrides and not scale_ovr
    if _is_uniform:
        _var_desc = "identical all floors"
    elif overrides:
        _var_desc = "varies per floor_overrides on specific levels"
    elif scale_ovr:
        _var_desc = "tapers via footprint_scale_overrides from {:.0f}×{:.0f} to {:.0f}×{:.0f} mm".format(
            _max_w, _max_l, _min_w, _min_l)
    else:
        _var_desc = "varies (check floor_overrides)"

    floor_plate_variation = {
        "is_uniform":        _is_uniform,
        "smallest_plate_mm": [_min_w, _min_l],
        "largest_plate_mm":  [_max_w, _max_l],
        "description":       _var_desc,
    }

    # ── 3D building form description ─────────────────────────────────────────
    _void_bounds = None
    if shape_classification in ("courtyard",) and fp_holes:
        _all_hx = [p[0] for h in fp_holes for p in h]
        _all_hy = [p[1] for h in fp_holes for p in h]
        if _all_hx:
            _void_bounds = [int(min(_all_hx)), int(min(_all_hy)), int(max(_all_hx)), int(max(_all_hy))]

    if _has_vols:
        _form_type = "volumes_composite"
        _vols = manifest.get("volumes", [])
        _vdesc_parts = []
        for _v in _vols:
            _vdesc_parts.append("Volume at ({},{}) {:.0f}×{:.0f}mm levels {}-{}".format(
                _v.get("offset_x", 0), _v.get("offset_y", 0),
                safe_num(_v.get("width", 0), 0), safe_num(_v.get("length", 0), 0),
                _v.get("start_level", 1), _v.get("end_level", num_storeys)))
        _form_desc = "; ".join(_vdesc_parts) if _vdesc_parts else "composite volume form"
    elif shape_classification in ("courtyard",):
        _form_type = "courtyard_extrusion"
        _vw = _void_bounds[2] - _void_bounds[0] if _void_bounds else 0
        _vl = _void_bounds[3] - _void_bounds[1] if _void_bounds else 0
        _form_desc = ("Courtyard plan extruded {0} floors. Outer plate: {1:.0f}×{2:.0f} mm. "
                      "Inner void approx: {3:.0f}×{4:.0f} mm centred at [{5:.0f},{6:.0f}].").format(
            num_storeys, _w_base, _l_base, _vw, _vl,
            (_void_bounds[0] + _void_bounds[2]) / 2 if _void_bounds else 0,
            (_void_bounds[1] + _void_bounds[3]) / 2 if _void_bounds else 0)
    elif shape_classification in ("L-shape", "U-shape or T-shape", "H-shape or complex") and fp_pts:
        _form_type = "irregular_polygon_extrusion"
        _xs = [p[0] for p in fp_pts]; _ys = [p[1] for p in fp_pts]
        _bb_w = max(_xs) - min(_xs); _bb_l = max(_ys) - min(_ys)
        _form_desc = ("{0} plan extruded {1} floors. Bounding box: {2:.0f}×{3:.0f} mm. "
                      "Plate uniform: {4}").format(
            shape_classification, num_storeys, _bb_w, _bb_l, _is_uniform)
    elif shape_classification in ("organic_svg",):
        _form_type = "organic_svg_extrusion"
        _form_desc = "Organic SVG footprint extruded {} floors.".format(num_storeys)
    elif not _is_uniform:
        _form_type = "tapered"
        _form_desc = ("Rectangular plan extruded {0} floors. "
                      "Base: {1}×{2} mm, minimum: {3}×{4} mm. {5}").format(
            num_storeys, _max_w, _max_l, _min_w, _min_l, _var_desc)
    else:
        _form_type = "direct_extrusion"
        _form_desc = "Rectangular plan {0}×{1} mm extruded {2} floors uniformly.".format(
            int(_w_base), int(_l_base), num_storeys)

    building_form_3d = {
        "form_type":         _form_type,
        "description":       _form_desc,
        "void_boundaries_mm": _void_bounds,
    }

    # ── Solid zone analysis for irregular polygons and courtyards ────────────
    solid_zones = []
    if fp_pts and len(fp_pts) >= 3 and shape_classification not in ("rectangular", "circular", "ellipse"):
        from revit_mcp.staircase_logic import _point_in_polygon as _pip_sz
        # Compute centroid of each arm by partitioning the polygon into rectangular-ish regions
        _xs_sz = [p[0] for p in fp_pts]; _ys_sz = [p[1] for p in fp_pts]
        _bb_xmin, _bb_xmax = min(_xs_sz), max(_xs_sz)
        _bb_ymin, _bb_ymax = min(_ys_sz), max(_ys_sz)
        _bb_w_sz = _bb_xmax - _bb_xmin; _bb_l_sz = _bb_ymax - _bb_ymin

        if shape_classification == "courtyard" and _void_bounds:
            # Courtyard: four band zones (N/S/E/W donut)
            _vx1, _vy1, _vx2, _vy2 = _void_bounds
            _zones_def = [
                ("North band", [_bb_xmin, _vy2, _bb_xmax, _bb_ymax]),
                ("South band", [_bb_xmin, _bb_ymin, _bb_xmax, _vy1]),
                ("East band",  [_vx2, _vy1, _bb_xmax, _vy2]),
                ("West band",  [_bb_xmin, _vy1, _vx1, _vy2]),
            ]
            for _zlabel, _zrect in _zones_def:
                _zw = _zrect[2] - _zrect[0]; _zh = _zrect[3] - _zrect[1]
                if _zw > 0 and _zh > 0:
                    solid_zones.append({
                        "label":          _zlabel,
                        "centroid_mm":    [int((_zrect[0] + _zrect[2]) / 2), int((_zrect[1] + _zrect[3]) / 2)],
                        "available_w_mm": int(_zw),
                        "available_d_mm": int(_zh),
                    })
        else:
            # Irregular polygon: detect arms and report each arm's LOCAL narrow dimension.
            # Strategy: fine grid sampling to find each cell's local cross-section width
            # (how wide the polygon is at that point horizontally AND vertically).
            # Arm grouping uses strict adjacency (1.05× step) so L/U/H corners don't
            # merge separate arms into one zone.
            _STEP = max(3000, int(min(_bb_w_sz, _bb_l_sz) / 10))
            _cells = []
            _x = _bb_xmin + _STEP / 2.0
            while _x < _bb_xmax:
                _y = _bb_ymin + _STEP / 2.0
                while _y < _bb_ymax:
                    if _pip_sz(_x, _y, fp_pts):
                        _inside_hole = any(_pip_sz(_x, _y, _h) for _h in fp_holes) if fp_holes else False
                        if not _inside_hole:
                            _cells.append((_x, _y))
                    _y += _STEP
                _x += _STEP

            # Helper: measure polygon width at a given x or y by counting consecutive
            # interior grid cells along that scan line.
            def _local_w_at_y(_cy):
                """Horizontal span of polygon at y=_cy using sampled cells."""
                _xs_hit = [_cx for (_cx, _cy2) in _cells if abs(_cy2 - _cy) <= _STEP * 0.6]
                return (max(_xs_hit) - min(_xs_hit) + _STEP) if len(_xs_hit) >= 2 else _STEP

            def _local_d_at_x(_cx):
                """Vertical span of polygon at x=_cx using sampled cells."""
                _ys_hit = [_cy for (_cx2, _cy) in _cells if abs(_cx2 - _cx) <= _STEP * 0.6]
                return (max(_ys_hit) - min(_ys_hit) + _STEP) if len(_ys_hit) >= 2 else _STEP

            # Group cells into arms using strict adjacency (avoids merging L/U corner arms).
            # Two cells belong to the same arm only if one step apart on the grid.
            _CONNECTIVITY_R = _STEP * 1.05
            _visited = [False] * len(_cells)
            _arms = []
            for _ci in range(len(_cells)):
                if _visited[_ci]:
                    continue
                _arm = [_ci]; _queue = [_ci]; _visited[_ci] = True
                while _queue:
                    _qi = _queue.pop()
                    _qx, _qy = _cells[_qi]
                    for _nj in range(len(_cells)):
                        if not _visited[_nj]:
                            _nx, _ny = _cells[_nj]
                            # Strictly axis-aligned adjacency (N/S/E/W only, no diagonal)
                            _same_col = abs(_nx - _qx) <= _CONNECTIVITY_R and abs(_ny - _qy) <= 0.1
                            _same_row = abs(_ny - _qy) <= _CONNECTIVITY_R and abs(_nx - _qx) <= 0.1
                            if _same_col or _same_row:
                                _visited[_nj] = True; _arm.append(_nj); _queue.append(_nj)
                _arms.append(_arm)

            # For each arm report its centroid plus its LOCAL narrow dimension.
            # The narrow dimension is the minimum cross-section across the arm
            # (not the bounding box, which over-reports for long rectangular arms).
            for _arm_i, _arm_cells in enumerate(_arms):
                if len(_arm_cells) < 2:
                    continue
                _axs = [_cells[_ci][0] for _ci in _arm_cells]
                _ays = [_cells[_ci][1] for _ci in _arm_cells]
                _cx_arm = sum(_axs) / len(_axs)
                _cy_arm = sum(_ays) / len(_ays)
                # Bounding extents of this arm
                _arm_xmin, _arm_xmax = min(_axs), max(_axs)
                _arm_ymin, _arm_ymax = min(_ays), max(_ays)
                _bbox_w = int(_arm_xmax - _arm_xmin + _STEP)
                _bbox_d = int(_arm_ymax - _arm_ymin + _STEP)
                # Local narrow dimension: sample cross-sections at 3 positions along the
                # arm's long axis and take the minimum — this is the actual arm width a
                # core must fit within, not the arm's full extent.
                _is_wider = _bbox_w >= _bbox_d  # arm runs east-west
                if _is_wider:
                    # Arm is horizontal → depth (Y) is the narrow dimension, width (X) is long.
                    # Sample Y-span at left, mid, right thirds of the arm.
                    _sample_xs = [_arm_xmin + (_arm_xmax - _arm_xmin) * f for f in (0.25, 0.5, 0.75)]
                    _d_samples = [_local_d_at_x(_sx) for _sx in _sample_xs]
                    _narrow_d = int(min(_d_samples))
                    _long_w   = _bbox_w
                    _available_w_mm = _long_w
                    _available_d_mm = _narrow_d
                else:
                    # Arm is vertical → width (X) is the narrow dimension, depth (Y) is long.
                    _sample_ys = [_arm_ymin + (_arm_ymax - _arm_ymin) * f for f in (0.25, 0.5, 0.75)]
                    _w_samples = [_local_w_at_y(_sy) for _sy in _sample_ys]
                    _narrow_w = int(min(_w_samples))
                    _long_d   = _bbox_d
                    _available_w_mm = _narrow_w
                    _available_d_mm = _long_d
                solid_zones.append({
                    "label":          "Arm {}".format(_arm_i + 1),
                    "centroid_mm":    [int(_cx_arm), int(_cy_arm)],
                    "available_w_mm": _available_w_mm,
                    "available_d_mm": _available_d_mm,
                })

    # ── Bounding box ─────────────────────────────────────────────────────────
    if fp_pts and len(fp_pts) >= 3:
        _bx = [p[0] for p in fp_pts]; _by = [p[1] for p in fp_pts]
        bounding_box_mm = [int(min(_bx)), int(min(_by)), int(max(_bx)), int(max(_by))]
    else:
        bounding_box_mm = [int(-_w_base / 2), int(-_l_base / 2), int(_w_base / 2), int(_l_base / 2)]

    # ── Courtyard multi-bank guidance ────────────────────────────────────────
    # When band width < bank_depth + 2×cluster_depth, two clusters cannot fit on
    # one bank in any single band.  Tell Gemini to use lifts.banks with one
    # cluster per opposing band instead.
    courtyard_split_guidance = {}
    if shape_classification == "courtyard" and _void_bounds:
        try:
            _vx1, _vy1, _vx2, _vy2 = _void_bounds
            _bb = bounding_box_mm  # [xmin, ymin, xmax, ymax]
            _bank_half_d = int(layout["total_d"] // 2)
            # Min band width needed for bank + 2 clusters on same side of the bank:
            # one cluster depth on each face, plus the bank itself
            _min_2c = 2 * _bank_half_d + 2 * _cluster_d_p
            _south_band_w = _vy1 - _bb[1]      # building_south → void_south
            _north_band_w = _bb[3] - _vy2      # void_north → building_north
            _requires_split = (_south_band_w < _min_2c) or (_north_band_w < _min_2c)
            if _requires_split:
                # Split lifts evenly — neither bank should have fewer than 2 lifts
                _sec_count  = max(2, num_lifts // 2)
                _main_count = max(2, num_lifts - _sec_count)
                # Main bank: 1000mm buffer from void south edge
                _main_cy = _vy1 - _bank_half_d - 1000
                _main_s_outer = _main_cy - _bank_half_d - _cluster_d_p
                _main_south_clr = int(_main_s_outer - _bb[1])
                # Secondary bank: 1000mm buffer from void north edge
                try:
                    _sec_layout = lift_logic.get_total_core_layout(_sec_count, lobby_width=lobby_w)
                    _sec_half_d = int(_sec_layout["total_d"] // 2)
                except Exception:
                    _sec_half_d = 1450
                _sec_cy = _vy2 + _sec_half_d + 1000
                _sec_n_outer = _sec_cy + _sec_half_d + _cluster_d_p
                _sec_north_clr = int(_bb[3] - _sec_n_outer)
                courtyard_split_guidance = {
                    "requires_separate_banks": True,
                    "reason": (
                        "South/North bands are {0}mm wide — too narrow for bank+2 clusters "
                        "({1}mm needed). Use lifts.banks with one cluster per band.".format(
                            int(min(_south_band_w, _north_band_w)), int(_min_2c))
                    ),
                    "main_bank": {
                        "count":        _main_count,
                        "position_y_mm": int(_main_cy),
                        "orientation":  "EW",
                        "cluster_side": "south",
                        "cluster_outer_y_mm": int(_main_s_outer),
                        "south_wall_clearance_mm": max(0, _main_south_clr),
                    },
                    "secondary_bank": {
                        "count":        _sec_count,
                        "position_y_mm": int(_sec_cy),
                        "orientation":  "EW",
                        "cluster_side": "north",
                        "cluster_outer_y_mm": int(_sec_n_outer),
                        "north_wall_clearance_mm": max(0, _sec_north_clr),
                    },
                }
        except Exception:
            pass

    # OR-Tools pre-computation removed: it was solving around a dummy anchor at
    # origin and the result was discarded after formatting into the prompt text.
    # The fire_cluster_elements block already provides all module sizes via pure
    # arithmetic. OR-Tools runs once at actual build time in generate_fire_safety_manifest.
    _core_ortools_envelope = None

    return {
        "user_goal":                user_goal,
        "shape_classification":     shape_classification,
        "floor_plate_area_mm2":     int(_floor_area_mm2),
        "footprint_polygon_mm":     fp_pts if _has_pts else None,
        "footprint_holes_mm":       fp_holes if fp_holes else [],
        "bounding_box_mm":          bounding_box_mm,
        "floor_plate_variation":    floor_plate_variation,
        "building_form_3d":         building_form_3d,
        "passenger_lift_elements":  passenger_lift_elements,
        "fire_cluster_elements":    fire_cluster_elements,
        "staircase_count_required":   staircase_count_required,
        "minimum_clearance_mm":       6000,
        "solid_zones":                solid_zones,
        "courtyard_split_guidance":   courtyard_split_guidance,
        "core_ortools_envelope":      _core_ortools_envelope,
    }


def _volume_footprint_pts(vol):
    """Return the footprint polygon [[x,y], ...] (mm) for a volume entry.
    Uses explicit footprint_points if provided, otherwise builds a rectangle
    from width/length centred on (offset_x, offset_y), then rotates by rotation_deg.
    """
    ox = safe_num(vol.get("offset_x", 0), 0)
    oy = safe_num(vol.get("offset_y", 0), 0)
    rot_rad = math.radians(safe_num(vol.get("rotation_deg", 0), 0))

    if vol.get("footprint_points"):
        pts = [list(p) for p in vol["footprint_points"]]
    else:
        w = safe_num(vol.get("width", 20000), 20000) / 2.0
        l = safe_num(vol.get("length", 20000), 20000) / 2.0
        # CCW rectangle centred on origin before offset
        pts = [[-w, -l], [w, -l], [w, l], [-w, l]]

    # Rotate about origin, then shift to offset position
    if rot_rad != 0.0:
        pts = _rotate_pts(pts, rot_rad)
    # Apply offset
    pts = [[p[0] + ox, p[1] + oy] + ([p[2]] if len(p) > 2 else []) for p in pts]
    return pts


def _get_interpolated_offset(offset_overrides, level_1based):
    """Interpolate per-level XY offset from sparse control points.

    offset_overrides format: {"1": [0,0], "10": [3000, -1500], "20": [0, 2000]}
    Values are [offset_x_mm, offset_y_mm]. Linearly interpolated between keys.
    Returns (offset_x_mm, offset_y_mm).
    """
    if not offset_overrides:
        return 0.0, 0.0
    pts = {}
    for k, v in offset_overrides.items():
        try:
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                pts[int(k)] = (float(v[0]), float(v[1]))
        except (ValueError, TypeError):
            pass
    if not pts:
        return 0.0, 0.0
    sorted_keys = sorted(pts.keys())
    if level_1based <= sorted_keys[0]:
        return pts[sorted_keys[0]]
    if level_1based >= sorted_keys[-1]:
        return pts[sorted_keys[-1]]
    lo = max(k for k in sorted_keys if k <= level_1based)
    hi = min(k for k in sorted_keys if k > level_1based)
    t = (level_1based - lo) / float(hi - lo)
    lox, loy = pts[lo]
    hix, hiy = pts[hi]
    return lox + t * (hix - lox), loy + t * (hiy - loy)


def _interp_floor_override_offset(floor_overrides, level_1based):
    """Linearly interpolate offset_x / offset_y from sparse floor_overrides keys.

    floor_overrides format: {"1": {"offset_x": 0, "offset_y": 0}, "30": {"offset_x": 15000, "offset_y": 0}}
    Levels without an explicit key are interpolated between their neighbours.
    Levels before the first key clamp to the first key; levels after the last key
    clamp to the last key.  Returns (offset_x_mm, offset_y_mm).
    """
    if not floor_overrides:
        return 0.0, 0.0
    pts = {}
    for k, v in floor_overrides.items():
        if not isinstance(v, dict):
            continue
        ox = safe_num(v.get("offset_x", None), None)
        oy = safe_num(v.get("offset_y", None), None)
        if ox is not None or oy is not None:
            try:
                pts[int(k)] = (float(ox or 0.0), float(oy or 0.0))
            except (ValueError, TypeError):
                pass
    if not pts:
        return 0.0, 0.0
    sorted_keys = sorted(pts.keys())
    if level_1based <= sorted_keys[0]:
        return pts[sorted_keys[0]]
    if level_1based >= sorted_keys[-1]:
        return pts[sorted_keys[-1]]
    lo = max(k for k in sorted_keys if k <= level_1based)
    hi = min(k for k in sorted_keys if k > level_1based)
    if lo == hi:
        return pts[lo]
    t = (level_1based - lo) / float(hi - lo)
    lox, loy = pts[lo]
    hix, hiy = pts[hi]
    return lox + t * (hix - lox), loy + t * (hiy - loy)


class RevitWorkers:
    def __init__(self, doc, tracker=None):
        from .spatial_registry import SpatialRegistry
        self.doc = doc
        self.tracker = tracker
        self.spatial_registry = SpatialRegistry()

    def log(self, message):
        client.log(message)

    @staticmethod
    def _phase_msg(label, new_count, reused_count):
        """Build a progress message that says 'Created X ...' or 'Updated X ...' depending on the operation."""
        parts = []
        if new_count > 0:
            parts.append(f"Created {new_count}")
        if reused_count > 0:
            parts.append(f"Updated {reused_count}")
        if not parts:
            return f"No changes to {label}."
        return f"{' + '.join(parts)} {label}."

    def execute_fast_manifest(self, manifest):
        """High-speed execution using intent-based logic and state-aware updates"""
        from revit_mcp.cancel_manager import check_cancelled
        self.log("--- execute_fast_manifest START ---")
        import Autodesk.Revit.DB as DB # type: ignore
        from revit_mcp.building_generator import get_model_registry # type: ignore
        doc = self.doc
        results = {"levels": [], "elements": [], "summary": {}}

        # --- SVG FOOTPRINT EXPANSION ---
        # Convert footprint_svg → footprint_points before any phase touches the manifest.
        # This is the real execution path; building_generator._expand_high_level_manifest
        # is the legacy path and is not called from here.
        #
        # IMPORTANT: if footprint_points already exist (Gemini supplied them directly, which
        # is the case for courtyard buildings using footprint_points + footprint_holes), skip
        # SVG conversion entirely.  Running SVG conversion would re-centre the polygon and
        # inject a non-zero _svg_offset, causing lifts.position to be shifted by that offset
        # even though the coordinates are already in the correct absolute space.
        # footprint_points always wins; footprint_svg is stripped so nothing downstream
        # accidentally re-triggers conversion.
        _shell = manifest.get("shell", {})
        if _shell.get("footprint_points") and _shell.get("footprint_svg"):
            # footprint_points is authoritative — drop the redundant SVG and normalise holes
            _shell = dict(_shell)
            _shell.pop("footprint_svg", None)
            if _shell.get("footprint_holes"):
                _shell["footprint_holes"] = _normalise_footprint_holes(_shell["footprint_holes"])
            manifest = dict(manifest)
            manifest["shell"] = _shell
            self.log("[SVG] footprint_points already present — footprint_svg stripped (no re-centring)")
        # Normalise footprint_points to a flat [[x,y],...] list.
        # Gemini sometimes sends [[outer_polygon], [hole_polygon]] or [[outer_polygon]]
        # (a list-of-polygons) when the footprint has both outer and hole subpaths.
        # Extract only the outer ring here; holes belong in footprint_holes.
        _fp_raw = manifest.get("shell", {}).get("footprint_points")
        if _fp_raw and _fp_raw[0] and isinstance(_fp_raw[0][0], (list, tuple)):
            _shell2 = dict(manifest.get("shell", {}))
            _outer = _fp_raw[0]
            _inner = _fp_raw[1:]
            _shell2["footprint_points"] = _outer
            if _inner and not _shell2.get("footprint_holes"):
                _shell2["footprint_holes"] = _normalise_footprint_holes(_inner)
            manifest = dict(manifest)
            manifest["shell"] = _shell2
            self.log("[Footprint] Normalised nested footprint_points: {} outer pts, {} hole(s)".format(
                len(_outer), len(_inner)))
        _svg_path = _shell.get("footprint_svg")
        if _svg_path:
            try:
                from revit_mcp.svg_to_footprint import svg_path_to_multiloop
                _ml = svg_path_to_multiloop(_svg_path)
                _svg_off = _ml.get("offset", [0.0, 0.0])
                _svg_dx, _svg_dy = float(_svg_off[0]), float(_svg_off[1])
                _shell = dict(_shell)
                _shell["footprint_points"] = _ml["outer"]
                # Store offset so lifts.position can be adjusted below
                _shell["_svg_offset"] = [_svg_dx, _svg_dy]
                if _ml.get("holes"):
                    # SVG contained inner subpaths — use them (already re-centred)
                    _shell["footprint_holes"] = _ml["holes"]
                    self.log("[SVG] footprint_svg converted: {} outer vertices, {} hole(s)".format(
                        len(_ml["outer"]), len(_ml["holes"])))
                elif _shell.get("footprint_holes"):
                    # JSON footprint_holes — normalise to list-of-polygons, then shift if needed
                    _raw_holes = _normalise_footprint_holes(_shell["footprint_holes"])
                    if _svg_dx != 0.0 or _svg_dy != 0.0:
                        _raw_holes = [
                            [[p[0] - _svg_dx, p[1] - _svg_dy] for p in hole]
                            for hole in _raw_holes
                        ]
                    _shell["footprint_holes"] = _raw_holes
                    self.log("[SVG] footprint_svg converted: {} vertices, JSON holes shifted by ({:.0f},{:.0f})".format(
                        len(_ml["outer"]), _svg_dx, _svg_dy))
                else:
                    self.log("[SVG] footprint_svg converted: {} vertices".format(len(_ml["outer"])))
                _shell.pop("footprint_svg", None)
                manifest = dict(manifest)
                manifest["shell"] = _shell
            except Exception as _svg_err:
                self.log("[SVG] footprint_svg conversion FAILED: {}".format(_svg_err))
                try: tg.RollBack()
                except: pass
                return {
                    "status": "CONFLICT",
                    "description": (
                        "footprint_svg parsing failed: {}. "
                        "The provided SVG path could not be converted to a valid floor plate polygon. "
                        "Common causes: self-intersecting path, missing Z close command, or coordinates "
                        "too far from origin. Fix the path or use footprint_points with straight-line "
                        "vertices instead. Original path (first 300 chars): {}"
                    ).format(str(_svg_err), str(_svg_path)[:300])
                }

        # 1. State Scan
        registry = get_model_registry(doc)
        self._registry_cache = registry  # Cache so sub-methods can access for lift count detection

        # Extract typology and compliance_params from manifest for downstream use
        _typology = manifest.get("typology", "")
        self._manifest_typology = _typology  # store so sub-methods can access
        self._compliance_params = manifest.get("compliance_parameters", {})
        self._current_manifest = manifest  # store for volume/asymmetry sub-methods
        
        # Track counts of elements handled/created/deleted
        reused = [0]
        created = [0]
        deleted = [0]
        
        # 1. NUCLEAR LOCKDOWN: Forcefully disjoint all walls before any build moves
        from revit_mcp.utils import nuclear_lockdown
        nuclear_lockdown(doc)
        
        tg = DB.TransactionGroup(doc, "AI Build: Fast Manifest")
        tg.Start()
        
        from revit_mcp.utils import setup_failure_handling
        
        # Track elements that actually changed for efficient re-join
        affected_elements = []
        
        try:
            # --- PHASE 1: LEVELS ---
            if self.tracker: self.tracker.set_status("Setting up building elevations...")
            t = DB.Transaction(doc, "AI Build: Levels")
            t.Start()
            setup_failure_handling(t, use_nuclear=True)
            c0, r0 = created[0], reused[0]
            elevations, current_levels = self._process_levels(manifest, registry, created, reused)
            lvl_new, lvl_reused = created[0] - c0, reused[0] - r0
            t.Commit()
            t.Dispose()

            if self.tracker:
                self.tracker.record_created("levels", len(current_levels))
                # Build detailed height summary
                heights_mm = []
                for ei in range(len(elevations) - 1):
                    heights_mm.append(round((elevations[ei+1] - elevations[ei]) * 304.8))
                height_desc = ""
                if heights_mm:
                    unique_h = sorted(set(heights_mm))
                    if len(unique_h) == 1:
                        height_desc = f", all at {unique_h[0]}mm"
                    else:
                        height_desc = f", heights: {min(heights_mm)}-{max(heights_mm)}mm"
                self.tracker.set_status(
                    f"{self._phase_msg('levels', lvl_new, lvl_reused)} "
                    f"({len(current_levels)} total, top elevation {elevations[-1]*304.8:.0f}mm{height_desc})"
                )
            results["levels"] = [str(l.Id.Value) for l in current_levels]
            # Expand shape shorthand FIRST so _process_shell_dimensions sees footprint_points
            shell = manifest.get("shell", {})
            shell = _expand_shape_shorthand(shell)
            # Normalise floor_overrides: Gemini occasionally sends the inverted schema
            # {"width": {"10": 28333}, "length": {"10": 28333}} instead of the correct
            # {"10": {"width": 28333, "length": 28333}}.  Detect and transpose.
            _raw_fo = shell.get("floor_overrides")
            if isinstance(_raw_fo, dict) and _raw_fo:
                _fo_vals = list(_raw_fo.values())
                if all(isinstance(v, dict) for v in _fo_vals):
                    # Correct format: {"10": {"width": ..., "length": ...}} — keys are floor indices
                    pass
                elif all(isinstance(v, (int, float)) for v in _fo_vals):
                    # Wrong format: {"width": 28333, "length": 28333} — scalar values, ignore
                    pass
                else:
                    # Check for inverted format: {"width": {"10": N, "20": N}, "length": {"10": N, "20": N}}
                    # Keys would be dimension names ("width", "length") not floor indices
                    _dim_keys = {"width", "length", "parapet_height", "cantilever_depth", "offset_x", "offset_y"}
                    if any(k in _dim_keys for k in _raw_fo):
                        _transposed = {}
                        for _dim_name, _floor_map in _raw_fo.items():
                            if isinstance(_floor_map, dict):
                                for _floor_k, _dim_v in _floor_map.items():
                                    if _floor_k not in _transposed:
                                        _transposed[_floor_k] = {}
                                    _transposed[_floor_k][_dim_name] = _dim_v
                        if _transposed:
                            shell = dict(shell)
                            shell["floor_overrides"] = _transposed
                            manifest["shell"] = shell
                            self.log("[Manifest] Normalised floor_overrides from inverted schema: {}".format(
                                list(_transposed.keys())))
            manifest["shell"] = shell  # write back so _process_shell_dimensions sees footprint_points
            # Fix 6: organic builds always define the full geometry — never inherit stale wall lengths
            if shell.get("footprint_points") and not shell.get("force_global_dimensions"):
                shell = dict(shell)
                shell["force_global_dimensions"] = True
                manifest["shell"] = shell
                self.log("[Manifest] Auto-set force_global_dimensions=true (footprint_points present)")
            floor_dims, shell = self._process_shell_dimensions(manifest, current_levels, registry)

            check_cancelled("after levels phase")
            # --- PHASE 2: VERTICAL CIRCULATION (Unified Core: Lifts + Staircases) ---
            # Pre-calculate core layout BEFORE opening a transaction — this is pure Python
            # (no Revit document writes). Keeping heavy calc inside a transaction was wasting
            # lock time and contributing to the overall build latency.
            import importlib
            importlib.reload(staircase_logic)
            importlib.reload(fire_safety_logic)
            self.log("Step 3a: Pre-calculating Unified Vertical Circulation layout...")
            if self.tracker: self.tracker.set_status("Calculating unified core layout (Lifts + Fire Safety)...")

            core_bounds_ft = None
            if "core_bounds" in manifest and manifest["core_bounds"]:
                self.log("Consolidation: Using pre-planned core bounds from manifest.")
                from revit_mcp.utils import mm_to_ft
                cb_list = manifest["core_bounds"]
                bx1 = min(cb[0] for cb in cb_list); by1 = min(cb[1] for cb in cb_list)
                bx2 = max(cb[2] for cb in cb_list); by2 = max(cb[3] for cb in cb_list)
                core_bounds_ft = (mm_to_ft(bx1), mm_to_ft(by1), mm_to_ft(bx2), mm_to_ft(by2))
                voids_mm = manifest.get("voids", [])
                u_voids_ft = []
                for (vx1, vy1, vx2, vy2) in voids_mm:
                    u_voids_ft.append((mm_to_ft(vx1), mm_to_ft(vy1), mm_to_ft(vx2), mm_to_ft(vy2)))
                self._stair_voids = u_voids_ft
                # void_polygons: rotated rectangle corner lists (mm) produced by
                # _rotate_geometry when rotation_deg is non-zero.  Convert to ft
                # for _process_floors to cut precisely-shaped slab openings.
                void_polys_mm = manifest.get("void_polygons", [])
                if void_polys_mm:
                    self._stair_void_polygons = [
                        [(mm_to_ft(p[0]), mm_to_ft(p[1])) for p in poly]
                        for poly in void_polys_mm
                    ]
                else:
                    self._stair_void_polygons = []
                core_bounds = core_bounds_ft
            else:
                self._stair_voids = []
                core_bounds = self._expand_unified_vertical_circulation(manifest, current_levels, elevations, floor_dims, affected_elements, results)

            # Catch Spatial Conflicts from the pre-calculation
            if isinstance(core_bounds, dict) and core_bounds.get("status") == "CONFLICT":
                try: tg.RollBack()
                except: pass
                return core_bounds

            # Now open the transaction only for the actual enforce-disjoint write
            t = DB.Transaction(doc, "AI Build: Vertical Circulation")
            t.Start()
            setup_failure_handling(t, use_nuclear=True)
            c0, r0 = created[0], reused[0]
            core_new, core_reused = created[0] - c0, reused[0] - r0
            self._enforce_disjoint(affected_elements)
            t.Commit()
            t.Dispose()
            t = None

            # --- PHASE 2.5: PRE-EMPTIVE CLEANUP ---
            # Delete old AI staircases BEFORE shell build to prevent Revit native "auto-adjust" ghosting.
            # Must be outside ANY transaction because it starts its own.
            self._stair_fps_cache = self._cleanup_staircases(doc, getattr(self, '_stair_run_data', []), manifest)

            if self.tracker and (core_new or core_reused):
                self.tracker.set_status(
                    f"{self._phase_msg('core elements', core_new, core_reused)} "
                    f"(Unified Lifts, Stairs, and Fire Safety lobbies synced to core bounds)"
                )

            # --- PHASE 2.9: AGGRESSIVE PRE-CLEANUP ---
            # Collect ALL old AI element IDs first, then bulk-delete in one call.
            # The previous per-element doc.Delete() in a loop was slow; bulk deletion
            # via a single ICollection[ElementId] call is significantly faster.
            t_clean = DB.Transaction(doc, "AI Build: Pre-Cleanup")
            t_clean.Start()
            setup_failure_handling(t_clean, use_nuclear=True)

            from System.Collections.Generic import List as CsList  # type: ignore
            ids_to_delete = CsList[DB.ElementId]()
            cats = [DB.BuiltInCategory.OST_Walls, DB.BuiltInCategory.OST_Floors, DB.BuiltInCategory.OST_Columns, DB.BuiltInCategory.OST_StructuralColumns]
            for c in cats:
                for el in DB.FilteredElementCollector(doc).OfCategory(c).WhereElementIsNotElementType().ToElements():
                    try:
                        p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                        if p and p.HasValue:
                            _cm = p.AsString()
                            if _cm.startswith("AI_") or (_cm.startswith("B") and "_AI_" in _cm):
                                ids_to_delete.Add(el.Id)
                    except:
                        pass

            pre_del_count = ids_to_delete.Count
            if pre_del_count > 0:
                doc.Delete(ids_to_delete)

            t_clean.Commit()
            t_clean.Dispose()
            if pre_del_count > 0:
                self.log("Atomic Pre-Cleanup: bulk-deleted {} legacy AI elements for clean build.".format(pre_del_count))

            check_cancelled("after vertical circulation phase")
            # --- PHASE 3: WALLS & FLOORS ---
            t = DB.Transaction(doc, "AI Build: Shell")
            t.Start()
            setup_failure_handling(t, use_nuclear=True)
            self.log("Step 3b: Processing Shell Walls and Floors...")
            if self.tracker: self.tracker.set_status("Processing shell walls and floors...")
            # footprint_points in shell = organic mode; shell handles it natively
            # suppress_shell only needed when AI uses granular walls[] with arcs AND no footprint_points
            has_footprint = bool(shell.get("footprint_points"))
            suppress_shell = (not has_footprint) and (
                manifest.get("suppress_shell", False) or any(
                    "mid_x" in w or "mid_y" in w or "mid" in w
                    for w in manifest.get("walls", [])
                )
            )
            c0, r0 = created[0], reused[0]
            if not suppress_shell:
                self._process_walls(current_levels, elevations, floor_dims, shell, registry, results, created, reused, affected_elements)
            walls_new, walls_reused = created[0] - c0, reused[0] - r0
            c0, r0 = created[0], reused[0]
            res_f = self._process_floors(current_levels, floor_dims, shell, registry, results, created, reused, affected_elements, core_bounds_ft=core_bounds)
            if isinstance(res_f, dict) and res_f.get("status") == "CONFLICT":
                try: t.RollBack(); t.Dispose()
                except: pass
                try: tg.RollBack()
                except: pass
                return res_f
            expanded_slab_dims = res_f
            floors_new, floors_reused = created[0] - c0, reused[0] - r0
            c0, r0 = created[0], reused[0]
            self._process_parapets(current_levels, expanded_slab_dims, floor_dims, shell, registry, results, created, reused, affected_elements)
            walls_new += created[0] - c0
            walls_reused += reused[0] - r0
            if self.tracker:
                self.tracker.record_created("walls", walls_new)
                self.tracker.record_created("floors", floors_new)
                # Compute footprint range for detail
                min_w = min(d[0] for d in floor_dims)
                max_w_d = max(d[0] for d in floor_dims)
                min_l = min(d[1] for d in floor_dims)
                max_l_d = max(d[1] for d in floor_dims)
                if min_w == max_w_d and min_l == max_l_d:
                    fp_desc = f"{max_w_d/1000:.0f}m x {max_l_d/1000:.0f}m"
                else:
                    fp_desc = f"{min_w/1000:.0f}-{max_w_d/1000:.0f}m x {min_l/1000:.0f}-{max_l_d/1000:.0f}m"
                cant = shell.get("cantilever_depth", 0)
                cant_desc = f", cantilever {cant}mm" if cant else ""
                self.tracker.set_status(
                    f"{self._phase_msg('perimeter walls', walls_new, walls_reused)} "
                    f"(4 per level, footprint {fp_desc})"
                )
                self.tracker.set_status(
                    f"{self._phase_msg('floor slabs', floors_new, floors_reused)} "
                    f"({len(current_levels)} levels{cant_desc})"
                )
            self._enforce_disjoint(affected_elements)
            t.Commit()
            t.Dispose()

            # --- PHASE 4: STRUCTURE ---
            # Synthesis Run (enforce 1/3 rule) before processing columns
            presets = load_presets()
            _t = getattr(self, "_manifest_typology", "")
            preset = presets.get(_t) or presets.get("default") or presets.get("commercial_office", {})
            max_w = max(d[0] for d in floor_dims)
            max_l = max(d[1] for d in floor_dims)
            
            p_col_logic = preset.get("column_logic", {})
            dna_span = p_col_logic.get("span", [12000, 15000])
            dna_offset = safe_num(p_col_logic.get("offset_from_edge", 1000), 1000)
            
            res_w = self._synthesize_structural_grid(max_w, dna_span, dna_offset)
            if isinstance(res_w, dict) and res_w.get("status") == "CONFLICT":
                try: tg.RollBack()
                except: pass
                return res_w
            _, synth_span_w = res_w

            res_l = self._synthesize_structural_grid(max_l, dna_span, dna_offset)
            if isinstance(res_l, dict) and res_l.get("status") == "CONFLICT":
                try: tg.RollBack()
                except: pass
                return res_l
            _, synth_span_l = res_l
            
            # Store synthesized spans for _process_columns_and_grids
            self._synth_span_w = synth_span_w
            self._synth_span_l = synth_span_l

            check_cancelled("after shell phase")
            t = DB.Transaction(doc, "AI Build: Structure")
            t.Start()
            setup_failure_handling(t, use_nuclear=True)
            self.log("Step 3c: Processing Structure with Core Alignment...")
            if self.tracker: self.tracker.set_status("Processing structural columns...")
            c0, r0 = created[0], reused[0]
            self._process_columns_and_grids(current_levels, elevations, floor_dims, expanded_slab_dims, shell, registry, results, created, reused, affected_elements, core_bounds)
            cols_new, cols_reused = created[0] - c0, reused[0] - r0
            if self.tracker:
                self.tracker.record_created("columns", cols_new)
                col_spacing = shell.get("column_spacing", 10000)
                offset_edge = shell.get("column_offset_from_edge", 500)
                # Normalise: AI may send a dict {"default":N}, {"value":N}, or {"x":N,"y":N}
                if isinstance(col_spacing, dict):
                    col_spacing = (col_spacing.get("default")
                                   or col_spacing.get("value")
                                   or col_spacing.get("x")
                                   or col_spacing.get("y")
                                   or 10000)
                elif not isinstance(col_spacing, (int, float)):
                    col_spacing = 10000
                # col_spacing may be a list [x_spacing, y_spacing] from the AI
                if isinstance(col_spacing, list):
                    spacing_str = "x".join(f"{v/1000:.0f}" for v in col_spacing) + "m"
                else:
                    spacing_str = f"{col_spacing/1000:.0f}m"
                self.tracker.set_status(
                    f"{self._phase_msg('structural columns', cols_new, cols_reused)} "
                    f"(grid spacing {spacing_str}, {offset_edge}mm from edge)"
                )
            t.Commit()
            t.Dispose()

            check_cancelled("after structure phase")
            # --- PHASE 5: GRANULAR ---
            t = DB.Transaction(doc, "AI Build: Granular")
            t.Start()
            setup_failure_handling(t, use_nuclear=True)
            self.log("Step 4: Processing Granular Element Overrides...")
            # Count granular walls from manifest (lift core + staircase + user-defined)
            gran_wall_count = len(manifest.get("walls", []))
            gran_floor_count = len(manifest.get("floors", []))
            gran_col_count = len(manifest.get("granular_columns", []))
            if self.tracker and (gran_wall_count + gran_floor_count + gran_col_count) > 0:
                parts = []
                if gran_wall_count: parts.append(f"{gran_wall_count} walls (lift cores, staircases, dividers)")
                if gran_floor_count: parts.append(f"{gran_floor_count} floors (landings, custom slabs)")
                if gran_col_count: parts.append(f"{gran_col_count} columns")
                self.tracker.set_status(f"Processing granular elements: {', '.join(parts)}...")
            c0, r0 = created[0], reused[0]
            self._process_granular_walls(manifest, current_levels, registry, results, created, reused, affected_elements)
            gw_new, gw_reused = created[0] - c0, reused[0] - r0
            c0, r0 = created[0], reused[0]
            res_gf = self._process_granular_floors(manifest, current_levels, registry, results, created, reused, affected_elements)
            if isinstance(res_gf, dict) and res_gf.get("status") == "CONFLICT":
                try: t.RollBack(); t.Dispose()
                except: pass
                try: tg.RollBack()
                except: pass
                return res_gf
            gf_new, gf_reused = created[0] - c0, reused[0] - r0
            c0, r0 = created[0], reused[0]
            self._process_granular_columns(manifest, current_levels, registry, results, created, reused)
            gc_new, gc_reused = created[0] - c0, reused[0] - r0
            if self.tracker:
                parts = []
                if gw_new or gw_reused: parts.append(self._phase_msg("walls", gw_new, gw_reused))
                if gf_new or gf_reused: parts.append(self._phase_msg("floors", gf_new, gf_reused))
                if gc_new or gc_reused: parts.append(self._phase_msg("columns", gc_new, gc_reused))
                if parts:
                    self.tracker.set_status(f"Granular overrides: {' | '.join(parts)}")
            self._enforce_disjoint(affected_elements)
            t.Commit()
            t.Dispose()

            # --- PHASE 5.5: STAIR RUNS (requires own StairsEditScope) ---
            # Wrapped in try/except so stair failures NEVER nuke the building.
            ss = getattr(self, '_stair_summary', None)
            if self.tracker:
                if ss:
                    self.tracker.set_status(
                        f"Creating stair runs ({ss['count']} cores, "
                        f"{ss['flights_typical']} flights/floor, "
                        f"{ss['risers_per_flight']} risers/flight at {ss['riser']}mm rise, "
                        f"{ss['tread']}mm tread)..."
                    )
                else:
                    self.tracker.set_status("Processing stair runs...")
            try:
                self.log("Step 4a: Creating Stair Runs...")
                stair_runs_before = self.tracker.elements_created["stair_runs"] if self.tracker else 0
                self._create_stair_runs(current_levels, results)
                if self.tracker:
                    stair_runs_added = self.tracker.elements_created["stair_runs"] - stair_runs_before
                    if stair_runs_added > 0:
                        self.tracker.set_status(f"Created {stair_runs_added} stair runs across {len(current_levels)-1} storeys.")
            except Exception as stair_err:
                self.log("Stair phase failed (non-fatal): {}".format(stair_err))

            # --- PHASE 5.6: DOORS ---
            if self.tracker: self.tracker.set_status("Placing doors in staircases, lobbies and lift shafts...")
            try:
                t = DB.Transaction(doc, "AI Build: Doors")
                t.Start()
                from revit_mcp.utils import setup_failure_handling
                setup_failure_handling(t, use_nuclear=False)
                doors_placed = self._process_doors(current_levels, results)
                t.Commit()
                t.Dispose()
                if self.tracker and doors_placed:
                    self.tracker.set_status(f"Placed {doors_placed} doors (staircases, fire lobbies, lift shafts).")
            except Exception as door_err:
                import traceback
                self.log("Door phase failed (non-fatal): {}\n{}".format(door_err, traceback.format_exc()))
                try: t.RollBack()
                except: pass

            # --- PHASE 6: CLEANUP & DOC ---
            if self.tracker: self.tracker.set_status("Finalizing and cleaning up...")
            t = DB.Transaction(doc, "AI Build: Cleanup")
            t.Start()
            setup_failure_handling(t, use_nuclear=True)
            self._cleanup_registry(registry, results, deleted)
            self._generate_documentation(current_levels, elevations, floor_dims, max_w, max_l)
            t.Commit()
            t.Dispose()
                
            # Store for potential future cleanup
            self._affected_elements = [el for el in affected_elements if el and el.IsValidObject]
            
            tg.Assimilate()
            
            results["summary"] = {"reused": reused[0], "created": created[0], "deleted": deleted[0]}
            self.log("Fast-Track Summary: {}".format(results["summary"]))

            # Persist shell parametric state so the next prompt can reference the
            # current shape/footprint_scale_overrides without reverse-engineering geometry.
            try:
                import json as _json, os as _os
                from revit_mcp.utils import get_log_path
                _shell_to_save = {k: v for k, v in manifest.get("shell", {}).items()}
                # footprint_points is always auto-derived (from footprint_svg, shape, or
                # rectangular expansion) — never persist it; it is a raw coordinate dump
                # that confuses Gemini when injected back as EXISTING SHELL PARAMETERS.
                # footprint_svg is the compact, human-readable source of truth — keep it.
                _shell_to_save.pop("footprint_points", None)
                # When volumes are used, do NOT persist organic shell overrides.
                # Carrying scale / offset / rotation / svg from a previous organic build
                # into a volumes build causes the old curved shape to bleed back.
                if manifest.get("volumes"):
                    for _organic_key in ("footprint_svg", "footprint_scale_overrides",
                                         "footprint_offset_overrides", "footprint_rotation_overrides",
                                         "shape"):
                        _shell_to_save.pop(_organic_key, None)
                from revit_mcp.utils import get_appdata_path
                _shell_path = _os.path.join(get_appdata_path("cache"), "last_shell_state.json")
                with open(_shell_path, "w") as _f:
                    _json.dump(_shell_to_save, _f)
                self.log("Shell state saved to last_shell_state.json")
            except Exception as _se:
                self.log("Shell state save warning: {}".format(_se))

            return results
            
        except Exception as e:
            import traceback
            self.log("CRITICAL ERROR in execute_fast_manifest: {}\n{}".format(str(e), traceback.format_exc()))
            # Transaction is disposed on Error or Commit, so we only RollBack the TG if fails.
            tg.RollBack()
            return {"error": str(e)}

    def _batch_disable_existing_joins(self, doc, registry):
        """High-speed pass to ensure ALL walls in the document are disjoint."""
        import Autodesk.Revit.DB as DB # type: ignore
        from revit_mcp.utils import disallow_joins, setup_failure_handling
        
        # Scan ALL walls, not just AI walls, to prevent AI cells joining to user walls.
        all_walls = DB.FilteredElementCollector(doc).OfClass(DB.Wall).WhereElementIsNotElementType().ToElements()
        
        if len(all_walls) == 0: return
        
        t = DB.Transaction(doc, "AI Build: Initial Join Guard")
        t.Start()
        setup_failure_handling(t)
        count = 0
        for wall in all_walls:
            if wall and isinstance(wall, DB.Wall):
                disallow_joins(wall)
                count += 1
        t.Commit()
        t.Dispose()
        self.log("Initial Join Guard: Forcefully disjointed {} total walls in project.".format(count))

    def _enforce_disjoint(self, affected_elements):
        """Final safety pass to ensure no implicit joins were triggered."""
        from revit_mcp.utils import disallow_joins
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        for el in affected_elements:
            if el and el.IsValidObject:
                # 1. Wall-Wall Join suppression (Ends)
                if isinstance(el, DB.Wall):
                    disallow_joins(el)
                
                # 2. Force Unjoin from everything (Floors, Columns, etc.)
                try:
                    joined_ids = DB.JoinGeometryUtils.GetJoinedElements(doc, el)
                    for j_id in joined_ids:
                        target = doc.GetElement(j_id)
                        if target and DB.JoinGeometryUtils.AreElementsJoined(doc, el, target):
                            DB.JoinGeometryUtils.UnjoinGeometry(doc, el, target)
                except:
                    pass

    def _synthesize_structural_grid(self, target_dim, span_range, min_offset):
        """
        Synthesises an optimal structural grid span for the given building dimension.

        The 1/3 cantilever rule governs the PERIMETER overhang (facade → first
        column = min_offset), which is always satisfied for typical values.
        It does NOT govern the inner zone between the last bay column and the
        building centre — in a central-core building that zone is the core and
        no structural columns are placed there.

        Returns: (final_dim, final_span) — never returns CONFLICT.
        """
        if isinstance(span_range, (int, float)):
            min_s, max_s = float(span_range), float(span_range)
        elif isinstance(span_range, list) and len(span_range) >= 2:
            min_s, max_s = float(span_range[0]), float(span_range[1])
        else:
            min_s, max_s = 10000.0, 12000.0

        mid_s = (min_s + max_s) / 2.0
        target_half = target_dim / 2.0

        for n in range(1, 20):
            s_lo = target_half / (n + 0.5)
            s_hi = (target_half - min_offset) / n if n > 0 else max_s
            lo = max(min_s, s_lo)
            hi = min(max_s, s_hi)
            if lo <= hi + 1.0:
                return target_dim, hi

        return target_dim, mid_s

    def _process_levels(self, manifest, registry, created, reused):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        setup = manifest.get("project_setup", {})
        
        # Default Calculation
        existing_count = sum(1 for k in registry.keys() if k.startswith("AI_Level_"))
        default_levels = max(1, existing_count - 1)
        
        default_height = 4000
        if "AI_Level_1" in registry and "AI_Level_2" in registry:
            try:
                l1, l2 = doc.GetElement(registry["AI_Level_1"]), doc.GetElement(registry["AI_Level_2"])
                if l1 and l2: default_height = (l2.Elevation - l1.Elevation) * 304.8
            except: pass

        levels_val = setup.get("levels", setup.get("storeys", default_levels))
        height_val = setup.get("level_height", default_height)
        height_overrides = setup.get("height_overrides", {})
        # Normalise: Gemini may send a list of {level, height} dicts instead of {str: int}
        if isinstance(height_overrides, list):
            height_overrides = {
                str(entry["level"]): entry["height"]
                for entry in height_overrides
                if isinstance(entry, dict) and "level" in entry and "height" in entry
            }
        elif not isinstance(height_overrides, dict):
            height_overrides = {}

        # Expand range keys like '2-10' â†' {'2':v, '3':v, ..., '10':v}
        expanded_overrides = {}
        for k, v in height_overrides.items():
            k_str = str(k).strip()
            if '-' in k_str:
                try:
                    parts = k_str.split('-')
                    lo, hi = int(parts[0].strip()), int(parts[1].strip())
                    for n in range(lo, hi + 1):
                        expanded_overrides[str(n)] = v
                except Exception:
                    expanded_overrides[k_str] = v
            else:
                expanded_overrides[k_str] = v
        height_overrides = expanded_overrides
        self.log("[Build] height_overrides after expansion: {}".format(
            {k: v for k, v in list(height_overrides.items())[:10]}))

        if isinstance(levels_val, list):
            elevations = [mm_to_ft(e) for e in levels_val]
        else:
            count = int(safe_num(levels_val, 1))
            elevations = [0.0]
            curr = 0.0
            from revit_mcp import staircase_logic

            # level_height may be a per-floor list [h1, h2, ...] or a scalar.
            # Extract a per-floor list and a representative "typical" scalar for
            # staircase compliance functions that expect a single number.
            if isinstance(height_val, list):
                _lh_list = [safe_num(h, default_height) for h in height_val]
                # Typical = most common value across all floors (use a simple frequency count)
                _freq = {}
                for _hv in _lh_list:
                    _freq[_hv] = _freq.get(_hv, 0) + 1
                _typical_h = max(_freq, key=_freq.get)
            else:
                _lh_list = None
                _typical_h = safe_num(height_val, default_height)

            for i in range(1, count + 1):
                # Priority: explicit height_overrides > per-floor list > scalar
                if str(i) in height_overrides:
                    h_src = height_overrides[str(i)]
                elif _lh_list and i <= len(_lh_list):
                    h_src = _lh_list[i - 1]
                else:
                    h_src = height_val if not isinstance(height_val, list) else _typical_h
                raw_h = get_random_dim(h_src, default_height, variation=0.15) if h_src == "random" else safe_num(h_src, default_height)
                h = staircase_logic.adjust_storey_height(raw_h, _typical_h, is_top_floor=False)
                if self.tracker and abs(h - raw_h) > 1.0:
                    from revit_mcp import staircase_logic as _sc
                    _rpf = _sc._risers_per_flight_typical(_typical_h, 150)
                    _total_risers = _sc._snap_risers(h, 150)
                    _num_flights = _sc._calc_num_flights(h, _typical_h, 150)
                    self.tracker.log_adjustment(
                        f"Level {i}: height adjusted {raw_h:.0f}mm -> {h:.0f}mm "
                        f"to ensure even flight count ({_num_flights} flights, "
                        f"{_rpf} risers/flight, {_total_risers} total risers at 150mm riser height). "
                        f"Dogleg stairs require even flights so landings align at each floor."
                    )
                curr += mm_to_ft(h)
                elevations.append(curr)
        
        existing_views = {v.GenLevel.Id for v in DB.FilteredElementCollector(doc).OfClass(DB.ViewPlan) if v.ViewType == DB.ViewType.FloorPlan and v.GenLevel}
        
        current_levels = []
        for i, elev in enumerate(elevations):
            tag = "AI_Level_" + str(i+1)
            name = "AI Level " + str(i+1)
            lvl = doc.GetElement(registry[tag]) if tag in registry else next((l for l in DB.FilteredElementCollector(doc).OfClass(DB.Level) if l.Name == name), None)
            
            if lvl:
                if abs(lvl.Elevation - elev) > 0.001: lvl.Elevation = elev
                reused[0] += 1
            else:
                lvl = DB.Level.Create(doc, elev)
                lvl.Name = name
                created[0] += 1
            
            safe_set_comment(lvl, tag)
            current_levels.append(lvl)
            
            if lvl.Id not in existing_views:
                vt = next((vft for vft in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType) if vft.ViewFamily == DB.ViewFamily.FloorPlan), None)
                if vt: DB.ViewPlan.Create(doc, vt.Id, lvl.Id)
        
        # ROBUSTNESS: Only preserve old AI levels when the manifest did NOT
        # provide an explicit level count (i.e. this is a partial/property edit,
        # not a storey-count change).  On contractions (50 â†' 5 floors) keeping
        # the surplus levels inflates current_levels â†' floor_dims â†' column grid,
        # producing columns for every old floor span instead of just the new 5.
        # _cleanup_registry handles the actual deletion of surplus elements.
        explicit_level_count = setup.get("levels") or setup.get("storeys")
        if not explicit_level_count:
            for tag, eid in registry.items():
                if tag.startswith("AI_Level_") and \
                        tag not in [f"AI_Level_{i+1}" for i in range(len(elevations))]:
                    lvl = doc.GetElement(eid)
                    if lvl:
                        current_levels.append(lvl)
                        reused[0] += 1

        return elevations, current_levels
    def _process_shell_dimensions(self, manifest, current_levels, registry):
        doc = self.doc
        shell = manifest.get("shell", {})
        # When the AI sets force_global_dimensions=true it means the user asked
        # for a full-building footprint change (e.g. "make it 80x100m").  Skip
        # the PRESERVE EXISTING model-reading so every floor -- including the ones
        # that already have walls -- adopts the new shell width/length.
        force_global = bool(shell.get("force_global_dimensions", False))
        w_def, l_def = 30000.0, 50000.0
        
        # 1. Base Dimension Logic:
        # Use explicit shell dimensions. If missing, try to infer from Level 1 
        # model state (most reliable reference for building-wide scale/position).
        m_w, m_l = shell.get("width"), shell.get("length")
        m_pos = shell.get("position", [0.0, 0.0])

        # INFERENCE from Level 1 if manifest hasn't provided building-wide scale
        if (m_w is None or m_l is None or shell.get("position") is None) and registry:
            w_tag_l1, l_tag_l1 = "AI_Wall_L1_S", "AI_Wall_L1_W"
            if w_tag_l1 in registry and l_tag_l1 in registry:
                w_el, l_el = doc.GetElement(registry[w_tag_l1]), doc.GetElement(registry[l_tag_l1])
                if w_el and l_el and hasattr(w_el.Location, "Curve") and hasattr(l_el.Location, "Curve"):
                    if m_w is None: m_w = w_el.Location.Curve.Length * 304.8
                    if m_l is None: m_l = l_el.Location.Curve.Length * 304.8
                    if shell.get("position") is None:
                        # South wall (S) is X-parallel; its midpoint X is the building center X.
                        # West wall (W) is Y-parallel; its midpoint Y is the building center Y.
                        p_s = w_el.Location.Curve.Evaluate(0.5, True)
                        p_w = l_el.Location.Curve.Evaluate(0.5, True)
                        m_pos = [p_s.X * 304.8, p_w.Y * 304.8]

        base_w = safe_num(m_w, w_def)
        base_l = safe_num(m_l, l_def)
        base_pos = [safe_num(m_pos[0], 0.0), safe_num(m_pos[1], 0.0)]
        shell["width"] = base_w
        shell["length"] = base_l
        shell["position"] = base_pos

        # C2: Override base dims with footprint polygon bounding box when available.
        # floor_overrides width/length can still specify per-level rectangular overrides.
        _fp_pts_dim = shell.get("footprint_points")
        if _fp_pts_dim and len(_fp_pts_dim) >= 3:
            try:
                _xs = [p[0] for p in _fp_pts_dim]
                _ys = [p[1] for p in _fp_pts_dim]
                base_w = max(_xs) - min(_xs)
                base_l = max(_ys) - min(_ys)
                shell["width"] = base_w
                shell["length"] = base_l
            except Exception:
                pass

        overrides = shell.get("floor_overrides", {})

        # When volumes are present, levels above the topmost volume should use the
        # top volume's bounding-box dims as a fallback instead of the full shell dims.
        # This prevents a full-footprint slab being placed at the roof level.
        _manifest_v = getattr(self, '_current_manifest', None) or manifest
        _vol_top_lvl = 0
        _vol_top_w = None
        _vol_top_l = None
        for _v in _manifest_v.get("volumes", []):
            try:
                _hi = int(_v["levels"][1])
                if _hi > _vol_top_lvl:
                    _vol_top_lvl = _hi
                    _vpts = _volume_footprint_pts(_v)
                    _vxs = [p[0] for p in _vpts]
                    _vys = [p[1] for p in _vpts]
                    _vol_top_w = max(_vxs) - min(_vxs)
                    _vol_top_l = max(_vys) - min(_vys)
            except Exception:
                pass

        dims = []
        for i in range(len(current_levels)):
            lvl_idx = i + 1
            ov = overrides.get(str(lvl_idx), {})

            # 1. Start with explicit per-level override (highest priority)
            w = ov.get("width")
            l = ov.get("length")

            # 2. PRESERVE EXISTING: Infer from existing model geometry.
            #    Only runs when:
            #    - force_global_dimensions is NOT set, AND
            #    - the manifest has NO floor_overrides (pure property edit, not
            #      a dimension-change operation).
            #    When floor_overrides exist, the manifest is explicitly defining
            #    which floors change and which stay at the shell default.
            #    Reading old wall dims for non-overridden floors would lock them
            #    to stale values from prior builds (e.g. L11 stuck at 50x50
            #    after L2-L10 were changed to 50x50 in a previous operation).
            has_floor_overrides = bool(overrides)
            if not force_global and not has_floor_overrides and (w is None or l is None):
                w_tag, l_tag = f"AI_Wall_L{lvl_idx}_S", f"AI_Wall_L{lvl_idx}_W"
                f_tag = f"AI_Floor_L{lvl_idx}"

                if w_tag in registry and l_tag in registry:
                    w_el, l_el = doc.GetElement(registry[w_tag]), doc.GetElement(registry[l_tag])
                    if w_el and l_el and hasattr(w_el.Location, "Curve") and hasattr(l_el.Location, "Curve"):
                        if w is None: w = w_el.Location.Curve.Length * 304.8
                        if l is None: l = l_el.Location.Curve.Length * 304.8

                if (w is None or l is None) and f_tag in registry:
                    floor_el = doc.GetElement(registry[f_tag])
                    if floor_el and hasattr(floor_el, "get_BoundingBox"):
                        bb = floor_el.get_BoundingBox(None)
                        if bb:
                            if w is None: w = (bb.Max.X - bb.Min.X) * 304.8
                            if l is None: l = (bb.Max.Y - bb.Min.Y) * 304.8

            # 3. Fall back to global shell dimensions (for NEW levels only).
            # Exception: levels above the topmost volume use the top volume's
            # bounding-box dims so the roof slab matches the uppermost mass, not
            # the full ground-floor shell footprint.
            dim_source = "override" if ov.get("width") or ov.get("length") else ("model" if w is not None else "shell")
            if w is None:
                if _vol_top_lvl > 0 and lvl_idx > _vol_top_lvl and _vol_top_w is not None:
                    w = _vol_top_w
                    l = _vol_top_l
                    dim_source = "top_volume"
                else:
                    w = shell.get("width")
                    if l is None:
                        l = shell.get("length")
            final_w = get_random_dim(w, base_w, variation=0.25)
            final_l = get_random_dim(l, base_l, variation=0.25)
            # Fix 1: apply per-level scale factor so tapered floor_dims match the actual slab size
            _scale_ovr = shell.get("footprint_scale_overrides")
            if _scale_ovr and shell.get("footprint_points"):
                _sf = _get_interpolated_scale(_scale_ovr, lvl_idx)
                final_w *= _sf
                final_l *= _sf

            # Log transitions where dimensions differ from shell default
            if abs(final_w - base_w) > 1.0 or abs(final_l - base_l) > 1.0:
                self.log("[ShellDims] L{}: {}x{} (source: {}, shell default: {}x{})".format(
                    lvl_idx, final_w, final_l, dim_source, base_w, base_l))

            dims.append((final_w, final_l))
        return dims, shell

    def _draw_volume_walls_for_level(self, k, lvl, elev, vol_pts, current_levels, elevations, registry, results, created, reused, affected_elements):
        """Draw a closed polygon of walls for a single level from a volume's footprint points."""
        import Autodesk.Revit.DB as DB  # type: ignore
        doc = self.doc
        n = len(vol_pts)
        updated = []
        for j in range(n):
            seg_start = vol_pts[j]
            seg_end   = vol_pts[(j + 1) % n]
            p1 = DB.XYZ(mm_to_ft(seg_start[0]), mm_to_ft(seg_start[1]), elev)
            p2 = DB.XYZ(mm_to_ft(seg_end[0]),   mm_to_ft(seg_end[1]),   elev)
            if p1.DistanceTo(p2) < mm_to_ft(2.0):
                continue
            arc_data = seg_start[2] if len(seg_start) > 2 else None
            is_arc_seg = arc_data and isinstance(arc_data, dict)
            if is_arc_seg:
                pm = DB.XYZ(mm_to_ft(arc_data["mid_x"]), mm_to_ft(arc_data["mid_y"]), elev)
                try:
                    curve = DB.Arc.Create(p1, p2, pm)
                except Exception:
                    try:
                        curve = DB.Line.CreateBound(p1, p2)
                    except Exception:
                        continue
                    is_arc_seg = False
            else:
                curve = DB.Line.CreateBound(p1, p2)

            tag = "AI_Wall_L{}_Vol{}".format(k + 1, j)
            wall_id = registry.get(tag)
            wall = doc.GetElement(wall_id) if wall_id else None
            if is_arc_seg and wall and isinstance(wall, DB.Wall):
                doc.Delete(wall.Id)
                wall = None
            if wall and isinstance(wall, DB.Wall):
                w_curve = wall.Location.Curve
                if not (w_curve.GetEndPoint(0).IsAlmostEqualTo(curve.GetEndPoint(0)) and
                        w_curve.GetEndPoint(1).IsAlmostEqualTo(curve.GetEndPoint(1))):
                    disallow_joins(wall)
                    try:
                        wall.Location.Curve = curve
                        affected_elements.append(wall)
                    except Exception:
                        doc.Delete(wall.Id)
                        wall = None
                if wall:
                    disallow_joins(wall)
                    reused[0] += 1
            if not wall or not isinstance(wall, DB.Wall):
                wall = DB.Wall.Create(doc, curve, lvl.Id, False)
                disallow_joins(wall)
                safe_set_comment(wall, tag)
                registry[tag] = wall.Id
                created[0] += 1
                affected_elements.append(wall)
            try:
                if k < len(current_levels) - 1:
                    wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE).Set(current_levels[k + 1].Id)
                else:
                    wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE).Set(DB.ElementId.InvalidElementId)
                    h_ft = elevations[k + 1] - elevations[k] if k < len(elevations) - 1 else mm_to_ft(1000)
                    p_h = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
                    if p_h: p_h.Set(h_ft)
            except:
                pass
            updated.append(wall)
            results["elements"].append(str(wall.Id.Value))
        return updated

    def _process_walls(self, current_levels, elevations, floor_dims, shell, registry, results, created, reused, affected_elements):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        updated = []

        # --- STACKED VOLUMES MODE ---
        # Levels assigned to a volume get their walls drawn here.
        # Remaining levels fall through to organic or rectangular mode.
        # _level_indices carries the original 1-based level number for each
        # entry in current_levels so that wall tags stay stable even when
        # some levels are consumed by volumes and the list is spliced.
        manifest = getattr(self, '_current_manifest', None) or {}
        vol_map = _build_volume_map(manifest, len(current_levels))
        _level_indices = list(range(1, len(current_levels) + 1))  # [1, 2, ..., N]
        if vol_map:
            vol_levels_done = set()
            for k, lvl in enumerate(current_levels):
                vol = vol_map.get(k + 1)
                if vol is None:
                    continue
                vol_levels_done.add(k)
                vol_pts = _volume_footprint_pts(vol)
                updated += self._draw_volume_walls_for_level(
                    k, lvl, elevations[k], vol_pts,
                    current_levels, elevations, registry, results, created, reused, affected_elements
                )
            if vol_levels_done:
                remaining_k = [k for k in range(len(current_levels)) if k not in vol_levels_done]
                if not remaining_k:
                    return updated
                # Splice down to non-volume levels; preserve original level indices for tags
                _level_indices  = [_level_indices[k]  for k in remaining_k]
                current_levels  = [current_levels[k]  for k in remaining_k]
                elevations      = [elevations[k]       for k in remaining_k]
                floor_dims      = [floor_dims[k]       for k in remaining_k]

        footprint_pts = shell.get("footprint_points")
        if footprint_pts:
            # --- ORGANIC MODE: walls from polygon/arc footprint, optionally scaled per level ---
            scale_overrides    = shell.get("footprint_scale_overrides", {})
            offset_overrides   = shell.get("footprint_offset_overrides", {})
            rotation_overrides = shell.get("footprint_rotation_overrides", {})
            for k, lvl in enumerate(current_levels):
                from revit_mcp.cancel_manager import check_cancelled
                check_cancelled("walls level {}".format(k + 1))
                elev = elevations[k]
                orig_lvl = _level_indices[k]
                scale = _get_interpolated_scale(scale_overrides, orig_lvl)
                ox, oy = _get_interpolated_offset(offset_overrides, orig_lvl)
                rot = _get_interpolated_rotation(rotation_overrides, orig_lvl)
                level_pts = _scale_footprint(footprint_pts, scale, ox, oy, rot) if (scale != 1.0 or ox or oy or rot) else footprint_pts
                n = len(level_pts)
                for j in range(n):
                    seg_start = level_pts[j]
                    seg_end   = level_pts[(j + 1) % n]
                    p1 = DB.XYZ(mm_to_ft(seg_start[0]), mm_to_ft(seg_start[1]), elev)
                    p2 = DB.XYZ(mm_to_ft(seg_end[0]),   mm_to_ft(seg_end[1]),   elev)
                    if p1.DistanceTo(p2) < mm_to_ft(2.0): continue
                    arc_data = seg_start[2] if len(seg_start) > 2 else None
                    is_arc_seg = arc_data and isinstance(arc_data, dict)
                    if is_arc_seg:
                        pm = DB.XYZ(mm_to_ft(arc_data["mid_x"]), mm_to_ft(arc_data["mid_y"]), elev)
                        try:
                            curve = DB.Arc.Create(p1, p2, pm)
                        except Exception:
                            try:
                                curve = DB.Line.CreateBound(p1, p2)
                            except Exception:
                                continue
                            is_arc_seg = False
                    else:
                        curve = DB.Line.CreateBound(p1, p2)

                    tag = "AI_Wall_L{}_Seg{}".format(orig_lvl, j)
                    wall_id = registry.get(tag)
                    wall = doc.GetElement(wall_id) if wall_id else None
                    # Arc walls must be delete-and-recreated (can't change Line->Arc in place)
                    if is_arc_seg and wall and isinstance(wall, DB.Wall):
                        doc.Delete(wall.Id)
                        wall = None

                    if wall and isinstance(wall, DB.Wall):
                        w_curve = wall.Location.Curve
                        if not (w_curve.GetEndPoint(0).IsAlmostEqualTo(curve.GetEndPoint(0)) and
                                w_curve.GetEndPoint(1).IsAlmostEqualTo(curve.GetEndPoint(1))):
                            disallow_joins(wall)
                            try:
                                wall.Location.Curve = curve
                                affected_elements.append(wall)
                            except Exception:
                                doc.Delete(wall.Id)
                                wall = None
                        if wall:
                            disallow_joins(wall)
                            reused[0] += 1

                    if not wall or not isinstance(wall, DB.Wall):
                        wall = DB.Wall.Create(doc, curve, lvl.Id, False)
                        disallow_joins(wall)
                        safe_set_comment(wall, tag)
                        registry[tag] = wall.Id
                        created[0] += 1
                        affected_elements.append(wall)

                    # Height constraint: attach to next level or set explicit height
                    try:
                        if k < len(current_levels) - 1:
                            wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE).Set(current_levels[k + 1].Id)
                        else:
                            wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE).Set(DB.ElementId.InvalidElementId)
                            h_ft = elevations[k + 1] - elevations[k] if k < len(elevations) - 1 else mm_to_ft(1000)
                            p_h = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
                            if p_h: p_h.Set(h_ft)
                    except: pass

                    updated.append(wall)
                    results["elements"].append(str(wall.Id.Value))

            # --- COURTYARD HOLE WALLS: generate enclosure walls for each footprint_holes polygon ---
            _hole_polys = _normalise_footprint_holes(shell.get("footprint_holes", []))
            for _h_idx, _hole_raw in enumerate(_hole_polys):
                for k, lvl in enumerate(current_levels):
                    from revit_mcp.cancel_manager import check_cancelled
                    check_cancelled("hole walls level {}".format(k + 1))
                    elev = elevations[k]
                    orig_lvl = _level_indices[k]
                    scale = _get_interpolated_scale(scale_overrides, orig_lvl)
                    ox, oy = _get_interpolated_offset(offset_overrides, orig_lvl)
                    rot = _get_interpolated_rotation(rotation_overrides, orig_lvl)
                    _hole_pts = _scale_footprint(_hole_raw, scale, ox, oy, rot) if (scale != 1.0 or ox or oy or rot) else _hole_raw
                    _hn = len(_hole_pts)
                    for j in range(_hn):
                        seg_start = _hole_pts[j]
                        seg_end   = _hole_pts[(j + 1) % _hn]
                        p1 = DB.XYZ(mm_to_ft(seg_start[0]), mm_to_ft(seg_start[1]), elev)
                        p2 = DB.XYZ(mm_to_ft(seg_end[0]),   mm_to_ft(seg_end[1]),   elev)
                        if p1.DistanceTo(p2) < mm_to_ft(2.0):
                            continue
                        curve = DB.Line.CreateBound(p1, p2)
                        tag = "AI_Wall_L{}_H{}_Seg{}".format(orig_lvl, _h_idx, j)
                        wall_id = registry.get(tag)
                        wall = doc.GetElement(wall_id) if wall_id else None
                        if wall and isinstance(wall, DB.Wall):
                            w_curve = wall.Location.Curve
                            if not (w_curve.GetEndPoint(0).IsAlmostEqualTo(curve.GetEndPoint(0)) and
                                    w_curve.GetEndPoint(1).IsAlmostEqualTo(curve.GetEndPoint(1))):
                                disallow_joins(wall)
                                try:
                                    wall.Location.Curve = curve
                                    affected_elements.append(wall)
                                except Exception:
                                    doc.Delete(wall.Id)
                                    wall = None
                            if wall:
                                disallow_joins(wall)
                                reused[0] += 1
                        if not wall or not isinstance(wall, DB.Wall):
                            wall = DB.Wall.Create(doc, curve, lvl.Id, False)
                            disallow_joins(wall)
                            safe_set_comment(wall, tag)
                            registry[tag] = wall.Id
                            created[0] += 1
                            affected_elements.append(wall)
                        try:
                            if k < len(current_levels) - 1:
                                wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE).Set(current_levels[k + 1].Id)
                            else:
                                wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE).Set(DB.ElementId.InvalidElementId)
                                h_ft = elevations[k + 1] - elevations[k] if k < len(elevations) - 1 else mm_to_ft(1000)
                                p_h = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
                                if p_h: p_h.Set(h_ft)
                        except: pass
                        updated.append(wall)
                        results["elements"].append(str(wall.Id.Value))
            return updated

        # --- RECTANGULAR MODE (default): 4 straight walls per level ---
        tags = ["AI_Wall_L{}_N", "AI_Wall_L{}_E", "AI_Wall_L{}_S", "AI_Wall_L{}_W"]
        _rect_overrides = shell.get("floor_overrides", {})

        for k, lvl in enumerate(current_levels):
            from revit_mcp.cancel_manager import check_cancelled
            check_cancelled("walls level {}".format(k + 1))
            orig_lvl = _level_indices[k]
            w_k, l_k = floor_dims[k]
            w_k_ft, l_k_ft = mm_to_ft(w_k), mm_to_ft(l_k)
            m_pos = shell.get("position", [0.0, 0.0])
            # Interpolate offset across sparse floor_overrides keys so lean/tilt
            # applies smoothly to every floor, not just explicitly listed ones.
            ox_mm, oy_mm = _interp_floor_override_offset(_rect_overrides, orig_lvl)
            cx_ft = mm_to_ft(m_pos[0] + ox_mm)
            cy_ft = mm_to_ft(m_pos[1] + oy_mm)
            pts = [DB.XYZ(cx_ft - w_k_ft/2, cy_ft - l_k_ft/2, 0),
                   DB.XYZ(cx_ft + w_k_ft/2, cy_ft - l_k_ft/2, 0),
                   DB.XYZ(cx_ft + w_k_ft/2, cy_ft + l_k_ft/2, 0),
                   DB.XYZ(cx_ft - w_k_ft/2, cy_ft + l_k_ft/2, 0)]

            for j in range(4):
                raw_p1 = pts[j]
                raw_p2 = pts[(j+1)%4]
                p1 = DB.XYZ(raw_p1.X, raw_p1.Y, elevations[k])
                p2 = DB.XYZ(raw_p2.X, raw_p2.Y, elevations[k])
                if p1.DistanceTo(p2) < mm_to_ft(2.0): continue
                line = DB.Line.CreateBound(p1, p2)

                tag = tags[j].format(orig_lvl)
                wall_id = registry.get(tag)
                wall = doc.GetElement(wall_id) if wall_id else None
                is_changed = False
                if wall and isinstance(wall, DB.Wall):
                    w_curve = wall.Location.Curve
                    if not (w_curve.GetEndPoint(0).IsAlmostEqualTo(line.GetEndPoint(0)) and
                            w_curve.GetEndPoint(1).IsAlmostEqualTo(line.GetEndPoint(1))):
                        disallow_joins(wall)
                        wall.Location.Curve = line
                        is_changed = True
                    disallow_joins(wall)
                    reused[0] += 1
                else:
                    wall = DB.Wall.Create(doc, line, lvl.Id, False)
                    disallow_joins(wall)
                    safe_set_comment(wall, tag)
                    created[0] += 1
                    is_changed = True
                if is_changed:
                    affected_elements.append(wall)
                try:
                    if k < len(current_levels) - 1:
                        wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE).Set(current_levels[k+1].Id)
                    else:
                        wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE).Set(DB.ElementId.InvalidElementId)
                        h_ft = elevations[k+1] - elevations[k] if k < len(elevations)-1 else mm_to_ft(1000)
                        p = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
                        if p: p.Set(h_ft)
                except: pass
                updated.append(wall)
                results["elements"].append(str(wall.Id.Value))
        return updated

    def _draw_wall(self, doc, p1, p2, lvl, wall_type, height, tag, registry, results, created, reused, affected_elements=[]):
        import Autodesk.Revit.DB as DB # type: ignore
        line = DB.Line.CreateBound(p1, p2)
        wall_id = registry.get(tag)
        wall = doc.GetElement(wall_id) if wall_id else None
        
        is_changed = False
        if wall and isinstance(wall, DB.Wall):
            w_curve = wall.Location.Curve
            if not (w_curve.GetEndPoint(0).IsAlmostEqualTo(line.GetEndPoint(0)) and \
                    w_curve.GetEndPoint(1).IsAlmostEqualTo(line.GetEndPoint(1))):
                # PRE-MOVE LOCK
                disallow_joins(wall)
                wall.Location.Curve = line
                is_changed = True
            # POST-MOVE RE-ENFORCE
            disallow_joins(wall)
            reused[0] += 1
        else:
            wall = DB.Wall.Create(doc, line, wall_type.Id, lvl.Id, height, 0, False, False)
            disallow_joins(wall)
            safe_set_comment(wall, tag)
            created[0] += 1
            is_changed = True
            
        if is_changed:
            affected_elements.append(wall)
        
        results["elements"].append(str(wall.Id.Value))
        return wall

    def _process_parapets(self, current_levels, expanded_slab_dims, floor_dims, shell, registry, results, created, reused, affected_elements):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc

        g_parapet_h = safe_num(shell.get("parapet_height"), None)
        overrides = shell.get("floor_overrides", {})
        footprint_pts = shell.get("footprint_points")
        scale_overrides    = shell.get("footprint_scale_overrides", {})
        offset_overrides   = shell.get("footprint_offset_overrides", {})
        rotation_overrides = shell.get("footprint_rotation_overrides", {})

        for k in range(len(current_levels)):
            lvl = current_levels[k]

            lvl_ov = overrides.get(str(k+1), {})
            p_h_mm = safe_num(lvl_ov.get("parapet_height", g_parapet_h), 1000)
            if p_h_mm <= 0: continue

            if footprint_pts:
                # Organic mode: parapets sit on the slab edge.
                # Slab scale = max(this level's scale, level below's scale) -- shelter rule.
                # Parapet is needed when the slab is larger than the wall above it.
                scale_here  = _get_interpolated_scale(scale_overrides, k + 1)
                scale_below = _get_interpolated_scale(scale_overrides, k) if k > 0 else 0.0
                slab_scale  = max(scale_here, scale_below)
                scale_above = _get_interpolated_scale(scale_overrides, k + 2) if k + 1 < len(current_levels) else 0.0
                if slab_scale <= scale_above:
                    continue  # wall above covers this slab edge -- no exposed parapet needed
                if scale_here >= scale_below:
                    ox, oy = _get_interpolated_offset(offset_overrides, k + 1)
                    rot    = _get_interpolated_rotation(rotation_overrides, k + 1)
                else:
                    ox, oy = _get_interpolated_offset(offset_overrides, k)
                    rot    = _get_interpolated_rotation(rotation_overrides, k)
                level_pts = _scale_footprint(footprint_pts, slab_scale, ox, oy, rot) if (slab_scale != 1.0 or ox or oy or rot) else footprint_pts
                n = len(level_pts)
                wall_type = DB.FilteredElementCollector(doc).OfClass(DB.WallType).FirstElement()
                for j in range(n):
                    seg_start = level_pts[j]
                    seg_end   = level_pts[(j + 1) % n]
                    p1 = DB.XYZ(mm_to_ft(seg_start[0]), mm_to_ft(seg_start[1]), 0)
                    p2 = DB.XYZ(mm_to_ft(seg_end[0]),   mm_to_ft(seg_end[1]),   0)
                    arc_data = seg_start[2] if len(seg_start) > 2 else None
                    tag = "AI_Parapet_L{}_Seg{}".format(k+1, j)
                    if arc_data and isinstance(arc_data, dict):
                        pm = DB.XYZ(mm_to_ft(arc_data["mid_x"]), mm_to_ft(arc_data["mid_y"]), 0)
                        try:
                            curve = DB.Arc.Create(p1, p2, pm)
                            wall_id = registry.get(tag)
                            existing = doc.GetElement(wall_id) if wall_id else None
                            if existing and isinstance(existing, DB.Wall):
                                doc.Delete(existing.Id)
                            wall = DB.Wall.Create(doc, curve, wall_type.Id, lvl.Id, mm_to_ft(p_h_mm), 0, False, False)
                            disallow_joins(wall)
                            safe_set_comment(wall, tag)
                            registry[tag] = wall.Id
                            created[0] += 1
                            affected_elements.append(wall)
                            results["elements"].append(str(wall.Id.Value))
                        except Exception:
                            pass
                    else:
                        if p1.DistanceTo(p2) < mm_to_ft(2.0): continue
                        self._draw_wall(doc, p1, p2, lvl, wall_type, mm_to_ft(p_h_mm), tag, registry, results, created, reused, affected_elements)
            else:
                # Rectangular mode: parapets only where slab overhangs the wall above
                slab_w, slab_l = expanded_slab_dims[k]
                w_above, l_above = (0, 0)
                if k < len(floor_dims):
                    w_above, l_above = floor_dims[k]

                if slab_w > w_above + 10 or slab_l > l_above + 10:
                    sw_ft, sl_ft = mm_to_ft(slab_w), mm_to_ft(slab_l)
                    wa_ft, la_ft = mm_to_ft(w_above), mm_to_ft(l_above)

                    pts = [DB.XYZ(-sw_ft/2.0, -sl_ft/2.0, 0),
                           DB.XYZ(sw_ft/2.0, -sl_ft/2.0, 0),
                           DB.XYZ(sw_ft/2.0, sl_ft/2.0, 0),
                           DB.XYZ(-sw_ft/2.0, sl_ft/2.0, 0)]

                    wall_type = DB.FilteredElementCollector(doc).OfClass(DB.WallType).FirstElement()
                    for i in range(4):
                        p1, p2 = pts[i], pts[(i+1)%4]
                        mid = (p1 + p2) / 2.0
                        is_exposed = abs(mid.X) > (wa_ft/2.0 + 0.1) or abs(mid.Y) > (la_ft/2.0 + 0.1)

                        if is_exposed:
                            tag = "AI_Parapet_L{}_{}".format(k+1, i)
                            self._draw_wall(doc, p1, p2, lvl, wall_type, mm_to_ft(p_h_mm), tag, registry, results, created, reused, affected_elements)

    def _process_floors(self, current_levels, floor_dims, shell, registry, results, created, reused, affected_elements, core_bounds_ft=None):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        ftype = DB.FilteredElementCollector(doc).OfClass(DB.FloorType).FirstElement()
        if not ftype: return
        
        # 1. SIMPLE SHELTER RULE: slab[k] = max(floor_dims[k-1], floor_dims[k])
        # Each slab covers the max of its two adjacent storeys. No upward
        # cascade -- only transition levels get expanded slabs.
        expanded_slab_dims = []
        g_c_depth = shell.get("cantilever_depth")
        overrides = shell.get("floor_overrides", {})

        for k in range(len(current_levels)):
            w_below = floor_dims[k-1][0] if k > 0 else 0
            l_below = floor_dims[k-1][1] if k > 0 else 0
            w_here = floor_dims[k][0] if k < len(floor_dims) else 0
            l_here = floor_dims[k][1] if k < len(floor_dims) else 0

            # Cantilever for THIS level
            lvl_ov = overrides.get(str(k+1), {})
            c_val = lvl_ov.get("cantilever_depth", g_c_depth)
            c_depth = get_random_dim(c_val, 1500, variation=0.5) if c_val == "random" else safe_num(c_val, 0)

            # SIMPLE SHELTER: max of adjacent storeys (no cascade propagation)
            base_w = max(w_below, w_here)
            base_l = max(l_below, l_here)
            slab_w = base_w + (c_depth * 2)
            slab_l = base_l + (c_depth * 2)

            expanded_slab_dims.append((slab_w, slab_l))

        # Diagnostic: log floor dims at transition levels so missing floors can be debugged
        from .runner import log as _flog
        for k in range(len(current_levels)):
            fd_w = floor_dims[k][0] if k < len(floor_dims) else 0
            fd_l = floor_dims[k][1] if k < len(floor_dims) else 0
            es_w, es_l = expanded_slab_dims[k]
            if k == 0 or fd_w != (floor_dims[k-1][0] if k > 0 else 0) or es_w != expanded_slab_dims[k-1][0]:
                _flog("[FloorDims] L{}: floor_dims=({:.0f},{:.0f}) expanded_slab=({:.0f},{:.0f})".format(
                    k+1, fd_w, fd_l, es_w, es_l))

        # Convert core bounds to mm for slab expansion check (core_bounds_ft is in feet).
        _core_mm = None
        if core_bounds_ft and not isinstance(core_bounds_ft, dict):
            _core_mm = (ft_to_mm(core_bounds_ft[0]), ft_to_mm(core_bounds_ft[1]),
                        ft_to_mm(core_bounds_ft[2]), ft_to_mm(core_bounds_ft[3]))

        # Build volume map once for floor loop (same manifest as _process_walls)
        _manifest_f = getattr(self, '_current_manifest', None) or {}
        vol_map_f = _build_volume_map(_manifest_f, len(current_levels))

        for k, lvl in enumerate(current_levels):
            from revit_mcp.cancel_manager import check_cancelled
            check_cancelled("floors level {}".format(k + 1))
            orig_lvl = k + 1  # 1-based; used for tags and override lookups
            slab_w, slab_l = expanded_slab_dims[k]

            # --- VOLUME SLAB ---
            vol_f = vol_map_f.get(orig_lvl)
            if vol_f is not None:
                vol_pts_f = _volume_footprint_pts(vol_f)
                # Ensure slab encompasses the central core footprint.
                # If the core extends beyond the volume polygon's bounding box,
                # expand the slab to a rectangle that contains both.
                if _core_mm:
                    _vxs = [p[0] for p in vol_pts_f]
                    _vys = [p[1] for p in vol_pts_f]
                    _vxmin, _vxmax = min(_vxs), max(_vxs)
                    _vymin, _vymax = min(_vys), max(_vys)
                    _cxmin, _cymin, _cxmax, _cymax = _core_mm
                    if (_cxmin < _vxmin - 1 or _cxmax > _vxmax + 1 or
                            _cymin < _vymin - 1 or _cymax > _vymax + 1):
                        _nxmin = min(_vxmin, _cxmin)
                        _nxmax = max(_vxmax, _cxmax)
                        _nymin = min(_vymin, _cymin)
                        _nymax = max(_vymax, _cymax)
                        vol_pts_f = [[_nxmin, _nymin], [_nxmax, _nymin],
                                     [_nxmax, _nymax], [_nxmin, _nymax]]
                        self.log("[FloorExpand] L{}: slab expanded to contain core "
                                 "({:.0f}x{:.0f} → {:.0f}x{:.0f}mm)".format(
                                     orig_lvl,
                                     _vxmax - _vxmin, _vymax - _vymin,
                                     _nxmax - _nxmin, _nymax - _nymin))
                tag = "AI_Floor_L{}".format(orig_lvl)
                loop = DB.CurveLoop()
                n_vf = len(vol_pts_f)
                for j in range(n_vf):
                    vs = vol_pts_f[j]; ve = vol_pts_f[(j + 1) % n_vf]
                    fp1 = DB.XYZ(mm_to_ft(vs[0]), mm_to_ft(vs[1]), 0)
                    fp2 = DB.XYZ(mm_to_ft(ve[0]), mm_to_ft(ve[1]), 0)
                    if fp1.DistanceTo(fp2) < mm_to_ft(2.0): continue
                    ad_v = vs[2] if len(vs) > 2 else None
                    if ad_v and isinstance(ad_v, dict):
                        fpm = DB.XYZ(mm_to_ft(ad_v["mid_x"]), mm_to_ft(ad_v["mid_y"]), 0)
                        try:
                            loop.Append(DB.Arc.Create(fp1, fp2, fpm))
                        except Exception:
                            try: loop.Append(DB.Line.CreateBound(fp1, fp2))
                            except Exception: pass
                    else:
                        try: loop.Append(DB.Line.CreateBound(fp1, fp2))
                        except Exception: pass
                if loop.NumberOfCurves() >= 3:
                    import System.Collections.Generic as Generic  # type: ignore
                    _vol_loops = Generic.List[DB.CurveLoop]()
                    _vol_loops.Add(loop)
                    try:
                        floor = DB.Floor.Create(doc, _vol_loops, ftype.Id, lvl.Id)
                        safe_set_comment(floor, tag)
                        affected_elements.append(floor)
                        registry[tag] = floor.Id
                        created[0] += 1
                        results["elements"].append(str(floor.Id.Value))
                    except Exception as _ve:
                        self.log("[Volumes] L{}: slab failed ({})".format(orig_lvl, _ve))
                continue  # volume slab done — skip shell logic below

            if slab_w < 1000 or slab_l < 1000:
                continue  # shell suppressed (organic mode uses granular floors[])
            tag = "AI_Floor_L{}".format(orig_lvl)

            footprint_pts = shell.get("footprint_points")
            m_pos = shell.get("position", [0.0, 0.0])
            cx_ft, cy_ft = mm_to_ft(m_pos[0]), mm_to_ft(m_pos[1])
            loop = DB.CurveLoop()
            if footprint_pts:
                # Organic footprint: build loop from polygon/arc points, scaled per level.
                scale_overrides    = shell.get("footprint_scale_overrides", {})
                offset_overrides   = shell.get("footprint_offset_overrides", {})
                rotation_overrides = shell.get("footprint_rotation_overrides", {})
                scale_here = _get_interpolated_scale(scale_overrides, orig_lvl)
                ox, oy = _get_interpolated_offset(offset_overrides, orig_lvl)
                rot    = _get_interpolated_rotation(rotation_overrides, orig_lvl)
                scale  = scale_here
                level_pts = _scale_footprint(footprint_pts, scale, ox, oy, rot) if (scale != 1.0 or ox or oy or rot) else footprint_pts

                # SHELTER RULE: when the floor directly below is offset or scaled differently
                # from the current floor, this slab must extend to cover it so no exposed
                # gap appears at the top of the walls below.
                # Method: for each vertex index i, pick whichever of level_pts[i] or
                # pts_below[i] is farther from the average centroid of the two polygons.
                # For convex shapes with the same vertex angular distribution (same base
                # footprint_pts template), this gives the exact outer union boundary.
                if k > 0 and (offset_overrides or scale_overrides):
                    _lvl_below = orig_lvl - 1
                    _sc_below  = _get_interpolated_scale(scale_overrides, _lvl_below)
                    _ox_b, _oy_b = _get_interpolated_offset(offset_overrides, _lvl_below)
                    _rot_b = _get_interpolated_rotation(rotation_overrides, _lvl_below)
                    _pts_below = _scale_footprint(footprint_pts, _sc_below, _ox_b, _oy_b, _rot_b) \
                        if (_sc_below != 1.0 or _ox_b or _oy_b or _rot_b) else footprint_pts
                    if len(_pts_below) == len(level_pts):
                        _cx_avg = (ox + _ox_b) / 2.0
                        _cy_avg = (oy + _oy_b) / 2.0
                        _shelter = []
                        for _ph, _pb in zip(level_pts, _pts_below):
                            _dxh = _ph[0] - _cx_avg;  _dyh = _ph[1] - _cy_avg
                            _dxb = _pb[0] - _cx_avg;  _dyb = _pb[1] - _cy_avg
                            _shelter.append(_ph if (_dxh*_dxh + _dyh*_dyh) >= (_dxb*_dxb + _dyb*_dyb) else _pb)
                        level_pts = _shelter

                # D1: Expand organic slab to cover the fixed core when offset upper
                # floors would otherwise expose the core shaft.  Skip for tapered
                # buildings (footprint_scale_overrides present) — the slab is supposed
                # to shrink there.  Also skip for footprint_points buildings — replacing
                # a custom polygon with a bounding-box rectangle destroys the building
                # shape (e.g. courtyard, L-shape).  The core must already be inside the
                # footprint_points polygon (validated by E1); if it is not, that is a
                # Gemini/engine placement error, not a slab coverage problem.
                if (_core_mm
                        and not shell.get("footprint_scale_overrides")
                        and not shell.get("footprint_points")):
                    _fp_xs_c = [p[0] for p in level_pts]
                    _fp_ys_c = [p[1] for p in level_pts]
                    _sxmin, _sxmax = min(_fp_xs_c), max(_fp_xs_c)
                    _symin, _symax = min(_fp_ys_c), max(_fp_ys_c)
                    _cx1, _cy1, _cx2, _cy2 = _core_mm
                    if (_cx1 < _sxmin - 1 or _cx2 > _sxmax + 1 or
                            _cy1 < _symin - 1 or _cy2 > _symax + 1):
                        _nxmin = min(_sxmin, _cx1); _nxmax = max(_sxmax, _cx2)
                        _nymin = min(_symin, _cy1); _nymax = max(_symax, _cy2)
                        level_pts = [[_nxmin, _nymin], [_nxmax, _nymin],
                                     [_nxmax, _nymax], [_nxmin, _nymax]]
                        self.log("[FloorExpand] L{}: organic slab expanded bbox to cover core "
                                 "({:.0f}x{:.0f}mm)".format(
                                     orig_lvl, _nxmax - _nxmin, _nymax - _nymin))

                n = len(level_pts)
                for j in range(n):
                    seg_start = level_pts[j]
                    seg_end   = level_pts[(j + 1) % n]
                    fp1 = DB.XYZ(mm_to_ft(seg_start[0]), mm_to_ft(seg_start[1]), 0)
                    fp2 = DB.XYZ(mm_to_ft(seg_end[0]),   mm_to_ft(seg_end[1]),   0)
                    if fp1.DistanceTo(fp2) < mm_to_ft(2.0): continue
                    # FLOORS ALWAYS USE STRAIGHT LINES — Revit's Floor.Create is far
                    # stricter than Wall.Create about arc-segment CurveLoops.  Any arc
                    # in the loop can trigger a "cannot compose valid boundary" rejection.
                    # With enough vertices from the bezier approximation (typically 30-100)
                    # the floor boundary looks smooth despite being a straight-line polygon.
                    # Arcs are preserved for WALLS where Revit is more permissive.
                    try:
                        loop.Append(DB.Line.CreateBound(fp1, fp2))
                    except Exception:
                        pass
                # Bounding box for void-clipping: derive from scaled footprint points
                fp_xs = [p[0] for p in level_pts]
                fp_ys = [p[1] for p in level_pts]
                w_ft = mm_to_ft(max(fp_xs) - min(fp_xs))
                l_ft = mm_to_ft(max(fp_ys) - min(fp_ys))
                # Validate: if loop has fewer than 3 curves (skipped segments left gaps),
                # fall back to a bounding-box rectangle so the slab can still be created.
                if loop.NumberOfCurves() < 3:
                    from .runner import log as _flog3
                    _flog3("[FloorDims] L{}: organic loop degenerate ({} curves), using bbox fallback".format(orig_lvl, loop.NumberOfCurves()))
                    loop = DB.CurveLoop()
                    fb_hw = w_ft / 2.0
                    fb_hl = l_ft / 2.0
                    # Use the already-computed per-level offset (ox, oy) so the fallback
                    # rectangle is centred on the actual shifted floor, not the shell origin.
                    fb_cx = cx_ft + mm_to_ft(ox)
                    fb_cy = cy_ft + mm_to_ft(oy)
                    fb1 = DB.XYZ(fb_cx - fb_hw, fb_cy - fb_hl, 0)
                    fb2 = DB.XYZ(fb_cx + fb_hw, fb_cy - fb_hl, 0)
                    fb3 = DB.XYZ(fb_cx + fb_hw, fb_cy + fb_hl, 0)
                    fb4 = DB.XYZ(fb_cx - fb_hw, fb_cy + fb_hl, 0)
                    loop.Append(DB.Line.CreateBound(fb1, fb2))
                    loop.Append(DB.Line.CreateBound(fb2, fb3))
                    loop.Append(DB.Line.CreateBound(fb3, fb4))
                    loop.Append(DB.Line.CreateBound(fb4, fb1))
            else:
                # Rectangular slab — interpolate per-level offset across sparse floor_overrides
                _sox_mm, _soy_mm = _interp_floor_override_offset(
                    shell.get("floor_overrides", {}), orig_lvl)
                _scx_ft = mm_to_ft(m_pos[0] + _sox_mm)
                _scy_ft = mm_to_ft(m_pos[1] + _soy_mm)
                w_ft, l_ft = mm_to_ft(slab_w), mm_to_ft(slab_l)
                p1 = DB.XYZ(_scx_ft - w_ft/2.0, _scy_ft - l_ft/2.0, 0)
                p2 = DB.XYZ(_scx_ft + w_ft/2.0, _scy_ft - l_ft/2.0, 0)
                p3 = DB.XYZ(_scx_ft + w_ft/2.0, _scy_ft + l_ft/2.0, 0)
                p4 = DB.XYZ(_scx_ft - w_ft/2.0, _scy_ft + l_ft/2.0, 0)
                loop.Append(DB.Line.CreateBound(p1, p2))
                loop.Append(DB.Line.CreateBound(p2, p3))
                loop.Append(DB.Line.CreateBound(p3, p4))
                loop.Append(DB.Line.CreateBound(p4, p1))

            # Build the curve loop list: outer boundary + courtyard holes + shaft voids.
            # ORGANIC FOOTPRINT NOTE: Revit's Floor.Create rejects shaft void loops
            # when the outer boundary contains arc segments.  Shaft/stair voids are
            # therefore skipped for organic shapes; the core walls enclose the shafts.
            # User-defined courtyard holes (footprint_holes) are always added — they
            # are part of the floor plate definition and use straight-line segments.
            import System.Collections.Generic as Generic # type: ignore
            floor_loops = Generic.List[DB.CurveLoop]()
            floor_loops.Add(loop)

            # Courtyard / inner-void holes from multi-subpath footprint_svg
            _courtyard_holes = _normalise_footprint_holes(shell.get("footprint_holes", []))
            if _courtyard_holes:
                _h_scale = _get_interpolated_scale(shell.get("footprint_scale_overrides", {}), orig_lvl)
                _h_ox, _h_oy = _get_interpolated_offset(shell.get("footprint_offset_overrides", {}), orig_lvl)
                _h_rot = _get_interpolated_rotation(shell.get("footprint_rotation_overrides", {}), orig_lvl)
                for _hole_raw in _courtyard_holes:
                    _hole_pts = _scale_footprint(_hole_raw, _h_scale, _h_ox, _h_oy, _h_rot) \
                        if (_h_scale != 1.0 or _h_ox or _h_oy or _h_rot) else _hole_raw
                    _hole_loop = DB.CurveLoop()
                    _nh = len(_hole_pts)
                    for _j in range(_nh):
                        _hs = _hole_pts[_j]; _he = _hole_pts[(_j + 1) % _nh]
                        _hp1 = DB.XYZ(mm_to_ft(_hs[0]), mm_to_ft(_hs[1]), 0)
                        _hp2 = DB.XYZ(mm_to_ft(_he[0]), mm_to_ft(_he[1]), 0)
                        if _hp1.DistanceTo(_hp2) < mm_to_ft(2.0):
                            continue
                        try:
                            _hole_loop.Append(DB.Line.CreateBound(_hp1, _hp2))
                        except Exception:
                            pass
                    if _hole_loop.NumberOfCurves() >= 3:
                        floor_loops.Add(_hole_loop)

            # Record loop count after courtyard holes so the void-retry fallback knows
            # which loops are safe (outer boundary + courtyard holes) vs shaft/stair voids.
            _n_safe_loops = floor_loops.Count

            # Floors ALWAYS use straight-line segments (DB.Line.CreateBound, line ~1693)
            # so _loop_has_arcs is always False.  Shaft/stair voids are now cut for
            # all slab types — organic slabs included.
            # F2: For organic slabs the void boundary uses the actual per-level
            # footprint bounding box (level_pts) so offset/scaled floors clip correctly.
            _loop_has_arcs = False  # floors never use arc segments

            if not _loop_has_arcs:
                # All slab types: add shaft/stair voids.
                shaft_voids = getattr(self, '_shaft_voids', None) or []
                stair_voids = getattr(self, '_stair_voids', None) or []
                all_voids = (list(shaft_voids) + list(stair_voids)) if k > 0 else []
                margin_ft = mm_to_ft(100.0) if footprint_pts else mm_to_ft(2.0)
                slab_hx = w_ft / 2.0 - margin_ft
                slab_hy = l_ft / 2.0 - margin_ft
                if all_voids and slab_hx > 0 and slab_hy > 0:
                    if footprint_pts:
                        # F2: Organic slab — clip voids to actual scaled/offset polygon bbox
                        # with an inset margin so void corners stay clear of curved edges.
                        _fp_xs_v = [mm_to_ft(p[0]) for p in level_pts]
                        _fp_ys_v = [mm_to_ft(p[1]) for p in level_pts]
                        slab_min_x = min(_fp_xs_v) + margin_ft
                        slab_max_x = max(_fp_xs_v) - margin_ft
                        slab_min_y = min(_fp_ys_v) + margin_ft
                        slab_max_y = max(_fp_ys_v) - margin_ft
                    else:
                        slab_min_x = cx_ft - slab_hx
                        slab_max_x = cx_ft + slab_hx
                        slab_min_y = cy_ft - slab_hy
                        slab_max_y = cy_ft + slab_hy
                    min_void_ft = mm_to_ft(200)
                    clipped = []
                    _pip_dropped = 0
                    for (vx1, vy1, vx2, vy2) in all_voids:
                        cx1 = max(vx1, slab_min_x)
                        cy1 = max(vy1, slab_min_y)
                        cx2 = min(vx2, slab_max_x)
                        cy2 = min(vy2, slab_max_y)
                        if (cx2 - cx1) < min_void_ft or (cy2 - cy1) < min_void_ft:
                            _pip_dropped += 1
                            continue
                        # For organic slabs, verify all 4 void corners lie inside the
                        # actual polygon (bbox inset is not enough for curved edges).
                        if footprint_pts:
                            from revit_mcp.staircase_logic import _point_in_polygon as _pip_v
                            _vc_mm = [
                                (ft_to_mm(cx1), ft_to_mm(cy1)), (ft_to_mm(cx2), ft_to_mm(cy1)),
                                (ft_to_mm(cx2), ft_to_mm(cy2)), (ft_to_mm(cx1), ft_to_mm(cy2)),
                            ]
                            if not all(_pip_v(x, y, level_pts) for x, y in _vc_mm):
                                _pip_dropped += 1
                                continue
                            # Reject voids whose corners land inside a courtyard hole (footprint_holes).
                            # A void inside the courtyard void is not part of the floor plate.
                            _courtyard_holes_v = _normalise_footprint_holes(shell.get("footprint_holes", []))
                            if _courtyard_holes_v:
                                _h_scale_v = _get_interpolated_scale(shell.get("footprint_scale_overrides", {}), orig_lvl)
                                _h_ox_v, _h_oy_v = _get_interpolated_offset(shell.get("footprint_offset_overrides", {}), orig_lvl)
                                _h_rot_v = _get_interpolated_rotation(shell.get("footprint_rotation_overrides", {}), orig_lvl)
                                _inside_hole = False
                                for _hole_raw_v in _courtyard_holes_v:
                                    _hole_pts_v = _scale_footprint(_hole_raw_v, _h_scale_v, _h_ox_v, _h_oy_v, _h_rot_v) \
                                        if (_h_scale_v != 1.0 or _h_ox_v or _h_oy_v or _h_rot_v) else _hole_raw_v
                                    if any(_pip_v(x, y, _hole_pts_v) for x, y in _vc_mm):
                                        _inside_hole = True
                                        break
                                if _inside_hole:
                                    _pip_dropped += 1
                                    continue
                        clipped.append((cx1, cy1, cx2, cy2))
                    if k == 1:
                        from .runner import log as _vlog
                        _vlog("[VoidDiag] L{}: {} raw voids, {} clipped, {} dropped (pip/size), slab=[{:.2f},{:.2f}]x[{:.2f},{:.2f}]ft".format(
                            k+1, len(all_voids), len(clipped), _pip_dropped,
                            slab_min_x, slab_max_x, slab_min_y, slab_max_y))
                        if clipped and footprint_pts:
                            _vlog("[VoidDiag] sample void (mm): [{:.0f},{:.0f}]x[{:.0f},{:.0f}]".format(
                                ft_to_mm(clipped[0][0]), ft_to_mm(clipped[0][1]),
                                ft_to_mm(clipped[0][2]), ft_to_mm(clipped[0][3])))
                    merged = _merge_void_rects(clipped)
                    for (vx1, vy1, vx2, vy2) in merged:
                        vp1 = DB.XYZ(vx1, vy1, 0)
                        vp2 = DB.XYZ(vx1, vy2, 0)
                        vp3 = DB.XYZ(vx2, vy2, 0)
                        vp4 = DB.XYZ(vx2, vy1, 0)
                        void_loop = DB.CurveLoop()
                        void_loop.Append(DB.Line.CreateBound(vp1, vp2))
                        void_loop.Append(DB.Line.CreateBound(vp2, vp3))
                        void_loop.Append(DB.Line.CreateBound(vp3, vp4))
                        void_loop.Append(DB.Line.CreateBound(vp4, vp1))
                        floor_loops.Add(void_loop)

                # Rotated shaft voids — stored as polygon point lists (in ft) when
                # core rotation is active.  Added after AABB voids; non-fatal if Revit
                # rejects any individual loop (the enclosure walls still seal the shaft).
                _void_polys = getattr(self, '_stair_void_polygons', None) or []
                for _poly_ft in _void_polys:
                    try:
                        _pvloop = DB.CurveLoop()
                        _pn = len(_poly_ft)
                        for _j in range(_pn):
                            _pp1 = DB.XYZ(_poly_ft[_j][0],         _poly_ft[_j][1],         0)
                            _pp2 = DB.XYZ(_poly_ft[(_j+1)%_pn][0], _poly_ft[(_j+1)%_pn][1], 0)
                            if _pp1.DistanceTo(_pp2) > mm_to_ft(2.0):
                                _pvloop.Append(DB.Line.CreateBound(_pp1, _pp2))
                        if _pvloop.NumberOfCurves() >= 3:
                            floor_loops.Add(_pvloop)
                    except Exception:
                        pass

            # Old floors were pre-deleted in PHASE 2.9 -- always create fresh.
            # Force Regenerate at dimension transitions so Revit finalises
            # the previous floor's geometry before creating the next one.
            try:
                prev_slab = expanded_slab_dims[k-1] if k > 0 else (0, 0)
                if k > 0 and (abs(slab_w - prev_slab[0]) > 1 or abs(slab_l - prev_slab[1]) > 1):
                    doc.Regenerate()

                # Try create with voids (or solid if organic)
                try:
                    floor = DB.Floor.Create(doc, floor_loops, ftype.Id, lvl.Id)
                except Exception as e_void:
                    from .runner import log as _flog2
                    # Shaft/stair voids rejected — retry with only outer boundary +
                    # courtyard holes (which always use straight-line segments and are
                    # part of the floor plate definition).  Concave footprints (L/U/T)
                    # can cause Revit to reject shaft voids whose edges touch the notch
                    # boundary; courtyard holes are unaffected by this.
                    if floor_loops.Count > _n_safe_loops:
                        _flog2("[FloorDims] L{}: void loops invalid, retrying with courtyard holes only".format(k+1))
                        _safe_loops = Generic.List[DB.CurveLoop]()
                        for _sli in range(_n_safe_loops):
                            _safe_loops.Add(floor_loops[_sli])
                        try:
                            floor = DB.Floor.Create(doc, _safe_loops, ftype.Id, lvl.Id)
                        except Exception:
                            _flog2("[FloorDims] L{}: courtyard retry failed, using solid floor".format(k+1))
                            fallback_loops = Generic.List[DB.CurveLoop]()
                            fallback_loops.Add(loop)
                            floor = DB.Floor.Create(doc, fallback_loops, ftype.Id, lvl.Id)
                    else:
                        _flog2("[FloorDims] L{}: void loops invalid, retrying solid floor".format(k+1))
                        fallback_loops = Generic.List[DB.CurveLoop]()
                        fallback_loops.Add(loop)
                        floor = DB.Floor.Create(doc, fallback_loops, ftype.Id, lvl.Id)
            except Exception as e_final:
                self.log("[FloorDims] L{}: SKIPPED ({}), trying plain bbox".format(k+1, str(e_final)[:120]))
                # Last resort: axis-aligned bounding box slab — never skip a floor entirely.
                # Use the offset-adjusted centroid so organic floors at high-offset
                # transition levels (e.g. floor 7/15/23 of an S-shape) don't drop a
                # centred rectangle that protrudes beyond the offset wall footprint.
                try:
                    _bb_ox_mm, _bb_oy_mm = (0.0, 0.0)
                    if footprint_pts:
                        _bb_ox_mm, _bb_oy_mm = _get_interpolated_offset(
                            shell.get("footprint_offset_overrides", {}), orig_lvl)
                    bb_cx_ft = cx_ft + mm_to_ft(_bb_ox_mm)
                    bb_cy_ft = cy_ft + mm_to_ft(_bb_oy_mm)
                    bb_loop = DB.CurveLoop()
                    bb_hw = w_ft / 2.0
                    bb_hl = l_ft / 2.0
                    bb1 = DB.XYZ(bb_cx_ft - bb_hw, bb_cy_ft - bb_hl, 0)
                    bb2 = DB.XYZ(bb_cx_ft + bb_hw, bb_cy_ft - bb_hl, 0)
                    bb3 = DB.XYZ(bb_cx_ft + bb_hw, bb_cy_ft + bb_hl, 0)
                    bb4 = DB.XYZ(bb_cx_ft - bb_hw, bb_cy_ft + bb_hl, 0)
                    bb_loop.Append(DB.Line.CreateBound(bb1, bb2))
                    bb_loop.Append(DB.Line.CreateBound(bb2, bb3))
                    bb_loop.Append(DB.Line.CreateBound(bb3, bb4))
                    bb_loop.Append(DB.Line.CreateBound(bb4, bb1))
                    bb_loops = Generic.List[DB.CurveLoop]()
                    bb_loops.Add(bb_loop)
                    floor = DB.Floor.Create(doc, bb_loops, ftype.Id, lvl.Id)
                    self.log("[FloorDims] L{}: bbox fallback succeeded".format(k+1))
                except Exception as e_bb:
                    self.log("[FloorDims] L{}: all floor attempts failed, skipping".format(k+1))
                    continue

            safe_set_comment(floor, tag)
            affected_elements.append(floor)
            registry[tag] = floor.Id

            if tag not in registry: created[0] += 1
            else: reused[0] += 1
            results["elements"].append(str(floor.Id.Value))
        
        return expanded_slab_dims

    def _process_columns_and_grids(self, current_levels, elevations, floor_dims, expanded_slab_dims, shell, registry, results, created, reused, affected_elements=[], core_bounds=None):
        """Structural column layout -- minimal columns, maximum span, core as structure.
        Rules:
        1. Core is structural -- no columns inside the core footprint.
        2. Maximize column span (use max of preset range) to minimize column count.
        3. Uniform grid from building center -- edge column only if cantilever > max_span.
        4. All columns inset by at least offset_from_edge from slab edge."""
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc

        # 1. Logic Setup: Span from shell -> existing model -> preset (use MAX for fewest columns)
        presets = load_presets()
        _t = getattr(self, "_manifest_typology", "")
        preset = presets.get(_t) or presets.get("default") or presets.get("commercial_office", {})
        p_col_logic = preset.get("column_logic", {})
        preset_span = p_col_logic.get("span", [12000, 15000])
        if isinstance(preset_span, list) and len(preset_span) >= 2:
            preset_span_max = float(preset_span[-1])
        elif isinstance(preset_span, (int, float)):
            preset_span_max = float(preset_span)
        else:
            preset_span_max = 15000.0

        # Use synthesized spans from synthesis run
        span_w_ft = mm_to_ft(getattr(self, '_synth_span_w', 12000))
        span_l_ft = mm_to_ft(getattr(self, '_synth_span_l', 12000))

        is_structural = True
        symbol = self._find_type(DB.BuiltInCategory.OST_StructuralColumns, shell.get("column_type", "Column"))
        if not symbol:
            # Fallback: grab first structural column family, then architectural
            symbol = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_StructuralColumns).OfClass(DB.FamilySymbol).FirstElement()
        if not symbol:
            symbol = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Columns).OfClass(DB.FamilySymbol).FirstElement()
            is_structural = False
        if not symbol: return
        if not symbol.IsActive: symbol.Activate()

        # Read offset_from_edge from shell or preset
        offset_from_edge_mm = safe_num(
            shell.get("column_offset", p_col_logic.get("offset_from_edge", 500)), 500)

        max_w = max(d[0] for d in floor_dims)
        max_l = max(d[1] for d in floor_dims)
        center_only = shell.get("columns_center_only", False) or "center area" in str(shell).lower()

        # 2. CORE-AWARE GRID: Columns are equally spaced between building edges
        #    and core walls. The grid is divided into regions by the lift core,
        #    and each region is subdivided with equal spans (minimizing column count
        #    while keeping span <= max_span). Staircases create additional 2D
        #    exclusion zones but don't affect the primary grid structure.
        import math as _math

        # Use the UNBUFFERED lift core bounds for grid definition (grid lines
        # align with actual core walls). Buffered exclusion zones are used for
        # 2D column culling only.
        exclusion_zones = getattr(self, '_core_exclusion_zones', [])
        if not exclusion_zones and core_bounds:
            exclusion_zones = [core_bounds]

        # Lift core bounds for grid computation -- use unbuffered bounds so grid
        # lines align with the actual core wall edges, not the column buffer.
        lift_core_ft = getattr(self, '_lift_core_bounds_ft', None)
        if lift_core_ft is None and exclusion_zones:
            lift_core_ft = exclusion_zones[0]

        m_pos = shell.get("position", [0.0, 0.0])
        cx_ft, cy_ft = mm_to_ft(m_pos[0]), mm_to_ft(m_pos[1])

        # Option B — polygon-aware grid anchor.
        # Use the footprint polygon's own bounding-box centre and span for grid synthesis
        # instead of shell.position + manifest width/length.  For symmetric polygons
        # (SVG-centred at origin, or plain rectangles) this is identical to the old
        # behaviour.  For asymmetric polygons (e.g. an L whose vertices are all in the
        # positive quadrant) this correctly recentres the grid so every arm is covered.
        _fp_for_grid = shell.get("footprint_points")
        if _fp_for_grid:
            try:
                _fpg_xs = [float(p[0]) for p in _fp_for_grid]
                _fpg_ys = [float(p[1]) for p in _fp_for_grid]
                _grid_dim_w  = max(_fpg_xs) - min(_fpg_xs)
                _grid_dim_l  = max(_fpg_ys) - min(_fpg_ys)
                _grid_cx_ft  = mm_to_ft((min(_fpg_xs) + max(_fpg_xs)) / 2.0)
                _grid_cy_ft  = mm_to_ft((min(_fpg_ys) + max(_fpg_ys)) / 2.0)
            except Exception:
                _grid_dim_w, _grid_dim_l = max_w, max_l
                _grid_cx_ft, _grid_cy_ft = cx_ft, cy_ft
        else:
            _grid_dim_w, _grid_dim_l = max_w, max_l
            _grid_cx_ft, _grid_cy_ft = cx_ft, cy_ft

        def compute_axis_grid(dim_mm, center_ft, core_min_ft, core_max_ft, span_ft):
            """Compute column grid positions along one axis with round-number spacing.

            All arithmetic is done in mm so that spacing is snapped to the nearest
            100 mm, then grid lines are placed symmetrically about the center.
            This guarantees positions like 12500 mm rather than 12523 mm.
            """
            # Work in mm for round-number arithmetic
            center_mm = round(center_ft * 304.8)
            span_mm = span_ft * 304.8
            reach_mm = dim_mm / 2.0 - offset_from_edge_mm  # center → outermost column

            if reach_mm <= 100:
                return [mm_to_ft(center_mm)]

            full_dist_mm = reach_mm * 2.0
            n_spans = max(1, int(_math.ceil(full_dist_mm / span_mm - 0.001)))

            # Snap spacing to nearest 50 mm so every position is a clean round number
            raw_spacing_mm = full_dist_mm / n_spans
            spacing_mm = round(raw_spacing_mm / 50.0) * 50.0
            if spacing_mm < 50:
                spacing_mm = 50.0

            # Place lines symmetrically outward from center
            positions = set()
            max_steps = int(_math.ceil(reach_mm / spacing_mm)) + 1
            for i in range(-max_steps, max_steps + 1):
                pos_mm = center_mm + i * spacing_mm
                if abs(pos_mm - center_mm) <= reach_mm + 1.0:
                    positions.add(pos_mm)

            return sorted(mm_to_ft(p) for p in positions)

        x_core_min = lift_core_ft[0] if lift_core_ft else None
        x_core_max = lift_core_ft[2] if lift_core_ft else None
        y_core_min = lift_core_ft[1] if lift_core_ft else None
        y_core_max = lift_core_ft[3] if lift_core_ft else None

        x_offsets = compute_axis_grid(_grid_dim_w, _grid_cx_ft, x_core_min, x_core_max, span_w_ft)
        y_offsets = compute_axis_grid(_grid_dim_l, _grid_cy_ft, y_core_min, y_core_max, span_l_ft)

        # Store grid positions for documentation (grid line creation)
        self._grid_x_offsets_ft = list(x_offsets)
        self._grid_y_offsets_ft = list(y_offsets)
        self._grid_cx_ft = _grid_cx_ft
        self._grid_cy_ft = _grid_cy_ft
        self._grid_max_w_ft = mm_to_ft(_grid_dim_w) / 2.0
        self._grid_max_l_ft = mm_to_ft(_grid_dim_l) / 2.0

        # Anchor for column tagging -- use lift core center (not merged core+stairs)
        if lift_core_ft:
            anchor_ft_x = (lift_core_ft[0] + lift_core_ft[2]) / 2.0
            anchor_ft_y = (lift_core_ft[1] + lift_core_ft[3]) / 2.0
        elif core_bounds:
            anchor_ft_x = (core_bounds[0] + core_bounds[2]) / 2.0
            anchor_ft_y = (core_bounds[1] + core_bounds[3]) / 2.0
        else:
            anchor_ft_x = _grid_cx_ft
            anchor_ft_y = _grid_cy_ft

        if center_only:
            half_w_ft = mm_to_ft(_grid_dim_w) / 2.0
            half_l_ft = mm_to_ft(_grid_dim_l) / 2.0
            x_offsets = [o for o in x_offsets if abs(o - _grid_cx_ft) < half_w_ft - 0.1]
            y_offsets = [o for o in y_offsets if abs(o - _grid_cy_ft) < half_l_ft - 0.1]

        # 3. PILLAR RULE with Stable Mapping
        _footprint_pts = shell.get("footprint_points")
        # Option A: for plain rectangular buildings (no SVG / organic overrides) synthesise
        # a 4-point rectangle so the unified polygon containment path always fires.
        # This is local to column logic — it does NOT affect wall or floor creation.
        if not _footprint_pts:
            _w_fp = safe_num(shell.get("width"), 30000)
            _l_fp = safe_num(shell.get("length"), _w_fp)
            _hw_fp, _hl_fp = _w_fp / 2.0, _l_fp / 2.0
            _footprint_pts = [
                [-_hw_fp, -_hl_fp], [_hw_fp, -_hl_fp],
                [_hw_fp,  _hl_fp], [-_hw_fp,  _hl_fp],
            ]
        _scale_ovr     = shell.get("footprint_scale_overrides", {})
        _offset_ovr    = shell.get("footprint_offset_overrides", {})
        _rot_ovr       = shell.get("footprint_rotation_overrides", {})

        def _tessellate_pts(pts):
            """Convert footprint_points (with optional arc mid-points) to a flat polygon."""
            import math as _mth
            poly = []
            n = len(pts)
            for _j in range(n):
                _s = pts[_j]
                _e = pts[(_j + 1) % n]
                _x1, _y1 = float(_s[0]), float(_s[1])
                _x2, _y2 = float(_e[0]), float(_e[1])
                _arc = _s[2] if len(_s) > 2 else None
                if _arc and isinstance(_arc, dict):
                    _mx, _my = float(_arc["mid_x"]), float(_arc["mid_y"])
                    _ax, _ay = _x2 - _x1, _y2 - _y1
                    _bx, _by = _mx - _x1, _my - _y1
                    _D = 2.0 * (_ax * _by - _ay * _bx)
                    if abs(_D) < 1e-6:
                        poly.append((_x1, _y1))
                        continue
                    _ux = (_by * (_ax*_ax + _ay*_ay) - _ay * (_bx*_bx + _by*_by)) / _D
                    _uy = (_ax * (_bx*_bx + _by*_by) - _bx * (_ax*_ax + _ay*_ay)) / _D
                    _ccx, _ccy = _x1 + _ux, _y1 + _uy
                    _r = _mth.sqrt(_ux*_ux + _uy*_uy)
                    _a1 = _mth.atan2(_y1 - _ccy, _x1 - _ccx)
                    _a2 = _mth.atan2(_y2 - _ccy, _x2 - _ccx)
                    _am = _mth.atan2(_my - _ccy, _mx - _ccx)
                    _PI2 = 2.0 * _mth.pi
                    _ccw = ((_am - _a1) % _PI2) < ((_a2 - _a1) % _PI2)
                    _span = (_a2 - _a1) % _PI2 if _ccw else (_a1 - _a2) % _PI2
                    _dir = 1 if _ccw else -1
                    for _si in range(24):
                        _ang = _a1 + _dir * _span * (_si / 24.0)
                        poly.append((_ccx + _r * _mth.cos(_ang), _ccy + _r * _mth.sin(_ang)))
                else:
                    poly.append((_x1, _y1))
            return poly

        def _point_in_poly(px, py, poly):
            inside = False
            n_p = len(poly)
            for _i in range(n_p):
                _fx1, _fy1 = poly[_i]
                _fx2, _fy2 = poly[(_i + 1) % n_p]
                if ((_fy1 > py) != (_fy2 > py)) and (px < (_fx2 - _fx1) * (py - _fy1) / (_fy2 - _fy1) + _fx1):
                    inside = not inside
            return inside

        # For organic builds, pre-tessellate the BASE footprint (scale=1, no offset/rotation)
        # to quickly reject positions that can never be inside any level's slab.
        _base_poly_mm = _tessellate_pts(_footprint_pts) if _footprint_pts else None

        # Cache per-level tessellated polygons (keyed by 1-based level index)
        _level_poly_cache = {}

        def _get_level_poly(level_1based):
            if level_1based in _level_poly_cache:
                return _level_poly_cache[level_1based]
            scale = _get_interpolated_scale(_scale_ovr, level_1based)
            ox_mm, oy_mm = _get_interpolated_offset(_offset_ovr, level_1based)
            rot = _get_interpolated_rotation(_rot_ovr, level_1based)
            transformed = _scale_footprint(_footprint_pts, scale, ox_mm, oy_mm, rot) if (scale != 1.0 or ox_mm or oy_mm or rot) else _footprint_pts
            poly = _tessellate_pts(transformed)
            _level_poly_cache[level_1based] = poly
            return poly

        # Option B — gap infill: for any axis gap wider than 1.1× the column span,
        # insert a midpoint grid line if the polygon actually crosses that axis position.
        # This ensures narrow arms and concave regions of irregular plans always receive
        # column coverage even when the initial rectangular grid misses them.
        # Skipped for center_only buildings (twisted towers) to avoid re-adding perimeter lines.
        if _base_poly_mm and not center_only:
            _span_w_mm = span_w_ft * 304.8
            _span_l_mm = span_l_ft * 304.8

            def _poly_crossed_by(coord_mm, is_x, poly):
                """True if the axis-aligned line at coord_mm has ≥2 edge crossings,
                meaning it passes through the polygon interior (not merely touches it)."""
                crossings = 0
                _n = len(poly)
                for _j in range(_n):
                    _x1, _y1 = poly[_j]
                    _x2, _y2 = poly[(_j + 1) % _n]
                    if is_x:
                        lo, hi = (min(_x1, _x2), max(_x1, _x2))
                        if lo < coord_mm < hi:
                            crossings += 1
                    else:
                        lo, hi = (min(_y1, _y2), max(_y1, _y2))
                        if lo < coord_mm < hi:
                            crossings += 1
                return crossings >= 2

            def _infill_axis(offsets, span_mm, is_x, poly, max_passes=6):
                out = list(offsets)
                for _ in range(max_passes):
                    inserted = False
                    for _i in range(len(out) - 1):
                        if (out[_i + 1] - out[_i]) * 304.8 > span_mm * 1.1:
                            mid_ft = (out[_i] + out[_i + 1]) / 2.0
                            if _poly_crossed_by(mid_ft * 304.8, is_x, poly):
                                out.insert(_i + 1, mid_ft)
                                inserted = True
                                break  # restart scan with updated list
                    if not inserted:
                        break
                return out

            x_offsets = _infill_axis(x_offsets, _span_w_mm, True,  _base_poly_mm)
            y_offsets = _infill_axis(y_offsets, _span_l_mm, False, _base_poly_mm)

        max_level_for_grid = {}

        for ox in x_offsets:
            for oy in y_offsets:
                # Fast-reject: if base polygon exists and point is outside even the untransformed
                # footprint bounding envelope, skip — no level could ever contain it.
                if _base_poly_mm is not None:
                    px_mm, py_mm = ft_to_mm(ox), ft_to_mm(oy)
                    if not _point_in_poly(px_mm, py_mm, _base_poly_mm):
                        # Check max-scale envelope — for flared/cantilever buildings the base
                        # poly is the smallest. Only skip if outside a generous bounding box.
                        max_scale = max((float(v) for v in _scale_ovr.values()), default=1.0) if _scale_ovr else 1.0
                        if max_scale <= 1.05:
                            continue  # No level will be larger than the base — safe to reject

                # Cull columns inside ANY core/staircase footprint (inclusive with 1mm tolerance).
                _TOL_FT = mm_to_ft(1.0)
                inside_zone = False
                for zone in exclusion_zones:
                    if (zone[0] - _TOL_FT < ox < zone[2] + _TOL_FT) and (zone[1] - _TOL_FT < oy < zone[3] + _TOL_FT):
                        inside_zone = True
                        break
                if inside_zone:
                    continue

                # Cull columns inside courtyard / inner-void holes — there is no slab there.
                _col_px_mm, _col_py_mm = ft_to_mm(ox), ft_to_mm(oy)
                _fp_holes_col = shell.get("footprint_holes", []) if shell else []
                if _fp_holes_col:
                    from revit_mcp.staircase_logic import _point_in_polygon as _pip_col
                    if any(_pip_col(_col_px_mm, _col_py_mm, _h) for _h in _fp_holes_col):
                        continue

                # Indices for tagging: normalize to span relative to anchor
                ix = int(round((ox - anchor_ft_x) / span_w_ft))
                iy = int(round((oy - anchor_ft_y) / span_l_ft))

                k_highest = -1
                px_mm, py_mm = ft_to_mm(ox), ft_to_mm(oy)
                for k in range(len(current_levels)):
                    level_1based = k + 1
                    # Per-level containment using the actual polygon for this floor.
                    # _footprint_pts is always set (Option A synthesises a rectangle when absent).
                    if _point_in_poly(px_mm, py_mm, _get_level_poly(level_1based)):
                        k_highest = k
                if k_highest >= 0:
                    max_level_for_grid[(ix, iy, ox, oy)] = k_highest

        total_cols = sum(k_max for k_max in max_level_for_grid.values())
        self.log("Structure: {} column positions planned.".format(total_cols))

        # Delete ALL existing AI columns before recreating.  When the
        # building size or grid spacing changes, old columns at stale
        # positions would persist and create an irregular grid.
        new_tags = set()
        for (ix, iy, ox, oy), k_max in max_level_for_grid.items():
            for k in range(k_max):
                new_tags.add("AI_Col_L{}_GX{}_GY{}".format(k+1, ix, iy))

        existing_col_positions = {}
        del_count = 0
        try:
            for c in [DB.BuiltInCategory.OST_Columns, DB.BuiltInCategory.OST_StructuralColumns]:
                try:
                    for el in DB.FilteredElementCollector(doc).OfCategory(c).WhereElementIsNotElementType().ToElements():
                        comment = ""
                        try:
                            p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                            if p: comment = p.AsString() or ""
                        except: pass
                        if comment.startswith("AI_Col_"):
                            if comment not in new_tags:
                                # Stale column -- no longer in the grid
                                try:
                                    doc.Delete(el.Id)
                                    del_count += 1
                                except: pass
                            elif el.Location and hasattr(el.Location, "Point"):
                                pt = el.Location.Point
                                for li, lv in enumerate(current_levels):
                                    if el.LevelId == lv.Id:
                                        key = (round(pt.X, 2), round(pt.Y, 2), li)
                                        existing_col_positions[key] = el.Id
                                        break
                except Exception:
                    pass
        except Exception:
            pass
        if del_count > 0:
            self.log("Structure: deleted {} stale columns.".format(del_count))

        count = 0
        for (ix, iy, ox, oy), k_max in max_level_for_grid.items():
            for k in range(k_max):
                count += 1
                # Consolidated logging: only report major milestones to tracker
                if count % 500 == 0:
                    self.log("Step 3c Progress: Processed {}/{} columns...".format(count, total_cols))

                tag = "AI_Col_L{}_GX{}_GY{}".format(k+1, ix, iy)
                p = DB.XYZ(ox, oy, 0)
                lvl = current_levels[k]

                col = doc.GetElement(registry[tag]) if tag in registry else None
                if not col:
                    # Check spatial index -- skip if a column already exists here
                    pos_key = (round(ox, 2), round(oy, 2), k)
                    if pos_key in existing_col_positions:
                        # Reuse the existing column at this position
                        col = doc.GetElement(existing_col_positions[pos_key])
                        if col:
                            safe_set_comment(col, tag)
                            reused[0] += 1
                if not col:
                    st = DB.Structure.StructuralType.Column if is_structural else DB.Structure.StructuralType.NonStructural
                    col = doc.Create.NewFamilyInstance(p, symbol, lvl, st)
                    safe_set_comment(col, tag)
                    existing_col_positions[(round(ox, 2), round(oy, 2), k)] = col.Id
                    created[0] += 1
                else:
                    # OPTIMIZATION: Only update if position changed
                    if col.Location.Point.DistanceTo(p) > 0.001:
                        col.Location.Point = p
                    reused[0] += 1
                
                try:
                    bip = DB.BuiltInParameter.FAMILY_TOP_LEVEL_PARAM if is_structural else DB.BuiltInParameter.COLUMN_TOP_LEVEL_PARAM
                    param = col.get_Parameter(bip)
                    # OPTIMIZATION: Only update if level changed
                    if param and param.AsElementId() != current_levels[k+1].Id:
                        param.Set(current_levels[k+1].Id)
                except: pass
                
                results["elements"].append(str(col.Id.Value))
                affected_elements.append(col)

    def _find_core_wall_type(self, doc):
        """Return a WallType whose compound-structure thickness is closest to 350mm.
        Falls back to the first available WallType if none match within 100mm."""
        import Autodesk.Revit.DB as DB  # type: ignore
        best_type = None
        best_diff = float('inf')
        fallback  = None
        target_mm = 350.0
        for wt in DB.FilteredElementCollector(doc).OfClass(DB.WallType):
            if fallback is None:
                fallback = wt
            try:
                cs = wt.GetCompoundStructure()
                if cs:
                    width_mm = cs.GetWidth() * 304.8  # internal units → mm
                    diff = abs(width_mm - target_mm)
                    if diff < best_diff:
                        best_diff = diff
                        best_type = wt
            except:
                pass
        if best_type and best_diff <= 100.0:
            return best_type
        return fallback or best_type

    def _get_door_type(self, doc, min_width_mm=1000, category_hint=""):
        """Return the best available door FamilySymbol matching category_hint and ≥ min_width_mm.

        category_hint:
          "lift"        — prefer families whose name contains lift/elevator/shutter/sliding
          "single_leaf" — prefer families whose name contains single/solid/flush
          ""            — no preference, just find first with sufficient width
        """
        import Autodesk.Revit.DB as DB  # type: ignore
        syms = list(DB.FilteredElementCollector(doc)
                    .OfCategory(DB.BuiltInCategory.OST_Doors)
                    .OfClass(DB.FamilySymbol).ToElements())
        if not syms:
            return None

        hint = category_hint.lower()
        if hint == "lift":
            keywords = ["lift", "elevator", "shutter", "sliding"]
        elif hint == "single_leaf":
            keywords = ["single", "solid", "flush"]
        else:
            keywords = []

        def _name(sym):
            parts = []
            try: parts.append(sym.Name or "")
            except: pass
            try: parts.append(sym.Family.Name or "")
            except: pass
            return " ".join(parts).lower()

        def _width_ok(sym):
            try:
                wp = sym.get_Parameter(DB.BuiltInParameter.DOOR_WIDTH)
                if not wp:
                    wp = sym.LookupParameter("Width")
                if wp:
                    return wp.AsDouble() * 304.8 >= min_width_mm
            except:
                pass
            return True  # unknown width → accept

        # Pass 1: name match + width
        if keywords:
            for sym in syms:
                n = _name(sym)
                if any(kw in n for kw in keywords) and _width_ok(sym):
                    return sym
            # Pass 2: name match only (ignore width)
            for sym in syms:
                n = _name(sym)
                if any(kw in n for kw in keywords):
                    return sym

        # Pass 3: width match only
        for sym in syms:
            if _width_ok(sym):
                return sym

        return syms[0]

    def _find_wall_for_door(self, doc, pos_mm, level=None, max_dist_mm=600, wall_line_mm=None):
        """Find the wall whose centreline is closest to pos_mm at the given level.

        wall_line_mm: optional [[x1,y1],[x2,y2]] — when provided, prefer walls
        that are collinear with this segment (same direction ± 10°).
        """
        import Autodesk.Revit.DB as DB  # type: ignore
        import math as _math
        tol_ft = mm_to_ft(max_dist_mm)
        px, py  = mm_to_ft(pos_mm[0]), mm_to_ft(pos_mm[1])
        outline = DB.Outline(
            DB.XYZ(px - tol_ft, py - tol_ft, -1000.0),
            DB.XYZ(px + tol_ft, py + tol_ft,  1000.0)
        )
        bbf      = DB.BoundingBoxIntersectsFilter(outline)
        walls    = (DB.FilteredElementCollector(doc)
                    .OfClass(DB.Wall)
                    .WherePasses(bbf)
                    .ToElements())
        target   = DB.XYZ(px, py, 0.0)

        # Precompute expected wall direction from wall_line_mm if provided
        _exp_angle = None
        if wall_line_mm and len(wall_line_mm) >= 2:
            try:
                dx = wall_line_mm[1][0] - wall_line_mm[0][0]
                dy = wall_line_mm[1][1] - wall_line_mm[0][1]
                if abs(dx) > 1 or abs(dy) > 1:
                    _exp_angle = _math.atan2(dy, dx)
            except Exception:
                pass

        best_wall, best_dist = None, float('inf')
        for wall in walls:
            try:
                if level and not self._wall_matches_level(wall, level):
                    continue
                loc = wall.Location
                if not isinstance(loc, DB.LocationCurve):
                    continue
                # If wall_line_mm provided, skip walls not collinear (±10°)
                if _exp_angle is not None:
                    try:
                        c = loc.Curve
                        wdx = c.GetEndPoint(1).X - c.GetEndPoint(0).X
                        wdy = c.GetEndPoint(1).Y - c.GetEndPoint(0).Y
                        if abs(wdx) > 1e-6 or abs(wdy) > 1e-6:
                            wall_angle = _math.atan2(wdy, wdx)
                            ang_diff = abs((_exp_angle - wall_angle + _math.pi) % _math.pi)
                            if ang_diff > _math.radians(10) and (_math.pi - ang_diff) > _math.radians(10):
                                continue
                    except Exception:
                        pass
                res = loc.Curve.Project(target)
                if res and res.Distance < best_dist:
                    best_dist = res.Distance
                    best_wall = wall
            except:
                pass
        # If directional filter returned nothing, retry without it
        if best_wall is None and _exp_angle is not None:
            return self._find_wall_for_door(doc, pos_mm, level=level,
                                            max_dist_mm=max_dist_mm, wall_line_mm=None)
        return best_wall if best_dist < tol_ft else None

    def _wall_matches_level(self, wall, level):
        """Return True if wall's base constraint matches the given level.

        Uses .Value (Revit 2024+ / Revit 2026 API) to compare ElementId values.
        """
        import Autodesk.Revit.DB as DB  # type: ignore
        try:
            base_p = wall.get_Parameter(DB.BuiltInParameter.WALL_BASE_CONSTRAINT)
            if base_p:
                wall_lvl_id = base_p.AsElementId()
                # Revit 2024+ replaced IntegerValue with Value (Int64)
                try:
                    return wall_lvl_id.Value == level.Id.Value
                except AttributeError:
                    return wall_lvl_id.IntegerValue == level.Id.IntegerValue
        except:
            pass
        return False

    def _process_doors(self, current_levels, results):
        """Place doors in staircase, fire-lobby and lift-shaft walls (Phase 5.6)."""
        import Autodesk.Revit.DB as DB  # type: ignore
        doc       = self.doc
        door_data = getattr(self, '_door_data', [])
        if not door_data:
            self.log("Door phase: no door specs, skipping.")
            return 0

        # Build level map
        level_map = {l.Name: l for l in current_levels}
        for l in current_levels:
            p = l.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if p and p.HasValue:
                level_map[p.AsString()] = l

        # Cache door symbols by category to avoid re-querying for every spec
        _door_sym_cache = {}
        def _get_sym(cat, width_mm):
            key = (cat, width_mm)
            if key not in _door_sym_cache:
                sym = self._get_door_type(doc, min_width_mm=width_mm, category_hint=cat)
                if sym and not sym.IsActive:
                    sym.Activate()
                    doc.Regenerate()
                _door_sym_cache[key] = sym
            return _door_sym_cache[key]

        # Pre-check that at least one door family is available
        if not self._get_door_type(doc, min_width_mm=0):
            self.log("Door phase: no door families loaded in project, skipping.")
            return 0

        # Refresh registry now — walls created in this build are not in the start-of-build cache
        try:
            from revit_mcp.building_generator import get_model_registry  # type: ignore
            registry = get_model_registry(doc)
            wall_keys = [k for k in registry if "Stair" in k or "LB" in k or "FL" in k]
            lb_keys = [k for k in wall_keys if "_LB_" in k]
            self.log("  Door registry: {} total, {} wall keys ({} LB lobby, sample: {}, LB sample: {})".format(
                len(registry), len(wall_keys), len(lb_keys), wall_keys[:3], lb_keys[:3]))
        except Exception as _reg_err:
            self.log("  Door registry refresh failed: {}".format(_reg_err))
            registry = getattr(self, '_registry_cache', {})

        placed = 0
        for spec in door_data:
            spec_id       = spec.get("id", "AI_Door")
            pos_mm        = spec.get("position_mm")
            lvl_ids       = spec.get("levels", [])
            swing_in      = spec.get("swing_in", True)
            flip_hand      = spec.get("flip_hand", False)
            door_cat       = spec.get("door_category", "")
            min_w          = spec.get("min_width_mm", 1000)
            wall_ai_id_map = spec.get("wall_ai_id_map", {})
            swing_out_l1   = spec.get("swing_out_level1", False)
            if not pos_mm or not lvl_ids:
                continue

            door_sym = _get_sym(door_cat, min_w)
            if not door_sym:
                self.log("  Door {}: no symbol found, skipping spec.".format(spec_id))
                continue

            for li, level_id in enumerate(lvl_ids):
                level = level_map.get(level_id)
                if not level:
                    continue
                try:
                    import Autodesk.Revit.DB as DB  # type: ignore
                    # LB lobby entry doors: level-constrained lookup is unreliable because
                    # LB walls may be registered under a different level key.  Skip directly
                    # to the wide fallback which reliably finds them by geometry.
                    _is_lb_entry = "_Lobby_EntryDoor" in spec_id and bool(wall_ai_id_map)
                    wall = None
                    if _is_lb_entry and li > 0:
                        # Floors L2+: go straight to wide fallback (faster, avoids spurious misses)
                        wall = self._find_wall_for_door(
                            doc, pos_mm, level=None,
                            max_dist_mm=3000,
                            wall_line_mm=spec.get("wall_line_mm"),
                        )
                        if wall:
                            self.log("  Door {} @ level {}: LB fast-path via wide fallback".format(spec_id, level_id))
                    else:
                        # 1. Registry-first: look up wall by deterministic AI ID
                        wall_ai_id = wall_ai_id_map.get(level_id) if wall_ai_id_map else None
                        if wall_ai_id and wall_ai_id in registry:
                            elem = doc.GetElement(registry[wall_ai_id])
                            if elem and isinstance(elem, DB.Wall):
                                wall = elem

                        # 2. Geometry fallback (level-constrained, direction-aware)
                        if not wall:
                            wall = self._find_wall_for_door(
                                doc, pos_mm, level=level,
                                wall_line_mm=spec.get("wall_line_mm"),
                            )
                        # 3. Wide geometry fallback — if still not found, try without level constraint
                        if not wall:
                            wall = self._find_wall_for_door(
                                doc, pos_mm, level=None,
                                max_dist_mm=3000,
                                wall_line_mm=spec.get("wall_line_mm"),
                            )
                            if wall:
                                self.log("  Door {} @ level {}: found via wide fallback (no-level, 3000mm)".format(spec_id, level_id))
                    if not wall:
                        # Log registry miss info for diagnosis
                        _dbg_ai_id = wall_ai_id_map.get(level_id) if wall_ai_id_map else None
                        _in_reg = _dbg_ai_id in registry if _dbg_ai_id else False
                        self.log("  Door {} @ level {}: no wall found (ai_id={}, in_registry={})".format(
                            spec_id, level_id, _dbg_ai_id, _in_reg))
                        continue

                    _raw_pt = DB.XYZ(
                        mm_to_ft(pos_mm[0]),
                        mm_to_ft(pos_mm[1]),
                        level.ProjectElevation
                    )
                    # Clamp insertion point onto the wall's curve so Revit doesn't
                    # error with "point is not on wall" when position is slightly outside.
                    try:
                        _wall_curve = wall.Location.Curve
                        _proj = _wall_curve.Project(_raw_pt)
                        ins_pt = DB.XYZ(_proj.XYZPoint.X, _proj.XYZPoint.Y, level.ProjectElevation)
                    except Exception:
                        ins_pt = _raw_pt
                    door_inst = doc.Create.NewFamilyInstance(
                        ins_pt, door_sym, wall, level,
                        DB.Structure.StructuralType.NonStructural
                    )
                    # Flip facing so external doors (and level-1 staircase doors) swing OUT
                    effective_swing_in = swing_in and not (li == 0 and swing_out_l1)
                    if not effective_swing_in:
                        try:
                            if door_inst.CanFlipFacing:
                                door_inst.FlipFacing()
                        except:
                            pass
                    # Flip hand so door leaf rests against the nearest perpendicular wall
                    if flip_hand:
                        try:
                            if door_inst.CanFlipHand:
                                door_inst.FlipHand()
                        except:
                            pass
                    safe_set_comment(door_inst, "{}_L{}".format(spec_id, li + 1))
                    results["elements"].append(str(door_inst.Id.Value))
                    placed += 1
                except Exception as _de:
                    self.log("  Door {} @ level {}: {}".format(spec_id, level_id, _de))

        self.log("Door phase complete: {} doors placed.".format(placed))
        return placed

    def _process_granular_walls(self, manifest, current_levels, registry, results, created, reused, affected_elements=[]):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        level_map = {l.Name: l for l in current_levels}
        for l in current_levels:
            p = l.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if p and p.HasValue: level_map[p.AsString()] = l
        # Diagnostic: log level_map keys to verify AI Level N keys are present
        _lvl_keys = sorted(level_map.keys())
        self.log("  GranWalls level_map: {} keys, first 4: {}".format(len(_lvl_keys), _lvl_keys[:4]))
        # Pre-fetch a 350mm WallType for core walls (user requirement); fall back to first available
        wall_type = self._find_core_wall_type(doc)

        granular_walls = []
        for w_data in manifest.get("walls", []):
            ai_id = w_data.get("id")
            if not ai_id: continue

            p1_raw = w_data.get("start", [0, 0, 0])
            p2_raw = w_data.get("end", [1000, 0, 0])
            p1 = DB.XYZ(mm_to_ft(p1_raw[0]), mm_to_ft(p1_raw[1]), 0)
            p2 = DB.XYZ(mm_to_ft(p2_raw[0]), mm_to_ft(p2_raw[1]), 0)

            # Arc wall: supported via mid_x/mid_y fields (or "mid": [x,y] list)
            mid_raw = w_data.get("mid") or (
                [w_data["mid_x"], w_data["mid_y"]] if "mid_x" in w_data and "mid_y" in w_data else None
            )
            is_arc = mid_raw is not None
            if is_arc:
                mx = mid_raw[0] if isinstance(mid_raw, (list, tuple)) else mid_raw.get("x", mid_raw.get("mid_x", 0))
                my = mid_raw[1] if isinstance(mid_raw, (list, tuple)) else mid_raw.get("y", mid_raw.get("mid_y", 0))
                pm = DB.XYZ(mm_to_ft(mx), mm_to_ft(my), 0)
                try:
                    curve = DB.Arc.Create(p1, p2, pm)
                except Exception as e:
                    self.log("  Arc wall {}: bad arc points ({}), falling back to line.".format(ai_id, e))
                    is_arc = False
                    curve = DB.Line.CreateBound(p1, p2)
            else:
                curve = DB.Line.CreateBound(p1, p2)

            lvl = level_map.get(w_data.get("level_id"))
            if not lvl:
                if "_LB_" in ai_id:
                    self.log("  GranWalls WARN: LB wall {} has unknown level_id '{}', falling back to L1".format(
                        ai_id, w_data.get("level_id")))
                lvl = current_levels[0]

            existing = doc.GetElement(registry[ai_id]) if ai_id in registry else None

            # For arc walls: always delete and recreate — Revit does not support
            # changing a straight wall's Location.Curve to an Arc in-place.
            if is_arc and existing and isinstance(existing, DB.Wall):
                doc.Delete(existing.Id)
                existing = None

            wall = None
            is_changed = False
            if existing and isinstance(existing, DB.Wall):
                wall = existing
                w_curve = wall.Location.Curve
                def _xy_eq(a, b):
                    return abs(a.X - b.X) < 0.001 and abs(a.Y - b.Y) < 0.001
                wp0 = w_curve.GetEndPoint(0)
                wp1 = w_curve.GetEndPoint(1)
                cp0 = curve.GetEndPoint(0)
                cp1 = curve.GetEndPoint(1)
                same_fwd = _xy_eq(wp0, cp0) and _xy_eq(wp1, cp1)
                same_rev = _xy_eq(wp0, cp1) and _xy_eq(wp1, cp0)
                if not same_fwd and not same_rev:
                    disallow_joins(wall)
                    try:
                        wall.Location.Curve = curve
                        is_changed = True
                    except Exception as e:
                        doc.Delete(wall.Id)
                        wall = DB.Wall.Create(doc, curve, wall_type.Id, lvl.Id, 10.0, 0.0, False, False)
                        disallow_joins(wall)
                        safe_set_comment(wall, ai_id)
                        registry[ai_id] = wall.Id
                        is_changed = True
                if not is_changed:
                    disallow_joins(wall)
                reused[0] += 1
            else:
                wall = DB.Wall.Create(doc, curve, wall_type.Id, lvl.Id, 10.0, 0.0, False, False)
                disallow_joins(wall)
                safe_set_comment(wall, ai_id)
                registry[ai_id] = wall.Id
                created[0] += 1
                is_changed = True
            
            if is_changed:
                affected_elements.append(wall)
            
            # Height & Constraints
            h = w_data.get("height")
            if h:
                # Disconnect from top level if height is literal
                try:
                    p_top = wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE)
                    if p_top: p_top.Set(DB.ElementId.InvalidElementId)
                    p_h = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
                    if p_h: p_h.Set(mm_to_ft(h))
                except: pass
            else:
                # Fallback: Try to connect to level above if not specified
                try:
                    current_idx = next((idx for idx, l in enumerate(current_levels) if l.Id == lvl.Id), -1)
                    if current_idx != -1 and current_idx < len(current_levels) - 1:
                        wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE).Set(current_levels[current_idx+1].Id)
                except: pass
            
            results["elements"].append(str(wall.Id.Value))
            granular_walls.append(wall)
        return granular_walls

    def _process_granular_floors(self, manifest, current_levels, registry, results, created, reused, affected_elements=[]):
        import Autodesk.Revit.DB as DB # type: ignore
        import System.Collections.Generic as Generic # type: ignore
        doc = self.doc
        level_map = {l.Name: l for l in current_levels}
        for l in current_levels:
            p = l.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if p and p.HasValue: level_map[p.AsString()] = l
            
        ftype = DB.FilteredElementCollector(doc).OfClass(DB.FloorType).FirstElement()

        for f_data in manifest.get("floors", []):
            ai_id = f_data.get("id")
            if not ai_id: continue
            
            pts = f_data.get("points", [])
            if len(pts) < 3: continue
            
            loop = DB.CurveLoop()
            for i in range(len(pts)):
                p1_raw = pts[i]
                p2_raw = pts[(i+1)%len(pts)]
                p1 = DB.XYZ(mm_to_ft(p1_raw[0]), mm_to_ft(p1_raw[1]), 0)
                p2 = DB.XYZ(mm_to_ft(p2_raw[0]), mm_to_ft(p2_raw[1]), 0)
                loop.Append(DB.Line.CreateBound(p1, p2))
            
            lvl_name = f_data.get("level_id")
            lvl = level_map.get(lvl_name)
            if not lvl: lvl = current_levels[0]
            
            # Elevation Logic
            target_elev_mm = f_data.get("elevation")
            offset_ft = 0.0
            if target_elev_mm is not None:
                lvl_base_elev_mm = lvl.ProjectElevation * 304.8
                offset_ft = mm_to_ft(target_elev_mm - lvl_base_elev_mm)
            
            floor = doc.GetElement(registry[ai_id]) if ai_id in registry else None
            if floor and isinstance(floor, DB.Floor):
                doc.Delete(floor.Id)
            
            try:
                loops = Generic.List[DB.CurveLoop]()
                loops.Add(loop)
                floor = DB.Floor.Create(doc, loops, ftype.Id, lvl.Id)
                safe_set_comment(floor, ai_id)

                # Set Height Offset if needed
                if abs(offset_ft) > 0.001:
                    p_offset = floor.get_Parameter(DB.BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM)
                    if p_offset: p_offset.Set(offset_ft)

                created[0] += 1
                results["elements"].append(str(floor.Id.Value))
            except Exception as e:
                # Non-fatal: skip this landing slab and log — do NOT roll back the
                # transaction (which would also wipe lift-core walls and shell floors).
                self.log(f"GEOMETRY WARN (skipped) granular floor '{ai_id}': {e}")
                continue
        return None

    def _process_granular_columns(self, manifest, current_levels, registry, results, created, reused):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        level_map = {l.Name: l for l in current_levels}
        for l in current_levels:
            p = l.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if p and p.HasValue: level_map[p.AsString()] = l
            
        symbol = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_StructuralColumns).OfClass(DB.FamilySymbol).FirstElement()
        if not symbol:
            symbol = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Columns).OfClass(DB.FamilySymbol).FirstElement()
        if not symbol: return
        if not symbol.IsActive: symbol.Activate()

        for c_data in manifest.get("columns", []):
            ai_id = c_data.get("id")
            if not ai_id: continue
            
            loc_raw = c_data.get("location", [0, 0, 0])
            p = DB.XYZ(mm_to_ft(loc_raw[0]), mm_to_ft(loc_raw[1]), mm_to_ft(loc_raw[2]))
            
            lvl = level_map.get(c_data.get("level_id"))
            if not lvl: lvl = current_levels[0]
            
            col = doc.GetElement(registry[ai_id]) if ai_id in registry else None
            if col and isinstance(col, DB.FamilyInstance):
                col.Location.Point = p
                reused[0] += 1
            else:
                st = DB.Structure.StructuralType.Column
                col = doc.Create.NewFamilyInstance(p, symbol, lvl, st)
                safe_set_comment(col, ai_id)
                created[0] += 1
            
            results["elements"].append(str(col.Id.Value))

    def _cleanup_registry(self, registry, results, deleted):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        touched = set(results["levels"]) | set(results["elements"])

        # Do not delete elements that were touched in this build (they are in `touched`).
        # There are no additional permanent exceptions — the old pattern of protecting
        # ALL "LiftR" and "Stair_" elements caused stale walls from a previous build to
        # survive cleanup when the staircase count or positions changed.
        def _is_protected_core_element(_el):
            return False  # touched-set check above already handles reused elements
        
        # 1. Broad Scan for any element with "AI_" comment that wasn't touched
        cats = [DB.BuiltInCategory.OST_Walls, DB.BuiltInCategory.OST_Floors, DB.BuiltInCategory.OST_Levels, 
                DB.BuiltInCategory.OST_Grids, DB.BuiltInCategory.OST_Columns, DB.BuiltInCategory.OST_StructuralColumns]
        import System.Collections.Generic as Generic # type: ignore
        net_cats = Generic.List[DB.BuiltInCategory]()
        for c in cats: net_cats.Add(c)
        filter = DB.ElementMulticategoryFilter(net_cats)
        
        # CRITICAL: Convert to list of IDs/Elements to avoid "Iterator Cannot Proceed" exception
        all_ai_elements = DB.FilteredElementCollector(doc).WherePasses(filter).WhereElementIsNotElementType().ToElements()
        
        # Collect all views for association check (Pre-Index by Level for speed)
        all_views = DB.FilteredElementCollector(doc).OfClass(DB.ViewPlan).ToElements()
        views_by_level = {}
        for v in all_views:
            if v.GenLevel:
                l_id = v.GenLevel.Id
                if l_id not in views_by_level: views_by_level[l_id] = []
                views_by_level[l_id].append(v)
        
        for el in list(all_ai_elements):
            if not el.IsValidObject: continue
            eid_str = str(el.Id.Value)
            if eid_str in touched: continue
            
            # Final check that it IS an AI element
            is_ai = False
            p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if not p: p = el.LookupParameter("Comments")
            if p and p.HasValue and p.AsString().startswith("AI_"):
                is_ai = True

            # Robustness: Also check Name for Levels and Views
            if not is_ai:
                if isinstance(el, DB.Level) and "AI Level" in el.Name: is_ai = True
                if hasattr(el, "Name") and "AI_" in el.Name: is_ai = True

            if is_ai:
                # NEVER delete protected core elements (lift / stair walls)
                if _is_protected_core_element(el):
                    continue
                # If it's a level, delete associated floor plans first
                if isinstance(el, DB.Level) and el.Id in views_by_level:
                    for v in views_by_level[el.Id]:
                        try:
                            if v.Pinned: v.Pinned = False
                            doc.Delete(v.Id)
                        except: pass
                
                try:
                    if hasattr(el, "Pinned") and el.Pinned: el.Pinned = False
                    # Grids with auto-dimensions cannot be deleted directly; cascade first
                    if isinstance(el, DB.Grid):
                        try:
                            for dep_id in el.GetDependentElements(None):
                                try:
                                    dep = doc.GetElement(dep_id)
                                    if dep and isinstance(dep, DB.Dimension):
                                        doc.Delete(dep_id)
                                except: pass
                        except: pass
                    doc.Delete(el.Id)
                    deleted[0] += 1
                except Exception as ex:
                    # Silent skip, but we've tried our best
                    pass

    def _generate_documentation(self, current_levels, elevations, floor_dims, max_w, max_l):
        """Auto-create Revit Grid objects at the structural column grid positions.

        X-axis grids (vertical lines) are labelled A, B, C, ...
        Y-axis grids (horizontal lines) are labelled 1, 2, 3, ...
        """
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc

        x_offsets = getattr(self, '_grid_x_offsets_ft', [])
        y_offsets = getattr(self, '_grid_y_offsets_ft', [])
        if not x_offsets and not y_offsets:
            return

        half_w_ft = mm_to_ft(max_w) / 2.0
        half_l_ft = mm_to_ft(max_l) / 2.0
        # Grid lines extend slightly beyond the building for clarity
        overshoot_ft = mm_to_ft(3000)

        def _delete_grid_cascade(g):
            """Delete a grid and its dependent dimensions so the delete cannot be blocked."""
            try:
                for dep_id in g.GetDependentElements(None):
                    try:
                        dep = doc.GetElement(dep_id)
                        if dep and isinstance(dep, DB.Dimension):
                            doc.Delete(dep_id)
                    except: pass
            except: pass
            try:
                doc.Delete(g.Id)
                return True
            except: return False

        # Delete existing AI grids to prevent duplicates.
        # Use LookupParameter fallback because ALL_MODEL_INSTANCE_COMMENTS may be
        # unavailable for Grid objects in some Revit versions.
        for g in list(DB.FilteredElementCollector(doc).OfClass(DB.Grid).ToElements()):
            try:
                p = g.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                if not p: p = g.LookupParameter("Comments")
                if p and p.HasValue and "AI_Grid" in p.AsString():
                    _delete_grid_cascade(g)
            except: pass

        # Collect all floor plan views once.
        all_plan_views = list(DB.FilteredElementCollector(doc).OfClass(DB.ViewPlan).ToElements())

        # --- PASS 1: create all grid objects, record (grid, line) pairs ---
        created_grids = []  # [(grid_element, creation_line), ...]

        def _create_grid(line, label, comment):
            for existing in list(DB.FilteredElementCollector(doc).OfClass(DB.Grid).ToElements()):
                try:
                    if existing.Name == label:
                        _delete_grid_cascade(existing)
                except: pass
            try:
                grid = DB.Grid.Create(doc, line)
                grid.Name = label
                safe_set_comment(grid, comment)
                created_grids.append((grid, line))
            except Exception: pass

        # X-axis grids: vertical lines (constant X, varying Y) -- labelled A, B, C, ...
        # x_offsets is sorted ascending (left → right), so A is always on the left.
        sorted_x = sorted(x_offsets)
        for idx, ox in enumerate(sorted_x):
            label = ""
            n = idx
            while True:
                label = chr(ord('A') + n % 26) + label
                n = n // 26 - 1
                if n < 0:
                    break
            p1 = DB.XYZ(ox, -half_l_ft - overshoot_ft, 0)
            p2 = DB.XYZ(ox,  half_l_ft + overshoot_ft, 0)
            _create_grid(DB.Line.CreateBound(p1, p2), label, "AI_Grid_X_{}".format(idx))

        # Y-axis grids: horizontal lines (constant Y, varying X) -- labelled 1, 2, 3, ...
        # y_offsets is sorted ascending (bottom → top in plan). Enumerate in reverse so
        # "1" lands on the topmost grid line (largest Y / North side), matching convention.
        sorted_y_top_first = sorted(y_offsets, reverse=True)
        for idx, oy in enumerate(sorted_y_top_first):
            label = str(idx + 1)
            p1 = DB.XYZ(-half_w_ft - overshoot_ft, oy, 0)
            p2 = DB.XYZ( half_w_ft + overshoot_ft, oy, 0)
            _create_grid(DB.Line.CreateBound(p1, p2), label, "AI_Grid_Y_{}".format(idx))

        # --- PASS 2: extend grid 3D height and propagate to all floor plan views ---
        # Root cause: Grid.Create() only registers the datum in the currently active
        # view (Level 1 plan). Two fixes applied together:
        #   1. SetVerticalExtents(0, top) — extends the grid's 3D model height so it
        #      spans all floors (also fixes section/elevation appearance).
        #   2. PropagateToViews(source, target_ids) — registers the datum in every
        #      other floor plan view so it becomes visible there.
        try: doc.Regenerate()
        except: pass

        total_height_ft = max(elevations) if elevations else mm_to_ft(50000)
        self.log("[Grid] Total building height for vertical extents: {:.2f} ft".format(total_height_ft))

        # Find the source view: lowest-elevation non-template floor plan (Level 1).
        # That is the view where Grid.Create() registered the datum.
        source_view = None
        for v in all_plan_views:
            try:
                if v.ViewType != DB.ViewType.FloorPlan or v.IsTemplate:
                    continue
                if source_view is None:
                    source_view = v
                elif v.GenLevel and source_view.GenLevel and v.GenLevel.Elevation < source_view.GenLevel.Elevation:
                    source_view = v
            except:
                pass
        if source_view is None and all_plan_views:
            source_view = all_plan_views[0]
        self.log("[Grid] Source view for PropagateToViews: {}".format(source_view.Name if source_view else "None"))

        # Build a .NET HashSet of target view IDs (all floor plan views except source).
        target_ids = None
        try:
            from System.Collections.Generic import HashSet as CSHashSet
            target_ids = CSHashSet[DB.ElementId]()
            for v in all_plan_views:
                try:
                    if v.ViewType == DB.ViewType.FloorPlan and not v.IsTemplate:
                        if source_view is None or v.Id != source_view.Id:
                            target_ids.Add(v.Id)
                except:
                    pass
            self.log("[Grid] Propagating to {} target floor plan views".format(target_ids.Count))
        except Exception as hse:
            self.log("[Grid] ERROR building HashSet for PropagateToViews: {}".format(hse))

        for grid, _line in created_grids:
            # Step A: extend 3D vertical height so the grid spans all levels
            try:
                grid.SetVerticalExtents(0.0, total_height_ft)
            except Exception as sve:
                self.log("[Grid] SetVerticalExtents FAILED for '{}': {}".format(grid.Name, sve))
            # Step B: register the datum in all other floor plan views
            if source_view and target_ids is not None and target_ids.Count > 0:
                try:
                    grid.PropagateToViews(source_view, target_ids)
                except Exception as pve:
                    self.log("[Grid] PropagateToViews FAILED for '{}': {}".format(grid.Name, pve))

        # --- PASS 3: dimensions on every floor plan view ---
        try: doc.Regenerate()
        except: pass

        for view in all_plan_views:
            try:
                self._dimension_grids_in_view(view)
            except: pass

    def _expand_unified_vertical_circulation(self, manifest, current_levels, elevations, floor_dims, affected_elements=[], results=None):
        """
        Unified expansion for all core elements (Lifts, Stairs, Lobbies).
        Ensures a single cohesive layout and prevents spatial conflicts.
        """
        import Autodesk.Revit.DB as DB # type: ignore
        from revit_mcp.utils import mm_to_ft, load_presets, safe_num
        from revit_mcp import staircase_logic, fire_safety_logic, lift_logic
        
        setup = manifest.get("project_setup", {})
        typology = (manifest.get("typology") or setup.get("typology", "")).lower().replace(" ", "_")
        presets = load_presets()
        preset = presets.get(typology) or presets.get("default") or presets.get("commercial_office", {})
        
        # 1. Base Dimensions
        num_storeys = len(elevations) - 1
        # Use manifest level_height as the true typical floor height.
        # DO NOT derive from elevations[1]-elevations[0]: that is the first storey (often a
        # tall lobby, e.g. 8400mm), which is NOT representative of the typical floor.
        # When the wrong value is passed to get_stair_run_data, the Smart Detection there
        # overrides it with min(heights) — causing all regular floors to be computed against
        # the MINIMUM height (e.g. 3000mm), producing too many flights and low headroom.
        _preset_typical = safe_num(preset.get("floor_heights", {}).get("typical", 0), 0)
        _manifest_level_h = safe_num(setup.get("level_height", 0), 0)
        if _manifest_level_h > 0:
            typical_h_mm = _manifest_level_h
        elif _preset_typical > 0:
            typical_h_mm = _preset_typical
        elif len(elevations) > 2:
            typical_h_mm = round((elevations[2] - elevations[1]) * 304.8)  # skip lobby (floor 1)
        else:
            typical_h_mm = round((elevations[1] - elevations[0]) * 304.8) if len(elevations) > 1 else 4000
        
        # 2. Lift Requirements
        lifts_config = manifest.get("lifts", {})

        # Read core centre + orientation up-front (used in both pre-computation and main layout)
        _raw_pos = lifts_config.get("position")
        if _raw_pos and isinstance(_raw_pos, (list, tuple)) and len(_raw_pos) >= 2:
            try:
                f_center_mm = [float(_raw_pos[0]), float(_raw_pos[1])]
            except (TypeError, ValueError):
                f_center_mm = [0.0, 0.0]
        else:
            f_center_mm = [0.0, 0.0]
        # If the SVG footprint was re-centred at origin, shift lifts.position by the same offset
        # so the position stays consistent with the re-centred footprint_points polygon.
        _svg_offset_shell = manifest.get("shell", {}).get("_svg_offset")
        if _svg_offset_shell and (f_center_mm[0] != 0.0 or f_center_mm[1] != 0.0):
            f_center_mm[0] -= float(_svg_offset_shell[0])
            f_center_mm[1] -= float(_svg_offset_shell[1])
            self.log("[SVG] lifts.position adjusted by offset ({:.0f},{:.0f}) -> ({:.0f},{:.0f})".format(
                float(_svg_offset_shell[0]), float(_svg_offset_shell[1]),
                f_center_mm[0], f_center_mm[1]))
        _core_orientation = str(lifts_config.get("orientation", "auto")).upper()
        if _core_orientation not in ("NS", "EW", "AUTO"):
            _core_orientation = "AUTO"
        try:
            _core_rotation_deg = float(lifts_config.get("rotation_deg", 0.0))
        except (TypeError, ValueError):
            _core_rotation_deg = 0.0

        num_lifts = lifts_config.get("count")
        shell = manifest.get("shell", {})
        _fw = safe_num(shell.get("width", 30000), 30000)
        _fl_dim = safe_num(shell.get("length", 50000), 50000)
        _fp_pts_early = shell.get("footprint_points")
        _occ_density = safe_num(lifts_config.get("occupancy_density", 0.067), 0.067)
        # Apply 60% NFA efficiency (core, corridors, toilets occupy ~40% of GFA)
        _NFA_RATIO = 0.60
        if _fp_pts_early and len(_fp_pts_early) >= 3:
            _floor_area_mm2 = _poly_area_mm2(_fp_pts_early)
        else:
            _floor_area_mm2 = _fw * _fl_dim
        _total_occ = max(100, (_floor_area_mm2 / 1e6) * _NFA_RATIO * _occ_density * num_storeys)
        _auto_lifts = lift_logic.calculate_lift_requirements(num_storeys, typical_h_mm, _total_occ)
        # AI may return count as a dict e.g. {"passenger": 4, "fire_fighting": 2}
        if isinstance(num_lifts, dict):
            num_lifts = num_lifts.get("passenger", num_lifts.get("total", num_lifts.get("count", None)))
        if num_lifts is None or num_lifts == "random":
            num_lifts = _auto_lifts
        else:
            try:
                _manifest_n = int(num_lifts)
                # Accept the manifest count if it is within 20% of the calculated
                # value; only enforce the RTT floor when the manifest is clearly
                # under-provisioned (< 80% of calculated).
                if _manifest_n >= max(2, int(_auto_lifts * 0.8)):
                    num_lifts = _manifest_n
                else:
                    num_lifts = _auto_lifts
            except (TypeError, ValueError):
                num_lifts = _auto_lifts
        num_lifts = int(num_lifts)
        self._last_num_lifts = num_lifts # Cache for later
        
        lobby_w = lifts_config.get("lobby_width", preset.get("core_logic", {}).get("lift_lobby_width", 3000))
        self._last_lobby_w = lobby_w
        
        # 3. Fire Safety & Staircase Spec
        preset_fs = preset.get("core_logic", {}).get("fire_safety", {})
        stair_spec = preset_fs.get("staircase_spec", {}).copy()
        m_stair_config = manifest.get("staircases", {})
        m_spec = m_stair_config.get("spec", {})
        for k in ["riser", "tread", "width_of_flight", "landing_width"]:
            if k in m_spec: stair_spec[k] = safe_num(m_spec[k], stair_spec.get(k))

        # 3a. Apply RAG-derived compliance_parameters as overrides.
        # compliance_parameters is populated by Gemini from live RAG values.
        # These take priority over both the preset and the static JSON fallbacks.
        # Only fall back to preset/JSON if a key is absent or zero.
        cp = self._compliance_params  # extracted from manifest at top of execute()
        _cp_overrides = {}

        # Staircase geometry — riser/tread/headroom/overrun from compliance_parameters
        if cp.get("stair_riser_mm"):
            stair_spec["riser"] = int(cp["stair_riser_mm"])
            _cp_overrides["max_riser_mm"] = int(cp["stair_riser_mm"])
        if cp.get("stair_tread_mm"):
            stair_spec["tread"] = int(cp["stair_tread_mm"])
            _cp_overrides["min_tread_mm"] = int(cp["stair_tread_mm"])
        if cp.get("stair_headroom_mm"):
            _cp_overrides["min_headroom_mm"] = int(cp["stair_headroom_mm"])
        if cp.get("stair_overrun_mm"):
            _cp_overrides["overrun_height_mm"] = int(cp["stair_overrun_mm"])
        if cp.get("max_travel_distance_mm"):
            _cp_overrides["max_travel_distance_mm"] = int(cp["max_travel_distance_mm"])
        if cp.get("max_travel_distance_sprinklered_mm"):
            _cp_overrides["max_travel_distance_sprinklered_mm"] = int(cp["max_travel_distance_sprinklered_mm"])

        # --- Pre-determine actual staircase count from travel distance compliance ---
        # Must happen before the width calculation so the occupancy load is divided
        # by the real number of stairs (central + any perimeter added for 60m rule).
        _fp_pts_pre = shell.get("footprint_points")
        if _fp_pts_pre and _fp_pts_pre[0] and isinstance(_fp_pts_pre[0][0], (list, tuple)):
            _fp_pts_pre = _fp_pts_pre[0]
        _pre_floor_dims = floor_dims  # _check_travel_distance uses footprint_pts polygon directly
        _f_center_pre = list(f_center_mm)  # use manifest-specified position

        # For multi-bank layouts, run the pre-check per bank and merge stair positions
        # so the travel distance test reflects the actual EW staircase placement.
        _banks_cfg_pre = lifts_config.get("banks", [])
        if _banks_cfg_pre:
            _all_sets_pre = []
            for _bk_p in _banks_cfg_pre:
                _bk_p_pos = _bk_p.get("position", [f_center_mm[0], f_center_mm[1]])
                _bk_p_cx, _bk_p_cy = float(_bk_p_pos[0]), float(_bk_p_pos[1])
                _bk_p_n = int(safe_num(_bk_p.get("count", max(1, num_lifts // max(len(_banks_cfg_pre), 1))), 1))
                _bk_p_orient = str(_bk_p.get("orientation", _core_orientation)).upper()
                if _bk_p_orient not in ("NS", "EW", "AUTO"):
                    _bk_p_orient = _core_orientation
                _bk_p_layout = lift_logic.get_total_core_layout(_bk_p_n, lobby_width=lobby_w)
                _bk_p_lcb = (
                    _bk_p_cx - _bk_p_layout["total_w"] / 2.0,
                    _bk_p_cy - _bk_p_layout["total_d"] / 2.0,
                    _bk_p_cx + _bk_p_layout["total_w"] / 2.0,
                    _bk_p_cy + _bk_p_layout["total_d"] / 2.0,
                )
                _bk_p_sets = fire_safety_logic.calculate_fire_safety_requirements(
                    _pre_floor_dims, [_bk_p_cx, _bk_p_cy], _bk_p_lcb,
                    typical_h_mm, preset_fs, _bk_p_n, lobby_w,
                    compliance_overrides=_cp_overrides,
                    footprint_pts=_fp_pts_pre,
                    footprint_holes=shell.get("footprint_holes", []),
                    orientation=_bk_p_orient,
                    num_banks=len(_banks_cfg_pre)
                )
                _all_sets_pre.extend(_bk_p_sets)
            _safety_sets_pre = _all_sets_pre
        else:
            _layout_pre = lift_logic.get_total_core_layout(num_lifts, lobby_width=lobby_w)
            _lift_bounds_pre = (
                _f_center_pre[0] - _layout_pre["total_w"] / 2.0,
                _f_center_pre[1] - _layout_pre["total_d"] / 2.0,
                _f_center_pre[0] + _layout_pre["total_w"] / 2.0,
                _f_center_pre[1] + _layout_pre["total_d"] / 2.0,
            )
            _safety_sets_pre = fire_safety_logic.calculate_fire_safety_requirements(
                _pre_floor_dims, _f_center_pre, _lift_bounds_pre,
                typical_h_mm, preset_fs, num_lifts, lobby_w,
                compliance_overrides=_cp_overrides,
                footprint_pts=_fp_pts_pre,
                footprint_holes=shell.get("footprint_holes", []),
                orientation=_core_orientation
            )
        _num_stairs_from_travel = max(len(_safety_sets_pre), 2)
        _num_stairs = _num_stairs_from_travel
        self.log("[StairCount] Travel-distance compliance requires {} staircases".format(_num_stairs_from_travel))

        # --- Staircase width calculation (SCDF Table 2.2A method) ---
        # Uses the largest floor plate so the worst-case occupant load governs.
        # Step a: functional space = largest floor plate - central core
        # Step b: total occupancy = functional_area_m2 / occupant_load_factor_m2
        # Step c: total unit widths needed = ceil(total_occupancy / persons_per_unit_width)
        # Step d: unit widths per stair = ceil(total_unit_widths / num_stairs)
        # Step e: required width = unit_widths_per_stair * exit_width_per_unit_mm
        #         then clamp to >= min_flight_width_mm from RAG
        import math as _math
        # Resolve the fire_safety nested block first (key name varies by Gemini output)
        _fs_cp_early = cp.get("fire_safety_scdf", cp.get("fire_safety", {}))
        _occ_nested  = _fs_cp_early.get("occupant_load", {})
        _exit_nested = _fs_cp_early.get("exit_width", {})
        _stair_nested = _fs_cp_early.get("staircase", {})

        # Flat key wins if present; otherwise fall back to nested path
        _occupant_load_factor = safe_num(cp.get("occupant_load_factor_m2"), 0) or \
                                safe_num(_occ_nested.get("occupant_load_factor_m2"), 0)
        _exit_width_per_unit  = safe_num(cp.get("exit_width_per_unit_mm"), 0) or \
                                safe_num(_exit_nested.get("exit_width_per_unit_mm"), 0)
        _min_flight_width     = safe_num(cp.get("stair_min_flight_width_mm"), 0) or \
                                safe_num(_stair_nested.get("min_flight_width_mm"), 0)
        _persons_per_unit     = safe_num(cp.get("persons_per_unit_width"), 0) or \
                                safe_num(_exit_nested.get("persons_per_unit_width"), 0)

        _calc_flight_width = None
        if _occupant_load_factor > 0 and _persons_per_unit > 0 and _exit_width_per_unit > 0 and floor_dims:
            # Largest floor plate (mm -> m2) — use actual polygon area when available
            if _fp_pts_early and len(_fp_pts_early) >= 3:
                _largest_area_mm2 = _poly_area_mm2(_fp_pts_early)
            else:
                _largest_area_mm2 = max(w * l for w, l in floor_dims)
            _largest_area_m2   = _largest_area_mm2 / 1e6
            # Core area using preset ratio midpoint
            _core_ratio        = sum(preset.get("program_requirements", {}).get("core_area_ratio", [0.20, 0.25])) / 2.0
            _functional_area   = _largest_area_m2 * (1.0 - _core_ratio)
            # Occupancy
            _total_occupancy   = _functional_area / _occupant_load_factor
            # Required unit widths across all stairs
            _total_unit_widths = _math.ceil(_total_occupancy / _persons_per_unit)
            # Split across stairs
            _units_per_stair   = _math.ceil(_total_unit_widths / max(int(_num_stairs), 1))
            _calc_flight_width = int(_units_per_stair * _exit_width_per_unit)
            self.log(
                "[StairWidth] largest_floor={:.0f}m2 core_ratio={:.0%} functional={:.0f}m2 "
                "occupants={:.0f} unit_widths={} stairs={} units/stair={} calc_width={}mm".format(
                    _largest_area_m2, _core_ratio, _functional_area, _total_occupancy,
                    _total_unit_widths, int(_num_stairs), _units_per_stair, _calc_flight_width
                )
            )
        else:
            self.log("[StairWidth] Skipping calculation — RAG missing occupant_load_factor={} "
                     "persons_per_unit={} exit_width_per_unit={}".format(
                         _occupant_load_factor, _persons_per_unit, _exit_width_per_unit))

        # Final flight width: max(calculated, RAG minimum, absolute code floor 1000mm).
        # Practical cap at 1800mm — single-flight widths beyond this are unbuildable;
        # excess egress demand is met by adding more staircases instead.
        _abs_min = 1200  # BS 9999 recommended minimum for fire-escape stairs
        _max_flight_w = 1800
        _final_flight_width = _abs_min
        if _calc_flight_width:
            _final_flight_width = max(_calc_flight_width, int(_min_flight_width) if _min_flight_width else _abs_min, _abs_min)
        elif _min_flight_width:
            _final_flight_width = max(int(_min_flight_width), _abs_min)
        elif cp.get("stair_flight_width_mm"):
            _final_flight_width = max(int(cp["stair_flight_width_mm"]), _abs_min)
        if _final_flight_width > _max_flight_w:
            # Redistribute excess across more stairs rather than widening beyond practical limit
            if _calc_flight_width and _occupant_load_factor > 0 and _persons_per_unit > 0 and _exit_width_per_unit > 0:
                _req_stairs = max(_math.ceil(_total_unit_widths * _exit_width_per_unit / _max_flight_w), int(_num_stairs))
                if _req_stairs > int(_num_stairs):
                    _num_stairs_from_travel = _req_stairs
                    _num_stairs = _req_stairs
                    _units_per_stair = _math.ceil(_total_unit_widths / _num_stairs)
                    _final_flight_width = int(_units_per_stair * _exit_width_per_unit)
                    self.log("[StairWidth] Width capped: {} stairs added to keep flight ≤{}mm".format(
                        _req_stairs, _max_flight_w))
            _final_flight_width = min(_final_flight_width, _max_flight_w)

        stair_spec["width_of_flight"] = _final_flight_width
        _cp_overrides["min_flight_width_mm"] = _final_flight_width
        self.log("[StairWidth] Final flight width: {}mm (calc={}, rag_min={}, preset={})".format(
            _final_flight_width, _calc_flight_width, _min_flight_width,
            preset.get("core_logic", {}).get("fire_safety", {}).get("staircase_spec", {}).get("width_of_flight")))

        # Landing width: use preset value if set, otherwise flight_width (U-turn minimum).
        # The preset already encodes the correct landing for the typology.
        _landing_width = stair_spec.get("landing_width") or _final_flight_width
        stair_spec["landing_width"] = _landing_width
        _cp_overrides["min_landing_width_mm"] = _landing_width

        # Fire lift and lobby — flat keys (old format Gemini used to emit)
        if cp.get("fire_lift_car_size_mm"):
            _cp_overrides["fire_lift_car_size_mm"] = int(cp["fire_lift_car_size_mm"])
        if cp.get("fire_lobby_min_area_mm2"):
            _cp_overrides["fire_lobby_min_area_mm2"] = int(cp["fire_lobby_min_area_mm2"])
        if cp.get("fire_lobby_min_depth_mm"):
            _cp_overrides["fire_lobby_min_depth_mm"] = int(cp["fire_lobby_min_depth_mm"])
        if cp.get("smoke_lobby_min_area_mm2"):
            _cp_overrides["smoke_lobby_min_area_mm2"] = int(cp["smoke_lobby_min_area_mm2"])
        if cp.get("smoke_lobby_min_depth_mm"):
            _cp_overrides["smoke_lobby_min_depth_mm"] = int(cp["smoke_lobby_min_depth_mm"])
        if cp.get("min_corridor_width_mm"):
            _cp_overrides["min_corridor_width_mm"] = int(cp["min_corridor_width_mm"])

        # Fire lift and lobby — nested keys (Gemini currently emits compliance_parameters
        # as a nested object: fire_safety.fire_lift.car_size_mm etc.).
        # Only apply if the flat key wasn't already set.
        _fs_cp = cp.get("fire_safety_scdf", cp.get("fire_safety", {}))
        _fl_cp = _fs_cp.get("fire_lift", {})
        _fl_lb_cp = _fs_cp.get("fire_lift_lobby", {})
        _sm_lb_cp = _fs_cp.get("smoke_stop_lobby", {})
        _cor_cp = _fs_cp.get("corridor", {})
        if not _cp_overrides.get("fire_lift_car_size_mm") and _fl_cp.get("car_size_mm"):
            _cp_overrides["fire_lift_car_size_mm"] = int(_fl_cp["car_size_mm"])
        if not _cp_overrides.get("fire_lobby_min_area_mm2") and _fl_lb_cp.get("min_area_mm2"):
            _cp_overrides["fire_lobby_min_area_mm2"] = int(_fl_lb_cp["min_area_mm2"])
        if not _cp_overrides.get("fire_lobby_min_width_mm") and _fl_lb_cp.get("min_width_mm"):
            _cp_overrides["fire_lobby_min_width_mm"] = int(_fl_lb_cp["min_width_mm"])
        if not _cp_overrides.get("fire_lobby_min_depth_mm") and _fl_lb_cp.get("min_depth_mm"):
            _cp_overrides["fire_lobby_min_depth_mm"] = int(_fl_lb_cp["min_depth_mm"])
        if not _cp_overrides.get("smoke_lobby_min_area_mm2") and _sm_lb_cp.get("min_area_mm2"):
            _cp_overrides["smoke_lobby_min_area_mm2"] = int(_sm_lb_cp["min_area_mm2"])
        if not _cp_overrides.get("smoke_lobby_min_depth_mm") and _sm_lb_cp.get("min_depth_mm"):
            _cp_overrides["smoke_lobby_min_depth_mm"] = int(_sm_lb_cp["min_depth_mm"])
        if not _cp_overrides.get("min_corridor_width_mm") and _cor_cp.get("min_corridor_width_mm"):
            _cp_overrides["min_corridor_width_mm"] = int(_cor_cp["min_corridor_width_mm"])

        if _cp_overrides:
            self.log("[ComplianceOverride] Applying {} RAG-derived value(s) to engine: {}".format(
                len(_cp_overrides), list(_cp_overrides.keys())))

        self._stair_spec = stair_spec
        self.log("[StairSpec] FINAL stair_spec: riser={} tread={} flight_w={} landing={} "
                 "entry_landing={} num_risers={}".format(
            stair_spec.get("riser", 150),
            stair_spec.get("tread", 280),
            stair_spec.get("width_of_flight", "?"),
            stair_spec.get("landing_width", "?"),
            stair_spec.get("entry_landing_width", "?"),
            stair_spec.get("num_risers", "?")))

        # 4. Calculate Unified Layout
        # f_center_mm and _core_orientation were read from lifts_config above (step 2)

        # When the shell uses footprint_offset_overrides (e.g. S-shape), the building
        # centroid shifts per floor.  Staircase positions must lie within the building
        # at the MOST-OFFSET floor, not just the nominal centre position.
        # Shrink the effective floor dims by 2× the maximum offset so that perimeter
        # stair candidates never land outside the building on any floor.
        _stair_floor_dims = floor_dims  # _check_travel_distance uses footprint_pts polygon directly
        _fp_pts = shell.get("footprint_points")
        # Normalise: Gemini may send [[outer_polygon], [hole]] instead of flat [[x,y],...].
        if _fp_pts and _fp_pts[0] and isinstance(_fp_pts[0][0], (list, tuple)):
            _fp_pts = _fp_pts[0]
        _offset_ovr = shell.get("footprint_offset_overrides", {})
        if _offset_ovr:
            try:
                _ox_vals = [abs(float(v[0])) for v in _offset_ovr.values() if isinstance(v, (list, tuple)) and len(v) >= 2]
                _oy_vals = [abs(float(v[1])) for v in _offset_ovr.values() if isinstance(v, (list, tuple)) and len(v) >= 2]
                _max_ox = max(_ox_vals) if _ox_vals else 0
                _max_oy = max(_oy_vals) if _oy_vals else 0
                if _max_ox > 0 or _max_oy > 0:
                    _stair_floor_dims = [
                        (max(w - 2.0 * _max_ox, w * 0.4), max(l - 2.0 * _max_oy, l * 0.4))
                        for w, l in floor_dims
                    ]
                    self.log("[Core] Offset override detected (max ±{:.0f},{:.0f}mm) — shrinking stair zone to {}×{}mm".format(
                        _max_ox, _max_oy,
                        int(_stair_floor_dims[0][0]) if _stair_floor_dims else 0,
                        int(_stair_floor_dims[0][1]) if _stair_floor_dims else 0,
                    ))
            except Exception:
                pass

        # Determine passenger lift bounds first for anchor
        layout_lifts = lift_logic.get_total_core_layout(num_lifts, lobby_width=lobby_w)
        lift_core_bounds_mm = (
            f_center_mm[0] - layout_lifts["total_w"]/2,
            f_center_mm[1] - layout_lifts["total_d"]/2,
            f_center_mm[0] + layout_lifts["total_w"]/2,
            f_center_mm[1] + layout_lifts["total_d"]/2
        )
        self.log("[CoreSetup] Single-bank: center=({:.0f},{:.0f}) num_lifts={} lobby_w={:.0f} "
                 "lcb=({:.0f},{:.0f},{:.0f},{:.0f}) lcb_w={:.0f} lcb_d={:.0f} orient={}".format(
            float(f_center_mm[0]), float(f_center_mm[1]),
            num_lifts, float(lobby_w),
            float(lift_core_bounds_mm[0]), float(lift_core_bounds_mm[1]),
            float(lift_core_bounds_mm[2]), float(lift_core_bounds_mm[3]),
            float(lift_core_bounds_mm[2] - lift_core_bounds_mm[0]),
            float(lift_core_bounds_mm[3] - lift_core_bounds_mm[1]),
            _core_orientation))

        safety_sets = fire_safety_logic.calculate_fire_safety_requirements(
            _stair_floor_dims, f_center_mm, lift_core_bounds_mm,
            typical_h_mm, preset_fs, num_lifts, lobby_w,
            compliance_overrides=_cp_overrides,
            footprint_pts=_fp_pts,
            footprint_holes=shell.get("footprint_holes", []),
            orientation=_core_orientation
        )
        self.log("[CoreSetup] safety_sets returned: {} sets: {}".format(
            len(safety_sets),
            [(s["type"], "({:.0f},{:.0f})".format(float(s["pos"][0]), float(s["pos"][1])),
              "perim={}".format(s.get("is_perimeter", False))) for s in safety_sets]))

        # For organic offset buildings (S-shape etc), drop any perimeter SMOKE_STOP
        # sets that fire_safety_logic may have added to satisfy the 60m travel rule.
        # Those sets are placed relative to the nominal floor boundary — at heavily
        # offset floors they land outside the building envelope, causing both spatial
        # conflicts and exposed cores.  The 2 core-adjacent fire sets always satisfy
        # travel distance for buildings ≤ ~80m in plan (max half-diagonal < 60m).
        if _offset_ovr:
            _before = len(safety_sets)
            safety_sets = [s for s in safety_sets if not s.get("is_perimeter", False)]
            if len(safety_sets) < _before:
                self.log("[Core] Organic offset: dropped {} perimeter stair set(s) — using {} core-adjacent set(s)".format(
                    _before - len(safety_sets), len(safety_sets)))

        # Cluster directive override: Gemini specifies which sides get fire clusters
        # and the arrangement / staircase_rotation per cluster.  Engine computes all
        # exact coordinates from side + lift_core_bounds_mm — no polygon arithmetic
        # by Gemini, which eliminates the root cause of spatial coordinate errors.
        _clusters = lifts_config.get("clusters", [])
        if _clusters:
            l_xmin_c, l_ymin_c, l_xmax_c, l_ymax_c = lift_core_bounds_mm
            _cluster_sets = []
            for _cl in _clusters:
                _side = str(_cl.get("side", "south")).lower()
                if _side == "south":
                    _entry = (f_center_mm[0], l_ymin_c)
                elif _side == "north":
                    _entry = (f_center_mm[0], l_ymax_c)
                elif _side == "east":
                    _entry = (l_xmax_c, f_center_mm[1])
                else:  # west
                    _entry = (l_xmin_c, f_center_mm[1])
                _cs = {"pos": _entry, "type": "FIRE_LIFT"}
                if _cl.get("arrangement") is not None:
                    _cs["arrangement"] = str(_cl["arrangement"]).lower()
                if _cl.get("staircase_rotation") is not None:
                    try:
                        _cs["staircase_rotation"] = float(_cl["staircase_rotation"])
                    except (TypeError, ValueError):
                        pass
                _cluster_sets.append(_cs)
            safety_sets = _cluster_sets
            self.log("[Core] Cluster directives: {} set(s) from lifts.clusters".format(len(_cluster_sets)))

        # 5. Build Manifest Data
        levels_manifest = []
        for i, elev_ft in enumerate(elevations):
            levels_manifest.append({"id": "AI Level {}".format(i+1), "elevation": elev_ft * 304.8})

        # ── Phase 2: core_layout path ──────────────────────────────────────────
        # If manifest contains core_layout.elements, Gemini specifies all spatial
        # decisions. Engine validates RAG minimums, runs E1 polygon check, generates
        # geometry from explicit footprints, and returns.  The existing
        # calculate_fire_safety_requirements pipeline is skipped.
        _core_layout = manifest.get("core_layout", {})
        _cl_elements = _core_layout.get("elements", []) if _core_layout else []
        if _cl_elements:
            _fl_car     = int(_cp_overrides.get("fire_lift_car_size_mm", 2500))
            _wall_t_cl  = 350
            _fl_shaft_min   = _fl_car + 2 * _wall_t_cl
            _fl_lb_area_min = int(_cp_overrides.get("fire_lobby_min_area_mm2", 6000000))
            _fl_lb_dep_min  = int(_cp_overrides.get("fire_lobby_min_depth_mm", 2000))
            _sm_area_min    = int(_cp_overrides.get("smoke_lobby_min_area_mm2", 4000000))
            _sm_dep_min     = int(_cp_overrides.get("smoke_lobby_min_depth_mm", 2000))

            for _el in _cl_elements:
                _el_type = _el.get("type", "")
                _el_id   = _el.get("id", "?")
                if _el_type == "staircase":
                    continue
                _fp_el = _el.get("footprint", [])
                if len(_fp_el) < 3:
                    continue
                _area_el  = _poly_area_mm2(_fp_el)
                _xs_el    = [p[0] for p in _fp_el]
                _ys_el    = [p[1] for p in _fp_el]
                _min_dim_el = min(max(_xs_el) - min(_xs_el), max(_ys_el) - min(_ys_el))
                if _el_type == "fire_lift_shaft":
                    if _area_el < _fl_shaft_min ** 2 or _min_dim_el < _fl_shaft_min:
                        return {"status": "CONFLICT", "description":
                            "Element '{}' (fire_lift_shaft) fails RAG minimum: "
                            "area={:.0f}mm2 (min={:.0f}), min_dim={:.0f}mm (min={}). "
                            "Widen the footprint to at least {}x{}mm.".format(
                                _el_id, _area_el, _fl_shaft_min**2, _min_dim_el, _fl_shaft_min,
                                _fl_shaft_min, _fl_shaft_min)}
                elif _el_type == "fire_lift_lobby":
                    if _area_el < _fl_lb_area_min or _min_dim_el < _fl_lb_dep_min:
                        return {"status": "CONFLICT", "description":
                            "Element '{}' (fire_lift_lobby) fails RAG minimum: "
                            "area={:.0f}mm2 (min={}), min_dim={:.0f}mm (min={}). "
                            "Enlarge the footprint.".format(
                                _el_id, _area_el, _fl_lb_area_min, _min_dim_el, _fl_lb_dep_min)}
                elif _el_type == "smoke_stop_lobby":
                    if _area_el < _sm_area_min or _min_dim_el < _sm_dep_min:
                        return {"status": "CONFLICT", "description":
                            "Element '{}' (smoke_stop_lobby) fails RAG minimum: "
                            "area={:.0f}mm2 (min={}), min_dim={:.0f}mm (min={}). "
                            "Enlarge the footprint.".format(
                                _el_id, _area_el, _sm_area_min, _min_dim_el, _sm_dep_min)}

            # E1: all element polygons must lie within the floor plate polygon
            _fp_pts_cl = shell.get("footprint_points")
            if _fp_pts_cl and len(_fp_pts_cl) >= 3:
                from revit_mcp.staircase_logic import _point_in_polygon as _pip_cl
                _fp_holes_cl = shell.get("footprint_holes", [])
                _outside_cl = []
                for _el in _cl_elements:
                    _el_fp = _el.get("footprint") or []
                    if not _el_fp:
                        _c = _el.get("center", [0, 0])
                        _sw_cl = staircase_logic.get_shaft_dimensions(typical_h_mm, stair_spec)[0]
                        _sd_cl = staircase_logic.get_max_shaft_depth(levels_manifest, stair_spec, typical_h_mm)
                        _el_fp = [[_c[0]-_sw_cl/2, _c[1]-_sd_cl/2], [_c[0]+_sw_cl/2, _c[1]-_sd_cl/2],
                                  [_c[0]+_sw_cl/2, _c[1]+_sd_cl/2], [_c[0]-_sw_cl/2, _c[1]+_sd_cl/2]]
                    for _pt in _el_fp:
                        if not _pip_cl(_pt[0], _pt[1], _fp_pts_cl) or \
                                any(_pip_cl(_pt[0], _pt[1], _h) for _h in _fp_holes_cl):
                            _outside_cl.append(_el.get("id", "?"))
                            break
                if _outside_cl:
                    return {"status": "CONFLICT", "description":
                        "core_layout element(s) {} have vertices outside the floor plate polygon. "
                        "Reposition all elements to lie fully within footprint_points and not inside "
                        "any footprint_holes.".format(", ".join("'{}'".format(e) for e in _outside_cl))}

            cl_man = fire_safety_logic.generate_core_layout_manifest(
                _cl_elements, levels_manifest, stair_spec, typical_h_mm, _cp_overrides,
                num_lifts=num_lifts, lobby_width=lobby_w)

            self._door_data = list(cl_man.get("door_specs", []))

            sub_bounds_cl = cl_man.get("sub_boundaries", [])
            _excl = []
            for _sb in sub_bounds_cl:
                _r = _sb["rect"]
                _excl.append((mm_to_ft(_r[0]), mm_to_ft(_r[1]), mm_to_ft(_r[2]), mm_to_ft(_r[3])))
                self.spatial_registry.reserve(_sb["id"], (_r[0], _r[1], 0, _r[2], _r[3], typical_h_mm))
            self._core_exclusion_zones = _excl
            if _excl:
                self._lift_core_bounds_ft = _excl[0]

            if "walls" not in manifest: manifest["walls"] = []
            if "floors" not in manifest: manifest["floors"] = []
            manifest["walls"].extend(cl_man.get("walls", []))
            manifest["floors"].extend(cl_man.get("floors", []))

            u_voids_ft = [(mm_to_ft(v[0]), mm_to_ft(v[1]), mm_to_ft(v[2]), mm_to_ft(v[3]))
                          for v in cl_man.get("voids", [])]
            self._stair_voids = u_voids_ft
            self._stair_void_polygons = []

            _stair_els = [e for e in _cl_elements if e.get("type") == "staircase"]
            _stair_pos = [(float(e["center"][0]), float(e["center"][1])) for e in _stair_els]
            _sd_nat_cl = staircase_logic.get_max_shaft_depth(levels_manifest, stair_spec, typical_h_mm)
            _stair_base_ys_cl = [float(e["center"][1]) - _sd_nat_cl / 2.0 for e in _stair_els]
            _shaft_w_cl = staircase_logic.get_shaft_dimensions(typical_h_mm, stair_spec)[0]
            self._stair_run_data = staircase_logic.get_stair_run_data(
                _stair_pos, levels_manifest, _shaft_w_cl, stair_spec, typical_h_mm,
                lift_core_bounds_mm=None, floor_dims_mm=floor_dims, num_lifts=0, lobby_width=lobby_w,
                rotated_indices=[], base_y_overrides=(_stair_base_ys_cl if _stair_base_ys_cl else None)
            ) if _stair_pos else []

            self._stair_summary = {
                "count": len(_stair_pos),
                "riser": stair_spec.get("riser", 150),
                "tread": stair_spec.get("tread", 280),
                "typical_h": typical_h_mm,
                "flights_typical": staircase_logic._calc_num_flights(typical_h_mm, typical_h_mm, 150),
                "risers_per_flight": staircase_logic._risers_per_flight_typical(typical_h_mm, 150)
            }

            _bounds_ft_cl = None
            for _sb in sub_bounds_cl:
                _r = _sb["rect"]
                _cb = (mm_to_ft(_r[0]), mm_to_ft(_r[1]), mm_to_ft(_r[2]), mm_to_ft(_r[3]))
                _bounds_ft_cl = _cb if _bounds_ft_cl is None else (
                    min(_bounds_ft_cl[0], _cb[0]), min(_bounds_ft_cl[1], _cb[1]),
                    max(_bounds_ft_cl[2], _cb[2]), max(_bounds_ft_cl[3], _cb[3]))
            return _bounds_ft_cl
        # ── End Phase 2 core_layout path ───────────────────────────────────────

        # ── Multi-bank path ────────────────────────────────────────────────────
        # lifts.banks: list of independent passenger lift banks, each with its
        # own position, count, rotation_deg, and optional clusters.
        # If present, each bank gets its own fire cluster; results are merged.
        # Backward compat: if banks absent, fall through to single-bank path.
        _banks_cfg = lifts_config.get("banks", [])
        if _banks_cfg:
            import math as _bkm
            _bk_all_voids_ft = []
            _bk_all_void_polys = []
            _bk_all_door_specs = []
            _bk_all_stair_run_data = []
            _bk_all_stair_centers_count = 0
            _bk_bounds_ft = None
            _bk_first_excl = None
            _per_bank_walls = []        # collected per-bank for Fix-4 alignment
            _per_bank_floors = []       # per-bank floors (deferred for alignment)
            _per_bank_voids_mm = []     # per-bank voids in mm (deferred for alignment)
            _per_bank_door_specs = []   # per-bank door specs (deferred for alignment)
            _per_bank_stair_sc = []     # per-bank stair_centers tuples (deferred for alignment)
            _per_bank_oc_list = []      # ortools_conflict dicts from each bank (for CONFLICT bubble-up)

            if "walls" not in manifest: manifest["walls"] = []
            if "floors" not in manifest: manifest["floors"] = []

            for _bk_idx, _bk in enumerate(_banks_cfg):
                _bk_pos = _bk.get("position", [0, 0])
                _bk_cx = float(_bk_pos[0])
                _bk_cy = float(_bk_pos[1])
                # Apply SVG re-centring offset to each bank position (same as single-bank path)
                _svg_off_bk = manifest.get("shell", {}).get("_svg_offset")
                if _svg_off_bk and (_bk_cx != 0.0 or _bk_cy != 0.0):
                    _bk_cx -= float(_svg_off_bk[0])
                    _bk_cy -= float(_svg_off_bk[1])
                    self.log("[SVG] Bank {} position adjusted by offset ({:.0f},{:.0f}) -> ({:.0f},{:.0f})".format(
                        _bk_idx, float(_svg_off_bk[0]), float(_svg_off_bk[1]), _bk_cx, _bk_cy))
                _bk_rot = float(_bk.get("rotation_deg", 0.0))
                _bk_n = int(safe_num(_bk.get("count", max(1, num_lifts // max(len(_banks_cfg), 1))), 1))
                _bk_clusters = _bk.get("clusters", [])
                # Per-bank orientation: bank can override the top-level orientation
                _bk_raw_orient = str(_bk.get("orientation", _core_orientation)).upper()
                if _bk_raw_orient not in ("NS", "EW", "AUTO"):
                    _bk_raw_orient = _core_orientation
                _bk_orientation = _bk_raw_orient

                _bk_layout = lift_logic.get_total_core_layout(_bk_n, lobby_width=lobby_w)
                _bk_lcb = (
                    _bk_cx - _bk_layout["total_w"] / 2.0,
                    _bk_cy - _bk_layout["total_d"] / 2.0,
                    _bk_cx + _bk_layout["total_w"] / 2.0,
                    _bk_cy + _bk_layout["total_d"] / 2.0,
                )
                self.log("[BankSetup] Bank {}: center=({:.0f},{:.0f}) n={} orient={} "
                         "lcb=({:.0f},{:.0f},{:.0f},{:.0f}) lcb_w={:.0f} lcb_d={:.0f} "
                         "total_w={:.0f} total_d={:.0f}".format(
                    _bk_idx, _bk_cx, _bk_cy, _bk_n, _bk_orientation,
                    _bk_lcb[0], _bk_lcb[1], _bk_lcb[2], _bk_lcb[3],
                    _bk_lcb[2] - _bk_lcb[0], _bk_lcb[3] - _bk_lcb[1],
                    float(_bk_layout["total_w"]), float(_bk_layout["total_d"])))

                _bk_sets = fire_safety_logic.calculate_fire_safety_requirements(
                    _stair_floor_dims, [_bk_cx, _bk_cy], _bk_lcb,
                    typical_h_mm, preset_fs, _bk_n, lobby_w,
                    compliance_overrides=_cp_overrides,
                    footprint_pts=_fp_pts,
                    footprint_holes=shell.get("footprint_holes", []),
                    orientation=_bk_orientation,
                    num_banks=len(_banks_cfg)
                )
                if _offset_ovr:
                    _bk_sets = [s for s in _bk_sets if not s.get("is_perimeter", False)]
                self.log("[BankSetup] Bank {} sets ({}): {}".format(
                    _bk_idx, len(_bk_sets),
                    [(s["type"], "({:.0f},{:.0f})".format(float(s["pos"][0]), float(s["pos"][1])),
                      "perim={}".format(s.get("is_perimeter", False))) for s in _bk_sets]))

                # Cluster directive override per bank
                # For EW banks: Gemini often sends NS sides ("south"/"north") which is
                # incorrect — EW clusters must be at "east"/"west". If all provided
                # cluster sides are NS-only for an EW bank, ignore the directive and
                # use the engine-computed sets (which already place fire lifts EW).
                _ew_bank = (_bk_orientation == "EW")
                _cluster_has_ew = any(
                    str(_cl.get("side", "")).lower() in ("east", "west")
                    for _cl in _bk_clusters
                ) if _bk_clusters else False
                _skip_cluster_override = _ew_bank and _bk_clusters and not _cluster_has_ew
                if _bk_clusters and not _skip_cluster_override:
                    l_xmin_b, l_ymin_b, l_xmax_b, l_ymax_b = _bk_lcb
                    _bk_cs = []
                    for _cl in _bk_clusters:
                        _side = str(_cl.get("side", "south")).lower()
                        if _side == "south":
                            _e = (_bk_cx, l_ymin_b)
                        elif _side == "north":
                            _e = (_bk_cx, l_ymax_b)
                        elif _side == "east":
                            _e = (l_xmax_b, _bk_cy)
                        else:
                            _e = (l_xmin_b, _bk_cy)
                        _d = {"pos": _e, "type": "FIRE_LIFT"}
                        if _cl.get("arrangement") is not None:
                            _d["arrangement"] = str(_cl["arrangement"]).lower()
                        if _cl.get("staircase_rotation") is not None:
                            try:
                                _d["staircase_rotation"] = float(_cl["staircase_rotation"])
                            except (TypeError, ValueError):
                                pass
                        _bk_cs.append(_d)
                    _bk_sets = _bk_cs
                elif _skip_cluster_override:
                    self.log("[MultiBank] Bank {}: EW bank with NS-only clusters — ignoring cluster directive, using engine-computed EW sets ({} sets).".format(
                        _bk_idx, len(_bk_sets)))

                # topology_graph may be on the bank dict OR on the parent lifts_config
                _bk_topo = _bk.get("topology_graph") or lifts_config.get("topology_graph")
                self.log("[MultiBank] Bank {}: {} lifts @ ({:.0f},{:.0f}) orient={} rot={:.1f}deg, {} cluster(s), topology_graph={}".format(
                    _bk_idx, _bk_n, _bk_cx, _bk_cy, _bk_orientation, _bk_rot, len(_bk_sets),
                    "YES ({} rules)".format(len(_bk_topo)) if _bk_topo else "NO"))

                # Always pass the bank dict to the solver so auto-inject can fire.
                # When topology_graph is absent, generate_fire_safety_manifest
                # injects the standard topology from orientation automatically.
                _bk_for_solver = dict(_bk)
                if _bk_topo:
                    _bk_for_solver["topology_graph"] = _bk_topo

                _bk_fs = fire_safety_logic.generate_fire_safety_manifest(
                    _bk_sets, levels_manifest, stair_spec, typical_h_mm, preset_fs,
                    _bk_lcb, _bk_n, lobby_w,
                    compliance_overrides=_cp_overrides,
                    footprint_pts=shell.get("footprint_points"),
                    footprint_holes=_normalise_footprint_holes(shell.get("footprint_holes", [])),
                    all_floor_dims=floor_dims,
                    manifest_lifts=_bk_for_solver,
                    center_pos=(_bk_cx, _bk_cy),
                )
                if isinstance(_bk_fs, dict) and _bk_fs.get("status") == "CONFLICT":
                    self.log("[BankSetup] Bank {} generate_fire_safety_manifest → CONFLICT: {}".format(
                        _bk_idx, _bk_fs.get("description", "")))
                    _bk_fs.setdefault("manifest_inputs", {
                        "lift_count": _bk_n,
                        "bank_index": _bk_idx,
                        "shell_width_mm": round(safe_num(manifest.get("shell", {}).get("width", 0), 0)),
                        "shell_length_mm": round(safe_num(manifest.get("shell", {}).get("length", 0), 0)),
                        "level_height_mm": round(typical_h_mm),
                    })
                    # Promote ortools_conflict.failure_details → ortools_failure_details so
                    # dispatcher.py can include per-bank clearance info in the Gemini retry prompt.
                    _oc = _bk_fs.get("ortools_conflict", {}) or {}
                    if _oc.get("failure_details") and "ortools_failure_details" not in _bk_fs:
                        _bk_fs["ortools_failure_details"] = _oc["failure_details"]
                    return _bk_fs
                self.log("[BankSetup] Bank {} manifest: walls={} floors={} door_specs={} voids={}".format(
                    _bk_idx,
                    len(_bk_fs.get("walls", [])),
                    len(_bk_fs.get("floors", [])),
                    len(_bk_fs.get("door_specs", [])),
                    len(_bk_fs.get("voids", []))))

                # Fix B: prefix all element IDs so each bank's walls have unique Revit IDs.
                # Both banks follow the single-bank path in fire_safety_logic which emits
                # identical IDs (e.g. AI_FireLobby_North__Level_1__W_N). Without a prefix
                # the sync engine treats them as the same element and moves rather than
                # creates, leaving only one bank with visible walls.
                _bk_pfx = "B{}_".format(_bk_idx)
                for _w in _bk_fs.get("walls", []):
                    if _w.get("id", "").startswith("AI_"):
                        _w["id"] = "AI_" + _bk_pfx + _w["id"][3:]
                for _f in _bk_fs.get("floors", []):
                    if _f.get("id", "").startswith("AI_"):
                        _f["id"] = "AI_" + _bk_pfx + _f["id"][3:]
                for _ds in _bk_fs.get("door_specs", []):
                    # Prefix door id so each bank's doors are unique — avoids duplicate
                    # placement when two banks have the same safety-set tag (e.g. SafetySet_1).
                    if _ds.get("id"):
                        _ds["id"] = _bk_pfx + _ds["id"]
                    if _ds.get("wall_ai_id", "").startswith("AI_"):
                        _ds["wall_ai_id"] = "AI_" + _bk_pfx + _ds["wall_ai_id"][3:]
                    _aiid_map = _ds.get("wall_ai_id_map")
                    if _aiid_map:
                        _ds["wall_ai_id_map"] = {
                            _lvl: ("AI_" + _bk_pfx + _v[3:] if _v.startswith("AI_") else _v)
                            for _lvl, _v in _aiid_map.items()
                        }

                _bk_walls = list(_bk_fs.get("walls", []))
                _bk_floors = list(_bk_fs.get("floors", []))

                # Add passenger-lift shaft walls when the hardcoded path ran.
                # The generative solver path generates passenger-lift walls internally via
                # generate_lift_shaft_from_polygon, so only add them here when no topology
                # graph was present (hardcoded path).  EW banks now always use the hardcoded
                # path regardless of _bk_for_solver, so use _bk_topo as the indicator.
                # flip_rows: smaller lift row goes to whichever side the staircase is on.
                # fire_set_attach_sides[0] is the primary cluster's attach side.
                # side=S → staircase below → smaller row south → flip_rows=True
                # side=N → staircase above → smaller row north → flip_rows=False
                _bk_attach_sides = _bk_fs.get("fire_set_attach_sides", [])
                _bk_flip = bool(_bk_attach_sides and _bk_attach_sides[0] == "S")
                self.log("[MultiBank] Bank {}: primary attach_side={} flip_rows={}".format(
                    _bk_idx,
                    _bk_attach_sides[0] if _bk_attach_sides else "?",
                    _bk_flip))

                if not _bk_topo:
                    try:
                        _bk_psgr = lift_logic.generate_lift_shaft_manifest(
                            _bk_n, levels_manifest,
                            center_pos=(_bk_cx, _bk_cy),
                            lobby_width=lobby_w,
                            flip_rows=_bk_flip,
                        )
                        for _w in _bk_psgr.get("walls", []):
                            if _w.get("id", "").startswith("AI_"):
                                _w["id"] = "AI_" + _bk_pfx + _w["id"][3:]
                        for _f in _bk_psgr.get("floors", []):
                            if _f.get("id", "").startswith("AI_"):
                                _f["id"] = "AI_" + _bk_pfx + _f["id"][3:]
                        _bk_walls.extend(_bk_psgr.get("walls", []))
                        _bk_floors.extend(_bk_psgr.get("floors", []))
                    except Exception as _bk_pe:
                        self.log("[MultiBank] Bank {} psgr shaft failed (non-fatal): {}".format(_bk_idx, _bk_pe))

                _bk_voids_mm = list(_bk_fs.get("voids", []))
                try:
                    _bk_voids_mm.extend(lift_logic.get_shaft_void_rectangles_mm(
                        _bk_n, center_pos=(_bk_cx, _bk_cy), lobby_width=lobby_w,
                        flip_rows=_bk_flip))
                except Exception as _bk_ve:
                    self.log("[MultiBank] Bank {} void rects failed (non-fatal): {}".format(_bk_idx, _bk_ve))

                # Apply per-bank rotation
                if _bk_rot:
                    _bk_rad = _bkm.radians(_bk_rot)
                    _bk_cos = _bkm.cos(_bk_rad)
                    _bk_sin = _bkm.sin(_bk_rad)

                    def _bk_rot_mm(x, y, cx=_bk_cx, cy=_bk_cy, c=_bk_cos, s=_bk_sin):
                        dx = x - cx; dy = y - cy
                        return (cx + dx * c - dy * s, cy + dx * s + dy * c)

                    for _w in _bk_walls:
                        if "start" in _w:
                            _nx, _ny = _bk_rot_mm(_w["start"][0], _w["start"][1])
                            _w["start"] = [_nx, _ny, _w["start"][2]]
                        if "end" in _w:
                            _nx, _ny = _bk_rot_mm(_w["end"][0], _w["end"][1])
                            _w["end"] = [_nx, _ny, _w["end"][2]]

                    for _f in _bk_floors:
                        if "points" in _f:
                            _f["points"] = [list(_bk_rot_mm(p[0], p[1])) for p in _f["points"]]

                    _bk_cx_ft = mm_to_ft(_bk_cx)
                    _bk_cy_ft = mm_to_ft(_bk_cy)
                    def _bk_rot_ft(x, y, cx=_bk_cx_ft, cy=_bk_cy_ft, c=_bk_cos, s=_bk_sin):
                        dx = x - cx; dy = y - cy
                        return (cx + dx * c - dy * s, cy + dx * s + dy * c)
                    # Also rotate the voids in mm so BankAlign shift (if any) is applied before ft conversion
                    _bk_voids_mm_rotated = []
                    for (vx1, vy1, vx2, vy2) in _bk_voids_mm:
                        rx1, ry1 = _bk_rot_ft(mm_to_ft(vx1), mm_to_ft(vy1))
                        rx2, ry2 = _bk_rot_ft(mm_to_ft(vx2), mm_to_ft(vy1))
                        rx3, ry3 = _bk_rot_ft(mm_to_ft(vx2), mm_to_ft(vy2))
                        rx4, ry4 = _bk_rot_ft(mm_to_ft(vx1), mm_to_ft(vy2))
                        _bk_voids_mm_rotated.append(("rotated_ft", rx1, ry1, rx2, ry2, rx3, ry3, rx4, ry4))
                    _bk_voids_mm = _bk_voids_mm_rotated

                _per_bank_walls.append(list(_bk_walls))
                _per_bank_floors.append(list(_bk_floors))

                _bk_door_specs_this = list(_bk_fs.get("door_specs", []))
                try:
                    _pax_door_raw = lift_logic.get_passenger_lift_door_positions(
                        _bk_n, center_pos=(_bk_cx, _bk_cy),
                        lobby_width=lobby_w, levels_data=levels_manifest,
                        flip_rows=_bk_flip)
                    _bank_wall_pfx = "B{}_".format(_bk_idx)
                    for _pd in _pax_door_raw:
                        _pid = _pd.get("id", "")
                        if _pid.startswith("AI_"):
                            _pd["id"] = "AI_B{}_".format(_bk_idx) + _pid[3:]
                        # Fix wall_ai_id_map: prefix every wall ID with the bank tag
                        # so it matches the IDs registered by fire_safety_logic
                        if "wall_ai_id_map" in _pd:
                            _pd["wall_ai_id_map"] = {
                                lvl_id: ("AI_" + _bank_wall_pfx + wid[3:])
                                        if wid.startswith("AI_") else wid
                                for lvl_id, wid in _pd["wall_ai_id_map"].items()
                            }
                    _bk_door_specs_this.extend(_pax_door_raw)
                except Exception:
                    pass
                _per_bank_door_specs.append(_bk_door_specs_this)
                _per_bank_voids_mm.append(list(_bk_voids_mm))

                # Spatial reservation + column exclusion
                _bk_sub = _bk_fs.get("sub_boundaries", [])
                _bk_excl = [(mm_to_ft(_bk_lcb[0]), mm_to_ft(_bk_lcb[1]),
                              mm_to_ft(_bk_lcb[2]), mm_to_ft(_bk_lcb[3]))]
                for _sb in _bk_sub:
                    _r = _sb["rect"]
                    _bk_excl.append((mm_to_ft(_r[0]), mm_to_ft(_r[1]),
                                     mm_to_ft(_r[2]), mm_to_ft(_r[3])))
                    self.spatial_registry.reserve(_sb["id"], (_r[0], _r[1], 0, _r[2], _r[3], typical_h_mm))
                for cb in _bk_fs.get("core_bounds", []):
                    cb_ft = (mm_to_ft(cb[0]), mm_to_ft(cb[1]), mm_to_ft(cb[2]), mm_to_ft(cb[3]))
                    if cb_ft not in _bk_excl:
                        _bk_excl.append(cb_ft)

                if _bk_first_excl is None:
                    _bk_first_excl = _bk_excl[0] if _bk_excl else None

                # Union bounds across banks
                for _cb in _bk_fs.get("core_bounds", []):
                    _cb_ft = (mm_to_ft(_cb[0]), mm_to_ft(_cb[1]),
                               mm_to_ft(_cb[2]), mm_to_ft(_cb[3]))
                    if _bk_bounds_ft is None:
                        _bk_bounds_ft = _cb_ft
                    else:
                        _bk_bounds_ft = (
                            min(_bk_bounds_ft[0], _cb_ft[0]), min(_bk_bounds_ft[1], _cb_ft[1]),
                            max(_bk_bounds_ft[2], _cb_ft[2]), max(_bk_bounds_ft[3], _cb_ft[3]))

                # Collect stair center tuples for deferred stair-run-data computation
                _bk_sc = _bk_fs.get("stair_centers", [])
                _bk_all_stair_centers_count += len(_bk_sc)
                self.log("[MultiBank] Bank {} fire_safety result: walls={} floors={} voids={} stair_centers={} door_specs={}".format(
                    _bk_idx, len(_bk_fs.get("walls", [])), len(_bk_fs.get("floors", [])),
                    len(_bk_fs.get("voids", [])), len(_bk_sc), len(_bk_fs.get("door_specs", []))))
                self.log("[MultiBank] Bank {} stair_centers: {}".format(_bk_idx, _bk_sc))
                if _bk_fs.get("ortools_conflict"):
                    _per_bank_oc_list.append(_bk_fs["ortools_conflict"])
                _per_bank_stair_sc.append({
                    "sc": _bk_sc,
                    "overrides": _bk_fs.get("stair_overrides", []),
                    "rot": _bk_rot, "cx": _bk_cx, "cy": _bk_cy,
                    "n": _bk_n, "lcb": _bk_lcb,
                })

            # Fix 4: within each X-collocated group of banks (same arm), align their
            # west (min-x) faces so staircase walls stay flush with stair flights.
            # Banks on different arms (|cx difference| > 5000mm) must NOT be shifted
            # relative to each other — doing so moves an entire arm's core onto another arm.
            # Also skip banks on different Y rows (e.g. courtyard north/south bands) —
            # those are independent arms and aligning their X faces is wrong.
            if len(_per_bank_walls) >= 2:
                # Group banks by rounded X centre (5000mm tolerance = same arm)
                _bk_cx_list = [_per_bank_stair_sc[_bi]["cx"] for _bi in range(len(_per_bank_walls))]
                _bk_cy_list = [_per_bank_stair_sc[_bi]["cy"] for _bi in range(len(_per_bank_walls))]
                _bk_x1s = []
                for _bk_wls in _per_bank_walls:
                    xs = [w["start"][0] for w in _bk_wls if w.get("start")]
                    _bk_x1s.append(min(xs) if xs else float("inf"))

                # Build groups: banks are in the same group if |cx_i - cx_j| <= 5000mm
                # AND their Y centres are within 5000mm (same horizontal row of banks).
                # Courtyard buildings have north+south banks with very different cy values —
                # those must NOT be aligned (they are on separate arms; aligning their X
                # faces would shift one bank laterally, misaligning it from its OR-Tools solution).
                _visited = set()
                _align_groups = []
                for _bi in range(len(_per_bank_walls)):
                    if _bi in _visited: continue
                    _grp = [_bi]
                    _visited.add(_bi)
                    for _bj in range(_bi + 1, len(_per_bank_walls)):
                        if _bj not in _visited and abs(_bk_cx_list[_bi] - _bk_cx_list[_bj]) <= 5000 and abs(_bk_cy_list[_bi] - _bk_cy_list[_bj]) <= 5000:
                            _grp.append(_bj)
                            _visited.add(_bj)
                    _align_groups.append(_grp)

                for _grp in _align_groups:
                    if len(_grp) < 2:
                        continue  # single bank in its arm — nothing to align
                    _grp_x1s = [_bk_x1s[_bi] for _bi in _grp]
                    _grp_finite = [x for x in _grp_x1s if x != float("inf")]
                    if len(_grp_finite) < 2:
                        continue
                    _target_x1 = max(_grp_finite)
                    for _bidx in _grp:
                        _bx1 = _bk_x1s[_bidx]
                        dx_al = _target_x1 - _bx1
                        if abs(dx_al) <= 1 or _bx1 == float("inf"):
                            continue
                        # Shift walls
                        for _w in _per_bank_walls[_bidx]:
                            if _w.get("start"): _w["start"][0] += dx_al
                            if _w.get("end"):   _w["end"][0]   += dx_al
                        # Shift floors
                        for _f in _per_bank_floors[_bidx]:
                            if "points" in _f:
                                _f["points"] = [[p[0] + dx_al, p[1]] for p in _f["points"]]
                        # Shift voids (non-rotated banks use mm rects; rotated use pre-converted ft corners)
                        _shifted_voids = []
                        for _vv in _per_bank_voids_mm[_bidx]:
                            if isinstance(_vv, tuple) and len(_vv) == 9 and _vv[0] == "rotated_ft":
                                _shifted_voids.append(_vv)  # rotated voids already in ft; skip mm shift
                            else:
                                _vx1, _vy1, _vx2, _vy2 = _vv
                                _shifted_voids.append((_vx1 + dx_al, _vy1, _vx2 + dx_al, _vy2))
                        _per_bank_voids_mm[_bidx] = _shifted_voids
                        # Shift door positions
                        for _ds in _per_bank_door_specs[_bidx]:
                            _pm = _ds.get("position_mm")
                            if _pm and len(_pm) >= 2: _pm[0] += dx_al
                            for _wl in (_ds.get("wall_line_mm") or []):
                                if len(_wl) >= 2: _wl[0] += dx_al
                        # Shift stair centre X
                        _pbs = _per_bank_stair_sc[_bidx]
                        _pbs["sc"] = [
                            (sc[0] + dx_al, sc[1], sc[2], sc[3] if len(sc) > 3 else None)
                            for sc in _pbs["sc"]
                        ]
                        # Shift lift core bounds X so staircase geometry anchors to the shifted bank
                        _old_lcb = _pbs.get("lcb")
                        if _old_lcb:
                            _pbs["lcb"] = [
                                _old_lcb[0] + dx_al, _old_lcb[1],
                                _old_lcb[2] + dx_al, _old_lcb[3],
                            ]
                        # base_y_overrides are Y-values — no X shift needed
                        self.log("[BankAlign] Bank {}: x-aligned {:.0f}mm east within arm-group (walls+floors+voids+doors+stairs+lcb)".format(_bidx, dx_al))

            # Now merge all per-bank data into combined lists
            for _bk_wls in _per_bank_walls:
                manifest["walls"].extend(_bk_wls)
            for _bk_fls in _per_bank_floors:
                manifest["floors"].extend(_bk_fls)
            for _bk_ds in _per_bank_door_specs:
                _bk_all_door_specs.extend(_bk_ds)
            for _bidx2, _pbs in enumerate(_per_bank_stair_sc):
                _bk_sc2   = _pbs["sc"]
                _bk_rot2  = _pbs["rot"]
                _bk_cx2   = _pbs["cx"]
                _bk_cy2   = _pbs["cy"]
                _bk_n2    = _pbs["n"]
                _bk_lcb2  = _pbs["lcb"]
                _bk_stair_pos2    = [(sc[0], sc[1]) for sc in _bk_sc2] if _bk_sc2 else []
                _bk_rotated_idx2  = [idx for idx, sc in enumerate(_bk_sc2) if sc[2]] if _bk_sc2 else []
                _bk_stair_ovr2    = _pbs["overrides"]
                if _bk_stair_pos2:
                    _bk_sw2 = staircase_logic.get_shaft_dimensions(typical_h_mm, stair_spec)[0]
                    _bk_sc_rot_degs2 = [sc[3] if len(sc) > 3 else None for sc in _bk_sc2]
                    _bk_rd2 = staircase_logic.get_stair_run_data(
                        _bk_stair_pos2, levels_manifest, _bk_sw2, stair_spec, typical_h_mm,
                        lift_core_bounds_mm=(_bk_lcb2 if not _bk_rot2 else None),
                        floor_dims_mm=floor_dims, num_lifts=(_bk_n2 if not _bk_rot2 else 0),
                        lobby_width=lobby_w,
                        rotated_indices=_bk_rotated_idx2,
                        base_y_overrides=(_bk_stair_ovr2 if _bk_stair_ovr2 else None),
                        rotation_degs=_bk_sc_rot_degs2,
                    )
                    if _bk_rot2:
                        _bk_rad_st2 = _bkm.radians(_bk_rot2)
                        for _rd2 in _bk_rd2:
                            _rd2['_global_rotation_rad'] = _bk_rad_st2
                            _rd2['_rotation_cx_ft'] = mm_to_ft(_bk_cx2)
                            _rd2['_rotation_cy_ft'] = mm_to_ft(_bk_cy2)
                    _bk_all_stair_run_data.extend(_bk_rd2)
            # Convert voids to ft and build void polys.
            # Rotated banks stored pre-rotated corners as ("rotated_ft", rx1,ry1,...,rx4,ry4).
            # Non-rotated banks stored plain (vx1,vy1,vx2,vy2) mm rectangles.
            for _bk_voids2 in _per_bank_voids_mm:
                for _v in _bk_voids2:
                    if isinstance(_v, tuple) and len(_v) == 9 and _v[0] == "rotated_ft":
                        _, rx1, ry1, rx2, ry2, rx3, ry3, rx4, ry4 = _v
                        _corners2 = [(rx1, ry1), (rx2, ry2), (rx3, ry3), (rx4, ry4)]
                        _bk_all_void_polys.append(_corners2)
                        _xs2 = [p[0] for p in _corners2]; _ys2 = [p[1] for p in _corners2]
                        _bk_all_voids_ft.append((min(_xs2), min(_ys2), max(_xs2), max(_ys2)))
                    else:
                        vx1, vy1, vx2, vy2 = _v
                        _bk_all_voids_ft.append((mm_to_ft(vx1), mm_to_ft(vy1), mm_to_ft(vx2), mm_to_ft(vy2)))

            # Commit multi-bank results
            self._stair_voids = _bk_all_voids_ft
            self._stair_void_polygons = _bk_all_void_polys
            self._door_data = _bk_all_door_specs
            self._stair_run_data = _bk_all_stair_run_data
            self._core_exclusion_zones = []
            # Rebuild combined exclusion list across all banks
            for _bk2_idx, _bk2 in enumerate(_banks_cfg):
                _bk2_pos = _bk2.get("position", [0, 0])
                _bk2_cx = float(_bk2_pos[0]); _bk2_cy = float(_bk2_pos[1])
                _svg_off_bk2 = manifest.get("shell", {}).get("_svg_offset")
                if _svg_off_bk2 and (_bk2_cx != 0.0 or _bk2_cy != 0.0):
                    _bk2_cx -= float(_svg_off_bk2[0])
                    _bk2_cy -= float(_svg_off_bk2[1])
                _bk2_n = int(safe_num(_bk2.get("count", max(1, num_lifts // max(len(_banks_cfg), 1))), 1))
                _bk2_layout = lift_logic.get_total_core_layout(_bk2_n, lobby_width=lobby_w)
                _bk2_lcb = (
                    _bk2_cx - _bk2_layout["total_w"] / 2.0,
                    _bk2_cy - _bk2_layout["total_d"] / 2.0,
                    _bk2_cx + _bk2_layout["total_w"] / 2.0,
                    _bk2_cy + _bk2_layout["total_d"] / 2.0,
                )
                self._core_exclusion_zones.append((
                    mm_to_ft(_bk2_lcb[0]), mm_to_ft(_bk2_lcb[1]),
                    mm_to_ft(_bk2_lcb[2]), mm_to_ft(_bk2_lcb[3])
                ))
            if _bk_first_excl is not None:
                self._lift_core_bounds_ft = _bk_first_excl
            self._stair_summary = {
                "count": _bk_all_stair_centers_count,
                "riser": stair_spec.get("riser", 150),
                "tread": stair_spec.get("tread", 280),
                "typical_h": typical_h_mm,
                "flights_typical": staircase_logic._calc_num_flights(typical_h_mm, typical_h_mm, 150),
                "risers_per_flight": staircase_logic._risers_per_flight_typical(typical_h_mm, 150)
            }
            self.log("[MultiBank] {} bank(s) complete: {} stair runs, {} voids".format(
                len(_banks_cfg), len(_bk_all_stair_run_data), len(_bk_all_voids_ft)))
            # If every bank produced 0 walls because OR-Tools + hardcoded both failed,
            # bubble up a CONFLICT so the dispatcher retries instead of saving silently.
            if _per_bank_oc_list:
                _total_walls = sum(len(w) for w in _per_bank_walls)
                if _total_walls == 0:
                    _merged_oc = {
                        "min_core_w":       max(oc["min_core_w"] for oc in _per_bank_oc_list),
                        "min_core_d":       max(oc["min_core_d"] for oc in _per_bank_oc_list),
                        "failed_set_count": sum(oc.get("failed_set_count", 0) for oc in _per_bank_oc_list),
                        "failure_details":  [fd for oc in _per_bank_oc_list
                                             for fd in oc.get("failure_details", [])],
                    }
                    _shell_w = round(safe_num(manifest.get("shell", {}).get("width",  0), 0))
                    _shell_l = round(safe_num(manifest.get("shell", {}).get("length", 0), 0))
                    _core_w  = _merged_oc["min_core_w"]
                    _core_d  = _merged_oc["min_core_d"]
                    _fp_pts  = manifest.get("shell", {}).get("footprint_points") or []
                    _narrow  = None
                    if _fp_pts:
                        try:
                            _xs = [p[0] for p in _fp_pts]; _ys = [p[1] for p in _fp_pts]
                            _narrow = int(min(max(_xs) - min(_xs), max(_ys) - min(_ys)))
                        except Exception:
                            pass
                    _narrow_note = (
                        " Narrowest footprint span: {:,}mm (arm too narrow — must be ≥{:,}mm).".format(
                            _narrow, _core_w)
                        if (_narrow and _narrow < _core_w) else ""
                    )
                    self.log("[MultiBank] All banks produced 0 walls — returning CONFLICT for retry.")
                    return {
                        "status":  "CONFLICT",
                        "type":    "ORTOOLS_INFEASIBLE",
                        "description": (
                            "OR-Tools could not fit the fire safety core ({} cluster(s)) in any bank "
                            "within the given footprint. Minimum core envelope: {:,}×{:,}mm. "
                            "Shell bounding box: {:,}×{:,}mm.{}"
                            .format(_merged_oc["failed_set_count"], _core_w, _core_d,
                                    _shell_w, _shell_l, _narrow_note)
                        ),
                        "core_layout_summary": "  total core envelope (min)  {:>6} x {:>5} mm\n".format(
                            _core_w, _core_d),
                        "core_total_mm":   {"width": _core_w, "depth": _core_d},
                        "manifest_inputs": {
                            "lift_count":      num_lifts,
                            "shell_width_mm":  _shell_w,
                            "shell_length_mm": _shell_l,
                            "level_height_mm": round(typical_h_mm),
                        },
                        "resolution_hints": [
                            "Minimum core width needed: {:,}mm.".format(_core_w),
                            "Minimum core depth needed: {:,}mm.".format(_core_d),
                            "Consider widening the arm, reducing lift count, or changing orientation.",
                        ],
                        "ortools_failure_details": _merged_oc["failure_details"],
                    }
            return _bk_bounds_ft
        # ── End multi-bank path ────────────────────────────────────────────────

        # When 2 fire clusters exist and lift count is odd, round UP to even so both
        # staircase sides see equal lift counts (floor(n/2) == ceil(n/2)).
        # Example: 5 lifts → 6 lifts (3+3). Both staircases see 3. Symmetric.
        _non_perim_count = sum(
            1 for s in safety_sets
            if not s.get("is_perimeter") and s.get("type") == "FIRE_LIFT"
        )
        _eff_lifts = num_lifts
        if _non_perim_count == 2 and num_lifts >= 4 and num_lifts % 2 != 0:
            _eff_lifts = num_lifts + 1
            self.log("[CoreSetup] 2 fire clusters + odd lift count ({}) → rounding to {} for symmetric split".format(
                num_lifts, _eff_lifts))
            # Recompute lift_core_bounds_mm with the adjusted count
            _layout_eff = lift_logic.get_total_core_layout(_eff_lifts, lobby_width=lobby_w)
            lift_core_bounds_mm = (
                f_center_mm[0] - _layout_eff["total_w"] / 2,
                f_center_mm[1] - _layout_eff["total_d"] / 2,
                f_center_mm[0] + _layout_eff["total_w"] / 2,
                f_center_mm[1] + _layout_eff["total_d"] / 2,
            )

        self.log("[CoreSetup] Single-bank generate_fire_safety_manifest: "
                 "sets={} stair_spec riser={} flight_w={} entry_landing={} "
                 "lcb=({:.0f},{:.0f},{:.0f},{:.0f}) num_lifts={} lobby_w={:.0f}".format(
            len(safety_sets),
            stair_spec.get("riser", 150), stair_spec.get("width_of_flight", "?"),
            stair_spec.get("entry_landing_width", "?"),
            float(lift_core_bounds_mm[0]), float(lift_core_bounds_mm[1]),
            float(lift_core_bounds_mm[2]), float(lift_core_bounds_mm[3]),
            _eff_lifts, float(lobby_w)))
        unified_man = fire_safety_logic.generate_fire_safety_manifest(
            safety_sets, levels_manifest, stair_spec, typical_h_mm, preset_fs,
            lift_core_bounds_mm, _eff_lifts, lobby_w,
            compliance_overrides=_cp_overrides,
            footprint_pts=shell.get("footprint_points"),
            footprint_holes=_normalise_footprint_holes(shell.get("footprint_holes", [])),
            all_floor_dims=floor_dims,
        )

        if isinstance(unified_man, dict) and unified_man.get("status") == "CONFLICT":
            self.log("[CoreSetup] Single-bank generate_fire_safety_manifest → CONFLICT: {}".format(
                unified_man.get("description", "")))
            # Attach manifest inputs so dispatcher can build a rich retry prompt
            unified_man.setdefault("manifest_inputs", {
                "lift_count": num_lifts,
                "shell_width_mm": round(safe_num(manifest.get("shell", {}).get("width", 0), 0)),
                "shell_length_mm": round(safe_num(manifest.get("shell", {}).get("length", 0), 0)),
                "level_height_mm": round(typical_h_mm),
            })
            return unified_man
        self.log("[CoreSetup] Single-bank manifest OK: walls={} floors={} door_specs={} voids={}".format(
            len(unified_man.get("walls", [])),
            len(unified_man.get("floors", [])),
            len(unified_man.get("door_specs", [])),
            len(unified_man.get("voids", []))))

        # OR-Tools infeasibility: solver could not fit one or more fire safety clusters.
        # Return a structured CONFLICT so dispatcher feeds real dimensions to Gemini.
        _oc = unified_man.get("ortools_conflict")
        if _oc:
            _core_w   = _oc["min_core_w"]
            _core_d   = _oc["min_core_d"]
            _shell_w  = safe_num(manifest.get("shell", {}).get("width",  0), 0)
            _shell_l  = safe_num(manifest.get("shell", {}).get("length", 0), 0)
            # Compute narrowest footprint span — for L/U/H shapes this is the arm width,
            # which is the actual binding constraint (not the shell bounding box).
            _fp_pts = manifest.get("shell", {}).get("footprint_points") or []
            _narrowest_span = None
            if _fp_pts and len(_fp_pts) >= 3:
                try:
                    _fp_xs = [p[0] for p in _fp_pts]
                    _fp_ys = [p[1] for p in _fp_pts]
                    _narrowest_span = int(min(max(_fp_xs) - min(_fp_xs),
                                             max(_fp_ys) - min(_fp_ys)))
                except Exception:
                    _narrowest_span = None
            _zones_text = (
                "  {:<30s}  {:>6} x {:>5} mm\n"
            ).format(
                "total core envelope (min)",  _core_w, _core_d,
            )
            _hints = [
                "Minimum core width needed: {:,}mm. Current shell width: {:,}mm.".format(
                    _core_w, int(_shell_w)),
                "Minimum core depth needed: {:,}mm. Current shell length: {:,}mm.".format(
                    _core_d, int(_shell_l)),
                "Consider: reduce lift count, increase shell dimensions, or change building shape.",
            ]
            if _narrowest_span and _narrowest_span < _core_w:
                _hints.append(
                    "CRITICAL: Narrowest footprint span is {:,}mm — smaller than the required "
                    "core width {:,}mm. The arm/wing is too narrow. "
                    "Widen the narrowest arm to at least {:,}mm via floor_overrides, "
                    "or reduce lift count.".format(_narrowest_span, _core_w, _core_w + 500))
            if _shell_w and _core_w > _shell_w * 0.6:
                _hints.append(
                    "Core width ({:,}mm) exceeds 60% of shell width ({:,}mm) — "
                    "suggest increasing shell.width to at least {:,}mm.".format(
                        _core_w, int(_shell_w), int(_core_w / 0.55)))
            if _shell_l and _core_d > _shell_l * 0.55:
                _hints.append(
                    "Core depth ({:,}mm) exceeds 55% of shell length ({:,}mm) — "
                    "suggest increasing shell.length to at least {:,}mm.".format(
                        _core_d, int(_shell_l), int(_core_d / 0.45)))
            _narrow_note = (
                " Narrowest footprint span: {:,}mm (arm/wing too narrow — must be ≥{:,}mm).".format(
                    _narrowest_span, _core_w)
                if (_narrowest_span and _narrowest_span < _core_w) else ""
            )
            return {
                "status":             "CONFLICT",
                "type":               "ORTOOLS_INFEASIBLE",
                "description": (
                    "OR-Tools could not fit the fire safety core ({} cluster(s)) within the "
                    "given footprint. Minimum core envelope required: {:,}x{:,}mm. "
                    "Shell bounding box: {:,}x{:,}mm.{}"
                    .format(
                        _oc["failed_set_count"], _core_w, _core_d,
                        int(_shell_w), int(_shell_l), _narrow_note)
                ),
                "core_layout_summary": _zones_text,
                "core_total_mm":       {"width": _core_w, "depth": _core_d},
                "manifest_inputs": {
                    "lift_count":       num_lifts,
                    "shell_width_mm":   int(_shell_w),
                    "shell_length_mm":  int(_shell_l),
                    "level_height_mm":  round(typical_h_mm),
                },
                "resolution_hints": _hints,
                "ortools_failure_details": _oc.get("failure_details", []),
            }

        # PAX bank row orientation. Odd lift counts are already rounded to even above
        # so both rows are equal size (floor==ceil). flip_rows=False keeps Row1 north.
        _psgr_flip = False

        # Collect door specs from fire-safety manifest + passenger lift doors
        _door_specs = list(unified_man.get("door_specs", []))
        try:
            psgr_doors = lift_logic.get_passenger_lift_door_positions(
                _eff_lifts, center_pos=(f_center_mm[0], f_center_mm[1]),
                lobby_width=lobby_w, levels_data=levels_manifest,
                flip_rows=_psgr_flip,
            )
            _door_specs.extend(psgr_doors)
        except Exception as _pd_e:
            self.log("Passenger lift door positions failed (non-fatal): {}".format(_pd_e))
        self._door_data = _door_specs

        # 5.5 Precise Spatial Reservation + Column Exclusion Zones
        # Use individual sub-boundary rects (not the merged core bounding rect) so
        # the column-exclusion logic culls only columns that actually fall inside a
        # core element — not everything inside the huge L-shaped merged bounding box.
        sub_bounds = unified_man.get("sub_boundaries", [])

        # E1: Validate all core zone corners lie inside the floor plate polygon.
        # A zone that lands in a notch, courtyard void, or outside the polygon
        # cannot be built — return CONFLICT so Gemini can reposition the core.
        _fp_pts_val = shell.get("footprint_points")
        if _fp_pts_val and len(_fp_pts_val) >= 3:
            from revit_mcp.staircase_logic import _point_in_polygon
            _fp_holes_val = _normalise_footprint_holes(shell.get("footprint_holes", []))
            _outside_zones = []
            _hole_zones = []   # zones whose corners land inside a footprint_holes void
            _all_zone_rects = [list(lift_core_bounds_mm)] + [sb["rect"] for sb in (sub_bounds or [])]
            for _zr in _all_zone_rects:
                _corners = [
                    (_zr[0], _zr[1]), (_zr[2], _zr[1]),
                    (_zr[2], _zr[3]), (_zr[0], _zr[3])
                ]
                _outside = not all(_point_in_polygon(cx, cy, _fp_pts_val) for cx, cy in _corners)
                _in_hole = any(
                    any(_point_in_polygon(cx, cy, h) for h in _fp_holes_val)
                    for cx, cy in _corners
                )
                if _in_hole:
                    _hole_zones.append(_zr)
                if _outside or _in_hole:
                    _outside_zones.append(_zr)
            if _outside_zones:
                self.log("[E1] lift_core={} | {} outside/hole zones: {}".format(
                    "[{:.0f},{:.0f},{:.0f},{:.0f}]".format(*lift_core_bounds_mm),
                    len(_outside_zones),
                    " | ".join("[{:.0f},{:.0f},{:.0f},{:.0f}]".format(*z) for z in _outside_zones[:4])
                ))
                _zone_desc = "; ".join(
                    "zone [{:.0f},{:.0f}]->[{:.0f},{:.0f}]mm".format(*z) for z in _outside_zones
                )
                _fp_bbox = "Polygon bbox: X=[{:.0f},{:.0f}], Y=[{:.0f},{:.0f}]".format(
                    min(p[0] for p in _fp_pts_val), max(p[0] for p in _fp_pts_val),
                    min(p[1] for p in _fp_pts_val), max(p[1] for p in _fp_pts_val)
                )
                _fp_summary = "Polygon vertices (mm): {}".format(
                    ", ".join("[{:.0f},{:.0f}]".format(p[0], p[1]) for p in _fp_pts_val)
                )
                _lc = lift_core_bounds_mm  # [xmin, ymin, xmax, ymax]
                _poly_ymin = min(p[1] for p in _fp_pts_val)
                _poly_ymax = max(p[1] for p in _fp_pts_val)
                _poly_xmin = min(p[0] for p in _fp_pts_val)
                _poly_xmax = max(p[0] for p in _fp_pts_val)
                _space_s = _lc[1] - _poly_ymin
                _space_n = _poly_ymax - _lc[3]
                _space_w = _lc[0] - _poly_xmin
                _space_e = _poly_xmax - _lc[2]
                _arm_diag = (
                    "Space available around lift core (mm): "
                    "South={:.0f}, North={:.0f}, West={:.0f}, East={:.0f}. "
                    "Lift core occupies X=[{:.0f},{:.0f}], Y=[{:.0f},{:.0f}]."
                ).format(_space_s, _space_n, _space_w, _space_e,
                         _lc[0], _lc[2], _lc[1], _lc[3])

                # Determine whether the failure is (a) core outside polygon, (b) zones in
                # a footprint_holes courtyard void, or (c) arm too narrow for fire clusters.
                _lc_rect = list(lift_core_bounds_mm)
                _core_outside = any(
                    abs(_zr[0] - _lc_rect[0]) < 1 and abs(_zr[1] - _lc_rect[1]) < 1
                    for _zr in _outside_zones
                )

                if _hole_zones:
                    # One or more zones clip into a courtyard/atrium void.
                    # Compute approximate minimum safe distance from void edge.
                    _all_hole_pts = [p for h in _fp_holes_val for p in h]
                    _void_cx = sum(p[0] for p in _all_hole_pts) / max(len(_all_hole_pts), 1)
                    _void_cy = sum(p[1] for p in _all_hole_pts) / max(len(_all_hole_pts), 1)
                    _action = (
                        "The fire safety cluster (shaft + lobby + staircase) clips into the "
                        "courtyard/atrium void (footprint_holes). "
                        "Move lifts.position further away from the void — place it at least "
                        "8000mm from the nearest footprint_holes boundary, towards the outer "
                        "perimeter of the building. "
                        "The void centroid is approximately [{:.0f},{:.0f}]mm — move the core "
                        "in the OPPOSITE direction. "
                        "Do NOT change the building shape. "
                        "Do NOT place any core zone inside the courtyard opening."
                    ).format(_void_cx, _void_cy)
                elif _core_outside:
                    # The passenger lift core itself is outside — reposition the core
                    _action = (
                        "The passenger lift core is outside the floor plate. "
                        "Move lifts.position to a point inside the solid floor plate polygon. "
                        "For L/U/H shapes the junction of the arms is the safest location. "
                        "Ensure all zone corners are at least 2000mm from any polygon edge. "
                        "Do NOT change the building shape."
                    )
                else:
                    # Only fire safety sub-zones are outside — the arm is too narrow.
                    # Compute per-direction overshoot from the outside zones to give Gemini
                    # an exact number of mm to widen by.
                    _oz_xmin = min(z[0] for z in _outside_zones)
                    _oz_ymin = min(z[1] for z in _outside_zones)
                    _oz_xmax = max(z[2] for z in _outside_zones)
                    _oz_ymax = max(z[3] for z in _outside_zones)
                    _overshoot_s = max(0, _poly_ymin - _oz_ymin)
                    _overshoot_n = max(0, _oz_ymax - _poly_ymax)
                    _overshoot_w = max(0, _poly_xmin - _oz_xmin)
                    _overshoot_e = max(0, _oz_xmax - _poly_xmax)
                    _widen_parts = []
                    if _overshoot_n > 0:
                        _widen_parts.append(
                            "north by {:.0f}mm (cluster extends to Y={:.0f}, polygon ends at Y={:.0f} — "
                            "move the concave north edge vertices to Y≥{:.0f})".format(
                                _overshoot_n + 500, _oz_ymax, _poly_ymax, _oz_ymax + 500))
                    if _overshoot_s > 0:
                        _widen_parts.append(
                            "south by {:.0f}mm (cluster extends to Y={:.0f}, polygon ends at Y={:.0f} — "
                            "move the concave south edge vertices to Y≤{:.0f})".format(
                                _overshoot_s + 500, _oz_ymin, _poly_ymin, _oz_ymin - 500))
                    if _overshoot_e > 0:
                        _widen_parts.append(
                            "east by {:.0f}mm (cluster extends to X={:.0f}, polygon ends at X={:.0f} — "
                            "move the concave east edge vertices to X≥{:.0f})".format(
                                _overshoot_e + 500, _oz_xmax, _poly_xmax, _oz_xmax + 500))
                    if _overshoot_w > 0:
                        _widen_parts.append(
                            "west by {:.0f}mm (cluster extends to X={:.0f}, polygon ends at X={:.0f} — "
                            "move the concave west edge vertices to X≤{:.0f})".format(
                                _overshoot_w + 500, _oz_xmin, _poly_xmin, _oz_xmin - 500))
                    _min_space = min(_space_s, _space_n, _space_w, _space_e)
                    _tightest = {_space_s: "south", _space_n: "north",
                                 _space_w: "west",  _space_e: "east"}[_min_space]
                    _widen_instruction = (
                        "Widen {} in footprint_points: {}.".format(
                            _tightest, "; ".join(_widen_parts))
                        if _widen_parts else
                        "Widen the {} arm by at least 6000mm.".format(_tightest)
                    )
                    _action = (
                        "The passenger lift core position is valid, but the fire safety clusters "
                        "(shaft + lobby + staircase) extend beyond the floor plate — the {} arm "
                        "of the building is too narrow to contain them. "
                        "{} "
                        "Move ONLY the concave (inside-notch) edge vertices outward — "
                        "do NOT move outer perimeter vertices. "
                        "Do NOT move lifts.position. "
                        "Do NOT change the building shape type (keep L/U/H/etc. as requested)."
                    ).format(_tightest, _widen_instruction)

                return {
                    "status": "CONFLICT",
                    "description": (
                        "{} core zone(s) extend outside the floor plate polygon: {}. "
                        "{}. {}. {}. {}"
                    ).format(len(_outside_zones), _zone_desc,
                             _fp_bbox, _fp_summary, _arm_diag, _action)
                }

        # F1: Taper containment — verify the full core cluster (pax lifts + all fire
        # safety sub-bounds) fits inside the scaled footprint at every level with a
        # non-unity scale override. Core walls are built at full height so they must
        # fit at every level, not just the base.
        _taper_ovr = shell.get("footprint_scale_overrides")
        _fp_pts_base = shell.get("footprint_points")
        if _taper_ovr and _fp_pts_base and len(_fp_pts_base) >= 3:
            from revit_mcp.staircase_logic import _point_in_polygon as _pip_t
            _rot_ovr = shell.get("footprint_rotation_overrides", {})
            _lc_t = lift_core_bounds_mm
            # Build union of pax core + all sub-boundary rects
            _all_rects_t = [_lc_t] + [sb["rect"] for sb in (sub_bounds or [])]
            _core_corners_t = []
            for _rt in _all_rects_t:
                _core_corners_t += [
                    (_rt[0], _rt[1]), (_rt[2], _rt[1]),
                    (_rt[2], _rt[3]), (_rt[0], _rt[3]),
                ]
            _taper_fail_levels = []
            for _lvl_1 in range(1, len(elevations) + 1):
                _sf = _get_interpolated_scale(_taper_ovr, _lvl_1)
                if abs(_sf - 1.0) < 0.001:
                    continue
                _rot = _get_interpolated_scale(_rot_ovr, _lvl_1) if _rot_ovr else 0.0
                _lvl_pts = _scale_footprint(_fp_pts_base, _sf, rotation_deg=_rot)
                _lvl_pts_2d = [[p[0], p[1]] for p in _lvl_pts]
                if not all(_pip_t(cx, cy, _lvl_pts_2d) for cx, cy in _core_corners_t):
                    _taper_fail_levels.append(_lvl_1)
            if _taper_fail_levels:
                _sf_min = min(_get_interpolated_scale(_taper_ovr, l) for l in _taper_fail_levels)
                _all_x = [c[0] for c in _core_corners_t]; _all_y = [c[1] for c in _core_corners_t]
                return {
                    "status": "CONFLICT",
                    "description": (
                        "Taper containment failure: full core cluster X=[{:.0f},{:.0f}] Y=[{:.0f},{:.0f}]mm "
                        "(passenger lifts + fire clusters + staircases) extends outside the scaled "
                        "floor plate on {} level(s) (first: level {}). "
                        "Smallest plate scale at failing levels: {:.2f}×. "
                        "Core walls are built at full height and cannot shrink with the taper. "
                        "Fix: (a) reduce lift count, (b) use 'parallel' arrangement to reduce cluster depth, "
                        "or (c) keep lifts.position at [0,0] and ensure the smallest scaled plate "
                        "is at least (total_core_width + 2×min_office_bay) wide."
                    ).format(
                        min(_all_x), max(_all_x), min(_all_y), max(_all_y),
                        len(_taper_fail_levels), _taper_fail_levels[0], _sf_min,
                    )
                }

        # F1b: floor_overrides taper — verify lift core fits within the smallest
        # per-floor width/length declared in shell.floor_overrides.
        # This catches tapers expressed as explicit per-floor dimensions rather than
        # footprint_scale_overrides (which F1 above already handles).
        # Only runs for rectangular shells (no footprint_points).
        _floor_ovr = shell.get("floor_overrides", {})
        if _floor_ovr and not shell.get("footprint_points"):
            _lc_b = lift_core_bounds_mm
            _fo_min_w = None
            _fo_min_l = None
            _fo_fail_floor = None
            for _fo_key, _fo_val in _floor_ovr.items():
                if not isinstance(_fo_val, dict):
                    continue
                if "width" in _fo_val:
                    _fw = safe_num(_fo_val["width"], 0)
                    if _fw > 0 and (_fo_min_w is None or _fw < _fo_min_w):
                        _fo_min_w = _fw
                        _fo_fail_floor = _fo_key
                if "length" in _fo_val:
                    _fl_v = safe_num(_fo_val["length"], 0)
                    if _fl_v > 0 and (_fo_min_l is None or _fl_v < _fo_min_l):
                        _fo_min_l = _fl_v
                        _fo_fail_floor = _fo_key
            _core_w_needed = _lc_b[2] - _lc_b[0]
            _core_l_needed = _lc_b[3] - _lc_b[1]
            _fo_w_fail = _fo_min_w is not None and _fo_min_w < _core_w_needed
            _fo_l_fail = _fo_min_l is not None and _fo_min_l < _core_l_needed
            if _fo_w_fail or _fo_l_fail:
                return {
                    "status": "CONFLICT",
                    "description": (
                        "Taper containment failure (floor_overrides): "
                        "lift core requires {:.0f}mm × {:.0f}mm (W×L) but "
                        "floor override '{}' reduces the plate to "
                        "{} × {} mm. "
                        "Either reduce the number of lifts, move lifts.position "
                        "closer to the origin, or ensure no floor_overrides shrinks "
                        "the plate below the core footprint size."
                    ).format(
                        _core_w_needed, _core_l_needed,
                        _fo_fail_floor,
                        "{:.0f}".format(_fo_min_w) if _fo_min_w else "unchanged",
                        "{:.0f}".format(_fo_min_l) if _fo_min_l else "unchanged",
                    )
                }

        # Build per-element column exclusion zones (in ft) — pax lift core + every
        # fire-safety sub-boundary.  This replaces the old single merged rect that
        # wrongly excluded all internal columns when the layout had an L-shape.
        _excl = []
        _excl.append((
            mm_to_ft(lift_core_bounds_mm[0]), mm_to_ft(lift_core_bounds_mm[1]),
            mm_to_ft(lift_core_bounds_mm[2]), mm_to_ft(lift_core_bounds_mm[3])
        ))
        if sub_bounds:
            for sb in sub_bounds:
                rect = sb["rect"]
                res_id = sb["id"]
                _excl.append((mm_to_ft(rect[0]), mm_to_ft(rect[1]),
                               mm_to_ft(rect[2]), mm_to_ft(rect[3])))
                res, conflict = self.spatial_registry.reserve(res_id, (rect[0], rect[1], 0, rect[2], rect[3], typical_h_mm))
                if not res:
                    # Compute total core footprint vs building shell to give Gemini a geometric hint
                    _all_rects = [sb2["rect"] for sb2 in sub_bounds]
                    _all_rects.append(list(lift_core_bounds_mm))
                    _core_x1 = min(r[0] for r in _all_rects)
                    _core_y1 = min(r[1] for r in _all_rects)
                    _core_x2 = max(r[2] for r in _all_rects)
                    _core_y2 = max(r[3] for r in _all_rects)
                    _core_w = _core_x2 - _core_x1
                    _core_d = _core_y2 - _core_y1
                    _shell_w = safe_num(manifest.get("shell", {}).get("width", 0), 0)
                    _shell_l = safe_num(manifest.get("shell", {}).get("length", 0), 0)

                    # Describe each sub-boundary so the AI can see what zones exist
                    _zone_lines = []
                    for _sb in sub_bounds:
                        _r = _sb["rect"]
                        _zw = _r[2] - _r[0]
                        _zd = _r[3] - _r[1]
                        _zone_lines.append("  {:<30s}  {:>6.0f} x {:>5.0f} mm".format(_sb["id"], _zw, _zd))
                    _lift_w = lift_core_bounds_mm[2] - lift_core_bounds_mm[0]
                    _lift_d = lift_core_bounds_mm[3] - lift_core_bounds_mm[1]
                    _zone_lines.insert(0, "  {:<30s}  {:>6.0f} x {:>5.0f} mm".format("PassengerLifts (core)", _lift_w, _lift_d))
                    _zones_text = "\n".join(_zone_lines)

                    # Compute a resolution hint
                    _hints = []
                    if _shell_w and _core_w > _shell_w * 0.45:
                        _needed_w = int(_core_w / 0.40)
                        _hints.append(
                            "Core is {:.0f}mm wide but shell.width is only {:.0f}mm "
                            "(core occupies {:.0%} of width — max comfortable is ~40%). "
                            "Suggest: increase shell.width to at least {:,}mm, "
                            "OR reduce lifts.count from {} to {}.".format(
                                _core_w, _shell_w, _core_w / _shell_w,
                                _needed_w, num_lifts, max(2, num_lifts - 2)
                            )
                        )
                    if _shell_l and _core_d > _shell_l * 0.55:
                        _needed_l = int(_core_d / 0.45)
                        _hints.append(
                            "Core is {:.0f}mm deep but shell.length is only {:.0f}mm "
                            "(core occupies {:.0%} of length — max comfortable is ~45%). "
                            "Suggest: increase shell.length to at least {:,}mm.".format(
                                _core_d, _shell_l, _core_d / _shell_l, _needed_l
                            )
                        )
                    if not _hints:
                        _hints.append(
                            "Core total footprint is {:.0f} x {:.0f}mm vs shell {:.0f} x {:.0f}mm. "
                            "Consider adjusting shell.width, shell.length, or lifts.count.".format(
                                _core_w, _core_d, _shell_w, _shell_l
                            )
                        )

                    return {
                        "status": "CONFLICT",
                        "type": "GEOMETRIC_INTERFERENCE",
                        "description": "Spatial Conflict detected in Central Core: '{}' overlaps with '{}'".format(res_id, conflict),
                        "conflicting_zones": (res_id, conflict),
                        "core_layout_summary": _zones_text,
                        "core_total_mm": {"width": round(_core_w), "depth": round(_core_d)},
                        "shell_mm": {"width": round(_shell_w), "length": round(_shell_l)},
                        "manifest_inputs": {
                            "lift_count": num_lifts,
                            "shell_width_mm": round(_shell_w),
                            "shell_length_mm": round(_shell_l),
                            "level_height_mm": round(typical_h_mm),
                        },
                        "resolution_hints": _hints,
                    }
        else:
            for i, cb in enumerate(unified_man.get("core_bounds", [])):
                res_id = "Core_Set_{}".format(i)
                self.spatial_registry.reserve(res_id, (cb[0], cb[1], 0, cb[2], cb[3], typical_h_mm))
                _excl.append((mm_to_ft(cb[0]), mm_to_ft(cb[1]), mm_to_ft(cb[2]), mm_to_ft(cb[3])))

        # Always add composite cluster bounds (core_bounds) as column exclusion zones.
        # sub_boundaries covers individual sub-components but each covers only part of
        # the lift bank width; core_bounds has the full-width cluster rect so columns
        # in the gap between the staircase east edge and the lift core east wall are culled.
        for cb in unified_man.get("core_bounds", []):
            cb_ft = (mm_to_ft(cb[0]), mm_to_ft(cb[1]), mm_to_ft(cb[2]), mm_to_ft(cb[3]))
            if cb_ft not in _excl:
                _excl.append(cb_ft)

        self._core_exclusion_zones = _excl
        # Lift-core bounds for column grid axis computation
        self._lift_core_bounds_ft = _excl[0]

        # 6. Inject into Build Pipeline
        _walls_base_idx  = len(manifest.get("walls",  []))
        _floors_base_idx = len(manifest.get("floors", []))
        if "walls" not in manifest: manifest["walls"] = []
        manifest["walls"].extend(unified_man.get("walls", []))
        
        if "floors" not in manifest: manifest["floors"] = []
        manifest["floors"].extend(unified_man.get("floors", []))
        
        # ── Passenger Lift Shaft Walls (previously missing!) ──────────────────
        # generate_lift_shaft_manifest produces the structural walls, internal
        # dividers, and top-cap slabs for every passenger lift shaft. These are
        # the actual lift car enclosure walls visible in plan and section.
        try:
            psgr_man = lift_logic.generate_lift_shaft_manifest(
                _eff_lifts,
                levels_manifest,
                center_pos=(f_center_mm[0], f_center_mm[1]),
                lobby_width=lobby_w,
                flip_rows=_psgr_flip,
            )
            manifest["walls"].extend(psgr_man.get("walls", []))
            manifest["floors"].extend(psgr_man.get("floors", []))
            self.log("Passenger lift shaft: {} walls, {} floors generated (flip_rows={}).".format(
                len(psgr_man.get("walls", [])), len(psgr_man.get("floors", [])), _psgr_flip))
        except Exception as _psgr_e:
            self.log("Passenger lift shaft generation failed (non-fatal): {}".format(_psgr_e))

        # Voids
        u_voids_ft = []
        for (vx1, vy1, vx2, vy2) in unified_man.get("voids", []):
            u_voids_ft.append((mm_to_ft(vx1), mm_to_ft(vy1), mm_to_ft(vx2), mm_to_ft(vy2)))
        # Also add passenger lift shaft voids for floor slab cutouts
        try:
            psgr_void_rects = lift_logic.get_shaft_void_rectangles_mm(
                _eff_lifts, center_pos=(f_center_mm[0], f_center_mm[1]), lobby_width=lobby_w,
                flip_rows=_psgr_flip,
            )
            for (vx1, vy1, vx2, vy2) in psgr_void_rects:
                u_voids_ft.append((mm_to_ft(vx1), mm_to_ft(vy1), mm_to_ft(vx2), mm_to_ft(vy2)))
        except Exception as _pv_e:
            self.log("Passenger lift void rects failed (non-fatal): {}".format(_pv_e))
        self._stair_voids = u_voids_ft
        self._stair_void_polygons = []  # filled below if rotation is active

        # 6b. Rotate the entire core assembly if rotation_deg is non-zero.
        # All wall start/end points, floor polygon points, and shaft voids are
        # rotated in-place around f_center_mm before being handed to the build
        # pipeline.  The stair run data receives the rotation parameters so that
        # _build_dogleg_in_scope / _build_multipair_in_scope can rotate all
        # DB.XYZ points just before passing them to the Revit Stairs API.
        if _core_rotation_deg:
            import math as _crot_math
            _crot_rad  = _crot_math.radians(_core_rotation_deg)
            _crot_cos  = _crot_math.cos(_crot_rad)
            _crot_sin  = _crot_math.sin(_crot_rad)
            _crot_cx   = f_center_mm[0]
            _crot_cy   = f_center_mm[1]
            _crot_cx_ft = mm_to_ft(_crot_cx)
            _crot_cy_ft = mm_to_ft(_crot_cy)

            def _crot_xy_mm(x, y):
                dx = x - _crot_cx; dy = y - _crot_cy
                return (_crot_cx + dx * _crot_cos - dy * _crot_sin,
                        _crot_cy + dx * _crot_sin + dy * _crot_cos)

            def _crot_xy_ft(x, y):
                dx = x - _crot_cx_ft; dy = y - _crot_cy_ft
                return (_crot_cx_ft + dx * _crot_cos - dy * _crot_sin,
                        _crot_cy_ft + dx * _crot_sin + dy * _crot_cos)

            # Rotate walls (start / end XY)
            for _w in manifest["walls"][_walls_base_idx:]:
                if "start" in _w:
                    _nx, _ny = _crot_xy_mm(_w["start"][0], _w["start"][1])
                    _w["start"] = [_nx, _ny, _w["start"][2]]
                if "end" in _w:
                    _nx, _ny = _crot_xy_mm(_w["end"][0], _w["end"][1])
                    _w["end"] = [_nx, _ny, _w["end"][2]]

            # Rotate floor polygon points
            for _f in manifest["floors"][_floors_base_idx:]:
                if "points" in _f:
                    _f["points"] = [list(_crot_xy_mm(p[0], p[1])) for p in _f["points"]]

            # Rotate shaft voids → store as rotated polygon loops (in ft).
            # Keep the AABB envelope in _stair_voids for spatial/column checks;
            # the actual polygon loops are stored in _stair_void_polygons for use
            # in _process_floors() to cut correctly-shaped holes in floor slabs.
            _void_polys = []
            _void_aabbs = []
            for (vx1, vy1, vx2, vy2) in u_voids_ft:
                _corners = [
                    _crot_xy_ft(vx1, vy1),
                    _crot_xy_ft(vx2, vy1),
                    _crot_xy_ft(vx2, vy2),
                    _crot_xy_ft(vx1, vy2),
                ]
                _void_polys.append(_corners)
                _xs = [c[0] for c in _corners]; _ys = [c[1] for c in _corners]
                _void_aabbs.append((min(_xs), min(_ys), max(_xs), max(_ys)))
            self._stair_void_polygons = _void_polys
            if _void_aabbs:
                self._stair_voids = _void_aabbs

        # Combined Bounds (ft)
        bounds_ft = None
        for cb in unified_man.get("core_bounds", []):
            cb_ft = (mm_to_ft(cb[0]), mm_to_ft(cb[1]), mm_to_ft(cb[2]), mm_to_ft(cb[3]))
            if bounds_ft is None:
                bounds_ft = cb_ft
            else:
                bounds_ft = (min(bounds_ft[0], cb_ft[0]), min(bounds_ft[1], cb_ft[1]),
                             max(bounds_ft[2], cb_ft[2]), max(bounds_ft[3], cb_ft[3]))
        
        # 7. Prepare Stair Run Data (Required for internal staircase geometry)
        # Use the true staircase shaft centres returned by generate_fire_safety_manifest.
        # safety_set["pos"] is the entry point at the lift-core boundary, NOT the shaft
        # centre — using it directly places stair runs inside the lift core.
        stair_center_data = unified_man.get("stair_centers", [])
        if stair_center_data:
            positions = [(sc[0], sc[1]) for sc in stair_center_data]
            rotated_indices = [idx for idx, sc in enumerate(stair_center_data) if sc[2]]
        else:
            positions = [s["pos"] for s in safety_sets]
            rotated_indices = [idx for idx, s in enumerate(safety_sets) if s.get("rotated")]
        shaft_w_nat = staircase_logic.get_shaft_dimensions(typical_h_mm, stair_spec)[0]
        enc_w = shaft_w_nat
        
        stair_overrides = unified_man.get("stair_overrides", [])
        
        _sc_rot_degs = [sc[3] if len(sc) > 3 else None for sc in stair_center_data] \
               if stair_center_data else None
        self._stair_run_data = staircase_logic.get_stair_run_data(
            positions, levels_manifest, enc_w, stair_spec, typical_h_mm, lift_core_bounds_mm,
            floor_dims_mm=floor_dims, num_lifts=num_lifts, lobby_width=lobby_w,
            rotated_indices=rotated_indices, base_y_overrides=stair_overrides,
            rotation_degs=_sc_rot_degs  # ← add this
        )

        # Stamp each run with the rotation parameters so _build_dogleg_in_scope
        # and _build_multipair_in_scope can rotate their DB.XYZ points in place.
        if _core_rotation_deg:
            for _rd in self._stair_run_data:
                _rd['_global_rotation_rad'] = _crot_rad
                _rd['_rotation_cx_ft']      = _crot_cx_ft
                _rd['_rotation_cy_ft']      = _crot_cy_ft

        # Per-staircase 90° rotation is now handled in get_stair_run_data():
        # when _coords_prerotated=True, flight coords are already in world orientation
        # and _build_dogleg_in_scope passes them directly to CreateStraightRun.
        # No _global_rotation_rad stamping needed for solver-driven 90° rotation.

        # Summary for tracker
        self._stair_summary = {
            "count": len(positions),
            "riser": stair_spec.get("riser", 150),
            "tread": stair_spec.get("tread", 280),
            "typical_h": typical_h_mm,
            "flights_typical": staircase_logic._calc_num_flights(typical_h_mm, typical_h_mm, 150),
            "risers_per_flight": staircase_logic._risers_per_flight_typical(typical_h_mm, 150)
        }
        
        return bounds_ft

    def _build_dogleg_in_scope(self, doc, stair_id, rd, base_lvl, current_levels,
                              tread_ft, hw, landing_ft, spec_riser_mm, spec_tread_mm,
                              _StairsRun, _StairsLanding, _StairsRunJust):
        """Build a standard 2-flight dogleg within a given Staircase scope."""
        import Autodesk.Revit.DB as DB # type: ignore
        f1 = rd['flight_1']
        f2 = rd['flight_2']
        p1s = DB.XYZ(mm_to_ft(f1['start'][0]), mm_to_ft(f1['start'][1]), base_lvl.ProjectElevation)
        p1e = DB.XYZ(mm_to_ft(f1['end'][0]),   mm_to_ft(f1['end'][1]),   base_lvl.ProjectElevation)
        p2s = DB.XYZ(mm_to_ft(f2['start'][0]), mm_to_ft(f2['start'][1]), base_lvl.ProjectElevation)
        p2e = DB.XYZ(mm_to_ft(f2['end'][0]),   mm_to_ft(f2['end'][1]),   base_lvl.ProjectElevation)
        
        _StairsRun.CreateStraightRun(doc, stair_id, DB.Line.CreateBound(p1s, p1e), _StairsRunJust.Center)
        _StairsRun.CreateStraightRun(doc, stair_id, DB.Line.CreateBound(p2s, p2e), _StairsRunJust.Center)
        
        # Automatic landing between runs
        runs = doc.GetElement(stair_id).GetStairsRuns()
        if runs.Count >= 2:
            _StairsLanding.CreateAutomaticLanding(doc, runs[0], runs[1])

    def _build_multipair_in_scope(self, doc, stair_id, rd, base_lvl, current_levels,
                                 tread_ft, hw, landing_ft, spec_riser_mm, spec_tread_mm,
                                 _StairsRun, _StairsLanding, _StairsRunJust):
        """Build a complex multi-pair stair (Tall Floor) within a given Staircase scope."""
        import Autodesk.Revit.DB as DB # type: ignore
        import math as _mp_math
        _mp_rot_rad = rd.get('_global_rotation_rad', 0.0)
        if _mp_rot_rad:
            _mp_cos = _mp_math.cos(_mp_rot_rad)
            _mp_sin = _mp_math.sin(_mp_rot_rad)
            _mp_cx  = rd['_rotation_cx_ft']
            _mp_cy  = rd['_rotation_cy_ft']
            def _rp(pt):
                dx = pt.X - _mp_cx; dy = pt.Y - _mp_cy
                return DB.XYZ(_mp_cx + dx*_mp_cos - dy*_mp_sin,
                               _mp_cy + dx*_mp_sin + dy*_mp_cos, pt.Z)
        else:
            def _rp(pt):
                return pt
        flight_list = rd.get('flight_list', [])
        num_pairs = rd.get('num_flight_pairs', 1)
        total_risers = sum(flight_list) if flight_list else num_pairs * 28
        
        floor_span_ft = rd['top_elev'] - rd['base_elev'] 
        # Note: use elev difference from rd instead of level elev to be robust
        floor_span_ft = mm_to_ft(floor_span_ft)
        base_z = base_lvl.ProjectElevation
        riser_h_ft = (floor_span_ft / total_risers) if total_risers else mm_to_ft(spec_riser_mm)
        
        dyn_landing_ft = mm_to_ft(rd.get("dyn_landing_w_mm", 1500))
        f1_data = rd['flight_1']
        f2_data = rd['flight_2']
        f1_cx = mm_to_ft(f1_data['start'][0])
        f2_cx = mm_to_ft(f2_data['start'][0])
        flight_y_start_ft = mm_to_ft(f1_data['start'][1])

        # Detect 180-degree rotation: after rotation f1 goes in -Y direction (south).
        # Use min/max so left/right X is correct regardless of which flight rotated to which side.
        f1_y_end = mm_to_ft(f1_data['end'][1])
        is_rotated = (f1_y_end < flight_y_start_ft)
        left_cx  = min(f1_cx, f2_cx)
        right_cx = max(f1_cx, f2_cx)
        land_x_left  = left_cx  - hw
        land_x_right = right_cx + hw

        current_base_z = base_z

        # Shaft back wall (far side from flight_y_start).
        # f1_y_end is the flight-zone end (already rotated); adding dyn_landing_ft
        # gives the inner face of the shaft back wall.  Pinning mid-landing to this
        # constant ensures no gap regardless of per-pair riser count.
        if is_rotated:
            shaft_back_y = f1_y_end - dyn_landing_ft
        else:
            shaft_back_y = f1_y_end + dyn_landing_ft

        pair_idx = 0
        while pair_idx < num_pairs and pair_idx * 2 + 1 < len(flight_list):
            a_risers = flight_list[pair_idx * 2]
            b_risers = flight_list[pair_idx * 2 + 1]

            # Run A — direction depends on rotation
            a_run_len = max(a_risers - 1, 1) * tread_ft
            p_as = _rp(DB.XYZ(f1_cx, flight_y_start_ft, current_base_z))
            if is_rotated:
                p_ae = _rp(DB.XYZ(f1_cx, flight_y_start_ft - a_run_len, current_base_z))
            else:
                p_ae = _rp(DB.XYZ(f1_cx, flight_y_start_ft + a_run_len, current_base_z))
            _StairsRun.CreateStraightRun(doc, stair_id, DB.Line.CreateBound(p_as, p_ae), _StairsRunJust.Center)

            mid_z = current_base_z + a_risers * riser_h_ft

            # Mid landing — spans from Run A end to the shaft back wall.
            # For pairs with fewer risers, Run A ends short of the max-riser flight zone;
            # extending to shaft_back_y fills the gap so no void appears at the back wall.
            if is_rotated:
                land_y_top = flight_y_start_ft - a_run_len  # near edge = end of Run A
                land_y_bot = shaft_back_y                   # far edge = shaft back wall
            else:
                land_y_bot = flight_y_start_ft + a_run_len  # near edge = end of Run A
                land_y_top = shaft_back_y                   # far edge = shaft back wall
            lp1 = _rp(DB.XYZ(land_x_left,  land_y_bot, mid_z))
            lp2 = _rp(DB.XYZ(land_x_right, land_y_bot, mid_z))
            lp3 = _rp(DB.XYZ(land_x_right, land_y_top, mid_z))
            lp4 = _rp(DB.XYZ(land_x_left,  land_y_top, mid_z))
            mid_loop = DB.CurveLoop()
            mid_loop.Append(DB.Line.CreateBound(lp1, lp2))
            mid_loop.Append(DB.Line.CreateBound(lp2, lp3))
            mid_loop.Append(DB.Line.CreateBound(lp3, lp4))
            mid_loop.Append(DB.Line.CreateBound(lp4, lp1))
            _StairsLanding.CreateSketchedLanding(doc, stair_id, mid_loop, mid_z - base_z)

            # Run B — returns toward the starting side from where Run A ended
            b_run_len = max(b_risers - 1, 1) * tread_ft
            top_z = mid_z + b_risers * riser_h_ft
            if is_rotated:
                p_bs = _rp(DB.XYZ(f2_cx, flight_y_start_ft - a_run_len, mid_z))
                p_be = _rp(DB.XYZ(f2_cx, flight_y_start_ft - a_run_len + b_run_len, mid_z))
            else:
                p_bs = _rp(DB.XYZ(f2_cx, flight_y_start_ft + a_run_len, mid_z))
                p_be = _rp(DB.XYZ(f2_cx, flight_y_start_ft + a_run_len - b_run_len, mid_z))
            _StairsRun.CreateStraightRun(doc, stair_id, DB.Line.CreateBound(p_bs, p_be), _StairsRunJust.Center)

            # Intermediate landing — connects Run B end to next pair's Run A start.
            # When b_risers < a_risers, Run B ends short of flight_y_start; the landing
            # is extended to cover that gap so the near-side shaft wall has no void.
            if pair_idx < num_pairs - 1:
                inter_z = top_z
                if is_rotated:
                    # Run B ends at: flight_y_start_ft - a_run_len + b_run_len
                    b_end_y = flight_y_start_ft - a_run_len + b_run_len
                    inter_far_y = min(flight_y_start_ft, b_end_y)  # southernmost cover
                    il1 = _rp(DB.XYZ(land_x_left,  inter_far_y, inter_z))
                    il2 = _rp(DB.XYZ(land_x_right, inter_far_y, inter_z))
                    il3 = _rp(DB.XYZ(land_x_right, flight_y_start_ft + dyn_landing_ft, inter_z))
                    il4 = _rp(DB.XYZ(land_x_left,  flight_y_start_ft + dyn_landing_ft, inter_z))
                else:
                    # Run B ends at: flight_y_start_ft + a_run_len - b_run_len
                    b_end_y = flight_y_start_ft + a_run_len - b_run_len
                    inter_far_y = max(flight_y_start_ft, b_end_y)  # northernmost cover
                    il1 = _rp(DB.XYZ(land_x_left,  flight_y_start_ft - dyn_landing_ft, inter_z))
                    il2 = _rp(DB.XYZ(land_x_right, flight_y_start_ft - dyn_landing_ft, inter_z))
                    il3 = _rp(DB.XYZ(land_x_right, inter_far_y, inter_z))
                    il4 = _rp(DB.XYZ(land_x_left,  inter_far_y, inter_z))
                iloop = DB.CurveLoop()
                iloop.Append(DB.Line.CreateBound(il1, il2))
                iloop.Append(DB.Line.CreateBound(il2, il3))
                iloop.Append(DB.Line.CreateBound(il3, il4))
                iloop.Append(DB.Line.CreateBound(il4, il1))
                _StairsLanding.CreateSketchedLanding(doc, stair_id, iloop, inter_z - base_z)

            current_base_z = top_z
            pair_idx += 1

    def _create_stair_runs(self, current_levels, results):
        """Create dogleg staircases. Force-reloads staircase_logic on every call."""
        # FORCE RELOAD: ensure freshly saved .py files are always used.
        # Revit caches modules in sys.modules; this bypasses stale .pyc state.
        import importlib
        import sys
        try:
            import revit_mcp.staircase_logic as _scl_mod
            importlib.reload(_scl_mod)
            self.log("Stair runs: reloaded staircase_logic ({}).".format(
                getattr(_scl_mod, '_BUILD_VERSION', 'unknown')))
        except Exception as _rl_e:
            self.log("Stair runs: reload warning: {}".format(_rl_e))

        import Autodesk.Revit.DB as DB  # type: ignore
        from revit_mcp.utils import setup_failure_handling
        from revit_mcp.preprocessors import NuclearJoinGuard
        import math
        import time
        import re
        doc = self.doc

        run_data_list = getattr(self, '_stair_run_data', [])
        if not run_data_list:
            self.log("Stair runs: no run data, skipping.")
            return

        try:
            _StairsEditScope = DB.StairsEditScope
            from Autodesk.Revit.DB.Architecture import (  # type: ignore
                StairsRun as _StairsRun,
                StairsLanding as _StairsLanding,
                StairsRunJustification as _StairsRunJust,
                MultistoryStairs as _MSS)
        except Exception as e:
            self.log("Stair runs: CANNOT load Stairs API: {}".format(e))
            return

    def _cleanup_staircases(self, doc, run_data_list, manifest):
        """Pre-emptive cleanup of AI staircase containers to prevent Revit auto-adjustment ghosting."""
        import Autodesk.Revit.DB as DB # type: ignore
        from revit_mcp.utils import setup_failure_handling
        
        manifest_fps = set()
        for rd in run_data_list:
            if 'fingerprint' in rd:
                manifest_fps.add("{}|{}".format(rd['tag'], rd['fingerprint']))

        t_del = DB.Transaction(doc, "AI Staircase Pre-Cleanup")
        t_del.Start()
        setup_failure_handling(t_del, use_nuclear=True)
        deleted_count = 0
        existing_fps = {} # Unconditional rebuild: always return empty

        ids_to_delete = []

        # 1. Cleanup old levels (always reset mid-levels to keep logic simple)
        for lvl in DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements():
            if lvl.Name.startswith("AI_Stair_Mid_"):
                ids_to_delete.append(lvl.Id)

        # 2. Total Nuclear Cleanup for ALL Vertical Circulation Containers
        # We delete ALL MultistoryStairs and ALL Stairs to ensure a 100% clean slate.
        # This prevents Revit's internal 'auto-adjustment' from preserving ghost stairs.
        for c in [DB.BuiltInCategory.OST_MultistoryStairs, DB.BuiltInCategory.OST_Stairs]:
            for el in DB.FilteredElementCollector(doc).OfCategory(c).WhereElementIsNotElementType().ToElements():
                try:
                    p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                    cmt = p.AsString() or "" if p else ""
                    name = el.Name or ""
                    # Aggressive: catch AI, Standard, or anything that looks like our managed stairs
                    if cmt.startswith("AI_") or "Stair" in name or not cmt:
                        if el.Id not in ids_to_delete:
                            ids_to_delete.append(el.Id)
                except: pass

        # 4. Atomic deletion pass
        for eid in ids_to_delete:
            try:
                doc.Delete(eid)
                deleted_count += 1
            except: pass

        t_del.Commit()
        t_del.Dispose()
        self.log("Staircase Nuclear Purge: {} deleted. Differential sync disabled for reliable generation.".format(deleted_count))
        return existing_fps


    def _create_stair_runs(self, current_levels, results):
        """Core assembly for dogleg/multi-pair staircases using StairsEditScope."""
        import Autodesk.Revit.DB as DB  # type: ignore
        from revit_mcp.utils import setup_failure_handling, mm_to_ft, ft_to_mm
        from revit_mcp.preprocessors import NuclearJoinGuard
        import re, math, time
        doc = self.doc
        run_data_list = getattr(self, '_stair_run_data', [])
        if not run_data_list:
            return

        try:
            _StairsEditScope = DB.StairsEditScope
            _MSS = DB.Architecture.MultistoryStairs
            from Autodesk.Revit.DB.Architecture import (  # type: ignore
                StairsRun as _StairsRun, 
                StairsLanding as _StairsLanding, 
                StairsRunJustification as _StairsRunJust
            )
        except Exception as e:
            self.log("Stair runs: CANNOT load Stairs API: {}".format(e))
            return

        spec = getattr(self, '_stair_spec', {})
        spec_riser_mm = spec.get("riser", 150)
        spec_tread_mm = spec.get("tread", 300)
        width_mm = spec.get("width_of_flight", 1500)
        landing_mm = spec.get("landing_width", 1500)
        tread_ft = mm_to_ft(spec_tread_mm)
        # EPSILON FIX: add a microscopic amount to avoid "Actual Tread < Min Tread" float warnings
        tread_ft += mm_to_ft(0.05) 
        hw = mm_to_ft(width_mm / 2.0)
        landing_ft = mm_to_ft(landing_mm)
        width_ft = mm_to_ft(width_mm)

        t_start = time.time()

        # --- Resolve risers-per-flight for this build ---
        rpf_typical = spec.get("_rpf", 14)
        try:
            from revit_mcp.staircase_logic import _risers_per_flight_typical
            rpf_typical = _risers_per_flight_typical(
                float(spec.get("_typical_h", 4200)), spec.get("riser", 150))
        except: pass

        pair_height_mm = rpf_typical * 2 * spec_riser_mm  # e.g. 14*2*150 = 4200mm
        pair_height_ft = mm_to_ft(pair_height_mm)

        # --- Create intermediate levels for multi-pair floors ---
        # Tall floors (e.g. 21000mm) get split into N identical 4200mm spans
        # via intermediate levels.  ALL spans become identical, enabling one
        # MultistoryStairs clone for the entire core.
        intermediate_levels = {}  # (base_level_idx, pair_index) -> Level
        t_mid = DB.Transaction(doc, "AI Stair Intermediate Levels")
        t_mid.Start()
        setup_failure_handling(t_mid, use_nuclear=True)
        for rd in run_data_list:
            n_intermediate = rd.get('_intermediate_count', 0)
            if n_intermediate <= 0:
                continue
            bi = rd['base_level_idx']
            if bi >= len(current_levels):
                continue
            base_elev = current_levels[bi].ProjectElevation
            import System.Collections.Generic as Generic # type: ignore
            for p in range(n_intermediate):
                key = (bi, p)
                if key in intermediate_levels:
                    continue
                intermediate_heights = rd.get('intermediate_heights_mm', [])
                if p < len(intermediate_heights):
                    mid_elev = base_elev + mm_to_ft(intermediate_heights[p])
                else:
                    mid_elev = base_elev + (p + 1) * pair_height_ft
                mid_lvl = DB.Level.Create(doc, mid_elev)
                mid_lvl.Name = "AI_Stair_Mid_{}_{}".format(bi + 1, p + 1)
                intermediate_levels[key] = mid_lvl

                import re
                tag_str = rd.get('tag', '')
                m_tag = re.match(r'AI_(Stair_\d+)_L\d+_Run', tag_str)
                c_tag = m_tag.group(1) if m_tag else tag_str
                
                # Generate missing intermediate landing slab inside the shaft
                try:
                    t_ft = mm_to_ft(200.0)
                    w_landing_ft = mm_to_ft(rd.get('width_mm', 1500))
                    f1_start_y_ft = mm_to_ft(rd['flight_1']['start'][1])
                    
                    std_y_start_ft = f1_start_y_ft - w_landing_ft
                    std_y_end_ft = f1_start_y_ft
                    
                    x_left_ft = mm_to_ft(rd['main_landing']['x_left']) + t_ft/2.0
                    x_right_ft = mm_to_ft(rd['main_landing']['x_right']) - t_ft/2.0
                    
                    c1 = DB.XYZ(x_left_ft, std_y_start_ft, mid_elev)
                    c2 = DB.XYZ(x_right_ft, std_y_start_ft, mid_elev)
                    c3 = DB.XYZ(x_right_ft, std_y_end_ft, mid_elev)
                    c4 = DB.XYZ(x_left_ft, std_y_end_ft, mid_elev)
                    loop = DB.CurveLoop()
                    loop.Append(DB.Line.CreateBound(c1, c2))
                    loop.Append(DB.Line.CreateBound(c2, c3))
                    loop.Append(DB.Line.CreateBound(c3, c4))
                    loop.Append(DB.Line.CreateBound(c4, c1))
                    
                    loops = Generic.List[DB.CurveLoop]()
                    loops.Add(loop)
                    
                    f_type = DB.FilteredElementCollector(doc).OfClass(DB.FloorType).FirstElement()
                    floor = DB.Floor.Create(doc, loops, f_type.Id, mid_lvl.Id)
                    from revit_mcp.utils import safe_set_comment
                    safe_set_comment(floor, "AI_{}_MS_MidLanding".format(c_tag))
                except Exception as e:
                    self.log("Failed to create intermediate landing: {}".format(e))
        t_mid.Commit()
        t_mid.Dispose()
        if intermediate_levels:
            self.log("Stair runs: created {} intermediate levels.".format(
                len(intermediate_levels)))

        # --- Group run_data by core ---
        cores = {}  # core_tag -> [run_data_dicts]
        for rd in run_data_list:
            m = re.match(r'AI_(Stair_\d+)_L\d+_Run', rd['tag'])
            core_tag = m.group(1) if m else "Stair_1"
            cores.setdefault(core_tag, []).append(rd)

        all_stair_ids = []
        from System.Collections.Generic import HashSet  # type: ignore
        existing_fps = getattr(self, '_stair_fps_cache', {})

        for core_tag, core_runs in sorted(cores.items()):
            # â"€â"€ ENRICH AND GROUP BY HEIGHT â"€â"€
            # We must enrich ALL runs with level references before grouping.
            target_runs = []
            for rd in core_runs:
                # Differential Sync Protection: Preserve valid existing stairs
                fp_key = "{}|{}".format(rd['tag'], rd.get('fingerprint',''))
                if rd['tag'] in existing_fps and existing_fps[rd['tag']] == rd.get('fingerprint',''):
                    stair_fec = DB.FilteredElementCollector(doc).OfClass(DB.Architecture.Stairs).WhereElementIsNotElementType()
                    for s in stair_fec:
                        sp = s.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                        if sp and sp.AsString() == fp_key:
                            all_stair_ids.append((s.Id, core_tag))
                            break
                    continue

                bi = rd['base_level_idx']
                ti = rd['top_level_idx']
                if bi < len(current_levels) and ti < len(current_levels):
                    rd['_pair_base_level'] = current_levels[bi]
                    rd['_pair_top_level'] = current_levels[ti]
                    target_runs.append(rd)

            # Unified Batching: Group by CONTIGUOUS blocks of the same height
            # (MultistoryStairs cannot reliably skip levels of different heights)
            contiguous_groups = [] # list of [run_data_dicts]
            if target_runs:
                current_group = [target_runs[0]]
                for i in range(1, len(target_runs)):
                    prev = target_runs[i-1]
                    curr = target_runs[i]
                    h_prev = round(prev['top_elev'] - prev['base_elev'], 1)
                    h_curr = round(curr['top_elev'] - curr['base_elev'], 1)
                    # Must be same height AND contiguous level indices
                    if h_curr == h_prev and curr['base_level_idx'] == prev['top_level_idx']:
                        current_group.append(curr)
                    else:
                        contiguous_groups.append(current_group)
                        current_group = [curr]
                contiguous_groups.append(current_group)

            skipped = []
            for h_idx, seg_runs in enumerate(contiguous_groups):
                # seg_runs now contains a block of contiguous floors of the same height
                if not seg_runs: continue
                ref_rd = seg_runs[0]
                h_val = round(ref_rd['top_elev'] - ref_rd['base_elev'], 1)
                ref_base = ref_rd['_pair_base_level']
                ref_top = ref_rd['_pair_top_level']

                # Create reference stair
                scope = None
                t = None
                ref_stair_id = None
                try:
                    scope = _StairsEditScope(doc, "AI Stair Ref")
                    ref_stair_id = scope.Start(ref_base.Id, ref_top.Id)
                    t = DB.Transaction(doc, "AI Stair Ref Dogleg")
                    t.Start()
                    if ref_rd.get('_intermediate_count', 0) > 0:
                        # Tall floor multi-pair build
                        self._build_multipair_in_scope(
                            doc, ref_stair_id, ref_rd, ref_base, current_levels,
                            tread_ft, hw, landing_ft, spec_riser_mm, spec_tread_mm,
                            _StairsRun, _StairsLanding, _StairsRunJust)
                    else:
                        # Standard typical build
                        self._build_dogleg_in_scope(
                            doc, ref_stair_id, ref_rd, ref_base, current_levels,
                            tread_ft, hw, landing_ft, spec_riser_mm, spec_tread_mm,
                            _StairsRun, _StairsLanding, _StairsRunJust)
                    t.Commit()
                    t.Dispose()
                    t = None
                    scope.Commit(NuclearJoinGuard(doc))
                    scope.Dispose()
                    scope = None

                    # EW rotation for this stair segment reference
                    _seg_rot_deg = ref_rd.get('_rot_deg', 0.0)
                    if _seg_rot_deg and abs(_seg_rot_deg) > 0.1:
                        import math as _seg_math
                        _seg_centre = ref_rd.get('_rot_centre_mm')
                        _seg_rcx = mm_to_ft(_seg_centre[0]) if _seg_centre else 0.0
                        _seg_rcy = mm_to_ft(_seg_centre[1]) if _seg_centre else 0.0
                        _seg_axis = DB.Line.CreateBound(
                            DB.XYZ(_seg_rcx, _seg_rcy, 0.0),
                            DB.XYZ(_seg_rcx, _seg_rcy, 1.0))
                        t_seg_rot = DB.Transaction(doc, "AI Stair Seg Rotate")
                        try:
                            t_seg_rot.Start()
                            setup_failure_handling(t_seg_rot)
                            from Autodesk.Revit.DB import ElementTransformUtils  # type: ignore
                            ElementTransformUtils.RotateElement(
                                doc, ref_stair_id, _seg_axis, _seg_math.radians(_seg_rot_deg))
                            t_seg_rot.Commit()
                        except Exception as _sre:
                            self.log("  Seg rotate failed (non-fatal): {}".format(_sre))
                            try: t_seg_rot.RollBack()
                            except: pass
                        finally:
                            try: t_seg_rot.Dispose()
                            except: pass

                except Exception as e:
                    self.log("  Core {} h_idx {} ref FAILED: {}".format(core_tag, h_idx, e))
                    if t:
                        try: t.RollBack()
                        except: pass
                        try: t.Dispose()
                        except: pass
                        t = None
                    if scope:
                        try: scope.RollBack()
                        except: pass
                        try: scope.Dispose()
                        except: pass
                        scope = None
                    skipped.extend(seg_runs)
                    continue

                # Clone to other floors in segment
                if len(seg_runs) > 1:
                    ref_stair_el = doc.GetElement(ref_stair_id)
                    if ref_stair_el:
                        try:
                            t_ms = DB.Transaction(doc, "AI MSS {} h_idx {}".format(core_tag, h_idx))
                            t_ms.Start()
                            setup_failure_handling(t_ms, use_nuclear=True)
                            ms = _MSS.Create(ref_stair_el)
                            level_ids = HashSet[DB.ElementId]()
                            for rd in seg_runs:
                                if rd is ref_rd:
                                    continue
                                lvl_id = current_levels[rd['top_level_idx']].Id
                                if ms.CanConnectLevel(lvl_id):
                                    level_ids.Add(lvl_id)
                                else:
                                    skipped.append(rd)
                            if level_ids.Count > 0:
                                ms.ConnectLevels(level_ids)
                            t_ms.Commit()
                            t_ms.Dispose()

                            # Collect stair IDs
                            ms_elem = None
                            try:
                                ref_el = doc.GetElement(ref_stair_id)
                                if ref_el:
                                    ms_id = ref_el.MultistoryStairsId
                                    if ms_id and ms_id != DB.ElementId.InvalidElementId:
                                        ms_elem = doc.GetElement(ms_id)
                            except: pass
                            for rd in seg_runs:
                                lvl_id = current_levels[rd['base_level_idx']].Id
                                found = False
                                if ms_elem:
                                    try:
                                        stair_on = ms_elem.GetStairsOnLevel(lvl_id)
                                        if stair_on:
                                            all_stair_ids.append((stair_on.Id, core_tag))
                                            found = True
                                    except: pass
                                if not found and rd is not ref_rd:
                                    skipped.append(rd)
                            if not found and rd is ref_rd:
                                 all_stair_ids.append((ref_stair_id, core_tag))

                            self.log("  Core {} h_idx {}: cloned to {} floors.".format(
                                core_tag, h_idx, level_ids.Count))
                        except Exception as e:
                            self.log("  Core {} MSS h_idx {} FAILED: {}".format(core_tag, h_idx, e))
                            try: t_ms.RollBack()
                            except: pass
                            skipped.extend([r for r in seg_runs if r is not ref_rd])
                            # Add the prototype even if cloning failed
                            all_stair_ids.append((ref_stair_id, core_tag))
                else:
                    all_stair_ids.append((ref_stair_id, core_tag))

            # (Redundant loops removed as Unified Batching handles them above)
            pass

        # --- Post-config: set widths and comments ---
        if all_stair_ids:
            t_cfg = None
            try:
                t_cfg = DB.Transaction(doc, "AI Stair Config")
                t_cfg.Start()
                setup_failure_handling(t_cfg, use_nuclear=True)
                for stair_id, core_tag in all_stair_ids:
                    try:
                        eid = stair_id if isinstance(stair_id, DB.ElementId) else stair_id.Id
                        stair_el = doc.GetElement(eid)
                        if stair_el:
                            # Differential Sync: Store tag + fingerprint for next-run comparison
                            c_val = "AI_{}_MS".format(core_tag)
                            for rd in run_data_list:
                                # Match this element back to its manifest data
                                if rd['tag'].startswith("AI_{}".format(core_tag)):
                                    # Since we use elements collection, we might need a better match.
                                    # For individual runs, tag is unique.
                                    c_val = "{}|{}".format(rd['tag'], rd.get('fingerprint', ''))
                                    break
                            
                            safe_set_comment(stair_el, c_val)
                            
                            runs_collection = stair_el.GetStairsRuns()
                            # Clean up all auto-railings per user request
                            dep_ids = stair_el.GetDependentElements(DB.ElementCategoryFilter(DB.BuiltInCategory.OST_StairsRailing))
                            for rid in dep_ids:
                                try: doc.Delete(rid)
                                except: pass
                            
                            for sub_id in runs_collection:
                                sub_run = doc.GetElement(sub_id)
                                if sub_run: sub_run.ActualRunWidth = width_ft
                        results["elements"].append(str(eid.Value))
                    except: pass
                t_cfg.Commit()
            except Exception as e:
                self.log("Stair post-config failed: {}".format(e))
                if t_cfg:
                    try: t_cfg.RollBack()
                    except: pass

        # Final diagnostic log dumping riser measurements
        try:
            from Autodesk.Revit.DB import BuiltInParameter as BIP2 # type: ignore
            for sid, c_tag in all_stair_ids:
                s_el = doc.GetElement(sid)
                if s_el:
                    try:
                        act_r = s_el.get_Parameter(BIP2.STAIRS_ACTUAL_NUMBER_OF_RISERS).AsInteger()
                        act_h = ft_to_mm(s_el.get_Parameter(BIP2.STAIRS_ACTUAL_RISER_HEIGHT).AsDouble())
                        self.log("[Stair Diagnostic] ID={} | Risers={} | RiserH={}mm".format(sid, act_r, round(act_h,1)))
                    except Exception: pass
        except Exception: pass

        t_end = time.time()
        self.log("Stair runs: {} cores, {} stairs, {:.1f}s".format(
            len(cores), len(all_stair_ids), t_end - t_start))
        
        if self.tracker:
            self.tracker.record_created("stair_runs", len(all_stair_ids))
            
        return results

    # â"€â"€ Legacy stair methods removed â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    # The following were replaced by the single _create_stair_runs above.
    # Stubs kept for compatibility if any external code references them.

    def _create_stair_runs_multistorey(self, current_levels, results):
        """Deprecated -- redirects to _create_stair_runs."""
        self._create_stair_runs(current_levels, results)

    def _create_stair_runs_batched(self, current_levels, results):
        """Deprecated -- redirects to _create_stair_runs."""
        self._create_stair_runs(current_levels, results)

    def _create_stair_runs_legacy(self, current_levels, results):
        """Deprecated -- redirects to _create_stair_runs."""
        self._create_stair_runs(current_levels, results)

    def _create_stair_runs_legacy_for_core(self, *args, **kwargs):
        """Deprecated -- no longer used."""
        pass

    # â"€â"€ End of stair run creation methods â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

    def _REMOVED_create_stair_runs_multistorey(self, current_levels, results):
        """REMOVED: Old multistorey method kept as dead code reference.

        Uses Revit's MultistoryStairs API:
          1. Group run_data by core, then by floor height
          2. For the most common floor height, create ONE reference stair (StairsEditScope)
          3. Call MultistoryStairs.Create(stairs) + ConnectLevels() to clone
          4. Handle non-standard floor heights as individual stairs

        This is the fastest approach (~10-20s for 5 cores Ã— 50 floors).
        """
        import Autodesk.Revit.DB as DB  # type: ignore
        from Autodesk.Revit.DB.Architecture import MultistoryStairs as _MSS  # type: ignore
        from revit_mcp.utils import setup_failure_handling
        import math
        import time
        import re
        doc = self.doc

        run_data_list = getattr(self, '_stair_run_data', [])
        if not run_data_list:
            self.log("Stair runs (multistorey): no run data, skipping.")
            return

        try:
            _StairsEditScope = DB.StairsEditScope
            from Autodesk.Revit.DB.Architecture import (  # type: ignore
                StairsRun as _StairsRun,
                StairsLanding as _StairsLanding,
                StairsRunJustification as _StairsRunJust)
        except Exception as e:
            self.log("Stair runs (multistorey): CANNOT load Stairs API: {}".format(e))
            raise

        from revit_mcp.preprocessors import NuclearJoinGuard

        # --- Batch delete ALL existing AI stair elements ---
        deleted_count = 0
        ids_to_delete = []
        try:
            for el in DB.FilteredElementCollector(doc).OfCategory(
                    DB.BuiltInCategory.OST_Stairs).WhereElementIsNotElementType().ToElements():
                p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                if not p:
                    p = el.LookupParameter("Comments")
                if p and p.HasValue:
                    cmt = p.AsString()
                    if cmt and cmt.startswith("AI_") and "Stair_" in cmt:
                        ids_to_delete.append(el.Id)
            # Also delete any existing MultistoryStairs elements
            try:
                for el in DB.FilteredElementCollector(doc).OfClass(
                        _MSS).ToElements():
                    ids_to_delete.append(el.Id)
            except Exception:
                pass
        except Exception:
            pass

        if ids_to_delete:
            t_del = DB.Transaction(doc, "AI Stair MS Delete")
            t_del.Start()
            setup_failure_handling(t_del, use_nuclear=True)
            for eid in ids_to_delete:
                try:
                    doc.Delete(eid)
                    deleted_count += 1
                except Exception:
                    pass
            t_del.Commit()
            t_del.Dispose()
            self.log("Stair runs (multistorey): deleted {} old elements.".format(deleted_count))

        spec = getattr(self, '_stair_spec', {})
        width_mm = spec.get("width_of_flight", 1500)
        landing_mm = spec.get("landing_width", 1500)
        spec_tread_mm = spec.get("tread", 300)
        spec_riser_mm = spec.get("riser", 150)

        # Pre-configure stair type
        try:
            stair_types = DB.FilteredElementCollector(doc).OfClass(
                DB.Architecture.StairsType).ToElements()
            if not stair_types:
                stair_types = DB.FilteredElementCollector(doc).OfCategory(
                    DB.BuiltInCategory.OST_Stairs).OfClass(DB.ElementType).ToElements()
            if stair_types:
                t_pre = DB.Transaction(doc, "AI Stair Type Pre-Config MS")
                t_pre.Start()
                for st in stair_types:
                    try:
                        p_riser = st.get_Parameter(
                            DB.BuiltInParameter.STAIRS_ATTR_MAX_RISER_HEIGHT)
                        if p_riser and not p_riser.IsReadOnly:
                            p_riser.Set(mm_to_ft(spec_riser_mm))
                        for tp in st.GetOrderedParameters():
                            n = tp.Definition.Name.lower()
                            if 'tread' in n and 'depth' in n and not tp.IsReadOnly:
                                tp.Set(mm_to_ft(spec_tread_mm))
                                break
                    except Exception:
                        pass
                t_pre.Commit()
                t_pre.Dispose()
        except Exception:
            pass

        # --- Group run_data by core ---
        cores = {}
        for rd in run_data_list:
            tag = rd['tag']
            m = re.match(r'AI_(Stair_\d+)_L\d+_Run', tag)
            core_tag = m.group(1) if m else tag
            cores.setdefault(core_tag, []).append(rd)
        for core_tag in cores:
            cores[core_tag].sort(key=lambda r: r['base_level_idx'])

        self.log("Stair runs (multistorey): {} cores, {} total runs.".format(
            len(cores), len(run_data_list)))

        t_start = time.time()
        created_count = 0
        all_stair_ids = []  # (stair_id, core_tag) for post-config

        tread_ft = mm_to_ft(spec_tread_mm)
        hw = mm_to_ft(width_mm) / 2.0
        landing_ft = mm_to_ft(landing_mm)

        for core_tag, core_runs in cores.items():
            # --- Group floors by height for this core ---
            height_groups = {}  # rounded_height_mm -> [run_data, ...]
            for rd in core_runs:
                bi = rd['base_level_idx']
                ti = rd['top_level_idx']
                if bi >= len(current_levels) or ti >= len(current_levels):
                    continue
                h_ft = current_levels[ti].ProjectElevation - current_levels[bi].ProjectElevation
                if h_ft <= 0:
                    continue
                h_mm = round(h_ft * 304.8)
                height_groups.setdefault(h_mm, []).append(rd)

            if not height_groups:
                continue

            # Find the most common floor height (typical)
            typical_h_mm = max(height_groups, key=lambda h: len(height_groups[h]))
            typical_runs = height_groups[typical_h_mm]
            non_typical_runs = []
            for h, runs in height_groups.items():
                if h != typical_h_mm:
                    non_typical_runs.extend(runs)

            self.log("  Core {}: {} typical floors ({}mm), {} non-typical floors.".format(
                core_tag, len(typical_runs), typical_h_mm, len(non_typical_runs)))

            # --- Step 1: Create ONE reference stair for typical height ---
            ref_rd = typical_runs[0]  # Use first typical floor as reference
            ref_base_idx = ref_rd['base_level_idx']
            ref_top_idx = ref_rd['top_level_idx']
            ref_base_lvl = current_levels[ref_base_idx]
            ref_top_lvl = current_levels[ref_top_idx]

            scope = None
            t = None
            ref_stair_id = None
            try:
                scope = _StairsEditScope(doc, "AI Stair MS Ref")
                ref_stair_id = scope.Start(ref_base_lvl.Id, ref_top_lvl.Id)

                t = DB.Transaction(doc, "AI Stair MS Ref Dogleg")
                t.Start()
                setup_failure_handling(t)

                # Build the reference dogleg
                self._build_dogleg_in_scope(
                    doc, ref_stair_id, ref_rd, ref_base_lvl, current_levels,
                    tread_ft, hw, landing_ft, spec_riser_mm, spec_tread_mm,
                    _StairsRun, _StairsLanding, _StairsRunJust)

                t.Commit()
                t = None
                scope.Commit(NuclearJoinGuard(doc))
                scope.Dispose() # Explicit disposal to release Revit lock
                scope = None

                self.log("  Core {}: reference stair created (L{}-L{}).".format(
                    core_tag, ref_base_idx + 1, ref_top_idx + 1))

                # --- EW rotation: rotate the whole Stairs element 90° ---
                # The stair was created NS (CreateStraightRun ignores line direction).
                # ElementTransformUtils.RotateElement rotates around a vertical axis
                # through the staircase centre, matching the enclosure wall rotation.
                _rot_deg_rd = ref_rd.get('_rot_deg', 0.0)
                if _rot_deg_rd and abs(_rot_deg_rd) > 0.1:
                    import math as _rot_math
                    _rot_centre = ref_rd.get('_rot_centre_mm')
                    if _rot_centre:
                        _rcx_ft = mm_to_ft(_rot_centre[0])
                        _rcy_ft = mm_to_ft(_rot_centre[1])
                    else:
                        # Fallback: use midpoint of flight_1 as centre
                        _f1s = ref_rd['flight_1']['start']
                        _f1e = ref_rd['flight_1']['end']
                        _rcx_ft = mm_to_ft((_f1s[0] + _f1e[0]) / 2.0)
                        _rcy_ft = mm_to_ft((_f1s[1] + _f1e[1]) / 2.0)
                    _rot_rad = _rot_math.radians(_rot_deg_rd)
                    _rot_axis = DB.Line.CreateBound(
                        DB.XYZ(_rcx_ft, _rcy_ft, 0.0),
                        DB.XYZ(_rcx_ft, _rcy_ft, 1.0))
                    t_rot = DB.Transaction(doc, "AI Stair Rotate")
                    try:
                        t_rot.Start()
                        setup_failure_handling(t_rot)
                        from Autodesk.Revit.DB import ElementTransformUtils  # type: ignore
                        ElementTransformUtils.RotateElement(doc, ref_stair_id, _rot_axis, _rot_rad)
                        t_rot.Commit()
                        self.log("  Core {}: stair rotated {}° around ({:.0f},{:.0f})mm".format(
                            core_tag, _rot_deg_rd, _rot_centre[0] if _rot_centre else 0,
                            _rot_centre[1] if _rot_centre else 0))
                    except Exception as _re:
                        self.log("  Core {}: stair rotation failed (non-fatal): {}".format(core_tag, _re))
                        try: t_rot.RollBack()
                        except: pass
                    finally:
                        try: t_rot.Dispose()
                        except: pass

            except Exception as e:
                self.log("  Core {} reference stair FAILED: {}".format(core_tag, e))
                if t:
                    try: t.RollBack()
                    except: pass
                if scope:
                    try:
                        scope.RollBack()
                        scope.Dispose()
                    except: pass
                raise  # Let the cascade handle it

            # --- Step 2: MultistoryStairs.Create + ConnectLevels ---
            ref_stair_el = doc.GetElement(ref_stair_id)
            if not ref_stair_el:
                self.log("  Core {}: could not retrieve reference stair element.".format(core_tag))
                continue

            skipped_typical_runs = []
            try:
                t_ms = DB.Transaction(doc, "AI MultistoryStairs {}".format(core_tag))
                t_ms.Start()
                setup_failure_handling(t_ms, use_nuclear=True)

                ms = _MSS.Create(ref_stair_el)

                # Collect level IDs for all OTHER typical floors (skip the reference)
                from System.Collections.Generic import HashSet  # type: ignore
                level_ids_to_connect = HashSet[DB.ElementId]()
                for rd in typical_runs:
                    if rd is ref_rd:
                        continue
                    bi = rd['base_level_idx']
                    if bi >= len(current_levels):
                        continue
                    lvl_id = current_levels[bi].Id
                    if ms.CanConnectLevel(lvl_id):
                        level_ids_to_connect.Add(lvl_id)
                    else:
                        skipped_typical_runs.append(rd)
                        self.log("  Core {}: L{} cannot be connected (CanConnectLevel=false), "
                                 "will create individually.".format(core_tag, bi + 1))

                if level_ids_to_connect.Count > 0:
                    ms.ConnectLevels(level_ids_to_connect)
                    self.log("  Core {}: ConnectLevels OK -- {} typical floors cloned.".format(
                        core_tag, level_ids_to_connect.Count))

                t_ms.Commit()
                t_ms.Dispose()

                # Retrieve the MultistoryStairs element via the reference stair
                ms_elem = None
                try:
                    ref_el = doc.GetElement(ref_stair_id)
                    if ref_el:
                        ms_id = ref_el.MultistoryStairsId
                        if ms_id and ms_id != DB.ElementId.InvalidElementId:
                            ms_elem = doc.GetElement(ms_id)
                except Exception:
                    pass

                # Collect individual stair IDs using GetStairsOnLevel.
                # GetAllStairsIds() only returns one per height-group (prototype),
                # NOT one per connected level.  GetStairsOnLevel returns the
                # actual stair ElementId at each base level.
                # Also verify each expected stair was created -- any missing
                # ones get added to the individual-creation list.
                expected_runs = [r for r in typical_runs if r not in skipped_typical_runs]
                for rd in expected_runs:
                    bi = rd['base_level_idx']
                    if bi >= len(current_levels):
                        continue
                    lvl_id = current_levels[bi].Id
                    found = False
                    if ms_elem:
                        try:
                            stair_on_level = ms_elem.GetStairsOnLevel(lvl_id)
                            # GetStairsOnLevel returns a Stairs element (not ElementId)
                            if stair_on_level:
                                all_stair_ids.append((stair_on_level.Id, core_tag))
                                found = True
                        except Exception:
                            pass
                    if not found:
                        skipped_typical_runs.append(rd)
                        self.log("  Core {}: L{} stair missing after ConnectLevels, "
                                 "will create individually.".format(core_tag, bi + 1))

                self.log("  Core {}: {} stairs found in MultistoryStairs, {} need individual creation.".format(
                    core_tag, len(all_stair_ids), len(skipped_typical_runs)))
                created_count += 1

            except Exception as e:
                self.log("  Core {} MultistoryStairs.Create/ConnectLevels FAILED: {}".format(
                    core_tag, e))
                try: t_ms.RollBack()
                except: pass
                raise

            # --- Step 3: Handle non-typical + unconnectable floors individually ---
            all_individual_runs = list(non_typical_runs) + skipped_typical_runs
            for rd in all_individual_runs:
                bi = rd['base_level_idx']
                ti = rd['top_level_idx']
                if bi >= len(current_levels) or ti >= len(current_levels):
                    continue
                b_lvl = current_levels[bi]
                t_lvl = current_levels[ti]

                scope2 = None
                t2 = None
                try:
                    scope2 = _StairsEditScope(doc, "AI Stair MS NonTyp")
                    nt_stair_id = scope2.Start(b_lvl.Id, t_lvl.Id)

                    t2 = DB.Transaction(doc, "AI Stair MS NonTyp Dogleg")
                    t2.Start()
                    setup_failure_handling(t2)

                    self._build_dogleg_in_scope(
                        doc, nt_stair_id, rd, b_lvl, current_levels,
                        tread_ft, hw, landing_ft, spec_riser_mm, spec_tread_mm,
                        _StairsRun, _StairsLanding, _StairsRunJust)

                    t2.Commit()
                    t2 = None
                    scope2.Commit(NuclearJoinGuard(doc))
                    scope2 = None

                    # EW rotation for non-typical stairs
                    _nt_rot_deg = rd.get('_rot_deg', 0.0)
                    if _nt_rot_deg and abs(_nt_rot_deg) > 0.1:
                        import math as _nt_math
                        _nt_centre = rd.get('_rot_centre_mm')
                        _nt_rcx = mm_to_ft(_nt_centre[0]) if _nt_centre else 0.0
                        _nt_rcy = mm_to_ft(_nt_centre[1]) if _nt_centre else 0.0
                        _nt_axis = DB.Line.CreateBound(
                            DB.XYZ(_nt_rcx, _nt_rcy, 0.0),
                            DB.XYZ(_nt_rcx, _nt_rcy, 1.0))
                        t_nt_rot = DB.Transaction(doc, "AI Stair NT Rotate")
                        try:
                            t_nt_rot.Start()
                            setup_failure_handling(t_nt_rot)
                            from Autodesk.Revit.DB import ElementTransformUtils  # type: ignore
                            ElementTransformUtils.RotateElement(
                                doc, nt_stair_id, _nt_axis, _nt_math.radians(_nt_rot_deg))
                            t_nt_rot.Commit()
                        except Exception as _ntre:
                            self.log("  NT rotate failed (non-fatal): {}".format(_ntre))
                            try: t_nt_rot.RollBack()
                            except: pass
                        finally:
                            try: t_nt_rot.Dispose()
                            except: pass

                    all_stair_ids.append((nt_stair_id, core_tag))
                    self.log("  Core {}: non-typical stair L{}-L{} created.".format(
                        core_tag, bi + 1, ti + 1))

                except Exception as e:
                    self.log("  Core {} non-typical L{} FAILED: {}".format(core_tag, bi + 1, e))
                finally:
                    if t2:
                        try: t2.RollBack()
                        except: pass
                    if scope2:
                        try: 
                            scope2.RollBack()
                            scope2.Dispose()
                        except: pass

        # --- Batch post-config ---
        if all_stair_ids:
            t_cfg = None
            try:
                t_cfg = DB.Transaction(doc, "AI Stair MS Config")
                t_cfg.Start()
                setup_failure_handling(t_cfg, use_nuclear=True)
                width_ft = mm_to_ft(width_mm)
                for stair_id, core_tag in all_stair_ids:
                    try:
                        eid = stair_id if isinstance(stair_id, DB.ElementId) else stair_id.Id
                        stair_el = doc.GetElement(eid)
                        if stair_el:
                            safe_set_comment(stair_el, "AI_{}_MS".format(core_tag))
                            
                            runs_collection = stair_el.GetStairsRuns()
                            
                            # Clean up all auto-railings per user request
                            dep_ids = stair_el.GetDependentElements(DB.ElementCategoryFilter(DB.BuiltInCategory.OST_StairsRailing))
                            for rid in dep_ids:
                                try:
                                    doc.Delete(rid)
                                except Exception:
                                    pass
                            
                            for sub_id in runs_collection:
                                sub_run = doc.GetElement(sub_id)
                                if sub_run:
                                    sub_run.ActualRunWidth = width_ft
                        results["elements"].append(str(eid.Value))
                    except Exception:
                        pass
                t_cfg.Commit()
            except Exception as cfg_err:
                self.log("Stair post-config failed: {}".format(cfg_err))
                if t_cfg:
                    try: t_cfg.RollBack()
                    except: pass
            finally:
                if t_cfg:
                    try: t_cfg.Dispose()
                    except: pass

        elapsed = time.time() - t_start
        self.log("Stair runs: {} stairs total. Time: {:.1f}s".format(len(all_stair_ids), elapsed))
        if self.tracker: self.tracker.record_created("stair_runs", len(all_stair_ids))

    def _build_dogleg_in_scope(self, doc, stair_id, rd, base_lvl, current_levels,
                                tread_ft, hw, landing_ft, spec_riser_mm, spec_tread_mm,
                                _StairsRun, _StairsLanding, _StairsRunJust):
        """Build a single dogleg stair inside an already-open StairsEditScope.

        Creates ONE dogleg: Run A (entry side → far side), mid-landing (U-turn),
        Run B (far side → entry side). Used by typical single-pair floors.

        When _coords_prerotated=True the flight_1/flight_2 start/end coords are
        already in final Revit world coordinates (EW or NS) and are passed
        directly to CreateStraightRun.  The landing polygon is derived as the
        bounding box between the end of Run A and the start of Run B.

        Mid-landing elevation is computed EXPLICITLY as a_risers * riser_h_ft.
        We do NOT use run_a.TopElevation because Revit adds a nosing offset
        that makes it 1 riser height taller than the actual rise of the run.
        """
        import Autodesk.Revit.DB as DB  # type: ignore

        base_z = base_lvl.ProjectElevation

        f1 = rd['flight_1']
        f2 = rd['flight_2']

        flight_list = rd.get('flight_list', [])
        if len(flight_list) >= 2:
            a_risers = flight_list[0]
            b_risers = flight_list[1]
        elif 'actual_risers_a' in rd:
            a_risers = rd['actual_risers_a']
            b_risers = rd.get('actual_risers_b', a_risers)
        elif len(flight_list) == 1:
            a_risers = flight_list[0]
            b_risers = 0
        else:
            a_risers = rd.get('risers_per_flight', 14)
            b_risers = a_risers

        total_risers = a_risers + b_risers

        try:
            top_lvl = rd.get('_pair_top_level') or current_levels[rd['top_level_idx']]
            scope_height_ft = round(top_lvl.ProjectElevation - base_z, 8)
        except Exception:
            scope_height_ft = round(mm_to_ft(spec_riser_mm * total_risers), 8)
        riser_h_ft = (scope_height_ft / total_risers) if total_risers > 0 else mm_to_ft(spec_riser_mm)

        try:
            stair_el = doc.GetElement(stair_id)
            if stair_el:
                from Autodesk.Revit.DB import BuiltInParameter as BIP2  # type: ignore
                p_desired = stair_el.get_Parameter(BIP2.STAIRS_DESIRED_NUMBER_OF_RISERS)
                if p_desired and not p_desired.IsReadOnly:
                    p_desired.Set(total_risers)
        except Exception:
            pass

        a_run_len = max(a_risers - 1, 1) * tread_ft

        # ── Standard build path (NS coordinates) ─────────────────────────────
        # EW staircases use the same NS build; revit_workers rotates the whole
        # Stairs element 90° via ElementTransformUtils.RotateElement() after creation.
        import math as _dg_math
        _dg_rot_rad = rd.get('_global_rotation_rad', 0.0)
        if _dg_rot_rad:
            _dg_cos = _dg_math.cos(_dg_rot_rad)
            _dg_sin = _dg_math.sin(_dg_rot_rad)
            _dg_cx  = rd['_rotation_cx_ft']
            _dg_cy  = rd['_rotation_cy_ft']
            def _rp(pt):
                dx = pt.X - _dg_cx; dy = pt.Y - _dg_cy
                return DB.XYZ(_dg_cx + dx*_dg_cos - dy*_dg_sin,
                               _dg_cy + dx*_dg_sin + dy*_dg_cos, pt.Z)
        else:
            def _rp(pt):
                return pt

        f1_cx = mm_to_ft(f1['start'][0])
        f2_cx = mm_to_ft(f2['start'][0])
        flight_y_start_ft = mm_to_ft(f1['start'][1])
        f1_y_end = mm_to_ft(f1['end'][1])
        is_rotated = (f1_y_end < flight_y_start_ft)
        left_cx  = min(f1_cx, f2_cx)
        right_cx = max(f1_cx, f2_cx)
        land_x_left  = left_cx  - hw
        land_x_right = right_cx + hw

        # --- Run A: direction depends on 180° rotation flag ---
        p_as = _rp(DB.XYZ(round(f1_cx, 8), round(flight_y_start_ft, 8), base_z))
        if is_rotated:
            p_ae = _rp(DB.XYZ(round(f1_cx, 8), round(flight_y_start_ft - a_run_len, 8), base_z))
        else:
            p_ae = _rp(DB.XYZ(round(f1_cx, 8), round(flight_y_start_ft + a_run_len, 8), base_z))

        if a_run_len < 0.001:
            self.log("  [Stair DIAGNOSTIC] {} - Run A length too short! a_risers={}, total_risers={}, total_h={:.4f}ft".format(
                rd['tag'], a_risers, total_risers, scope_height_ft))

        try:
            _StairsRun.CreateStraightRun(doc, stair_id, DB.Line.CreateBound(p_as, p_ae), _StairsRunJust.Center)
        except Exception as e:
            self.log("  [Stair DIAGNOSTIC FAILED] {} Run A: p_as={}, p_ae={}, error={}".format(
                rd['tag'], p_as, p_ae, e))
            raise e

        mid_elev_rel = a_risers * riser_h_ft
        mid_elev_abs = base_z + mid_elev_rel

        if b_risers <= 0:
            return

        if "dyn_landing_w_mm" in rd:
            dyn_landing_ft = mm_to_ft(rd["dyn_landing_w_mm"])
        else:
            dyn_landing_ft = landing_ft

        # --- Mid-landing (U-turn at far side of Run A) ---
        if is_rotated:
            land_y_top = round(flight_y_start_ft - a_run_len, 8)
            land_y_bot = round(land_y_top - dyn_landing_ft, 8)
        else:
            land_y_bot = round(flight_y_start_ft + a_run_len, 8)
            land_y_top = round(land_y_bot + dyn_landing_ft, 8)

        lp1 = _rp(DB.XYZ(round(land_x_left,  8), land_y_bot, mid_elev_abs))
        lp2 = _rp(DB.XYZ(round(land_x_right, 8), land_y_bot, mid_elev_abs))
        lp3 = _rp(DB.XYZ(round(land_x_right, 8), land_y_top, mid_elev_abs))
        lp4 = _rp(DB.XYZ(round(land_x_left,  8), land_y_top, mid_elev_abs))

        mid_loop = DB.CurveLoop()
        mid_loop.Append(DB.Line.CreateBound(lp1, lp2))
        mid_loop.Append(DB.Line.CreateBound(lp2, lp3))
        mid_loop.Append(DB.Line.CreateBound(lp3, lp4))
        mid_loop.Append(DB.Line.CreateBound(lp4, lp1))
        _StairsLanding.CreateSketchedLanding(doc, stair_id, mid_loop, mid_elev_rel)

        # --- Run B: returns toward starting side ---
        b_run_len = max(b_risers - 1, 1) * tread_ft
        if is_rotated:
            p_bs = _rp(DB.XYZ(round(f2_cx, 8), round(flight_y_start_ft - a_run_len, 8), mid_elev_abs))
            p_be = _rp(DB.XYZ(round(f2_cx, 8), round(flight_y_start_ft - a_run_len + b_run_len, 8), mid_elev_abs))
        else:
            p_bs = _rp(DB.XYZ(round(f2_cx, 8), land_y_bot, mid_elev_abs))
            p_be = _rp(DB.XYZ(round(f2_cx, 8), round(land_y_bot - b_run_len, 8), mid_elev_abs))
        
        # Diagnostic Log
        if b_run_len < 0.001:
            self.log("  [Stair DIAGNOSTIC] {} - Run B length too short! b_risers={}".format(rd['tag'], b_risers))

        try:
            line_b = DB.Line.CreateBound(p_bs, p_be)
            run_b = _StairsRun.CreateStraightRun(doc, stair_id, line_b, _StairsRunJust.Center)
        except Exception as e:
            self.log("  [Stair DIAGNOSTIC FAILED] {} Run B: p_bs={}, p_be={}, error={}".format(
                rd['tag'], p_bs, p_be, e))
            raise e
        
        try:
            p_nr_b = run_b.LookupParameter("Number of Risers") or run_b.LookupParameter("Actual Number of Risers")
            if p_nr_b and not p_nr_b.IsReadOnly: p_nr_b.Set(b_risers)
        except Exception: pass

        # Detailed logging -- what WE asked for AND what Revit actually set
        try:
            flight_w_mm = hw * 2.0 * 304.8
            tread_mm = tread_ft * 304.8
            run_a_len_mm = a_run_len * 304.8
            run_b_len_mm = b_run_len * 304.8
            mid_elev_mm = mid_elev_rel * 304.8
            
            # Read back what Revit actually stored on the stair element
            actual_risers_revit = "?"
            actual_riser_h_revit = "?"
            actual_tread_revit = "?"
            desired_risers_readonly = "?"
            try:
                from Autodesk.Revit.DB import BuiltInParameter as BIP2 # type: ignore
                se = doc.GetElement(stair_id)
                if se:
                    p_act_r = se.get_Parameter(BIP2.STAIRS_RUN_ACTUAL_NUMBER_OF_RISERS)
                    if p_act_r: actual_risers_revit = p_act_r.AsInteger()
                    p_act_rh = se.get_Parameter(BIP2.STAIRS_RUN_ACTUAL_RISER_HEIGHT)
                    if p_act_rh: actual_riser_h_revit = "{:.1f}mm".format(p_act_rh.AsDouble() * 304.8)
                    p_act_t = se.get_Parameter(BIP2.STAIRS_RUN_ACTUAL_TREAD_DEPTH)
                    if p_act_t: actual_tread_revit = "{:.1f}mm".format(p_act_t.AsDouble() * 304.8)
                    p_des = se.get_Parameter(BIP2.STAIRS_DESIRED_NUMBER_OF_RISERS)
                    if p_des: desired_risers_readonly = "RO={}".format(p_des.IsReadOnly)
            except Exception as rb_e:
                actual_risers_revit = "err:{}".format(rb_e)

            log_str = (
                "    [Stair Diagnostic] Dogleg ID={id} | Z_Start={z:.0f}mm | MidElev={me:.0f}mm | "
                "ASKED: A={ra}r+B={rb}r={tr}total Tread={trd:.0f}mm RunW={w:.0f}mm | "
                "REVIT_ACTUAL: risers={ar} riserH={arh} tread={at} DesiredRO={dro}"
            ).format(
                id=stair_id.Value if hasattr(stair_id, "Value") else str(stair_id),
                z=base_lvl.ProjectElevation * 304.8, me=mid_elev_mm,
                ra=a_risers, rb=b_risers, tr=a_risers + b_risers,
                trd=tread_mm, w=flight_w_mm,
                ar=actual_risers_revit, arh=actual_riser_h_revit,
                at=actual_tread_revit, dro=desired_risers_readonly
            )
            self.log(log_str)
        except Exception as e:
            self.log("    [Stair Diagnostic] Logging failed: {}".format(e))

    def _create_stair_runs_batched(self, current_levels, results):
        """BATCHED: One StairsEditScope per core spanning all floors.

        Groups run_data by staircase core (e.g. all AI_Stair_1_L*_Run entries
        become one multi-storey stair element).  All runs + landings for all
        floors are added inside a single scope, committed once.
        """
        import Autodesk.Revit.DB as DB  # type: ignore
        from revit_mcp.utils import setup_failure_handling
        import math
        import time
        doc = self.doc

        run_data_list = getattr(self, '_stair_run_data', [])
        if not run_data_list:
            self.log("Stair runs (batched): no run data available, skipping.")
            return

        try:
            _StairsEditScope = DB.StairsEditScope
            from Autodesk.Revit.DB.Architecture import (  # type: ignore
                StairsRun as _StairsRun,
                StairsLanding as _StairsLanding,
                StairsRunJustification as _StairsRunJust)
        except Exception as e:
            self.log("Stair runs (batched): CANNOT load Stairs API: {}".format(e))
            raise

        from revit_mcp.preprocessors import NuclearJoinGuard

        # --- Batch delete ALL existing AI stair elements in ONE transaction ---
        deleted_count = 0
        ids_to_delete = []
        try:
            for el in DB.FilteredElementCollector(doc).OfCategory(
                    DB.BuiltInCategory.OST_Stairs).WhereElementIsNotElementType().ToElements():
                p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                if not p:
                    p = el.LookupParameter("Comments")
                if p and p.HasValue:
                    cmt = p.AsString()
                    if cmt and cmt.startswith("AI_") and "Stair_" in cmt:
                        ids_to_delete.append(el.Id)
        except Exception:
            pass

        if ids_to_delete:
            t_del = DB.Transaction(doc, "AI Stair Batch Delete")
            t_del.Start()
            setup_failure_handling(t_del, use_nuclear=True)
            for eid in ids_to_delete:
                try:
                    doc.Delete(eid)
                    deleted_count += 1
                except Exception:
                    pass
            t_del.Commit()
            t_del.Dispose()
            self.log("Stair runs (batched): deleted {} old stair elements in 1 transaction.".format(deleted_count))

        spec = getattr(self, '_stair_spec', {})
        width_mm = spec.get("width_of_flight", 1500)
        landing_mm = spec.get("landing_width", 1500)
        spec_tread_mm = spec.get("tread", 300)
        spec_riser_mm = spec.get("riser", 150)

        # --- Pre-configure stair type ---
        try:
            stair_types = DB.FilteredElementCollector(doc).OfClass(
                DB.Architecture.StairsType).ToElements()
            if not stair_types:
                stair_types = DB.FilteredElementCollector(doc).OfCategory(
                    DB.BuiltInCategory.OST_Stairs).OfClass(DB.ElementType).ToElements()
            if stair_types:
                t_pre = DB.Transaction(doc, "AI Stair Type Pre-Config")
                t_pre.Start()
                for st in stair_types:
                    try:
                        p_riser = st.get_Parameter(
                            DB.BuiltInParameter.STAIRS_ATTR_MAX_RISER_HEIGHT)
                        if p_riser and not p_riser.IsReadOnly:
                            p_riser.Set(mm_to_ft(spec_riser_mm))
                        for tp in st.GetOrderedParameters():
                            n = tp.Definition.Name.lower()
                            if 'tread' in n and 'depth' in n and not tp.IsReadOnly:
                                tp.Set(mm_to_ft(spec_tread_mm))
                                break
                    except Exception:
                        pass
                t_pre.Commit()
                t_pre.Dispose()
        except Exception as e:
            self.log("Stair runs (batched): type pre-config note: {}".format(e))

        # --- Group run_data by staircase core ---
        # Tag format: "AI_Stair_N_LX_Run" -- core identifier is "Stair_N"
        import re
        cores = {}  # core_tag -> [run_data, ...] sorted by base_level_idx
        for rd in run_data_list:
            tag = rd['tag']
            m = re.match(r'AI_(Stair_\d+)_L\d+_Run', tag)
            core_tag = m.group(1) if m else tag
            cores.setdefault(core_tag, []).append(rd)
        # Sort each core's runs by base level
        for core_tag in cores:
            cores[core_tag].sort(key=lambda r: r['base_level_idx'])

        self.log("Stair runs (batched): {} cores, {} total runs.".format(
            len(cores), len(run_data_list)))

        created_count = 0
        created_stair_ids = []  # (stair_id, core_tag, width_mm) for post-config
        t_start = time.time()

        for core_tag, core_runs in cores.items():
            # Find overall base and top levels for this core
            base_idx = core_runs[0]['base_level_idx']
            top_idx = core_runs[-1]['top_level_idx']
            if base_idx >= len(current_levels) or top_idx >= len(current_levels):
                self.log("  Core {}: level index out of range, skipping.".format(core_tag))
                continue

            base_lvl = current_levels[base_idx]
            top_lvl = current_levels[top_idx]

            scope = None
            t = None
            stair_id = None
            succeeded = False
            try:
                scope = _StairsEditScope(doc, "AI Stair Batched")
                stair_id = scope.Start(base_lvl.Id, top_lvl.Id)

                t = DB.Transaction(doc, "AI Stair Dogleg Batched")
                t.Start()
                setup_failure_handling(t)

                # Push spec riser onto stair type
                stair_el = doc.GetElement(stair_id)
                if stair_el:
                    try:
                        stair_type = doc.GetElement(stair_el.GetTypeId())
                        if stair_type:
                            p_riser = stair_type.get_Parameter(
                                DB.BuiltInParameter.STAIRS_ATTR_MAX_RISER_HEIGHT)
                            if p_riser and not p_riser.IsReadOnly:
                                p_riser.Set(mm_to_ft(spec_riser_mm))
                    except Exception:
                        pass

                tread_ft = mm_to_ft(spec_tread_mm)
                hw = mm_to_ft(width_mm) / 2.0
                landing_ft = mm_to_ft(landing_mm)

                # Initial pass: calculate total risers for the ENTIRE core height
                total_core_risers = 0
                for rd in core_runs:
                    rd_base_lvl = current_levels[rd['base_level_idx']]
                    rd_top_lvl = current_levels[rd['top_level_idx']]
                    fh_mm = round((rd_top_lvl.ProjectElevation - rd_base_lvl.ProjectElevation) * 304.8)
                    n_flights = rd.get('num_flight_pairs', 1) * 2
                    f_risers = int(math.ceil(fh_mm / float(spec_riser_mm)))
                    if n_flights > 0 and f_risers % n_flights != 0:
                        f_risers = int(math.ceil(f_risers / float(n_flights))) * n_flights
                    total_core_risers += f_risers

                stair_el = doc.GetElement(stair_id)
                if stair_el:
                    try:
                        p_desired = stair_el.get_Parameter(DB.BuiltInParameter.STAIRS_DESIRED_NUMBER_OF_RISERS)
                        if p_desired and not p_desired.IsReadOnly:
                            p_desired.Set(total_core_risers)
                    except: pass

                # Process each floor's runs within this single scope
                for rd in core_runs:
                    rd_base_idx = rd['base_level_idx']
                    rd_top_idx = rd['top_level_idx']
                    if rd_base_idx >= len(current_levels) or rd_top_idx >= len(current_levels):
                        continue

                    rd_base_lvl = current_levels[rd_base_idx]
                    rd_top_lvl = current_levels[rd_top_idx]
                    floor_h_ft = rd_top_lvl.ProjectElevation - rd_base_lvl.ProjectElevation
                    if floor_h_ft <= 0:
                        continue
                    base_z = rd_base_lvl.ProjectElevation

                    num_pairs = rd.get('num_flight_pairs', 1)
                    f1 = rd['flight_1']
                    f2 = rd['flight_2']
                    f1_cx = mm_to_ft(f1['start'][0])
                    f2_cx = mm_to_ft(f2['start'][0])
                    flight_y_start_ft = mm_to_ft(f1['start'][1])

                    land_x_left = f1_cx - hw
                    land_x_right = f2_cx + hw

                    # Re-calculate riser distribution for THIS floor
                    floor_h_mm = round(floor_h_ft * 304.8)
                    curr_floor_risers = int(math.ceil(floor_h_mm / float(spec_riser_mm)))
                    num_flights = num_pairs * 2
                    if num_flights > 0 and curr_floor_risers % num_flights != 0:
                        curr_floor_risers = int(math.ceil(curr_floor_risers / float(num_flights))) * num_flights

                    rpf = rd.get('risers_per_flight', max(curr_floor_risers // 2, 1))
                    flight_risers_list = [rpf] * num_flights

                    flight_pairs = []
                    for fi in range(0, len(flight_risers_list), 2):
                        a = flight_risers_list[fi]
                        b = flight_risers_list[fi + 1] if fi + 1 < len(flight_risers_list) else 0
                        if a > 0:
                            flight_pairs.append((a, b))

                    self.log("  Stair {}: total_risers={} rpf={} pairs={} "
                             "flights={} floor_h={:.0f}mm".format(
                        rd['tag'], total_risers, rpf, len(flight_pairs),
                        flight_risers_list, floor_h_mm))

                    # Elevation tracking is RELATIVE to the stair's base level
                    # For multi-storey: offset from the overall base
                    storey_base_rel = base_z - base_lvl.ProjectElevation
                    current_elev_rel = storey_base_rel

                    for p_idx, (a_risers, b_risers) in enumerate(flight_pairs):
                        current_elev_abs = base_lvl.ProjectElevation + current_elev_rel

                        # NOTE: No explicit intermediate landing between pairs.
                        # Revit auto-generates landings between consecutive runs.
                        # Manually creating one here caused overlapping sketch
                        # lines ("Highlighted lines overlap" warning + 20s stall).

                        # Run A: left side, going +Y
                        a_run_len = max(a_risers - 1, 1) * tread_ft
                        p_as = DB.XYZ(f1_cx, flight_y_start_ft, current_elev_abs)
                        p_ae = DB.XYZ(f1_cx, flight_y_start_ft + a_run_len,
                                      current_elev_abs)
                        line_a = DB.Line.CreateBound(p_as, p_ae)
                        run_a = _StairsRun.CreateStraightRun(
                            doc, stair_id, line_a, _StairsRunJust.Center)

                        mid_elev_rel = run_a.TopElevation
                        mid_elev_abs = base_lvl.ProjectElevation + mid_elev_rel

                        # Mid-landing (U-turn at back of shaft)
                        land_y_bot = flight_y_start_ft + a_run_len
                        land_y_top = land_y_bot + landing_ft
                        lp1 = DB.XYZ(land_x_left,  land_y_bot, mid_elev_abs)
                        lp2 = DB.XYZ(land_x_right, land_y_bot, mid_elev_abs)
                        lp3 = DB.XYZ(land_x_right, land_y_top, mid_elev_abs)
                        lp4 = DB.XYZ(land_x_left,  land_y_top, mid_elev_abs)
                        mid_loop = DB.CurveLoop()
                        mid_loop.Append(DB.Line.CreateBound(lp1, lp2))
                        mid_loop.Append(DB.Line.CreateBound(lp2, lp3))
                        mid_loop.Append(DB.Line.CreateBound(lp3, lp4))
                        mid_loop.Append(DB.Line.CreateBound(lp4, lp1))
                        _StairsLanding.CreateSketchedLanding(
                            doc, stair_id, mid_loop, mid_elev_rel)

                        # Run B: right side, going -Y
                        if b_risers <= 0:
                            current_elev_rel = mid_elev_rel
                            continue

                        b_run_len = max(b_risers - 1, 1) * tread_ft
                        p_bs = DB.XYZ(f2_cx, flight_y_start_ft + a_run_len,
                                      mid_elev_abs)
                        p_be = DB.XYZ(f2_cx,
                                      flight_y_start_ft + a_run_len - b_run_len,
                                      mid_elev_abs)
                        line_b = DB.Line.CreateBound(p_bs, p_be)
                        run_b = _StairsRun.CreateStraightRun(
                            doc, stair_id, line_b, _StairsRunJust.Center)

                        current_elev_rel = run_b.TopElevation

                # Commit the entire multi-storey stair in one go
                t.Commit()
                t = None
                scope.Commit(NuclearJoinGuard(doc))
                scope = None
                succeeded = True
                created_stair_ids.append((stair_id, core_tag, width_mm))
                created_count += 1
                self.log("  Core {} committed ({} floors in 1 scope).".format(
                    core_tag, len(core_runs)))

            except Exception as e:
                import traceback
                self.log("  Core {} FAILED: {}\n{}".format(
                    core_tag, e, traceback.format_exc()))
            finally:
                if t is not None:
                    try: t.RollBack()
                    except Exception: pass
                    try: t.Dispose()
                    except Exception: pass
                if scope is not None:
                    try: scope.RollBack()
                    except Exception: pass
                    try: scope.Dispose()
                    except Exception: pass

            if not succeeded:
                self.log("  Core {} failed -- will attempt legacy fallback for this core.".format(core_tag))
                # Fall back to legacy per-floor for this specific core
                try:
                    self._create_stair_runs_legacy_for_core(
                        core_tag, core_runs, current_levels, results,
                        _StairsEditScope, _StairsRun, _StairsLanding,
                        _StairsRunJust)
                except Exception as legacy_err:
                    self.log("  Core {} legacy fallback also failed: {}".format(
                        core_tag, legacy_err))

        # --- Batch post-config: tag + apply preset width in ONE transaction ---
        if created_stair_ids:
            t_cfg = DB.Transaction(doc, "AI Stair Batch Config")
            t_cfg.Start()
            setup_failure_handling(t_cfg, use_nuclear=True)
            for stair_id, core_tag, w_mm in created_stair_ids:
                try:
                    stair_el = doc.GetElement(stair_id)
                    if stair_el:
                        safe_set_comment(stair_el, "AI_{}_MultiStorey".format(core_tag))
                        width_ft = mm_to_ft(w_mm)
                        for sub_id in stair_el.GetStairsRuns():
                            sub_run = doc.GetElement(sub_id)
                            if sub_run:
                                sub_run.ActualRunWidth = width_ft
                except Exception:
                    pass
                results["elements"].append(str(stair_id.Value))
            t_cfg.Commit()
            t_cfg.Dispose()

        elapsed = time.time() - t_start
        self.log("Stair runs (batched): {} cores created, {} deleted. Time: {:.1f}s".format(
            created_count, deleted_count, elapsed))
        if self.tracker: self.tracker.record_created("stair_runs", len(created_stair_ids))

    def _create_stair_runs_legacy_for_core(self, core_tag, core_runs, current_levels,
                                            results, _StairsEditScope, _StairsRun,
                                            _StairsLanding, _StairsRunJust):
        """Legacy per-floor stair creation for a single core (used as fallback)."""
        import Autodesk.Revit.DB as DB  # type: ignore
        from revit_mcp.utils import setup_failure_handling
        from revit_mcp.preprocessors import NuclearJoinGuard
        import math
        doc = self.doc

        spec = getattr(self, '_stair_spec', {})
        width_mm = spec.get("width_of_flight", 1500)
        landing_mm = spec.get("landing_width", 1500)
        spec_tread_mm = spec.get("tread", 300)
        spec_riser_mm = spec.get("riser", 150)

        for rd in core_runs:
            tag = rd['tag']
            base_idx = rd['base_level_idx']
            top_idx = rd['top_level_idx']
            if base_idx >= len(current_levels) or top_idx >= len(current_levels):
                continue

            base_lvl = current_levels[base_idx]
            top_lvl = current_levels[top_idx]
            floor_h_ft = top_lvl.ProjectElevation - base_lvl.ProjectElevation
            if floor_h_ft <= 0:
                continue
            base_z = base_lvl.ProjectElevation

            num_pairs = rd.get('num_flight_pairs', 1)
            f1 = rd['flight_1']
            f2 = rd['flight_2']
            hw = mm_to_ft(width_mm) / 2.0
            landing_ft = mm_to_ft(landing_mm)
            tread_ft = mm_to_ft(spec_tread_mm)
            f1_cx = mm_to_ft(f1['start'][0])
            f2_cx = mm_to_ft(f2['start'][0])
            flight_y_start_ft = mm_to_ft(f1['start'][1])
            land_x_left = f1_cx - hw
            land_x_right = f2_cx
            try:
                scope = _StairsEditScope(doc, "AI Stair Legacy")
                stair_id = scope.Start(base_lvl.Id, top_lvl.Id)

                t = DB.Transaction(doc, "AI Stair Dogleg Legacy")
                t.Start()
                setup_failure_handling(t)

                # --- NEW: Use manifestation points directly to support rotation ---
                p1_start = DB.Point(mm_to_ft(f1['start'][0]), mm_to_ft(f1['start'][1]), base_z)
                p1_end   = DB.Point(mm_to_ft(f1['end'][0]),   mm_to_ft(f1['end'][1]),   base_z)
                p2_start = DB.Point(mm_to_ft(f2['start'][0]), mm_to_ft(f2['start'][1]), base_z)
                p2_end   = DB.Point(mm_to_ft(f2['end'][0]),   mm_to_ft(f2['end'][1]),   base_z)

                run1 = _StairsRun.CreateStraightRun(doc, stair_id, base_lvl.Id, 
                        DB.Line.CreateBound(p1_start, p1_end),
                        _StairsRunJust.Center)
                
                run2 = _StairsRun.CreateStraightRun(doc, stair_id, base_lvl.Id, 
                        DB.Line.CreateBound(p2_start, p2_end),
                        _StairsRunJust.Center)

                # --- FORCE PERFECT RISER PARITY (Revit 2026 Fix) ---
                # Any remainder in riser distribution creates a Y-gap between flights. 
                # For 4 flights, we must ensure total_risers is a multiple of 4.
                # For 2 flights, a multiple of 2.
                num_flights = num_pairs * 2
                if num_flights > 0 and total_risers % num_flights != 0:
                    total_risers = int(math.ceil(total_risers / float(num_flights))) * num_flights

                # Distribute risers strictly EQUALLY
                flight_risers_list = []
                if num_flights > 0:
                    per_flight = total_risers // num_flights
                    flight_risers_list = [per_flight] * num_flights
                else:
                    flight_risers_list = [total_risers]

                flight_pairs = []
                for fi in range(0, len(flight_risers_list), 2):
                    a = flight_risers_list[fi]
                    b = flight_risers_list[fi + 1] if fi + 1 < len(flight_risers_list) else 0
                    if a > 0:
                        flight_pairs.append((a, b))

                # Explicitly set the DesiredNumberRisers to match our even count
                stair_el = doc.GetElement(stair_id)
                if stair_el:
                    # Robust parameter lookup: try BuiltIn enum, then string name
                    p_desired = None
                    try: p_desired = stair_el.LookupParameter("Desired Number of Risers")
                    except: pass
                    if not p_desired:
                        try: 
                            from Autodesk.Revit.DB import BuiltInParameter as BIP2 # type: ignore
                            p_desired = stair_el.get_Parameter(BIP2.STAIRS_DESIRED_NUMBER_OF_RISERS)
                        except Exception: pass
                    if p_desired and not p_desired.IsReadOnly:
                        p_desired.Set(total_risers)
                    elif hasattr(stair_el, "DesiredNumberRisers"):
                        try: stair_el.DesiredNumberRisers = total_risers
                        except: pass

                current_elev_rel = 0.0
                for p_idx, (a_risers, b_risers) in enumerate(flight_pairs):
                    current_elev_abs = base_z + current_elev_rel

                    # No explicit intermediate landing -- Revit auto-generates
                    # landings between consecutive runs (avoids overlap warning).

                    a_run_len = max(a_risers - 1, 1) * tread_ft
                    p_as = DB.XYZ(f1_cx, flight_y_start_ft, current_elev_abs)
                    p_ae = DB.XYZ(f1_cx, flight_y_start_ft + a_run_len, current_elev_abs)
                    line_a = DB.Line.CreateBound(p_as, p_ae)
                    run_a = _StairsRun.CreateStraightRun(doc, stair_id, line_a, _StairsRunJust.Center)

                    mid_elev_rel = run_a.TopElevation
                    mid_elev_abs = base_z + mid_elev_rel

                    if b_risers <= 0:
                        current_elev_rel = mid_elev_rel
                        continue

                    land_y_bot = flight_y_start_ft + a_run_len
                    land_y_top = land_y_bot + landing_ft
                    lp1 = DB.XYZ(land_x_left,  land_y_bot, mid_elev_abs)
                    lp2 = DB.XYZ(land_x_right, land_y_bot, mid_elev_abs)
                    lp3 = DB.XYZ(land_x_right, land_y_top, mid_elev_abs)
                    lp4 = DB.XYZ(land_x_left,  land_y_top, mid_elev_abs)
                    mid_loop = DB.CurveLoop()
                    mid_loop.Append(DB.Line.CreateBound(lp1, lp2))
                    mid_loop.Append(DB.Line.CreateBound(lp2, lp3))
                    mid_loop.Append(DB.Line.CreateBound(lp3, lp4))
                    mid_loop.Append(DB.Line.CreateBound(lp4, lp1))
                    _StairsLanding.CreateSketchedLanding(doc, stair_id, mid_loop, mid_elev_rel)

                    b_run_len = max(b_risers - 1, 1) * tread_ft
                    p_bs = DB.XYZ(f2_cx, flight_y_start_ft + a_run_len, mid_elev_abs)
                    p_be = DB.XYZ(f2_cx, flight_y_start_ft + a_run_len - b_run_len, mid_elev_abs)
                    line_b = DB.Line.CreateBound(p_bs, p_be)
                    run_b = _StairsRun.CreateStraightRun(doc, stair_id, line_b, _StairsRunJust.Center)
                    current_elev_rel = run_b.TopElevation

                t.Commit()
                t = None
                scope.Commit(NuclearJoinGuard(doc))
                scope = None

                # Post-config
                t2 = DB.Transaction(doc, "AI Stair Config Legacy")
                t2.Start()
                setup_failure_handling(t2)
                stair_el = doc.GetElement(stair_id)
                if stair_el:
                    safe_set_comment(stair_el, tag)
                    try:
                        width_ft = mm_to_ft(width_mm)
                        for sub_id in stair_el.GetStairsRuns():
                            sub_run = doc.GetElement(sub_id)
                            if sub_run:
                                sub_run.ActualRunWidth = width_ft
                    except Exception:
                        pass
                t2.Commit()
                t2.Dispose()
                results["elements"].append(str(stair_id.Value))
                self.log("  Legacy fallback: {} created.".format(tag))

            except Exception as e:
                self.log("  Legacy fallback {} FAILED: {}".format(tag, e))
            finally:
                if t is not None:
                    try: t.RollBack()
                    except Exception: pass
                    try: t.Dispose()
                    except Exception: pass
                if scope is not None:
                    try: scope.RollBack()
                    except Exception: pass
                    try: scope.Dispose()
                    except Exception: pass

    def _create_stair_runs_legacy(self, current_levels, results):
        """LEGACY: Create dogleg staircases one-per-floor using Revit's StairsEditScope.

        Kept as fallback if the batched multi-storey approach fails.

        Layout per dogleg pair (plan view, Y-axis up):
            Main landing (front of shaft) -- CreateSketchedLanding
            Flight A: left side, going +Y
            Mid-landing (U-turn, back of shaft) -- CreateSketchedLanding
            Flight B: right side, going -Y
            [Intermediate landing at front -- repeat for more pairs]

        Riser height is FIXED at spec value.  Revit computes the number
        of risers from the floor height and the fixed riser height.
        Taller floors get more flights (same run length each).

        Stairs are always DELETED and RECREATED -- never reused -- because
        Revit stair elements cannot be edited after StairsEditScope.Commit.
        """
        import Autodesk.Revit.DB as DB  # type: ignore
        from revit_mcp.utils import setup_failure_handling
        import math
        doc = self.doc

        run_data_list = getattr(self, '_stair_run_data', [])
        if not run_data_list:
            self.log("Stair runs: no run data available, skipping.")
            return

        try:
            _StairsEditScope = DB.StairsEditScope
            from Autodesk.Revit.DB.Architecture import (  # type: ignore
                StairsRun as _StairsRun,
                StairsLanding as _StairsLanding,
                StairsRunJustification as _StairsRunJust)
            self.log("Stair runs: native Stairs API loaded OK.")
        except Exception as e:
            self.log("Stair runs: CANNOT load Stairs API: {}".format(e))
            return

        from revit_mcp.preprocessors import NuclearJoinGuard

        # --- Delete ALL existing AI stair elements (always recreate) ---
        deleted_count = 0
        try:
            for el in DB.FilteredElementCollector(doc).OfCategory(
                    DB.BuiltInCategory.OST_Stairs).WhereElementIsNotElementType().ToElements():
                p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                if not p:
                    p = el.LookupParameter("Comments")
                if p and p.HasValue:
                    cmt = p.AsString()
                    if cmt and cmt.startswith("AI_") and "Stair_" in cmt:
                        try:
                            t_del = DB.Transaction(doc, "AI Stair Delete")
                            t_del.Start()
                            doc.Delete(el.Id)
                            t_del.Commit()
                            t_del.Dispose()
                            deleted_count += 1
                        except Exception:
                            try:
                                t_del.RollBack()
                            except Exception:
                                pass
        except Exception:
            pass
        if deleted_count:
            self.log("Stair runs: deleted {} old stair elements.".format(deleted_count))

        spec = getattr(self, '_stair_spec', {})
        width_mm = spec.get("width_of_flight", 1500)
        landing_mm = spec.get("landing_width", 1500)
        spec_tread_mm = spec.get("tread", 300)
        spec_riser_mm = spec.get("riser", 150)

        # --- Pre-configure stair type BEFORE creating any stairs ---
        # The first stair in a fresh session uses the template's default
        # riser (e.g. 178mm), not our spec (150mm).  Setting the type
        # param inside StairsEditScope is too late -- Revit already
        # allocated risers with the old value.  Fix: commit the type
        # change in a separate transaction first.
        try:
            stair_types = DB.FilteredElementCollector(doc).OfClass(
                DB.Architecture.StairsType).ToElements()
            if not stair_types:
                stair_types = DB.FilteredElementCollector(doc).OfCategory(
                    DB.BuiltInCategory.OST_Stairs).OfClass(DB.ElementType).ToElements()
            if stair_types:
                t_pre = DB.Transaction(doc, "AI Stair Type Pre-Config")
                t_pre.Start()
                riser_ft = mm_to_ft(spec_riser_mm)
                tread_ft = mm_to_ft(spec_tread_mm)
                for st in stair_types:
                    try:
                        p_riser = st.get_Parameter(
                            DB.BuiltInParameter.STAIRS_ATTR_MAX_RISER_HEIGHT)
                        if p_riser and not p_riser.IsReadOnly:
                            p_riser.Set(riser_ft)
                        for tp in st.GetOrderedParameters():
                            n = tp.Definition.Name.lower()
                            if 'tread' in n and 'depth' in n and not tp.IsReadOnly:
                                tp.Set(tread_ft)
                                break
                    except Exception:
                        pass
                t_pre.Commit()
                t_pre.Dispose()
                self.log("Stair runs: pre-configured {} stair type(s) "
                         "riser={}mm tread={}mm".format(
                    len(stair_types), spec_riser_mm, spec_tread_mm))
        except Exception as e:
            self.log("Stair runs: type pre-config note: {}".format(e))

        created_count = 0

        for rd in run_data_list:
            tag = rd['tag']

            base_idx = rd['base_level_idx']
            top_idx = rd['top_level_idx']
            if base_idx >= len(current_levels) or top_idx >= len(current_levels):
                continue

            base_lvl = current_levels[base_idx]
            top_lvl = current_levels[top_idx]
            floor_h_ft = top_lvl.ProjectElevation - base_lvl.ProjectElevation
            if floor_h_ft <= 0:
                continue
            base_z = base_lvl.ProjectElevation

            num_pairs = rd.get('num_flight_pairs', 1)
            f1 = rd['flight_1']
            f2 = rd['flight_2']
            f1_cx_mm = f1['start'][0]
            f2_cx_mm = f2['start'][0]
            flight_y_start_mm = f1['start'][1]

            scope = None
            t = None
            stair_id = None
            succeeded = False
            try:
                scope = _StairsEditScope(doc, "AI Stair")
                stair_id = scope.Start(base_lvl.Id, top_lvl.Id)

                stair_el = doc.GetElement(stair_id)
                tread_ft = mm_to_ft(spec_tread_mm)
                riser_ft = mm_to_ft(spec_riser_mm)

                t = DB.Transaction(doc, "AI Stair Dogleg")
                t.Start()
                setup_failure_handling(t)

                # --- Push spec riser onto the Revit stair type ---
                if stair_el:
                    try:
                        stair_type = doc.GetElement(stair_el.GetTypeId())
                        if stair_type:
                            p_riser = stair_type.get_Parameter(
                                DB.BuiltInParameter.STAIRS_ATTR_MAX_RISER_HEIGHT)
                            if p_riser and not p_riser.IsReadOnly:
                                p_riser.Set(riser_ft)
                            # Try to set tread depth via name lookup
                            # (BuiltInParameter name varies across Revit versions)
                            for tp in stair_type.GetOrderedParameters():
                                n = tp.Definition.Name.lower()
                                if 'tread' in n and 'depth' in n and not tp.IsReadOnly:
                                    tp.Set(tread_ft)
                                    break
                    except Exception as e:
                        self.log("  Stair type param set note: {}".format(e))

                # --- Geometry from SPEC values (NOT Revit readback) ---
                # Use spec riser to compute total_risers -- must match the
                # num_pairs from staircase_logic which also uses spec riser.
                # Round to nearest mm to avoid float precision errors
                # (e.g. 4200mm stored as 13.7795â€¦ft * 304.8 = 4199.9999â€¦mm)
                floor_h_mm = round(floor_h_ft * 304.8)
                total_risers = int(math.ceil(floor_h_mm / float(spec_riser_mm)))
                rpf = rd.get('risers_per_flight', max(total_risers // 2, 1))

                # Convert mm â†' ft
                f1_cx = mm_to_ft(f1_cx_mm)
                f2_cx = mm_to_ft(f2_cx_mm)
                flight_y_start_ft = mm_to_ft(flight_y_start_mm)
                hw = mm_to_ft(width_mm) / 2.0
                landing_ft = mm_to_ft(landing_mm)

                # Landing X bounds -- span both flights
                land_x_left = f1_cx - hw
                land_x_right = f2_cx + hw

                # --- Distribute risers across flights ---
                # Each flight gets min(rpf, remaining). Last flights may
                # be shorter -- their run_len is computed individually.
                flight_risers_list = []
                remaining = total_risers
                for _ in range(num_pairs * 2):
                    give = min(rpf, remaining)
                    if give <= 0:
                        break
                    flight_risers_list.append(give)
                    remaining -= give

                # Group into pairs: [(a_risers, b_risers), ...]
                flight_pairs = []
                for fi in range(0, len(flight_risers_list), 2):
                    a = flight_risers_list[fi]
                    b = flight_risers_list[fi + 1] if fi + 1 < len(flight_risers_list) else 0
                    if a > 0:
                        flight_pairs.append((a, b))

                self.log("  Stair {}: total_risers={} rpf={} pairs={} "
                         "flights={} floor_h={:.0f}mm".format(
                    tag, total_risers, rpf, len(flight_pairs),
                    flight_risers_list, floor_h_mm))

                # --- Build runs per pair ---
                current_elev_rel = 0.0  # relative to stair base

                for p_idx, (a_risers, b_risers) in enumerate(flight_pairs):
                    current_elev_abs = round(base_z + current_elev_rel, 8)

                    # --- Run A: left side, going +Y ---
                    a_run_len = max(a_risers - 1, 1) * tread_ft
                    p_as = DB.XYZ(round(f1_cx, 8), round(flight_y_start_ft, 8), current_elev_abs)
                    p_ae = DB.XYZ(round(f1_cx, 8), round(flight_y_start_ft + a_run_len, 8), current_elev_abs)
                    line_a = DB.Line.CreateBound(p_as, p_ae)
                    run_a = _StairsRun.CreateStraightRun(doc, stair_id, line_a, _StairsRunJust.Center)

                    mid_elev_rel = a_risers * riser_ft
                    mid_elev_abs = round(base_z + mid_elev_rel, 8)

                    if b_risers <= 0:
                        current_elev_rel = mid_elev_rel
                        continue

                    # --- Mid-landing (U-turn at back of shaft) ---
                    land_y_bot = round(flight_y_start_ft + a_run_len, 8)
                    land_y_top = round(land_y_bot + landing_ft, 8)
                    lp1 = DB.XYZ(round(land_x_left,  8), land_y_bot, mid_elev_abs)
                    lp2 = DB.XYZ(round(land_x_right, 8), land_y_bot, mid_elev_abs)
                    lp3 = DB.XYZ(round(land_x_right, 8), land_y_top, mid_elev_abs)
                    lp4 = DB.XYZ(round(land_x_left,  8), land_y_top, mid_elev_abs)
                    mid_loop = DB.CurveLoop()
                    mid_loop.Append(DB.Line.CreateBound(lp1, lp2))
                    mid_loop.Append(DB.Line.CreateBound(lp2, lp3))
                    mid_loop.Append(DB.Line.CreateBound(lp3, lp4))
                    mid_loop.Append(DB.Line.CreateBound(lp4, lp1))
                    _StairsLanding.CreateSketchedLanding(doc, stair_id, mid_loop, mid_elev_rel)

                    # --- Run B: right side, going -Y ---
                    b_run_len = max(b_risers - 1, 1) * tread_ft
                    p_bs = DB.XYZ(round(f2_cx, 8), land_y_bot, mid_elev_abs)
                    p_be = DB.XYZ(round(f2_cx, 8), round(land_y_bot - b_run_len, 8), mid_elev_abs)
                    line_b = DB.Line.CreateBound(p_bs, p_be)
                    run_b = _StairsRun.CreateStraightRun(doc, stair_id, line_b, _StairsRunJust.Center)

                    current_elev_rel = mid_elev_rel + (b_risers * riser_ft)

                    self.log("  Pair {}/{}: A={} B={} a_run={:.0f} b_run={:.0f}mm "
                             "mid_z={:.0f}mm end_z={:.0f}mm".format(
                        p_idx + 1, len(flight_pairs), a_risers, b_risers,
                        a_run_len * 304.8, b_run_len * 304.8,
                        mid_elev_abs * 304.8,
                        (base_z + current_elev_rel) * 304.8))

                t.Commit()
                t = None

                scope.Commit(NuclearJoinGuard(doc))
                scope = None
                succeeded = True

            except Exception as e:
                self.log("Stair {} FAILED: {}".format(tag, str(e)))
            finally:
                if t is not None:
                    try:
                        t.RollBack()
                    except Exception:
                        pass
                    try:
                        t.Dispose()
                    except Exception:
                        pass
                if scope is not None:
                    try:
                        scope.RollBack()
                    except Exception:
                        pass
                    try:
                        scope.Dispose()
                    except Exception:
                        pass

            if not succeeded:
                continue

            # Post-scope: tag + apply preset width
            t2 = None
            try:
                t2 = DB.Transaction(doc, "AI Stair Config")
                t2.Start()
                setup_failure_handling(t2)
                stair_el = doc.GetElement(stair_id)
                if stair_el:
                    safe_set_comment(stair_el, tag)
                    try:
                        width_ft = mm_to_ft(width_mm)
                        for sub_id in stair_el.GetStairsRuns():
                            sub_run = doc.GetElement(sub_id)
                            if sub_run:
                                sub_run.ActualRunWidth = width_ft
                    except Exception:
                        pass
                t2.Commit()
            except Exception:
                if t2:
                    try:
                        t2.RollBack()
                    except Exception:
                        pass
            finally:
                if t2:
                    try:
                        t2.Dispose()
                    except Exception:
                        pass

            results["elements"].append(str(stair_id.Value))
            created_count += 1

        self.log("Stair runs: {} created, {} deleted".format(
            created_count, deleted_count))
        if self.tracker: self.tracker.record_created("stair_runs", created_count)

    def regenerate_staircases_only(self):
        """Regenerate ONLY the staircase runs using current Revit level heights.
        Uses existing staircase enclosure walls to find the positions, deletes old
        staircase elements, and builds fresh flights adapted to any manually
        changed level heights.
        """
        import Autodesk.Revit.DB as DB # type: ignore
        from revit_mcp.building_generator import get_model_registry
        from revit_mcp.utils import setup_failure_handling, load_presets
        from revit_mcp import staircase_logic
        import re

        doc = self.doc
        registry = get_model_registry(doc)
        
        # 1. Get levels
        levels = []
        for l in DB.FilteredElementCollector(doc).OfClass(DB.Level):
            name = l.Name
            if name.startswith("AI Level") or name.startswith("AI_Level"):
                levels.append(l)
        levels.sort(key=lambda x: x.Elevation)
        
        if len(levels) < 2:
            return {"status": "Error", "message": "Not enough AI Levels found."}
            
        elevations = [l.Elevation for l in levels]
        current_levels = levels
        
        levels_data = []
        for i, elev_ft in enumerate(elevations):
            levels_data.append({
                "id": "AI Level {}".format(i + 1),
                "elevation": elev_ft * 304.8,
            })
            
        # 2. Determine typical height (mode)
        typical_h_mm = 4000.0
        if len(elevations) >= 3:
            sh_list = [round((elevations[i+1]-elevations[i])*304.8) for i in range(len(elevations)-1) if elevations[i+1]-elevations[i] > 0]
            if sh_list:
                typical_h_mm = float(max(set(sh_list), key=sh_list.count))
        elif len(elevations) >= 2:
            typical_h_mm = (elevations[1] - elevations[0]) * 304.8

        # --- AUTO-ADJUST LEVEL HEIGHTS FOR EVEN FLIGHTS ---
        t_levels = DB.Transaction(doc, "AI: Auto-Adjust Levels for Stairs")
        t_levels.Start()
        setup_failure_handling(t_levels)
        needs_commit = False
        
        current_elev = levels[0].Elevation
        for i in range(len(levels) - 1):
            raw_elev_diff_ft = levels[i+1].Elevation - current_elev
            if raw_elev_diff_ft <= 0:
                current_elev = levels[i+1].Elevation
                continue
                
            raw_h_mm = raw_elev_diff_ft * 304.8
            adj_h_mm = staircase_logic.adjust_storey_height(raw_h_mm, typical_h_mm)
            
            if abs(adj_h_mm - raw_h_mm) > 1.0:
                new_elev_ft = current_elev + (adj_h_mm / 304.8)
                levels[i+1].Elevation = new_elev_ft
                levels_data[i+1]['elevation'] = new_elev_ft * 304.8
                self.log("Adjusted Level {} from {:.0f}mm to {:.0f}mm".format(i+2, raw_h_mm, adj_h_mm))
                needs_commit = True
                current_elev = new_elev_ft
            else:
                current_elev = levels[i+1].Elevation
                
        if needs_commit:
            t_levels.Commit()
        else:
            t_levels.RollBack()

        # 3. Find Stair positions by scanning registry for AI_Stair_X_L1_S
        positions = []
        enc_w = 0.0
        stair_tags_found = set()
        
        for tag in registry.keys():
            m = re.match(r'AI_(Stair_\d+)_L1_S', tag)
            if m:
                stair_tags_found.add(m.group(1))
                
        for s_tag in sorted(list(stair_tags_found)):
            e_tag = "AI_{}_L1_E".format(s_tag)
            w_tag = "AI_{}_L1_W".format(s_tag)
            s_wall_tag = "AI_{}_L1_S".format(s_tag)
            n_wall_tag = "AI_{}_L1_N".format(s_tag)
            
            if e_tag in registry and w_tag in registry and s_wall_tag in registry and n_wall_tag in registry:
                e_wall = doc.GetElement(registry[e_tag])
                w_wall = doc.GetElement(registry[w_tag])
                s_wall = doc.GetElement(registry[s_wall_tag])
                n_wall = doc.GetElement(registry[n_wall_tag])
                
                if e_wall and w_wall and s_wall and n_wall:
                    bb_e = e_wall.get_BoundingBox(None)
                    bb_w = w_wall.get_BoundingBox(None)
                    bb_s = s_wall.get_BoundingBox(None)
                    bb_n = n_wall.get_BoundingBox(None)
                    
                    if bb_e and bb_w and bb_s and bb_n:
                        min_x = min(bb_e.Min.X, bb_w.Min.X, bb_s.Min.X, bb_n.Min.X)
                        max_x = max(bb_e.Max.X, bb_w.Max.X, bb_s.Max.X, bb_n.Max.X)
                        min_y = min(bb_e.Min.Y, bb_w.Min.Y, bb_s.Min.Y, bb_n.Min.Y)
                        max_y = max(bb_e.Max.Y, bb_w.Max.Y, bb_s.Max.Y, bb_n.Max.Y)
                        
                        s_cx = (min_x + max_x) / 2.0 * 304.8
                        s_cy = (min_y + max_y) / 2.0 * 304.8
                        width_mm = (max_x - min_x) * 304.8
                        
                        positions.append((s_cx, s_cy))
                        enc_w = max(enc_w, width_mm)
                        
        # --- 3a. Compute Lift Core Bounds [Stair Alignment Fix] ---
        # We need the core bounds to ensure regenerated stairs align correctly
        lift_core_bounds_mm = None
        lx_min, ly_min, lx_max, ly_max = float('inf'), float('inf'), float('-inf'), float('-inf')
        found_lift = False
        for tag, eid in registry.items():
            if "_Lift" in tag and "Wall" in tag:
                found_lift = True
                el = doc.GetElement(eid)
                if not el: continue
                bb = el.get_BoundingBox(None)
                if bb:
                    lx_min = min(lx_min, bb.Min.X * 304.8)
                    ly_min = min(ly_min, bb.Min.Y * 304.8)
                    lx_max = max(lx_max, bb.Max.X * 304.8)
                    ly_max = max(ly_max, bb.Max.Y * 304.8)
        if found_lift:
            lift_core_bounds_mm = (lx_min, ly_min, lx_max, ly_max)

        if not positions:
            return {"status": "Error", "message": "No existing AI staircases found to regenerate."}
            
        stair_spec = getattr(self, "_compliance_params", {})
        
        self.log("Regenerating {} stairs with typical_h_mm={}".format(len(positions), typical_h_mm))
        
        self._stair_run_data = staircase_logic.get_stair_run_data(
            positions, levels_data, enc_w, stair_spec, 
            typical_floor_height_mm=typical_h_mm,
            lift_core_bounds_mm=lift_core_bounds_mm,
            floor_dims_mm=None # floor_dims not easily available here
        )
        stair_spec['_typical_h'] = typical_h_mm
        stair_spec['_rpf'] = staircase_logic._risers_per_flight_typical(
            typical_h_mm, stair_spec.get("riser", 150))
        self._stair_spec = stair_spec

        results = {"elements": []}
        self._create_stair_runs(current_levels, results)

        return {"status": "Success", "message": "Regenerated {} staircases based on new floor heights.".format(len(positions))}

    def _auto_array_windows(self, wall, spacing_mm):
        """Math Delegation: LLM doesn't need to calculate window positions"""
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        spacing = mm_to_ft(spacing_mm)
        lc = wall.Location
        line = lc.Curve
        length = line.Length
        
        if length < spacing: return
        
        count = int(length / spacing)
        symbol = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Windows).OfClass(DB.FamilySymbol).FirstElement()
        if not symbol: return
        if not symbol.IsActive: symbol.Activate()
        
        direction = (line.GetEndPoint(1) - line.GetEndPoint(0)).Normalize()
        for i in range(1, count):
            p = line.GetEndPoint(0) + direction * (i * spacing)
            doc.Create.NewFamilyInstance(p, symbol, wall, doc.GetElement(wall.LevelId), DB.Structure.StructuralType.NonStructural)

    def _find_type(self, category_bip, name):
        import Autodesk.Revit.DB as DB # type: ignore
        cl = DB.FilteredElementCollector(self.doc).OfCategory(category_bip).OfClass(DB.ElementType)
        for t in cl:
            if name.lower() in t.Name.lower(): return t
        return None

    def build_standard_stair(self, data, state):
        """Worker for Vertical Circulation"""
        import Autodesk.Revit.DB as DB # type: ignore
        from Autodesk.Revit.DB.Architecture import StairsRun, StairsRunJustification # type: ignore

        base_lvl_id = DB.ElementId(int(state.get(data['base_level_id'])))
        top_lvl_id = DB.ElementId(int(state.get(data['top_level_id'])))
        loc = DB.XYZ(mm_to_ft(data['x']), mm_to_ft(data['y']), 0)

        # StairsEditScope is in DB, not Architecture
        scope = DB.StairsEditScope(self.doc, "BIM: Stair")
        stair_id = scope.Start(base_lvl_id, top_lvl_id)
        t = DB.Transaction(self.doc, "Stair Run")
        t.Start()
        from revit_mcp.utils import setup_failure_handling
        setup_failure_handling(t)
        try:
            p1 = loc
            p2 = DB.XYZ(loc.X + mm_to_ft(3000), loc.Y, loc.Z)
            line = DB.Line.CreateBound(p1, p2)
            StairsRun.CreateStraightRun(self.doc, stair_id, line, StairsRunJustification.Center)
            t.Commit()
            t.Dispose()
            t = None

            class _FH(DB.IFailuresPreprocessor):
                __namespace__ = "StairFH_legacy"
                def PreprocessFailures(self, accessor):
                    return DB.FailureProcessingResult.Continue
            scope.Commit(_FH())
            scope.Dispose()
            scope = None
            return [{"stair_id": str(stair_id.Value)}]

        except Exception as e:
            if t:
                try: t.RollBack()
                except: pass
                try: t.Dispose()
                except: pass
            if scope:
                try: scope.RollBack()
                except: pass
                try: scope.Dispose()
                except: pass
            raise e
        finally:
            if t is not None:
                try: t.RollBack()
                except: pass
                try: t.Dispose()
                except: pass
            if scope is not None:
                try: scope.RollBack()
                except: pass
                try: scope.Dispose()
                except: pass

    def generate_service_core(self, data, state):
        """Worker for Core Generation"""
        import Autodesk.Revit.DB as DB # type: ignore
        pts = data['boundary_points']
        doc = self.doc
        
        t = DB.Transaction(doc, "BIM: Service Core")
        t.Start()
        from revit_mcp.utils import setup_failure_handling
        setup_failure_handling(t)
        try:
            # 1. Create reinforced concrete walls
            wt = DB.FilteredElementCollector(doc).OfClass(DB.WallType).FirstElement()
            lvl = doc.ActiveView.GenLevel
            curve_loop = DB.CurveLoop()
            
            for i in range(len(pts)):
                p1 = DB.XYZ(mm_to_ft(pts[i]['x']), mm_to_ft(pts[i]['y']), 0)
                p2 = DB.XYZ(mm_to_ft(pts[(i+1)%len(pts)]['x']), mm_to_ft(pts[(i+1)%len(pts)]['y']), 0)
                line = DB.Line.CreateBound(p1, p2)
                wall = DB.Wall.Create(doc, line, wt.Id, lvl.Id, mm_to_ft(20000), 0, False, False)
                disallow_joins(wall)
                curve_loop.Append(line)
            
            # 2. Shaft Opening
            loops = [curve_loop]
            DB.Opening.CreateShaft(doc, lvl.Id, lvl.Id, curve_loop) # Simplified
            t.Commit()
        except Exception as e:
            t.RollBack()
            raise e
        return {"success": True}

    def generate_curtain_facade(self, data, state):
        """Worker for Curtain Systems"""
        import Autodesk.Revit.DB as DB # type: ignore
        wall = self.doc.GetElement(DB.ElementId(int(data['wall_id'])))
        
        t = DB.Transaction(self.doc, "BIM: Curtain Facade")
        t.Start()
        from revit_mcp.utils import setup_failure_handling
        setup_failure_handling(t)
        try:
            # Change wall type to Curtain Wall
            cw_type = None
            for wt in DB.FilteredElementCollector(self.doc).OfClass(DB.WallType):
                if wt.Kind == DB.WallKind.Curtain:
                    cw_type = wt; break
            if cw_type: wall.WallType = cw_type
            t.Commit()
        except Exception as e:
            t.RollBack()
            raise e
        return {"success": True}

    def create_parametric_roof(self, data, state):
        """Worker for Roof Generation"""
        import Autodesk.Revit.DB as DB # type: ignore
        import System.Collections.Generic as Generic # type: ignore
        
        pts = data['boundary_points']
        lvl = self.doc.ActiveView.GenLevel
        
        t = DB.Transaction(self.doc, "BIM: Roof")
        t.Start()
        from revit_mcp.utils import setup_failure_handling
        setup_failure_handling(t)
        try:
            footprint = DB.CurveArray()
            for i in range(len(pts)):
                p1 = DB.XYZ(mm_to_ft(pts[i]['x']), mm_to_ft(pts[i]['y']), 0)
                p2 = DB.XYZ(mm_to_ft(pts[(i+1)%len(pts)]['x']), mm_to_ft(pts[(i+1)%len(pts)]['y']), 0)
                footprint.Append(DB.Line.CreateBound(p1, p2))
            
            mapping = DB.ModelCurveArray()
            roof = self.doc.Create.NewFootprintRoof(footprint, lvl, DB.FilteredElementCollector(self.doc).OfClass(DB.RoofType).FirstElement(), mapping)
            for curve in mapping:
                roof.set_DefinesSlope(curve, True)
                roof.set_Slope(curve, data.get('slope', 30.0) * (3.14159 / 180.0))
            t.Commit()
        except Exception as e:
            t.RollBack()
            raise e
        return {"roof_id": str(roof.Id.Value)}

    def perform_global_cleanup(self):
        """Worker for BIM Health: Targeted Join for AI elements only"""
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        t = DB.Transaction(doc, "BIM: Targeted Global Cleanup")
        t.Start()
        from revit_mcp.utils import setup_failure_handling
        setup_failure_handling(t)
        try:
            # OPTIMIZATION: Instead of scanning the entire document, only scan AI elements
            # This is significantly faster in large projects.
            from revit_mcp.building_generator import get_model_registry
            registry = get_model_registry(doc)
            ai_ids = [eid for eid in registry.values()]
            
            if not ai_ids: 
                t.RollBack()
                return {"status": "No AI elements to cleanup"}
                
            from System.Collections.Generic import List
            ids_list = List[DB.ElementId]()
            for eid in ai_ids: ids_list.Add(eid)
            
            if hasattr(self, "_affected_elements") and self._affected_elements:
                ai_elements = self._affected_elements
            else:
                ai_elements = DB.FilteredElementCollector(doc, ids_list).WhereElementIsNotElementType().ToElements()
            
            # ONLY cleanup shell elements (Walls/Floors). Skip Lifts for speed.
            walls = []
            for e in ai_elements:
                if isinstance(e, DB.Wall):
                    p = e.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                    if p and p.HasValue and "Lift" in p.AsString(): continue 
                    walls.append(e)
            
            floors = [e for e in ai_elements if isinstance(e, DB.Floor)]
            
            # NO AUTOMATIC JOINING: User requested 100% disjoint mode.
            # 1. Targeted Join: Walls with Floors - REMOVED for performance and user request
            pass
            
            # 2. Wall-Wall joins: REMOVED O(N^2) Manual Join
            # Revit's native AutoJoin handles this much more efficiently during Transaction.Commit
            # when SetAllowAutoJoin(True) is called.
            
            t.Commit()
        except Exception as e:
            t.RollBack()
            raise e
        return {"status": "Targeted Cleanup completed successfully"}

    def generate_submission_set(self):
        """Worker for Documentation"""
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        
        t = DB.Transaction(doc, "BIM: Documentation")
        t.Start()
        from revit_mcp.utils import setup_failure_handling
        setup_failure_handling(t)
        try:
            # 1. Create Views
            lvls = DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements()
            vt = None
            for f in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType):
                if f.ViewFamily == DB.ViewFamily.FloorPlan:
                    vt = f; break
            
            plans = []
            existing_plans = DB.FilteredElementCollector(doc).OfClass(DB.ViewPlan).ToElements()
            
            for lvl in lvls:
                # Check if a floor plan already exists for this level
                existing = None
                for ep in existing_plans:
                    if ep.ViewType == DB.ViewType.FloorPlan and ep.GenLevel and ep.GenLevel.Id == lvl.Id:
                        existing = ep
                        break
                        
                if existing:
                    plans.append(existing)
                    continue
                    
                v = DB.ViewPlan.Create(doc, vt.Id, lvl.Id)
                plans.append(v)
                # 1.5 Auto-Dimension Grids
                try: self._dimension_grids_in_view(v)
                except: pass
            
            # 2. Create Sheet
            tb = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_TitleBlocks).FirstElementId()
            sheet = DB.ViewSheet.Create(doc, tb)
            sheet.Name = "SUBMISSION SET"
            
            # 3. Simple Placement
            pt = DB.XYZ(0, 0, 0)
            if plans:
                try:
                    # Viewport.Create throws ArgumentException if the view is already placed
                    DB.Viewport.Create(doc, sheet.Id, plans[0].Id, pt)
                except:
                    pass
            
            t.Commit()
        except Exception as e:
            t.RollBack()
            raise e
        return {"sheet_id": str(sheet.Id.Value)}

    def _dimension_grids_in_view(self, view):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        
        # 1. Cleanup old AI dimensions in this view
        dim_count = 0
        try:
            old_dims = DB.FilteredElementCollector(doc, view.Id).OfClass(DB.Dimension).ToElements()
            for od in old_dims:
                p = od.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                if p and "AI_Dim" in p.AsString():
                    doc.Delete(od.Id)
                    dim_count += 1
        except: pass
        if dim_count > 0: self.log("RevitWorkers: Cleaned up {} old AI dimensions.".format(dim_count))

        # 2. Collect Grids once
        grids = list(DB.FilteredElementCollector(doc, view.Id).OfClass(DB.Grid))
        if len(grids) < 2: return
        
        x_grids = []
        y_grids = []
        for g in grids:
            curve = g.Curve
            if not isinstance(curve, DB.Line): continue
            direction = curve.Direction
            if abs(direction.Y) > 0.9: x_grids.append(g)
            elif abs(direction.X) > 0.9: y_grids.append(g)

        def create_dim(view, grid_list, offset, is_overall=False):
            if len(grid_list) < 2: return
            refs = DB.ReferenceArray()
            
            # Sort by coordinate
            is_x_dim = abs(offset.X) > 0.1 # This is a Y-Grid dimension (horizontal chain)
            grid_list.sort(key=lambda g: g.Curve.GetEndPoint(0).X if not is_x_dim else g.Curve.GetEndPoint(0).Y)
            
            if is_overall:
                refs.Append(DB.Reference(grid_list[0]))
                refs.Append(DB.Reference(grid_list[-1]))
            else:
                for g in grid_list: refs.Append(DB.Reference(g))
            
            # Placement line
            p1 = grid_list[0].Curve.GetEndPoint(0) + offset
            p2 = grid_list[-1].Curve.GetEndPoint(0) + offset
            if p1.DistanceTo(p2) < mm_to_ft(2.0): return 
            line = DB.Line.CreateBound(p1, p2)
            
            try:
                dim = doc.Create.NewDimension(view, line, refs)
                safe_set_comment(dim, "AI_Dim")
            except: pass

        # Only dimension floor plan views. Section/elevation grid dimensions cause
        # "References not parallel" errors when the user adjusts grid extents.
        if view.ViewType != DB.ViewType.FloorPlan:
            return

        create_dim(view, x_grids, DB.XYZ(0, -mm_to_ft(3000), 0), is_overall=False)
        create_dim(view, x_grids, DB.XYZ(0, -mm_to_ft(5000), 0), is_overall=True)
        create_dim(view, y_grids, DB.XYZ(-mm_to_ft(3000), 0, 0), is_overall=False)
        create_dim(view, y_grids, DB.XYZ(-mm_to_ft(5000), 0, 0), is_overall=True)

    def _dimension_levels_in_view(self, view):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        levels = list(DB.FilteredElementCollector(doc).OfClass(DB.Level))
        if len(levels) < 2: return
        
        # Sort by elevation
        levels.sort(key=lambda l: l.Elevation)
        
        refs = DB.ReferenceArray()
        for l in levels: refs.Append(DB.Reference(l))
        
        # Vertical placement line (offset left of building)
        offset_x = -mm_to_ft(5000)
        p1 = DB.XYZ(offset_x, 0, levels[0].Elevation)
        p2 = DB.XYZ(offset_x, 0, levels[-1].Elevation)
        if p1.DistanceTo(p2) < mm_to_ft(2.0): return 
        line = DB.Line.CreateBound(p1, p2)
        
        try:
            dim = doc.Create.NewDimension(view, line, refs)
            safe_set_comment(dim, "AI_Dim")
            
            # Overall height
            refs_o = DB.ReferenceArray()
            refs_o.Append(DB.Reference(levels[0]))
            refs_o.Append(DB.Reference(levels[-1]))
            line_o = DB.Line.CreateBound(p1 + DB.XYZ(-mm_to_ft(2000),0,0), p2 + DB.XYZ(-mm_to_ft(2000),0,0))
            dim_o = doc.Create.NewDimension(view, line_o, refs_o)
            safe_set_comment(dim_o, "AI_Dim")
        except: pass

    def _create_or_update_section(self, name, center, basis_x, basis_y, basis_z, width, height, far_clip):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        
        # 1. Find or Create Section View
        view = None
        for v in DB.FilteredElementCollector(doc).OfClass(DB.ViewSection):
            if v.Name == name:
                view = v; break
        
        # 2. Bounding Box for Section
        # This defines the view's coordinate system and crop region
        bbox = DB.BoundingBoxXYZ()
        bbox.Enabled = True
        bbox.Transform = DB.Transform.Identity
        bbox.Transform.Origin = center
        bbox.Transform.BasisX = basis_x
        bbox.Transform.BasisY = basis_y
        bbox.Transform.BasisZ = basis_z
        
        # Extents (in internal coordinates of the BBox)
        bbox.Min = DB.XYZ(-width/2.0, -height/2.0, -far_clip)
        bbox.Max = DB.XYZ(width/2.0, height/2.0, 0)
        
        if not view:
            # Find Section ViewFamilyType
            vft = None
            for vfam in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType):
                if vfam.ViewFamily == DB.ViewFamily.Section:
                    vft = vfam; break
            if not vft: return None
            view = DB.ViewSection.CreateSection(doc, vft.Id, bbox)
            view.Name = name
        else:
            # Update Crop Region (This is complex in Revit, but we can update the Section Box if we had the original tag)
            # For simplicity, we just reuse the existing view with its previous box unless user asks for re-centering.
            pass
            
        return view

    def polish_model(self):
        """
        BIM Polish: The 'Denuclearization' command.
        Restores joins for all AI elements to finalize the model visuals.
        """
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        from revit_mcp.building_generator import get_model_registry
        registry = get_model_registry(doc)
        
        t = DB.Transaction(doc, "BIM: Final Polish (Restoring Joins)")
        t.Start()
        # NOTE: We do NOT use the JoinGuard here, as we WANT the joined results.
        try:
            count = 0
            for tag, eid in registry.items():
                el = doc.GetElement(eid)
                if el and isinstance(el, DB.Wall):
                    # 1. Re-enable AutoJoin property
                    el.SetAllowAutoJoin(True)
                    # 2. Re-allow end joins (The 'Blue Dot' action)
                    DB.WallUtils.AllowWallJoinAtEnd(el, 0)
                    DB.WallUtils.AllowWallJoinAtEnd(el, 1)
                    count += 1
            
            # 3. Force a global join pass for Walls/Floors
            self.perform_global_cleanup() 
            
            t.Commit()
            self.log("BIM Polish complete: Restored joins for {} walls. Model is now in 'Final Mode'.".format(count))
            return {"status": "Success", "walls_processed": count}
        except Exception as e:
            t.RollBack()
            return {"error": str(e)}


def execute_in_transaction_group(doc, name, action_func):
    import Autodesk.Revit.DB as DB # type: ignore
    tg = DB.TransactionGroup(doc, name)
    tg.Start()
    try:
        # We might have nested individual transactions inside workers
        result = action_func()
        tg.Assimilate()
        return result
    except Exception as e:
        tg.RollBack()
        raise e
