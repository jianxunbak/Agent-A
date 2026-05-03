# -*- coding: utf-8 -*-
"""
OR-Tools CP-SAT Core Layout Engine

Replaces the previous brute-force 24-candidate engine with a full constraint
optimisation solver.  OR-Tools CP-SAT explores every valid 50mm-grid position
and finds the provably best layout (or first feasible within the 5s timeout).

Three mandatory exports consumed by fire_safety_logic.py:
    _USE_LAYOUT_ENGINE       bool — set False to roll back instantly
    _box_inside_footprint()  geometry helper used directly by caller
    find_layout_for_set()    main solver entry point

Rollback:
    Set _USE_LAYOUT_ENGINE = False.  fire_safety_logic.py immediately uses its
    own hardcoded EW/NS arithmetic.  No other files need changing.

OR-Tools availability:
    If ortools is not installed / not bundled, the module falls back to the
    legacy brute-force engine automatically (_ORTOOLS_AVAILABLE = False).
    Bundle ortools for Windows Python 3.12:
        ortools-9.x-cp312-cp312-win_amd64.whl  →  unzip into lib/
"""
import math

# ─────────────────────────────────────────────────────────────────────────────
#  Rollback flag
# ─────────────────────────────────────────────────────────────────────────────
_USE_LAYOUT_ENGINE = True

# ─────────────────────────────────────────────────────────────────────────────
#  OR-Tools import
#  On Windows, ortools/.libs/ contains native DLLs that must be on the DLL
#  search path before the .pyd extension is loaded.  os.add_dll_directory()
#  (Python 3.8+) is the correct way to do this — WinDLL() alone is not enough
#  because it loads the DLL into the process but doesn't extend the search path
#  used when the .pyd's own import-time DLL references are resolved.
# ─────────────────────────────────────────────────────────────────────────────
_ORTOOLS_IMPORT_ERROR = None
try:
    import os as _os
    # Add ortools/.libs/ to the DLL search path before importing the .pyd
    _ortools_lib_dir = _os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)),  # revit_mcp/
        "..", "lib", "ortools", ".libs"
    )
    _ortools_lib_dir = _os.path.normpath(_ortools_lib_dir)
    if _os.path.isdir(_ortools_lib_dir) and hasattr(_os, "add_dll_directory"):
        _os.add_dll_directory(_ortools_lib_dir)
    from ortools.sat.python import cp_model as _cp_model  # type: ignore[import]
    _ORTOOLS_AVAILABLE = True
except Exception as _e:
    _ORTOOLS_AVAILABLE = False
    _ORTOOLS_IMPORT_ERROR = "{}: {}".format(type(_e).__name__, _e)

# ─────────────────────────────────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────────────────────────────────
GRID               = 50     # mm per grid unit
MIN_DOOR_MM        = 1000   # minimum shared face for standard door
MIN_FL_DOOR_MM     = 1100   # minimum shared face for fire lift door
MIN_CLEARANCE_MM   = 1200   # passenger lobby end clearance (SCDF min corridor width)
ALIGNMENT_BONUS    = 10     # grid² reward per aligned wall pair
SOLVER_TIMEOUT_S   = 5.0   # seconds per cluster


# ═════════════════════════════════════════════════════════════════════════════
#  Geometry helpers — keep signatures identical to previous version
# ═════════════════════════════════════════════════════════════════════════════

def _point_in_polygon(px, py, polygon):
    """Ray-casting point-in-polygon test. polygon is [[x,y], ...]."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _box_overlaps_hole(box, hole):
    """True if rectangle box=(x1,y1,x2,y2) overlaps convex/concave hole polygon."""
    x1, y1, x2, y2 = box
    hxs = [p[0] for p in hole]
    hys = [p[1] for p in hole]
    if x2 < min(hxs) or x1 > max(hxs) or y2 < min(hys) or y1 > max(hys):
        return False
    corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2), ((x1+x2)/2, (y1+y2)/2)]
    if any(_point_in_polygon(px, py, hole) for px, py in corners):
        return True
    if any(x1 <= hx <= x2 and y1 <= hy <= y2 for hx, hy in zip(hxs, hys)):
        return True
    return False


def _box_inside_footprint(box, footprint_pts):
    """True if all 4 corners of box are inside the outer footprint polygon."""
    x1, y1, x2, y2 = box
    corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    return all(_point_in_polygon(px, py, footprint_pts) for px, py in corners)


def _boxes_abut(b1, b2, tol=1.0):
    """True if b1 and b2 share at least one edge with positive length overlap."""
    x1a, y1a, x2a, y2a = b1
    x1b, y1b, x2b, y2b = b2
    if abs(x2a - x1b) < tol:
        return min(y2a, y2b) - max(y1a, y1b) > tol
    if abs(x1a - x2b) < tol:
        return min(y2a, y2b) - max(y1a, y1b) > tol
    if abs(y2a - y1b) < tol:
        return min(x2a, x2b) - max(x1a, x1b) > tol
    if abs(y1a - y2b) < tol:
        return min(x2a, x2b) - max(x1a, x1b) > tol
    return False


def _boxes_overlap(b1, b2):
    """True if two boxes have any overlapping interior."""
    return not (b1[2] <= b2[0] or b1[0] >= b2[2] or
                b1[3] <= b2[1] or b1[1] >= b2[3])


def _convex_hull(points):
    """Jarvis march convex hull. Returns ordered list of (x,y) points."""
    pts = list(set(points))
    if len(pts) < 3:
        return pts

    def _cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    start = min(pts, key=lambda p: (p[0], p[1]))
    hull = []
    current = start
    while True:
        hull.append(current)
        candidate = pts[0]
        for p in pts[1:]:
            if candidate == current:
                candidate = p
                continue
            c = _cross(current, candidate, p)
            if c < 0 or (c == 0 and
                         math.dist(current, p) > math.dist(current, candidate)):
                candidate = p
        current = candidate
        if current == start:
            break
        if len(hull) > len(pts):
            break
    return hull


def _cluster_score(boxes, pax_box=None):
    """
    Score = convex-hull perimeter × convex-hull area of the entire core.
    Lower is better.  Includes pax_box so the hull covers the full core.
    """
    all_boxes = list(boxes)
    if pax_box is not None:
        all_boxes.append(pax_box)
    pts = []
    for b in all_boxes:
        pts += [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]
    hull = _convex_hull(pts)
    n = len(hull)
    if n < 3:
        all_x = [b[0] for b in all_boxes] + [b[2] for b in all_boxes]
        all_y = [b[1] for b in all_boxes] + [b[3] for b in all_boxes]
        return (max(all_x) - min(all_x)) * (max(all_y) - min(all_y))
    perim = sum(math.sqrt((hull[i][0] - hull[i-1][0])**2 +
                          (hull[i][1] - hull[i-1][1])**2)
                for i in range(n))
    area = abs(sum(hull[i][0] * (hull[(i+1) % n][1] - hull[i-1][1])
                   for i in range(n))) / 2.0
    centroids = [((b[0]+b[2])/2, (b[1]+b[3])/2) for b in boxes]
    spread = sum(
        math.sqrt((centroids[i][0]-centroids[j][0])**2 +
                  (centroids[i][1]-centroids[j][1])**2)
        for i in range(len(centroids))
        for j in range(i+1, len(centroids))
    )
    return perim * area + spread * 0.001


# ═════════════════════════════════════════════════════════════════════════════
#  OR-Tools helpers
# ═════════════════════════════════════════════════════════════════════════════

def _g(mm):
    """Convert mm to grid units (floor division)."""
    return int(mm) // GRID


def _grid_box_to_mm(x_g, y_g, w_g, d_g):
    """Convert grid-unit box back to mm tuple (x1,y1,x2,y2)."""
    return (x_g * GRID, y_g * GRID,
            (x_g + w_g) * GRID, (y_g + d_g) * GRID)


def _infer_attach_side(lobby_box, anchor_bounds):
    """Infer which face of the anchor the cluster attaches to from lobby centroid."""
    lx1, ly1, lx2, ly2 = lobby_box
    ax1, ay1, ax2, ay2 = anchor_bounds
    lcx = (lx1 + lx2) / 2.0
    lcy = (ly1 + ly2) / 2.0
    acx = (ax1 + ax2) / 2.0
    acy = (ay1 + ay2) / 2.0
    dx = lcx - acx
    dy = lcy - acy
    if abs(dx) > abs(dy):
        return "E" if dx > 0 else "W"
    return "N" if dy > 0 else "S"


def _build_result(solver, positions, eff_w, eff_d, rotations, anchor_bounds,
                  anc_pos_vars=None, anc_w_g=None, anc_d_g=None):
    """Extract solution values from solver and build result dict.

    anc_pos_vars: {"x": IntVar, "y": IntVar} when anchor was movable; None for fixed.
    When provided the solved anchor position is included in the result as
    "solved_anchor_bounds" so callers can update lift_core_bounds_mm.
    """
    fl_box = _grid_box_to_mm(
        solver.Value(positions["fire_lift"]["x"]),
        solver.Value(positions["fire_lift"]["y"]),
        solver.Value(eff_w["fire_lift"]),
        solver.Value(eff_d["fire_lift"]))
    lb_box = _grid_box_to_mm(
        solver.Value(positions["lobby"]["x"]),
        solver.Value(positions["lobby"]["y"]),
        solver.Value(eff_w["lobby"]),
        solver.Value(eff_d["lobby"]))
    st_box = _grid_box_to_mm(
        solver.Value(positions["staircase"]["x"]),
        solver.Value(positions["staircase"]["y"]),
        solver.Value(eff_w["staircase"]),
        solver.Value(eff_d["staircase"]))
    st_rot_deg = 90 * solver.Value(rotations["staircase"])

    if anc_pos_vars is not None:
        solved_ax1 = solver.Value(anc_pos_vars["x"]) * GRID
        solved_ay1 = solver.Value(anc_pos_vars["y"]) * GRID
        solved_anchor = (solved_ax1, solved_ay1,
                         solved_ax1 + anc_w_g * GRID,
                         solved_ay1 + anc_d_g * GRID)
    else:
        solved_anchor = anchor_bounds

    return {
        "fire_lift":            fl_box,
        "lobby":                lb_box,
        "staircase":            st_box,
        "attach_side":          _infer_attach_side(lb_box, solved_anchor),
        "chain_order":          "OR",
        "stair_rot":            st_rot_deg,
        "score":                _cluster_score([fl_box, lb_box, st_box], pax_box=solved_anchor),
        "solved_anchor_bounds": solved_anchor,
    }


def _add_touch_vars(model, a_pos, a_ew, a_ed, b_pos, b_ew, b_ed, label):
    """
    Create four boolean variables (N/S/E/W) indicating which face b touches a on.
    Also creates overlap auxiliary vars for each direction.
    Returns dict {dir: (touch_bool, overlap_var)}.
    """
    result = {}
    ax, ay = a_pos["x"], a_pos["y"]
    bx, by = b_pos["x"], b_pos["y"]

    for dir_name in ("N", "S", "E", "W"):
        t = model.NewBoolVar("{}_{}".format(label, dir_name))

        if dir_name == "N":   # b is north of a  (a.y2 == b.y1)
            model.Add(ay + a_ed == by).OnlyEnforceIf(t)
            model.Add(ay + a_ed != by).OnlyEnforceIf(t.Not())
            ovs = model.NewIntVar(-10**7, 10**7, "{}_N_ovs".format(label))
            ove = model.NewIntVar(-10**7, 10**7, "{}_N_ove".format(label))
            model.AddMaxEquality(ovs, [ax, bx])
            model.AddMinEquality(ove, [ax + a_ew, bx + b_ew])
            ov = model.NewIntVar(0, 10**7, "{}_N_ov".format(label))
            ov_raw = model.NewIntVar(-10**7, 10**7, "{}_N_ovr".format(label))
            model.Add(ov_raw == ove - ovs)
            model.AddMaxEquality(ov, [ov_raw, model.NewConstant(0)])

        elif dir_name == "S":  # b is south of a  (b.y2 == a.y1)
            model.Add(by + b_ed == ay).OnlyEnforceIf(t)
            model.Add(by + b_ed != ay).OnlyEnforceIf(t.Not())
            ovs = model.NewIntVar(-10**7, 10**7, "{}_S_ovs".format(label))
            ove = model.NewIntVar(-10**7, 10**7, "{}_S_ove".format(label))
            model.AddMaxEquality(ovs, [ax, bx])
            model.AddMinEquality(ove, [ax + a_ew, bx + b_ew])
            ov = model.NewIntVar(0, 10**7, "{}_S_ov".format(label))
            ov_raw = model.NewIntVar(-10**7, 10**7, "{}_S_ovr".format(label))
            model.Add(ov_raw == ove - ovs)
            model.AddMaxEquality(ov, [ov_raw, model.NewConstant(0)])

        elif dir_name == "E":  # b is east of a  (a.x2 == b.x1)
            model.Add(ax + a_ew == bx).OnlyEnforceIf(t)
            model.Add(ax + a_ew != bx).OnlyEnforceIf(t.Not())
            ovs = model.NewIntVar(-10**7, 10**7, "{}_E_ovs".format(label))
            ove = model.NewIntVar(-10**7, 10**7, "{}_E_ove".format(label))
            model.AddMaxEquality(ovs, [ay, by])
            model.AddMinEquality(ove, [ay + a_ed, by + b_ed])
            ov = model.NewIntVar(0, 10**7, "{}_E_ov".format(label))
            ov_raw = model.NewIntVar(-10**7, 10**7, "{}_E_ovr".format(label))
            model.Add(ov_raw == ove - ovs)
            model.AddMaxEquality(ov, [ov_raw, model.NewConstant(0)])

        else:  # W — b is west of a  (b.x2 == a.x1)
            model.Add(bx + b_ew == ax).OnlyEnforceIf(t)
            model.Add(bx + b_ew != ax).OnlyEnforceIf(t.Not())
            ovs = model.NewIntVar(-10**7, 10**7, "{}_W_ovs".format(label))
            ove = model.NewIntVar(-10**7, 10**7, "{}_W_ove".format(label))
            model.AddMaxEquality(ovs, [ay, by])
            model.AddMinEquality(ove, [ay + a_ed, by + b_ed])
            ov = model.NewIntVar(0, 10**7, "{}_W_ov".format(label))
            ov_raw = model.NewIntVar(-10**7, 10**7, "{}_W_ovr".format(label))
            model.Add(ov_raw == ove - ovs)
            model.AddMaxEquality(ov, [ov_raw, model.NewConstant(0)])

        result[dir_name] = (t, ov)
    return result


def _require_adjacency(model, touch_vars, min_overlap_g):
    """
    Enforce that at least one direction is touching AND has overlap >= min_overlap_g.
    touch_vars: result of _add_touch_vars — dict {dir: (touch_bool, overlap_var)}.
    """
    # For each direction create a bool: touching AND overlap sufficient
    valid_touch = []
    for dir_name, (t, ov) in touch_vars.items():
        vt = model.NewBoolVar("{}_valid".format(t.Name()))
        # vt → t is true
        model.AddImplication(vt, t)
        # vt → ov >= min_overlap_g
        model.Add(ov >= min_overlap_g).OnlyEnforceIf(vt)
        # t.Not() → vt.Not()
        model.AddImplication(t.Not(), vt.Not())
        valid_touch.append(vt)
    model.AddBoolOr(valid_touch)


def _forbid_direct_touch(model, touch_vars):
    """Ensure no direction in touch_vars is True (modules must not touch at all)."""
    for dir_name, (t, ov) in touch_vars.items():
        model.Add(t == 0)


def _add_side_preference(model, preferred_side, anchor_g,
                         positions, eff_w, eff_d, module_names,
                         anc_pos_vars=None, anc_w_g=0, anc_d_g=0):
    """
    Hard constraint: cluster centroid must be on preferred_side of the anchor centroid.
    anchor_g: (x1_g, y1_g, x2_g, y2_g) — used as fallback when anchor is fixed.
    anc_pos_vars: {"x": IntVar, "y": IntVar} — when anchor is movable, the constraint
        is expressed relative to the live anchor position, not the original grid coords.

    Centroid expressed as sum(2*pos_i + dim_i) to avoid integer-division issues.
    When dim_i is odd in grid units, the old (cx_i * 2 == 2*pos_i + dim_i) formulation
    has no integer solution (LHS even, RHS odd) → instant INFEASIBLE.  Direct sum avoids
    the intermediate variable entirely.
    """
    ax1, ay1, ax2, ay2 = anchor_g

    # sum_2cx = sum(2*pos_xi + eff_wi) = 2 * sum(centroid_xi).
    # Comparing sum_2cx >= 2*n*anc_centroid_x is equivalent to avg centroid >= anc centroid.
    # No intermediate cx_i variable — avoids the parity infeasibility when eff_w is odd.
    n = len(module_names)
    sum_2cx = model.NewIntVar(-10**8, 10**8, "cluster_sum_2cx")
    sum_2cy = model.NewIntVar(-10**8, 10**8, "cluster_sum_2cy")
    model.Add(sum_2cx == sum(positions[name]["x"] * 2 + eff_w[name] for name in module_names))
    model.Add(sum_2cy == sum(positions[name]["y"] * 2 + eff_d[name] for name in module_names))

    if anc_pos_vars is not None:
        # Movable anchor: express anchor centroid as 2*anc_centre = 2*anc_pos + anc_dim
        # sum_2cx >= n * (2*anc_cx) = n * (2*anc_x_var + anc_w_g)
        if preferred_side == "N":
            model.Add(sum_2cy >= n * (anc_pos_vars["y"] * 2 + anc_d_g))
        elif preferred_side == "S":
            model.Add(sum_2cy <= n * (anc_pos_vars["y"] * 2 + anc_d_g))
        elif preferred_side == "E":
            model.Add(sum_2cx >= n * (anc_pos_vars["x"] * 2 + anc_w_g))
        elif preferred_side == "W":
            model.Add(sum_2cx <= n * (anc_pos_vars["x"] * 2 + anc_w_g))
    else:
        # Fixed anchor: use original grid coords; 2*anc_centre = ax1+ax2 (exact integer)
        if preferred_side == "N":
            model.Add(sum_2cy >= n * (ay1 + ay2))
        elif preferred_side == "S":
            model.Add(sum_2cy <= n * (ay1 + ay2))
        elif preferred_side == "E":
            model.Add(sum_2cx >= n * (ax1 + ax2))
        elif preferred_side == "W":
            model.Add(sum_2cx <= n * (ax1 + ax2))


# ═════════════════════════════════════════════════════════════════════════════
#  Core OR-Tools solver
# ═════════════════════════════════════════════════════════════════════════════

def _solve(anchor_bounds, modules_mm, already_placed,
           footprint_pts, footprint_holes,
           preferred_side, log_fn,
           enforce_side=False,
           pax_lobby_bounds=None,
           anchor_snap_zone=None):
    """
    Internal solver.  Returns result dict or None.

    modules_mm: {"fire_lift": (w,d), "lobby": (w,d), "staircase": (w,d)}
    enforce_side: if True, add hard side constraint (first pass).
    pax_lobby_bounds: (x1,y1,x2,y2) mm — the passenger lift corridor strip whose
        ends must stay clear.  When provided, Rule 4 is applied against this
        corridor box instead of the full anchor bank.
    anchor_snap_zone: (zx1, zy1, zx2, zy2) mm — bounding box within which OR-Tools
        may slide the anchor (passenger lift bank).  The anchor retains its size
        (anc_w_g × anc_d_g) but its top-left corner becomes a pair of decision
        variables bounded to this zone.  An anchor-drift penalty in the objective
        keeps it near the original Gemini position unless forced away by obstacles.
        None (default) = anchor stays fixed at anchor_bounds (legacy behaviour).
    """
    cp = _cp_model

    def _log(msg):
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    # ── Search bounds ──────────────────────────────────────────────────────
    # Build bounding box from footprint or anchor + generous margin
    if footprint_pts and len(footprint_pts) >= 3:
        fp_xs = [p[0] for p in footprint_pts]
        fp_ys = [p[1] for p in footprint_pts]
        domain_x1 = _g(min(fp_xs))
        domain_y1 = _g(min(fp_ys))
        domain_x2 = _g(max(fp_xs))
        domain_y2 = _g(max(fp_ys))
        _log("[Solve] domain from footprint: x=[{},{}]mm y=[{},{}]mm".format(
             domain_x1*GRID, domain_x2*GRID, domain_y1*GRID, domain_y2*GRID))
    else:
        ax1, ay1, ax2, ay2 = anchor_bounds
        margin = max(sum(m[0] for m in modules_mm.values()),
                     sum(m[1] for m in modules_mm.values()))
        domain_x1 = _g(ax1 - margin)
        domain_y1 = _g(ay1 - margin)
        domain_x2 = _g(ax2 + margin)
        domain_y2 = _g(ay2 + margin)
        _log("[Solve] domain from anchor+margin({}mm): x=[{},{}]mm y=[{},{}]mm".format(
             margin, domain_x1*GRID, domain_x2*GRID, domain_y1*GRID, domain_y2*GRID))

    # ── Anchor in grid units ───────────────────────────────────────────────
    anc_x1_g = _g(anchor_bounds[0])
    anc_y1_g = _g(anchor_bounds[1])
    anc_x2_g = _g(anchor_bounds[2])
    anc_y2_g = _g(anchor_bounds[3])
    anc_w_g  = anc_x2_g - anc_x1_g
    anc_d_g  = anc_y2_g - anc_y1_g
    _log("[Solve] anchor: ({},{},{},{})mm  w={}mm d={}mm  side={} enforce={} snap={}".format(
         anchor_bounds[0], anchor_bounds[1], anchor_bounds[2], anchor_bounds[3],
         anc_w_g*GRID, anc_d_g*GRID, preferred_side, enforce_side,
         anchor_snap_zone is not None))
    _log("[Solve] modules: fl={}x{}mm lb={}x{}mm st={}x{}mm obstacles={}".format(
         modules_mm["fire_lift"][0], modules_mm["fire_lift"][1],
         modules_mm["lobby"][0],     modules_mm["lobby"][1],
         modules_mm["staircase"][0], modules_mm["staircase"][1],
         len(already_placed) if already_placed else 0))

    # ── Passenger lobby corridor bounds for Rule 4 ─────────────────────────
    # Rule 4 must protect the corridor ends, not the full bank.  Use the
    # provided corridor strip when available; fall back to anchor_bounds.
    if pax_lobby_bounds:
        clr_x1_g = _g(pax_lobby_bounds[0])
        clr_y1_g = _g(pax_lobby_bounds[1])
        clr_x2_g = _g(pax_lobby_bounds[2])
        clr_y2_g = _g(pax_lobby_bounds[3])
    else:
        clr_x1_g, clr_y1_g, clr_x2_g, clr_y2_g = anc_x1_g, anc_y1_g, anc_x2_g, anc_y2_g

    # ── Build model ────────────────────────────────────────────────────────
    model = cp.CpModel()
    module_names = ["fire_lift", "lobby", "staircase"]
    positions = {}
    rotations = {}
    eff_w     = {}
    eff_d     = {}

    for name in module_names:
        w_mm, d_mm = modules_mm[name]
        w_g = _g(w_mm)
        d_g = _g(d_mm)

        positions[name] = {
            "x": model.NewIntVar(domain_x1, domain_x2, "{}_x".format(name)),
            "y": model.NewIntVar(domain_y1, domain_y2, "{}_y".format(name)),
        }

        # All modules (fire_lift, lobby, staircase) may be rotated by the solver.
        # Staircase geometry is generated AFTER solving using the returned stair_rot
        # value, so the generator always sees the correct orientation.
        max_dim_g = max(w_g, d_g)
        rotations[name] = model.NewBoolVar("{}_rot".format(name))
        eff_w[name] = model.NewIntVar(0, max_dim_g, "{}_ew".format(name))
        eff_d[name] = model.NewIntVar(0, max_dim_g, "{}_ed".format(name))
        model.Add(eff_w[name] == w_g).OnlyEnforceIf(rotations[name].Not())
        model.Add(eff_d[name] == d_g).OnlyEnforceIf(rotations[name].Not())
        model.Add(eff_w[name] == d_g).OnlyEnforceIf(rotations[name])
        model.Add(eff_d[name] == w_g).OnlyEnforceIf(rotations[name])

    # ── Rule 8: No overlaps — AddNoOverlap2D ──────────────────────────────
    # NewIntervalVar requires end to be a named IntVar, not an inline expression.
    x_ivs = []
    y_ivs = []
    end_x = {}
    end_y = {}

    for name in module_names:
        ex = model.NewIntVar(domain_x1, domain_x2 + max(
            _g(modules_mm[n][0]) for n in module_names), "{}_ex".format(name))
        ey = model.NewIntVar(domain_y1, domain_y2 + max(
            _g(modules_mm[n][1]) for n in module_names), "{}_ey".format(name))
        model.Add(ex == positions[name]["x"] + eff_w[name])
        model.Add(ey == positions[name]["y"] + eff_d[name])
        end_x[name] = ex
        end_y[name] = ey
        xi = model.NewIntervalVar(positions[name]["x"], eff_w[name], ex, "{}_xi".format(name))
        yi = model.NewIntervalVar(positions[name]["y"], eff_d[name], ey, "{}_yi".format(name))
        x_ivs.append(xi)
        y_ivs.append(yi)

    # ── Anchor — fixed or snap-movable ────────────────────────────────────
    # When anchor_snap_zone is provided the anchor slides within the zone.
    # The anchor size (anc_w_g × anc_d_g) is always preserved.
    if anchor_snap_zone is not None:
        sz_x1, sz_y1, sz_x2, sz_y2 = anchor_snap_zone
        # Zone bounds for the anchor top-left corner (anchor must fit fully inside zone)
        sz_ax1_lo = max(domain_x1, _g(sz_x1))
        sz_ax1_hi = min(domain_x2 - anc_w_g, _g(sz_x2) - anc_w_g)
        sz_ay1_lo = max(domain_y1, _g(sz_y1))
        sz_ay1_hi = min(domain_y2 - anc_d_g, _g(sz_y2) - anc_d_g)
        # Clamp: if zone is tighter than bank size, fall back to original position
        _x_clamped = sz_ax1_lo > sz_ax1_hi
        _y_clamped = sz_ay1_lo > sz_ay1_hi
        if _x_clamped:
            sz_ax1_lo = sz_ax1_hi = anc_x1_g
        if _y_clamped:
            sz_ay1_lo = sz_ay1_hi = anc_y1_g
        anc_x_var = model.NewIntVar(sz_ax1_lo, sz_ax1_hi, "anc_x")
        anc_y_var = model.NewIntVar(sz_ay1_lo, sz_ay1_hi, "anc_y")
        anc_x2_var = model.NewIntVar(sz_ax1_lo + anc_w_g, sz_ax1_hi + anc_w_g, "anc_x2")
        anc_y2_var = model.NewIntVar(sz_ay1_lo + anc_d_g, sz_ay1_hi + anc_d_g, "anc_y2")
        model.Add(anc_x2_var == anc_x_var + anc_w_g)
        model.Add(anc_y2_var == anc_y_var + anc_d_g)
        anc_xi = model.NewIntervalVar(anc_x_var, anc_w_g, anc_x2_var, "anc_xi")
        anc_yi = model.NewIntervalVar(anc_y_var, anc_d_g, anc_y2_var, "anc_yi")
        _anc_pos_vars = {"x": anc_x_var, "y": anc_y_var}
        _log("[Solve] anchor snap zone: x=[{},{}]g=[{},{}]mm y=[{},{}]g=[{},{}]mm "
             "(orig x={}={:.0f}mm y={}={:.0f}mm) x_clamped={} y_clamped={}".format(
             sz_ax1_lo, sz_ax1_hi, sz_ax1_lo*GRID, sz_ax1_hi*GRID,
             sz_ay1_lo, sz_ay1_hi, sz_ay1_lo*GRID, sz_ay1_hi*GRID,
             anc_x1_g, anc_x1_g*GRID, anc_y1_g, anc_y1_g*GRID,
             _x_clamped, _y_clamped))
    else:
        anc_xi = model.NewFixedSizeIntervalVar(anc_x1_g, anc_w_g, "anc_xi")
        anc_yi = model.NewFixedSizeIntervalVar(anc_y1_g, anc_d_g, "anc_yi")
        _anc_pos_vars = None
    x_ivs.append(anc_xi)
    y_ivs.append(anc_yi)

    # already_placed as fixed obstacles
    for idx, obs in enumerate(already_placed or []):
        ox1 = _g(obs[0]); oy1 = _g(obs[1])
        ox2 = _g(obs[2]); oy2 = _g(obs[3])
        ow  = max(1, ox2 - ox1); od = max(1, oy2 - oy1)
        x_ivs.append(model.NewFixedSizeIntervalVar(ox1, ow, "obs{}_xi".format(idx)))
        y_ivs.append(model.NewFixedSizeIntervalVar(oy1, od, "obs{}_yi".format(idx)))

    # Void / hole polygons as fixed obstacles (bounding-box approximation).
    # Conservative: solver avoids the whole bounding rect of each hole,
    # guaranteed to exclude any module overlapping the actual polygon.
    for idx, hole in enumerate(footprint_holes or []):
        hxs = [p[0] for p in hole]; hys = [p[1] for p in hole]
        hx1 = _g(min(hxs)); hy1 = _g(min(hys))
        hx2 = _g(max(hxs)); hy2 = _g(max(hys))
        hw  = max(1, hx2 - hx1); hd = max(1, hy2 - hy1)
        x_ivs.append(model.NewFixedSizeIntervalVar(hx1, hw, "hole{}_xi".format(idx)))
        y_ivs.append(model.NewFixedSizeIntervalVar(hy1, hd, "hole{}_yi".format(idx)))
        _log("[Solve][DIAG] hole{}: ({},{},{},{})mm  w={}mm h={}mm".format(
             idx, hx1*GRID, hy1*GRID, hx2*GRID, hy2*GRID, hw*GRID, hd*GRID))

    _log("[Solve][DIAG] NoOverlap2D: {} intervals total "
         "(1 anchor + {} modules + {} obstacles + {} holes)".format(
         len(x_ivs), len(module_names),
         len(already_placed) if already_placed else 0,
         len(footprint_holes) if footprint_holes else 0))
    model.AddNoOverlap2D(x_ivs, y_ivs)

    # ── Rule 9: All modules inside footprint ──────────────────────────────
    # Use rectangular domain already set above; polygonal check done post-solve.
    for name in module_names:
        model.Add(positions[name]["x"] >= domain_x1)
        model.Add(positions[name]["y"] >= domain_y1)
        model.Add(positions[name]["x"] + eff_w[name] <= domain_x2)
        model.Add(positions[name]["y"] + eff_d[name] <= domain_y2)

    # ── Build anchor touch-vars ────────────────────────────────────────────
    # Fixed anchor: wrap constants.  Movable anchor: use the decision vars.
    if _anc_pos_vars is not None:
        anc_pos  = _anc_pos_vars
        anc_ew_c = model.NewConstant(anc_w_g)
        anc_ed_c = model.NewConstant(anc_d_g)
    else:
        anc_pos  = {"x": model.NewConstant(anc_x1_g), "y": model.NewConstant(anc_y1_g)}
        anc_ew_c = model.NewConstant(anc_w_g)
        anc_ed_c = model.NewConstant(anc_d_g)

    # ── Build all pairwise touch-var sets ─────────────────────────────────
    # Module ↔ anchor
    touch_fl_anc  = _add_touch_vars(model, anc_pos,              anc_ew_c, anc_ed_c,
                                     positions["fire_lift"],  eff_w["fire_lift"],  eff_d["fire_lift"],
                                     "fl_anc")
    touch_lb_anc  = _add_touch_vars(model, anc_pos,              anc_ew_c, anc_ed_c,
                                     positions["lobby"],      eff_w["lobby"],      eff_d["lobby"],
                                     "lb_anc")
    touch_st_anc  = _add_touch_vars(model, anc_pos,              anc_ew_c, anc_ed_c,
                                     positions["staircase"],  eff_w["staircase"],  eff_d["staircase"],
                                     "st_anc")

    # Module ↔ module
    touch_lb_fl   = _add_touch_vars(model, positions["fire_lift"],  eff_w["fire_lift"],  eff_d["fire_lift"],
                                     positions["lobby"],      eff_w["lobby"],      eff_d["lobby"],
                                     "lb_fl")
    touch_lb_st   = _add_touch_vars(model, positions["lobby"],      eff_w["lobby"],      eff_d["lobby"],
                                     positions["staircase"],  eff_w["staircase"],  eff_d["staircase"],
                                     "lb_st")

    # ── Rule 1a: lobby must touch fire_lift (min FL door width) ───────────
    MIN_FL_G = _g(MIN_FL_DOOR_MM)
    _require_adjacency(model, touch_lb_fl, MIN_FL_G)

    # ── Rule 1a-align: fire_lift and lobby centres must align on shared axis ──
    # When stacked N/S: centres must share the same X (2*fl_cx == 2*lb_cx).
    # When stacked E/W: centres must share the same Y (2*fl_cy == 2*lb_cy).
    # Prevents the visual misalignment where a 1-grid X-offset makes the shaft
    # appear to float offset beside the lobby in the Revit floor plan.
    fl_x  = positions["fire_lift"]["x"]
    fl_y  = positions["fire_lift"]["y"]
    fl_ew = eff_w["fire_lift"]
    lb_x  = positions["lobby"]["x"]
    lb_y  = positions["lobby"]["y"]
    lb_ew = eff_w["lobby"]
    t_lb_fl_N, _ = touch_lb_fl["N"]   # lobby is NORTH of fire_lift
    t_lb_fl_S, _ = touch_lb_fl["S"]   # lobby is SOUTH of fire_lift
    t_lb_fl_E, _ = touch_lb_fl["E"]   # lobby is EAST of fire_lift
    t_lb_fl_W, _ = touch_lb_fl["W"]   # lobby is WEST of fire_lift
    # When stacked N/S: enforce same WIDTH so left+right edges flush.
    # When stacked E/W (side-by-side compact rectangle): enforce shared near edge only.
    # Do NOT enforce equal depth for E/W — fire_lift (3200mm) and lobby (4700mm) differ
    # in depth intentionally; forcing fl_ed==lb_ed makes the compact rectangle infeasible.
    model.Add(fl_ew == lb_ew).OnlyEnforceIf(t_lb_fl_N)
    model.Add(fl_x  == lb_x ).OnlyEnforceIf(t_lb_fl_N)
    model.Add(fl_ew == lb_ew).OnlyEnforceIf(t_lb_fl_S)
    model.Add(fl_x  == lb_x ).OnlyEnforceIf(t_lb_fl_S)
    model.Add(fl_y  == lb_y ).OnlyEnforceIf(t_lb_fl_E)
    model.Add(fl_y  == lb_y ).OnlyEnforceIf(t_lb_fl_W)

    # ── Rule 1b: lobby must touch staircase (min standard door width) ─────
    MIN_D_G = _g(MIN_DOOR_MM)
    _require_adjacency(model, touch_lb_st, MIN_D_G)

    # ── Rule 1b-align: lobby and staircase centre-aligned when stacked N/S ──────
    # When stacked N/S: share the same X-centre so the staircase (wider than lobby)
    # is centred above/below rather than left-edge flush — prevents the C-notch that
    # appears when a 7000mm staircase protrudes 3800mm past a 3200mm lobby.
    # When stacked E/W: do NOT enforce shared y — lobby depth ≠ staircase depth.
    st_x  = positions["staircase"]["x"]
    st_ew = eff_w["staircase"]
    t_lb_st_N, _ = touch_lb_st["N"]   # staircase is NORTH of lobby
    t_lb_st_S, _ = touch_lb_st["S"]   # staircase is SOUTH of lobby
    model.Add(2 * lb_x + lb_ew == 2 * st_x + st_ew).OnlyEnforceIf(t_lb_st_N)
    model.Add(2 * lb_x + lb_ew == 2 * st_x + st_ew).OnlyEnforceIf(t_lb_st_S)

    # Rule 1c (fire_lift must not touch staircase) intentionally removed.
    # Sharing a wall between fire_lift and staircase shafts is architecturally
    # valid — the lobby provides the required access separation.  The constraint
    # was blocking the compact 2×2 arrangement (staircase W of lobby, fire_lift
    # S of lobby, both flush against the anchor face) which has a much smaller
    # total-core bounding box than any linear chain.

    # ── Rule 2: all cluster modules must stay on their side of the anchor ───
    # Hard-enforced only on the first pass (enforce_side=True) where OR-Tools
    # is asked to place the cluster strictly on one side.
    # On pass 2 (enforce_side=False) this constraint is omitted so OR-Tools can
    # find L-shaped or other creative arrangements — e.g. fire_lift south of the
    # bank, staircase rotated and placed east/west of the lobby — when the
    # straight linear chain doesn't fit on either side.
    # The anchor NoOverlap2D obstacle already prevents modules from overlapping
    # the bank itself; Rule 3 keeps the fire lift adjacent to the anchor face.
    if enforce_side:
        # When anchor is movable, constrain relative to its position vars.
        _anc_y1 = anc_pos["y"]              if _anc_pos_vars is not None else model.NewConstant(anc_y1_g)
        _anc_x1 = anc_pos["x"]              if _anc_pos_vars is not None else model.NewConstant(anc_x1_g)
        _anc_y2 = anc_y2_var                if _anc_pos_vars is not None else model.NewConstant(anc_y2_g)
        _anc_x2 = anc_x2_var                if _anc_pos_vars is not None else model.NewConstant(anc_x2_g)
        if preferred_side == "S":
            for name in module_names:
                model.Add(end_y[name] <= _anc_y1)
        elif preferred_side == "N":
            for name in module_names:
                model.Add(positions[name]["y"] >= _anc_y2)
        elif preferred_side == "W":
            for name in module_names:
                model.Add(end_x[name] <= _anc_x1)
        elif preferred_side == "E":
            for name in module_names:
                model.Add(positions[name]["x"] >= _anc_x2)

    # ── Rule 3: fire_lift must touch anchor with meaningful face overlap ──────
    # The fire lift must be the module adjacent to the passenger bank — it must
    # touch the anchor face directly.  Allowing lobby/staircase to satisfy this
    # instead produces inverted chains (ST→LB→FL far from anchor).
    # Require fire_lift specifically to share ≥ MIN_FL_DOOR_MM with the anchor.
    MIN_ANC_OV_G = _g(MIN_FL_DOOR_MM)
    valid_anchor_touch = []
    for dir_name, (t, ov) in touch_fl_anc.items():
        vt = model.NewBoolVar("anc_valid_fl_{}".format(dir_name))
        model.AddImplication(vt, t)
        model.Add(ov >= MIN_ANC_OV_G).OnlyEnforceIf(vt)
        model.AddImplication(t.Not(), vt.Not())
        valid_anchor_touch.append(vt)
    model.AddBoolOr(valid_anchor_touch)

    # ── Rule 4: passenger lift corridor open at both ends ─────────────────
    # No module may simultaneously block both the west AND east ends of the
    # corridor's X span, NOR both the south AND north ends of the corridor's
    # Y span.  The constraint is UNCONDITIONAL and targets the passenger lift
    # corridor strip (clr_*) — not the full bank — so the solver knows exactly
    # which ends must stay open for people to walk through.
    MIN_CLR_G = _g(MIN_CLEARANCE_MM)

    for name in module_names:
        py = positions[name]["y"]

        # Y-axis: only relevant for N/S clusters (EW bank, modules above/below anchor).
        # For an EW bank the passenger corridor runs EW and its N and S ends must stay
        # open so people can reach the exits.  A module that simultaneously blocks the
        # south entry (py <= clr_y1+MIN_CLR) AND the north entry (ey >= clr_y2-MIN_CLR)
        # seals both corridor ends — forbidden.
        # For E/W clusters (NS bank) the corridor runs NS and its ends are the EAST and
        # WEST faces; Y-blocking is architecturally irrelevant and applying this rule
        # here causes false INFEASIBLE when the lobby (3200mm) straddles the narrow
        # 3000mm corridor strip — lobby spans both clearance zones simultaneously.
        if preferred_side in ("N", "S"):
            blk_south_y = model.NewBoolVar("{}_blk_south_y".format(name))
            blk_north_y = model.NewBoolVar("{}_blk_north_y".format(name))
            model.Add(py          <= clr_y1_g + MIN_CLR_G).OnlyEnforceIf(blk_south_y)
            model.Add(py          >  clr_y1_g + MIN_CLR_G).OnlyEnforceIf(blk_south_y.Not())
            model.Add(end_y[name] >= clr_y2_g - MIN_CLR_G).OnlyEnforceIf(blk_north_y)
            model.Add(end_y[name] <  clr_y2_g - MIN_CLR_G).OnlyEnforceIf(blk_north_y.Not())
            model.AddBoolOr([blk_south_y.Not(), blk_north_y.Not()])

    # Rule 4 X-axis for N/S clusters is intentionally omitted (and for E/W clusters too).
    # For N/S clusters: they sit above or below the anchor; the corridor X-ends are open
    # by default and the X constraint would make wide-lobby placements infeasible.
    # For E/W clusters: Rule 4b-X below handles the X-end clearance requirement instead.

    # ── Rule 4b: cluster must leave at least one corridor Y-end fully open ───
    # Only for N/S clusters (EW bank) — the corridor's N and S ends must remain
    # accessible.  For E/W clusters the relevant exits are E/W (handled by 4b-X).
    # Applying this to E/W clusters incorrectly constrains the lobby Y-range and
    # causes INFEASIBLE when the 3200mm lobby straddles the narrow corridor strip.
    if preferred_side in ("N", "S"):
        _4b_south_clear = []
        _4b_north_clear = []
        for name in module_names:
            py = positions[name]["y"]
            ey = end_y[name]
            _sc = model.NewBoolVar("{}_4b_sc".format(name))
            model.Add(py >= clr_y1_g + MIN_CLR_G).OnlyEnforceIf(_sc)
            model.Add(py <  clr_y1_g + MIN_CLR_G).OnlyEnforceIf(_sc.Not())
            _4b_south_clear.append(_sc)
            _nc = model.NewBoolVar("{}_4b_nc".format(name))
            model.Add(ey <= clr_y2_g - MIN_CLR_G).OnlyEnforceIf(_nc)
            model.Add(ey >  clr_y2_g - MIN_CLR_G).OnlyEnforceIf(_nc.Not())
            _4b_north_clear.append(_nc)
        _all_sc = model.NewBoolVar("4b_all_south_clear")
        _all_nc = model.NewBoolVar("4b_all_north_clear")
        model.AddBoolAnd(_4b_south_clear).OnlyEnforceIf(_all_sc)
        model.AddBoolOr([b.Not() for b in _4b_south_clear]).OnlyEnforceIf(_all_sc.Not())
        model.AddBoolAnd(_4b_north_clear).OnlyEnforceIf(_all_nc)
        model.AddBoolOr([b.Not() for b in _4b_north_clear]).OnlyEnforceIf(_all_nc.Not())
        model.AddBoolOr([_all_sc, _all_nc])

    # ── Rule 4b-X: cluster must leave at least one corridor X-end fully open ─
    # Only relevant when the cluster is E/W of the anchor — N/S clusters sit
    # above or below the corridor and cannot seal its X-ends regardless.
    # Skipping for N/S clusters prevents Rule 4b-X from making L-shaped layouts
    # infeasible when the staircase extends past the corridor X boundary.
    #
    # In snap mode the anchor slides in X, so the corridor X-extent is live.
    # We use anc_x_var / anc_x2_var instead of the fixed clr_x1_g / clr_x2_g.
    # Otherwise a cluster placed east of a right-shifted anchor would have
    # end_x >> clr_x2_g and could never satisfy the east-clear condition.
    # In fixed-anchor mode clr_x1_g / clr_x2_g equal the anchor x-extent, which
    # happens to equal the pax_lobby x-bounds, so the old constants are fine.
    if preferred_side in ("E", "W"):
        _4bx_west_clear = []
        _4bx_east_clear = []
        if _anc_pos_vars is not None:
            _r4bx_x1 = anc_x_var   # live anchor left edge
            _r4bx_x2 = anc_x2_var  # live anchor right edge
        else:
            _r4bx_x1 = model.NewConstant(clr_x1_g)
            _r4bx_x2 = model.NewConstant(clr_x2_g)
        for name in module_names:
            px = positions[name]["x"]
            ex = end_x[name]
            _wc = model.NewBoolVar("{}_4bx_wc".format(name))
            model.Add(px >= _r4bx_x1 + MIN_CLR_G).OnlyEnforceIf(_wc)
            model.Add(px <  _r4bx_x1 + MIN_CLR_G).OnlyEnforceIf(_wc.Not())
            _4bx_west_clear.append(_wc)
            _ec = model.NewBoolVar("{}_4bx_ec".format(name))
            model.Add(ex <= _r4bx_x2 - MIN_CLR_G).OnlyEnforceIf(_ec)
            model.Add(ex >  _r4bx_x2 - MIN_CLR_G).OnlyEnforceIf(_ec.Not())
            _4bx_east_clear.append(_ec)
        _all_wc = model.NewBoolVar("4bx_all_west_clear")
        _all_ec = model.NewBoolVar("4bx_all_east_clear")
        model.AddBoolAnd(_4bx_west_clear).OnlyEnforceIf(_all_wc)
        model.AddBoolOr([b.Not() for b in _4bx_west_clear]).OnlyEnforceIf(_all_wc.Not())
        model.AddBoolAnd(_4bx_east_clear).OnlyEnforceIf(_all_ec)
        model.AddBoolOr([b.Not() for b in _4bx_east_clear]).OnlyEnforceIf(_all_ec.Not())
        model.AddBoolOr([_all_wc, _all_ec])

    # ── Rule 5: fire lobby max 2 shared faces ─────────────────────────────
    # Collect all touching bools per face of the lobby
    # "b touches lobby on face D" = touch_lb_X[D][0] where X is the other module
    # Note: touch_lb_fl has lobby as 'b' relative to fire_lift — so face direction
    # is from fire_lift's perspective toward lobby.  We need lobby's face perspective.
    # "lobby west face touched" = fire_lift is west of lobby = touch_lb_fl["W"] (fire_lift west of lobby means lobby.x1 == fl.x2)
    # Direction semantics in _add_touch_vars: dir = direction from 'a' to 'b'.
    # touch_lb_fl: a=fire_lift, b=lobby.  "N" means lobby is north of fire_lift → lobby.y1 == fl.y2 → lobby's SOUTH face shared.
    # So lobby face shared map:
    #   touch_lb_fl["N"] → lobby's S face shared with FL
    #   touch_lb_fl["S"] → lobby's N face shared with FL
    #   touch_lb_fl["E"] → lobby's W face shared with FL
    #   touch_lb_fl["W"] → lobby's E face shared with FL
    # touch_lb_st: a=lobby, b=staircase.
    #   touch_lb_st["N"] → stair is north of lobby → lobby's N face shared with ST
    #   touch_lb_st["S"] → stair is south of lobby → lobby's S face shared with ST
    #   touch_lb_st["E"] → stair is east of lobby  → lobby's E face shared with ST
    #   touch_lb_st["W"] → stair is west of lobby  → lobby's W face shared with ST
    # touch_lb_anc: a=anchor, b=lobby.
    #   touch_lb_anc["N"] → lobby is north of anchor → lobby's S face touches anchor
    #   touch_lb_anc["S"] → lobby is south of anchor → lobby's N face touches anchor
    #   touch_lb_anc["E"] → lobby is east of anchor  → lobby's W face touches anchor
    #   touch_lb_anc["W"] → lobby is west of anchor  → lobby's E face touches anchor

    lobby_face_touched = {"N": [], "S": [], "E": [], "W": []}

    # FL touching lobby: dir from FL's perspective, invert for lobby's perspective
    _fl_to_lb_face = {"N": "S", "S": "N", "E": "W", "W": "E"}
    for d, (t, ov) in touch_lb_fl.items():
        lobby_face_touched[_fl_to_lb_face[d]].append(t)

    # Stair touching lobby: dir FROM lobby TO stair → that's lobby's face
    for d, (t, ov) in touch_lb_st.items():
        lobby_face_touched[d].append(t)

    # Anchor touching lobby: dir from anchor to lobby, invert for lobby face
    _anc_to_lb_face = {"N": "S", "S": "N", "E": "W", "W": "E"}
    for d, (t, ov) in touch_lb_anc.items():
        lobby_face_touched[_anc_to_lb_face[d]].append(t)

    # face_shared = BoolOr(touch_list) for each lobby face
    face_shared_bools = []
    for face in ("N", "S", "E", "W"):
        touch_list = lobby_face_touched[face]
        face_shared = model.NewBoolVar("lbf_{}_shared".format(face))
        if touch_list:
            model.AddBoolOr(touch_list).OnlyEnforceIf(face_shared)
            for t in touch_list:
                model.AddImplication(t, face_shared)
            model.AddBoolAnd([t.Not() for t in touch_list]).OnlyEnforceIf(face_shared.Not())
        else:
            model.Add(face_shared == 0)
        face_shared_bools.append(face_shared)

    model.Add(sum(face_shared_bools) <= 2)

    # ── Rule 6: already enforced via min_overlap in _require_adjacency ────
    # (MIN_FL_G and MIN_D_G passed above)

    # ── Rule 7: staircase must have at least one free face ────────────────
    stair_face_touched = {"N": [], "S": [], "E": [], "W": []}

    # Lobby touching staircase: touch_lb_st dir = from lobby to staircase
    # "N" = stair north of lobby → stair's S face shared; invert
    _lb_to_st_face = {"N": "S", "S": "N", "E": "W", "W": "E"}
    for d, (t, ov) in touch_lb_st.items():
        stair_face_touched[_lb_to_st_face[d]].append(t)

    # Anchor touching staircase: touch_st_anc dir = from anchor to stair, invert
    for d, (t, ov) in touch_st_anc.items():
        stair_face_touched[_anc_to_lb_face[d]].append(t)   # same inversion as lobby←anchor

    stair_face_free_bools = []
    for face in ("N", "S", "E", "W"):
        touch_list = stair_face_touched[face]
        face_free = model.NewBoolVar("stf_{}_free".format(face))
        if touch_list:
            # face_free = NOT (any touch on this face)
            none_touch = model.NewBoolVar("stf_{}_none".format(face))
            model.AddBoolAnd([t.Not() for t in touch_list]).OnlyEnforceIf(none_touch)
            model.AddBoolOr(touch_list).OnlyEnforceIf(none_touch.Not())
            model.Add(face_free == none_touch)
        else:
            model.Add(face_free == 1)
        stair_face_free_bools.append(face_free)

    model.AddBoolOr(stair_face_free_bools)

    # ── Side preference ───────────────────────────────────────────────────
    # Always apply the centroid preference so the cluster stays on the correct
    # side of the anchor even when Rule 2 (per-module hard boundary) is off.
    if preferred_side:
        _add_side_preference(model, preferred_side,
                             (anc_x1_g, anc_y1_g, anc_x2_g, anc_y2_g),
                             positions, eff_w, eff_d, module_names,
                             anc_pos_vars=_anc_pos_vars,
                             anc_w_g=anc_w_g, anc_d_g=anc_d_g)

    # ── Objective ─────────────────────────────────────────────────────────
    # Bounding box of the ENTIRE CORE: fire modules + passenger lift anchor.
    # Including the anchor forces OR-Tools to minimise the combined footprint of
    # all modules together, producing compact rectangular cores.  Previously only
    # fire_lift + lobby + staircase were included, which allowed L/C-shaped clusters
    # that looked compact in isolation but had wasted notches around the passenger bank.
    # Anchor drift is also naturally penalised: sliding the anchor expands cl_perim.
    cl_x1 = model.NewIntVar(-10**7, 10**7, "cl_x1")
    cl_y1 = model.NewIntVar(-10**7, 10**7, "cl_y1")
    cl_x2 = model.NewIntVar(-10**7, 10**7, "cl_x2")
    cl_y2 = model.NewIntVar(-10**7, 10**7, "cl_y2")

    if _anc_pos_vars is not None:
        _anc_x1_obj = anc_pos["x"]
        _anc_x2_obj = anc_x2_var
        _anc_y1_obj = anc_pos["y"]
        _anc_y2_obj = anc_y2_var
    else:
        _anc_x1_obj = model.NewConstant(anc_x1_g)
        _anc_x2_obj = model.NewConstant(anc_x2_g)
        _anc_y1_obj = model.NewConstant(anc_y1_g)
        _anc_y2_obj = model.NewConstant(anc_y2_g)

    model.AddMinEquality(cl_x1, [positions[n]["x"] for n in module_names] + [_anc_x1_obj])
    model.AddMaxEquality(cl_x2, [end_x[n]          for n in module_names] + [_anc_x2_obj])
    model.AddMinEquality(cl_y1, [positions[n]["y"]  for n in module_names] + [_anc_y1_obj])
    model.AddMaxEquality(cl_y2, [end_y[n]           for n in module_names] + [_anc_y2_obj])

    cl_w = model.NewIntVar(0, 10**7, "cl_w")
    cl_h = model.NewIntVar(0, 10**7, "cl_h")
    model.Add(cl_w == cl_x2 - cl_x1)
    model.Add(cl_h == cl_y2 - cl_y1)

    cl_perim = model.NewIntVar(0, 10**8, "cl_perim")
    model.Add(cl_perim == cl_w + cl_h)

    # Anti-spread is zero — cl_perim already includes the anchor so any
    # spreading in X or Y directly increases cl_perim.  A separate per-axis
    # penalty double-penalises layouts that spread in one axis but are compact
    # in total, blocking the 2×2 arrangement that is better overall.
    anti_spread = model.NewConstant(0)

    # Wall alignment bonus (secondary objective term)
    alignment_pairs = [
        ("fire_lift", "lobby",     "y1"),
        ("fire_lift", "lobby",     "y2"),
        ("lobby",     "staircase", "y1"),
        ("lobby",     "staircase", "y2"),
        ("fire_lift", "lobby",     "x1"),
        ("fire_lift", "lobby",     "x2"),
        ("lobby",     "staircase", "x1"),
        ("lobby",     "staircase", "x2"),
    ]
    total_alignment_bonus = model.NewIntVar(0, ALIGNMENT_BONUS * len(alignment_pairs) + 1, "aln_bonus")
    aln_bools = []
    for a_name, b_name, edge in alignment_pairs:
        ab = model.NewBoolVar("aln_{}_{}_{}" .format(a_name, b_name, edge))
        if edge == "y1":
            model.Add(positions[a_name]["y"] == positions[b_name]["y"]).OnlyEnforceIf(ab)
            model.Add(positions[a_name]["y"] != positions[b_name]["y"]).OnlyEnforceIf(ab.Not())
        elif edge == "y2":
            model.Add(end_y[a_name] == end_y[b_name]).OnlyEnforceIf(ab)
            model.Add(end_y[a_name] != end_y[b_name]).OnlyEnforceIf(ab.Not())
        elif edge == "x1":
            model.Add(positions[a_name]["x"] == positions[b_name]["x"]).OnlyEnforceIf(ab)
            model.Add(positions[a_name]["x"] != positions[b_name]["x"]).OnlyEnforceIf(ab.Not())
        else:  # x2
            model.Add(end_x[a_name] == end_x[b_name]).OnlyEnforceIf(ab)
            model.Add(end_x[a_name] != end_x[b_name]).OnlyEnforceIf(ab.Not())
        aln_bools.append(ab)

    model.Add(total_alignment_bonus == sum(aln_bools) * ALIGNMENT_BONUS)

    # ── Anchor-drift penalty (snap mode only) ─────────────────────────────
    # Penalise how far the anchor drifts from Gemini's intended position.
    # Weight 200 per grid unit — strong enough to prefer minimal movement but
    # weaker than cluster compactness (1000 per grid) so the solver moves the
    # anchor only when doing so meaningfully reduces the cluster perimeter.
    # Manhattan distance: |anc_x - orig_x| + |anc_y - orig_y|
    if _anc_pos_vars is not None:
        DRIFT_W = 200
        drift_x = model.NewIntVar(0, 10**7, "drift_x")
        drift_y = model.NewIntVar(0, 10**7, "drift_y")
        model.AddAbsEquality(drift_x, anc_pos["x"] - anc_x1_g)
        model.AddAbsEquality(drift_y, anc_pos["y"] - anc_y1_g)
        anchor_drift = model.NewIntVar(0, 10**8, "anchor_drift")
        model.Add(anchor_drift == (drift_x + drift_y) * DRIFT_W)
    else:
        anchor_drift = model.NewConstant(0)

    _log("[Solve][DIAG] objective: clperim×1000 + anti_spread({}) + anchor_drift({}) - aln_bonus  "
         "snap_mode={} anti_spread_axis={}".format(
         "cl_w×500" if preferred_side in ("N","S") else "cl_h×500" if preferred_side in ("E","W") else "0",
         "drift×200" if _anc_pos_vars is not None else "0",
         _anc_pos_vars is not None,
         "X(cl_w)" if preferred_side in ("N","S") else "Y(cl_h)" if preferred_side in ("E","W") else "none"))
    model.Minimize(cl_perim * 1000 + anti_spread + anchor_drift - total_alignment_bonus)

    # ── Solve ──────────────────────────────────────────────────────────────
    solver = cp.CpSolver()
    solver.parameters.max_time_in_seconds = SOLVER_TIMEOUT_S
    solver.parameters.num_search_workers  = 4
    solver.parameters.log_search_progress = False

    import time as _t
    _t0 = _t.time()
    status = solver.Solve(model)
    _solve_ms = (_t.time() - _t0) * 1000

    _status_name = {0: "UNKNOWN", 1: "MODEL_INVALID", 2: "FEASIBLE", 3: "INFEASIBLE", 4: "OPTIMAL"}.get(status, str(status))
    if status not in (cp.OPTIMAL, cp.FEASIBLE):
        _log("[Solve] FAILED status={} ({:.0f}ms) side={} enforce={}".format(
             _status_name, _solve_ms, preferred_side, enforce_side))
        return None

    _log("[Solve] OK status={} ({:.0f}ms) obj={:.0f} side={} enforce={}".format(
         _status_name, _solve_ms, solver.ObjectiveValue(), preferred_side, enforce_side))

    # ── Post-solve: polygonal footprint check ─────────────────────────────
    result = _build_result(solver, positions, eff_w, eff_d, rotations, anchor_bounds,
                           anc_pos_vars=_anc_pos_vars, anc_w_g=anc_w_g, anc_d_g=anc_d_g)
    _fl = result["fire_lift"]; _lb = result["lobby"]; _st = result["staircase"]
    _sa = result["solved_anchor_bounds"]
    _log("[Solve] fl=({:.0f},{:.0f},{:.0f},{:.0f}) lb=({:.0f},{:.0f},{:.0f},{:.0f}) "
         "st=({:.0f},{:.0f},{:.0f},{:.0f}) stair_rot={} chain={}".format(
         _fl[0],_fl[1],_fl[2],_fl[3], _lb[0],_lb[1],_lb[2],_lb[3],
         _st[0],_st[1],_st[2],_st[3], result.get("stair_rot","?"), result.get("chain_order","?")))
    _fl_to_anc = min(abs(_fl[2]-_sa[0]), abs(_fl[0]-_sa[2]),
                     abs(_fl[3]-_sa[1]), abs(_fl[1]-_sa[3]))
    _log("[Solve] fire_lift dist_to_anchor={:.0f}mm solved_anchor=({:.0f},{:.0f},{:.0f},{:.0f}) "
         "orig_anchor=({:.0f},{:.0f},{:.0f},{:.0f})".format(
         _fl_to_anc, _sa[0],_sa[1],_sa[2],_sa[3],
         anchor_bounds[0],anchor_bounds[1],anchor_bounds[2],anchor_bounds[3]))

    if footprint_pts and len(footprint_pts) >= 3:
        for key in ("fire_lift", "lobby", "staircase"):
            if not _box_inside_footprint(result[key], footprint_pts):
                _log("[Solve] REJECTED — {} outside footprint".format(key))
                return None

    return result


# ═════════════════════════════════════════════════════════════════════════════
#  Legacy brute-force fallback (when OR-Tools not available)
# ═════════════════════════════════════════════════════════════════════════════

def _blocks_lobby_ends(box, pax_bounds, side, tol=200):
    px1, py1, px2, py2 = pax_bounds
    bx1, by1, bx2, by2 = box
    if side in ("N", "S"):
        return bx1 <= px1 + tol and bx2 >= px2 - tol
    else:
        return by1 <= py1 + tol and by2 >= py2 - tol


def _make_box_along_face(face_x1, face_y1, face_x2, face_y2, side, w, d):
    if side == "N":
        cx = (face_x1 + face_x2) / 2.0
        return (cx - w / 2.0, face_y2, cx + w / 2.0, face_y2 + d)
    elif side == "S":
        cx = (face_x1 + face_x2) / 2.0
        return (cx - w / 2.0, face_y1 - d, cx + w / 2.0, face_y1)
    elif side == "E":
        cy = (face_y1 + face_y2) / 2.0
        return (face_x2, cy - d / 2.0, face_x2 + w, cy + d / 2.0)
    else:
        cy = (face_y1 + face_y2) / 2.0
        return (face_x1 - w, cy - d / 2.0, face_x1, cy + d / 2.0)


def _generate_candidate(pax_bounds, side, chain_order, stair_rot,
                        fl_w, fl_d, lb_w, lb_d, st_w, st_d):
    px1, py1, px2, py2 = pax_bounds

    if side == "N":
        face = (px1, py2, px2, py2)
        perp_dir = "N"
    elif side == "S":
        face = (px1, py1, px2, py1)
        perp_dir = "S"
    elif side == "E":
        face = (px2, py1, px2, py2)
        perp_dir = "E"
    else:
        face = (px1, py1, px1, py2)
        perp_dir = "W"

    if side in ("N", "S"):
        _fl = (fl_w, fl_d); _lb = (lb_w, lb_d)
        _st_0 = (st_w, st_d); _st_90 = (st_d, st_w)
    else:
        _fl = (fl_d, fl_w); _lb = (lb_d, lb_w)
        _st_0 = (st_d, st_w); _st_90 = (st_w, st_d)

    _st = _st_0 if stair_rot == 0 else _st_90

    try:
        if chain_order == "A":
            fl_box = _make_box_along_face(face[0], face[1], face[2], face[3], perp_dir, _fl[0], _fl[1])
            lb_box = _make_box_along_face(fl_box[0], fl_box[1], fl_box[2], fl_box[3], perp_dir, _lb[0], _lb[1])
            st_box = _make_box_along_face(lb_box[0], lb_box[1], lb_box[2], lb_box[3], perp_dir, _st[0], _st[1])
            pax_box = (px1, py1, px2, py2)
            if not (_boxes_abut(pax_box, fl_box) and _boxes_abut(fl_box, lb_box) and _boxes_abut(lb_box, st_box)):
                return None
        elif chain_order == "B":
            lb_box = _make_box_along_face(face[0], face[1], face[2], face[3], perp_dir, _lb[0], _lb[1])
            fl_box = _make_box_along_face(lb_box[0], lb_box[1], lb_box[2], lb_box[3], perp_dir, _fl[0], _fl[1])
            st_box = _make_box_along_face(fl_box[0], fl_box[1], fl_box[2], fl_box[3], perp_dir, _st[0], _st[1])
            pax_box = (px1, py1, px2, py2)
            if not (_boxes_abut(pax_box, lb_box) and _boxes_abut(lb_box, fl_box) and _boxes_abut(fl_box, st_box)):
                return None
        elif chain_order == "NE":
            # Order NE: corner-combined 2×2 grid.
            # Staircase (rotated 90°, long side EW) north/south of anchor, right-flush
            # with anchor east edge.  Fire lift east of anchor, flush with anchor N/S edge.
            # Lobby in the NE/SE corner: adjacent to FL and staircase.
            # Layout (top view, N case):
            #   [ staircase (sd_nat wide × sw_nat deep) | lobby (fl_w × lb_d) ]
            #   [ anchor (PAX bank)                     | fire lift (fl_w × fl_d) ]
            # Valid for all bank widths — when staircase is narrower than anchor there
            # is a notch at the west end of the N face which is architecturally acceptable.
            if side not in ("N", "S"):
                return None  # NE order only defined for N/S orientation
            if side == "N":
                # Layout (top to bottom = N→S):
                #   Row A:  staircase (EW, long side south)  |  lobby (NE corner)
                #   Row B:  PAX bank (anchor)                |  fire lift (east face, top portion)
                # FL sits in the upper portion of the anchor's east face.
                # Staircase and lobby sit north of the anchor.
                st_box = (px2 - _st[0],  py2,           px2,           py2 + _st[1])
                fl_box = (px2,           py2 - _fl[1],  px2 + _fl[0],  py2)
                lb_box = (px2,           py2,           px2 + _lb[0],  py2 + _lb[1])
            else:  # S — mirror vertically
                st_box = (px2 - _st[0],  py1 - _st[1],  px2,           py1)
                fl_box = (px2,           py1,            px2 + _fl[0],  py1 + _fl[1])
                lb_box = (px2,           py1 - _lb[1],   px2 + _lb[0],  py1)
            pax_box_ne = (px1, py1, px2, py2)
            # anchor N/S face abuts st; anchor E face abuts fl; fl abuts lb; st right-corner meets lb left
            if not (_boxes_abut(pax_box_ne, st_box) and _boxes_abut(pax_box_ne, fl_box)
                    and _boxes_abut(fl_box, lb_box)):
                return None
        elif chain_order in ("D", "DW"):
            # Order D: compact 2×2 grid.
            # Staircase and fire_lift both touch the anchor face, stacked side-by-side.
            # Lobby extends away from the anchor, adjacent to fire_lift.
            # With the anchor this forms a near-rectangle: [anchor | st / fl+lb]
            # Order DW: same as D but only valid for rot==1 (EW/rotated staircase).
            # Used when anchor is wider than sd_nat, so NE layout would have a notch
            # but we still want the staircase long side abutting the PAX bank.
            if chain_order == "DW" and stair_rot != 1:
                return None
            if side == "N":
                # N face = py2.  st south-flush with fl, both touch py2.
                # fl at east end; st to the west of fl; lb north of fl.
                fl_box = (px2 - _fl[0],         py2,              px2,              py2 + _fl[1])
                st_box = (px2 - _fl[0] - _st[0], py2,              px2 - _fl[0],     py2 + _st[1])
                lb_box = (px2 - _lb[0],          py2 + _fl[1],     px2,              py2 + _fl[1] + _lb[1])
            elif side == "S":
                # S face = py1.  fl at east end; st to the west; lb south of fl.
                fl_box = (px2 - _fl[0],          py1 - _fl[1],     px2,              py1)
                st_box = (px2 - _fl[0] - _st[0], py1 - _st[1],     px2 - _fl[0],     py1)
                lb_box = (px2 - _lb[0],          py1 - _fl[1] - _lb[1], px2,         py1 - _fl[1])
            elif side == "E":
                # E face = px2.  fl at south end; st north of fl; lb east of fl.
                # _fl=(fl_d, fl_w), _st=(st_d, st_w), _lb=(lb_d, lb_w)
                fl_box = (px2,          py1,              px2 + _fl[0],  py1 + _fl[1])
                st_box = (px2,          py1 + _fl[1],     px2 + _st[0],  py1 + _fl[1] + _st[1])
                lb_box = (px2 + _fl[0], py1,              px2 + _fl[0] + _lb[0], py1 + _lb[1])
            else:  # W
                # W face = px1.  fl at south end; st north of fl; lb west of fl.
                fl_box = (px1 - _fl[0], py1,              px1,           py1 + _fl[1])
                st_box = (px1 - _st[0], py1 + _fl[1],     px1,           py1 + _fl[1] + _st[1])
                lb_box = (px1 - _fl[0] - _lb[0], py1,    px1 - _fl[0],  py1 + _lb[1])
            pax_box_d = (px1, py1, px2, py2)
            if not (_boxes_abut(pax_box_d, fl_box) and _boxes_abut(pax_box_d, st_box)
                    and _boxes_abut(fl_box, lb_box)):
                return None
        else:
            # Order C: compact rectangle — all 3 modules side-by-side parallel to
            # the bank face.  Arrangement: [fl | lb | st] left-to-right (or bottom-
            # to-top for E/W banks).  Each module touches the bank face at its near
            # edge; total width = fl+lb+st; depth = max of the three depths.
            # Centred on the bank face so the core sits in the middle of the bank.
            total_par = _fl[0] + _lb[0] + _st[0]
            if side in ("N", "S"):
                # Centre the row on the bank's X midpoint
                cx = (px1 + px2) / 2.0
                fl_x1 = cx - total_par / 2.0
                fl_x2 = fl_x1 + _fl[0]
                lb_x1 = fl_x2
                lb_x2 = lb_x1 + _lb[0]
                st_x1 = lb_x2
                st_x2 = st_x1 + _st[0]
                if side == "S":
                    fl_box = (fl_x1, py1 - _fl[1], fl_x2, py1)
                    lb_box = (lb_x1, py1 - _lb[1], lb_x2, py1)
                    st_box = (st_x1, py1 - _st[1], st_x2, py1)
                else:  # N
                    fl_box = (fl_x1, py2, fl_x2, py2 + _fl[1])
                    lb_box = (lb_x1, py2, lb_x2, py2 + _lb[1])
                    st_box = (st_x1, py2, st_x2, py2 + _st[1])
            else:
                # E/W bank: centre row on the bank's Y midpoint
                cy = (py1 + py2) / 2.0
                fl_y1 = cy - total_par / 2.0
                fl_y2 = fl_y1 + _fl[0]
                lb_y1 = fl_y2
                lb_y2 = lb_y1 + _lb[0]
                st_y1 = lb_y2
                st_y2 = st_y1 + _st[0]
                if side == "W":
                    fl_box = (px1 - _fl[1], fl_y1, px1, fl_y2)
                    lb_box = (px1 - _lb[1], lb_y1, px1, lb_y2)
                    st_box = (px1 - _st[1], st_y1, px1, st_y2)
                else:  # E
                    fl_box = (px2, fl_y1, px2 + _fl[1], fl_y2)
                    lb_box = (px2, lb_y1, px2 + _lb[1], lb_y2)
                    st_box = (px2, st_y1, px2 + _st[1], st_y2)
            pax_box = (px1, py1, px2, py2)
            # Verify adjacency: fl and lb must touch, lb and st must touch
            if not (_boxes_abut(pax_box, fl_box) and _boxes_abut(pax_box, lb_box)
                    and _boxes_abut(pax_box, st_box) and _boxes_abut(fl_box, lb_box)
                    and _boxes_abut(lb_box, st_box)):
                return None

        for b in (fl_box, lb_box, st_box):
            if b[2] - b[0] < 100 or b[3] - b[1] < 100:
                return None

        pax_box = (px1, py1, px2, py2)
        placed = [pax_box, fl_box, lb_box, st_box]
        for ai in range(len(placed)):
            for bi in range(ai + 1, len(placed)):
                if _boxes_overlap(placed[ai], placed[bi]):
                    return None

        return {"fire_lift": fl_box, "lobby": lb_box, "staircase": st_box}
    except Exception:
        return None


def _validate_candidate(cand, pax_bounds, side, holes, footprint_pts,
                        already_placed, log_prefix, log_fn, chain_order=None):
    def _log(msg):
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    boxes = [cand["fire_lift"], cand["lobby"], cand["staircase"]]

    if holes:
        if any(_box_overlaps_hole(b, h) for b in boxes for h in holes):
            _log("{}: void_collision".format(log_prefix))
            return False, "void_collision"

    if footprint_pts and len(footprint_pts) >= 3:
        if not all(_box_inside_footprint(b, footprint_pts) for b in boxes):
            _log("{}: out_of_footprint".format(log_prefix))
            return False, "out_of_footprint"

    # NE order: staircase intentionally spans the full N face (north of anchor) — skip
    # lobby-ends check since it's not blocking the PAX corridor, which runs E-W under it.
    if chain_order != "NE":
        if any(_blocks_lobby_ends(b, pax_bounds, side) for b in boxes):
            _log("{}: blocks_lobby_ends".format(log_prefix))
            return False, "blocks_lobby_ends"

    if already_placed:
        for nb in boxes:
            for ob in already_placed:
                if _boxes_overlap(nb, ob):
                    _log("{}: overlaps_placed".format(log_prefix))
                    return False, "overlaps_placed"

    score = _cluster_score(boxes, pax_box=pax_bounds)
    _log("{}: VALID score={:.0f}".format(log_prefix, score))
    return True, score


def _legacy_find_layout(anchor_bounds, fire_lift_size, lobby_size, staircase_size,
                        already_placed, footprint_pts, footprint_holes,
                        log_fn, preferred_order, preferred_side):
    """Brute-force 24-candidate fallback used when OR-Tools is not available."""
    def _log(msg):
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    fl_w, fl_d = fire_lift_size
    lb_w, lb_d = lobby_size
    st_w, st_d = staircase_size
    holes = footprint_holes or []
    placed = already_placed or []

    best = None
    best_score = float("inf")

    _sides = ["N", "S", "E", "W"]
    if preferred_side and preferred_side in _sides:
        _sides = [preferred_side] + [s for s in _sides if s != preferred_side]

    _orders = ["NE", "DW", "D", "A", "B", "C"]   # NE=corner 2×2; DW=D rot=1 only; D=2×2 L-shape
    if preferred_order and preferred_order in _orders:
        _orders = [preferred_order] + [o for o in _orders if o != preferred_order]

    # When a preferred_order is set (not None), treat the first valid candidate with
    # that order as the mandatory result — skip score comparison.
    _mandatory_order = preferred_order if preferred_order else None

    for side in _sides:
        for order in _orders:
            for rot in [0, 1]:
                # NE order only makes sense with staircase rotated (rot=1)
                if order == "NE" and rot == 0:
                    continue
                # DW order only valid for rot=1 (EW/rotated staircase) — _generate_candidate
                # also enforces this but skip early to avoid logging degenerate candidates
                if order == "DW" and rot == 0:
                    continue
                cand = _generate_candidate(
                    anchor_bounds, side, order, rot,
                    fl_w, fl_d, lb_w, lb_d, st_w, st_d)
                prefix = "[LayoutEngine] Candidate side={} order={} rot={}".format(side, order, rot)
                if cand is None:
                    _log("{}: degenerate".format(prefix))
                    continue
                ok, result = _validate_candidate(
                    cand, anchor_bounds, side,
                    holes, footprint_pts, placed,
                    prefix, log_fn, chain_order=order)
                if ok:
                    cand_result = {
                        "fire_lift":   cand["fire_lift"],
                        "lobby":       cand["lobby"],
                        "staircase":   cand["staircase"],
                        "attach_side": side,
                        "chain_order": order,
                        "stair_rot":   rot,
                        "score":       result,
                    }
                    if _mandatory_order and order == _mandatory_order:
                        # First valid candidate matching the mandatory order wins immediately.
                        best = cand_result
                        _log("[LayoutEngine] Mandatory order={} matched — using first valid".format(order))
                        break
                    if result < best_score:
                        best_score = result
                        best = cand_result
            if best and _mandatory_order and best["chain_order"] == _mandatory_order:
                break
        if best and _mandatory_order and best["chain_order"] == _mandatory_order:
            break

    if best:
        _log("[LayoutEngine] (legacy) Selected: side={} order={} rot={} score={:.0f}".format(
            best["attach_side"], best["chain_order"], best["stair_rot"], best["score"]))
    else:
        _log("[LayoutEngine] (legacy) No valid candidate")

    return best


# ═════════════════════════════════════════════════════════════════════════════
#  Public API — single-set entry point (mandatory export)
# ═════════════════════════════════════════════════════════════════════════════

def find_layout_for_set(
        anchor_bounds,
        fire_lift_size,
        lobby_size,
        staircase_size,
        already_placed=None,
        footprint_pts=None,
        footprint_holes=None,
        log_fn=None,
        preferred_order=None,
        preferred_side=None,
        pax_lobby_bounds=None,
        allowed_sides=None,
        anchor_snap_zone=None):
    """
    Find the most compact valid arrangement of {FireLift, Lobby, Staircase}
    relative to anchor_bounds, avoiding boxes in already_placed.

    Uses OR-Tools CP-SAT when available; falls back to the legacy brute-force
    engine when ortools is not installed.

    Args:
        anchor_bounds:    (x1, y1, x2, y2) mm — full passenger lift bank box
        fire_lift_size:   (w, d) mm
        lobby_size:       (w, d) mm
        staircase_size:   (w, d) mm
        already_placed:   list of (x1,y1,x2,y2) obstacles
        footprint_pts:    [[x,y],...] outer building polygon (None = rectangular)
        footprint_holes:  [[[x,y],...]] void polygons
        log_fn:           optional callable(str) for diagnostic logging
        preferred_order:  str "A"/"B"/"C" — hint for chain order
        preferred_side:   str "N"/"S"/"E"/"W" — try this side first
        pax_lobby_bounds: (x1,y1,x2,y2) mm — passenger lift corridor strip.
            Rule 4 (corridor ends open) is applied against this box instead of
            the full anchor bank, so the solver knows which specific ends to keep
            clear for passengers to walk through.
        allowed_sides:    list of str e.g. ["N","S"] — restrict which sides the
            solver may try.  None means all four sides are allowed.  Use ["N","S"]
            for EW-oriented banks (cluster must exit N or S, never E/W) and
            ["E","W"] for NS-oriented banks.
        anchor_snap_zone: (zx1, zy1, zx2, zy2) mm — OR-Tools may slide the
            anchor within this zone to find a feasible position.  The bank size
            is preserved; only its top-left corner moves.  A drift penalty in the
            objective keeps the anchor near anchor_bounds unless forced away.
            None = anchor is fixed at anchor_bounds (default / legacy behaviour).

    Returns:
        dict: fire_lift, lobby, staircase, attach_side, chain_order, stair_rot,
              score, solved_anchor_bounds
            stair_rot is 0 or 90 (degrees).
            solved_anchor_bounds is the (possibly shifted) bank bounding box.
        or None if no valid layout found.
    """
    def _log(msg):
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    if not _ORTOOLS_AVAILABLE or preferred_order == "NE":
        if not _ORTOOLS_AVAILABLE:
            _log("[LayoutEngine] ortools not available ({}), using legacy engine".format(
                _ORTOOLS_IMPORT_ERROR or "unknown error"))
        else:
            _log("[LayoutEngine] preferred_order=NE — using legacy engine (OR-Tools cannot enforce corner topology)")
        return _legacy_find_layout(
            anchor_bounds, fire_lift_size, lobby_size, staircase_size,
            already_placed, footprint_pts, footprint_holes,
            log_fn, preferred_order, preferred_side)

    modules_mm = {
        "fire_lift":  fire_lift_size,
        "lobby":      lobby_size,
        "staircase":  staircase_size,
    }

    # Build the side trial order, filtered by allowed_sides
    _all_sides = ["S", "N", "E", "W"]
    if allowed_sides:
        _all_sides = [s for s in _all_sides if s in allowed_sides]
        if preferred_side and preferred_side not in _all_sides:
            preferred_side = None  # preferred side is outside allowed set — ignore it

    # Pass 1: try preferred side as hard constraint
    result = None
    if preferred_side:
        _log("[LayoutEngine] OR-Tools: pass 1 — enforcing preferred_side={}".format(preferred_side))
        result = _solve(anchor_bounds, modules_mm, already_placed,
                        footprint_pts, footprint_holes,
                        preferred_side, log_fn,
                        enforce_side=True,
                        pax_lobby_bounds=pax_lobby_bounds,
                        anchor_snap_zone=anchor_snap_zone)
        if result:
            _log("[LayoutEngine] OR-Tools: solved on preferred side={} score={:.0f}".format(
                result["attach_side"], result["score"]))
            return result
        _log("[LayoutEngine] OR-Tools: preferred side infeasible, retrying unconstrained")

    # Pass 2: try each allowed side without hard side constraint, take first feasible
    _sides_order = list(_all_sides)
    if preferred_side:
        _sides_order = [preferred_side] + [s for s in _sides_order if s != preferred_side]
    if allowed_sides:
        _log("[LayoutEngine] OR-Tools: pass 2 — trying sides {} (allowed={})".format(
            _sides_order, allowed_sides))
    best_result = None
    for _side in _sides_order:
        result = _solve(anchor_bounds, modules_mm, already_placed,
                        footprint_pts, footprint_holes,
                        _side, log_fn,
                        enforce_side=False,
                        pax_lobby_bounds=pax_lobby_bounds,
                        anchor_snap_zone=anchor_snap_zone)
        if result:
            if best_result is None or result["score"] < best_result["score"]:
                best_result = result
        # Try all sides — the best score (smallest total-core bounding box) wins.
    result = best_result
    if result:
        _log("[LayoutEngine] OR-Tools: solved side={} stair_rot={} score={:.0f}".format(
            result["attach_side"], result["stair_rot"], result["score"]))
    else:
        _log("[LayoutEngine] OR-Tools: no valid solution (allowed_sides={})".format(allowed_sides))

    return result


# ═════════════════════════════════════════════════════════════════════════════
#  Public API — convenience wrapper
# ═════════════════════════════════════════════════════════════════════════════

def find_best_core_layout(
        pax_bank_bounds,
        fire_lift_size,
        lobby_size,
        staircase_size,
        footprint_pts=None,
        footprint_holes=None,
        num_banks=1,  # noqa: ARG001
        log_fn=None,
        pax_lobby_bounds=None):
    """
    Convenience wrapper for single-set callers.
    Delegates to find_layout_for_set with no obstacles or side preference.
    num_banks: reserved for future multi-bank support, not used by solver.
    """
    return find_layout_for_set(
        anchor_bounds    = pax_bank_bounds,
        fire_lift_size   = fire_lift_size,
        lobby_size       = lobby_size,
        staircase_size   = staircase_size,
        already_placed   = None,
        footprint_pts    = footprint_pts,
        footprint_holes  = footprint_holes,
        log_fn           = log_fn,
        pax_lobby_bounds = pax_lobby_bounds,
    )
