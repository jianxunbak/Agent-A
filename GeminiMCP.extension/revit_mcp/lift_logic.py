# -*- coding: utf-8 -*-
import math

# ─────────────────────────────────────────────────────────────────────────────
#  Load compliance data from JSON files — single source of truth.
#  Falls back to hardcoded values if files are missing.
# ─────────────────────────────────────────────────────────────────────────────

def _load_lc():
    try:
        import os, json
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compliance_lift_engineering.json")
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _load_structural():
    try:
        import os, json
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compliance_structural.json")
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
    except Exception:
        pass
    return {}

_LC = _load_lc()
_LT = _LC.get("traffic_analysis", {})

def _ll_log(msg):
    try:
        import os, time
        _lp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "fastmcp_server.log")
        with open(_lp, "a") as _f:
            _f.write("[{}] {}\n".format(time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass
_WALL_T        = _load_structural().get("wall_thickness_mm", {}).get("core_structural", 350)
_SPEEDS        = _LT.get("speed_by_height_m", [
    {"max_building_height_m": 30,   "speed_m_s": 1.6},
    {"max_building_height_m": 60,   "speed_m_s": 2.5},
    {"max_building_height_m": 120,  "speed_m_s": 5.0},
    {"max_building_height_m": 9999, "speed_m_s": 7.0},
])
_AVG_PAX       = _LT.get("avg_passengers_per_trip",       10)
_DOOR_TIME     = _LT.get("door_time_s",                  4.0)
_TRANSFER_T    = _LT.get("passenger_transfer_time_s",    1.1)
_PEAK_FRAC     = _LT.get("peak_demand_fraction",        0.12)
_INTERVAL_S    = _LT.get("interval_period_s",            300)
_OCC_PER_LIFT  = _LT.get("occupants_per_lift",           300)
_MAX_PER_BLOCK = _LC.get("capacity_limits", {}).get("max_lifts_per_block", 12)
_MAX_TOTAL     = _LC.get("capacity_limits", {}).get("max_total_lifts",     24)
_MIN_LIFTS     = _LC.get("capacity_limits", {}).get("min_lifts",            2)
_DOOR_MIN_W    = _LC.get("door_min_width_mm",           1000)


def calculate_lift_requirements(num_floors, avg_floor_height_mm, total_building_occupancy, target_interval=25.0):
    """
    Calculate the number of lifts using Round Trip Time (RTT) analysis.
    Uses the RTT method from BS EN 81-20 / CIBSE Guide D.
    Result is capped at realistic architectural norms to prevent absurd counts.
    """
    total_height_m = (num_floors * avg_floor_height_mm) / 1000.0
    H = total_height_m * 0.8  # Average highest reversal floor

    # Lift speed by building height (from compliance_lift_engineering.json)
    V = next((s["speed_m_s"] for s in _SPEEDS if total_height_m < s["max_building_height_m"]), _SPEEDS[-1]["speed_m_s"])

    P = _AVG_PAX    # Average passengers per trip
    n = float(num_floors)
    S = n * (1 - math.pow(1 - 1.0 / n, P)) if n > 1 else 1.0

    t_d = _DOOR_TIME    # Door open+close time per stop (s)
    t_p = _TRANSFER_T   # Passenger transfer time per person (s)

    RTT = (2 * H / V) + (S + 1) * t_d + (2 * P * t_p)

    # 5-minute peak demand: peak fraction of occupancy travels in interval_period_s
    # Each lift carries P passengers per RTT seconds.
    # Lifts needed = peak_demand / (P * interval_s / RTT)
    peak_demand = total_building_occupancy * _PEAK_FRAC
    handling_capacity_per_lift = P * (float(_INTERVAL_S) / RTT)
    lifts_by_demand = math.ceil(peak_demand / handling_capacity_per_lift) if handling_capacity_per_lift > 0 else _MIN_LIFTS

    # RTT-based count, minimum from compliance, hard cap from compliance.
    # Population-based sanity cap: ~1 lift per N occupants (CIBSE Guide D).
    max_from_pop = max(_MIN_LIFTS, int(math.ceil(total_building_occupancy / float(_OCC_PER_LIFT))))
    num_lifts = max(lifts_by_demand, _MIN_LIFTS)
    num_lifts = min(num_lifts, max_from_pop, _MAX_TOTAL)

    _ll_log("[LiftCalc] floors={} avg_h={}mm occ={} height={:.1f}m speed={:.1f}m/s "
            "H={:.1f} RTT={:.1f}s S={:.1f} peak_demand={:.0f} "
            "handling_per_lift={:.1f} lifts_by_demand={} max_from_pop={} -> lifts={}".format(
            num_floors, avg_floor_height_mm, int(total_building_occupancy),
            total_height_m, V, H, RTT, S, peak_demand,
            handling_capacity_per_lift, int(lifts_by_demand), max_from_pop, int(num_lifts)))
    return int(num_lifts)

def get_core_dimensions(num_lifts, internal_size=(2500, 2500), lobby_width=3000):
    """Calculates the total width and depth of a SINGLE lift core block (max 12)."""
    w, l = internal_size
    t = _WALL_T  # wall thickness from compliance_lift_engineering.json

    # Each core: max 12 lifts, max 6 per side
    if num_lifts >= 4:
        # Split into two rows. Row 1 (north, adjacent to fire cluster) gets the
        # smaller group so the fire lift can use the gap space beside the shorter row.
        n1 = int(math.floor(num_lifts / 2.0))
        n2 = int(math.ceil(num_lifts / 2.0))
        # Clamp to max 6 per side (though num_lifts should be capped at 12 anyway)
        n1 = min(6, n1)
        bw1 = (n1 * w) + ((n1 + 1) * t)
        bw2 = (n2 * w) + ((n2 + 1) * t)
        block_width = max(bw1, bw2)
        block_depth = (2 * (l + 2 * t)) + lobby_width
        _ll_log("[CoreDims] n={} 2-row: n1={} n2={} bw1={} bw2={} -> w={} d={} lobby_w={}".format(
                num_lifts, n1, n2, bw1, bw2, block_width, block_depth, lobby_width))
    else:
        block_width = (num_lifts * w) + ((num_lifts + 1) * t)
        # Single row: include lobby_width so a waiting corridor exists south of the shaft
        block_depth = (l + (2 * t)) + lobby_width
        _ll_log("[CoreDims] n={} 1-row -> w={} d={} lobby_w={}".format(
                num_lifts, block_width, block_depth, lobby_width))

    return block_width, block_depth

def get_total_core_layout(num_lifts, internal_size=(2500, 2500), lobby_width=3000):
    """Calculates multi-core layout (back-to-back) if num_lifts > 12.
    Ensures equal distribution among cores."""
    if num_lifts == 0:
        return {"num_blocks": 0, "lifts_per_block": 0, "total_lifts": 0,
                "block_w": 0, "block_d": 0, "total_w": 0, "total_d": 0}
    # Strict 12-lift max per block
    num_blocks = int(math.ceil(num_lifts / float(_MAX_PER_BLOCK)))

    # Ensure equal number of lifts per core as requested
    # We round up the total count to a multiple of num_blocks
    total_lifts = int(math.ceil(num_lifts / float(num_blocks)) * num_blocks)
    lifts_per_block = total_lifts // num_blocks

    block_w, block_d = get_core_dimensions(lifts_per_block, internal_size, lobby_width)
    total_w = block_w
    total_d = block_d * num_blocks

    _ll_log("[CoreLayout] requested={} blocks={} lifts_per_block={} total_lifts={} "
            "block_w={} block_d={} total_w={} total_d={}".format(
            num_lifts, num_blocks, lifts_per_block, total_lifts,
            block_w, block_d, total_w, total_d))
    return {
        "num_blocks": num_blocks,
        "lifts_per_block": lifts_per_block,
        "total_lifts": total_lifts, # The adjusted total
        "block_w": block_w,
        "block_d": block_d,
        "total_w": total_w,
        "total_d": total_d
    }

def get_block_y_offset(b_idx, num_blocks, block_d):
    """
    Return the Y offset for lift-core block b_idx so the entire multi-block
    assembly is centred on the building centroid (Y = 0).

    For N blocks each of depth block_d the assembly total depth is N * block_d.
    We start block 0 at  -(N-1)/2 * block_d  so the geometric centre lands at 0.

    Examples
    --------
    N=1 : block 0 at  0              (single core, unchanged)
    N=2 : block 0 at -block_d/2,     block 1 at +block_d/2
    N=3 : block 0 at -block_d,       block 1 at  0,  block 2 at +block_d
    N=4 : block 0 at -1.5*block_d,   ...,           block 3 at +1.5*block_d

    Previously block 0 was always at 0 and block 1 at +block_d, which shifted
    the two-block assembly centre to +block_d/2 (~4 400 mm for a typical core),
    making both the lift core and the staircases appear off-centre on typical
    floor plates.
    """
    start_offset = -((num_blocks - 1) / 2.0) * block_d
    return start_offset + b_idx * block_d


def get_shaft_void_rectangles_mm(num_lifts, center_pos=(0, 0), internal_size=(2500, 2500), lobby_width=3000, flip_rows=False):
    """
    Returns a list of (x1, y1, x2, y2) mm rectangles for the INNER CLEAR SPACE of each
    individual lift shaft.  These are used to cut floor-slab openings.
    The rectangles are aligned to the inner faces of the shaft walls (offset by wall
    thickness t = 200 mm inward from the outer wall face).
    One rectangle per lift car — e.g. 4 lifts → 4 rectangles.
    Multi-block layouts (>12 lifts) are handled internally using get_total_core_layout.
    """
    layout = get_total_core_layout(num_lifts, internal_size, lobby_width)
    num_blocks = layout["num_blocks"]
    lifts_per_block = layout["lifts_per_block"]
    block_d = layout["block_d"]

    all_voids = []
    for b_idx in range(num_blocks):
        block_center_y = center_pos[1] + get_block_y_offset(b_idx, num_blocks, block_d)
        block_center = (center_pos[0], block_center_y)
        all_voids.extend(_get_block_void_rectangles_mm(lifts_per_block, block_center, internal_size, lobby_width, flip_rows=flip_rows))
    return all_voids


def _get_block_void_rectangles_mm(num_lifts, center_pos, internal_size=(2500, 2500), lobby_width=3000, flip_rows=False):
    """Void rectangles for a single lift core block (max 12 lifts)."""
    w, l = internal_size
    t = _WALL_T  # wall thickness from compliance_lift_engineering.json

    total_in_block = min(12, num_lifts)
    if total_in_block >= 4:
        n1 = int(math.floor(total_in_block / 2.0))   # smaller row → north (fire cluster side)
        n2 = int(math.ceil(total_in_block / 2.0))
    else:
        n1 = total_in_block
        n2 = 0

    row1_w = (n1 * w) + ((n1 + 1) * t)
    row2_w = (n2 * w) + ((n2 + 1) * t) if n2 > 0 else 0
    max_block_w = max(row1_w, row2_w)
    shaft_depth = l + (2 * t)

    voids = []

    def _row_voids(n_lifts, row_y_offset):
        row_base_x = center_pos[0] - (max_block_w / 2.0)  # left-align to match wall generation
        base_y = center_pos[1] + row_y_offset
        for j in range(n_lifts):
            x1 = row_base_x + j * (w + t) + (t / 2.0) + 1.0
            x2 = x1 + w - 2.0
            y1 = base_y + (t / 2.0) + 1.0
            y2 = y1 + l + t - 2.0
            voids.append((x1, y1, x2, y2))

    if n1 > 0:
        if n2 > 0:
            if flip_rows:
                _row_voids(n1, -(shaft_depth + lobby_width / 2.0))
                _row_voids(n2, lobby_width / 2.0)
            else:
                # Default: Row1 (smaller) north, Row2 (larger) south
                _row_voids(n1, lobby_width / 2.0)
                _row_voids(n2, -(shaft_depth + lobby_width / 2.0))
        else:
            # Single row: Y offset must match _generate_single_block_manifest where
            # r1_y_start = (lobby_width - shaft_depth) / 2.0 so the lobby gap appears south.
            _row_voids(n1, (lobby_width - shaft_depth) / 2.0)

    return voids


def generate_lift_shaft_manifest(num_lifts, levels_data, center_pos=(0, 0), internal_size=(2500, 2500), lobby_width=3000, flip_rows=False):
    """
    Generates manifest data for lift shafts centered around center_pos.
    Both rows are aligned to the same max_block_width for correct visual symmetry.
    Wall IDs use the level name (e.g. AI Level 3) for stable re-identification.
    Supports multi-block layouts (>12 lifts) by generating walls for each block.
    """
    layout = get_total_core_layout(num_lifts, internal_size, lobby_width)
    num_blocks = layout["num_blocks"]
    lifts_per_block = layout["lifts_per_block"]
    block_d = layout["block_d"]

    all_walls_total = []
    all_floors_total = []

    for b_idx in range(num_blocks):
        block_center_y = center_pos[1] + get_block_y_offset(b_idx, num_blocks, block_d)
        block_center = (center_pos[0], block_center_y)
        block_tag = "B{}".format(b_idx) if num_blocks > 1 else ""
        w_block, f_block = _generate_single_block_manifest(
            lifts_per_block, levels_data, block_center, internal_size, lobby_width, block_tag,
            flip_rows=flip_rows
        )
        all_walls_total.extend(w_block)
        all_floors_total.extend(f_block)

    return {"walls": all_walls_total, "floors": all_floors_total}


def _generate_single_block_manifest(num_lifts, levels_data, center_pos=(0, 0), internal_size=(2500, 2500), lobby_width=3000, block_tag="", flip_rows=False):
    """Generate wall/floor manifest for a single lift core block (max 12 lifts)."""
    w, l = internal_size
    t = _WALL_T  # wall thickness from compliance_lift_engineering.json

    # Symmetry: Split into two rows if >= 4 lifts
    # Row 1 (north) = smaller group → adjacent to fire cluster with space for fire lift.
    if num_lifts >= 4:
        lifts_in_row1 = int(math.floor(num_lifts / 2.0))
        lifts_in_row2 = int(math.ceil(num_lifts / 2.0))
    else:
        lifts_in_row1 = int(num_lifts)
        lifts_in_row2 = 0

    # PRE-COMPUTE max block width so BOTH rows are centered relative to it.
    # This prevents the visual off-centre look when rows have unequal lift counts.
    row1_width = (lifts_in_row1 * w) + ((lifts_in_row1 + 1) * t)
    row2_width = (lifts_in_row2 * w) + ((lifts_in_row2 + 1) * t) if lifts_in_row2 > 0 else 0
    max_block_width = max(row1_width, row2_width)

    def create_row_manifest(n_lifts, row_y_offset, row_tag_prefix):
        row_walls = []
        row_floors = []
        this_row_width = (n_lifts * w) + ((n_lifts + 1) * t)
        block_depth = l + (2 * t)

        # Left-align both rows to the OR-Tools west boundary — no centering offset.
        # Shorter rows no longer create a gap between fire lift lobby and pax lift shaft walls.
        row_base_x = center_pos[0] - (max_block_width / 2.0)

        for i, lvl in enumerate(levels_data):
            lvl_id = lvl['id']
            is_last = (i == len(levels_data) - 1)

            base_y = center_pos[1] + row_y_offset

            # Wall height from elevation delta
            wall_h = 4000.0
            if not is_last and i + 1 < len(levels_data):
                wall_h = levels_data[i + 1].get('elevation', 0) - lvl.get('elevation', 0)

            # FIX: Use level NAME as ID tag component (not loop index) for stable re-identification.
            # The level name is always deterministic: "AI Level 1", "AI Level 2" etc.
            safe_lvl_tag = lvl_id.replace(" ", "_")  # e.g. "AI_Level_3"
            common_props = {"level_id": lvl_id, "height": wall_h}

            row_walls.append({
                "id": "AI_{}__{}_W_Front".format(row_tag_prefix, safe_lvl_tag),
                "start": [row_base_x, base_y, 0],
                "end": [row_base_x + this_row_width, base_y, 0],
                **common_props
            })
            row_walls.append({
                "id": "AI_{}__{}_W_Back".format(row_tag_prefix, safe_lvl_tag),
                "start": [row_base_x, base_y + block_depth, 0],
                "end": [row_base_x + this_row_width, base_y + block_depth, 0],
                **common_props
            })
            row_walls.append({
                "id": "AI_{}__{}_W_Left".format(row_tag_prefix, safe_lvl_tag),
                "start": [row_base_x, base_y, 0],
                "end": [row_base_x, base_y + block_depth, 0],
                **common_props
            })
            row_walls.append({
                "id": "AI_{}__{}_W_Right".format(row_tag_prefix, safe_lvl_tag),
                "start": [row_base_x + this_row_width, base_y, 0],
                "end": [row_base_x + this_row_width, base_y + block_depth, 0],
                **common_props
            })

            # Internal Dividers
            for j in range(1, n_lifts):
                div_x = row_base_x + (j * (w + t))
                row_walls.append({
                    "id": "AI_{}__{}__Div{}".format(row_tag_prefix, safe_lvl_tag, j),
                    "start": [div_x, base_y, 0],
                    "end": [div_x, base_y + block_depth, 0],
                    **common_props
                })

            # TOP CAP on last level (overrun walls + closing slab)
            if is_last:
                for wall in row_walls:
                    if wall['level_id'] == lvl_id:
                        wall['height'] = 5000

                ov_elevation = lvl.get('elevation', 0) + 5000
                row_floors.append({
                    "id": "AI_{}__TOPCAP".format(row_tag_prefix),
                    "level_id": lvl_id,
                    "elevation": ov_elevation,
                    "points": [
                        [row_base_x, base_y],
                        [row_base_x + this_row_width, base_y],
                        [row_base_x + this_row_width, base_y + block_depth],
                        [row_base_x, base_y + block_depth]
                    ]
                })

        return row_walls, row_floors

    # Y-offsets
    shaft_depth = l + (2 * t)
    all_walls = []
    all_floors = []

    if lifts_in_row1 > 0:
        if lifts_in_row2 > 0:
            if flip_rows:
                # Fire cluster is south → smaller row (Row1) goes south, larger row (Row2) goes north
                r1_y_start = -(shaft_depth + lobby_width / 2.0)
                r2_y_start = lobby_width / 2.0
            else:
                # Default: smaller row (Row1) goes north, larger row (Row2) goes south.
                # NE layout side=N places FL at anchor north face (py2) beside the north row.
                r1_y_start = lobby_width / 2.0
                r2_y_start = -(shaft_depth + lobby_width / 2.0)
            w1, f1 = create_row_manifest(lifts_in_row1, r1_y_start, "LiftR1" + block_tag)
            w2, f2 = create_row_manifest(lifts_in_row2, r2_y_start, "LiftR2" + block_tag)
            all_walls.extend(w1)
            all_walls.extend(w2)
            all_floors.extend(f1)
            all_floors.extend(f2)
        else:
            # Single row: shift shaft northward so lobby corridor appears to the south.
            # Shaft is centered within (shaft_depth + lobby_width), south half = lobby.
            r1_y_start = (lobby_width - shaft_depth) / 2.0
            w1, f1 = create_row_manifest(lifts_in_row1, r1_y_start, "LiftR1" + block_tag)
            all_walls.extend(w1)
            all_floors.extend(f1)

    return all_walls, all_floors


def get_passenger_lift_door_positions(num_lifts, center_pos=(0, 0), internal_size=(2500, 2500),
                                      lobby_width=3000, levels_data=None, bank_prefix="", flip_rows=False):
    """Return door specs for passenger lift openings facing the central lobby.

    For a 2-row block (>=4 lifts per block):
        Row 1 (south) doors face NORTH toward lobby (back wall).
        Row 2 (north) doors face SOUTH toward lobby (front wall).
    For a single row (<4 lifts): doors face the open (south) side.
    """
    w, l = internal_size
    t = _WALL_T  # from compliance_lift_engineering.json

    layout = get_total_core_layout(num_lifts, internal_size, lobby_width)
    num_blocks = layout["num_blocks"]
    lifts_per_block = layout["lifts_per_block"]
    block_d = layout["block_d"]

    all_level_ids = [lvl['id'] for lvl in levels_data[:-1]] if levels_data and len(levels_data) > 1 else []
    if not all_level_ids and levels_data:
        all_level_ids = [levels_data[0]['id']]

    door_specs = []
    shaft_depth = l + 2 * t

    for b_idx in range(num_blocks):
        block_cy = center_pos[1] + get_block_y_offset(b_idx, num_blocks, block_d)
        cx = center_pos[0]

        block_tag_str = "B{}".format(b_idx) if layout["num_blocks"] > 1 else ""

        if lifts_per_block >= 4:
            n1 = int(math.floor(lifts_per_block / 2.0))   # smaller row → north
            n2 = int(math.ceil(lifts_per_block / 2.0))

            row1_w = (n1 * w) + ((n1 + 1) * t)
            row2_w = (n2 * w) + ((n2 + 1) * t) if n2 > 0 else 0
            max_bw = max(row1_w, row2_w)

            # Row 1: north by default (flip_rows=False).
            # North row's lobby-facing wall is its SOUTH face = the front wall (W_Front).
            if flip_rows:
                r1_y_start = block_cy - (shaft_depth + lobby_width / 2.0)
                r1_lobby_y = r1_y_start + shaft_depth  # back wall faces lobby (north)
                r1_lobby_wall = "W_Back"
            else:
                r1_y_start = block_cy + lobby_width / 2.0
                r1_lobby_y = r1_y_start                # front wall faces lobby (south)
                r1_lobby_wall = "W_Front"
            r1_base_x  = cx - max_bw / 2.0
            row1_tag = "LiftR1" + block_tag_str
            for j in range(n1):
                lift_cx = r1_base_x + j * (w + t) + t + w / 2.0
                door_specs.append({
                    "id": "AI_PaxLift_B{}_R1_J{}".format(b_idx, j),
                    "position_mm": [lift_cx, r1_lobby_y],
                    "wall_line_mm": [[r1_base_x, r1_lobby_y], [r1_base_x + row1_w, r1_lobby_y]],
                    "levels": all_level_ids,
                    "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "lift",
                    "wall_ai_id_map": {
                        lvl['id']: "AI_{}{}__{}_{}" .format(bank_prefix, row1_tag, lvl['id'].replace(" ", "_"), r1_lobby_wall)
                        for lvl in (levels_data or [])
                    },
                })

            # Row 2: south by default (flip_rows=False).
            # South row's lobby-facing wall is its NORTH face = the back wall (W_Back).
            if n2 > 0:
                if flip_rows:
                    r2_y_start = block_cy + lobby_width / 2.0
                    r2_lobby_y = r2_y_start             # front wall faces lobby (south)
                    r2_lobby_wall = "W_Front"
                else:
                    r2_y_start = block_cy - (shaft_depth + lobby_width / 2.0)
                    r2_lobby_y = r2_y_start + shaft_depth  # back wall faces lobby (north)
                    r2_lobby_wall = "W_Back"
                r2_base_x  = cx - max_bw / 2.0
                row2_tag = "LiftR2" + block_tag_str
                for j in range(n2):
                    lift_cx = r2_base_x + j * (w + t) + t + w / 2.0
                    door_specs.append({
                        "id": "AI_PaxLift_B{}_R2_J{}".format(b_idx, j),
                        "position_mm": [lift_cx, r2_lobby_y],
                        "wall_line_mm": [[r2_base_x, r2_lobby_y], [r2_base_x + row2_w, r2_lobby_y]],
                        "levels": all_level_ids,
                        "swing_in": True, "min_width_mm": 1000,
                        "door_category": "lift",
                        "wall_ai_id_map": {
                            lvl['id']: "AI_{}{}__{}_{}" .format(bank_prefix, row2_tag, lvl['id'].replace(" ", "_"), r2_lobby_wall)
                            for lvl in (levels_data or [])
                        },
                    })
        else:
            # Single row — shaft shifted north; door on front (south) wall facing lobby corridor
            row1_w    = (lifts_per_block * w) + ((lifts_per_block + 1) * t)
            r1_y_start = block_cy + (lobby_width - shaft_depth) / 2.0
            r1_front_y = r1_y_start
            r1_base_x  = cx - row1_w / 2.0
            row1_tag = "LiftR1" + block_tag_str
            for j in range(lifts_per_block):
                lift_cx = r1_base_x + j * (w + t) + t + w / 2.0
                door_specs.append({
                    "id": "AI_PaxLift_B{}_R1_J{}".format(b_idx, j),
                    "position_mm": [lift_cx, r1_front_y],
                    "wall_line_mm": [[r1_base_x, r1_front_y], [r1_base_x + row1_w, r1_front_y]],
                    "levels": all_level_ids,
                    "swing_in": True, "flip_hand": True, "min_width_mm": 1000,
                    "door_category": "lift",
                    "wall_ai_id_map": {
                        lvl['id']: "AI_{}{}__{}_W_Front".format(bank_prefix, row1_tag, lvl['id'].replace(" ", "_"))
                        for lvl in (levels_data or [])
                    },
                })

    return door_specs


def generate_lift_shaft_from_polygon(footprint, num_lifts, levels_data,
                                     lobby_width=3000, internal_size=(2500, 2500)):
    """Generate lift shaft walls/floors using a Gemini-specified footprint polygon.

    Uses the polygon centroid as the layout centre so the shaft grid is centred
    within the Gemini-provided passenger_lift_core footprint.

    Args:
        footprint: list of [x,y] points from core_layout.elements passenger_lift_core
        num_lifts: total number of passenger lift cars
        levels_data: list of {'id', 'elevation'} dicts
        lobby_width: mm — gap between facing lift rows
        internal_size: (w, d) mm — single lift car internal dimensions

    Returns:
        dict with 'walls' and 'floors' (same format as generate_lift_shaft_manifest)
    """
    if not footprint or len(footprint) < 3:
        return {"walls": [], "floors": []}
    cx = sum(p[0] for p in footprint) / len(footprint)
    cy = sum(p[1] for p in footprint) / len(footprint)
    return generate_lift_shaft_manifest(
        num_lifts, levels_data,
        center_pos=(cx, cy),
        internal_size=internal_size,
        lobby_width=lobby_width
    )
