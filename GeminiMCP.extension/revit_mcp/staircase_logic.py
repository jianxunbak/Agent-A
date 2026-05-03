# -*- coding: utf-8 -*-
"""
Staircase generation logic for the Revit MCP building system.

Generates manifest data (walls, floors, voids) for fire-escape staircases
that integrate with the lift core as a compact rectangular assembly.

Rules implemented:
  (a) Min 2 staircases per floor.
  (b) Positioned at Y-ends of lift core, not obstructing lobby entrances.
  (c) Enclosure width matches lift core width -> single rectangular core.
  (d) Abutt lift shaft wall first; shift away only for compliance.
  (e) Every floor-plate point within 60 m of 2 staircases (recalculates).
  (f) Additional staircases added for large floor plates; minimise count.
  (g) Structural walls around staircase core -> no columns in footprint.
  (h) Floor-slab void where staircase shaft is.
  (i) Runs from 1st storey to roof with overrun; adapts to height / count.
  (j) Main landing at every floor level inside the shaft.
  (k) Tall floors use multiple flight pairs (2, 4, 6, 8) to maintain
      consistent shaft depth — sized from the typical storey height.
"""
import math
import hashlib

# BUILD_VERSION is read by _create_stair_runs to verify fresh code is loaded
_BUILD_VERSION = "v2026-04-18-ENTRY-LANDING-ALIGN"

# ─────────────────────────────────────────────────────────────────────────────
#  Load compliance data from JSON files — single source of truth.
#  Falls back to hardcoded values if files are missing.
# ─────────────────────────────────────────────────────────────────────────────

def _load_sc():
    try:
        import os, json
        d = os.path.dirname(os.path.abspath(__file__))
        fs_path = os.path.join(d, "compliance_fire_safety.json")
        st_path = os.path.join(d, "compliance_structural.json")
        c_fs = json.load(open(fs_path)) if os.path.exists(fs_path) else {}
        c_st = json.load(open(st_path)) if os.path.exists(st_path) else {}
        return c_fs, c_st
    except Exception:
        return {}, {}

_SC, _SC2    = _load_sc()
_STAIR_C     = _SC.get("staircase", {})

def _sl_log(msg):
    try:
        import os, time
        _lp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "fastmcp_server.log")
        with open(_lp, "a") as _f:
            _f.write("[{}] {}\n".format(time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass
# Authority minimums — sourced from compliance JSON files
_WALL_THICKNESS  = _SC2.get("wall_thickness_mm", {}).get("core_structural",    350)
_OVERRUN_HEIGHT  = _STAIR_C.get("overrun_height_mm",                          5000)
_MAX_TRAVEL      = _STAIR_C.get("max_travel_distance_mm",                    60000)
_MIN_HEADROOM    = _STAIR_C.get("min_headroom_mm",                            2400)
_FIRE_LB_AREA    = _SC.get("fire_lift_lobby", {}).get("min_area_mm2",      6000000)
_SMOKE_LB_AREA   = _SC.get("smoke_stop_lobby", {}).get("min_area_mm2",     4000000)
_MIN_LB_DEPTH    = _SC.get("smoke_stop_lobby", {}).get("min_clear_depth_mm", 2000)
_PERIMETER_C     = _SC.get("perimeter_staircase", {})
_EDGE_GAP        = _PERIMETER_C.get("edge_inset_gap_mm",                       500)
# Design defaults — last-resort fallbacks when no preset staircase_spec is provided.
# Actual values are set per typology in building_presets.json → core_logic.staircase_spec
_STD_RISER       = 150
_STD_TREAD       = 300
_STD_W_FLIGHT    = 1500
_STD_LANDING     = 1500


# ─────────────────────────────────────────────────────────────────────
#  Flight count helper  [rule k]
# ─────────────────────────────────────────────────────────────────────

def _snap_risers(floor_height_mm, riser=150):
    """Return the number of risers for a given floor height.

    Uses floor() so the risers ALWAYS FIT within the floor height.
    The actual riser height will be floor_height / num_risers
    (usually within ±5mm of the target riser, which is code-compliant).
    Using ceil() caused stairs to OVERSHOOT the floor level.
    """
    return max(int(math.floor(round(floor_height_mm) / float(riser))), 1)


def _risers_per_flight_typical(typical_floor_height_mm, riser=150):
    """Return the number of risers per flight for a typical storey (2 flights)."""
    typical_risers = _snap_risers(typical_floor_height_mm, riser)
    return max(int(math.ceil(typical_risers / 2.0)), 1)


def _calc_num_flights(floor_height_mm, typical_floor_height_mm, riser=150, is_top_floor=False):
    """Return the number of flights (always even) for a given storey height."""
    if typical_floor_height_mm <= 0 or floor_height_mm <= 0:
        return 2

    # NOTE: is_top_floor no longer forces 2 flights. Tall overrun floors (e.g. 5000mm)
    # need more flights to fit within the shaft depth. Normal flight count logic applies.

    rpf = _risers_per_flight_typical(typical_floor_height_mm, riser)
    actual_risers = _snap_risers(floor_height_mm, riser)

    # Number of flights needed so each flight has <= rpf risers
    num_flights = int(math.ceil(actual_risers / float(rpf)))
    if num_flights < 2:
        num_flights = 2
    if num_flights % 2 != 0:
        num_flights += 1

    return num_flights


def _get_flight_list(floor_height_mm, typical_floor_height_mm, riser=150, is_top_floor=False):
    """Return a list of riser counts per flight, ensuring consistency across the building."""
    num_flights = _calc_num_flights(floor_height_mm, typical_floor_height_mm, riser, is_top_floor)
    total_risers = _snap_risers(floor_height_mm, riser)
    
    # SPECIAL CASE: For the top floor, always use an even symmetric split.
    # The asymmetric [14, 16] split that used to be forced here makes Run B
    # longer than the available shaft flight zone → L-shape clash.
    # A symmetric split keeps both runs equal and within the shaft depth.
    if is_top_floor and num_flights == 2:
        half = total_risers // 2
        return [half, total_risers - half]

    if num_flights > 0:
        per_flight = total_risers // num_flights
        remainder = total_risers % num_flights
        return [per_flight + (1 if i < remainder else 0) for i in range(num_flights)]
    
    return [total_risers]


def adjust_storey_height(floor_height_mm, typical_floor_height_mm, riser=150, is_top_floor=False):
    """Adjust a floor height so it produces an even number of risers (even flights)
    AND respects the 2400mm vertical headroom requirement for multi-wrap stairs.
    """
    if riser <= 0 or floor_height_mm <= 0:
        return floor_height_mm

    # Helper to check if a riser count violates the 2400mm headroom rule 
    def get_violation(test_risers):
        import math
        rpf_local = _risers_per_flight_typical(typical_floor_height_mm, riser)
        nf = int(math.ceil(test_risers / float(rpf_local)))
        if nf < 2: nf = 2
        if nf % 2 != 0: nf += 1
        clearance = (test_risers * riser) / (nf / 2.0)
        return clearance < _MIN_HEADROOM

    n_risers = int(round(floor_height_mm / float(riser)))
    if n_risers % 2 != 0:
        below = n_risers - 1
        above = n_risers + 1
        h_below = abs(floor_height_mm - below * riser)
        h_above = abs(floor_height_mm - above * riser)
        n_risers = below if h_below <= h_above else above

    if not is_top_floor and get_violation(n_risers):
        offset = 2
        while True:
            # Favor shorter if both equidistant, as it guarantees fitting inside shaft better
            if not get_violation(n_risers - offset):
                n_risers = n_risers - offset
                break
            if not get_violation(n_risers + offset):
                n_risers = n_risers + offset
                break
            offset += 2

    # Minimum 2 risers
    n_risers = max(2, n_risers)
    return n_risers * riser


# ─────────────────────────────────────────────────────────────────────
#  Shaft geometry
# ─────────────────────────────────────────────────────────────────────

def get_shaft_dimensions(floor_height_mm, spec=None):
    """Calculate staircase shaft dimensions for a given storey height.

    The shaft depth includes both a **main landing** (at the floor
    level, front of shaft) and a **mid-landing** (U-turn, back of
    shaft).  Layout (Y-axis, front to back)::

        wall | main_landing | flight_area | mid_landing | wall

    Returns:
        (shaft_width, shaft_depth) in mm.
    """
    spec = spec or {}
    riser = spec.get("riser", _STD_RISER)
    tread = spec.get("tread", _STD_TREAD)
    w_flight = max(spec.get("width_of_flight", _STD_W_FLIGHT), 1200)
    w_landing = spec.get("landing_width", _STD_LANDING)
    t = _WALL_THICKNESS

    num_risers = _snap_risers(floor_height_mm, riser)
    flight_1_risers = max(num_risers // 2, 1)
    flight_2_risers = max(num_risers - flight_1_risers, 1)
    max_treads = max(flight_1_risers - 1, flight_2_risers - 1, 1)

    w_entry_landing = spec.get("entry_landing_width", w_landing)  # door-side landing; may be wider
    flight_length = max_treads * tread
    shaft_width = 2 * w_flight + 3 * t
    shaft_depth = flight_length + w_landing + w_entry_landing + 2 * t  # entry + mid landing

    _sl_log("[ShaftDims] fh={}mm riser={} tread={} flight_w={} landing={} entry_landing={} "
            "wall_t={} num_risers={} max_treads={} flight_len={} -> sw={} sd={}".format(
            floor_height_mm, riser, tread, w_flight, w_landing, w_entry_landing,
            t, num_risers, max_treads, flight_length, shaft_width, shaft_depth))
    return shaft_width, shaft_depth


def get_max_shaft_depth(levels_data, spec=None, typical_floor_height_mm=None):
    """Return the shaft depth for the staircase enclosure.

    FIX: Automatically identifies the most common floor height if
    typical_floor_height_mm seems wrong (e.g. first floor is very tall).
    """
    # Robust Typical Floor Detection
    typ_h = typical_floor_height_mm
    heights = []
    for i in range(len(levels_data) - 1):
        fh = levels_data[i + 1]['elevation'] - levels_data[i]['elevation']
        if fh > 0: heights.append(fh)

    if heights:
        # If the passed typical_h is way larger than the average,
        # it's likely the first floor (lobby). Find the minimum height.
        min_h = min(heights)
        if not typ_h or typ_h > min_h * 1.5:
            _sl_log("[MaxShaftDepth] passed_typ_h={} > min_h*1.5={:.0f} -> using min_h={}".format(
                    typ_h, min_h * 1.5, min_h))
            typ_h = min_h

    if not typ_h or typ_h <= 0: typ_h = 4000.0
    _, d = get_shaft_dimensions(typ_h, spec)
    _sl_log("[MaxShaftDepth] levels={} typical_h={}mm -> max_shaft_depth={}mm".format(
            len(levels_data) if levels_data else 0, typ_h, d))
    return d


def calc_required_flight_width(floor_area_mm2, num_staircases, compliance_overrides=None):
    """Minimum stair flight width from occupant load (SCDF exit-width formula).

    Distributes total exit-width units across ALL staircases (central + perimeter):
        occupants      = floor_area_m2 / occupant_load_factor_m2
        total_units    = ceil(occupants / persons_per_unit_width)
        units_per_stair= ceil(total_units / num_staircases)
        flight_width   = max(units_per_stair * exit_width_per_unit_mm, min_flight_width_mm)

    Factors come from compliance_overrides (RAG-retrieved) with sensible fallbacks.
    Returns flight width in mm.
    """
    co = compliance_overrides or {}
    occ_factor   = co.get("occupant_load_factor_m2",  10.0)   # m² per person
    persons_unit = co.get("persons_per_unit_width",   100)    # persons per unit of width
    unit_mm      = co.get("exit_width_per_unit_mm",   550)    # mm per unit (SCDF: 550mm)
    min_w        = co.get("min_flight_width_mm",      1000)   # code absolute minimum
    if not floor_area_mm2 or not num_staircases or num_staircases <= 0:
        return max(_STD_W_FLIGHT, int(min_w))
    floor_area_m2 = floor_area_mm2 / 1e6
    occupants     = math.ceil(floor_area_m2 / occ_factor)
    total_units   = math.ceil(occupants / persons_unit)
    per_stair     = math.ceil(total_units / num_staircases)
    return max(per_stair * int(unit_mm), int(min_w))


# ─────────────────────────────────────────────────────────────────────
#  Position calculation (fire-safety rules)
# ─────────────────────────────────────────────────────────────────────

def get_safety_set_dimensions(typical_floor_height_mm, spec=None, is_fire_lift=False, levels_data=None,
                              compliance_overrides=None):
    """Return width/depth for a fire safety suite (lobby + stairs).

    Uses get_max_shaft_depth to robustly identify the true typical depth.
    compliance_overrides: dict of RAG-derived values that override static module constants.
    """
    co = compliance_overrides or {}
    fire_lb_area  = co.get("fire_lobby_min_area_mm2",  _FIRE_LB_AREA)
    smoke_lb_area = co.get("smoke_lobby_min_area_mm2", _SMOKE_LB_AREA)
    min_lb_depth  = (co.get("fire_lobby_min_depth_mm", _MIN_LB_DEPTH) if is_fire_lift
                     else co.get("smoke_lobby_min_depth_mm", _MIN_LB_DEPTH))

    net_w = get_shaft_dimensions(4000, spec)[0]
    # Identify typical depth across all levels if data is provided, else fallback
    if levels_data:
        net_d = get_max_shaft_depth(levels_data, spec, typical_floor_height_mm)
    else:
        # Fallback to smart typical detection from the single height
        net_d = get_shaft_dimensions(typical_floor_height_mm, spec)[1]
    t = _WALL_THICKNESS
    # Minimum required lobby area (e.g. 6.0 m2 for fire lift)
    target_net_area = fire_lb_area if is_fire_lift else smoke_lb_area
    # Minimum required lobby clear dimension (as per compliance rules)
    min_net_depth = min_lb_depth
    
    # Add depth to accommodate lobby area
    # net_d already includes 2*w_landing. We need to ADD a dedicated lobby depth.
    # The staircase and lobby share one wall (T-junction).
    # Total depth = stair_d + lobby_d
    
    # Lobby net width matches shaft net width
    net_w_internal = net_w - 2*t
    lobby_net_d = max(min_net_depth, target_net_area / (max(net_w_internal, 1000)))
    
    total_w = net_w
    total_d = net_d + lobby_net_d + t
    
    # Boundary B: Fire Lift Shaft — same size as passenger lift (2500 mm internal, Rule 3)
    if is_fire_lift:
        fire_lift_d = 2500 + 2 * t  # = 2900 mm outer shaft, matches passenger lift
        total_d += fire_lift_d
    
    return total_w, total_d


def calculate_staircase_positions(floor_dims_mm, core_center_mm,
                                  lift_core_bounds_mm,
                                  typical_floor_height_mm, spec=None,
                                  max_travel_mm=_MAX_TRAVEL,
                                  num_lifts=None, lobby_width=3000,
                                  levels_data=None, orientation="NS"):
    """Determine staircase centre positions.

    orientation: "NS" — stairs at north/south ends of lift core (default).
                 "EW" — stairs at east/west ends of lift core.
    """
    if levels_data:
        shaft_d = get_max_shaft_depth(levels_data, spec, typical_floor_height_mm)
    else:
        _, shaft_d = get_shaft_dimensions(typical_floor_height_mm, spec)
    cx, cy = core_center_mm

    if lift_core_bounds_mm:
        core_xmin, core_ymin, core_xmax, core_ymax = lift_core_bounds_mm
    else:
        core_xmin = cx - 2500; core_xmax = cx + 2500
        core_ymin = cy - 2500; core_ymax = cy + 2500

    if orientation == "EW":
        # --- Primary pair: abutt X-ends of lift core ---
        stair_west_x = core_xmin - shaft_d / 2.0
        stair_east_x = core_xmax + shaft_d / 2.0
        if num_lifts is not None and num_lifts < 4:
            stair_west_x -= lobby_width
        positions = [(stair_west_x, cy), (stair_east_x, cy)]
    else:
        # --- Primary pair: abutt Y-ends of lift core (NS default) ---
        stair_south_y = core_ymin - shaft_d / 2.0
        stair_north_y = core_ymax + shaft_d / 2.0
        if num_lifts is not None and num_lifts < 4:
            stair_south_y -= lobby_width
        positions = [(cx, stair_south_y), (cx, stair_north_y)]

    # --- 60 m rule [rules e, f] ---
    if _check_travel_distance(positions, floor_dims_mm, max_travel_mm):
        return positions

    # Need additional staircases — add at perimeter, minimising count.
    t = 200
    w_flight = spec.get("width_of_flight", _STD_W_FLIGHT) if spec else _STD_W_FLIGHT
    enc_w = 2 * w_flight + 3 * t
    _, enc_d = get_shaft_dimensions(typical_floor_height_mm, spec)

    # Perimeter positions based on 10m setback rule
    max_w = max(d[0] for d in floor_dims_mm) if floor_dims_mm else 50000
    max_l = max(d[1] for d in floor_dims_mm) if floor_dims_mm else 50000
    min_w = min(d[0] for d in floor_dims_mm) if floor_dims_mm else max_w
    min_l = min(d[1] for d in floor_dims_mm) if floor_dims_mm else max_l

    # Rule: If distance > 10m, align to smallest floor plate; else align to largest.
    if (max_w - min_w) / 2.0 > 10000.0:
        edge_x = min_w / 2.0 - enc_w / 2.0
    else:
        edge_x = max_w / 2.0 - enc_w / 2.0

    if (max_l - min_l) / 2.0 > 10000.0:
        edge_y = min_l / 2.0 - enc_d / 2.0
    else:
        edge_y = max_l / 2.0 - enc_d / 2.0

    # 1/3 diagonal spacing rule for the target area (the box where we place staircases)
    diagonal_mm = math.sqrt((2*edge_x)**2 + (2*edge_y)**2)
    min_spacing_mm = diagonal_mm / 3.0

    # Generate perimeter candidate points
    candidates = [
        # Corners (furthest from core)
        (-edge_x, -edge_y),
        ( edge_x, -edge_y),
        (-edge_x,  edge_y),
        ( edge_x,  edge_y),
        # Mid-edges
        (-edge_x, cy),
        ( edge_x, cy),
        ( cx, -edge_y),
        ( cx,  edge_y),
        # Quarter points along long edges
        (-edge_x, -max_l / 4.0),
        ( edge_x, -max_l / 4.0),
        (-edge_x,  max_l / 4.0),
        ( edge_x,  max_l / 4.0),
    ]

    # Iteratively add the candidate that is furthest from all existing staircases
    while candidates:
        best_cand = None
        best_min_dist = -1

        for cand in candidates:
            # Distance from this candidate to the closest existing staircase
            min_dist_to_existing = min(
                math.sqrt((cand[0] - sx)**2 + (cand[1] - sy)**2)
                for sx, sy in positions
            )
            if min_dist_to_existing > best_min_dist:
                best_min_dist = min_dist_to_existing
                best_cand = cand

        # Remove the evaluated best candidate
        candidates.remove(best_cand)

        # Apply the 1/3 diagonal spacing rule
        if best_min_dist < min_spacing_mm:
            continue

        # Add the best candidate if it satisfies spacing
        positions.append(best_cand)

        # Stop early if travel distance rule is satisfied for all floors
        if _check_travel_distance(positions, floor_dims_mm, max_travel_mm):
            break

    return positions


def _point_in_polygon(px, py, polygon_pts):
    """Ray-casting point-in-polygon test. Returns True if (px, py) is inside the polygon."""
    n = len(polygon_pts)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon_pts[i][0], polygon_pts[i][1]
        xj, yj = polygon_pts[j][0], polygon_pts[j][1]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _check_travel_distance(stair_positions, floor_dims_mm,
                           max_dist_mm, num_required=2, footprint_pts=None,
                           footprint_holes=None):
    """Return True if every sampled floor-plate point can reach at least
    *num_required* staircases within *max_dist_mm* (Euclidean).

    footprint_pts: optional list of [x, y] polygon vertices (mm, centred at
    origin) describing the actual floor shape.  When provided, rectangular
    grid test-points that fall outside the polygon are discarded, and polygon
    vertices (slightly inset toward the centroid) are added to guarantee
    coverage of each actual corner — which is the worst-case travel point for
    non-rectangular shapes such as L, H, U, or T floor plates.

    footprint_holes: optional list of hole polygons (each a list of [x, y]
    vertices).  Grid test-points inside any hole are excluded so courtyard
    voids are not counted as occupiable floor area.
    """
    if len(stair_positions) < num_required:
        return False

    # Collect all unique floor dimensions to test
    to_test = []
    if isinstance(floor_dims_mm, list):
        seen = set()
        for d in floor_dims_mm:
            w, l = d[0], d[1]
            if (w, l) not in seen:
                to_test.append((w, l))
                seen.add((w, l))
    else:
        to_test = [(floor_dims_mm[0], floor_dims_mm[1])]

    # When a polygon is supplied, build sample points from a regular grid over the
    # polygon bounding box, keeping only those whose centres fall inside the shape.
    # Sampling at cell centres (not edges/vertices) avoids the boundary ambiguity
    # of ray-casting, so no point lands exactly on a polygon edge.
    # This replaces the rectangular 12-point grid entirely for organic shapes.
    if footprint_pts and len(footprint_pts) >= 3:
        _fp_xs = [p[0] for p in footprint_pts]
        _fp_ys = [p[1] for p in footprint_pts]
        _fp_xmin, _fp_xmax = min(_fp_xs), max(_fp_xs)
        _fp_ymin, _fp_ymax = min(_fp_ys), max(_fp_ys)
        _N = 8  # 8×8 grid → ~64 candidates, cell centres avoid polygon edges
        _dx = (_fp_xmax - _fp_xmin) / float(_N)
        _dy = (_fp_ymax - _fp_ymin) / float(_N)
        _td_holes = footprint_holes or []
        poly_test_pts = []
        for _ix in range(_N):
            for _iy in range(_N):
                _gx = _fp_xmin + (_ix + 0.5) * _dx
                _gy = _fp_ymin + (_iy + 0.5) * _dy
                if not _point_in_polygon(_gx, _gy, footprint_pts):
                    continue
                if any(_point_in_polygon(_gx, _gy, h) for h in _td_holes):
                    continue
                poly_test_pts.append((_gx, _gy))
        if not poly_test_pts:
            poly_test_pts = [(0.0, 0.0)]
        for px, py in poly_test_pts:
            dists = sorted(
                math.sqrt((px - sx) ** 2 + (py - sy) ** 2)
                for sx, sy in stair_positions
            )
            if dists[num_required - 1] > max_dist_mm:
                return False
        return True

    for floor_w_mm, floor_l_mm in to_test:
        hw = floor_w_mm / 2.0
        hl = floor_l_mm / 2.0
        # Corners, edge midpoints, and quarter-points give good coverage.
        test_points = [
            (-hw, -hl), (hw, -hl), (hw, hl), (-hw, hl),
            (0, -hl), (0, hl), (-hw, 0), (hw, 0),
            (-hw / 2, -hl / 2), (hw / 2, -hl / 2),
            (hw / 2, hl / 2), (-hw / 2, hl / 2),
        ]
        for px, py in test_points:
            dists = sorted(
                math.sqrt((px - sx) ** 2 + (py - sy) ** 2)
                for sx, sy in stair_positions
            )
            if dists[num_required - 1] > max_dist_mm:
                return False
    return True


# ─────────────────────────────────────────────────────────────────────
#  Void rectangles (for floor-slab openings)  [rule h]
# ─────────────────────────────────────────────────────────────────────

def get_void_rectangles_mm(positions, enclosure_width_mm, enclosure_depth_mm, 
                           lift_core_bounds_mm=None, num_lifts=None, lobby_width=3000,
                           base_y_override=None, rotated_indices=[]):
    """Return (x1, y1, x2, y2) rectangles for floor-slab openings.
    One rectangle per staircase enclosure. Alignment is core-aware.
    """
    voids = []
    t = _WALL_THICKNESS
    for s_idx, (cx, cy) in enumerate(positions):
        is_rot = (s_idx in rotated_indices)
        # Use explicit override if provided (e.g. from Fire Safety logic)
        if base_y_override is not None:
            base_y = base_y_override
        else:
            base_y = _calc_base_y(s_idx, cy, enclosure_depth_mm, lift_core_bounds_mm, num_lifts, lobby_width)
        
        # Void covers the net internal area (shaft dimensions - walls)
        x1 = cx - enclosure_width_mm / 2.0 + t / 2.0
        x2 = cx + enclosure_width_mm / 2.0 - t / 2.0
        y1 = base_y + t / 2.0
        y2 = base_y + enclosure_depth_mm - t / 2.0
        
        voids.append((x1, y1, x2, y2))
    return voids


# ─────────────────────────────────────────────────────────────────────
#  Manifest generation  [rules a–k]
# ─────────────────────────────────────────────────────────────────────

def wall_overlaps_box(wall_start, wall_end, box_bounds, tol=100):
    """Check if a wall segment lies on a bounding box edge.

    Returns:
        None  — wall does NOT overlap, emit it as-is.
        []    — wall is FULLY covered by the box, skip it entirely.
        list  — gap-filler wall segments [(start, end), ...] for
                portions NOT covered by the box.
    """
    if box_bounds is None:
        return None
    lx_min, ly_min, lx_max, ly_max = box_bounds
    sx, sy = wall_start[0], wall_start[1]
    ex, ey = wall_end[0], wall_end[1]

    # Horizontal wall (constant Y)?
    if abs(sy - ey) < 1:
        y = sy
        w_xmin, w_xmax = min(sx, ex), max(sx, ex)
        # Check if this Y matches a box core horizontal boundary
        on_core_y = (abs(y - ly_min) < tol or abs(y - ly_max) < tol)
        if not on_core_y:
            return None
        # How much of the wall is covered by the box?
        overlap_xmin = max(w_xmin, lx_min)
        overlap_xmax = min(w_xmax, lx_max)
        if overlap_xmin >= overlap_xmax - 5: # 5mm minimum overlap to trigger split
            return None 
            
        gaps = []
        if w_xmin < overlap_xmin - 5:
            gaps.append(([w_xmin, y, 0], [overlap_xmin, y, 0]))
        if w_xmax > overlap_xmax + 5:
            gaps.append(([overlap_xmax, y, 0], [w_xmax, y, 0]))
        return gaps

    # Vertical wall (constant X)?
    elif abs(sx - ex) < 1:
        x = sx
        w_ymin, w_ymax = min(sy, ey), max(sy, ey)
        on_core_x = (abs(x - lx_min) < tol or abs(x - lx_max) < tol)
        if not on_core_x:
            return None
        overlap_ymin = max(w_ymin, ly_min)
        overlap_ymax = min(w_ymax, ly_max)
        if overlap_ymin >= overlap_ymax - 5:
            return None
            
        gaps = []
        if w_ymin < overlap_ymin - 5:
            gaps.append(([x, w_ymin, 0], [x, overlap_ymin, 0]))
        if w_ymax > overlap_ymax + 5:
            gaps.append(([x, overlap_ymax, 0], [x, w_ymax, 0]))
        return gaps

    return None

def _calc_base_y(s_idx, s_cy, enc_d, lift_core_bounds_mm=None, num_lifts=None, lobby_width=3000):
    """Return staircase base_y centred on the spatial-solver position."""
    return s_cy - enc_d / 2.0


def generate_staircase_manifest(positions, levels_data, _enclosure_width_mm=None,
                                spec=None, typical_floor_height_mm=None,
                                lift_core_bounds_mm=None, floor_dims_mm=None,
                                num_lifts=None, lobby_width=3000,
                                base_y_override=None, rotated_indices=[],
                                stair_idx_offset=0, compliance_overrides=None,
                                rotation_degs=None):
    """Generate wall and floor manifest entries for all staircases.

    The enclosure footprint (plan size) is **fixed** for every level,
    determined by the typical storey height.  Taller storeys use more
    flights rather than a deeper shaft.

    Includes:
      - Enclosure walls (4 per level + roof overrun)
      - Divider wall (separates left/right flights)
      - Main landing floor slab at every floor level  [rule j]
      - Roof-level overrun closing slab (TOPCAP)

    Args:
        positions: list of (x, y) staircase centres in mm.
        levels_data: list of dicts with ``'id'`` and ``'elevation'`` (mm).
        enclosure_width_mm: outer enclosure width (= lift-core width for
                            alignment).  Falls back to natural shaft width.
        spec: staircase specification dict.
        typical_floor_height_mm: storey height used to size the shaft.
        lift_core_bounds_mm: (xmin, ymin, xmax, ymax) of lift core in mm,
                             or None.  When provided, staircase enclosure
                             walls that overlap with the lift core boundary
                             are omitted (the lift core wall already serves
                             as enclosure).  Gap-filling walls are created
                             if the lift core wall is shorter than the
                             staircase wall it replaces.

    Returns:
        ``{"walls": [...], "floors": [...]}``
    """
    co = compliance_overrides or {}
    overrun_height = co.get("overrun_height_mm", _OVERRUN_HEIGHT)

    spec = spec or {}
    riser = spec.get("riser", _STD_RISER)
    tread = spec.get("tread", _STD_TREAD)
    w_flight = max(spec.get("width_of_flight", _STD_W_FLIGHT), 1200)
    w_landing = spec.get("landing_width", _STD_LANDING)
    t = _WALL_THICKNESS

    # --- Fixed enclosure dimensions (based on typical storey) ---
    # Enclosure width = staircase shaft width (NOT lift core width).
    # Walls butt directly against the staircase flights.
    shaft_width_nat = 2 * w_flight + 3 * t
    enc_w = shaft_width_nat
    enc_d = get_max_shaft_depth(levels_data, spec, typical_floor_height_mm)
    if enc_d <= 0:
        enc_d = get_shaft_dimensions(4000, spec)[1]  # safe fallback
    # Guard: enc_d must be the long (N-S flight) axis. If geometry produces enc_d < enc_w
    # (e.g. very low storey count) the enclosure is wider than deep — swap so flights always
    # run along the longer axis and walls are oriented correctly.
    if enc_d < enc_w:
        enc_w, enc_d = enc_d, enc_w

    # --- Compute per-level landing extensions ---
    # The landing at level i+1 may need to extend deeper into the shaft
    # to bridge a gap between the stair endpoint and the landing edge.
    #
    # Two cases for the LAST flight pair arriving at level i+1:
    #   (a, b) with 0 < b < a: Run B (-Y RIGHT side) ends short → RIGHT extension
    #   (a, 0) solo Run A:     Run A (+Y LEFT side) overshoots  → LEFT extension
    #   (a, a) or (a, b>=a):   symmetric, no gap                → standard
    #
    # Each entry: (left_ext_mm, right_ext_mm) — extra depth beyond w_landing.
    typ_h = typical_floor_height_mm or 4000.0
    landing_ext_for_next = {}  # level_idx -> (right_ext, y_shift)
    for li in range(len(levels_data) - 1):
        floor_height_mm = levels_data[li + 1]['elevation'] - levels_data[li]['elevation']
        if floor_height_mm <= 0:
            continue
            
        is_top_floor = (li == len(levels_data) - 2)

        total_r = _snap_risers(floor_height_mm, riser)
        nf = _calc_num_flights(floor_height_mm, typ_h, riser, is_top_floor=is_top_floor)
        
        # Simple floor-division: distribute risers evenly across flights
        if nf > 0:
            per_flight = total_r // nf
            remainder = total_r % nf
            flight_list = [per_flight + (1 if i < remainder else 0) for i in range(nf)]
        else:
            flight_list = [total_r]
        # Group flights into actual dogleg pairs
        flight_pairs = []
        for fi in range(0, len(flight_list), 2):
            a = flight_list[fi]
            b = flight_list[fi + 1] if fi + 1 < len(flight_list) else 0
            if a > 0:
                flight_pairs.append((a, b))
        # Determine landing geometry adjustments at level i+1.
        # The landing must start where the LAST STEP ends:
        #
        # (a, a) symmetric: Run B arrives at flight_y_start (front)
        #   -> standard landing at front, no adjustment
        #
        # (a, b<a) short Run B: Run B ends short on RIGHT side
        #   -> RIGHT side needs extension to bridge gap
        #   -> LEFT side is fine (next Run A departs from front)
        #
        # (a, 0) solo Run A: Run A ends a_run_len past flight_y_start on LEFT
        #   -> landing must SHIFT deeper into shaft by a_run_len
        #   -> it starts where Run A ends, not at the shaft front
        right_ext = 0
        y_shift = 0  # how far the landing shifts deeper into the shaft
        if flight_pairs:
            last_a, last_b = flight_pairs[-1]
            if last_b == 0 and last_a > 0:
                # Solo Run A: landing shifts to where Run A ends
                y_shift = max(last_a - 1, 1) * tread
            elif 0 < last_b < last_a:
                # Short Run B: RIGHT side gap
                right_ext = (max(last_a - 1, 1) - max(last_b - 1, 1)) * tread
        landing_ext_for_next[li + 1] = (right_ext, y_shift)

    walls = []
    floors = []

    # --- Lift-core overlap detection ---
    # Tolerance for "same coordinate" comparison (mm).
    _TOL = 50  # 50mm — walls within this distance are considered overlapping

    def _wall_overlaps_lift_core(wall_start, wall_end, lift_bounds):
        """Check if a wall segment lies on a lift-core boundary.

        Returns:
            None  — wall does NOT overlap, emit it as-is.
            []    — wall is FULLY covered by lift core, skip it entirely.
            list  — gap-filler wall segments [(start, end), ...] for
                    portions NOT covered by the lift core.
        """
        if lift_bounds is None:
            return None
        lx_min, ly_min, lx_max, ly_max = lift_bounds
        sx, sy = wall_start[0], wall_start[1]
        ex, ey = wall_end[0], wall_end[1]

        # Horizontal wall (constant Y)?
        if abs(sy - ey) < 1:
            y = sy
            w_xmin, w_xmax = min(sx, ex), max(sx, ex)
            # Check if this Y matches a lift-core horizontal boundary
            on_core_y = (abs(y - ly_min) < _TOL or abs(y - ly_max) < _TOL)
            if not on_core_y:
                return None
            # Lift core horizontal extent
            # How much of the staircase wall is covered by the lift core?
            overlap_min = max(w_xmin, lx_min)
            overlap_max = min(w_xmax, lx_max)
            if overlap_max - overlap_min < _TOL:
                return None  # No meaningful overlap
            # Fully covered?
            if lx_min <= w_xmin + _TOL and lx_max >= w_xmax - _TOL:
                return []  # Fully covered — skip wall
            # Partially covered — return gap segments
            gaps = []
            if w_xmin < lx_min - _TOL:
                gaps.append(([w_xmin, y, 0], [lx_min, y, 0]))
            if w_xmax > lx_max + _TOL:
                gaps.append(([lx_max, y, 0], [w_xmax, y, 0]))
            return gaps

        # Vertical wall (constant X)?
        if abs(sx - ex) < 1:
            x = sx
            w_ymin, w_ymax = min(sy, ey), max(sy, ey)
            on_core_x = (abs(x - lx_min) < _TOL or abs(x - lx_max) < _TOL)
            if not on_core_x:
                return None
            overlap_min = max(w_ymin, ly_min)
            overlap_max = min(w_ymax, ly_max)
            if overlap_max - overlap_min < _TOL:
                return None
            if ly_min <= w_ymin + _TOL and ly_max >= w_ymax - _TOL:
                return []
            gaps = []
            if w_ymin < ly_min - _TOL:
                gaps.append(([x, w_ymin, 0], [x, ly_min, 0]))
            if w_ymax > ly_max + _TOL:
                gaps.append(([x, ly_max, 0], [x, w_ymax, 0]))
            return gaps

        return None  # Diagonal wall — never overlaps

    def _emit_wall(wall_id, start, end, common_dict):
        """Create a wall dict, filtering out zero-length segments."""
        dx = abs(start[0] - end[0])
        dy = abs(start[1] - end[1])
        if dx < 2 and dy < 2:
            return None  # Too short
        return {"id": wall_id, "start": start, "end": end, **common_dict}

    for s_idx, (s_cx, s_cy) in enumerate(positions):
        s_tag = "Stair_{}".format(s_idx + 1 + stair_idx_offset)
        is_rot_s = (s_idx in rotated_indices)
        _w0 = len(walls)
        _f0 = len(floors)
        # Compute _rot_deg here so the landing-generation loop can guard _mirror_y.
        # If a rotation matrix will be applied to this staircase, skip _mirror_y to
        # avoid stacking two transforms that produce self-intersecting geometry.
        _rot_deg = None
        if rotation_degs is not None:
            if isinstance(rotation_degs, (int, float)):
                _rot_deg = float(rotation_degs) or None
            elif s_idx < len(rotation_degs):
                _rot_deg = float(rotation_degs[s_idx]) or None

        # --- Stable base coordinates [Alignment Fix] ---
        base_x = s_cx - enc_w / 2.0
        if base_y_override is not None:
            base_y = base_y_override
        else:
            base_y = _calc_base_y(s_idx, s_cy, enc_d, lift_core_bounds_mm, num_lifts, lobby_width)

        for i, lvl in enumerate(levels_data):
            lvl_id = lvl['id']
            is_roof = (i == len(levels_data) - 1)

            # --- Determine wall height ---
            if is_roof:
                wall_height = overrun_height
            else:
                next_lvl = levels_data[i + 1]
                wall_height = next_lvl['elevation'] - lvl['elevation']
                if wall_height <= 0:
                    continue

            common = {"level_id": lvl_id, "height": wall_height}

            # — 4 enclosure walls (fixed plan, every level including roof) —
            # Each wall is checked against the lift core boundary.  If the
            # lift core already provides enclosure at that location the
            # staircase wall is omitted (or replaced with gap-fillers).
            left_y_start = base_y
            left_y_end = base_y + enc_d
            right_y_start = base_y
            right_y_end = base_y + enc_d

            # Apply T-junction geometric pullback to prevent volume overlap
            # with the lift core walls when joins are disallowed.
            if lift_core_bounds_mm:
                _, core_ymin, _, core_ymax = lift_core_bounds_mm
                core_cy = (core_ymin + core_ymax) / 2.0
                if s_cy < core_cy - 100:
                    left_y_end -= t / 2.0
                    right_y_end -= t / 2.0
                elif s_cy > core_cy + 100:
                    left_y_start += t / 2.0
                    right_y_start += t / 2.0

            candidate_walls = [
                ("AI_{}_L{}_W_Front".format(s_tag, i + 1),
                 [base_x, base_y, 0],
                 [base_x + enc_w, base_y, 0]),
                ("AI_{}_L{}_W_Back".format(s_tag, i + 1),
                 [base_x, base_y + enc_d, 0],
                 [base_x + enc_w, base_y + enc_d, 0]),
                ("AI_{}_L{}_W_Left".format(s_tag, i + 1),
                 [base_x, left_y_start, 0],
                 [base_x, left_y_end, 0]),
                ("AI_{}_L{}_W_Right".format(s_tag, i + 1),
                 [base_x + enc_w, right_y_start, 0],
                 [base_x + enc_w, right_y_end, 0]),
            ]

            for w_id, w_start, w_end in candidate_walls:
                result = wall_overlaps_box(w_start, w_end, lift_core_bounds_mm, _TOL)
                if result is None:
                    walls.append({"id": w_id, "start": w_start, "end": w_end, **common})
                elif len(result) == 0:
                    pass
                else:
                    for gi, (gs, ge) in enumerate(result):
                        w = _emit_wall("{}_Gap{}".format(w_id, gi + 1), gs, ge, common)
                        if w: walls.append(w)

            # --- Main landing floor slab [rule j] ---
            if i > 0:
                lvl = levels_data[i]
                lvl_id = lvl.get('id', 'AI_Level_{}'.format(i+1))
                stair_base_x = s_cx - shaft_width_nat / 2.0
                div_x = stair_base_x + w_flight + t
                
                def _get_flight_arr(li):
                    # Returns (f_y_start, arr_left, arr_right) for level li+1
                    f_h = levels_data[li+1]['elevation'] - levels_data[li]['elevation']
                    # Use entry_landing_width to match get_stair_run_data's ml_depth.
                    # w_landing (1500mm) was previously used here, but get_stair_run_data
                    # uses w_entry_landing (2800mm), creating a visible gap in the model.
                    _w_ent = spec.get("entry_landing_width", w_landing)
                    if f_h <= 0: return base_y + t + _w_ent, base_y + t + _w_ent, base_y + t + _w_ent

                    is_top_f = (li == len(levels_data) - 2)
                    typ_h = typical_floor_height_mm or 4000.0
                    f_list = _get_flight_list(f_h, typ_h, riser, is_top_floor=is_top_f)

                    trd = spec.get("tread", 300)
                    r_len = max(f_list[0] - 1, 1) * trd
                    dyn_w = max(_w_ent, (enc_d - 2*t - r_len) / 2.0)
                    fy_s = base_y + t + dyn_w
                    
                    l_a = f_list[-2] if len(f_list) >= 2 else f_list[-1]
                    l_b = f_list[-1] if len(f_list) >= 2 and len(f_list) % 2 == 0 else 0
                    la_len = max(l_a - 1, 1) * trd
                    lb_len = max(l_b - 1, 1) * trd
                    
                    if len(f_list) % 2 != 0 or l_b == 0:
                        return fy_s, fy_s + la_len, fy_s
                    else:
                        # Safety: pull landing depth back 50mm from calculated arrival to avoid angled clashes
                        # Only apply if not aligning perfectly
                        arr_y_l = fy_s
                        arr_y_r = fy_s + (la_len - lb_len)
                        if la_len != lb_len:
                            arr_y_r -= 50
                        return fy_s, arr_y_l, arr_y_r

                # Landing at level i:
                # Left side matches departing flight (i) or standard if roof
                # Right side matches arriving flight (i-1)
                if is_roof:
                    # Roof landing should match the arrival of the last flight exactly
                    _, arr_l, arr_r = _get_flight_arr(i-1)
                    req_l, req_r = arr_l, arr_r
                else:
                    fy_s_dep, _, _ = _get_flight_arr(i)
                    _, arr_l, arr_r = _get_flight_arr(i-1)
                    req_l = max(fy_s_dep, arr_l)
                    req_r = arr_r

                # For 180°-rotated staircases (north cluster), mirror the landing
                # so it sits on the floor-plate face (north outer face) rather
                # than the lift-core face (south inner face).
                # 180° rotation around (s_cx, s_cy): y_rot = 2*s_cy - y
                # For Y only (X is symmetric): mirror = 2*base_y + enc_d
                # Left/right also swap because X mirrors around s_cx.
                # Apply 180° Y-mirror only when no rotation matrix will follow.
                # If _rot_deg is set, the matrix handles orientation — stacking
                # _mirror_y on top would produce self-intersecting geometry.
                if is_rot_s and not _rot_deg:
                    _mirror_y = 2 * base_y + enc_d
                    front_y = base_y + enc_d - t
                    req_l, req_r = _mirror_y - req_r, _mirror_y - req_l
                    _div_x = 2 * s_cx - div_x  # mirrored divider wall X
                else:
                    front_y = base_y + t
                    _div_x = div_x

                if abs(req_l - req_r) < 5.0:
                    floors.append({
                        "id": "AI_{}_L{}_MainLanding".format(s_tag, i + 1),
                        "level_id": lvl_id, "elevation": lvl['elevation'],
                        "points": [[base_x, front_y], [base_x + enc_w, front_y], [base_x + enc_w, req_r], [base_x, req_l]]
                    })
                else:
                    floors.append({
                        "id": "AI_{}_L{}_MainLanding".format(s_tag, i + 1),
                        "level_id": lvl_id, "elevation": lvl['elevation'],
                        "points": [[base_x, front_y], [base_x + enc_w, front_y], [base_x + enc_w, req_r], [_div_x, req_r], [_div_x, req_l], [base_x, req_l]]
                    })

            # --- Fire Safety / Spatial Conflict Detection ---
            if floor_dims_mm and i < len(floor_dims_mm) and not is_roof:
                f_w, f_l = floor_dims_mm[i]
                hw_f, hl_f = f_w / 2.0, f_l / 2.0
                
                # Check if staircase enclosure is outside the floor plate
                stair_xmin, stair_xmax = s_cx - enc_w / 2.0, s_cx + enc_w / 2.0
                stair_ymin, stair_ymax = s_cy - enc_d / 2.0, s_cy + enc_d / 2.0
                
                is_outside_x = (stair_xmin > hw_f + 10) or (stair_xmax < -hw_f - 10)
                is_outside_y = (stair_ymin > hl_f + 10) or (stair_ymax < -hl_f - 10)
                
                if is_outside_x or is_outside_y:
                    return {
                        "status": "CONFLICT",
                        "description": "Staircase {} (at level {}) is outside the floor plate boundaries. Floor: {}x{}mm, Stair Bounds: [{}, {}, {}, {}]".format(
                            s_tag, i + 1, f_w, f_l, stair_xmin, stair_ymin, stair_xmax, stair_ymax
                        )
                    }

            # Center divider wall removed per user request — not necessary
            # for the dogleg stair layout.

            if is_roof:
                # — TOPCAP: closing slab above overrun (matches lift core) —
                cap_elev = lvl['elevation'] + overrun_height
                floors.append({
                    "id": "AI_{}_TOPCAP".format(s_tag),
                    "level_id": lvl_id,
                    "elevation": cap_elev,
                    "points": [
                        [base_x, base_y],
                        [base_x + enc_w, base_y],
                        [base_x + enc_w, base_y + enc_d],
                        [base_x, base_y + enc_d]
                    ]
                })
                continue

        # Apply arbitrary rotation around staircase centre if requested.
        if _rot_deg:
            _rad = math.radians(_rot_deg)
            _cos_r, _sin_r = math.cos(_rad), math.sin(_rad)
            def _rot_pt(x, y, cx=s_cx, cy=s_cy, cr=_cos_r, sr=_sin_r):
                dx, dy = x - cx, y - cy
                return cx + dx * cr - dy * sr, cy + dx * sr + dy * cr
            for _w in walls[_w0:]:
                _wx, _wy = _rot_pt(_w["start"][0], _w["start"][1])
                _w["start"] = [_wx, _wy, _w["start"][2]]
                _wx, _wy = _rot_pt(_w["end"][0], _w["end"][1])
                _w["end"] = [_wx, _wy, _w["end"][2]]
            for _fl in floors[_f0:]:
                _fl["points"] = [list(_rot_pt(p[0], p[1])) for p in _fl["points"]]

    return {"walls": walls, "floors": floors}

# ─────────────────────────────────────────────────────────────────────
#  Stair run geometry (for creating actual Revit stair elements)
# ─────────────────────────────────────────────────────────────────────

def get_stair_run_data(positions, levels_data, shaft_width_mm, spec, typical_floor_height_mm=4000,
                       lift_core_bounds_mm=None, floor_dims_mm=None, num_lifts=0, lobby_width=3000,
                       rotated_indices=[], base_y_overrides=None, rotation_degs=None):
    """Return geometry data for creating Revit stair runs.

    Supports rotation via rotated_indices (list of indices in positions).
    Uses 180-degree rotation (Flip) to match core orientation.
    """
    import hashlib
    def calc_fingerprint(data):
        # A unique string representing the geometry of this specific stair instance
        raw = "{}:{}:{}:{}:{}".format(
            data['tag'], 
            round(data['base_elev'], 1), 
            round(data['top_elev'], 1),
            data['num_flight_pairs'],
            data['width_mm']
        )
        return hashlib.md5(raw.encode('utf-8')).hexdigest()[:12]

    rotated_indices = rotated_indices or []
    spec = spec or {}
    riser = spec.get("riser", _STD_RISER)
    w_flight = spec.get("width_of_flight", _STD_W_FLIGHT)
    w_landing = spec.get("landing_width", _STD_LANDING)
    t = _WALL_THICKNESS

    shaft_width_nat = 2 * w_flight + 3 * t
    enc_d = get_max_shaft_depth(levels_data, spec, typical_floor_height_mm)
    if enc_d <= 0:
        enc_d = get_shaft_dimensions(4000, spec)[1]

    # Smart Typical Floor Height detection
    typ_h = typical_floor_height_mm
    heights = [levels_data[i+1]['elevation'] - levels_data[i]['elevation'] for i in range(len(levels_data)-1) if levels_data[i+1]['elevation'] > levels_data[i]['elevation']]
    if heights:
        m_h = min(heights)
        if not typ_h or typ_h > m_h * 1.5:
            typ_h = m_h
    if not typ_h or typ_h <= 0: typ_h = 4000.0

    _sl_log("[StairRunData] positions={} levels={} shaft_w={}mm typ_h={}mm enc_d={}mm "
            "riser={} flight_w={} landing={} rotated_idx={} base_y_ovr={}".format(
            positions, len(levels_data) if levels_data else 0, shaft_width_mm,
            typ_h, enc_d, riser, w_flight, w_landing, list(rotated_indices),
            base_y_overrides))

    runs = []

    for s_idx, (s_cx, s_cy) in enumerate(positions):
        is_rot = (s_idx in rotated_indices)
        s_tag = "Stair_{}".format(s_idx + 1)
        if base_y_overrides is not None and s_idx < len(base_y_overrides):
            base_y = base_y_overrides[s_idx]
        else:
            base_y = _calc_base_y(s_idx, s_cy, enc_d, lift_core_bounds_mm, num_lifts, lobby_width)
        stair_base_x = s_cx - shaft_width_nat / 2.0

        # Landing X bounds (inner edges of enclosure)
        land_x_left = stair_base_x + t
        land_x_right = stair_base_x + shaft_width_nat - t

        # Flight centre X positions (inside the two halves of the shaft)
        f1_cx = stair_base_x + t + w_flight / 2.0
        f2_cx = stair_base_x + 2 * t + w_flight + w_flight / 2.0

        # Determine per-staircase rotation angle (degrees).
        _s_rot_deg = 0.0
        if rotation_degs is not None:
            if isinstance(rotation_degs, (list, tuple)):
                _s_rot_deg = float(rotation_degs[s_idx]) if s_idx < len(rotation_degs) and rotation_degs[s_idx] else 0.0
            else:
                _s_rot_deg = float(rotation_degs) if rotation_degs else 0.0
        _s_is_ew = abs(_s_rot_deg - 90.0) < 0.5

        def rotate_pt(pt_xy):
            # For EW staircases (_s_is_ew=True): return NS coords unchanged.
            # revit_workers creates the stair NS first, then rotates the whole
            # Stairs element 90° via ElementTransformUtils.RotateElement().
            if _s_is_ew:
                return pt_xy

            # Legacy path: 180° flip for is_rot=True (NS entry flip).
            if not is_rot:
                return pt_xy
            vx, vy = pt_xy[0] - s_cx, pt_xy[1] - s_cy
            return [s_cx - vx, s_cy - vy]
        # Create stairs for all level pairs including up to the roof.
        for i in range(len(levels_data) - 1):
            floor_height = levels_data[i + 1]['elevation'] - levels_data[i]['elevation']
            if floor_height <= 0:
                continue
            is_top_floor = (i == len(levels_data) - 2)
            flight_list = _get_flight_list(floor_height, typ_h, riser, is_top_floor=is_top_floor)
            floor_risers = sum(flight_list)
            
            # Compute actual riser height for use in Revit
            actual_riser_h = floor_height / float(floor_risers) if floor_risers else float(riser)
            
            num_pairs = len(flight_list) // 2

            intermediate_heights_mm = []
            curr_h = 0
            for pi in range(num_pairs - 1):
                pair_risers = flight_list[2*pi] + (flight_list[2*pi + 1] if 2*pi + 1 < len(flight_list) else 0)
                curr_h += pair_risers * actual_riser_h
                intermediate_heights_mm.append(curr_h)

            tread = spec.get("tread", _STD_TREAD)
            target_risers = flight_list[0] if flight_list else 1
            run_len = max(target_risers - 1, 1) * tread
            shaft_inner_d = enc_d - 2 * t
            # Remaining space is split equally between main and mid landing
            # NON-SYMMETRIC POSITIONING:
            # The shaft depth is enc_d. It contains:
            # [wall (t)] [main_landing (ml_depth)] [flight (run_len)] [mid_landing (mid_d)] [wall (t)]
            
            ml_y_bot = base_y + t
            # Main landing (door-side) uses entry_landing_width if provided; mid landing uses landing_width
            w_entry_landing = spec.get("entry_landing_width", w_landing)
            ml_depth = w_entry_landing
            if len(flight_list) >= 2 and flight_list[1] > flight_list[0]:
                extra_treads = flight_list[1] - flight_list[0]
                proposed_extra = extra_treads * tread
                # Only extend ml_depth if there is enough shaft space to still
                # fit run_len + w_landing (mid-landing minimum) without overflow.
                available = enc_d - 2 * t - run_len - w_landing
                if w_entry_landing + proposed_extra <= available:
                    ml_depth += proposed_extra
            ml_y_top = ml_y_bot + ml_depth

            # Mid-landing depth is whatever remains (capped to w_landing — never bloated)
            mid_d = max(w_landing, min(w_landing, enc_d - 2*t - ml_depth - run_len))
            
            # Flights start after main landing and end before mid landing
            # We add a 20mm safety buffer to ensure NO wall clashing
            flight_y_start = ml_y_top + 20
            flight_y_end = base_y + enc_d - t - mid_d - 20
            
            # Strict bounds enforcement: flights must stay strictly inside shaft walls
            # Inner shaft bounds: [base_y + t, base_y + enc_d - t]
            shaft_inner_start = base_y + t + 1   # 1mm safety clearance from wall face
            shaft_inner_end   = base_y + enc_d - t - 1
            # Clamp flight zone to inner shaft
            flight_y_start = max(flight_y_start, shaft_inner_start + ml_depth + 1)
            flight_y_end   = min(flight_y_end,   shaft_inner_end   - mid_d - 1)
            # Final sanity: ensure minimum flight length
            if flight_y_end < flight_y_start + 100:
                flight_y_end = flight_y_start + 100

            # ml_y_bot/top already calculated above
            pass

            run_data = {
                "tag": "AI_{}_L{}_Run".format(s_tag, i + 1),
                "stair_pos_idx": s_idx,
                "base_level_idx": i,
                "top_level_idx": i + 1,
                "base_elev": levels_data[i]['elevation'],
                "top_elev": levels_data[i+1]['elevation'],
                "num_flight_pairs": num_pairs,
                "_intermediate_count": max(0, num_pairs - 1),
                "intermediate_heights_mm": intermediate_heights_mm,
                "flight_list": flight_list,
                "risers_per_flight": flight_list[0] if flight_list else 1,
                "actual_riser_height_mm": actual_riser_h,
                # _coords_prerotated: True when rotate_pt() already applied the 90° rotation.
                # revit_workers uses ElementTransformUtils.RotateElement on the stair after
                # creation to rotate it 90° around _rot_centre_mm.
                "_coords_prerotated": _s_is_ew,
                "_rot_centre_mm": [s_cx, s_cy] if _s_is_ew else None,
                "_rot_deg": _s_rot_deg if _s_is_ew else 0.0,
                "flight_1": {
                    "start": rotate_pt([f1_cx, flight_y_start]),
                    "end":   rotate_pt([f1_cx, flight_y_end]),
                },
                "flight_2": {
                    "start": rotate_pt([f2_cx, flight_y_end]),
                    "end":   rotate_pt([f2_cx, flight_y_start]),
                },
                "main_landing": {
                    # Revit worker expects these fields for axis-aligned box generation
                    # We can't easily rotate them to tilted rects, but for 180 deg we can just swap.
                    "y_start": ml_y_bot if not is_rot else (base_y + enc_d - ml_depth - t),
                    "y_end": ml_y_top if not is_rot else (base_y + enc_d - t),
                    "x_left": land_x_left if not is_rot else land_x_left, # X doesn't change for 180 rot around CX
                    "x_right": land_x_right if not is_rot else land_x_right,
                },
                "width_mm": w_flight,
                "dyn_landing_w_mm": mid_d,
            }
            run_data["fingerprint"] = calc_fingerprint(run_data)
            runs.append(run_data)

    return runs
