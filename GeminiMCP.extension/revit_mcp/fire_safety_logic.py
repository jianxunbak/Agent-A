# -*- coding: utf-8 -*-
import math
from . import staircase_logic
from .utils import safe_num

# ─────────────────────────────────────────────────────────────────────────────
#  Load compliance data from JSON files — single source of truth.
#  Falls back to hardcoded values if files are missing.
# ─────────────────────────────────────────────────────────────────────────────

def _load_fsc():
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

_FSC, _STC      = _load_fsc()
_WALL_THICKNESS = _STC.get("wall_thickness_mm", {}).get("core_structural",  350)
_OVERRUN_HEIGHT = _FSC.get("staircase", {}).get("overrun_height_mm",       5000)
_FL_CAR_SIZE    = _FSC.get("fire_lift", {}).get("car_size_mm",             2500)
_FL_PLATFORM_MIN = _FSC.get("fire_lift", {}).get("platform_min_mm",        3000)
_FL_SHAFT_D     = max(_FL_CAR_SIZE, _FL_PLATFORM_MIN) + 2 * _WALL_THICKNESS
_MAX_TRAVEL     = _FSC.get("staircase", {}).get("max_travel_distance_mm", 60000)
_PERIMETER_C    = _FSC.get("perimeter_staircase", {})
_EDGE_GAP_MM    = _PERIMETER_C.get("edge_inset_gap_mm",                     500)
_SMOKE_CLEAR_D  = _PERIMETER_C.get("smoke_stop_lobby_clear_depth_mm",      2000)

from . import lift_logic


def _fsl_log(msg):
    try:
        import os, time
        from .utils import get_log_path
        with open(get_log_path(), "a", encoding="utf-8") as _f:
            _f.write("[{}] {}\n".format(time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _passenger_lift_row_centers(center_pos_mm, layout, lobby_width):
    """Return (row1_cy, row2_cy) in mm — the Y-centre of each passenger lift row.

    For a single-block, 2-row layout (>=4 lifts per block):
        row1 faces south, row2 faces north.
    For a single-row layout (<4 lifts): both entries = block centre Y.
    Multi-block layouts return the outermost rows of the entire assembly.
    """
    l = 2500  # default internal lift car depth
    shaft_depth = l + 2 * _WALL_THICKNESS  # 2900 mm

    num_blocks = layout["num_blocks"]
    lifts_per_block = layout["lifts_per_block"]
    block_d = layout["block_d"]
    cy = center_pos_mm[1]

    if lifts_per_block >= 4:
        # 2-row block; row offsets relative to each block centre
        r1_offset = -(shaft_depth + lobby_width / 2.0) + shaft_depth / 2.0   # row1 centre offset
        r2_offset =  (lobby_width / 2.0)               + shaft_depth / 2.0   # row2 centre offset

        # Block 0 = southernmost block
        b0_cy = cy + lift_logic.get_block_y_offset(0, num_blocks, block_d)
        # Block last = northernmost block
        bN_cy = cy + lift_logic.get_block_y_offset(num_blocks - 1, num_blocks, block_d)

        row1_cy = b0_cy + r1_offset   # southernmost row centre
        row2_cy = bN_cy + r2_offset   # northernmost row centre
    else:
        # Single row — both fire lifts align to the single row centre
        row1_cy = cy
        row2_cy = cy

    return row1_cy, row2_cy


def _fire_lift_shaft_walls(tag, cx_mm, cy_mm, fw_mm, fd_mm, levels_data, overrun_height=_OVERRUN_HEIGHT, skip_wall=None, skip_walls=None):
    """Generate walls + topcap for a single fire-fighting lift shaft.

    skip_wall:  single direction "N"/"S"/"E"/"W" (legacy, still accepted).
    skip_walls: set/list of directions to skip (superset of skip_wall).
    """
    _skip = set()
    if skip_wall:
        _skip.add(skip_wall)
    if skip_walls:
        _skip.update(skip_walls)

    walls = []
    floors = []
    hfw, hfd = fw_mm / 2.0, fd_mm / 2.0
    x1, x2 = cx_mm - hfw, cx_mm + hfw
    y1, y2 = cy_mm - hfd, cy_mm + hfd

    for l_idx, lvl in enumerate(levels_data):
        lvl_id, elev = lvl['id'], lvl['elevation']
        is_last = (l_idx == len(levels_data) - 1)
        h = overrun_height if is_last else (levels_data[l_idx + 1]['elevation'] - elev)
        if h <= 0:
            continue
        common = {"level_id": lvl_id, "height": h, "type": "AI_Wall_Core"}
        if "S" not in _skip:
            walls.append({"id": "AI_{}_S_L{}".format(tag, l_idx + 1), "start": [x1, y1, 0], "end": [x2, y1, 0], **common})
        if "N" not in _skip:
            walls.append({"id": "AI_{}_N_L{}".format(tag, l_idx + 1), "start": [x1, y2, 0], "end": [x2, y2, 0], **common})
        if "W" not in _skip:
            walls.append({"id": "AI_{}_W_L{}".format(tag, l_idx + 1), "start": [x1, y1, 0], "end": [x1, y2, 0], **common})
        if "E" not in _skip:
            walls.append({"id": "AI_{}_E_L{}".format(tag, l_idx + 1), "start": [x2, y1, 0], "end": [x2, y2, 0], **common})
        if is_last:
            cap_elev = elev + overrun_height
            floors.append({"id": "AI_{}_TOPCAP".format(tag), "level_id": lvl_id, "elevation": cap_elev,
                           "points": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]})
    return walls, floors


def _should_use_ew_orientation(_lift_core_bounds_mm, _sw_nat, _sd_nat,
                               orientation="AUTO", floor_dims=None):
    """Return True if EW orientation should be used for the core stack.

    EW layout: lift row runs N-S, stairs at E/W ends.
    NS layout: lift row runs E-W, stairs at N/S ends (default).

    orientation: "NS" / "EW" — explicit override from manifest.
                 "AUTO" — auto-select based on floor plate aspect ratio and
                          available clearance N/S of the lift core.
    """
    # Physical feasibility check: if either the north or south side of the lift
    # core lacks enough clearance for a staircase shaft, NS layout is impossible
    # regardless of the requested orientation.  Both fire sets need their own
    # clear side (one N, one S); if one side is blocked the second set has
    # nowhere to go and the layout engine produces a scrambled result.
    # This happens when Gemini pushes the core near a building edge to avoid a void.
    if _lift_core_bounds_mm and floor_dims:
        _avg_l = sum(d[1] for d in floor_dims) / len(floor_dims)
        l_xmin, l_ymin, l_xmax, l_ymax = _lift_core_bounds_mm
        # Determine the building's south/north edge in the same coordinate frame.
        # Gemini emits coordinates with the SW corner at (0,0), so l_ymin is the
        # absolute distance from the south wall.  In legacy centred-origin tests
        # the core straddles Y=0 (l_ymin < 0); in that case infer edges from avg_l.
        if l_ymin >= 0:
            _bld_south = 0.0
            _bld_north = _avg_l
        else:
            _core_cy = (l_ymin + l_ymax) / 2.0
            _bld_south = _core_cy - _avg_l / 2.0
            _bld_north = _core_cy + _avg_l / 2.0
        _MARGIN = 500  # edge gap + wall thickness tolerance
        _space_n = _bld_north - l_ymax
        _space_s = l_ymin - _bld_south
        if _space_n < _sd_nat + _MARGIN or _space_s < _sd_nat + _MARGIN:
            return True  # force EW regardless of requested orientation

    if orientation == "EW":
        return True
    if orientation == "NS":
        return False
    # AUTO: choose EW when the floor plate is significantly wider than deep
    if floor_dims:
        avg_w = sum(d[0] for d in floor_dims) / len(floor_dims)
        avg_l = sum(d[1] for d in floor_dims) / len(floor_dims)
        if avg_w > avg_l * 1.5:
            return True
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _nearest_polygon_edge_angle_deg(px, py, polygon_pts):
    """Return angle (degrees, 0=east, CCW) of the polygon edge nearest to (px, py),
    snapped to the nearest 90° so stair walls remain axis-aligned."""
    best_dist = float('inf')
    best_angle = 0.0
    n = len(polygon_pts)
    for i in range(n):
        x1, y1 = polygon_pts[i][0], polygon_pts[i][1]
        x2, y2 = polygon_pts[(i + 1) % n][0], polygon_pts[(i + 1) % n][1]
        dx, dy = x2 - x1, y2 - y1
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq < 1.0:
            continue
        t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / seg_len_sq))
        nx, ny = x1 + t * dx, y1 + t * dy
        dist = math.sqrt((px - nx) ** 2 + (py - ny) ** 2)
        if dist < best_dist:
            best_dist = dist
            best_angle = math.degrees(math.atan2(dy, dx))
    return round(best_angle / 90.0) * 90.0


# ---------------------------------------------------------------------------
# Phase 2 — Core Layout standalone geometry helpers
# ---------------------------------------------------------------------------

_EDGE_MATCH_TOL = 50  # mm — tolerance for shared-wall edge matching


def _polygon_walls(tag, polygon, levels_data, overrun_height, skip_shared_walls=None, wall_type="AI_Wall_Core"):
    """Generate walls along each polygon edge, skipping shared faces.

    Returns (walls_list, topcap_floor_or_None).
    skip_shared_walls: list of [[x1,y1],[x2,y2]] segments to omit.
    """
    def _edge_matches(p1, p2, skip_list):
        for s in skip_list:
            s1, s2 = s[0], s[1]
            d11 = math.sqrt((p1[0] - s1[0]) ** 2 + (p1[1] - s1[1]) ** 2)
            d22 = math.sqrt((p2[0] - s2[0]) ** 2 + (p2[1] - s2[1]) ** 2)
            d12 = math.sqrt((p1[0] - s2[0]) ** 2 + (p1[1] - s2[1]) ** 2)
            d21 = math.sqrt((p2[0] - s1[0]) ** 2 + (p2[1] - s1[1]) ** 2)
            if (d11 < _EDGE_MATCH_TOL and d22 < _EDGE_MATCH_TOL) or \
               (d12 < _EDGE_MATCH_TOL and d21 < _EDGE_MATCH_TOL):
                return True
        return False

    skip_list = skip_shared_walls or []
    n = len(polygon)
    walls = []

    for l_idx, lvl in enumerate(levels_data):
        lvl_id = lvl['id']
        is_last = (l_idx == len(levels_data) - 1)
        h = overrun_height if is_last else (levels_data[l_idx + 1]['elevation'] - lvl['elevation'])
        if h <= 0:
            continue
        common = {"level_id": lvl_id, "height": h, "type": wall_type}
        for i in range(n):
            p1 = polygon[i]
            p2 = polygon[(i + 1) % n]
            if _edge_matches(p1, p2, skip_list):
                continue
            if abs(p1[0] - p2[0]) < 2 and abs(p1[1] - p2[1]) < 2:
                continue
            walls.append({
                "id": "AI_{}_E{}_L{}".format(tag, i + 1, l_idx + 1),
                "start": [p1[0], p1[1], 0],
                "end":   [p2[0], p2[1], 0],
                **common
            })

    topcap = None
    if levels_data:
        last_lvl = levels_data[-1]
        topcap = {
            "id": "AI_{}_TOPCAP".format(tag),
            "level_id": last_lvl['id'],
            "elevation": last_lvl['elevation'] + overrun_height,
            "points": [[p[0], p[1]] for p in polygon]
        }
    return walls, topcap


_CONNECTION_TO_DOOR_CATEGORY = {
    "lift_door":          "lift",
    "fire_lift_door":     "lift",
    "stair_lobby_door":   "single_leaf",
    "lobby_entry_door":   "single_leaf",
    "stair_main_landing": "single_leaf",
    "stair_exit_door":    "single_leaf",
}


def _door_spec_from_relation(element_id, relation, all_lvl_ids, first_lvl_id,
                             wall_ai_id_map=None):
    """Convert a single relation into a door spec dict, or None if no door needed.

    wall_ai_id_map: optional {level_id: ai_wall_id} dict pre-computed by the
    caller so the Revit worker can find the wall by registry ID rather than
    falling back to the slow geometry scan.
    """
    connection  = relation.get("connection", "")
    door_pos    = relation.get("door_position")
    shared_wall = relation.get("shared_wall")
    adjacent_to = relation.get("adjacent_to", "ext")

    if not door_pos or not shared_wall:
        return None
    cat = _CONNECTION_TO_DOOR_CATEGORY.get(connection)
    if not cat:
        return None

    ground_only = (connection == "stair_exit_door")
    levels = [first_lvl_id] if (ground_only and first_lvl_id) else list(all_lvl_ids)

    spec = {
        "id": "{}_{}_{}".format(element_id, adjacent_to or "ext", connection),
        "position_mm": door_pos,
        "wall_line_mm": shared_wall,
        "levels": levels,
        "swing_in": True,
        "flip_hand": True,
        "min_width_mm": 900 if cat == "lift" else 1000,
        "door_category": cat,
        "swing_out_level1": connection != "stair_exit_door",
    }
    if wall_ai_id_map:
        spec["wall_ai_id_map"] = wall_ai_id_map
    return spec


def generate_core_layout_manifest(elements, levels_data, stair_spec=None,
                                   typical_floor_height_mm=None, compliance_overrides=None,
                                   num_lifts=None, lobby_width=3000):
    """Generate walls, floors, voids, and door specs for a solver-placed core layout.

    Each element type uses the same pre-built, self-contained module builder
    that the hardcoded path uses — but anchored to the solver's footprint
    bounding box rather than a computed centre:

      passenger_lift_core → generate_lift_shaft_from_polygon (internal cells)
                            + _polygon_walls for outer boundary
      fire_lift_car       → _fire_lift_shaft_walls (4-wall shaft)
      fire_lobby          → _polygon_walls (enclosure)
      staircase           → generate_staircase_manifest with base_y_override
                            = footprint y1, enclosure_width = footprint width

    Shared edges are deduplicated via _edge_owner so the first element that
    owns a boundary creates the wall; the second element skips it.

    wall_ai_id_map is populated on every door spec so the Revit worker can
    find the host wall by registry ID without relying on the slow geometry scan.
    """
    co = compliance_overrides or {}
    overrun_height = co.get("overrun_height_mm", _OVERRUN_HEIGHT)
    typ_h = typical_floor_height_mm or 4000

    all_lvl_ids  = [lvl['id'] for lvl in levels_data[:-1]] if len(levels_data) > 1 else ([levels_data[0]['id']] if levels_data else [])
    first_lvl_id = all_lvl_ids[0] if all_lvl_ids else None

    walls, floors, voids, door_specs, sub_boundaries = [], [], [], [], []
    stair_center_tuples = []
    stair_global_idx = 0

    def _edge_key(p1, p2):
        r = 10
        ax, ay = round(p1[0] / r) * r, round(p1[1] / r) * r
        bx, by = round(p2[0] / r) * r, round(p2[1] / r) * r
        return (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))

    # Maps edge_key → (owner_tag, edge_index_in_polygon)
    # owner_tag is the string used in wall AI IDs: e.g. "PassengerLifts_outer"
    _edge_owner = {}  # edge_key -> (tag, edge_idx)

    # Per-element type and bounds — used in _pending_doors to build correct
    # wall AI IDs (fire_lift_car uses cardinal naming, others use edge-index).
    _el_types  = {}   # tag → element type string
    _el_bounds = {}   # tag → (fp_x1, fp_y1, fp_x2, fp_y2)

    # Pending door relations — processed after all walls are built so
    # wall_ai_id_map can reference already-registered edge owners.
    _pending_doors = []  # list of (element_id, relation)

    for el in elements:
        el_id   = el["id"]
        el_type = el.get("type", "")
        relations = el.get("relations", [])

        if el_type == "staircase":
            footprint    = el.get("footprint", [])
            rotation_deg = float(el.get("rotation", 0.0))

            # Use the solver footprint bounding box directly.
            # fp_y1 = southern (bottom) edge of the staircase box.
            # fp_cx = east-west centre.
            # fp_w  = enclosure width — matches solver's stair_width.
            if footprint and len(footprint) >= 3:
                fp_xs = [p[0] for p in footprint]
                fp_ys = [p[1] for p in footprint]
                fp_x1, fp_y1 = min(fp_xs), min(fp_ys)
                fp_x2, fp_y2 = max(fp_xs), max(fp_ys)
                fp_cx = (fp_x1 + fp_x2) / 2.0
                fp_w  = fp_x2 - fp_x1
            else:
                center  = el.get("center", [0, 0])
                fp_cx   = center[0]
                fp_y1   = center[1] - staircase_logic.get_max_shaft_depth(levels_data, stair_spec, typ_h) / 2.0
                fp_w    = staircase_logic.get_shaft_dimensions(typ_h, stair_spec)[0]

            sw_nat = staircase_logic.get_shaft_dimensions(typ_h, stair_spec)[0]
            sd_nat = staircase_logic.get_max_shaft_depth(levels_data, stair_spec, typ_h)

            fp_d  = fp_y2 - fp_y1 if footprint and len(footprint) >= 3 else sd_nat
            fp_cy = (fp_y1 + fp_y2) / 2.0 if footprint and len(footprint) >= 3 else fp_y1 + sd_nat / 2.0
            # Footprint wider in x than y → East/West staircase placed by solver after Fix 1
            # dimension swap.  Generate unrotated shaft (sw_nat × sd_nat) then rotate 90° so
            # the final enclosure occupies the solver footprint correctly.
            _is_ew = fp_w > fp_d and fp_d > 10

            if _is_ew:
                enc_w     = sw_nat
                st_ctr_y  = fp_cy
                base_y_ov = fp_cy - sd_nat / 2.0
                _rot_degs = [90.0]
                void_enc_w = sd_nat
                void_enc_d = sw_nat
            else:
                enc_w     = max(fp_w, sw_nat)
                st_ctr_y  = fp_y1 + sd_nat / 2.0
                base_y_ov = fp_y1
                _rot_degs = [rotation_deg] if rotation_deg else None
                void_enc_w = enc_w
                void_enc_d = sd_nat

            st_man = staircase_logic.generate_staircase_manifest(
                [(fp_cx, st_ctr_y)], levels_data, enc_w, stair_spec,
                typ_h, lift_core_bounds_mm=None, num_lifts=None, lobby_width=lobby_width,
                base_y_override=base_y_ov, rotated_indices=[],
                stair_idx_offset=stair_global_idx, compliance_overrides=co,
                rotation_degs=_rot_degs
            )
            stair_global_idx += 1
            walls.extend(st_man.get("walls", []))
            floors.extend(st_man.get("floors", []))
            voids.extend(staircase_logic.get_void_rectangles_mm(
                [(fp_cx, st_ctr_y)], void_enc_w, void_enc_d,
                lift_core_bounds_mm=None, num_lifts=None, lobby_width=lobby_width,
                base_y_override=base_y_ov
            ))
            if footprint and len(footprint) >= 3:
                sub_boundaries.append({"id": el_id, "rect": [fp_x1, fp_y1, fp_x2, fp_y2]})
            else:
                sub_boundaries.append({"id": el_id, "rect": [fp_cx - enc_w / 2.0, fp_y1,
                                                               fp_cx + enc_w / 2.0, fp_y1 + sd_nat]})
            stair_center_tuples.append((
                float(fp_cx), float(st_ctr_y),
                bool(_rot_degs), float(_rot_degs[0]) if _rot_degs else None
            ))

        else:
            footprint = el.get("footprint", [])
            if len(footprint) < 3:
                continue
            xs = [p[0] for p in footprint]
            ys = [p[1] for p in footprint]
            fp_x1, fp_y1 = min(xs), min(ys)
            fp_x2, fp_y2 = max(xs), max(ys)
            fp_cx = (fp_x1 + fp_x2) / 2.0
            fp_cy = (fp_y1 + fp_y2) / 2.0
            fp_w  = fp_x2 - fp_x1
            fp_d  = fp_y2 - fp_y1
            sub_boundaries.append({"id": el_id, "rect": [fp_x1, fp_y1, fp_x2, fp_y2]})

            if el_type == "passenger_lift_core" and num_lifts:
                # Internal shaft: car cells + dividers centred on footprint centroid.
                pax_man = lift_logic.generate_lift_shaft_from_polygon(
                    footprint, num_lifts, levels_data, lobby_width=lobby_width)
                # Filter out outer perimeter walls (W_Front/Back/Left/Right) —
                # _polygon_walls below generates them with consistent AI IDs that
                # wall_ai_id_map references in door specs. Including both sets
                # creates coincident duplicate walls in Revit.
                _OUTER_TOL = 50  # mm — tolerance for "same position as outer boundary"
                for w in pax_man.get("walls", []):
                    wid = w.get("id", "")
                    ws  = w.get("start", [0, 0])
                    we  = w.get("end",   [0, 0])

                    is_perimeter_name = ("_W_Front" in wid or "_W_Back" in wid
                                        or "_W_Left" in wid or "_W_Right" in wid)
                    if not is_perimeter_name:
                        walls.append(w)  # dividers / topcap — always keep
                        continue

                    # Check if the wall lies ON the outer footprint boundary
                    # Horizontal wall: both endpoints share a Y that matches fp_y1 or fp_y2
                    _is_horiz = abs(ws[1] - we[1]) < 1
                    # Vertical wall: both endpoints share an X that matches fp_x1 or fp_x2
                    _is_vert  = abs(ws[0] - we[0]) < 1

                    _on_boundary = False
                    if _is_horiz:
                        _on_boundary = (abs(ws[1] - fp_y1) < _OUTER_TOL or
                                        abs(ws[1] - fp_y2) < _OUTER_TOL)
                    elif _is_vert:
                        _on_boundary = (abs(ws[0] - fp_x1) < _OUTER_TOL or
                                        abs(ws[0] - fp_x2) < _OUTER_TOL)

                    if _on_boundary:
                        continue  # _polygon_walls below generates this edge — skip duplicate
                    walls.append(w)  # interior wall — keep for door placement
                floors.extend(pax_man.get("floors", []))

                # Outer boundary walls — use tag "PassengerLifts_outer" so IDs
                # are deterministic and wall_ai_id_map can reference them.
                outer_tag = el_id + "_outer"
                n_fp = len(footprint)
                for i in range(n_fp):
                    ek = _edge_key(footprint[i], footprint[(i + 1) % n_fp])
                    if ek not in _edge_owner:
                        _edge_owner[ek] = (outer_tag, i + 1)
                pax_outer, pax_topcap = _polygon_walls(
                    outer_tag, footprint, levels_data, overrun_height,
                    skip_shared_walls=[]  # outer boundary always fully built
                )
                walls.extend(pax_outer)
                if pax_topcap:
                    floors.append(pax_topcap)

            elif el_type == "fire_lift_car":
                # Use the pre-built fire lift shaft builder (same as hardcoded path).
                # It generates all 4 walls + topcap for every level.
                fl_walls, fl_floors = _fire_lift_shaft_walls(
                    el_id, fp_cx, fp_cy, fp_w, fp_d, levels_data, overrun_height)

                # Track bounds and type so _pending_doors can use cardinal face IDs
                # (fire_lift_car walls are named _S/_N/_W/_E, not _E1/_E2 etc.).
                _el_types[el_id]  = el_type
                _el_bounds[el_id] = (fp_x1, fp_y1, fp_x2, fp_y2)

                # Deduplicate: drop individual wall segments whose edge is already
                # owned by a prior element (e.g. the PassengerLifts outer boundary).
                shared_edges_fl = {_edge_key(r["shared_wall"][0], r["shared_wall"][1])
                                   for r in relations if r.get("shared_wall")}
                n_fp = len(footprint)
                for i in range(n_fp):
                    ek = _edge_key(footprint[i], footprint[(i + 1) % n_fp])
                    if ek not in _edge_owner:
                        _edge_owner[ek] = (el_id, i + 1)

                # _fire_lift_shaft_walls names walls AI_{tag}_{S/N/W/E}_L{n}.
                # Skip walls whose edge was already built by a prior element.
                for w in fl_walls:
                    s = w.get("start", [0, 0])
                    e = w.get("end",   [0, 0])
                    ek = _edge_key([s[0], s[1]], [e[0], e[1]])
                    if ek in shared_edges_fl and _edge_owner.get(ek, (el_id,))[0] != el_id:
                        continue  # already built by PassengerLifts outer boundary
                    walls.append(w)
                floors.extend(fl_floors)

                # Internal partition walls — divide zone into individual car spaces.
                # Each wall spans the zone's SHORT axis; partitions placed at equal
                # intervals along the zone's LONG axis.
                _fl_car_sz = co.get("fire_lift_car_size_mm", _FL_CAR_SIZE)
                _shaft_sz  = _fl_car_sz + 2 * _WALL_THICKNESS
                _n_cars    = max(2, _FSC.get("fire_lift", {}).get("min_count", 2))
                _lvl_iter2 = levels_data[:-1] if len(levels_data) > 1 else levels_data
                if fp_d >= fp_w and fp_d > _shaft_sz:
                    # EW layout: zone is long in NS (Y) — partitions are horizontal (along X)
                    for _k in range(1, _n_cars):
                        _py = round((fp_y1 + fp_d * _k / _n_cars) / 10) * 10
                        for _li2, _lv2 in enumerate(_lvl_iter2):
                            _h2 = levels_data[_li2 + 1]['elevation'] - _lv2['elevation']
                            walls.append({
                                "id": "AI_{}_INT{}_L{}".format(el_id, _k, _li2 + 1),
                                "start": [fp_x1, _py, 0], "end": [fp_x2, _py, 0],
                                "level_id": _lv2['id'], "height": _h2,
                                "type": "AI_Wall_Core",
                            })
                elif fp_w > fp_d and fp_w > _shaft_sz:
                    # NS layout: zone is long in EW (X) — partitions are vertical (along Y)
                    for _k in range(1, _n_cars):
                        _px = round((fp_x1 + fp_w * _k / _n_cars) / 10) * 10
                        for _li2, _lv2 in enumerate(_lvl_iter2):
                            _h2 = levels_data[_li2 + 1]['elevation'] - _lv2['elevation']
                            walls.append({
                                "id": "AI_{}_INT{}_L{}".format(el_id, _k, _li2 + 1),
                                "start": [_px, fp_y1, 0], "end": [_px, fp_y2, 0],
                                "level_id": _lv2['id'], "height": _h2,
                                "type": "AI_Wall_Core",
                            })

            else:
                # fire_lobby, smoke_stop_lobby, and any other polygon-based space.
                # smoke_stop_lobby uses identical wall geometry to fire_lobby — only
                # the semantic type differs (no fire lift shaft adjacent, SCDF lobby only).
                shared_edges = [r["shared_wall"] for r in relations if r.get("shared_wall")]
                skip_already_built = []
                n_fp = len(footprint)
                for i in range(n_fp):
                    ek = _edge_key(footprint[i], footprint[(i + 1) % n_fp])
                    if ek in _edge_owner:
                        # Edge already built by prior element — check if it's a shared wall
                        for sw in shared_edges:
                            if _edge_key(sw[0], sw[1]) == ek:
                                skip_already_built.append(sw)
                    else:
                        _edge_owner[ek] = (el_id, i + 1)

                el_walls, el_topcap = _polygon_walls(
                    el_id, footprint, levels_data, overrun_height,
                    skip_shared_walls=skip_already_built
                )
                walls.extend(el_walls)
                if el_topcap:
                    floors.append(el_topcap)

        _pending_doors.extend((el_id, rel) for rel in relations)

    # ── Build door specs with wall_ai_id_map ─────────────────────────────────
    # For each door relation, find which element owns the shared wall edge and
    # build the deterministic AI wall ID per level.
    # • fire_lift_car walls: AI_{tag}_{S/N/W/E}_L{n}  (cardinal suffix)
    # • all other walls:     AI_{tag}_E{edge_idx}_L{n} (edge-index suffix)
    # This lets the Revit worker locate the wall by registry ID instead of
    # falling back to the slow 600mm geometry scan.
    for (el_id, rel) in _pending_doors:
        shared_wall = rel.get("shared_wall")
        if not shared_wall:
            continue
        ek = _edge_key(shared_wall[0], shared_wall[1])
        owner_info = _edge_owner.get(ek)  # (tag, edge_idx) or None

        # Containment fallback: _find_shared_wall returns a PARTIAL segment when
        # two adjacent zones differ in their perpendicular extents (e.g. fire_lift_car
        # 8900mm NS shares only 3050mm with a staircase).  The partial segment's edge
        # key won't match the full registered edge, so scan all registered edges for
        # the one with the greatest axis-aligned overlap with the shared_wall segment.
        if not owner_info:
            _ex1, _ey1, _ex2, _ey2 = ek
            _best_ovl = 0
            for _reg_ek, _reg_info in _edge_owner.items():
                _rx1, _ry1, _rx2, _ry2 = _reg_ek
                if _rx1 == _ex1 and _rx2 == _ex2:      # same X → vertical edge
                    _ovl = max(0, min(_ry2, _ey2) - max(_ry1, _ey1))
                elif _ry1 == _ey1 and _ry2 == _ey2:    # same Y → horizontal edge
                    _ovl = max(0, min(_rx2, _ex2) - max(_rx1, _ex1))
                else:
                    _ovl = 0
                if _ovl > _best_ovl:
                    _best_ovl = _ovl
                    owner_info = _reg_info

        aiid_map = {}
        if owner_info:
            owner_tag, edge_idx = owner_info
            lvl_iter = levels_data[:-1] if len(levels_data) > 1 else levels_data
            if _el_types.get(owner_tag) == "fire_lift_car" and owner_tag in _el_bounds:
                # Cardinal naming: determine which face of the fire lift shaft
                # the shared wall lies on (S=south/y1, N=north/y2, W=west/x1, E=east/x2).
                fp = _el_bounds[owner_tag]  # (x1, y1, x2, y2)
                sw_y = (shared_wall[0][1] + shared_wall[1][1]) / 2.0
                sw_x = (shared_wall[0][0] + shared_wall[1][0]) / 2.0
                if abs(sw_y - fp[1]) < 50:
                    face = "S"
                elif abs(sw_y - fp[3]) < 50:
                    face = "N"
                elif abs(sw_x - fp[0]) < 50:
                    face = "W"
                else:
                    face = "E"
                for l_idx, lvl in enumerate(lvl_iter):
                    aiid_map[lvl['id']] = "AI_{}_{}_L{}".format(owner_tag, face, l_idx + 1)
            else:
                for l_idx, lvl in enumerate(lvl_iter):
                    aiid_map[lvl['id']] = "AI_{}_E{}_L{}".format(owner_tag, edge_idx, l_idx + 1)

        ds = _door_spec_from_relation(el_id, rel, all_lvl_ids, first_lvl_id,
                                      wall_ai_id_map=aiid_map if aiid_map else None)
        if ds:
            door_specs.append(ds)

    return {
        "walls":          walls,
        "floors":         floors,
        "voids":          voids,
        "door_specs":     door_specs,
        "sub_boundaries": sub_boundaries,
        "stair_centers":  stair_center_tuples,
    }


# ---------------------------------------------------------------------------
# Geometry rotation helper (generative solver path)
# ---------------------------------------------------------------------------

def _rotate_geometry(result, cx, cy, angle_deg):
    """Rotate all 2-D coordinate data in *result* by *angle_deg* degrees
    (counter-clockwise) around the point (cx, cy).

    Affected keys: walls (start/end), floors (points), voids (AABB tuples),
    door_specs (position_mm, wall_line_mm), core_bounds (AABB lists).

    For voids a "void_polygons" key is also written — a list of rotated
    rectangle corner lists (mm).  revit_workers.py uses this for
    precise floor-slab void cutting when rotation is active.

    Mutates result in-place and returns it.
    """
    import math as _rot_math
    cos_a = _rot_math.cos(_rot_math.radians(angle_deg))
    sin_a = _rot_math.sin(_rot_math.radians(angle_deg))

    def _rp(pt):
        dx, dy = pt[0] - cx, pt[1] - cy
        return [cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a]

    # Walls — start/end are [x, y, z]; preserve z
    for w in result.get("walls", []):
        if "start" in w:
            rx, ry = _rp(w["start"])
            w["start"] = [rx, ry, w["start"][2]]
        if "end" in w:
            rx, ry = _rp(w["end"])
            w["end"] = [rx, ry, w["end"][2]]

    # Floors — "points" is [[x, y], ...]
    for f in result.get("floors", []):
        if "points" in f:
            f["points"] = [_rp(p) for p in f["points"]]

    # Voids — (x1, y1, x2, y2) tuples; after rotation store both the AABB
    # (for backward-compatible spatial checks) and the rotated polygon corners
    # (for precise floor-slab void cutting in revit_workers._process_floors).
    new_voids = []
    void_polys = []
    for v in result.get("voids", []):
        x1, y1, x2, y2 = v[0], v[1], v[2], v[3]
        corners = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
        rot_c = [_rp(c) for c in corners]
        void_polys.append(rot_c)
        rxs = [c[0] for c in rot_c]; rys = [c[1] for c in rot_c]
        new_voids.append((min(rxs), min(rys), max(rxs), max(rys)))
    result["voids"] = new_voids
    result["void_polygons"] = void_polys

    # Door specs — position_mm [x, y] and wall_line_mm [[x,y],[x,y]]
    for ds in result.get("door_specs", []):
        if ds.get("position_mm"):
            ds["position_mm"] = _rp(ds["position_mm"])
        if ds.get("wall_line_mm"):
            ds["wall_line_mm"] = [_rp(pt) for pt in ds["wall_line_mm"]]

    # Core bounds — list of [x1, y1, x2, y2]; rotate corners, recompute AABB
    new_cb = []
    for cb in result.get("core_bounds", []):
        corners = [[cb[0], cb[1]], [cb[2], cb[1]], [cb[2], cb[3]], [cb[0], cb[3]]]
        rot_c = [_rp(c) for c in corners]
        rxs = [c[0] for c in rot_c]; rys = [c[1] for c in rot_c]
        new_cb.append([min(rxs), min(rys), max(rxs), max(rys)])
    result["core_bounds"] = new_cb

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def calculate_fire_safety_requirements(floor_dims_mm, core_center_mm, lift_core_bounds_mm,
                                       typical_floor_height_mm, _preset_fs, num_lifts,
                                       lobby_width=3000, compliance_overrides=None,
                                       footprint_pts=None, footprint_holes=None,
                                       orientation="AUTO", num_banks=1):
    """Determine positions and types for fire safety cores.

    Returns list of dicts: {"pos": (x, y), "type": "FIRE_LIFT"|"SMOKE_STOP"}

    NS layout  — pos is the entry point at the lift-core Y boundary
                 (y = lift_ymin or lift_ymax, x = lift_core_cx).
    EW layout  — pos is the entry point at the lift-core X boundary
                 (x = lift_xmin or lift_xmax, y = lift_core_cy).

    compliance_overrides: dict of RAG-derived values that override the static
        module constants (e.g. {"max_travel_distance_mm": 60000, "fire_lift_car_size_mm": 2500}).
        Falls back to module constants for any key not present.
    """
    co = compliance_overrides or {}
    max_travel_dist = (co.get("max_travel_distance_sprinklered_mm")
                       or co.get("max_travel_distance_mm", _MAX_TRAVEL))

    _fsl_log("[FireReqs] ENTER calculate_fire_safety_requirements: "
             "floor_dims={} core_center=({:.0f},{:.0f}) lcb=({:.0f},{:.0f},{:.0f},{:.0f}) "
             "typ_h={:.0f} num_lifts={} lobby_w={:.0f} orientation={} num_banks={} "
             "max_travel_dist={:.0f} overrides={}".format(
        floor_dims_mm,
        float(core_center_mm[0]) if core_center_mm else 0,
        float(core_center_mm[1]) if core_center_mm else 0,
        float(lift_core_bounds_mm[0]) if lift_core_bounds_mm else 0,
        float(lift_core_bounds_mm[1]) if lift_core_bounds_mm else 0,
        float(lift_core_bounds_mm[2]) if lift_core_bounds_mm else 0,
        float(lift_core_bounds_mm[3]) if lift_core_bounds_mm else 0,
        float(typical_floor_height_mm or 0),
        num_lifts, float(lobby_width), orientation, num_banks,
        float(max_travel_dist),
        {k: v for k, v in co.items() if k in (
            "max_travel_distance_mm", "max_travel_distance_sprinklered_mm",
            "fire_lobby_min_length_mm", "fire_lobby_min_width_mm")}
    ))

    # Fire lift shaft = passenger lift shaft outer size (2500mm car + 2×wall).
    _PAX_CAR_D  = 2500
    fl_shaft_d  = _PAX_CAR_D + 2 * _WALL_THICKNESS   # 3200mm, matches pax shaft depth
    final_sets = []

    # Compute passenger lift core bounds if not provided
    if not lift_core_bounds_mm and num_lifts:
        layout = lift_logic.get_total_core_layout(num_lifts, lobby_width=lobby_width)
        lift_core_bounds_mm = (
            core_center_mm[0] - layout["total_w"] / 2.0,
            core_center_mm[1] - layout["total_d"] / 2.0,
            core_center_mm[0] + layout["total_w"] / 2.0,
            core_center_mm[1] + layout["total_d"] / 2.0
        )

    if not lift_core_bounds_mm:
        # No lifts - place at center
        final_sets.append({"pos": (core_center_mm[0], core_center_mm[1]), "type": "FIRE_LIFT"})
        return final_sets

    l_xmin, l_ymin, l_xmax, l_ymax = lift_core_bounds_mm
    lift_core_cx = (l_xmin + l_xmax) / 2.0
    lift_core_cy = (l_ymin + l_ymax) / 2.0

    # Determine whether EW or NS placement gives a more compact core
    _typ_h = typical_floor_height_mm or 4000
    sw_nat = staircase_logic.get_shaft_dimensions(_typ_h, None)[0]
    sd_nat = staircase_logic.get_shaft_dimensions(_typ_h, None)[1]
    use_ew = _should_use_ew_orientation(lift_core_bounds_mm, sw_nat, sd_nat,
                                        orientation=orientation, floor_dims=floor_dims_mm)
    _fsl_log("[FireReqs] orientation decision: use_ew={} (requested={}) "
             "sw_nat={:.0f} sd_nat={:.0f} lcb_w={:.0f} lcb_d={:.0f}".format(
        use_ew, orientation, float(sw_nat), float(sd_nat),
        float(l_xmax - l_xmin), float(l_ymax - l_ymin)))

    _lobby_d_est  = max(2000, sd_nat - fl_shaft_d)
    # True distance from lift-core face to staircase centre:
    # fire_lift shaft depth + lobby depth + half staircase depth.
    _stair_centre_offset = fl_shaft_d + _lobby_d_est + sd_nat / 2.0

    # Keep footprint polygon in absolute building coordinates.
    # Both stair positions (from FIRE_LIFT sets) and travel-distance test points
    # must share the same coordinate frame.  Translating the footprint to match
    # the core breaks distance checks when the core is offset from the building
    # centroid (e.g. core near north edge to avoid a void).
    _fp_use = footprint_pts
    _fp_holes_use = footprint_holes

    def _ew_stair_pos(sets_list):
        """Estimate stair centres for EW fire-set entry positions (exit E or W)."""
        pos = []
        for s in sets_list:
            ex, ey = s["pos"]
            if ex >= l_xmax - 10:
                pos.append((ex + _stair_centre_offset, ey))
            else:
                pos.append((ex - _stair_centre_offset, ey))
        return pos

    def _ns_bank_ew_stair_pos(_sets_list):
        """NS-bank fire sets sit at Y-ends of the lift bank, but OR-Tools places
        clusters East and West (allowed_sides=['E','W']).  Project stair centres
        east and west of the bank rather than north/south from the entry points."""
        return [
            (l_xmax + _stair_centre_offset, lift_core_cy),
            (l_xmin - _stair_centre_offset, lift_core_cy),
        ]

    if use_ew:
        # EW layout: fire lifts aligned with passenger lift rows so the passenger
        # lift lobby ends remain clear (Rule 1) and fire lifts are in-row (Rule 4).
        layout_ew = lift_logic.get_total_core_layout(num_lifts, lobby_width=lobby_width) if num_lifts else None
        if layout_ew and layout_ew["lifts_per_block"] >= 4:
            # 2-row block has east (north row) and west (south row) positions.
            row_cy_s = lift_core_cy - lobby_width / 2.0 - fl_shaft_d / 2.0  # south row
            row_cy_n = lift_core_cy + lobby_width / 2.0 + fl_shaft_d / 2.0  # north row
            east_set = {"pos": (l_xmax, row_cy_n), "type": "FIRE_LIFT"}
            west_set = {"pos": (l_xmin, row_cy_s), "type": "FIRE_LIFT"}
            # ── Minimum viable EW set check ─────────────────────────────────
            # Try just ONE set first (centred on the lift core Y axis for maximum
            # reach). If travel distance is already satisfied, use 1 set → 1
            # staircase per bank instead of 2.  Only escalate to 2 if needed.
            _single_ew = [{"pos": (l_xmax, lift_core_cy), "type": "FIRE_LIFT"},
                          {"pos": (l_xmin, lift_core_cy), "type": "FIRE_LIFT"}]
            # Only reduce to 1 set per bank when there are ≥2 banks — the global
            # 2-exit rule is then satisfied by 1 staircase per bank × num_banks.
            if num_banks >= 2:
                for _candidate_single in _single_ew:
                    _sp1 = _ew_stair_pos([_candidate_single])
                    if staircase_logic._check_travel_distance(
                            _sp1, floor_dims_mm, max_travel_dist,
                            num_required=1,
                            footprint_pts=_fp_use,
                            footprint_holes=_fp_holes_use):
                        final_sets.append(_candidate_single)
                        _fsl_log("[FireReqs] EW single-set satisfies travel: pos={}".format(
                            _candidate_single["pos"]))
                        return final_sets  # 1 EW set satisfies travel distance
            # 1 set insufficient — use both (east + west at row centres)
            final_sets.append(east_set)
            final_sets.append(west_set)
        else:
            # Single row: both fire lifts centred on the single row (= lift_core_cy)
            final_sets.append({"pos": (l_xmax, lift_core_cy), "type": "FIRE_LIFT"})
            final_sets.append({"pos": (l_xmin, lift_core_cy), "type": "FIRE_LIFT"})
    else:
        # NS (default): one safety set at each Y-end of the passenger lift core
        final_sets.append({"pos": (lift_core_cx, l_ymin), "type": "FIRE_LIFT"})
        final_sets.append({"pos": (lift_core_cx, l_ymax), "type": "FIRE_LIFT"})

    stair_pos = _ew_stair_pos(final_sets) if use_ew else _ns_bank_ew_stair_pos(final_sets)

    _fsl_log("[FireReqs] core sets={} FIRE_LIFT stair_pos={}".format(
        [(s["pos"]) for s in final_sets],
        [(round(p[0]), round(p[1])) for p in stair_pos]))

    if staircase_logic._check_travel_distance(stair_pos, floor_dims_mm, max_travel_dist,
                                              footprint_pts=_fp_use):
        _fsl_log("[FireReqs] RETURN {} sets: {}".format(
            len(final_sets), [(s["type"], s["pos"]) for s in final_sets]))
        return final_sets

    # ── Perimeter SMOKE_STOP staircases (60m rule not yet satisfied) ──────────
    # For large floor plates (e.g. 80×100m) the central core alone cannot cover
    # all corners within 60m.  Add N-S oriented smoke-stop staircase/lobby sets
    # at the building perimeter until the rule is satisfied.
    # The "pos" stores (x, y_edge) where y_edge is the building boundary line.
    # generate_fire_safety_manifest detects is_perimeter=True and uses a
    # dedicated layout rather than the core-relative NS/EW branches.
    #
    # SMALLEST-FOOTPRINT-FIRST STRATEGY (SCDF-aware):
    # Try placing perimeter stairs aligned to the smallest floor plate first.
    # If that satisfies 60m for ALL floor dimensions, use it — stairs will sit
    # inside the core of the building on most floors, minimising exposure.
    # If not compliant for any floor, escalate to the next-smallest footprint,
    # repeating until all floors comply.  Only fall back to max footprint as a
    # last resort.  This mirrors SCDF Code Part IV cl. 5.3.3 intent.
    if floor_dims_mm:
        # Build sorted lists of unique half-dimensions for N/S and E/W edge positions.
        unique_half_l = sorted(set(d[1] / 2.0 for d in floor_dims_mm))  # Y half-lengths
        unique_half_w = sorted(set(d[0] / 2.0 for d in floor_dims_mm))  # X half-widths
    else:
        unique_half_l = [25000.0]  # 50 m default half-length
        unique_half_w = [25000.0]

    # Approximate shaft depth to compute staircase centre from edge position.
    _sd_approx = staircase_logic.get_shaft_dimensions(_typ_h, None)[1]

    # For non-rectangular polygons (L, U, H, etc.), pre-compute an 8×8 grid of interior
    # candidate positions so perimeter stairs always land inside the actual floor plate,
    # not at bounding-box edge midpoints that may be outside the footprint.
    # Simple rectangles (with or without interior holes/courtyards) always use the
    # rectangular edge-candidate path so stairs target outer building edges, not the void.
    def _is_simple_rectangle(pts):
        """True if pts describes an axis-aligned rectangle (4 unique corners, ±closed)."""
        unique = list({(round(p[0]), round(p[1])) for p in pts})
        if len(unique) != 4:
            return False
        xs = sorted(set(p[0] for p in unique))
        ys = sorted(set(p[1] for p in unique))
        return len(xs) == 2 and len(ys) == 2

    _poly_candidates = None
    if footprint_pts and len(footprint_pts) >= 3 and not _is_simple_rectangle(footprint_pts):
        _fp_xs = [p[0] for p in footprint_pts]
        _fp_ys = [p[1] for p in footprint_pts]
        _fp_xmin, _fp_xmax = min(_fp_xs), max(_fp_xs)
        _fp_ymin, _fp_ymax = min(_fp_ys), max(_fp_ys)
        _gN = 8
        _gdx = (_fp_xmax - _fp_xmin) / float(_gN)
        _gdy = (_fp_ymax - _fp_ymin) / float(_gN)
        _holes = footprint_holes or []
        _interior = []
        for _ix in range(_gN):
            for _iy in range(_gN):
                _gx = _fp_xmin + (_ix + 0.5) * _gdx
                _gy = _fp_ymin + (_iy + 0.5) * _gdy
                if not staircase_logic._point_in_polygon(_gx, _gy, footprint_pts):
                    continue
                if any(staircase_logic._point_in_polygon(_gx, _gy, h) for h in _holes):
                    continue
                _interior.append((_gx, _gy))
        if _interior:
            _poly_candidates = _interior

    # Greedy: always pick the candidate furthest from ALL existing staircases.
    all_stair_pos = list(stair_pos)

    # Building centre for perimeter candidate placement.
    # Use the footprint bbox centre when a footprint polygon is available;
    # fall back to floor_dims bbox centre.  Do NOT use core_center_mm — when
    # the core is offset (e.g. placed near the north edge to avoid a void) the
    # perimeter candidates would land outside the building or inside the void.
    if footprint_pts and len(footprint_pts) >= 3:
        _fp_xs_c = [p[0] for p in footprint_pts]
        _fp_ys_c = [p[1] for p in footprint_pts]
        _cx0 = (min(_fp_xs_c) + max(_fp_xs_c)) / 2.0
        _cy0 = (min(_fp_ys_c) + max(_fp_ys_c)) / 2.0
    elif floor_dims_mm:
        # Derive from the largest floor plate bbox; half-widths are already available.
        _cx0 = core_center_mm[0]  # X centre matches core (no void in X typically)
        _cy0 = core_center_mm[1]
    else:
        _cx0 = core_center_mm[0]
        _cy0 = core_center_mm[1]

    # Build a unified tier list covering both N/S (from half-length) and E/W (from half-width)
    # edge positions.  For each tier we build candidates for all four building edges so the
    # greedy loop can place stairs at corners/east/west when N/S alone is insufficient.
    _unique_dims = sorted(set(unique_half_l + unique_half_w))

    # Try each footprint dimension tier from smallest to largest until 60m rule satisfied.
    for try_hl in _unique_dims:
        if staircase_logic._check_travel_distance(all_stair_pos, floor_dims_mm, max_travel_dist,
                                                   footprint_pts=_fp_use,
                                                   footprint_holes=_fp_holes_use):
            break  # already compliant from central stairs alone

        # Build candidates: polygon-interior grid points (irregular shapes) or
        # all four building-edge midpoints (rectangular shapes).
        # Each candidate is (stair_cx, stair_cy, pos_x, pos_y, rotation_deg) where
        # (stair_cx, stair_cy) is the staircase shaft centre used for distance checks
        # and (pos_x, pos_y) + rotation_deg is what gets stored in the safety set.
        if _poly_candidates is not None:
            # Polygon path: candidates are interior grid points; pos derived at placement time.
            perim_candidates = [(cx, cy, None, None, None) for cx, cy in _poly_candidates]
        else:
            # Rectangular path: build mid-edge candidates for all four building faces.
            # The N/S edge midpoints use the current tier Y half-length;
            # the E/W edge midpoints use the corresponding X half-width for that tier.
            _ns_hl = try_hl if try_hl in unique_half_l else max(unique_half_l)
            _ew_hw = try_hl if try_hl in unique_half_w else max(unique_half_w)
            # N/S: stair centre is inset from the edge by half the shaft depth; rotation=0.
            _sc_ns = _sd_approx / 2.0
            # E/W: stair centre is inset from the east/west edge; rotation=90 triggers EW path.
            _sc_ew = _sd_approx / 2.0
            _raw_cands = [
                # (stair_cx, stair_cy, pos_x, pos_y, rot_deg)
                # pos_x/pos_y are ABSOLUTE building coordinates (not origin-relative).
                (_cx0,  _cy0 - _ns_hl + _sc_ns,  _cx0,  _cy0 - _ns_hl,  0.0),  # south NS
                (_cx0,  _cy0 + _ns_hl - _sc_ns,  _cx0,  _cy0 + _ns_hl,  0.0),  # north NS
                (_cx0 - _ew_hw + _sc_ew,  _cy0,  _cx0 - _ew_hw,  _cy0,  90.0),  # west EW
                (_cx0 + _ew_hw - _sc_ew,  _cy0,  _cx0 + _ew_hw,  _cy0,  90.0),  # east EW
            ]
            # Skip candidates already placed in a previous tier to avoid duplicates.
            _placed = set((round(sx), round(sy)) for sx, sy in all_stair_pos)
            perim_candidates = [c for c in _raw_cands
                                 if (round(c[0]), round(c[1])) not in _placed]

        # Add the best candidate from this footprint tier.
        while perim_candidates:
            if staircase_logic._check_travel_distance(all_stair_pos, floor_dims_mm, max_travel_dist,
                                                       footprint_pts=_fp_use,
                                                       footprint_holes=_fp_holes_use):
                break
            best = max(perim_candidates, key=lambda c: min(
                math.sqrt((c[0] - sx) ** 2 + (c[1] - sy) ** 2)
                for sx, sy in all_stair_pos
            ))
            perim_candidates.remove(best)
            _bcx, _bcy, _bpx, _bpy, _brot = best
            if _bpx is None:
                # Polygon interior path: derive pos/rotation from nearest polygon edge.
                _bpx = _bcx
                _bpy = _bcy - _sd_approx / 2.0 if _bcy < _cy0 else _bcy + _sd_approx / 2.0
                _brot = 0.0
                if _fp_use and len(_fp_use) >= 3:
                    _brot = _nearest_polygon_edge_angle_deg(_bcx, _bcy, _fp_use)
            final_sets.append({"pos": (_bpx, _bpy), "type": "SMOKE_STOP",
                                "is_perimeter": True,
                                "ref_half_l": try_hl,
                                "rotation_deg": _brot})
            all_stair_pos.append((_bcx, _bcy))

    # Tag each perimeter staircase with the highest 0-based floor index where it
    # is still required.  Above that index the central core stairs alone satisfy
    # the travel-distance rule, so the extra shaft can terminate early.
    if floor_dims_mm:
        for _fs in final_sets:
            if _fs.get("is_perimeter"):
                _last = 0
                for _k in range(len(floor_dims_mm) - 1, -1, -1):
                    if not staircase_logic._check_travel_distance(
                            stair_pos, [floor_dims_mm[_k]], max_travel_dist,
                            footprint_pts=_fp_use,
                            footprint_holes=_fp_holes_use):
                        _last = _k
                        break
                _fs["last_floor_idx"] = _last

    _fsl_log("[FireReqs] RETURN {} sets (incl perimeter): {}".format(
        len(final_sets),
        [(s["type"], s["pos"], "perim={}".format(s.get("is_perimeter", False))) for s in final_sets]))
    return final_sets


def _mirror_layout(layout, anchor_bounds, mirror_side, log_fn=None):
    """
    Mirror a solved cluster layout onto the opposite side of the passenger lift
    anchor.  Used to place a symmetric second cluster without re-running OR-Tools.

    mirror_side: the target attach_side for the mirrored result ("W", "E", "N", "S").

    Reflection axis:
      E↔W mirror  →  reflect X around anchor centre X  (x' = 2*acx - x)
      N↔S mirror  →  reflect Y around anchor centre Y  (y' = 2*acy - y)
    For each box (x1,y1,x2,y2) the reflection swaps the two coordinates on the
    reflected axis so x1 < x2 always holds in the result.
    """
    try:
        ax1, ay1, ax2, ay2 = anchor_bounds
        acx = (ax1 + ax2) / 2.0
        acy = (ay1 + ay2) / 2.0
        src_side = layout["attach_side"]
        # Snap helper: round to nearest 50mm grid to prevent sub-grid drift from
        # float arithmetic when anchor centre is not a multiple of 25mm.
        _MGRID = 50.0
        def _snap(v):
            return round(v / _MGRID) * _MGRID

        # Determine reflection axis.
        # NE order: staircase is N/S of anchor, FL is on E face.
        # The symmetric second cluster reflects around anchor centre Y only —
        # Set 0 N → Set 1 S; both FLs stay on the E face at different Y positions.
        # (Earlier diagonal reflection was wrong — it moved FL to W face causing overlap.)
        if src_side in ("E", "W"):
            # Reflect X around anchor centre X
            def _reflect_box(b):
                nx1 = _snap(2 * acx - b[2])
                nx2 = _snap(2 * acx - b[0])
                return (nx1, b[1], nx2, b[3])
        else:
            # Reflect Y around anchor centre Y
            def _reflect_box(b):
                ny1 = _snap(2 * acy - b[3])
                ny2 = _snap(2 * acy - b[1])
                return (b[0], ny1, b[2], ny2)

        mirrored = {
            "fire_lift":   _reflect_box(layout["fire_lift"]),
            "lobby":       _reflect_box(layout["lobby"]),
            "staircase":   _reflect_box(layout["staircase"]),
            "attach_side": mirror_side,
            "chain_order": layout["chain_order"],
            "stair_rot":   layout["stair_rot"],
            "score":       layout["score"],
        }
        if log_fn:
            try:
                log_fn("[LayoutEngine] _mirror_layout: {} → {} OK".format(src_side, mirror_side))
            except Exception:
                pass
        return mirrored
    except Exception as _me:
        if log_fn:
            try:
                log_fn("[LayoutEngine] _mirror_layout failed: {}".format(_me))
            except Exception:
                pass
        return None


def _snap_bank_to_valid(anchor_bounds, cluster_d, attach_side, footprint_pts, snap_tolerance_mm=2000):
    """Shift the lift bank anchor by the minimum amount so the fire cluster fits inside the footprint.

    Only activates when the bank is outside the valid range by ≤ snap_tolerance_mm.
    Returns the adjusted (x1,y1,x2,y2) anchor bounds, or the original if no snap needed/possible.
    """
    if not footprint_pts or len(footprint_pts) < 3:
        return anchor_bounds
    ax1, ay1, ax2, ay2 = anchor_bounds
    acx = (ax1 + ax2) / 2.0
    acy = (ay1 + ay2) / 2.0
    bank_half_d = (ay2 - ay1) / 2.0
    bank_half_w = (ax2 - ax1) / 2.0
    fp_xs = [p[0] for p in footprint_pts]
    fp_ys = [p[1] for p in footprint_pts]
    fp_xmin, fp_xmax = min(fp_xs), max(fp_xs)
    fp_ymin, fp_ymax = min(fp_ys), max(fp_ys)

    dx, dy = 0.0, 0.0
    if attach_side == "N":
        needed_cy = fp_ymax - bank_half_d - cluster_d
        overshoot = acy - needed_cy
        if 0 < overshoot <= snap_tolerance_mm:
            dy = -overshoot
    elif attach_side == "S":
        needed_cy = fp_ymin + bank_half_d + cluster_d
        overshoot = needed_cy - acy
        if 0 < overshoot <= snap_tolerance_mm:
            dy = overshoot
    elif attach_side == "E":
        needed_cx = fp_xmax - bank_half_w - cluster_d
        overshoot = acx - needed_cx
        if 0 < overshoot <= snap_tolerance_mm:
            dx = -overshoot
    elif attach_side == "W":
        needed_cx = fp_xmin + bank_half_w + cluster_d
        overshoot = needed_cx - acx
        if 0 < overshoot <= snap_tolerance_mm:
            dx = overshoot

    if dx == 0.0 and dy == 0.0:
        return anchor_bounds
    return (ax1 + dx, ay1 + dy, ax2 + dx, ay2 + dy)


def compute_core_envelope_from_ortools(safety_sets, lift_core_bounds_mm, stair_spec,
                                       typical_floor_height_mm, num_lifts=None,  # noqa: unused-kept for API symmetry
                                       lobby_width=3000, footprint_pts=None,
                                       footprint_holes=None, all_floor_dims=None,
                                       compliance_overrides=None, log_fn=None):
    """Run OR-Tools to determine whether the fire safety cluster(s) fit and compute
    their combined envelope.  Does NOT build any Revit geometry — pure geometry
    computation only.  Safe to call during pre-analysis (no Revit API).

    Returns a dict with:
        solved          bool  — True if all FIRE_LIFT sets were placed
        sets            list  — per-set result: attach_side, cluster_w_mm, cluster_d_mm,
                                fire_lift box, lobby box, staircase box, stair_rot
        min_arm_w_needed_mm  int  — minimum arm width to fit one cluster
        min_arm_d_needed_mm  int  — minimum arm depth to fit one cluster
        total_core_w_mm      int  — sum of all cluster widths (parallel arrangement)
        total_core_d_mm      int  — deepest single cluster
        ortools_infeasible   bool — True if any set could not be placed
    """
    from .core_layout_engine import find_layout_for_set, _USE_LAYOUT_ENGINE, _box_inside_footprint

    def _log(msg):
        if log_fn:
            try:
                log_fn(msg)
            except Exception:
                pass

    co = compliance_overrides or {}
    lobby_width = max(lobby_width, 3000)
    _PAX_CAR_D = 2500
    fl_shaft_d = _PAX_CAR_D + 2 * _WALL_THICKNESS
    t = _WALL_THICKNESS
    fire_lb_area = co.get("fire_lobby_min_area_mm2",
                          _FSC.get("fire_lift_lobby", {}).get("min_area_mm2", 6000000))

    _fl_lb_min_w = co.get("fire_lobby_min_width_mm", 2000)
    _fl_lb_min_l = co.get("fire_lobby_min_length_mm", 3200)
    lb_net_y = max(fl_shaft_d - 2 * t, _fl_lb_min_w)
    ew_lb_dx = max(_fl_lb_min_w, _fl_lb_min_l, int(math.ceil(fire_lb_area / lb_net_y)))
    # Cap lobby width to bank width so oversized RAG area values don't exceed the anchor.
    if lift_core_bounds_mm:
        _bw = lift_core_bounds_mm[2] - lift_core_bounds_mm[0]
        if _bw > 0 and ew_lb_dx > _bw:
            ew_lb_dx = int(_bw)

    sw_nat, sd_nat = staircase_logic.get_shaft_dimensions(typical_floor_height_mm, stair_spec)

    # Build footprint polygon (same logic as generate_fire_safety_manifest).
    # For the pre-check the anchor is a dummy centred at origin — we do NOT check
    # _box_inside_footprint here because the dummy anchor is often in the void of
    # L/U/H shapes.  The footprint is only used as the boundary for OR-Tools.
    _engine_footprint = footprint_pts
    if _engine_footprint and _engine_footprint[0] and isinstance(_engine_footprint[0][0], (list, tuple)):
        _engine_footprint = _engine_footprint[0]
    _smallest_plate = None
    if _engine_footprint is None and all_floor_dims:
        _smallest_plate = min(all_floor_dims, key=lambda d: d[0] * d[1])
        _fp_w, _fp_l = _smallest_plate
        _engine_footprint = [
            [-_fp_w / 2.0, -_fp_l / 2.0],
            [ _fp_w / 2.0, -_fp_l / 2.0],
            [ _fp_w / 2.0,  _fp_l / 2.0],
            [-_fp_w / 2.0,  _fp_l / 2.0],
        ]

    if not (_USE_LAYOUT_ENGINE and lift_core_bounds_mm):
        # Cannot pre-compute without layout engine or anchor bounds
        _min_w = fl_shaft_d + ew_lb_dx + sw_nat
        _min_d = max(fl_shaft_d, lb_net_y + 2 * t, sd_nat)
        return {
            "solved": None,
            "sets": [],
            "min_arm_w_needed_mm": round(_min_w),
            "min_arm_d_needed_mm": round(_min_d),
            "total_core_w_mm": round(_min_w),
            "total_core_d_mm": round(_min_d),
            "ortools_infeasible": None,
        }

    _anchor = lift_core_bounds_mm
    lift_core_cy = (_anchor[1] + _anchor[3]) / 2.0
    _pax_lobby_bounds = (
        _anchor[0],
        lift_core_cy - lobby_width / 2.0,
        _anchor[2],
        lift_core_cy + lobby_width / 2.0,
    )

    _placed_boxes = []
    _preferred_order = None
    _preferred_side  = None
    _opposite_side   = {"N": "S", "S": "N", "E": "W", "W": "E"}
    _set_results = []
    _any_infeasible = False

    for _sidx, _sset in enumerate(safety_sets):
        if _sset.get("is_perimeter") or _sset.get("type") != "FIRE_LIFT":
            continue
        _layout = find_layout_for_set(
            anchor_bounds   = _anchor,
            fire_lift_size  = (fl_shaft_d, fl_shaft_d),
            lobby_size      = (ew_lb_dx,   lb_net_y + 2 * t),
            staircase_size  = (sw_nat,     sd_nat),
            already_placed  = list(_placed_boxes),
            footprint_pts   = _engine_footprint,
            footprint_holes = footprint_holes,
            log_fn          = _log,
            preferred_order = _preferred_order,
            preferred_side  = _preferred_side,
            pax_lobby_bounds= _pax_lobby_bounds,
        )
        if _layout is None:
            _any_infeasible = True
            _set_results.append({"solved": False, "set_index": _sidx})
            continue

        # Compute combined bounding box of the three placed modules
        _all_boxes = [_layout["fire_lift"], _layout["lobby"], _layout["staircase"]]
        _cx1 = min(b[0] for b in _all_boxes)
        _cy1 = min(b[1] for b in _all_boxes)
        _cx2 = max(b[2] for b in _all_boxes)
        _cy2 = max(b[3] for b in _all_boxes)
        _cluster_w = round(_cx2 - _cx1)
        _cluster_d = round(_cy2 - _cy1)

        _set_results.append({
            "solved":       True,
            "set_index":    _sidx,
            "attach_side":  _layout["attach_side"],
            "cluster_w_mm": _cluster_w,
            "cluster_d_mm": _cluster_d,
            "stair_rot":    _layout.get("stair_rot", 0),
            "fire_lift_box":  _layout["fire_lift"],
            "lobby_box":      _layout["lobby"],
            "staircase_box":  _layout["staircase"],
        })

        _placed_boxes.extend(_all_boxes)
        if _preferred_order is None:
            _preferred_order = _layout["chain_order"]
            _preferred_side  = _opposite_side.get(_layout["attach_side"])

    # Aggregate envelope
    _solved_sets = [s for s in _set_results if s.get("solved")]
    _min_w = fl_shaft_d + ew_lb_dx + sw_nat
    _min_d = max(fl_shaft_d, lb_net_y + 2 * t, sd_nat)

    if _solved_sets:
        _total_w = sum(s["cluster_w_mm"] for s in _solved_sets)
        _total_d = max(s["cluster_d_mm"] for s in _solved_sets)
        _arm_w   = max(s["cluster_w_mm"] for s in _solved_sets)
        _arm_d   = max(s["cluster_d_mm"] for s in _solved_sets)
    else:
        _total_w = round(_min_w)
        _total_d = round(_min_d)
        _arm_w   = round(_min_w)
        _arm_d   = round(_min_d)

    return {
        "solved":                not _any_infeasible,
        "sets":                  _set_results,
        "min_arm_w_needed_mm":   _arm_w,
        "min_arm_d_needed_mm":   _arm_d,
        "total_core_w_mm":       _total_w,
        "total_core_d_mm":       _total_d,
        "ortools_infeasible":    _any_infeasible,
        "fire_lift_shaft_d_mm":  round(fl_shaft_d),
        "lobby_w_mm":            round(ew_lb_dx),
        "lobby_d_mm":            round(lb_net_y + 2 * t),
        "staircase_w_mm":        round(sw_nat),
        "staircase_d_mm":        round(sd_nat),
        "smallest_plate_mm":     list(_smallest_plate) if _smallest_plate else None,
    }


def generate_fire_safety_manifest(safety_sets, levels_data, stair_spec,
                                  typical_floor_height_mm, _preset_fs,
                                  lift_core_bounds_mm=None, num_lifts=None,
                                  lobby_width=3000, all_floor_dims=None,
                                  compliance_overrides=None, footprint_pts=None,
                                  footprint_holes=None,
                                  manifest_lifts=None,
                                  center_pos=None,
                                  **kwargs):
    """Generate manifest for fire lifts, lobbies and staircases.

    Supports two layout orientations, chosen automatically in
    calculate_fire_safety_requirements() based on which gives the smaller
    aspect ratio for the combined central core:

    NS layout (default, fire lift stacked above/below passenger lift in Y):
        [S-Stair] → [S-Lobby] → [S-FireLift] → [PassengerLifts] → [N-FireLift] → [N-Lobby] → [N-Stair]

    EW layout (fire lift adjacent to passenger lift in X — more compact for
    narrower lift banks):
        [Stair_W] [Lobby_W] [FireLift_W] [PassengerLifts] [FireLift_E] [Lobby_E] [Stair_E]
        Staircase runs N-S (normal), centred on the lift-core Y centre.

    compliance_overrides: dict of RAG-derived values that override static module constants.
    """
    from .utils import get_log_path as _glp
    import time as _time
    def _fslog(msg):
        try:
            with open(_glp(), "a") as _f:
                _f.write("[{}] {}\n".format(_time.strftime("%H:%M:%S"), msg))
        except Exception:
            pass

    co = compliance_overrides or {}
    lobby_width = max(lobby_width, 3000)   # minimum 3 m passenger lift lobby
    # Fire lift shaft = passenger lift shaft outer size (2500mm car + 2×wall).
    _PAX_CAR_D  = 2500
    fl_shaft_d  = _PAX_CAR_D + 2 * _WALL_THICKNESS   # 3200mm, matches pax shaft depth
    overrun_height  = co.get("overrun_height_mm",       _OVERRUN_HEIGHT)
    fire_lb_area    = co.get("fire_lobby_min_area_mm2", _FSC.get("fire_lift_lobby",  {}).get("min_area_mm2",   6000000))
    smoke_clear_d   = co.get("smoke_lobby_min_depth_mm",_SMOKE_CLEAR_D)

    walls = []
    floors = []
    voids = []
    core_bounds = []
    stair_centers = []
    door_specs = []

    # Level lists for door spec generation (exclude final roof/overrun level)
    _all_lvl_ids = [lvl['id'] for lvl in levels_data[:-1]] if len(levels_data) > 1 else [levels_data[0]['id']] if levels_data else []
    _first_lvl_id = _all_lvl_ids[0] if _all_lvl_ids else None

    sw_nat = staircase_logic.get_shaft_dimensions(typical_floor_height_mm, stair_spec)[0]
    sd_nat = staircase_logic.get_max_shaft_depth(levels_data, stair_spec, typical_floor_height_mm)
    # NS layout also needs total_set_d
    _, total_set_d = staircase_logic.get_safety_set_dimensions(typical_floor_height_mm, stair_spec, True, levels_data=levels_data, compliance_overrides=co)

    l_xmin, l_ymin, l_xmax, l_ymax = lift_core_bounds_mm if lift_core_bounds_mm else (0, 0, 0, 0)
    lift_core_cy = (l_ymin + l_ymax) / 2.0

    # Building centre used to classify perimeter stair orientation (east/west/south/north).
    # Prefer caller-supplied center_pos; fall back to lift core centre.
    _bld_cx = center_pos[0] if center_pos else (l_xmin + l_xmax) / 2.0
    _bld_cy = center_pos[1] if center_pos else (l_ymin + l_ymax) / 2.0

    layout = lift_logic.get_total_core_layout(num_lifts, lobby_width=lobby_width) if num_lifts else None
    row1_cy, row2_cy = _passenger_lift_row_centers((0, 0), layout, lobby_width) if layout else (0, 0)

    stair_overrides = []
    sub_boundaries = []
    stair_global_idx = 0  # incremented for every staircase across all safety sets

    t = _WALL_THICKNESS

    # Pre-compute EW dimensions (Rule 3: fire lift shaft = passenger lift shaft = fl_shaft_d)
    _fl_lb_min_w = co.get("fire_lobby_min_width_mm", 2000)
    _fl_lb_min_l = co.get("fire_lobby_min_length_mm", 3200)
    ew_fl_dx  = fl_shaft_d                                              # EW fire-lift X extent
    lb_net_y  = max(fl_shaft_d - 2 * t, _fl_lb_min_w)                 # lobby internal Y (≥ RAG min width)
    ew_lb_dx  = max(_fl_lb_min_w, _fl_lb_min_l, int(math.ceil(fire_lb_area / lb_net_y)))  # EW lobby X extent (≥ min area, ≥ min length)
    # Cap lobby width to the bank width so it never spans wider than the anchor it serves.
    # An oversized RAG area value (e.g. 20m²) can otherwise push ew_lb_dx > bank_width,
    # making OR-Tools Rule 4 X-axis infeasible because the lobby blocks both corridor ends.
    _bank_w = l_xmax - l_xmin
    if _bank_w > 0 and ew_lb_dx > _bank_w:
        ew_lb_dx = int(_bank_w)

    # Lobby depth for layout engine.
    # Two valid arrangements — lobby depth must satisfy both:
    #   N/S stacking (fl below anchor, lb above fl):  fl_d + lb_d = sd_nat  → lb_d = sd_nat - fl_shaft_d
    #   E-side arrangement (fl east of anchor, lb above fl, st above anchor):
    #       lb_d must equal sw_nat so the top row (st + lb) is perfectly flush.
    # Use sw_nat as lb_d so the E-side 2×2 is flush; it also satisfies the N/S case
    # because sw_nat >= sd_nat - fl_shaft_d for typical floor heights.
    # Must also satisfy minimum compliance depth lb_net_y + 2*t.
    _sd_nat_pre  = staircase_logic.get_max_shaft_depth(levels_data, stair_spec, typical_floor_height_mm)
    _lb_d_flush  = _sd_nat_pre - fl_shaft_d    # depth for fl+lb flush with staircase (N/S stacking)
    _lb_d_min    = lb_net_y + 2 * t            # minimum compliance depth
    lb_d_eng     = max(_lb_d_flush, _lb_d_min, sw_nat)  # sw_nat makes E-side top row flush

    # ── Layout engine: iterative placement — one call per FIRE_LIFT set ────────
    # Each call treats previously-placed boxes as obstacles so subsequent sets
    # naturally land on a different (non-overlapping) side of the anchor.
    from .core_layout_engine import find_layout_for_set, _USE_LAYOUT_ENGINE, _box_inside_footprint

    # Synthesize rectangular footprint polygon for standard buildings.
    # footprint_pts is None for rectangular buildings (only set for organic/SVG).
    # Without a polygon the engine has no boundary to enforce — clusters can protrude.
    # The entire core (pax bank + fire cluster) runs full height, so it must stay
    # inside the smallest floor plate (setback upper floors are the binding constraint).
    # Normalise footprint_pts: Gemini sometimes sends [[outer_polygon], [hole_polygon]] or
    # [[outer_polygon]] instead of a flat [[x,y],...] list.  Extract just the outer ring.
    _engine_footprint = footprint_pts
    if _engine_footprint and _engine_footprint[0] and isinstance(_engine_footprint[0][0], (list, tuple)):
        _engine_footprint = _engine_footprint[0]
    if _engine_footprint is None and all_floor_dims:
        _fp_w, _fp_l = min(all_floor_dims, key=lambda d: d[0] * d[1])
        _engine_footprint = [
            [-_fp_w / 2.0, -_fp_l / 2.0],
            [ _fp_w / 2.0, -_fp_l / 2.0],
            [ _fp_w / 2.0,  _fp_l / 2.0],
            [-_fp_w / 2.0,  _fp_l / 2.0],
        ]
    if _engine_footprint and lift_core_bounds_mm:
        if not _box_inside_footprint(lift_core_bounds_mm, _engine_footprint):
            _fslog("[LayoutEngine] WARNING: passenger lift bank extends outside footprint")
            # Return a CONFLICT immediately — OR-Tools will reject every candidate anyway
            # because the anchor itself violates the polygon.  Giving Gemini the right
            # diagnosis (move the lift bank, not resize the shell) avoids blind retries.
            _fp_xs = [p[0] for p in _engine_footprint]
            _fp_ys = [p[1] for p in _engine_footprint]
            return {
                "status": "CONFLICT",
                "type":   "ANCHOR_OUTSIDE_FOOTPRINT",
                "description": (
                    "The passenger lift bank position [{:.0f},{:.0f}]->[{:.0f},{:.0f}]mm is "
                    "outside (or straddling) the floor plate polygon. "
                    "Floor plate bbox: X=[{:.0f},{:.0f}], Y=[{:.0f},{:.0f}]mm. "
                    "Move lifts.position inside the floor plate so the entire passenger lift "
                    "bank fits within the building footprint. Do NOT resize the shell.".format(
                        lift_core_bounds_mm[0], lift_core_bounds_mm[1],
                        lift_core_bounds_mm[2], lift_core_bounds_mm[3],
                        min(_fp_xs), max(_fp_xs), min(_fp_ys), max(_fp_ys),
                    )
                ),
                "resolution_hints": [
                    "Move lifts.position so the full passenger lift bank sits inside the floor plate polygon.",
                    "For L/U/H shapes: place the bank inside one arm, not in the void/notch.",
                    "Do NOT change shell dimensions or building shape to fix this.",
                ],
            }

    # ── Pre-check: anchor must not overlap any footprint hole (courtyard void) ──
    # If the anchor AABB intersects a hole AABB, OR-Tools immediately returns
    # INFEASIBLE during presolve (two fixed NoOverlap2D intervals clash).
    # Return CONFLICT right away with precise repositioning guidance.
    if footprint_holes and lift_core_bounds_mm:
        _ax1, _ay1, _ax2, _ay2 = lift_core_bounds_mm
        _a_bank_hd = (_ay2 - _ay1) / 2.0
        _a_bank_hw = (_ax2 - _ax1) / 2.0
        for _h in footprint_holes:
            _hxs = [p[0] for p in _h]; _hys = [p[1] for p in _h]
            _hx1, _hx2 = min(_hxs), max(_hxs)
            _hy1, _hy2 = min(_hys), max(_hys)
            # AABB overlap (strict — 1mm tolerance)
            if _ax1 < _hx2 - 1 and _ax2 > _hx1 + 1 and _ay1 < _hy2 - 1 and _ay2 > _hy1 + 1:
                _fslog("[LayoutEngine] CONFLICT: anchor ({:.0f},{:.0f},{:.0f},{:.0f}) overlaps "
                       "hole ({:.0f},{:.0f},{:.0f},{:.0f})".format(
                       _ax1, _ay1, _ax2, _ay2, _hx1, _hy1, _hx2, _hy2))
                # Compact rectangle: all modules side-by-side parallel to bank face.
                # Perpendicular depth = deepest single module = staircase depth.
                _pre_chain_d = sd_nat
                # For south placement: bank_y2 ≤ hole_y1 AND bank_y1 ≥ fp_ymin + chain_d
                # → valid south centre: fp_ymin + chain_d + bank_hd ≤ bcy ≤ hole_y1 - bank_hd
                # For north placement: bank_y1 ≥ hole_y2 AND bank_y2 ≤ fp_ymax - chain_d
                # → valid north centre: hole_y2 + bank_hd ≤ bcy ≤ fp_ymax - chain_d - bank_hd
                _fp_ys_all = [p[1] for p in _engine_footprint] if _engine_footprint else [0, 60000]
                _fp_ymin, _fp_ymax = min(_fp_ys_all), max(_fp_ys_all)
                _fp_xs_all = [p[0] for p in _engine_footprint] if _engine_footprint else [0, 60000]
                _fp_xmin, _fp_xmax = min(_fp_xs_all), max(_fp_xs_all)
                _s_lo = int(_fp_ymin + _pre_chain_d + _a_bank_hd)
                _s_hi = int(_hy1 - _a_bank_hd)
                _n_lo = int(_hy2 + _a_bank_hd)
                _n_hi = int(_fp_ymax - _pre_chain_d - _a_bank_hd)
                _e_lo = int(_hx2 + _a_bank_hw)
                _e_hi = int(_fp_xmax - _pre_chain_d - _a_bank_hw)
                _w_lo = int(_fp_xmin + _pre_chain_d + _a_bank_hw)
                _w_hi = int(_hx1 - _a_bank_hw)
                _hints = []
                if _s_lo <= _s_hi:
                    _hints.append("South of void: bank_centre_y in [{:.0f}, {:.0f}]mm".format(_s_lo, _s_hi))
                else:
                    _hints.append("South of void: no valid position (need {:.0f}mm gap south of bank + {:.0f}mm chain — insufficient space)".format(
                        _a_bank_hd, _pre_chain_d))
                if _n_lo <= _n_hi:
                    _hints.append("North of void: bank_centre_y in [{:.0f}, {:.0f}]mm".format(_n_lo, _n_hi))
                else:
                    _hints.append("North of void: no valid position (need {:.0f}mm chain north of bank before wall)".format(_pre_chain_d))
                if _e_lo <= _e_hi:
                    _hints.append("East of void: bank_centre_x in [{:.0f}, {:.0f}]mm".format(_e_lo, _e_hi))
                if _w_lo <= _w_hi:
                    _hints.append("West of void: bank_centre_x in [{:.0f}, {:.0f}]mm".format(_w_lo, _w_hi))
                _viable = [h for h in _hints if "no valid" not in h]
                _desc = (
                    "Passenger lift bank [{:.0f},{:.0f}]->[{:.0f},{:.0f}]mm overlaps "
                    "courtyard void [{:.0f},{:.0f}]->[{:.0f},{:.0f}]mm. "
                    "The bank must sit entirely outside the void. "
                    "Chain depth needed: {:.0f}mm (fire_lift {:.0f} + lobby {:.0f} + stair {:.0f}). "
                    "{}".format(
                        _ax1, _ay1, _ax2, _ay2,
                        _hx1, _hy1, _hx2, _hy2,
                        _pre_chain_d, fl_shaft_d, ew_lb_dx, sd_nat,
                        " | ".join(_hints) if _hints else "No valid EW position exists — use NS bank orientation instead."
                    )
                )
                return {
                    "status": "CONFLICT",
                    "type":   "ANCHOR_OVERLAPS_VOID",
                    "description": _desc,
                    "resolution_hints": (
                        _viable if _viable else [
                            "No valid EW bank position exists outside the courtyard void with sufficient chain clearance.",
                            "Use NS bank orientation (place lifts.position on the east or west side of the floor plate, "
                            "away from the courtyard). NS banks use E/W chain direction.",
                            "Do NOT change shell dimensions or building shape to fix this.",
                        ]
                    ),
                }

    _placed_layouts = []
    _placed_boxes   = []
    _preferred_order = "NE"   # NE = rotated-staircase corner layout (user's preferred 2×2)
    _preferred_side  = None   # opposite side locked after first placement
    _opposite_side   = {"N": "S", "S": "N", "E": "W", "W": "E"}
    _ew_bank         = None   # kept for CONFLICT branch guard; always None (no side restriction)
    _allowed_sides   = None

    if _USE_LAYOUT_ENGINE and lift_core_bounds_mm:
        _anchor = lift_core_bounds_mm
        # Corridor strip inside the passenger lift bank: lobby_width wide, full bank X extent.
        # OR-Tools uses this to know which specific ends of the passenger lobby must stay open.
        lift_core_cy = (lift_core_bounds_mm[1] + lift_core_bounds_mm[3]) / 2.0
        _pax_lobby_bounds = (
            lift_core_bounds_mm[0],
            lift_core_cy - lobby_width / 2.0,
            lift_core_bounds_mm[2],
            lift_core_cy + lobby_width / 2.0,
        )

        _l_xmin, _l_ymin, _l_xmax, _l_ymax = _anchor
        # OR-Tools tries all four sides and picks the most compact via cl_perim objective.
        _allowed_sides = None

        # Always use NE layout: staircase (rotated, long side EW) on N/S face of anchor,
        # fire lift on E face, lobby in NE/SE corner.  Works for all anchor widths —
        # when staircase is narrower a notch appears at the west end which is acceptable.
        _anc_w_chk = _l_xmax - _l_xmin
        _fslog("[LayoutEngine] anchor_w={:.0f} sd_nat={:.0f} gap={:.0f} → preferred_order=NE".format(
            _anc_w_chk, sd_nat, _anc_w_chk - sd_nat))
        _fslog("[LayoutEngine] bank orientation: unrestricted — OR-Tools picks best side via cl_perim")

        # Per-set anchors: when there are exactly 2 non-perimeter fire sets,
        # each set gets its own anchor so both staircases attach to an independent
        # portion of the bank.  Two cases:
        #
        #   Multi-block bank (num_blocks >= 2):
        #     Each fire set anchors to a full block (N block / S block).
        #     The staircase extends outward from the block's outer face, so it
        #     always lands outside the overall bank Y extents.
        #
        #   Single-block 2-row bank (num_blocks == 1):
        #     Use half-row anchors as before — Set 0 (north) = inner N row,
        #     Set 1 (south) = inner S row.
        _non_perim_fire_sets = [
            i for i, s in enumerate(safety_sets)
            if not s.get("is_perimeter") and s["type"] == "FIRE_LIFT"
        ]
        _lift_shaft_depth = _PAX_CAR_D + 2 * _WALL_THICKNESS  # matches fl_shaft_d
        _bank_cy = (_l_ymin + _l_ymax) / 2.0
        _layout_nb = layout["num_blocks"] if layout else 1
        _layout_bd = layout["block_d"]    if layout else (_l_ymax - _l_ymin)
        if len(_non_perim_fire_sets) == 2 and num_lifts and num_lifts >= 4:
            if _layout_nb >= 2:
                # Multi-block: anchor each fire set to a full block.
                # Block 0 (southernmost) spans [bank_cy - N*bd/2, bank_cy - (N-1)*bd/2].
                # For N=2: Block 0 = [_bank_cy - block_d, _bank_cy], Block 1 = [_bank_cy, _bank_cy + block_d].
                _north_anc = (_l_xmin, _bank_cy,                           # Block N-1 (top)
                              _l_xmax, _bank_cy + _layout_bd)
                _south_anc = (_l_xmin, _bank_cy - _layout_bd,              # Block 0 (bottom)
                              _l_xmax, _bank_cy)
                _per_set_anchors = {
                    _non_perim_fire_sets[0]: _north_anc,
                    _non_perim_fire_sets[1]: _south_anc,
                }
                _fslog("[LayoutEngine] 2-set block anchors (num_blocks={}): Set{}→N-block({:.0f},{:.0f},{:.0f},{:.0f}) "
                       "Set{}→S-block({:.0f},{:.0f},{:.0f},{:.0f})".format(
                       _layout_nb,
                       _non_perim_fire_sets[0], *_north_anc,
                       _non_perim_fire_sets[1], *_south_anc))
            else:
                # Single-block: use inner-row half-bank anchors (original logic).
                # North half-bank (Row1, upper y-range)
                _north_anc = (_l_xmin, _bank_cy + lobby_width / 2.0,
                              _l_xmax, _bank_cy + lobby_width / 2.0 + _lift_shaft_depth)
                # South half-bank (Row2, lower y-range)
                _south_anc = (_l_xmin, _bank_cy - _lift_shaft_depth - lobby_width / 2.0,
                              _l_xmax, _bank_cy - lobby_width / 2.0)
                _per_set_anchors = {
                    _non_perim_fire_sets[0]: _north_anc,
                    _non_perim_fire_sets[1]: _south_anc,
                }
                _fslog("[LayoutEngine] 2-set half-bank anchors: Set{}→N({:.0f},{:.0f},{:.0f},{:.0f}) "
                       "Set{}→S({:.0f},{:.0f},{:.0f},{:.0f})".format(
                       _non_perim_fire_sets[0], *_north_anc,
                       _non_perim_fire_sets[1], *_south_anc))
        else:
            _per_set_anchors = {}

        # Anchor is fixed at Gemini's intended position — no internal sliding.
        # The snap-to-valid retry (below) shifts anchor_bounds for the whole solve
        # attempt when the primary solve fails, which IS reflected in the manifest.
        _snap_zone = None
        _snap_all_bands_infeasible = False
        _snap_min_building = 0

        for _sidx, _sset in enumerate(safety_sets):
            if _sset.get("is_perimeter") or _sset["type"] != "FIRE_LIFT":
                _placed_layouts.append(None)
                continue
            _fslog("[LayoutInput] set={} anchor=({:.0f},{:.0f},{:.0f},{:.0f}) "
                   "fl={}x{} lb={}x{} st={}x{} pref_side={} pref_order={} allowed={}".format(
                   _sidx,
                   _anchor[0], _anchor[1], _anchor[2], _anchor[3],
                   fl_shaft_d, fl_shaft_d, ew_lb_dx, lb_d_eng, sw_nat, sd_nat,
                   _preferred_side, _preferred_order, _allowed_sides))
            # Select per-set anchor: half-bank when 2 fire sets exist, full bank otherwise.
            _set_anchor = _per_set_anchors.get(_sidx, _anchor)
            # Half-bank has no internal lobby corridor — suppress pax_lobby_bounds.
            _set_pax_lobby = None if _sidx in _per_set_anchors else _pax_lobby_bounds
            # Lock side: Set 0 (north half-bank) → attach N; Set 1 (south half-bank) → attach S.
            if _per_set_anchors:
                _side_hint = "N" if _sidx == _non_perim_fire_sets[0] else "S"
            else:
                _side_hint = _preferred_side if _preferred_side else ("N" if _preferred_order in ("NE", "DW") else None)
            _layout = find_layout_for_set(
                anchor_bounds    = _set_anchor,
                fire_lift_size   = (fl_shaft_d, fl_shaft_d),
                lobby_size       = (ew_lb_dx,   lb_d_eng),
                staircase_size   = (sw_nat,     sd_nat),
                already_placed   = list(_placed_boxes),
                footprint_pts    = _engine_footprint,
                footprint_holes  = footprint_holes,
                log_fn           = _fslog,
                preferred_order  = _preferred_order,
                preferred_side   = _side_hint,
                pax_lobby_bounds = _set_pax_lobby,
                allowed_sides    = (_allowed_sides if not _per_set_anchors
                                    else (["N"] if _sidx == _non_perim_fire_sets[0] else ["S"])),
                anchor_snap_zone = _snap_zone,
            )
            # If this is Set N>0 and OR-Tools returned the same attach_side as Set 0,
            # check whether the placement actually collides with already-placed boxes.
            # If there is no collision, accept it — two clusters on the same side at
            # different positions is architecturally valid (e.g. both on West at opposite
            # ends of a rectangular building).  Only discard (→ hardcoded fallback) when
            # the placement would genuinely overlap an existing cluster.
            if (_layout and _sidx > 0 and _preferred_side is not None
                    and _layout["attach_side"] != _preferred_side):
                _tol_same = 50   # 50mm collision tolerance
                _new_boxes_chk = [_layout["fire_lift"], _layout["lobby"], _layout["staircase"]]
                _collides = any(
                    (_nb[0] < _ob[2] - _tol_same and _nb[2] > _ob[0] + _tol_same and
                     _nb[1] < _ob[3] - _tol_same and _nb[3] > _ob[1] + _tol_same)
                    for _nb in _new_boxes_chk
                    for _ob in _placed_boxes
                )
                if _collides:
                    _fslog("[LayoutEngine] Set {} solved to same side ({}) as Set 0 and collides — "
                           "discarding, using hardcoded fallback".format(
                               _sidx, _layout["attach_side"]))
                    _layout = None
                else:
                    _fslog("[LayoutEngine] Set {} solved to same side ({}) as Set 0 but no collision — "
                           "accepting placement".format(_sidx, _layout["attach_side"]))

            # Snap-to-valid: if OR-Tools failed, try shifting the bank by up to 2000mm
            # to the nearest valid position and re-solving. Gemini is unreliable at exact
            # polygon arithmetic, so a small offset often produces a solvable position.
            # When no side is locked yet (_preferred_side is None), try all 4 cardinal sides.
            if _layout is None and _engine_footprint:
                _snap_sides = [_preferred_side] if _preferred_side else ["N", "S", "E", "W"]
                _cluster_d_need = max(fl_shaft_d, lb_net_y + 2 * t, sd_nat)
                for _snap_side in _snap_sides:
                    _snapped = _snap_bank_to_valid(
                        _anchor, _cluster_d_need, _snap_side, _engine_footprint)
                    if _snapped != _anchor:
                        _fslog("[LayoutEngine] Set {} — snap-to-valid: anchor shifted {} → {} on {} face".format(
                            _sidx, (round(_anchor[0]), round(_anchor[1]), round(_anchor[2]), round(_anchor[3])),
                            (round(_snapped[0]), round(_snapped[1]), round(_snapped[2]), round(_snapped[3])),
                            _snap_side))
                        # Rebuild pax_lobby_bounds for the snapped anchor
                        _snapped_cy = (_snapped[1] + _snapped[3]) / 2.0
                        _snapped_pax_lobby = (
                            _snapped[0],
                            _snapped_cy - lobby_width / 2.0,
                            _snapped[2],
                            _snapped_cy + lobby_width / 2.0,
                        )
                        _layout = find_layout_for_set(
                            anchor_bounds    = _snapped,
                            fire_lift_size   = (fl_shaft_d, fl_shaft_d),
                            lobby_size       = (ew_lb_dx,   lb_net_y + 2 * t),
                            staircase_size   = (sw_nat,     sd_nat),
                            already_placed   = list(_placed_boxes),
                            footprint_pts    = _engine_footprint,
                            footprint_holes  = footprint_holes,
                            log_fn           = _fslog,
                            preferred_order  = _preferred_order,
                            preferred_side   = _snap_side,
                            pax_lobby_bounds = _snapped_pax_lobby,
                            anchor_snap_zone = _snap_zone,
                        )
                        if _layout:
                            _fslog("[LayoutEngine] Set {} — snap retry succeeded (side={})".format(
                                _sidx, _layout.get("attach_side")))
                            break
                        else:
                            _fslog("[LayoutEngine] Set {} — snap retry failed on {} side".format(
                                _sidx, _snap_side))
                else:
                    if not (_preferred_side is None and _snapped == _anchor):
                        _fslog("[LayoutEngine] Set {} — all snap attempts exhausted".format(_sidx))

            if _layout:
                _fl_b = _layout["fire_lift"]; _lb_b = _layout["lobby"]; _st_b = _layout["staircase"]
                _solved_anc = _layout.get("solved_anchor_bounds", _anchor)
                _drift_mm = math.hypot(
                    (_solved_anc[0] + _solved_anc[2]) / 2.0 - (_anchor[0] + _anchor[2]) / 2.0,
                    (_solved_anc[1] + _solved_anc[3]) / 2.0 - (_anchor[1] + _anchor[3]) / 2.0,
                )
                _fslog("[LayoutResult] set={} side={} rot={} anchor_drift={:.0f}mm "
                       "solved_anchor=({:.0f},{:.0f},{:.0f},{:.0f}) "
                       "fl=({:.0f},{:.0f},{:.0f},{:.0f}) "
                       "lb=({:.0f},{:.0f},{:.0f},{:.0f}) "
                       "st=({:.0f},{:.0f},{:.0f},{:.0f})".format(
                       _sidx, _layout["attach_side"], _layout.get("stair_rot", "?"),
                       _drift_mm,
                       _solved_anc[0],_solved_anc[1],_solved_anc[2],_solved_anc[3],
                       _fl_b[0],_fl_b[1],_fl_b[2],_fl_b[3],
                       _lb_b[0],_lb_b[1],_lb_b[2],_lb_b[3],
                       _st_b[0],_st_b[1],_st_b[2],_st_b[3],
                ))

                # ── Corner-snap: shift the entire fire cluster so fl is flush with the
                # nearest anchor edge (E or W for N/S attach, N or S for E/W attach).
                # OR-Tools can float the cluster anywhere along the anchor face; snapping
                # to the corner closes the open gap between the cluster and the anchor
                # side wall, and realises the user's compact 2×2 layout.
                # NE order is already right-flush by construction — skip snap.
                _att_side = _layout.get("attach_side", "")
                _chain_order = _layout.get("chain_order", "")
                _ax1s, _ay1s, _ax2s, _ay2s = _solved_anc
                _snap_dx = _snap_dy = 0.0
                if _chain_order != "NE":
                    if _att_side in ("N", "S"):
                        _fl_cx = (_fl_b[0] + _fl_b[2]) / 2.0
                        _anc_cx = (_ax1s + _ax2s) / 2.0
                        if _fl_cx >= _anc_cx:  # fl in E half → flush fl right edge with anchor right
                            _snap_dx = _ax2s - _fl_b[2]
                        else:                  # fl in W half → flush fl left edge with anchor left
                            _snap_dx = _ax1s - _fl_b[0]
                    elif _att_side in ("E", "W"):
                        _fl_cy = (_fl_b[1] + _fl_b[3]) / 2.0
                        _anc_cy = (_ay1s + _ay2s) / 2.0
                        if _fl_cy >= _anc_cy:
                            _snap_dy = _ay2s - _fl_b[3]
                        else:
                            _snap_dy = _ay1s - _fl_b[1]

                if abs(_snap_dx) > 0.5 or abs(_snap_dy) > 0.5:
                    def _shift_box(box, dx, dy):
                        return (box[0]+dx, box[1]+dy, box[2]+dx, box[3]+dy)
                    _fl_b = _shift_box(_fl_b, _snap_dx, _snap_dy)
                    _lb_b = _shift_box(_lb_b, _snap_dx, _snap_dy)
                    _st_b = _shift_box(_st_b, _snap_dx, _snap_dy)
                    _layout = dict(_layout)
                    _layout["fire_lift"]  = _fl_b
                    _layout["lobby"]      = _lb_b
                    _layout["staircase"]  = _st_b
                    _fslog("[LayoutSnap] set={} dx={:.0f} dy={:.0f} → "
                           "fl=({:.0f},{:.0f},{:.0f},{:.0f}) "
                           "lb=({:.0f},{:.0f},{:.0f},{:.0f}) "
                           "st=({:.0f},{:.0f},{:.0f},{:.0f})".format(
                           _sidx, _snap_dx, _snap_dy,
                           _fl_b[0],_fl_b[1],_fl_b[2],_fl_b[3],
                           _lb_b[0],_lb_b[1],_lb_b[2],_lb_b[3],
                           _st_b[0],_st_b[1],_st_b[2],_st_b[3]))

                # If the solver slid the anchor, update _anchor and _pax_lobby_bounds
                # so Set 1 treats the correct bank position as its fixed anchor.
                if _drift_mm > 1.0:
                    _anchor = _solved_anc
                    _solved_cy = (_anchor[1] + _anchor[3]) / 2.0
                    _pax_lobby_bounds = (
                        _anchor[0],
                        _solved_cy - lobby_width / 2.0,
                        _anchor[2],
                        _solved_cy + lobby_width / 2.0,
                    )
                    _fslog("[LayoutEngine] Anchor updated to solved position for Set 1+")
            else:
                _fslog("[LayoutResult] set={} — NO SOLUTION".format(_sidx))
            _placed_layouts.append(_layout)
            if _layout:
                # Register this set's boxes as obstacles for all subsequent sets.
                _new_boxes = [_layout["fire_lift"], _layout["lobby"], _layout["staircase"]]
                _placed_boxes.extend(_new_boxes)
                # Lock order + opposite side for all subsequent sets
                if _preferred_side is None:
                    _preferred_order = _layout["chain_order"]
                    _preferred_side  = _opposite_side.get(_layout["attach_side"])
                    _fslog("[LayoutEngine] Locking order={} side={} for symmetry".format(
                        _preferred_order, _preferred_side))
                    # Always try to mirror Set 0 to get Set 1 — not just for 2-set buildings.
                    # The mirror is geometrically valid by construction (it's a reflection
                    # of a constraint-satisfying OR-Tools solution) so it won't produce the
                    # corridor-blocking or staircase-gap artefacts that the hardcoded fallback
                    # can produce.  The collision guard below rejects it if it overlaps Set 0.
                    if _sidx == 0:
                        _mirror = _mirror_layout(_layout, lift_core_bounds_mm,
                                                 _preferred_side, _fslog)
                        # Validate the mirror doesn't collide with Set 0.
                        # If it does (can happen when anchor isn't centred at origin),
                        # fall through to OR-Tools for Set 1 instead of using the mirror.
                        _mirror_ok = False
                        if _mirror:
                            _mir_boxes = [_mirror["fire_lift"], _mirror["lobby"], _mirror["staircase"]]
                            _tol_g = 1  # 1 grid unit = 50mm
                            _mirror_ok = True
                            for _mb in _mir_boxes:
                                for _ob in _placed_boxes:
                                    # AABB overlap check
                                    if (_mb[0] < _ob[2] - _tol_g and _mb[2] > _ob[0] + _tol_g and
                                            _mb[1] < _ob[3] - _tol_g and _mb[3] > _ob[1] + _tol_g):
                                        _mirror_ok = False
                                        _fslog("[LayoutEngine] Mirror collision detected — re-solving Set 1")
                                        break
                                if not _mirror_ok:
                                    break
                        if _mirror_ok:
                            _placed_layouts.append(_mirror)
                            _placed_boxes.extend([_mirror["fire_lift"],
                                                  _mirror["lobby"],
                                                  _mirror["staircase"]])
                            _fslog("[LayoutEngine] Set 1 — mirrored from Set 0 (side={})".format(
                                _mirror["attach_side"]))
                            break  # both sets placed, skip remaining loop iterations
                        # Mirror failed or collided — continue loop to let OR-Tools solve Set 1
            else:
                _fslog("[LayoutEngine] Set {} — no valid placement, will use hardcoded path".format(_sidx))

    # Detect OR-Tools failures and record minimum core dimensions for CONFLICT feedback.
    _ortools_failed_sets = [
        i for i, l in enumerate(_placed_layouts)
        if l is None
        and i < len(safety_sets)
        and not safety_sets[i].get("is_perimeter")
        and safety_sets[i].get("type") == "FIRE_LIFT"
    ]
    if _ortools_failed_sets:
        # Compact rectangle: all 3 modules side-by-side parallel to bank face.
        # EW width = sum of all module widths (fire_lift + lobby + stair).
        # NS depth = deepest single module = staircase depth.
        _min_core_w   = fl_shaft_d + ew_lb_dx + sw_nat    # EW width  (for CONFLICT message label)
        _min_chain_d  = sd_nat                             # NS depth  (deepest module = staircase)
        _min_core_d   = max(fl_shaft_d, lb_net_y + 2 * t, sd_nat)   # deepest single module
        _fslog("[LayoutEngine] {} set(s) unsolvable. Min core: w={}mm chain_depth={}mm".format(
            len(_ortools_failed_sets), round(_min_core_w), round(_min_chain_d)))

        # Build per-set failure details so Gemini knows exactly where to move each bank.
        _failure_details = []
        _fp_for_clr = _engine_footprint  # polygon used during solving
        for _fi in _ortools_failed_sets:
            _fs = safety_sets[_fi]
            _bx, _by = _fs.get("pos", (0, 0))
            _tried = _preferred_side or (_allowed_sides[0] if _allowed_sides and len(_allowed_sides) == 1 else "any")
            # Compute available clearance on the tried side from the bank edge to the
            # nearest obstruction — either the footprint boundary or a hole that overlaps
            # the bank's X/Y extent (e.g. a courtyard).
            _bank_hd = (l_ymax - l_ymin) / 2.0 if lift_core_bounds_mm else 0
            _bank_hw = (l_xmax - l_xmin) / 2.0 if lift_core_bounds_mm else 0
            _bank_y1 = _by - _bank_hd
            _bank_y2 = _by + _bank_hd
            _bank_x1 = _bx - _bank_hw
            _bank_x2 = _bx + _bank_hw

            def _hole_adj_clr(side, bx1, bx2, by1, by2, fp_xmin, fp_xmax, fp_ymin, fp_ymax, holes):
                """Clearance on `side` reduced by any hole that overlaps the bank's transverse span."""
                if side == "N":
                    wall = fp_ymax
                    for h in (holes or []):
                        hxs = [p[0] for p in h]; hys = [p[1] for p in h]
                        hx1, hx2, hy1 = min(hxs), max(hxs), min(hys)
                        # hole overlaps bank X-extent and is north of bank
                        if hx1 < bx2 and hx2 > bx1 and hy1 >= by2:
                            wall = min(wall, hy1)
                    return int(wall - by2)
                elif side == "S":
                    wall = fp_ymin
                    for h in (holes or []):
                        hxs = [p[0] for p in h]; hys = [p[1] for p in h]
                        hx1, hx2, hy2 = min(hxs), max(hxs), max(hys)
                        if hx1 < bx2 and hx2 > bx1 and hy2 <= by1:
                            wall = max(wall, hy2)
                    return int(by1 - wall)
                elif side == "E":
                    wall = fp_xmax
                    for h in (holes or []):
                        hxs = [p[0] for p in h]; hys = [p[1] for p in h]
                        hx1, hy1, hy2 = min(hxs), min(hys), max(hys)
                        if hy1 < by2 and hy2 > by1 and hx1 >= bx2:
                            wall = min(wall, hx1)
                    return int(wall - bx2)
                elif side == "W":
                    wall = fp_xmin
                    for h in (holes or []):
                        hxs = [p[0] for p in h]; hys = [p[1] for p in h]
                        hx2, hy1, hy2 = max(hxs), min(hys), max(hys)
                        if hy1 < by2 and hy2 > by1 and hx2 <= bx1:
                            wall = max(wall, hx2)
                    return int(bx1 - wall)
                return 0

            _holes_for_clr = footprint_holes or []
            _clr_avail = None
            _valid_range = None
            # _min_chain_d = NS clearance needed (fl+lb+st stacked in Y for EW banks).
            # _min_core_d = deepest single module — used only for the conflict dict entry.
            _chain_depth = _min_chain_d

            def _band_range_y(side, fp_ymin, fp_ymax, bank_hd, chain_d, holes, bx1, bx2):
                """Return (lo, hi, void_limit) for the valid bank_centre_y range on side N or S.
                lo > hi means geometrically impossible (band too narrow).
                void_limit is the closest void face restricting this side, or None.
                """
                void_limit = None
                if side == "S":
                    lo = fp_ymin + bank_hd + chain_d
                    hi = fp_ymax  # will be tightened by void
                    for h in (holes or []):
                        hxs = [p[0] for p in h]; hys = [p[1] for p in h]
                        hx1h, hx2h, hy1h = min(hxs), max(hxs), min(hys)
                        # bank must sit BELOW void south face (hy1h), not north face (hy2h)
                        if hx1h < bx2 and hx2h > bx1:
                            hi = min(hi, hy1h - bank_hd)
                            void_limit = hy1h
                    return int(lo), int(hi), void_limit
                else:  # "N"
                    hi = fp_ymax - bank_hd - chain_d
                    lo = fp_ymin  # will be tightened by void
                    for h in (holes or []):
                        hxs = [p[0] for p in h]; hys = [p[1] for p in h]
                        hx1h, hx2h, hy2h = min(hxs), max(hxs), max(hys)
                        # bank must be NORTH of void north face (hy2h), not south face
                        if hx1h < bx2 and hx2h > bx1:
                            lo = max(lo, hy2h + bank_hd)
                            void_limit = hy2h
                    return int(lo), int(hi), void_limit

            def _band_range_x(side, fp_xmin, fp_xmax, bank_hw, chain_d, holes, by1, by2):
                """Return (lo, hi, void_limit) for the valid bank_centre_x range on side E or W."""
                void_limit = None
                if side == "W":
                    lo = fp_xmin + bank_hw + chain_d
                    hi = fp_xmax
                    for h in (holes or []):
                        hxs = [p[0] for p in h]; hys = [p[1] for p in h]
                        hx1h, hy1h, hy2h = min(hxs), min(hys), max(hys)
                        # bank must sit WEST of void west face (hx1h), not east face (hx2h)
                        if hy1h < by2 and hy2h > by1:
                            hi = min(hi, hx1h - bank_hw)
                            void_limit = hx1h
                    return int(lo), int(hi), void_limit
                else:  # "E"
                    hi = fp_xmax - bank_hw - chain_d
                    lo = fp_xmin
                    for h in (holes or []):
                        hxs = [p[0] for p in h]; hys = [p[1] for p in h]
                        hx2h, hy1h, hy2h = max(hxs), min(hys), max(hys)
                        # bank must be EAST of void east face (hx2h), not west face
                        if hy1h < by2 and hy2h > by1:
                            lo = max(lo, hx2h + bank_hw)
                            void_limit = hx2h
                    return int(lo), int(hi), void_limit

            def _fmt_side_y(side, lo, hi, void_limit, fp_ymin, fp_ymax, bank_hd, chain_d, clr, by):
                band_depth = (fp_ymax - fp_ymin) - 2 * bank_hd if void_limit is None else (
                    (void_limit - fp_ymin) if side == "S" else (fp_ymax - void_limit))
                if lo > hi:
                    return ("{} IMPOSSIBLE: band {}mm deep, need chain {}mm + bank_depth {}mm = {}mm. "
                            "avail={}mm (hole-adj). Fix: reduce lift count, widen building, or shrink courtyard.").format(
                        side, int(band_depth), int(chain_d), int(2 * bank_hd),
                        int(chain_d + 2 * bank_hd), clr)
                rec = int((lo + hi) / 2)
                if side == "S":
                    return ("S avail={}mm (hole-adj). Need {}mm. bank_centre_y in [{}, {}]mm. "
                            "Recommended: bank_centre_y={}mm (centred in valid range)").format(
                        clr, int(chain_d), lo, hi, rec)
                else:
                    return ("N avail={}mm (hole-adj). Need {}mm. bank_centre_y in [{}, {}]mm. "
                            "Recommended: bank_centre_y={}mm (centred in valid range)").format(
                        clr, int(chain_d), lo, hi, rec)

            def _fmt_side_x(side, lo, hi, void_limit, fp_xmin, fp_xmax, bank_hw, chain_d, clr, bx):
                band_depth = (fp_xmax - fp_xmin) - 2 * bank_hw if void_limit is None else (
                    (void_limit - fp_xmin) if side == "W" else (fp_xmax - void_limit))
                if lo > hi:
                    return ("{} IMPOSSIBLE: band {}mm wide, need chain {}mm + bank_depth {}mm = {}mm. "
                            "avail={}mm (hole-adj). Fix: reduce lift count, widen building, or shrink courtyard.").format(
                        side, int(band_depth), int(chain_d), int(2 * bank_hw),
                        int(chain_d + 2 * bank_hw), clr)
                if side == "W":
                    return ("W avail={}mm (hole-adj). Need {}mm. bank_centre_x in [{}, {}]mm. "
                            "Recommended: bank_centre_x={}mm (move east {}mm)").format(
                        clr, int(chain_d), lo, hi, lo, max(0, lo - int(bx)))
                else:
                    return ("E avail={}mm (hole-adj). Need {}mm. bank_centre_x in [{}, {}]mm. "
                            "Recommended: bank_centre_x={}mm (move west {}mm)").format(
                        clr, int(chain_d), lo, hi, hi, max(0, int(bx) - hi))

            if _fp_for_clr and len(_fp_for_clr) >= 3:
                _fp_xs = [p[0] for p in _fp_for_clr]
                _fp_ys = [p[1] for p in _fp_for_clr]
                _fp_xmin, _fp_xmax = min(_fp_xs), max(_fp_xs)
                _fp_ymin, _fp_ymax = min(_fp_ys), max(_fp_ys)
                if _tried == "N":
                    _clr_avail = _hole_adj_clr("N", _bank_x1, _bank_x2, _bank_y1, _bank_y2,
                                               _fp_xmin, _fp_xmax, _fp_ymin, _fp_ymax, _holes_for_clr)
                    _lo, _hi, _vl = _band_range_y("N", _fp_ymin, _fp_ymax, _bank_hd, _chain_depth,
                                                   _holes_for_clr, _bank_x1, _bank_x2)
                    _valid_range = _fmt_side_y("N", _lo, _hi, _vl, _fp_ymin, _fp_ymax,
                                               _bank_hd, _chain_depth, _clr_avail, _by)
                elif _tried == "S":
                    _clr_avail = _hole_adj_clr("S", _bank_x1, _bank_x2, _bank_y1, _bank_y2,
                                               _fp_xmin, _fp_xmax, _fp_ymin, _fp_ymax, _holes_for_clr)
                    _lo, _hi, _vl = _band_range_y("S", _fp_ymin, _fp_ymax, _bank_hd, _chain_depth,
                                                   _holes_for_clr, _bank_x1, _bank_x2)
                    _valid_range = _fmt_side_y("S", _lo, _hi, _vl, _fp_ymin, _fp_ymax,
                                               _bank_hd, _chain_depth, _clr_avail, _by)
                elif _tried == "E":
                    _clr_avail = _hole_adj_clr("E", _bank_x1, _bank_x2, _bank_y1, _bank_y2,
                                               _fp_xmin, _fp_xmax, _fp_ymin, _fp_ymax, _holes_for_clr)
                    _lo, _hi, _vl = _band_range_x("E", _fp_xmin, _fp_xmax, _bank_hw, _chain_depth,
                                                   _holes_for_clr, _bank_y1, _bank_y2)
                    _valid_range = _fmt_side_x("E", _lo, _hi, _vl, _fp_xmin, _fp_xmax,
                                               _bank_hw, _chain_depth, _clr_avail, _bx)
                elif _tried == "W":
                    _clr_avail = _hole_adj_clr("W", _bank_x1, _bank_x2, _bank_y1, _bank_y2,
                                               _fp_xmin, _fp_xmax, _fp_ymin, _fp_ymax, _holes_for_clr)
                    _lo, _hi, _vl = _band_range_x("W", _fp_xmin, _fp_xmax, _bank_hw, _chain_depth,
                                                   _holes_for_clr, _bank_y1, _bank_y2)
                    _valid_range = _fmt_side_x("W", _lo, _hi, _vl, _fp_xmin, _fp_xmax,
                                               _bank_hw, _chain_depth, _clr_avail, _bx)
                elif _tried == "restricted":
                    # Multiple allowed sides tried; report per-side feasibility with void-aware ranges.
                    _hints = []
                    _clr_avail = 0
                    for _side in _allowed_sides:
                        _c = _hole_adj_clr(_side, _bank_x1, _bank_x2, _bank_y1, _bank_y2,
                                           _fp_xmin, _fp_xmax, _fp_ymin, _fp_ymax, _holes_for_clr)
                        _clr_avail = max(_clr_avail, _c)
                        if _side in ("N", "S"):
                            _lo, _hi, _vl = _band_range_y(_side, _fp_ymin, _fp_ymax, _bank_hd,
                                                           _chain_depth, _holes_for_clr, _bank_x1, _bank_x2)
                            _hints.append(_fmt_side_y(_side, _lo, _hi, _vl, _fp_ymin, _fp_ymax,
                                                      _bank_hd, _chain_depth, _c, _by))
                        elif _side in ("E", "W"):
                            _lo, _hi, _vl = _band_range_x(_side, _fp_xmin, _fp_xmax, _bank_hw,
                                                           _chain_depth, _holes_for_clr, _bank_y1, _bank_y2)
                            _hints.append(_fmt_side_x(_side, _lo, _hi, _vl, _fp_xmin, _fp_xmax,
                                                      _bank_hw, _chain_depth, _c, _bx))
                    _valid_range = ("Tried sides {}. Need {}mm on at least one side. ".format(
                        "/".join(_allowed_sides), int(_chain_depth)) + " | ".join(_hints))
                else:
                    # "any" — engine tried all sides freely; report per-side feasibility
                    _clr_sides = {s: _hole_adj_clr(s, _bank_x1, _bank_x2, _bank_y1, _bank_y2,
                                                    _fp_xmin, _fp_xmax, _fp_ymin, _fp_ymax, _holes_for_clr)
                                  for s in ("N", "S", "E", "W")}
                    _best_side = max(_clr_sides.items(), key=lambda kv: kv[1])
                    _clr_avail = _best_side[1]
                    _side_msgs = []
                    for _s in ("N", "S", "E", "W"):
                        _c = _clr_sides[_s]
                        if _s in ("N", "S"):
                            _lo2, _hi2, _vl2 = _band_range_y(_s, _fp_ymin, _fp_ymax, _bank_hd,
                                                              _chain_depth, _holes_for_clr, _bank_x1, _bank_x2)
                            _side_msgs.append(_fmt_side_y(_s, _lo2, _hi2, _vl2, _fp_ymin, _fp_ymax,
                                                          _bank_hd, _chain_depth, _c, _by))
                        else:
                            _lo2, _hi2, _vl2 = _band_range_x(_s, _fp_xmin, _fp_xmax, _bank_hw,
                                                              _chain_depth, _holes_for_clr, _bank_y1, _bank_y2)
                            _side_msgs.append(_fmt_side_x(_s, _lo2, _hi2, _vl2, _fp_xmin, _fp_xmax,
                                                          _bank_hw, _chain_depth, _c, _bx))
                    _valid_range = "All sides tried. Need {}mm. ".format(int(_chain_depth)) + " | ".join(_side_msgs)
            _failure_details.append({
                "set_index":        _fi,
                "bank_pos_mm":      [int(_bx), int(_by)],
                "tried_side":       _tried,
                "cluster_d_needed": int(_chain_depth),
                "clearance_avail":  _clr_avail,
                "valid_range":      _valid_range,
            })
            _fslog("[LayoutEngine] Set {} failure: bank=[{},{}] tried={} chain_needed={}mm avail={}mm".format(
                _fi, int(_bx), int(_by), _tried, int(_min_core_w),
                _clr_avail if _clr_avail is not None else "?"))

        _ortools_conflict = {
            "min_core_w": round(_min_core_w),
            "min_core_d": round(_min_core_d),
            "failed_set_count": len(_ortools_failed_sets),
            "failure_details": _failure_details,
        }

        # When OR-Tools failed with restricted sides (EW/NS bank orientation enforced),
        # the hardcoded fallback produces wrong geometry — return CONFLICT immediately
        # so Gemini can reposition the bank with the correct clearance information.
        if _ew_bank is not None:  # _ew_bank is set only when allowed_sides was applied
            _desc_parts = []
            for _fd in _failure_details:
                _desc_parts.append(
                    "Bank at [{},{}]mm needs {}mm clearance on {} side(s). {}".format(
                        _fd["bank_pos_mm"][0], _fd["bank_pos_mm"][1],
                        _fd["cluster_d_needed"],
                        "/".join(_allowed_sides),
                        _fd.get("valid_range") or ""))
            _chain_d = round(_min_chain_d)  # NS clearance needed: fl + lb + sd_nat stacked
            _conflict_desc = (
                "Fire cluster cannot fit on required {} side(s) of lift bank. "
                "Cluster chain needs {}mm deep (fire_lift {}mm + lobby {}mm + stair {}mm). "
                "{}".format(
                    "/".join(_allowed_sides), _chain_d,
                    fl_shaft_d, ew_lb_dx, sd_nat,
                    " | ".join(_desc_parts)))
            _fslog("[LayoutEngine] Returning CONFLICT: {}".format(_conflict_desc))
            _hints_out = [
                "Cluster chain depth needed: {}mm = fire_lift({}mm) + lobby({}mm) + stair({}mm). "
                "Each bank must have at least {}mm of clear space on its outward face "
                "(away from courtyard/void) before the building boundary.".format(
                    _chain_d, fl_shaft_d, ew_lb_dx, sd_nat, _chain_d),
            ]
            if _snap_all_bands_infeasible and _snap_min_building:
                _hints_out.append(
                    "ALL BANDS INFEASIBLE: building is too small for this core. "
                    "Minimum building size needed: {}mm. "
                    "MANDATORY: increase shell.length or shell.width to >= {}mm, "
                    "OR reduce lifts.count. Moving the bank position will NOT fix this.".format(
                        _snap_min_building, _snap_min_building))
            else:
                _hints_out.append(
                    "For EW banks in courtyard buildings: place south bank so bank_y1 >= {}mm "
                    "(centre_y = bank_y1 + half_bank_depth), place north bank so bank_y2 <= building_height - {}mm.".format(
                        _chain_d, _chain_d))
            return {
                "status": "CONFLICT",
                "type": "ORTOOLS_NO_SPACE",
                "description": _conflict_desc,
                "ortools_conflict": _ortools_conflict,
                "all_bands_infeasible": _snap_all_bands_infeasible,
                "min_building_mm": _snap_min_building,
                "resolution_hints": _hints_out,
            }
    else:
        _ortools_conflict = None

    for i, s_set in enumerate(safety_sets):
        is_fl      = (s_set["type"] == "FIRE_LIFT")
        entry_x, entry_y = s_set["pos"]
        tag        = "SafetySet_{}".format(i + 1)
        _skip_set  = False  # set True when cluster can't fit on either side

        # ── Perimeter SMOKE_STOP — dedicated layout (building edge, no fire lift) ──
        is_perimeter = s_set.get("is_perimeter", False)
        if is_perimeter:
            # Simple layout: staircase at building edge, lobby between staircase and floor plate.
            _rot_deg = s_set.get("rotation_deg", 0.0)
            _is_ew_edge = abs(abs(_rot_deg) - 90.0) < 1.0  # ±90° → EW (east/west) face

            # Truncate levels to only those where this perimeter staircase is needed.
            # "last_floor_idx" is the 0-based index (in all_floor_dims) of the highest
            # floor that requires this stair.  We include one extra level as the
            # overrun/cap so the shaft terminates cleanly at that storey.
            _last_fi = s_set.get("last_floor_idx")
            if (_last_fi is not None and all_floor_dims and
                    _last_fi + 2 < len(levels_data)):
                _set_levels = levels_data[:_last_fi + 2]
            else:
                _set_levels = levels_data
            _set_lvl_ids = [lvl['id'] for lvl in _set_levels[:-1]] if len(_set_levels) > 1 else ([_set_levels[0]['id']] if _set_levels else [])
            # Smoke-stop lobby: min 2000mm clear width (= sw_nat - 2t) already
            # satisfied by the staircase width.  Depth: 2000mm clear = 2400mm outer.
            # Target ~4-5 sqm net: 2000mm clear × (sw_nat - 2t) already large enough.
            lobby_d_p  = 2 * _WALL_THICKNESS + smoke_clear_d  # outer = clear depth + 2 walls
            fl_box = None  # no fire lift

            # Inset the staircase from the building edge so the floor-slab void does
            # not coincide with the slab boundary (which causes "can't make extrusion"
            # errors in Revit).  500 mm is enough to clear the 200 mm wall thickness
            # plus Revit's minimum-face-width tolerance.
            _EDGE_GAP = _EDGE_GAP_MM

            if _is_ew_edge:
                # EW-edge stair: positioned at east or west building edge.
                # The shaft itself runs N-S (sw_nat in X, sd_nat in Y) — same orientation
                # as generate_staircase_manifest always produces, so the walls align.
                # The lobby is a N-S strip on the floor-plate side (west of east stair,
                # east of west stair), lobby depth running in X.
                is_east_p = (entry_x >= _bld_cx)
                if is_east_p:
                    # Shaft near east wall: right edge inset by _EDGE_GAP from building edge.
                    st_x2 = entry_x - _EDGE_GAP
                    st_x1 = st_x2 - sw_nat          # shaft width (narrow) in X
                    lb_x2 = st_x1                   # lobby east face = stair west face
                    lb_x1 = lb_x2 - lobby_d_p       # lobby depth runs west
                    is_rotated_suit = False          # stair main landing faces west (floor-plate side)
                else:
                    # Shaft near west wall: left edge inset by _EDGE_GAP from building edge.
                    st_x1 = entry_x + _EDGE_GAP
                    st_x2 = st_x1 + sw_nat          # shaft width (narrow) in X
                    lb_x1 = st_x2                   # lobby west face = stair east face
                    lb_x2 = lb_x1 + lobby_d_p       # lobby depth runs east
                    is_rotated_suit = True           # stair main landing faces east (floor-plate side)
                # Shaft height (sd_nat) runs N-S, centred on entry_y
                st_base_y = entry_y - sd_nat / 2.0
                st_y2     = entry_y + sd_nat / 2.0
                lb_y1     = st_base_y               # lobby same Y extents as shaft
                lb_y2     = st_y2
                lb_box    = [lb_x1, lb_y1, lb_x2, lb_y2]
                st_box    = [st_x1, st_base_y, st_x2, st_y2]
                st_cx     = (st_x1 + st_x2) / 2.0
                st_cy     = entry_y
                st_rect   = [min(st_x1, lb_x1), st_base_y, max(st_x2, lb_x2), st_y2]
                st_y1     = st_base_y               # alias used by door-spec block below
                st_y2_ew  = st_y2                   # alias for door-spec block below
                is_south_p = False                  # EW: use "not south" door-orientation path
            else:
                # NS-facing stair: entry_y is absolute building-edge Y coordinate
                is_south_p = (entry_y <= _bld_cy)
                if is_south_p:
                    # Staircase at south edge (inset), lobby north of it (facing floor plate)
                    st_base_y  = entry_y + _EDGE_GAP
                    st_y2      = st_base_y + sd_nat
                    lb_y1, lb_y2 = st_y2, st_y2 + lobby_d_p
                    is_rotated_suit = True   # people enter staircase from north (floor-plate) face
                else:
                    # Staircase at north edge (inset), lobby south of it (facing floor plate)
                    st_y2      = entry_y - _EDGE_GAP
                    st_base_y  = st_y2 - sd_nat
                    lb_y1, lb_y2 = st_base_y - lobby_d_p, st_base_y
                    is_rotated_suit = False  # people enter staircase from south (floor-plate) face

                st_x1 = entry_x - sw_nat / 2.0
                st_x2 = entry_x + sw_nat / 2.0
                lb_box = [st_x1, lb_y1, st_x2, lb_y2]
                st_box = [st_x1, st_base_y, st_x2, st_y2]
                st_cx  = entry_x
                st_cy  = (st_base_y + st_y2) / 2.0
                st_rect = [st_x1, min(st_base_y, lb_y1), st_x2, max(st_y2, lb_y2)]

            sub_boundaries.append(None)  # no shaft
            sub_boundaries.append({"id": tag + "_Lobby", "rect": lb_box})
            sub_boundaries.append({"id": tag + "_Staircase", "rect": st_box})
            stair_overrides.append(st_base_y)
            core_bounds.append(st_rect)
            _sc_rot_perim = s_set.get("staircase_rotation")
            stair_centers.append((st_cx, st_cy, is_rotated_suit, float(_sc_rot_perim) if _sc_rot_perim is not None else None))

            # Lobby walls — only up to the last floor where this stair is needed
            lobby_tag  = tag + "_LB"
            lb_x1_w, lb_x2_w = lb_box[0], lb_box[2]
            lb_y1_w, lb_y2_w = lb_box[1], lb_box[3]
            for l_idx, lvl in enumerate(_set_levels):
                is_last_lvl = (l_idx == len(_set_levels) - 1)
                if is_last_lvl:
                    lvl_h = overrun_height
                else:
                    lvl_h = _set_levels[l_idx + 1]['elevation'] - lvl['elevation']
                    if lvl_h <= 0:
                        continue
                common = {"level_id": lvl['id'], "height": lvl_h, "type": "AI_Wall_Core"}
                # Skip lobby face shared with staircase to avoid duplicate wall.
                # NS stair: shared face is S (south stair) or N (north stair).
                # EW stair: shared face is E (east stair, lb_x2==st_x1) or W (west stair, lb_x1==st_x2).
                if _is_ew_edge:
                    _perim_skip = "E" if is_east_p else "W"
                else:
                    _perim_skip = "S" if is_south_p else "N"
                if _perim_skip != "W":
                    walls.append({"id": "AI_{}_W_L{}".format(lobby_tag, l_idx + 1),
                                  "start": [lb_x1_w, lb_y1_w, 0], "end": [lb_x1_w, lb_y2_w, 0], **common})
                if _perim_skip != "E":
                    walls.append({"id": "AI_{}_E_L{}".format(lobby_tag, l_idx + 1),
                                  "start": [lb_x2_w, lb_y1_w, 0], "end": [lb_x2_w, lb_y2_w, 0], **common})
                if _perim_skip != "N":
                    walls.append({"id": "AI_{}_N_L{}".format(lobby_tag, l_idx + 1),
                                  "start": [lb_x1_w, lb_y2_w, 0], "end": [lb_x2_w, lb_y2_w, 0], **common})
                if _perim_skip != "S":
                    walls.append({"id": "AI_{}_S_L{}".format(lobby_tag, l_idx + 1),
                                  "start": [lb_x1_w, lb_y1_w, 0], "end": [lb_x2_w, lb_y1_w, 0], **common})
            if _set_levels:
                last_lvl = _set_levels[-1]
                floors.append({
                    "id": "AI_{}_TOPCAP".format(lobby_tag),
                    "level_id": last_lvl['id'],
                    "elevation": last_lvl['elevation'] + overrun_height,
                    "points": [[lb_x1_w, lb_y1_w], [lb_x2_w, lb_y1_w], [lb_x2_w, lb_y2_w], [lb_x1_w, lb_y2_w]]
                })

            # Staircase manifest — truncated to the same level range
            st_man = staircase_logic.generate_staircase_manifest(
                [(st_cx, st_cy)], _set_levels, sw_nat, stair_spec, typical_floor_height_mm,
                lift_core_bounds_mm=None, num_lifts=None, lobby_width=lobby_width,
                compliance_overrides=co,
                base_y_override=st_base_y, rotated_indices=([0] if is_rotated_suit else []),
                stair_idx_offset=stair_global_idx
            )
            stair_global_idx += 1
            walls.extend(st_man.get("walls", []))
            floors.extend(st_man.get("floors", []))
            voids.extend(staircase_logic.get_void_rectangles_mm(
                [(st_cx, st_cy)], sw_nat, sd_nat,
                lift_core_bounds_mm=None, num_lifts=None, lobby_width=lobby_width,
                base_y_override=st_base_y,
                rotated_indices=([0] if is_rotated_suit else [])
            ))

            # ── Door specs for perimeter smoke-stop ─────────────────────────
            # stair_global_idx already incremented above → stair_num = stair_global_idx
            # Use _set_lvl_ids (truncated) so doors only appear on floors the stair serves.
            _sn_p = stair_global_idx
            _set_first_id = _set_lvl_ids[0] if _set_lvl_ids else None
            if is_south_p:
                # Staircase main landing faces NORTH (is_rotated=True), external door on SOUTH wall
                _ext_y = st_base_y          # south wall of staircase = W_Front
                _lby_conn = st_y2           # shared wall between staircase and lobby = W_Back
                door_specs.append({
                    "id": tag + "_Stair_ExtDoor",
                    "position_mm": [st_x1 + 900, _ext_y],
                    "wall_line_mm": [[st_x1, _ext_y], [st_x2, _ext_y]],
                    "levels": [_set_first_id] if _set_first_id else [],
                    "swing_in": False, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "wall_ai_id_map": ({_set_lvl_ids[0]: "AI_Stair_{}_L1_W_Front".format(_sn_p)} if _set_lvl_ids else {}),
                })
                door_specs.append({
                    "id": tag + "_Stair_LobbyDoor",
                    "position_mm": [st_x1 + 900, _lby_conn],
                    "wall_line_mm": [[st_x1, _lby_conn], [st_x2, _lby_conn]],
                    "levels": _set_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "swing_out_level1": True,
                    "wall_ai_id_map": {_set_lvl_ids[k]: "AI_Stair_{}_L{}_W_Back".format(_sn_p, k + 1) for k in range(len(_set_lvl_ids))},
                })
                door_specs.append({
                    "id": tag + "_Lobby_EntryDoor",
                    "position_mm": [st_x1 + 900, lb_y2],
                    "wall_line_mm": [[lb_box[0], lb_y2], [lb_box[2], lb_y2]],
                    "levels": _set_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "swing_out_level1": True,
                    "wall_ai_id_map": {_set_lvl_ids[k]: "AI_SafetySet_{}_LB_N_L{}".format(i + 1, k + 1) for k in range(len(_set_lvl_ids))},
                })
            elif _is_ew_edge:
                # EW perimeter stair: shaft runs E-W, lobby is on the floor-plate side.
                # is_east_p=True  → staircase near east edge, lobby west of stair.
                #   External door on east wall of staircase (st_x2).
                #   Lobby entry door on west wall of lobby (lb_x1, floor-plate side).
                # is_east_p=False → staircase near west edge, lobby east of stair.
                #   External door on west wall of staircase (st_x1).
                #   Lobby entry door on east wall of lobby (lb_x2, floor-plate side).
                _door_y_off = st_y1 + 900      # 900mm from south edge for position
                if is_east_p:
                    # Staircase near EAST edge.  Lobby is WEST of staircase.
                    # lb_x2 == st_x1 (shared wall stair↔lobby, lobby E = stair W).
                    # _perim_skip="E" → lobby E wall omitted; use staircase's W_Left wall instead.
                    _ext_x        = st_x2   # external door: east wall of staircase
                    _lby_conn_x   = lb_x2   # stair↔lobby door: at lobby east = stair west X
                    _entry_x      = lb_x1   # floor-plate entry: west wall of lobby
                    _entry_dir    = "W"     # lobby wall direction for floor-plate entry door
                    _ext_wall     = "AI_Stair_{}_L1_W_Right".format(_sn_p)
                    # Lobby E wall is skipped (shared with stair); door goes on stair W wall.
                    _lby_conn_wall_key = lambda k: "AI_Stair_{}_L{}_W_Left".format(_sn_p, k + 1)
                else:
                    # Staircase near WEST edge.  Lobby is EAST of staircase.
                    # lb_x1 == st_x2 (shared wall stair↔lobby, lobby W = stair E).
                    # _perim_skip="W" → lobby W wall omitted; use staircase's W_Right wall instead.
                    _ext_x        = st_x1   # external door: west wall of staircase
                    _lby_conn_x   = lb_x1   # stair↔lobby door: at lobby west = stair east X
                    _entry_x      = lb_x2   # floor-plate entry: east wall of lobby
                    _entry_dir    = "E"     # lobby wall direction for floor-plate entry door
                    _ext_wall     = "AI_Stair_{}_L1_W_Left".format(_sn_p)
                    # Lobby W wall is skipped (shared with stair); door goes on stair E wall.
                    _lby_conn_wall_key = lambda k: "AI_Stair_{}_L{}_W_Right".format(_sn_p, k + 1)
                _entry_wall_fmt    = "AI_SafetySet_{}_LB_{}_L{{}}".format(i + 1, _entry_dir)
                door_specs.append({
                    "id": tag + "_Stair_ExtDoor",
                    "position_mm": [_ext_x, _door_y_off],
                    "wall_line_mm": [[_ext_x, st_y1], [_ext_x, st_y2_ew]],
                    "levels": [_set_first_id] if _set_first_id else [],
                    "swing_in": False, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "wall_ai_id_map": ({_set_lvl_ids[0]: _ext_wall} if _set_lvl_ids else {}),
                })
                door_specs.append({
                    "id": tag + "_Stair_LobbyDoor",
                    "position_mm": [_lby_conn_x, _door_y_off],
                    "wall_line_mm": [[_lby_conn_x, st_y1], [_lby_conn_x, st_y2_ew]],
                    "levels": _set_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "swing_out_level1": True,
                    "wall_ai_id_map": {_set_lvl_ids[k]: _lby_conn_wall_key(k) for k in range(len(_set_lvl_ids))},
                })
                door_specs.append({
                    "id": tag + "_Lobby_EntryDoor",
                    "position_mm": [_entry_x, _door_y_off],
                    "wall_line_mm": [[_entry_x, lb_box[1]], [_entry_x, lb_box[3]]],
                    "levels": _set_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "swing_out_level1": True,
                    "wall_ai_id_map": {_set_lvl_ids[k]: _entry_wall_fmt.format(k + 1) for k in range(len(_set_lvl_ids))},
                })
            else:
                # NS-north perimeter stair: main landing faces SOUTH (is_rotated=False),
                # external door on NORTH wall, lobby entry on SOUTH wall of lobby.
                _ext_y = st_y2              # north wall of staircase = W_Back
                _lby_conn = st_base_y       # shared wall between staircase and lobby = W_Front
                door_specs.append({
                    "id": tag + "_Stair_ExtDoor",
                    "position_mm": [st_x1 + 900, _ext_y],
                    "wall_line_mm": [[st_x1, _ext_y], [st_x2, _ext_y]],
                    "levels": [_set_first_id] if _set_first_id else [],
                    "swing_in": False, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "wall_ai_id_map": ({_set_lvl_ids[0]: "AI_Stair_{}_L1_W_Back".format(_sn_p)} if _set_lvl_ids else {}),
                })
                door_specs.append({
                    "id": tag + "_Stair_LobbyDoor",
                    "position_mm": [st_x1 + 900, _lby_conn],
                    "wall_line_mm": [[st_x1, _lby_conn], [st_x2, _lby_conn]],
                    "levels": _set_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "swing_out_level1": True,
                    "wall_ai_id_map": {_set_lvl_ids[k]: "AI_Stair_{}_L{}_W_Front".format(_sn_p, k + 1) for k in range(len(_set_lvl_ids))},
                })
                door_specs.append({
                    "id": tag + "_Lobby_EntryDoor",
                    "position_mm": [st_x1 + 900, lb_y1],
                    "wall_line_mm": [[lb_box[0], lb_y1], [lb_box[2], lb_y1]],
                    "levels": _set_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "swing_out_level1": True,
                    "wall_ai_id_map": {_set_lvl_ids[k]: "AI_SafetySet_{}_LB_S_L{}".format(i + 1, k + 1) for k in range(len(_set_lvl_ids))},
                })
            continue  # skip the normal orientation detection below

        # ── Layout engine: index-aware box-driven generation ─────────────────────
        # Look up this set's pre-computed layout from the iterative engine run.
        # Each set gets its own unique layout (different boxes, different side).
        _this_layout = (_placed_layouts[i]
                        if i < len(_placed_layouts) and _placed_layouts[i]
                        else None)
        if _this_layout and not is_perimeter:
            _att       = _this_layout["attach_side"]
            _fl_box_e  = list(_this_layout["fire_lift"])
            _lb_box_e  = list(_this_layout["lobby"])
            _st_box_e  = list(_this_layout["staircase"])

            # Shared-face detection — which LOBBY wall to skip (lobby face touching the shaft).
            # Box coords: [x1, y1, x2, y2] where x1<x2 (W<E), y1<y2 (S<N in Revit).
            # "skip_lf=X" means skip wall X on the LOBBY; shaft door goes on the OPPOSITE face.
            # 25mm tolerance (half a 50mm grid unit) absorbs float drift from _mirror_layout.
            _tol_face = 25.0
            if   abs(_fl_box_e[2] - _lb_box_e[0]) < _tol_face:  _skip_lf = "W"   # shaft east (x2) = lobby west (x1) → skip lobby W wall; shaft door on E
            elif abs(_fl_box_e[0] - _lb_box_e[2]) < _tol_face:  _skip_lf = "E"   # shaft west (x1) = lobby east (x2) → skip lobby E wall; shaft door on W
            elif abs(_fl_box_e[3] - _lb_box_e[1]) < _tol_face:  _skip_lf = "S"   # shaft north (y2) = lobby south (y1) → skip lobby S wall; shaft door on N
            elif abs(_fl_box_e[1] - _lb_box_e[3]) < _tol_face:  _skip_lf = "N"   # shaft south (y1) = lobby north (y2) → skip lobby N wall; shaft door on S
            else:                                                  _skip_lf = None  # chain C: branched

            # Shared-face detection — lobby face touching staircase.
            # skip_st = the LOBBY wall direction to skip (shared with staircase).
            # "staircase east = lobby west" → skip lobby W wall → skip_st="W"
            if   abs(_st_box_e[0] - _lb_box_e[2]) < _tol_face:  _skip_st = "E"   # staircase W (x1) = lobby E (x2) → skip lobby E
            elif abs(_st_box_e[2] - _lb_box_e[0]) < _tol_face:  _skip_st = "W"   # staircase E (x2) = lobby W (x1) → skip lobby W
            elif abs(_st_box_e[1] - _lb_box_e[3]) < _tol_face:  _skip_st = "N"   # staircase S (y1) = lobby N (y2) → skip lobby N
            elif abs(_st_box_e[3] - _lb_box_e[1]) < _tol_face:  _skip_st = "S"   # staircase N (y2) = lobby S (y1) → skip lobby S
            else:                                                  _skip_st = None  # not adjacent

            # Detect ALL shaft and lobby faces shared with the passenger lift anchor (pax bank).
            # Each shared face is skipped — the pax bank's own wall already covers it.
            # A solver result can place the shaft flush with 2 anchor faces simultaneously
            # (e.g. south AND west when shaft sits at the SW corner of the bank).
            _anc = _anchor if _anchor else None
            _skip_fl_anc  = None     # primary single-face skip (legacy, for _fire_lift_shaft_walls)
            _skip_fl_ancs = set()    # full set of skipped shaft faces
            _skip_lb_ancs = set()    # lobby faces coincident with anchor → skip to avoid double wall
            if _anc:
                _ax1, _ay1, _ax2, _ay2 = _anc
                if is_fl:
                    if abs(_fl_box_e[2] - _ax1) < _tol_face:  _skip_fl_ancs.add("E")
                    if abs(_fl_box_e[0] - _ax2) < _tol_face:  _skip_fl_ancs.add("W")
                    if abs(_fl_box_e[3] - _ay1) < _tol_face:  _skip_fl_ancs.add("N")
                    if abs(_fl_box_e[1] - _ay2) < _tol_face:  _skip_fl_ancs.add("S")
                    _skip_fl_anc = next(iter(_skip_fl_ancs), None)  # keep single value for legacy callers
                # Lobby faces that are double-walled by anchor — skip only when the
                # anchor wall ACTUALLY spans the lobby face (overlapping in the parallel axis).
                # Vertical lobby faces (W/E) share an anchor X edge and overlap in Y.
                # Horizontal lobby faces (N/S) share an anchor Y edge and overlap in X.
                _lb_x1, _lb_y1, _lb_x2, _lb_y2 = _lb_box_e
                _y_overlap = (max(_lb_y1, _ay1) < min(_lb_y2, _ay2))  # lobby Y range overlaps anchor Y range
                _x_overlap = (max(_lb_x1, _ax1) < min(_lb_x2, _ax2))  # lobby X range overlaps anchor X range
                if abs(_lb_x1 - _ax1) < _tol_face and _y_overlap:  _skip_lb_ancs.add("W")  # lobby W = anchor W, and Y ranges overlap
                if abs(_lb_x2 - _ax2) < _tol_face and _y_overlap:  _skip_lb_ancs.add("E")  # lobby E = anchor E, and Y ranges overlap
                if abs(_lb_x1 - _ax2) < _tol_face and _y_overlap:  _skip_lb_ancs.add("W")  # lobby W = anchor E (shaft east of anchor)
                if abs(_lb_x2 - _ax1) < _tol_face and _y_overlap:  _skip_lb_ancs.add("E")  # lobby E = anchor W
                if abs(_lb_y1 - _ay1) < _tol_face and _x_overlap:  _skip_lb_ancs.add("S")  # lobby S = anchor S, X overlap
                if abs(_lb_y2 - _ay2) < _tol_face and _x_overlap:  _skip_lb_ancs.add("N")  # lobby N = anchor N, X overlap
                if abs(_lb_y1 - _ay2) < _tol_face and _x_overlap:  _skip_lb_ancs.add("S")  # lobby S = anchor N (lobby above anchor)
                if abs(_lb_y2 - _ay1) < _tol_face and _x_overlap:  _skip_lb_ancs.add("N")  # lobby N = anchor S (lobby below anchor)

            # Fire-lift shaft faces coincident with the staircase — avoid double wall
            # where the staircase box and shaft box share a face (not just a corner touch).
            # Require meaningful overlap (>50mm) in the axis parallel to the shared edge
            # so that corner-touching layouts (NE: FL and staircase meet at a single point)
            # do not falsely skip the FL face that is actually the lobby boundary.
            _min_face_ov = 50.0
            if is_fl:
                _fl_x1, _fl_y1, _fl_x2, _fl_y2 = _fl_box_e
                _st_x1, _st_y1, _st_x2, _st_y2 = _st_box_e
                # E/W shared face: boxes align in Y (vertical face) — check Y overlap
                _yov = max(0.0, min(_fl_y2, _st_y2) - max(_fl_y1, _st_y1))
                if abs(_fl_x2 - _st_x1) < _tol_face and _yov > _min_face_ov:  _skip_fl_ancs.add("E")
                if abs(_fl_x1 - _st_x2) < _tol_face and _yov > _min_face_ov:  _skip_fl_ancs.add("W")
                # N/S shared face: boxes align in X (horizontal face) — check X overlap
                _xov = max(0.0, min(_fl_x2, _st_x2) - max(_fl_x1, _st_x1))
                if abs(_fl_y2 - _st_y1) < _tol_face and _xov > _min_face_ov:  _skip_fl_ancs.add("N")
                if abs(_fl_y1 - _st_y2) < _tol_face and _xov > _min_face_ov:  _skip_fl_ancs.add("S")

            # The FL face that carries the lobby door must NEVER be skipped, even if it
            # coincides with an anchor face.  The door needs a physical wall to sit in.
            # _skip_lf tells which LOBBY wall is absent — the opposite is the FL door face.
            _fl_door_face = {"N": "S", "S": "N", "E": "W", "W": "E"}.get(_skip_lf)
            if _fl_door_face and is_fl:
                _skip_fl_ancs.discard(_fl_door_face)

            if is_fl:
                _skip_fl_anc = next(iter(_skip_fl_ancs), None)

            _fslog("[LayoutEngine] Set {}: fl={} lb={} st={} skip_lf={} skip_st={} skip_fl_anc={} skip_fl_ancs={} skip_lb_ancs={}".format(
                i, tuple(int(v) for v in _fl_box_e), tuple(int(v) for v in _lb_box_e),
                tuple(int(v) for v in _st_box_e), _skip_lf, _skip_st, _skip_fl_anc, _skip_fl_ancs, _skip_lb_ancs))

            # Staircase rotation from solver.
            # OR-Tools stores degrees (0 or 90); legacy engine stores index (0 or 1).
            # Normalise to degrees so all downstream checks use a consistent value.
            _st_rot_raw = int(_this_layout.get("stair_rot", 0))
            _st_rot_from_engine = 90 if _st_rot_raw == 1 else _st_rot_raw
            _st_is_ew = (_st_rot_from_engine == 90)

            # Staircase centre — derived from the exact solver box.
            # enc_w and enc_d are always the natural compliance dimensions (pre-rotation).
            # The generator builds in natural orientation; rotation_degs post-rotates walls;
            # revit_workers._rp() rotates flight XYZ at draw time.
            # base_y_override anchors the generator's south edge to the solver box bottom
            # so the staircase wall is exactly flush with the adjacent module edge.
            # The centre used as the rotation pivot must be the natural-orientation centre
            # (box centre for no-rotation; computed from natural dims when rotated).
            _st_cx_e  = (_st_box_e[0] + _st_box_e[2]) / 2.0
            _st_cy_e  = (_st_box_e[1] + _st_box_e[3]) / 2.0
            # _st_enc_w: used only for door wall_line_mm X coordinates.
            # The generator ignores _enclosure_width_mm and always uses sw_nat internally,
            # so use sw_nat here to match actual wall positions.
            _st_enc_w = sw_nat
            # base_y_override: generator's Y=0 south edge.
            # rot=0: anchor to exact solver box bottom for flush walls.
            # rot=90: generator runs sd_nat in Y; pivot is box centre;
            #   south edge = box centre Y - sw_nat/2 (pre-rot natural depth direction).
            if _st_is_ew:
                _st_base_e = _st_cy_e - sd_nat / 2.0
            else:
                _st_base_e = _st_box_e[1]

            # is_rotated_suit controls the 180°Y-mirror inside generate_staircase_manifest:
            #   False → main landing at W_Front (pre-rotation south, base_y+t)
            #   True  → main landing at W_Back  (pre-rotation north, base_y+enc_d-t)
            # After 90°CCW rotation (EW staircase):
            #   W_Front(S)→east, W_Back(N)→west, W_Left(W)→south, W_Right(E)→north
            # Lobby must always be at the main-landing face.
            # Rule: is_rotated_suit selects the pre-rotation face that points toward the lobby
            # after any post-generation rotation.
            # _skip_st=="N": staircase south (W_Front) = lobby north → False (W_Front faces lobby)
            # _skip_st=="S": staircase north (W_Back) = lobby south → True  (W_Back faces lobby)
            # _skip_st=="E" + EW rot: W_Back(pre-N)→west = lobby west → True
            # _skip_st=="W" + EW rot: W_Front(pre-S)→east = lobby east → False
            if   _skip_st == "N":   is_rotated_suit = False
            elif _skip_st == "S":   is_rotated_suit = True
            elif _skip_st == "E":   is_rotated_suit = True   # EW stair, W_Back→west faces lobby
            elif _skip_st == "W":   is_rotated_suit = False  # EW stair, W_Front→east faces lobby
            else:                   is_rotated_suit = False

            # Bounding rectangle for spatial registry / collision checks
            _all_boxes_e  = ([_fl_box_e] if is_fl else []) + [_lb_box_e, _st_box_e]
            _sr_x1 = min(b[0] for b in _all_boxes_e)
            _sr_y1 = min(b[1] for b in _all_boxes_e)
            _sr_x2 = max(b[2] for b in _all_boxes_e)
            _sr_y2 = max(b[3] for b in _all_boxes_e)
            _st_rect_e = [_sr_x1, _sr_y1, _sr_x2, _sr_y2]

            # Sub-boundary reservations
            if not _skip_set:
                sub_boundaries.append({"id": tag + "_Shaft", "rect": _fl_box_e} if is_fl else None)
                sub_boundaries.append({"id": tag + "_Lobby", "rect": _lb_box_e})
                sub_boundaries.append({"id": tag + "_Staircase", "rect": _st_box_e})
                stair_overrides.append(_st_base_e)
                core_bounds.append(_st_rect_e)
                # stair_rot from solver (0 or 90°) takes priority over any legacy field
                _sc_rot_e = _st_rot_from_engine if _st_rot_from_engine else s_set.get("staircase_rotation")
                stair_centers.append((_st_cx_e, _st_cy_e, is_rotated_suit,
                                      float(_sc_rot_e) if _sc_rot_e is not None else None))

                # ── Fire-lift shaft walls ──────────────────────────────────────
                if is_fl:
                    _fl_cx_e = (_fl_box_e[0] + _fl_box_e[2]) / 2.0
                    _fl_cy_e = (_fl_box_e[1] + _fl_box_e[3]) / 2.0
                    _fl_fw_e = _fl_box_e[2] - _fl_box_e[0]
                    _fl_fd_e = _fl_box_e[3] - _fl_box_e[1]
                    # Shaft keeps walls on all non-shared faces. The lobby skips its shared face
                    # (_skip_lf) to avoid double walls. All anchor-coincident shaft faces are
                    # skipped (_skip_fl_ancs) — pax bank walls already cover those.
                    _ls_walls_e, _ls_floors_e = _fire_lift_shaft_walls(
                        tag + "_FL", _fl_cx_e, _fl_cy_e, _fl_fw_e, _fl_fd_e,
                        levels_data, overrun_height=overrun_height,
                        skip_wall=_skip_fl_anc, skip_walls=_skip_fl_ancs)
                    walls.extend(_ls_walls_e)
                    floors.extend(_ls_floors_e)
                    _t2e = _WALL_THICKNESS / 2.0 + 1.0
                    voids.append((_fl_box_e[0] + _t2e, _fl_box_e[1] + _t2e,
                                  _fl_box_e[2] - _t2e, _fl_box_e[3] - _t2e))

                # ── Lobby walls (4 sides, skip shared faces) ──────────────────
                # Skipped faces: shared with shaft (_skip_lf) and pax-anchor walls
                # coincident with lobby perimeter (_skip_lb_ancs).
                # _skip_st is intentionally NOT applied here — Revit Stairs elements
                # have no explicit enclosure wall, so the lobby wall on the staircase
                # face must be generated to close the enclosure.
                #
                # PARTIAL-SHARE PATCH:
                # _skip_lf only drops the WHOLE lobby face. When the shaft's parallel-
                # axis range is narrower than the lobby's (e.g. lobby S face spans
                # 8000mm but the shaft only covers 3200mm of it), the un-shared
                # portion is left open and the lobby is not enclosed.
                # We compute a stub-segment list per shared face: the portions of the
                # lobby face NOT covered by the shaft, and emit walls for them.
                _ltag_e = tag + "_LB"
                _lx1, _lx2 = _lb_box_e[0], _lb_box_e[2]
                _ly1, _ly2 = _lb_box_e[1], _lb_box_e[3]
                # Stub segments along the shared face: list of (a1, a2) sub-ranges
                # of the lobby face that are NOT covered by the shaft. Each is
                # emitted as a separate wall.
                _stub_W = []  # list of (y1, y2) when _skip_lf == "W"
                _stub_E = []
                _stub_N = []  # list of (x1, x2) when _skip_lf == "N"
                _stub_S = []
                if _skip_lf in ("W", "E"):
                    # Shared face is vertical (lobby W or E). Compare Y ranges.
                    _shaft_y1, _shaft_y2 = _fl_box_e[1], _fl_box_e[3]
                    _gap_lo = max(_ly1, min(_ly2, _shaft_y1))
                    _gap_hi = min(_ly2, max(_ly1, _shaft_y2))
                    _stub_list = _stub_W if _skip_lf == "W" else _stub_E
                    if _gap_lo > _ly1 + 1.0:
                        _stub_list.append((_ly1, _gap_lo))
                    if _gap_hi < _ly2 - 1.0:
                        _stub_list.append((_gap_hi, _ly2))
                elif _skip_lf in ("N", "S"):
                    # Shared face is horizontal (lobby N or S). Compare X ranges.
                    _shaft_x1, _shaft_x2 = _fl_box_e[0], _fl_box_e[2]
                    _gap_lo = max(_lx1, min(_lx2, _shaft_x1))
                    _gap_hi = min(_lx2, max(_lx1, _shaft_x2))
                    _stub_list = _stub_N if _skip_lf == "N" else _stub_S
                    if _gap_lo > _lx1 + 1.0:
                        _stub_list.append((_lx1, _gap_lo))
                    if _gap_hi < _lx2 - 1.0:
                        _stub_list.append((_gap_hi, _lx2))
                if _stub_W or _stub_E or _stub_N or _stub_S:
                    _fslog("[LayoutEngine] Set {}: lobby {} face partially shared "
                           "with shaft → stub segments W={} E={} N={} S={}".format(
                               i, _skip_lf, _stub_W, _stub_E, _stub_N, _stub_S))
                # Stubs only apply when the face is dropped because of _skip_lf
                # (partial shaft share). When the face is dropped via _skip_lb_ancs
                # (pax-anchor coverage), the anchor wall covers it fully — no stub.
                _emit_stubs_W = (_skip_lf == "W" and "W" not in _skip_lb_ancs)
                _emit_stubs_E = (_skip_lf == "E" and "E" not in _skip_lb_ancs)
                _emit_stubs_N = (_skip_lf == "N" and "N" not in _skip_lb_ancs)
                _emit_stubs_S = (_skip_lf == "S" and "S" not in _skip_lb_ancs)
                for _li, _lvl in enumerate(levels_data):
                    _is_last = (_li == len(levels_data) - 1)
                    _lh = overrun_height if _is_last else (
                        levels_data[_li + 1]['elevation'] - _lvl['elevation'])
                    if _lh <= 0:
                        continue
                    _lcommon = {"level_id": _lvl['id'], "height": _lh, "type": "AI_Wall_Core"}
                    if _skip_lf != "W" and "W" not in _skip_lb_ancs:
                        walls.append({"id": "AI_{}_W_L{}".format(_ltag_e, _li + 1),
                                      "start": [_lx1, _ly1, 0], "end": [_lx1, _ly2, 0], **_lcommon})
                    elif _emit_stubs_W:
                        for _si, (_sa, _sb) in enumerate(_stub_W):
                            walls.append({"id": "AI_{}_W{}_L{}".format(_ltag_e, _si, _li + 1),
                                          "start": [_lx1, _sa, 0], "end": [_lx1, _sb, 0], **_lcommon})
                    if _skip_lf != "E" and "E" not in _skip_lb_ancs:
                        walls.append({"id": "AI_{}_E_L{}".format(_ltag_e, _li + 1),
                                      "start": [_lx2, _ly1, 0], "end": [_lx2, _ly2, 0], **_lcommon})
                    elif _emit_stubs_E:
                        for _si, (_sa, _sb) in enumerate(_stub_E):
                            walls.append({"id": "AI_{}_E{}_L{}".format(_ltag_e, _si, _li + 1),
                                          "start": [_lx2, _sa, 0], "end": [_lx2, _sb, 0], **_lcommon})
                    if _skip_lf != "N" and "N" not in _skip_lb_ancs:
                        walls.append({"id": "AI_{}_N_L{}".format(_ltag_e, _li + 1),
                                      "start": [_lx1, _ly2, 0], "end": [_lx2, _ly2, 0], **_lcommon})
                    elif _emit_stubs_N:
                        for _si, (_sa, _sb) in enumerate(_stub_N):
                            walls.append({"id": "AI_{}_N{}_L{}".format(_ltag_e, _si, _li + 1),
                                          "start": [_sa, _ly2, 0], "end": [_sb, _ly2, 0], **_lcommon})
                    if _skip_lf != "S" and "S" not in _skip_lb_ancs:
                        walls.append({"id": "AI_{}_S_L{}".format(_ltag_e, _li + 1),
                                      "start": [_lx1, _ly1, 0], "end": [_lx2, _ly1, 0], **_lcommon})
                    elif _emit_stubs_S:
                        for _si, (_sa, _sb) in enumerate(_stub_S):
                            walls.append({"id": "AI_{}_S{}_L{}".format(_ltag_e, _si, _li + 1),
                                          "start": [_sa, _ly1, 0], "end": [_sb, _ly1, 0], **_lcommon})

                # Lobby topcap
                if levels_data:
                    _last_lvl_e = levels_data[-1]
                    floors.append({
                        "id": "AI_{}_TOPCAP".format(_ltag_e),
                        "level_id": _last_lvl_e['id'],
                        "elevation": _last_lvl_e['elevation'] + overrun_height,
                        "points": [[_lx1, _ly1], [_lx2, _ly1], [_lx2, _ly2], [_lx1, _ly2]]
                    })

                # ── Door specs (geometry-derived, no hardcoded direction strings) ──
                _sn_e = stair_global_idx + 1

                # Which face of each box is "outer" (accessible / external)?
                # Outer face = the lobby face NOT touching the shaft or staircase.
                # Use _skip_st to find the staircase face, then pick the opposite as outer.
                # For N/S attach: staircase is further from pax, corridor access is from pax side.
                # For E/W attach: corridor access is from the N or S face (perpendicular to pax row).
                _opposite = {"N": "S", "S": "N", "E": "W", "W": "E"}
                if _skip_st in ("N", "S", "E", "W"):
                    _lb_outer_face = _opposite[_skip_st]
                else:
                    # Fallback: use attach_side to guess (staircase not detected adjacent)
                    _lb_outer_face = _att
                if _lb_outer_face == "N":
                    _lb_outer_wall = [[_lx1, _ly2], [_lx2, _ly2]]
                    _lb_door_pos   = [(_lx1 + _lx2) / 2.0, _ly2]
                elif _lb_outer_face == "S":
                    _lb_outer_wall = [[_lx1, _ly1], [_lx2, _ly1]]
                    _lb_door_pos   = [(_lx1 + _lx2) / 2.0, _ly1]
                elif _lb_outer_face == "E":
                    _lb_outer_wall = [[_lx2, _ly1], [_lx2, _ly2]]
                    _lb_door_pos   = [_lx2, (_ly1 + _ly2) / 2.0]
                else:  # W
                    _lb_outer_wall = [[_lx1, _ly1], [_lx1, _ly2]]
                    _lb_door_pos   = [_lx1, (_ly1 + _ly2) / 2.0]

                # Staircase door wall coordinates derived from actual generator geometry,
                # not raw solver box corners — ensures door position is exactly at
                # wall centre and wall_line_mm matches the physical wall extent.
                # Generator builds in natural orientation: enc_w in X, sd_nat in Y.
                # For EW (90° CCW rotation), X/Y axes swap after rotation.
                _st_wx1 = _st_cx_e - _st_enc_w / 2.0    # generator west X (pre-rotation)
                _st_wx2 = _st_cx_e + _st_enc_w / 2.0    # generator east X (pre-rotation)
                _st_wy1 = _st_base_e                      # generator south Y (W_Front)
                _st_wy2 = _st_base_e + sd_nat             # generator north Y (W_Back)

                # Staircase door wall coordinates.
                # _skip_st tells which lobby face abuts the staircase → that is the
                # staircase's lobby-facing (inner) side. Outer = far side (fire exit).
                #
                # Wall IDs use pre-rotation names in generate_staircase_manifest:
                #   W_Front = south (base_y),   W_Back = north (base_y+enc_d)
                #   W_Left  = west (base_x),    W_Right = east (base_x+enc_w)
                # After 90°CCW rotation (_st_is_ew=True):
                #   W_Front(pre-S)→east,  W_Back(pre-N)→west
                #   W_Left(pre-W)→south,  W_Right(pre-E)→north
                #
                # is_rotated_suit selects which pre-rotation Y-face is the main landing:
                #   False → W_Front (pre-rotation south) = main landing face
                #   True  → W_Back  (pre-rotation north) = main landing face
                # The lobby-door wall AI ID references the pre-rotation wall name.
                _sx1, _sy1, _sx2, _sy2 = _st_box_e

                if _skip_st == "N":
                    # Staircase north of lobby; lobby north wall = staircase W_Front (south).
                    # is_rotated_suit=False: main landing at W_Front = lobby side. ✓
                    # stair_rot=0 guaranteed (solver forbids E/W touch when rot=0, d>=w).
                    _st_inner_wall     = [[_st_wx1, _st_wy1], [_st_wx2, _st_wy1]]
                    _st_outer_wall     = [[_st_wx1, _st_wy2], [_st_wx2, _st_wy2]]
                    _st_inner_wall_sfx = "W_Front"
                    _st_outer_wall_sfx = "W_Back"
                    _st_lby_pos        = [_st_cx_e, _st_wy1]
                    _st_ext_pos        = [_st_cx_e, _st_wy2]

                elif _skip_st == "S":
                    # Staircase south of lobby; lobby south wall = staircase W_Back (north).
                    # is_rotated_suit=True: main landing at W_Back = lobby side. ✓
                    # stair_rot=0 guaranteed.
                    _st_inner_wall     = [[_st_wx1, _st_wy2], [_st_wx2, _st_wy2]]
                    _st_outer_wall     = [[_st_wx1, _st_wy1], [_st_wx2, _st_wy1]]
                    _st_inner_wall_sfx = "W_Back"
                    _st_outer_wall_sfx = "W_Front"
                    _st_lby_pos        = [_st_cx_e, _st_wy2]
                    _st_ext_pos        = [_st_cx_e, _st_wy1]

                elif _skip_st == "E":
                    # Staircase east of lobby; lobby east wall = staircase physical west face.
                    # stair_rot=90 guaranteed (solver forbids N/S touch when rot=1, d>=w).
                    # After 90°CCW rotation: physical west face = W_Back (pre-rotation north).
                    # is_rotated_suit=True: main landing at W_Back(pre-N) → west physical = lobby. ✓
                    # Physical wall coordinates: vertical line at staircase x1 (west edge).
                    _st_inner_wall     = [[_sx1, _sy1], [_sx1, _sy2]]   # west edge, vertical
                    _st_outer_wall     = [[_sx2, _sy1], [_sx2, _sy2]]   # east edge, vertical
                    _st_inner_wall_sfx = "W_Back"    # pre-rotation name → west physical
                    _st_outer_wall_sfx = "W_Front"   # pre-rotation name → east physical
                    _st_lby_pos        = [_sx1, _st_cy_e]
                    _st_ext_pos        = [_sx2, _st_cy_e]

                elif _skip_st == "W":
                    # Staircase west of lobby; lobby west wall = staircase physical east face.
                    # stair_rot=90 guaranteed.
                    # After 90°CCW rotation: physical east face = W_Front (pre-rotation south).
                    # is_rotated_suit=False: main landing at W_Front(pre-S) → east physical = lobby. ✓
                    _st_inner_wall     = [[_sx2, _sy1], [_sx2, _sy2]]   # east edge, vertical
                    _st_outer_wall     = [[_sx1, _sy1], [_sx1, _sy2]]   # west edge, vertical
                    _st_inner_wall_sfx = "W_Front"   # pre-rotation name → east physical
                    _st_outer_wall_sfx = "W_Back"    # pre-rotation name → west physical
                    _st_lby_pos        = [_sx2, _st_cy_e]
                    _st_ext_pos        = [_sx1, _st_cy_e]

                else:
                    # Staircase not directly adjacent to lobby — default to south as inner
                    _st_inner_wall     = [[_st_wx1, _st_wy1], [_st_wx2, _st_wy1]]
                    _st_outer_wall     = [[_st_wx1, _st_wy2], [_st_wx2, _st_wy2]]
                    _st_inner_wall_sfx = "W_Front"
                    _st_outer_wall_sfx = "W_Back"
                    _st_lby_pos        = [_st_cx_e, _st_wy1]
                    _st_ext_pos        = [_st_cx_e, _st_wy2]

                # Stair external door (ground floor only)
                door_specs.append({
                    "id": tag + "_Stair_ExtDoor",
                    "position_mm": _st_ext_pos,
                    "wall_line_mm": _st_outer_wall,
                    "levels": [_first_lvl_id] if _first_lvl_id else [],
                    "swing_in": False, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf",
                    "wall_ai_id_map": ({_all_lvl_ids[0]: "AI_Stair_{}_L1_{}".format(
                        _sn_e, _st_outer_wall_sfx)} if _all_lvl_ids else {}),
                })
                # Stair lobby door (all floors)
                door_specs.append({
                    "id": tag + "_Stair_LobbyDoor",
                    "position_mm": _st_lby_pos,
                    "wall_line_mm": _st_inner_wall,
                    "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf", "swing_out_level1": True,
                    "wall_ai_id_map": {_all_lvl_ids[k]: "AI_Stair_{}_L{}_{}".format(
                        _sn_e, k + 1, _st_inner_wall_sfx) for k in range(len(_all_lvl_ids))},
                })
                # Lobby entry door (all floors) — on outer face of lobby
                door_specs.append({
                    "id": tag + "_Lobby_EntryDoor",
                    "position_mm": _lb_door_pos,
                    "wall_line_mm": _lb_outer_wall,
                    "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "single_leaf", "swing_out_level1": True,
                    "wall_ai_id_map": {_all_lvl_ids[k]: "AI_{}_{}_L{}".format(
                        _ltag_e, _lb_outer_face, k + 1) for k in range(len(_all_lvl_ids))},
                })
                # Fire lift door — on the lobby-facing face of the shaft.
                # _skip_lf = which LOBBY wall is skipped (the face touching the shaft).
                # The shaft door goes on the OPPOSITE face of the shaft (the one facing the lobby).
                if is_fl:
                    if _skip_lf == "W":    # lobby W skipped = shaft is WEST of lobby → shaft EAST face touches lobby → door on shaft EAST
                        _fl_door_wall = [[_fl_box_e[2], _fl_box_e[1]], [_fl_box_e[2], _fl_box_e[3]]]
                        _fl_door_pos  = [_fl_box_e[2], (_fl_box_e[1] + _fl_box_e[3]) / 2.0]
                        _fl_wall_dir  = "E"
                    elif _skip_lf == "E":  # lobby E skipped = shaft is EAST of lobby → shaft WEST face touches lobby → door on shaft WEST
                        _fl_door_wall = [[_fl_box_e[0], _fl_box_e[1]], [_fl_box_e[0], _fl_box_e[3]]]
                        _fl_door_pos  = [_fl_box_e[0], (_fl_box_e[1] + _fl_box_e[3]) / 2.0]
                        _fl_wall_dir  = "W"
                    elif _skip_lf == "S":  # lobby S skipped = shaft is SOUTH of lobby → shaft NORTH face touches lobby → door on shaft NORTH
                        _fl_door_wall = [[_fl_box_e[0], _fl_box_e[3]], [_fl_box_e[2], _fl_box_e[3]]]
                        _fl_door_pos  = [(_fl_box_e[0] + _fl_box_e[2]) / 2.0, _fl_box_e[3]]
                        _fl_wall_dir  = "N"
                    elif _skip_lf == "N":  # lobby N skipped = shaft is NORTH of lobby → shaft SOUTH face touches lobby → door on shaft SOUTH
                        _fl_door_wall = [[_fl_box_e[0], _fl_box_e[1]], [_fl_box_e[2], _fl_box_e[1]]]
                        _fl_door_pos  = [(_fl_box_e[0] + _fl_box_e[2]) / 2.0, _fl_box_e[1]]
                        _fl_wall_dir  = "S"
                    else:  # chain C branched — derive shaft face from actual lobby position
                        _lb_cx_e = (_lb_box_e[0] + _lb_box_e[2]) / 2.0
                        _lb_cy_e = (_lb_box_e[1] + _lb_box_e[3]) / 2.0
                        _fl_cx_e = (_fl_box_e[0] + _fl_box_e[2]) / 2.0
                        _fl_cy_e = (_fl_box_e[1] + _fl_box_e[3]) / 2.0
                        _cdx = _lb_cx_e - _fl_cx_e
                        _cdy = _lb_cy_e - _fl_cy_e
                        if abs(_cdx) >= abs(_cdy):
                            if _cdx > 0:  # lobby EAST of shaft → door on shaft east face
                                _fl_door_wall = [[_fl_box_e[2], _fl_box_e[1]], [_fl_box_e[2], _fl_box_e[3]]]
                                _fl_door_pos  = [_fl_box_e[2], _fl_cy_e]
                                _fl_wall_dir  = "E"
                            else:         # lobby WEST of shaft → door on shaft west face
                                _fl_door_wall = [[_fl_box_e[0], _fl_box_e[1]], [_fl_box_e[0], _fl_box_e[3]]]
                                _fl_door_pos  = [_fl_box_e[0], _fl_cy_e]
                                _fl_wall_dir  = "W"
                        else:
                            if _cdy > 0:  # lobby NORTH of shaft → door on shaft north face
                                _fl_door_wall = [[_fl_box_e[0], _fl_box_e[3]], [_fl_box_e[2], _fl_box_e[3]]]
                                _fl_door_pos  = [_fl_cx_e, _fl_box_e[3]]
                                _fl_wall_dir  = "N"
                            else:         # lobby SOUTH of shaft → door on shaft south face
                                _fl_door_wall = [[_fl_box_e[0], _fl_box_e[1]], [_fl_box_e[2], _fl_box_e[1]]]
                                _fl_door_pos  = [_fl_cx_e, _fl_box_e[1]]
                                _fl_wall_dir  = "S"
                    door_specs.append({
                        "id": tag + "_FireLift_Door",
                        "position_mm": _fl_door_pos,
                        "wall_line_mm": _fl_door_wall,
                        "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "lift",
                        "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_FL_{}_L{}".format(
                            i + 1, _fl_wall_dir, k + 1) for k in range(len(_all_lvl_ids))},
                    })

                # ── Staircase manifest ─────────────────────────────────────────
                # lift_core_bounds_mm=None: avoid the T-junction pullback inside the
                # generator (staircase is adjacent to anchor, not beside it — the
                # pullback shortens side walls incorrectly).  Anchor-adjacent staircase
                # walls are filtered below after generation.
                _st_man_e = staircase_logic.generate_staircase_manifest(
                    [(_st_cx_e, _st_cy_e)], levels_data, _st_enc_w, stair_spec,
                    typical_floor_height_mm,
                    lift_core_bounds_mm=None, num_lifts=None, lobby_width=lobby_width,
                    base_y_override=_st_base_e,
                    rotated_indices=([0] if is_rotated_suit else []),
                    stair_idx_offset=stair_global_idx, compliance_overrides=co,
                    rotation_degs=([90.0] if _st_is_ew else None),
                )
                stair_global_idx += 1
                # Filter out staircase walls that coincide with the anchor (pax bank) face —
                # the pax bank already has its own wall there; a duplicate causes Revit join errors.
                # Walls that only partially overlap are trimmed to the non-overlapping portion.
                # For EW-rotated staircases the generator produces pre-rotation walls; after
                # 90°CCW rotation, a pre-rotation vertical wall at x=X maps to physical y = st_cy_e+(X-st_cx_e).
                # So we need to filter pre-rotation vertical walls whose rotated-Y hits anchor N/S faces.
                _st_walls_raw = _st_man_e.get("walls", [])
                if _anchor:
                    _ax1, _ay1, _ax2, _ay2 = _anchor
                    _tol_w = _WALL_THICKNESS / 2.0 + 5.0  # half-wall + 5mm tolerance
                    _filtered_st_walls = []
                    for _sw in _st_walls_raw:
                        _ss, _se = _sw.get("start", []), _sw.get("end", [])
                        if len(_ss) < 2 or len(_se) < 2:
                            _filtered_st_walls.append(_sw)
                            continue
                        _skip_this = False
                        # ── NS staircase (no rotation): check pre-rotation coords directly ──
                        # Horizontal wall (constant Y) at an anchor Y boundary?
                        if abs(_ss[1] - _se[1]) < 1:
                            _phy_y = _ss[1]
                            if abs(_phy_y - _ay1) < _tol_w or abs(_phy_y - _ay2) < _tol_w:
                                _wx1, _wx2 = min(_ss[0], _se[0]), max(_ss[0], _se[0])
                                _ov1 = max(_wx1, _ax1 - _tol_w)
                                _ov2 = min(_wx2, _ax2 + _tol_w)
                                if _ov1 < _ov2:
                                    if _wx1 < _ov1 - 50:  # keep non-overlapping left stub
                                        _sw2 = dict(_sw)
                                        _sw2["start"] = [_wx1, _ss[1]] + list(_ss[2:])
                                        _sw2["end"]   = [_ov1, _se[1]] + list(_se[2:])
                                        _filtered_st_walls.append(_sw2)
                                    _skip_this = True
                        # Vertical wall (constant X) at an anchor X boundary?
                        elif abs(_ss[0] - _se[0]) < 1:
                            _phy_x = _ss[0]
                            if abs(_phy_x - _ax1) < _tol_w or abs(_phy_x - _ax2) < _tol_w:
                                _wy1, _wy2 = min(_ss[1], _se[1]), max(_ss[1], _se[1])
                                _ov1 = max(_wy1, _ay1 - _tol_w)
                                _ov2 = min(_wy2, _ay2 + _tol_w)
                                if _ov1 < _ov2:
                                    if _wy1 < _ov1 - 50:
                                        _sw2 = dict(_sw)
                                        _sw2["start"] = [_ss[0], _wy1] + list(_ss[2:])
                                        _sw2["end"]   = [_se[0], _ov1] + list(_se[2:])
                                        _filtered_st_walls.append(_sw2)
                                    _skip_this = True
                            # ── EW staircase (90° rotation): pre-rotation vertical wall at x=X
                            # becomes physical horizontal wall at y = _st_cy_e + (X - _st_cx_e).
                            # Check if that physical Y lands on an anchor N/S boundary.
                            if not _skip_this and _st_is_ew:
                                _phy_y_rot = _st_cy_e + (_phy_x - _st_cx_e)
                                if abs(_phy_y_rot - _ay1) < _tol_w or abs(_phy_y_rot - _ay2) < _tol_w:
                                    # In rotated space, this wall's Y extent maps to X.
                                    # Pre-rotation Y extent [_wy1, _wy2] → physical X [_st_cx_e-(_wy2-_st_cy_e), ...]
                                    _wy1, _wy2 = min(_ss[1], _se[1]), max(_ss[1], _se[1])
                                    # Physical X after rotation: x_phy = _st_cx_e - (y_pre - _st_cy_e)
                                    _px1 = _st_cx_e - (_wy2 - _st_cy_e)
                                    _px2 = _st_cx_e - (_wy1 - _st_cy_e)
                                    _ov1 = max(min(_px1, _px2), _ax1 - _tol_w)
                                    _ov2 = min(max(_px1, _px2), _ax2 + _tol_w)
                                    if _ov1 < _ov2:
                                        _skip_this = True  # fully inside anchor span — skip
                        if not _skip_this:
                            _filtered_st_walls.append(_sw)
                    walls.extend(_filtered_st_walls)
                else:
                    walls.extend(_st_walls_raw)
                floors.extend(_st_man_e.get("floors", []))
                # Derive void from the solver box directly — rotation-invariant.
                _t2e = _WALL_THICKNESS / 2.0 + 1.0
                voids.append((_st_box_e[0] + _t2e, _st_box_e[1] + _t2e,
                               _st_box_e[2] - _t2e, _st_box_e[3] - _t2e))

            continue  # skip hardcoded EW/NS arithmetic

        # ── Orientation detection ────────────────────────────────────────────
        # EW entry: entry_x is at l_xmax or l_xmin; entry_y is near lift_core_cy
        _lift_cy_tol = (l_ymax - l_ymin) / 2.0 + 10
        is_east_ew = lift_core_bounds_mm and (entry_x >= l_xmax - 10) and (abs(entry_y - lift_core_cy) <= _lift_cy_tol)
        is_west_ew = lift_core_bounds_mm and (entry_x <= l_xmin + 10) and (abs(entry_y - lift_core_cy) <= _lift_cy_tol)
        is_ew      = is_east_ew or is_west_ew

        # NS orientation: entry_y at l_ymin (south) or l_ymax (north)
        is_south    = (not is_ew) and (entry_y <= l_ymin + 10)
        use_parallel = False  # set True only in NS parallel branch

        if is_ew:
            # ── EW layout (row-aligned, compact rectangular block) ───────────
            # Fire lift + lobby extend outward in X from the passenger lift row
            # end.  The staircase extends in Y (N or S) centred on the COMBINED
            # fire-lift + lobby X span so the entire set forms a compact
            # rectangle rather than an L-shape.
            fl_dx  = ew_fl_dx if is_fl else 0
            fl_Y_h = fl_shaft_d / 2.0  # fire-lift Y half-extent = pax lift shaft (Rule 3)

            if is_east_ew:
                fl_x1, fl_x2 = l_xmax,           l_xmax + fl_dx
                lb_x1, lb_x2 = l_xmax + fl_dx,   l_xmax + fl_dx + ew_lb_dx
            else:  # west
                fl_x1, fl_x2 = l_xmin - fl_dx,              l_xmin
                lb_x1, lb_x2 = l_xmin - fl_dx - ew_lb_dx,   l_xmin - fl_dx

            fl_box = [fl_x1, entry_y - fl_Y_h, fl_x2, entry_y + fl_Y_h] if is_fl else None
            lb_box = [lb_x1, entry_y - fl_Y_h, lb_x2, entry_y + fl_Y_h]

            # ── Compact rectangle fix ─────────────────────────────────────────
            # Centre the staircase on the COMBINED fire-lift + lobby X extent.
            # If the staircase is wider than FL+LB, extend the lobby so it still
            # fits — preventing any protrusion that would create an L-shape.
            combined_x1 = fl_x1 if is_east_ew else lb_x1
            combined_x2 = lb_x2 if is_east_ew else fl_x2
            combined_span = combined_x2 - combined_x1  # fl_dx + ew_lb_dx

            if combined_span < sw_nat:
                # Widen the lobby just enough so staircase sits entirely inside
                extra = sw_nat - combined_span
                if is_east_ew:
                    lb_x2 += extra
                    combined_x2 = lb_x2
                else:
                    lb_x1 -= extra
                    combined_x1 = lb_x1
                lb_box = [lb_x1, entry_y - fl_Y_h, lb_x2, entry_y + fl_Y_h]

            combined_cx = (combined_x1 + combined_x2) / 2.0
            st_x1_c = combined_cx - sw_nat / 2.0
            st_x2_c = combined_cx + sw_nat / 2.0

            if entry_y >= lift_core_cy:
                # North-side fire lift → staircase extends north from FL+LB north face.
                # Standard (non-rotated): main landing at staircase south = lobby north ✓
                st_y1 = entry_y + fl_Y_h
                st_y2 = entry_y + fl_Y_h + sd_nat
                is_rotated_suit = False
            else:
                # South-side fire lift → staircase extends south from FL+LB south face.
                # Rotated: main landing at staircase north = lobby south ✓
                st_y1 = entry_y - fl_Y_h - sd_nat
                st_y2 = entry_y - fl_Y_h
                is_rotated_suit = True

            # Void/footprint check: if the staircase lands inside a courtyard void,
            # flip it to the opposite Y side of the FL+LB block.
            if footprint_holes:
                from revit_mcp.staircase_logic import _point_in_polygon as _pip_ew
                _ew_st_corners = [
                    (st_x1_c, st_y1), (st_x2_c, st_y1),
                    (st_x2_c, st_y2), (st_x1_c, st_y2),
                ]
                _in_hole = any(
                    _pip_ew(cx, cy, _h)
                    for _h in footprint_holes
                    for cx, cy in _ew_st_corners
                )
                if _in_hole:
                    # Flip: if was going north, now go south (and vice versa)
                    if not is_rotated_suit:
                        st_y1 = entry_y - fl_Y_h - sd_nat
                        st_y2 = entry_y - fl_Y_h
                        is_rotated_suit = True
                    else:
                        st_y1 = entry_y + fl_Y_h
                        st_y2 = entry_y + fl_Y_h + sd_nat
                        is_rotated_suit = False

            st_box    = [st_x1_c, st_y1, st_x2_c, st_y2]
            st_cx     = (st_x1_c + st_x2_c) / 2.0
            st_cy     = (st_y1 + st_y2) / 2.0
            st_base_y = st_y1

            # Footprint polygon check for EW hardcoded path.
            # Mirrors the NS sequential path (see ~line 2196): if any corner of
            # fl_box, lb_box, or st_box falls outside the outer footprint polygon
            # (or inside a courtyard hole), suppress this safety set entirely.
            # Without this, clusters are silently placed outside the building for
            # irregular shapes (U/L/T) when the hardcoded fallback is used.
            if footprint_pts and len(footprint_pts) >= 3:
                from revit_mcp.staircase_logic import _point_in_polygon as _pip_ew2
                _ew_all = ([fl_box] if fl_box else []) + [lb_box, st_box]
                _ew_corners = [pt for b in _ew_all
                               for pt in [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]]
                _ew_in_hole = footprint_holes and any(
                    _pip_ew2(cx, cy, _h) for _h in footprint_holes for cx, cy in _ew_corners)
                if not all(_pip_ew2(cx, cy, footprint_pts) for cx, cy in _ew_corners) or _ew_in_hole:
                    _skip_set = True

            # Bounding rect: proper rectangle (staircase X contained within FL+LB X)
            set_x1 = combined_x1
            set_x2 = combined_x2
            set_y1 = min(entry_y - fl_Y_h, st_y1)
            set_y2 = max(entry_y + fl_Y_h, st_y2)
            st_rect = [set_x1, set_y1, set_x2, set_y2]

            if _skip_set:
                continue  # skip door/wall generation for out-of-footprint EW cluster

            # ── Door specs: EW layout ────────────────────────────────────────
            # EW geometry:
            #   fl_box  = fire lift shaft  (extends in X from lift bank end)
            #   lb_box  = fire lift lobby  (further in X from shaft)
            #   st_box  = staircase        (extends in Y from combined FL+LB block)
            # For east cluster:  outer X face = lb_x2 (east);  inner X = fl_x1 (at lift bank)
            # For west cluster:  outer X face = lb_x1 (west);  inner X = fl_x2 (at lift bank)
            # Staircase is above (north, is_rotated_suit=False) or below (south, =True) the FL+LB block.
            # Stair outer Y face = st_y2 (north) or st_y1 (south)
            # Stair inner Y face = st_y1 (north) or st_y2 (south) = FL+LB north/south face
            _sn_ew = stair_global_idx + 1
            _fl_cy_ew = entry_y          # fire-lift Y centre = entry_y
            _outer_x = lb_x2 if is_east_ew else lb_x1   # outermost X of lobby block
            _outer_st_y = st_y2 if not is_rotated_suit else st_y1   # outer face of staircase
            _inner_st_y = st_y1 if not is_rotated_suit else st_y2   # inner face (=FL+LB N/S face)
            # Wall ID suffixes: stair walls are W_Front (outer) / W_Back (inner) for non-rotated
            _stair_outer_wall_suffix = "W_Back" if is_rotated_suit else "W_Front"
            _stair_inner_wall_suffix = "W_Front" if is_rotated_suit else "W_Back"
            # Stair outer wall X range = st_x1_c..st_x2_c; inner wall same X range
            door_specs.append({
                "id": tag + "_Stair_ExtDoor",
                "position_mm": [(st_x1_c + st_x2_c) / 2.0, _outer_st_y],
                "wall_line_mm": [[st_x1_c, _outer_st_y], [st_x2_c, _outer_st_y]],
                "levels": [_first_lvl_id] if _first_lvl_id else [],
                "swing_in": False, "flip_hand": True, "min_width_mm": 1000,
                "door_category": "single_leaf",
                "wall_ai_id_map": ({_all_lvl_ids[0]: "AI_Stair_{}_L1_{}".format(_sn_ew, _stair_outer_wall_suffix)} if _all_lvl_ids else {}),
            })
            door_specs.append({
                "id": tag + "_Stair_LobbyDoor",
                "position_mm": [(st_x1_c + st_x2_c) / 2.0, _inner_st_y],
                "wall_line_mm": [[st_x1_c, _inner_st_y], [st_x2_c, _inner_st_y]],
                "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                "door_category": "single_leaf", "swing_out_level1": True,
                "wall_ai_id_map": {_all_lvl_ids[k]: "AI_Stair_{}_L{}_{}".format(_sn_ew, k + 1, _stair_inner_wall_suffix) for k in range(len(_all_lvl_ids))},
            })
            # Lobby entry door: on the outer X face of the lobby (accessible from open floor plate)
            door_specs.append({
                "id": tag + "_Lobby_EntryDoor",
                "position_mm": [_outer_x, _fl_cy_ew],
                "wall_line_mm": [[_outer_x, entry_y - fl_Y_h], [_outer_x, entry_y + fl_Y_h]],
                "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                "door_category": "single_leaf", "swing_out_level1": True,
                "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_LB_{}_L{}".format(
                    i + 1, "E" if is_east_ew else "W", k + 1) for k in range(len(_all_lvl_ids))},
            })
            if is_fl and fl_box:
                # Fire lift door: on the outer X face of the fire lift shaft
                _fl_outer_x = fl_x2 if is_east_ew else fl_x1
                door_specs.append({
                    "id": tag + "_FireLift_Door",
                    "position_mm": [_fl_outer_x, _fl_cy_ew],
                    "wall_line_mm": [[_fl_outer_x, fl_box[1]], [_fl_outer_x, fl_box[3]]],
                    "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "lift",
                    "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_FL_{}_L{}".format(
                        i + 1, "E" if is_east_ew else "W", k + 1) for k in range(len(_all_lvl_ids))},
                })

        else:
            # ── NS PARALLEL layout ───────────────────────────────────────────
            # Staircase and fire zone (shaft + lobby) placed SIDE-BY-SIDE,
            # both extending the same cluster depth away from the lift bank.
            #
            # Plan view (south cluster example):
            #
            #   lift bank Y face (l_ymin)
            #   ┌──────────────────────┬───────────────┐
            #   │   Fire Lift Shaft    │               │
            #   │   (inner, 2900mm D)  │  Staircase    │
            #   ├──────────────────────┤  (sw_nat wide)│
            #   │   Fire Lift Lobby    │               │
            #   │   (outer, 2000mm D)  │               │
            #   └──────────────────────┴───────────────┘
            #   (open floor-plate side — lobby accessed from here)
            #
            # Pinwheel arrangement:
            #   South → fire zone WEST, staircase EAST
            #   North → fire zone EAST, staircase WEST
            #
            # Fire zone width = fl_shaft_d; lobby same width.
            # Cluster depth   = fl_shaft_d + lobby_d (= fl_shaft_d + max(2000, remaining))
            # Total X         = fl_shaft_d + sw_nat  (must fit inside lift bank)
            # Fallback to sequential stack if lift bank too narrow.

            lift_bank_w  = l_xmax - l_xmin
            fz_w         = fl_shaft_d                             # fire-zone X width = shaft width
            total_pair_w = fz_w + sw_nat                          # total X needed
            use_parallel = (total_pair_w <= lift_bank_w + 1)      # fits with 1mm tolerance
            # Cluster directive override from lifts.clusters
            if s_set.get("arrangement") == "parallel":
                use_parallel = True
            elif s_set.get("arrangement") == "series":
                use_parallel = False

            # Cluster depth: shaft (inner) + lobby (outer).
            # Lobby must satisfy minimum depth from RAG AND minimum area from RAG.
            fl_shaft_d_p = fl_shaft_d
            _fire_lb_min_d = co.get("fire_lobby_min_depth_mm", 2000)
            _fire_lb_min_l = co.get("fire_lobby_min_length_mm", 3200)
            _lb_net_w_p    = fl_shaft_d - 2 * t         # lobby net width (shaft width - 2 walls)
            _lb_d_from_area = int(math.ceil(fire_lb_area / max(_lb_net_w_p, 1))) + t
            lobby_d_p    = max(_fire_lb_min_d, _fire_lb_min_l, _lb_d_from_area, sd_nat - fl_shaft_d_p)
            cluster_d_p  = fl_shaft_d_p + lobby_d_p

            if not use_parallel:
                # ── Fallback: original sequential NS stack ───────────────────
                fire_lift_d = fl_shaft_d if is_fl else 0
                lobby_d     = total_set_d - sd_nat - fire_lift_d
                if not is_fl:
                    lobby_d = total_set_d - sd_nat
                sw_h  = sw_nat / 2.0
                fl_hw = fl_shaft_d / 2.0
                st_cx = entry_x
                if is_south:
                    fl_box    = [entry_x - fl_hw, entry_y - fire_lift_d, entry_x + fl_hw, entry_y] if is_fl else None
                    lb_box    = [entry_x - sw_h, entry_y - fire_lift_d - lobby_d, entry_x + sw_h, entry_y - fire_lift_d]
                    st_box    = [entry_x - sw_h, entry_y - fire_lift_d - lobby_d - sd_nat, entry_x + sw_h, entry_y - fire_lift_d - lobby_d]
                    st_cy     = (st_box[1] + st_box[3]) / 2.0
                    st_base_y = st_box[1]
                    st_rect   = [entry_x - sw_h, st_box[1], entry_x + sw_h, entry_y]
                    # Polygon check: if south cluster falls outside floor plate (e.g. U-shape notch) flip north
                    if footprint_pts and len(footprint_pts) >= 3:
                        from revit_mcp.staircase_logic import _point_in_polygon as _pip_sq
                        _sq_all = ([fl_box] if fl_box else []) + [lb_box, st_box]
                        _sq_corners = [pt for b in _sq_all
                                       for pt in [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]]
                        _sq_in_hole = footprint_holes and any(
                            _pip_sq(cx, cy, _h) for _h in footprint_holes for cx, cy in _sq_corners)
                        if not all(_pip_sq(cx, cy, footprint_pts) for cx, cy in _sq_corners) or _sq_in_hole:
                            is_south = False
                            _ny = l_ymax
                            fl_box    = [entry_x - fl_hw, _ny, entry_x + fl_hw, _ny + fire_lift_d] if is_fl else None
                            lb_box    = [entry_x - sw_h, _ny + fire_lift_d, entry_x + sw_h, _ny + fire_lift_d + lobby_d]
                            st_box    = [entry_x - sw_h, _ny + fire_lift_d + lobby_d, entry_x + sw_h, _ny + fire_lift_d + lobby_d + sd_nat]
                            st_cy     = (st_box[1] + st_box[3]) / 2.0
                            st_base_y = st_box[1]
                            st_rect   = [entry_x - sw_h, _ny, entry_x + sw_h, st_box[3]]
                            # Both sides failed — suppress this safety set
                            _sq_n_all = ([fl_box] if fl_box else []) + [lb_box, st_box]
                            _sq_n_corners = [pt for b in _sq_n_all
                                             for pt in [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]]
                            _sq_n_in_hole = footprint_holes and any(
                                _pip_sq(cx, cy, _h) for _h in footprint_holes for cx, cy in _sq_n_corners)
                            if not all(_pip_sq(cx, cy, footprint_pts) for cx, cy in _sq_n_corners) or _sq_n_in_hole:
                                _skip_set = True
                else:
                    fl_box    = [entry_x - fl_hw, entry_y, entry_x + fl_hw, entry_y + fire_lift_d] if is_fl else None
                    lb_box    = [entry_x - sw_h, entry_y + fire_lift_d, entry_x + sw_h, entry_y + fire_lift_d + lobby_d]
                    st_box    = [entry_x - sw_h, entry_y + fire_lift_d + lobby_d, entry_x + sw_h, entry_y + fire_lift_d + lobby_d + sd_nat]
                    st_cy     = (st_box[1] + st_box[3]) / 2.0
                    st_base_y = st_box[1]
                    st_rect   = [entry_x - sw_h, entry_y, entry_x + sw_h, st_box[3]]
                    # Polygon check: if north cluster also outside floor plate
                    if footprint_pts and len(footprint_pts) >= 3:
                        from revit_mcp.staircase_logic import _point_in_polygon as _pip_sq
                        _sq_n_all = ([fl_box] if fl_box else []) + [lb_box, st_box]
                        _sq_n_corners = [pt for b in _sq_n_all
                                         for pt in [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]]
                        _sq_n_in_hole = footprint_holes and any(
                            _pip_sq(cx, cy, _h) for _h in footprint_holes for cx, cy in _sq_n_corners)
                        if not all(_pip_sq(cx, cy, footprint_pts) for cx, cy in _sq_n_corners) or _sq_n_in_hole:
                            _skip_set = True
                is_rotated_suit = is_south
                # ── Door specs: NS sequential ────────────────────────────────
                # stair_global_idx not yet incremented → stair_num = stair_global_idx + 1
                _sn_seq = stair_global_idx + 1
                if is_south:
                    # Staircase main landing faces NORTH (is_rotated=True)
                    # ExtDoor on south face (W_Front); LobbyDoor on north face (W_Back)
                    # Doors tucked 900mm from west end (= t + half-door + 50mm clearance)
                    # FireLiftDoor centered on fire shaft (issue 1)
                    _fl_cx_s = (fl_box[0] + fl_box[2]) / 2.0 if fl_box else (lb_box[0] + lb_box[2]) / 2.0
                    door_specs.append({
                        "id": tag + "_Stair_ExtDoor",
                        "position_mm": [st_box[0] + 900, st_box[1]],
                        "wall_line_mm": [[st_box[0], st_box[1]], [st_box[2], st_box[1]]],
                        "levels": [_first_lvl_id] if _first_lvl_id else [],
                        "swing_in": False, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf",
                        "wall_ai_id_map": ({_all_lvl_ids[0]: "AI_Stair_{}_L1_W_Front".format(_sn_seq)} if _all_lvl_ids else {}),
                    })
                    door_specs.append({
                        "id": tag + "_Stair_LobbyDoor",
                        "position_mm": [st_box[0] + 900, st_box[3]],
                        "wall_line_mm": [[st_box[0], st_box[3]], [st_box[2], st_box[3]]],
                        "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf", "swing_out_level1": True,
                        "wall_ai_id_map": {_all_lvl_ids[k]: "AI_Stair_{}_L{}_W_Back".format(_sn_seq, k + 1) for k in range(len(_all_lvl_ids))},
                    })
                    # Lobby entry from office floor: on EAST wall (side accessible from open floor plate)
                    # tucked 900mm from south end so hinge is near the staircase perpendicular wall
                    door_specs.append({
                        "id": tag + "_Lobby_EntryDoor",
                        "position_mm": [lb_box[2], lb_box[1] + 900],
                        "wall_line_mm": [[lb_box[2], lb_box[1]], [lb_box[2], lb_box[3]]],
                        "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf", "swing_out_level1": True,
                        "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_LB_E_L{}".format(i + 1, k + 1) for k in range(len(_all_lvl_ids))},
                    })
                    if is_fl and fl_box:
                        door_specs.append({
                            "id": tag + "_FireLift_Door",
                            "position_mm": [_fl_cx_s, fl_box[1]],
                            "wall_line_mm": [[fl_box[0], fl_box[1]], [fl_box[2], fl_box[1]]],
                            "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                            "door_category": "lift",
                            "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_FL_S_L{}".format(i + 1, k + 1) for k in range(len(_all_lvl_ids))},
                        })
                else:
                    # Main landing faces SOUTH (is_rotated=False)
                    # ExtDoor on north face (W_Back); LobbyDoor on south face (W_Front)
                    _fl_cx_n = (fl_box[0] + fl_box[2]) / 2.0 if fl_box else (lb_box[0] + lb_box[2]) / 2.0
                    door_specs.append({
                        "id": tag + "_Stair_ExtDoor",
                        "position_mm": [st_box[0] + 900, st_box[3]],
                        "wall_line_mm": [[st_box[0], st_box[3]], [st_box[2], st_box[3]]],
                        "levels": [_first_lvl_id] if _first_lvl_id else [],
                        "swing_in": False, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf",
                        "wall_ai_id_map": ({_all_lvl_ids[0]: "AI_Stair_{}_L1_W_Back".format(_sn_seq)} if _all_lvl_ids else {}),
                    })
                    door_specs.append({
                        "id": tag + "_Stair_LobbyDoor",
                        "position_mm": [st_box[0] + 900, st_box[1]],
                        "wall_line_mm": [[st_box[0], st_box[1]], [st_box[2], st_box[1]]],
                        "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf", "swing_out_level1": True,
                        "wall_ai_id_map": {_all_lvl_ids[k]: "AI_Stair_{}_L{}_W_Front".format(_sn_seq, k + 1) for k in range(len(_all_lvl_ids))},
                    })
                    # Lobby entry from office floor: on EAST wall (side accessible from open floor plate)
                    # tucked 900mm from north end so hinge is near the staircase perpendicular wall
                    door_specs.append({
                        "id": tag + "_Lobby_EntryDoor",
                        "position_mm": [lb_box[2], lb_box[3] - 900],
                        "wall_line_mm": [[lb_box[2], lb_box[1]], [lb_box[2], lb_box[3]]],
                        "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf", "swing_out_level1": True,
                        "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_LB_E_L{}".format(i + 1, k + 1) for k in range(len(_all_lvl_ids))},
                    })
                    if is_fl and fl_box:
                        door_specs.append({
                            "id": tag + "_FireLift_Door",
                            "position_mm": [_fl_cx_n, fl_box[3]],
                            "wall_line_mm": [[fl_box[0], fl_box[3]], [fl_box[2], fl_box[3]]],
                            "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                            "door_category": "lift",
                            "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_FL_N_L{}".format(i + 1, k + 1) for k in range(len(_all_lvl_ids))},
                        })
            else:
                # ── Parallel layout ──────────────────────────────────────────
                use_parallel = True

                # Expand fire zone X to fill the full lift bank width.
                # The staircase keeps its code-minimum width (sw_nat); the fire
                # zone (shaft + lobby together) takes the remaining bank width.
                # This eliminates the gap that occurs when sw_nat + fz_w < bank_w.
                lift_bank_w = l_xmax - l_xmin  # noqa: used for layout arithmetic below

                if is_south:
                    # South cluster: extends southward from l_ymin
                    # Fire zone WEST, staircase EAST (pinwheel).
                    # Fire zone = shaft (fl_shaft_d wide) + lobby (remainder to staircase).
                    # Together they fill the full lift bank width so no gap remains.
                    fz_x1   = l_xmin
                    fl_x2_s = l_xmin + fl_shaft_d_p          # shaft east edge (code-min width)
                    st_x1   = l_xmax - sw_nat                 # staircase anchored to east wall
                    st_x2   = l_xmax
                    fz_x2   = st_x1                           # fire zone fills to staircase west edge

                    # Fire shaft at inner Y (adjacent to lift bank), lobby at outer Y
                    fl_y1 = l_ymin - fl_shaft_d_p;  fl_y2 = l_ymin
                    lb_y1 = l_ymin - cluster_d_p;   lb_y2 = fl_y1
                    fl_box = [fz_x1, fl_y1, fl_x2_s, fl_y2] if is_fl else None
                    lb_box = [fz_x1, lb_y1, fz_x2, lb_y2]   # lobby spans full fire zone X

                    st_box    = [st_x1, l_ymin - cluster_d_p, st_x2, l_ymin]
                    st_cx     = (st_x1 + st_x2) / 2.0
                    st_cy     = (st_box[1] + st_box[3]) / 2.0
                    st_base_y = st_box[1]     # south face = outer (floor plate side)
                    is_rotated_suit = False   # people enter from south face, flights go north
                    st_rect   = [l_xmin, l_ymin - cluster_d_p, l_xmax, l_ymin]
                    # Polygon check: if south cluster falls outside floor plate (e.g. U/H notch) flip north
                    if footprint_pts and len(footprint_pts) >= 3:
                        from revit_mcp.staircase_logic import _point_in_polygon as _pip_p
                        _p_all = ([fl_box] if fl_box else []) + [lb_box, st_box]
                        _p_corners = [pt for b in _p_all
                                      for pt in [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]]
                        _p_in_hole = footprint_holes and any(
                            _pip_p(cx, cy, _h) for _h in footprint_holes for cx, cy in _p_corners)
                        if not all(_pip_p(cx, cy, footprint_pts) for cx, cy in _p_corners) or _p_in_hole:
                            is_south = False
                            # Flip north: staircase WEST, fire zone EAST
                            st_x1 = l_xmin;  st_x2 = l_xmin + sw_nat
                            fl_x2_s = l_xmax  # shaft on east side
                            fz_x1 = st_x2;   fz_x2 = l_xmax
                            fl_x1_n = l_xmax - fl_shaft_d_p  # shaft west edge
                            fl_y1 = l_ymax;  fl_y2 = l_ymax + fl_shaft_d_p
                            lb_y1 = fl_y2;   lb_y2 = l_ymax + cluster_d_p
                            fl_box = [fl_x1_n, fl_y1, l_xmax, fl_y2] if is_fl else None
                            lb_box = [fz_x1, lb_y1, fz_x2, lb_y2]
                            st_box    = [st_x1, l_ymax, st_x2, l_ymax + cluster_d_p]
                            st_cx     = (st_x1 + st_x2) / 2.0
                            st_cy     = (st_box[1] + st_box[3]) / 2.0
                            st_base_y = st_box[1]
                            is_rotated_suit = True
                            st_rect   = [l_xmin, l_ymax, l_xmax, l_ymax + cluster_d_p]
                            # Both sides failed — suppress this safety set
                            _p_n_all = ([fl_box] if fl_box else []) + [lb_box, st_box]
                            _p_n_corners = [pt for b in _p_n_all
                                            for pt in [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]]
                            _p_n_in_hole = footprint_holes and any(
                                _pip_p(cx, cy, _h) for _h in footprint_holes for cx, cy in _p_n_corners)
                            if not all(_pip_p(cx, cy, footprint_pts) for cx, cy in _p_n_corners) or _p_n_in_hole:
                                _skip_set = True
                else:
                    # North cluster: extends northward from l_ymax
                    # Fire zone EAST, staircase WEST (pinwheel).
                    # Staircase anchored to west wall; fire zone fills remainder to east wall.
                    st_x1   = l_xmin
                    st_x2   = l_xmin + sw_nat
                    fz_x1   = st_x2
                    fl_x1_n = l_xmax - fl_shaft_d_p          # shaft west edge (code-min width)
                    fz_x2   = l_xmax                          # fire zone fills to east wall

                    # Fire shaft at inner Y (adjacent to lift bank), lobby at outer Y
                    fl_y1 = l_ymax;              fl_y2 = l_ymax + fl_shaft_d_p
                    lb_y1 = fl_y2;               lb_y2 = l_ymax + cluster_d_p
                    fl_box = [fl_x1_n, fl_y1, fz_x2, fl_y2] if is_fl else None
                    lb_box = [fz_x1, lb_y1, fz_x2, lb_y2]

                    st_box    = [st_x1, l_ymax, st_x2, l_ymax + cluster_d_p]
                    st_cx     = (st_x1 + st_x2) / 2.0
                    st_cy     = (st_box[1] + st_box[3]) / 2.0
                    st_base_y = st_box[1]     # south face = inner (adjacent to lift bank)
                    is_rotated_suit = True    # people enter from north face, so rotate 180°
                    st_rect   = [l_xmin, l_ymax, l_xmax, l_ymax + cluster_d_p]
                    # Polygon check: if north cluster falls outside floor plate
                    if footprint_pts and len(footprint_pts) >= 3:
                        from revit_mcp.staircase_logic import _point_in_polygon as _pip_p
                        _p_n_all = ([fl_box] if fl_box else []) + [lb_box, st_box]
                        _p_n_corners = [pt for b in _p_n_all
                                        for pt in [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]]
                        _p_n_in_hole = footprint_holes and any(
                            _pip_p(cx, cy, _h) for _h in footprint_holes for cx, cy in _p_n_corners)
                        if not all(_pip_p(cx, cy, footprint_pts) for cx, cy in _p_n_corners) or _p_n_in_hole:
                            _skip_set = True

                # ── Door specs: NS parallel ──────────────────────────────────
                # stair_global_idx not yet incremented → stair_num = stair_global_idx + 1
                _sn_par = stair_global_idx + 1
                if is_south:
                    # South cluster: ExtDoor on south outer wall, tucked 900mm from west end.
                    # LobbyDoor on west wall (W_Left), at midpoint of the lobby wall section.
                    # EntryDoor and FireLiftDoor centered on respective shaft south walls.
                    _fl_cx = (fz_x1 + fl_x2_s) / 2.0  # shaft centre X (west face to shaft east edge)
                    door_specs.append({
                        "id": tag + "_Stair_ExtDoor",
                        "position_mm": [st_x1 + 900, lb_y1],
                        "wall_line_mm": [[st_x1, lb_y1], [st_x2, lb_y1]],
                        "levels": [_first_lvl_id] if _first_lvl_id else [],
                        "swing_in": False, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf",
                        "wall_ai_id_map": ({_all_lvl_ids[0]: "AI_Stair_{}_L1_W_Front".format(_sn_par)} if _all_lvl_ids else {}),
                    })
                    door_specs.append({
                        "id": tag + "_Stair_LobbyDoor",
                        "position_mm": [st_x1, (lb_y1 + lb_y2) / 2.0],
                        "wall_line_mm": [[st_x1, lb_y1], [st_x1, lb_y2]],
                        "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf", "swing_out_level1": True,
                        "wall_ai_id_map": {_all_lvl_ids[k]: "AI_Stair_{}_L{}_W_Left".format(_sn_par, k + 1) for k in range(len(_all_lvl_ids))},
                    })
                    door_specs.append({
                        "id": tag + "_Lobby_EntryDoor",
                        "position_mm": [_fl_cx, lb_y1],
                        "wall_line_mm": [[fz_x1, lb_y1], [fz_x2, lb_y1]],
                        "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf", "swing_out_level1": True,
                        "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_LB_S_L{}".format(i + 1, k + 1) for k in range(len(_all_lvl_ids))},
                    })
                    if is_fl and fl_box:
                        door_specs.append({
                            "id": tag + "_FireLift_Door",
                            "position_mm": [_fl_cx, fl_y1],
                            "wall_line_mm": [[fz_x1, fl_y1], [fl_x2_s, fl_y1]],  # shaft face only
                            "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                            "door_category": "lift",
                            "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_FL_S_L{}".format(i + 1, k + 1) for k in range(len(_all_lvl_ids))},
                        })
                else:
                    # North cluster: ExtDoor on north outer wall, tucked 900mm from east end.
                    # LobbyDoor on east wall (W_Right), at midpoint of the lobby wall section.
                    # EntryDoor and FireLiftDoor centered on the actual shaft (east side).
                    _fl_cx = (fl_x1_n + fz_x2) / 2.0  # shaft centre X (shaft west edge to east wall)
                    door_specs.append({
                        "id": tag + "_Stair_ExtDoor",
                        "position_mm": [st_x2 - 900, l_ymax + cluster_d_p],
                        "wall_line_mm": [[st_x1, l_ymax + cluster_d_p], [st_x2, l_ymax + cluster_d_p]],
                        "levels": [_first_lvl_id] if _first_lvl_id else [],
                        "swing_in": False, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf",
                        "wall_ai_id_map": ({_all_lvl_ids[0]: "AI_Stair_{}_L1_W_Back".format(_sn_par)} if _all_lvl_ids else {}),
                    })
                    door_specs.append({
                        "id": tag + "_Stair_LobbyDoor",
                        "position_mm": [st_x2, (lb_y1 + lb_y2) / 2.0],
                        "wall_line_mm": [[st_x2, lb_y1], [st_x2, lb_y2]],
                        "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf", "swing_out_level1": True,
                        "wall_ai_id_map": {_all_lvl_ids[k]: "AI_Stair_{}_L{}_W_Right".format(_sn_par, k + 1) for k in range(len(_all_lvl_ids))},
                    })
                    door_specs.append({
                        "id": tag + "_Lobby_EntryDoor",
                        "position_mm": [_fl_cx, lb_y2],
                        "wall_line_mm": [[fz_x1, lb_y2], [fz_x2, lb_y2]],
                        "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                        "door_category": "single_leaf", "swing_out_level1": True,
                        "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_LB_N_L{}".format(i + 1, k + 1) for k in range(len(_all_lvl_ids))},
                    })
                    if is_fl and fl_box:
                        door_specs.append({
                            "id": tag + "_FireLift_Door",
                            "position_mm": [_fl_cx, fl_y2],
                            "wall_line_mm": [[fl_x1_n, fl_y2], [fz_x2, fl_y2]],  # shaft face only
                            "levels": _all_lvl_ids, "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                            "door_category": "lift",
                            "wall_ai_id_map": {_all_lvl_ids[k]: "AI_SafetySet_{}_FL_N_L{}".format(i + 1, k + 1) for k in range(len(_all_lvl_ids))},
                        })

        # ── Determine which lobby face is shared with the fire shaft (to avoid duplicate wall) ──
        if is_fl:
            if is_ew:
                _skip_lobby_face = "W" if is_east_ew else "E"
            else:
                _skip_lobby_face = "N" if is_south else "S"
        else:
            _skip_lobby_face = None

        # ── Determine which fire lift shaft face is shared with the passenger bank ──
        # The shaft face that abuts the bank boundary is a duplicate of the bank's own
        # wall; skip it to prevent co-planar double walls that cause a visible gap in Revit.
        if is_fl and lift_core_bounds_mm:
            if is_ew:
                _skip_fl_bank_face = "W" if is_east_ew else "E"
            else:
                _skip_fl_bank_face = "N" if is_south else "S"
        else:
            _skip_fl_bank_face = None

        # ── Sub-boundary reservations ────────────────────────────────────────
        if _skip_set:
            continue
        sub_boundaries.append({"id": tag + "_Shaft", "rect": fl_box} if is_fl else None)
        sub_boundaries.append({"id": tag + "_Lobby", "rect": lb_box})
        sub_boundaries.append({"id": tag + "_Staircase", "rect": st_box})

        stair_overrides.append(st_base_y)
        core_bounds.append(st_rect)
        _sc_rot = s_set.get("staircase_rotation")
        stair_centers.append((st_cx, st_cy, is_rotated_suit, float(_sc_rot) if _sc_rot is not None else None))

        # ── Fire-lift shaft walls ─────────────────────────────────────────────
        if is_fl and fl_box is not None:
            fl_cx = (fl_box[0] + fl_box[2]) / 2.0
            fl_cy = (fl_box[1] + fl_box[3]) / 2.0
            fl_fw = fl_box[2] - fl_box[0]   # X extent → fw_mm
            fl_fd = fl_box[3] - fl_box[1]   # Y extent → fd_mm
            ls_walls, ls_floors = _fire_lift_shaft_walls(tag + "_FL", fl_cx, fl_cy, fl_fw, fl_fd, levels_data, overrun_height=overrun_height, skip_wall=_skip_fl_bank_face)
            walls.extend(ls_walls)
            floors.extend(ls_floors)
            # Floor-slab void for fire lift shaft — inner clear space (wall centerlines
            # are at fl_box edges, so inner face is ±t/2 inward from each edge).
            _t2 = _WALL_THICKNESS / 2.0 + 1.0
            voids.append((fl_box[0] + _t2, fl_box[1] + _t2, fl_box[2] - _t2, fl_box[3] - _t2))

        # ── Lobby walls (all 4 sides — doors added separately at later stage) ──
        lobby_tag   = tag + "_LB"
        lb_x1_w, lb_x2_w = lb_box[0], lb_box[2]
        lb_y1_w, lb_y2_w = lb_box[1], lb_box[3]
        # Determine which lobby faces coincide with the passenger bank boundary so
        # we don't generate a duplicate wall (bank already has walls on those edges).
        _lb_tol = 25.0
        _skip_lb_bank = set()
        if lift_core_bounds_mm:
            _lcb = lift_core_bounds_mm
            if abs(lb_y2_w - _lcb[3]) < _lb_tol:  _skip_lb_bank.add("N")
            if abs(lb_y1_w - _lcb[1]) < _lb_tol:  _skip_lb_bank.add("S")
            if abs(lb_x2_w - _lcb[2]) < _lb_tol:  _skip_lb_bank.add("E")
            if abs(lb_x1_w - _lcb[0]) < _lb_tol:  _skip_lb_bank.add("W")
        for l_idx, lvl in enumerate(levels_data):
            is_last_lvl = (l_idx == len(levels_data) - 1)
            if is_last_lvl:
                lvl_h = overrun_height
            else:
                lvl_h = levels_data[l_idx + 1]['elevation'] - lvl['elevation']
                if lvl_h <= 0:
                    continue
            common = {"level_id": lvl['id'], "height": lvl_h, "type": "AI_Wall_Core"}
            # Skip the face shared with the fire shaft — the shaft wall already covers it.
            # Also skip any face coincident with the passenger bank boundary.
            if _skip_lobby_face != "W" and "W" not in _skip_lb_bank:
                walls.append({"id": "AI_{}_W_L{}".format(lobby_tag, l_idx + 1),
                              "start": [lb_x1_w, lb_y1_w, 0], "end": [lb_x1_w, lb_y2_w, 0], **common})
            if _skip_lobby_face != "E" and "E" not in _skip_lb_bank:
                walls.append({"id": "AI_{}_E_L{}".format(lobby_tag, l_idx + 1),
                              "start": [lb_x2_w, lb_y1_w, 0], "end": [lb_x2_w, lb_y2_w, 0], **common})
            if _skip_lobby_face != "N" and "N" not in _skip_lb_bank:
                walls.append({"id": "AI_{}_N_L{}".format(lobby_tag, l_idx + 1),
                              "start": [lb_x1_w, lb_y2_w, 0], "end": [lb_x2_w, lb_y2_w, 0], **common})
            if _skip_lobby_face != "S" and "S" not in _skip_lb_bank:
                walls.append({"id": "AI_{}_S_L{}".format(lobby_tag, l_idx + 1),
                              "start": [lb_x1_w, lb_y1_w, 0], "end": [lb_x2_w, lb_y1_w, 0], **common})

        # ── Lobby TOPCAP — closing slab above overrun (matches lift shaft / staircase) ──
        if levels_data:
            last_lvl = levels_data[-1]
            cap_elev = last_lvl['elevation'] + overrun_height
            floors.append({
                "id": "AI_{}_TOPCAP".format(lobby_tag),
                "level_id": last_lvl['id'],
                "elevation": cap_elev,
                "points": [
                    [lb_x1_w, lb_y1_w], [lb_x2_w, lb_y1_w],
                    [lb_x2_w, lb_y2_w], [lb_x1_w, lb_y2_w]
                ]
            })

        # ── Staircase manifest ────────────────────────────────────────────────
        # EW staircases sit on a different axis — no Y-boundary suppression needed.
        # Parallel-NS staircases share a wall with the passenger lift bank; pass
        # lift_core_bounds_mm so wall_overlaps_box suppresses the duplicated shared edge.
        st_lc_bounds = None if is_ew else lift_core_bounds_mm
        st_num_lifts = None if (is_ew or use_parallel) else num_lifts
        _st_rot_dir = s_set.get("staircase_rotation")
        _st_rot_degs = [float(_st_rot_dir)] if _st_rot_dir is not None else None
        st_man = staircase_logic.generate_staircase_manifest(
            [(st_cx, st_cy)], levels_data, sw_nat, stair_spec, typical_floor_height_mm,
            lift_core_bounds_mm=st_lc_bounds, num_lifts=st_num_lifts, lobby_width=lobby_width,
            base_y_override=st_base_y, rotated_indices=([0] if is_rotated_suit else []),
            stair_idx_offset=stair_global_idx, compliance_overrides=co,
            rotation_degs=_st_rot_degs
        )
        stair_global_idx += 1
        walls.extend(st_man.get("walls", []))
        floors.extend(st_man.get("floors", []))
        voids.extend(staircase_logic.get_void_rectangles_mm(
            [(st_cx, st_cy)], sw_nat, sd_nat,
            lift_core_bounds_mm=st_lc_bounds,
            num_lifts=st_num_lifts, lobby_width=lobby_width,
            base_y_override=st_base_y,
            rotated_indices=([0] if is_rotated_suit else [])
        ))

    # ── Exposed staircase detection ─────────────────────────────────────────
    # For each perimeter staircase, determine which floor levels have a floor
    # plate smaller than the staircase footprint.  These levels have the
    # staircase partially or fully outside the slab edge.
    # Returns a list of dicts so building_generator.py can optionally widen
    # those floor plates to enclose the stairs (Step 2 of the design intent).
    exposed_stair_info = []
    if all_floor_dims and stair_centers:
        for s_set_idx, s_set in enumerate(safety_sets):
            if not s_set.get("is_perimeter"):
                continue
            # Retrieve the staircase X-extents and Y-extents for this set.
            # stair_centers list is 1:1 with is_perimeter sets only — find index.
            # core_bounds tracks [xmin, ymin, xmax, ymax] for each safety set.
            if s_set_idx >= len(core_bounds):
                continue
            sb = core_bounds[s_set_idx]  # [x1, y1, x2, y2]
            st_x1, st_y1, st_x2, st_y2 = sb
            st_hl = max(abs(st_y1), abs(st_y2))  # furthest Y extent from centre

            exposed_levels = []
            for lvl_idx, (fw, fl) in enumerate(all_floor_dims):
                floor_hl = fl / 2.0
                floor_hw = fw / 2.0
                # Exposed if staircase Y-extent exceeds floor half-length
                # OR staircase X-extent exceeds floor half-width
                y_exposed = st_hl > floor_hl
                x_exposed = (abs(st_x1) > floor_hw) or (abs(st_x2) > floor_hw)
                if y_exposed or x_exposed:
                    # Minimum widths needed to fully enclose the staircase
                    min_l_needed = st_hl * 2.0
                    min_w_needed = (abs(st_x1) + abs(st_x2))
                    exposed_levels.append({
                        "level_index": lvl_idx + 1,   # 1-based
                        "floor_width": fw,
                        "floor_length": fl,
                        "min_width_to_enclose": round(min_w_needed),
                        "min_length_to_enclose": round(min_l_needed),
                    })
            if exposed_levels:
                exposed_stair_info.append({
                    "stair_set_index": s_set_idx,
                    "stair_bounds": [st_x1, st_y1, st_x2, st_y2],
                    "exposed_levels": exposed_levels,
                })

    # Collect attach_side per placed set so revit_workers can flip PAX bank rows
    # when the fire cluster attaches to the south face of the passenger bank.
    _fire_set_attach_sides = [
        (l["attach_side"] if l else None)
        for l in _placed_layouts
    ]

    return {
        "walls":                  walls,
        "floors":                 floors,
        "voids":                  voids,
        "core_bounds":            core_bounds,
        "stair_centers":          stair_centers,
        "sub_boundaries":         [s for s in sub_boundaries if s],
        "stair_overrides":        stair_overrides,
        "door_specs":             door_specs,
        "exposed_stair_info":     exposed_stair_info,
        "ortools_conflict":       _ortools_conflict,
        "fire_set_attach_sides":  _fire_set_attach_sides,
    }


def _check_radius_coverage(positions, floor_dims_mm, radius_mm):
    """Check if all corners of the floor plate are within radius_mm of at least one position."""
    if not positions: return False
    for (fw, fd) in floor_dims_mm:
        corners = [
            (-fw/2, -fd/2), (fw/2, -fd/2),
            (fw/2, fd/2),  (-fw/2, fd/2)
        ]
        for cx, cy in corners:
            covered = False
            for px, py in positions:
                dist = math.sqrt((cx - px)**2 + (cy - py)**2)
                if dist <= radius_mm:
                    covered = True
                    break
            if not covered: return False
    return True
