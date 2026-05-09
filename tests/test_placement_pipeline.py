# -*- coding: utf-8 -*-
"""
test_placement_pipeline.py
==========================
Step-by-step diagnostic tests for the 20-storey 60×60m courtyard building prompt.

Run with:
    cd "c:\\Users\\jianxun\\Documents\\Revit 2026 MCP\\revit-MCP\\GeminiMCP.extension"
    python -m revit_mcp.test_placement_pipeline

Each test is independent and prints a clear PASS / FAIL / INFO summary.
Tests cover the 4 pipeline stages:

  Test 1 – Gemini intent:    Does Gemini produce a valid anchor position?
  Test 2 – Snap zone:        Does the snap zone calculation produce a feasible band?
  Test 3 – Anchor freedom:   Can OR-Tools move the anchor anywhere within the zone?
  Test 4 – Module placement: Can OR-Tools arrange all 3 modules around the anchor?
"""

import sys, math, time

# ─────────────────────────────────────────────────────────────────────────────
#  SCENARIO — mirrors "Create a 20-storey courtyard building. Square footprint
#  60×60m with a 20×20m central courtyard void."
# ─────────────────────────────────────────────────────────────────────────────

# Building geometry (absolute mm, origin at bottom-left corner)
FP_X1, FP_Y1 = 0.0,     0.0
FP_X2, FP_Y2 = 60000.0, 60000.0
VOID_X1, VOID_Y1 = 20000.0, 20000.0
VOID_X2, VOID_Y2 = 40000.0, 40000.0

FOOTPRINT_PTS = [
    [FP_X1, FP_Y1],
    [FP_X2, FP_Y1],
    [FP_X2, FP_Y2],
    [FP_X1, FP_Y2],
]
FOOTPRINT_HOLES = [[
    [VOID_X1, VOID_Y1],
    [VOID_X2, VOID_Y1],
    [VOID_X2, VOID_Y2],
    [VOID_X1, VOID_Y2],
]]

NUM_STOREYS   = 20
LEVEL_HEIGHT  = 3500   # mm
NUM_LIFTS     = 5      # what a 20-storey ~3600m² floor needs

# Passenger lift bank — 5 lifts, each 2500mm wide + 200mm wall.
# Bank: total_w = 5×(2500+200)+200=13700mm, total_d = 2500+2×350+3000=6200mm
# (These match lift_logic.get_total_core_layout outputs for 5 lifts)
BANK_W   = 13700  # mm  total bank width
BANK_D   = 6200   # mm  total bank depth (shaft + lobby)
WALL_T   = 350    # mm  structural wall thickness

# Fire cluster dimensions (SCDF RAG-overridden values — 5000mm lobby)
FL_SHAFT_D  = 3200   # mm  fire lift shaft depth
EW_LB_DX    = 5000   # mm  fire lobby depth (RAG: 20m² @ 4000mm wide)
SD_NAT      = 7600   # mm  staircase shaft depth
# Compact rectangle: all 3 modules side-by-side parallel to bank face.
# Perpendicular depth = deepest single module = staircase depth only.
CHAIN_DEPTH = SD_NAT                            # 7600mm (not linear sum 15800mm)

# Bank half-dimensions (for centre-coordinate calculations)
BANK_HD = BANK_D / 2.0   # 3100mm
BANK_HW = BANK_W / 2.0   # 6850mm

# ─────────────────────────────────────────────────────────────────────────────
#  COMPLIANCE OVERRIDES — mirror what SCDF RAG returns
# ─────────────────────────────────────────────────────────────────────────────
COMPLIANCE_OVERRIDES = {
    "fire_lobby_min_area_mm2":   20_000_000,   # 20 m² (what RAG returns; capped to bank width)
    "fire_lobby_min_width_mm":   4000,
    "fire_lobby_min_length_mm":  5000,
}

# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sep(title):
    print("\n" + "=" * 70)
    print("  {}".format(title))
    print("=" * 70)

def _ok(msg):  print("  [PASS] {}".format(msg))
def _fail(msg): print("  [FAIL] {}".format(msg))
def _info(msg): print("  [INFO] {}".format(msg))

def _box_str(b):
    return "({:.0f},{:.0f})→({:.0f},{:.0f}) mm  [w={:.0f} d={:.0f}]".format(
        b[0], b[1], b[2], b[3], b[2]-b[0], b[3]-b[1])


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 1 — Gemini intent position
#  Purpose: Verify that Gemini returns a valid anchor centre that is:
#    (a) inside the footprint
#    (b) outside the courtyard void
#    (c) on a side where a band theoretically exists (even if infeasible)
# ═════════════════════════════════════════════════════════════════════════════

def test1_gemini_intent():
    _sep("TEST 1 — Gemini intent position")
    _info("Scenario: 20-storey 60×60m courtyard, 5 lifts, SCDF RAG active")
    _info("Bank: {}×{}mm  chain: {}mm".format(BANK_W, BANK_D, CHAIN_DEPTH))

    # Simulate a few representative positions Gemini might choose.
    # We test each position for validity and what band it implies.
    candidates = [
        {"label": "South band intent (y=9600)",  "cx": 30000, "cy": 9600},
        {"label": "South band intent (y=14300)", "cx": 30000, "cy": 14300},
        {"label": "North band intent (y=50400)", "cx": 30000, "cy": 50400},
        {"label": "Inside void (y=30000)",       "cx": 30000, "cy": 30000},
        {"label": "Edge of void S face (y=19900)","cx": 30000,"cy": 19900},
    ]

    print()
    for c in candidates:
        cx, cy = c["cx"], c["cy"]
        bx1 = cx - BANK_HW; bx2 = cx + BANK_HW
        by1 = cy - BANK_HD; by2 = cy + BANK_HD

        inside_fp   = (bx1 >= FP_X1 and bx2 <= FP_X2 and
                       by1 >= FP_Y1 and by2 <= FP_Y2)
        overlaps_void = not (bx2 <= VOID_X1 or bx1 >= VOID_X2 or
                              by2 <= VOID_Y1 or by1 >= VOID_Y2)
        implied_side = "INSIDE VOID" if overlaps_void else (
            "S" if cy < (VOID_Y1 + VOID_Y2) / 2 else "N")

        # Compute whether this side has a feasible band
        s_lo = FP_Y1 + BANK_HD + CHAIN_DEPTH   # 3100+15800=18900
        s_hi = VOID_Y1 - BANK_HD               # 20000-3100=16900
        n_lo = VOID_Y2 + BANK_HD               # 40000+3100=43100
        n_hi = FP_Y2 - BANK_HD - CHAIN_DEPTH   # 60000-3100-15800=41100
        s_feasible = s_lo <= s_hi
        n_feasible = n_lo <= n_hi

        status = "OK" if (inside_fp and not overlaps_void) else "PROBLEM"
        print("  {} — {}".format(status, c["label"]))
        print("    Bank: {}".format(_box_str((bx1,by1,bx2,by2))))
        print("    Inside FP: {}  Overlaps void: {}  Implied side: {}".format(
            inside_fp, overlaps_void, implied_side))
        if implied_side == "S":
            print("    S-band: [{:.0f}, {:.0f}] → {}".format(
                s_lo, s_hi, "FEASIBLE" if s_feasible else "INFEASIBLE (building too small)"))
        elif implied_side == "N":
            print("    N-band: [{:.0f}, {:.0f}] → {}".format(
                n_lo, n_hi, "FEASIBLE" if n_feasible else "INFEASIBLE (building too small)"))

    print()
    _info("Band feasibility summary for 60×60m building with chain={}mm:".format(CHAIN_DEPTH))
    s_lo = FP_Y1 + BANK_HD + CHAIN_DEPTH
    s_hi = VOID_Y1 - BANK_HD
    n_lo = VOID_Y2 + BANK_HD
    n_hi = FP_Y2 - BANK_HD - CHAIN_DEPTH
    print("    S-band: [{:.0f}, {:.0f}] → {}  (need {:.0f}mm, have {:.0f}mm)".format(
        s_lo, s_hi, "FEASIBLE" if s_lo<=s_hi else "INFEASIBLE",
        CHAIN_DEPTH + 2*BANK_HD, VOID_Y1 - FP_Y1))
    print("    N-band: [{:.0f}, {:.0f}] → {}  (need {:.0f}mm, have {:.0f}mm)".format(
        n_lo, n_hi, "FEASIBLE" if n_lo<=n_hi else "INFEASIBLE",
        CHAIN_DEPTH + 2*BANK_HD, FP_Y2 - VOID_Y2))

    if s_lo > s_hi and n_lo > n_hi:
        min_band = CHAIN_DEPTH + 2*BANK_HD + 1000
        void_depth = VOID_Y2 - VOID_Y1
        min_building = int(void_depth + 2 * min_band)
        _fail("ALL BANDS INFEASIBLE — 60m building cannot fit 5-lift core with SCDF lobby.")
        _info("  Need {}mm per band, have {}mm".format(int(CHAIN_DEPTH+2*BANK_HD), int(VOID_Y1-FP_Y1)))
        _info("  Minimum building size: {}mm — need {}mm more".format(
            min_building, min_building - int(FP_Y2-FP_Y1)))
        _info("  OR reduce lifts to ≤3 (chain drops to ~14000mm)")
    else:
        _ok("At least one band is feasible — Gemini should target that band")


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 2 — Snap zone calculation (calls fire_safety_logic internals)
#  Purpose: Verify the snap zone is computed correctly using absolute geometry,
#  and reports INFEASIBLE accurately (not a fake feasible zone).
# ═════════════════════════════════════════════════════════════════════════════

def test2_snap_zone():
    _sep("TEST 2 — Snap zone calculation")

    try:
        from revit_mcp import fire_safety_logic as fsl
    except ImportError as e:
        _fail("Cannot import fire_safety_logic: {}".format(e))
        return

    _info("Calling generate_fire_safety_manifest with 60×60m courtyard, anchor at S-intent position")
    _info("Bank=({:.0f}x{:.0f}mm), chain={}mm, compliance=SCDF".format(BANK_W, BANK_D, CHAIN_DEPTH))

    # Build the minimum inputs needed to trigger the snap zone path.
    # We give Gemini's intent as anchor near the south band (y=9600 = by1).
    _anc_cx = 30000.0
    _anc_cy = 9600.0   # south-intent: bank centre 9600mm from south wall
    anchor_mm = (
        _anc_cx - BANK_HW, _anc_cy - BANK_HD,
        _anc_cx + BANK_HW, _anc_cy + BANK_HD,
    )

    # Build a minimal safety_sets list (single FIRE_LIFT set)
    safety_sets = [{
        "type": "FIRE_LIFT",
        "is_perimeter": False,
        "pos": (_anc_cx, _anc_cy),
        "floors": list(range(NUM_STOREYS)),
        "n_lifts": 1,
    }]

    # Minimal stair_spec
    stair_spec = {"riser": 150, "tread": 300, "width_of_flight": 1200, "landing_width": 1500}
    levels_data = [{"id": "L{}".format(i), "elevation": i * LEVEL_HEIGHT} for i in range(NUM_STOREYS + 1)]
    preset_fs = {
        "staircase_spec": stair_spec,
        "max_lifts_per_bank": 12,
    }

    try:
        result = fsl.generate_fire_safety_manifest(
            safety_sets      = safety_sets,
            levels_data      = levels_data,
            stair_spec       = stair_spec,
            typical_floor_height_mm = LEVEL_HEIGHT,
            _preset_fs       = preset_fs,
            lift_core_bounds_mm = anchor_mm,
            num_lifts        = NUM_LIFTS,
            lobby_width      = 3000,
            compliance_overrides = COMPLIANCE_OVERRIDES,
            footprint_pts    = FOOTPRINT_PTS,
            footprint_holes  = FOOTPRINT_HOLES,
        )
    except Exception as exc:
        _fail("generate_fire_safety_manifest raised: {}".format(exc))
        import traceback; traceback.print_exc()
        return

    if isinstance(result, dict) and result.get("status") == "CONFLICT":
        _info("Result: CONFLICT — {}".format(result.get("type", "?")))
        _info("Description: {}".format(result.get("description", "")[:200]))
        hints = result.get("resolution_hints", [])
        for h in hints:
            _info("  Hint: {}".format(str(h)[:150]))
        if result.get("all_bands_infeasible"):
            min_b = result.get("min_building_mm", "?")
            _fail("Snap zone: ALL BANDS INFEASIBLE — min building needed = {}mm".format(min_b))
        else:
            _fail("Snap zone: conflict but bands NOT flagged all-infeasible — may be overlap or partial failure")
    elif isinstance(result, dict) and result.get("status") in ("ok", "OK", None):
        _ok("generate_fire_safety_manifest returned a layout (no conflict)!")
        # Try to read snap zone from log
        _info("Check fastmcp_server.log for [DIAG][SnapZone] lines to see zone details.")
    else:
        _info("Result type: {}  keys: {}".format(type(result), list(result.keys()) if isinstance(result, dict) else "N/A"))
        _info("(This may be a full manifest list — check for walls/sub_boundaries keys)")

    # Now run the snap zone math directly so we can see the exact values
    print()
    _info("Direct snap zone math (no function call — manual computation):")
    void_hy1_min = VOID_Y1   # 20000
    void_hy2_max = VOID_Y2   # 40000
    s_lo = FP_Y1 + BANK_HD + CHAIN_DEPTH
    s_hi = void_hy1_min - BANK_HD
    n_lo = void_hy2_max + BANK_HD
    n_hi = FP_Y2 - BANK_HD - CHAIN_DEPTH
    print("    fp_y1={:.0f} fp_y2={:.0f}  void_hy1={:.0f} void_hy2={:.0f}".format(
        FP_Y1, FP_Y2, void_hy1_min, void_hy2_max))
    print("    bank_hd={:.0f}  chain={:.0f}".format(BANK_HD, CHAIN_DEPTH))
    print("    S-band centre_y: [{:.0f}, {:.0f}] → {}".format(
        s_lo, s_hi, "FEASIBLE ✓" if s_lo<=s_hi else "INFEASIBLE ✗"))
    print("    N-band centre_y: [{:.0f}, {:.0f}] → {}".format(
        n_lo, n_hi, "FEASIBLE ✓" if n_lo<=n_hi else "INFEASIBLE ✗"))

    if s_lo > s_hi and n_lo > n_hi:
        void_depth = VOID_Y2 - VOID_Y1
        min_band = CHAIN_DEPTH + 2 * BANK_HD + 1000
        min_bldg = int(void_depth + 2 * min_band)
        _fail("Both bands infeasible — building needs to be {}mm+ (currently {}mm)".format(
            min_bldg, int(FP_Y2 - FP_Y1)))

        # Show what a building that WOULD work looks like
        print()
        _info("Simulation: What building size makes S-band feasible?")
        for trial_fp_y2 in [70000, 72400, 75000, 80000]:
            trial_s_hi = void_hy1_min - BANK_HD   # unchanged — void is at absolute pos
            trial_void_y2 = trial_fp_y2 - (FP_Y2 - VOID_Y2)  # void shifts if building grows symmetrically
            trial_n_lo = trial_void_y2 + BANK_HD
            trial_n_hi = trial_fp_y2 - BANK_HD - CHAIN_DEPTH
            # For S-band: void is still at 20000 if only length changes (void at same position)
            trial_s_feasible = s_lo <= trial_s_hi  # s_hi unchanged
            trial_n_feasible = trial_n_lo <= trial_n_hi
            print("    shell={}mm: S-band {} [{:.0f},{:.0f}]  N-band {} [{:.0f},{:.0f}]".format(
                trial_fp_y2,
                "FEASIBLE" if trial_s_feasible else "INFEASIBLE",
                s_lo, trial_s_hi,
                "FEASIBLE" if trial_n_feasible else "INFEASIBLE",
                trial_n_lo, trial_n_hi))
    else:
        _ok("Snap zone has at least one feasible band")
        if s_lo <= s_hi:
            sz = (FP_X1, FP_Y1 + BANK_HD - s_hi + s_lo, FP_X2, s_hi + BANK_HD)
            _info("  Snap zone (S band, top-left corner): {}".format(_box_str(sz)))
        if n_lo <= n_hi:
            sz = (FP_X1, n_lo - BANK_HD, FP_X2, FP_Y2 - (FP_Y2 - BANK_HD - n_hi))
            _info("  Snap zone (N band, top-left corner): {}".format(_box_str(sz)))


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 3 — Anchor freedom within snap zone
#  Purpose: Verify OR-Tools can move the anchor to different positions within
#  a feasible snap zone. Uses a larger building (75000mm) where bands are feasible.
# ═════════════════════════════════════════════════════════════════════════════

def test3_anchor_freedom():
    _sep("TEST 3 — OR-Tools anchor movement within snap zone")

    try:
        from revit_mcp.core_layout_engine import find_layout_for_set
    except ImportError as e:
        _fail("Cannot import core_layout_engine: {}".format(e))
        return

    # Use a 75000mm building so bands are feasible.
    # Void at same position [20000,40000], building extends to 75000mm.
    FP_Y2_75 = 75000.0
    VOID_Y1_75 = 20000.0
    VOID_Y2_75 = 40000.0

    fp_pts_75 = [
        [0.0, 0.0], [60000.0, 0.0],
        [60000.0, FP_Y2_75], [0.0, FP_Y2_75],
    ]
    fp_holes_75 = [[
        [VOID_X1, VOID_Y1_75], [VOID_X2, VOID_Y1_75],
        [VOID_X2, VOID_Y2_75], [VOID_X1, VOID_Y2_75],
    ]]

    # S-band for 75000mm building with void still at [20000,40000]:
    s_lo_75 = 0.0  + BANK_HD + CHAIN_DEPTH   # 3100+15800=18900
    s_hi_75 = VOID_Y1_75 - BANK_HD           # 20000-3100=16900
    # Still infeasible! Void is at 20000mm regardless of building size.
    # The building grew north, so N-band is what changed:
    n_lo_75 = VOID_Y2_75 + BANK_HD           # 40000+3100=43100
    n_hi_75 = FP_Y2_75 - BANK_HD - CHAIN_DEPTH   # 75000-3100-15800=56100

    _info("Building: 60×75000mm  Void: [{:.0f},{:.0f}]".format(VOID_Y1_75, VOID_Y2_75))
    _info("S-band: [{:.0f},{:.0f}] → {}".format(s_lo_75, s_hi_75,
        "FEASIBLE" if s_lo_75<=s_hi_75 else "INFEASIBLE"))
    _info("N-band: [{:.0f},{:.0f}] → {}  ({:.0f}mm range)".format(n_lo_75, n_hi_75,
        "FEASIBLE" if n_lo_75<=n_hi_75 else "INFEASIBLE", max(0, n_hi_75-n_lo_75)))

    if n_lo_75 > n_hi_75:
        _fail("N-band still infeasible for 75000mm building. Void must shift too.")
        _info("Note: When building grows, void must also shift northward proportionally.")
        _info("For snap zone test, using centred void = building_mid ± 10000mm")
        # Use centred void instead
        mid_75 = FP_Y2_75 / 2
        VOID_Y1_75 = mid_75 - 10000; VOID_Y2_75 = mid_75 + 10000
        fp_holes_75 = [[
            [VOID_X1, VOID_Y1_75],[VOID_X2, VOID_Y1_75],
            [VOID_X2, VOID_Y2_75],[VOID_X1, VOID_Y2_75],
        ]]
        n_lo_75 = VOID_Y2_75 + BANK_HD
        n_hi_75 = FP_Y2_75 - BANK_HD - CHAIN_DEPTH
        s_lo_75 = 0 + BANK_HD + CHAIN_DEPTH
        s_hi_75 = VOID_Y1_75 - BANK_HD
        _info("Adjusted: void=[{:.0f},{:.0f}]  S-band:[{:.0f},{:.0f}] {}  N-band:[{:.0f},{:.0f}] {}".format(
            VOID_Y1_75, VOID_Y2_75,
            s_lo_75, s_hi_75, "FEASIBLE" if s_lo_75<=s_hi_75 else "INFEASIBLE",
            n_lo_75, n_hi_75, "FEASIBLE" if n_lo_75<=n_hi_75 else "INFEASIBLE"))

    # Try 3 different snap zones within the N-band: lo, mid, hi
    _anchor_cx = 30000.0
    test_positions = []
    if n_lo_75 <= n_hi_75:
        mid_n = (n_lo_75 + n_hi_75) / 2
        test_positions = [
            ("N-band lo",  _anchor_cx, n_lo_75 + 500),
            ("N-band mid", _anchor_cx, mid_n),
            ("N-band hi",  _anchor_cx, n_hi_75 - 500),
        ]
    elif s_lo_75 <= s_hi_75:
        mid_s = (s_lo_75 + s_hi_75) / 2
        test_positions = [
            ("S-band lo",  _anchor_cx, s_lo_75 + 500),
            ("S-band mid", _anchor_cx, mid_s),
            ("S-band hi",  _anchor_cx, s_hi_75 - 500),
        ]

    if not test_positions:
        _fail("No feasible band found — cannot test anchor freedom")
        return

    # Build snap zone using the feasible band
    if n_lo_75 <= n_hi_75:
        # N-band: snap zone top-left corner y = [n_lo - bank_hd, n_hi - bank_hd]
        snap_y1 = n_lo_75 - BANK_HD
        snap_y2 = n_hi_75 + BANK_HD
        snap_zone = (0.0, snap_y1, 60000.0, snap_y2)
        allowed_sides = ["N", "S"]
    else:
        snap_y1 = s_lo_75 - BANK_HD
        snap_y2 = s_hi_75 + BANK_HD
        snap_zone = (0.0, snap_y1, 60000.0, snap_y2)
        allowed_sides = ["N", "S"]

    _info("Snap zone (top-left AABB): {}".format(_box_str(snap_zone)))
    print()

    for label, cx, cy in test_positions:
        anchor = (cx - BANK_HW, cy - BANK_HD, cx + BANK_HW, cy + BANK_HD)
        t0 = time.time()
        result = find_layout_for_set(
            anchor_bounds    = anchor,
            fire_lift_size   = (FL_SHAFT_D, FL_SHAFT_D),
            lobby_size       = (EW_LB_DX,   3200),
            staircase_size   = (4050,        SD_NAT),
            footprint_pts    = fp_pts_75,
            footprint_holes  = fp_holes_75,
            allowed_sides    = allowed_sides,
            anchor_snap_zone = snap_zone,
            log_fn           = None,
        )
        elapsed = (time.time() - t0) * 1000

        print("  Anchor intent: {} — cy={:.0f}mm".format(label, cy))
        print("    Anchor bounds: {}".format(_box_str(anchor)))
        if result is None:
            _fail("OR-Tools returned None (INFEASIBLE) in {:.0f}ms".format(elapsed))
        else:
            solved_anchor = result.get("solved_anchor_bounds")
            attach_side   = result.get("attach_side", "?")
            if solved_anchor:
                solved_cy = (solved_anchor[1] + solved_anchor[3]) / 2
                drift = abs(solved_cy - cy)
                _ok("OR-Tools solved in {:.0f}ms — attach_side={}, anchor drifted {:.0f}mm from intent".format(
                    elapsed, attach_side, drift))
                print("    Solved anchor: {}".format(_box_str(solved_anchor)))
            else:
                _ok("Solved in {:.0f}ms — attach_side={}".format(elapsed, attach_side))
        print()


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 4 — Full module placement
#  Purpose: Verify OR-Tools places fire_lift + lobby + staircase correctly
#  around the anchor, reports their positions, and allows free rearrangement.
# ═════════════════════════════════════════════════════════════════════════════

def test4_module_placement():
    _sep("TEST 4 — Module placement around anchor")

    try:
        from revit_mcp.core_layout_engine import find_layout_for_set
    except ImportError as e:
        _fail("Cannot import core_layout_engine: {}".format(e))
        return

    # Use a building where we know a feasible band exists.
    # 60×60m with centred void shifted so N-band is 25000mm deep.
    # N-band needs: CHAIN_DEPTH + 2*BANK_HD = 15800+6200=22000mm
    # Use void at [20000,20000]→[40000,35000] (15000mm N-band = too narrow)
    # Use 80000mm building with centred void [30000,30000]→[50000,50000] instead.
    BLDG = 80000.0
    VY1, VY2 = 30000.0, 50000.0
    VX1, VX2 = 30000.0, 50000.0

    fp_pts_80 = [[0,0],[BLDG,0],[BLDG,BLDG],[0,BLDG]]
    fp_holes_80 = [[[VX1,VY1],[VX2,VY1],[VX2,VY2],[VX1,VY2]]]

    # N-band: lo=50000+3100=53100, hi=80000-3100-15800=61100 → 8000mm range
    n_lo = VY2 + BANK_HD     # 53100
    n_hi = BLDG - BANK_HD - CHAIN_DEPTH  # 61100
    s_lo = 0 + BANK_HD + CHAIN_DEPTH    # 18900
    s_hi = VY1 - BANK_HD               # 26900

    _info("Building: {}×{}mm  Void: [{:.0f},{:.0f}]→[{:.0f},{:.0f}]".format(
        int(BLDG), int(BLDG), VX1, VY1, VX2, VY2))
    _info("S-band: [{:.0f},{:.0f}] → {}  ({:.0f}mm range)".format(
        s_lo, s_hi, "FEASIBLE" if s_lo<=s_hi else "INFEASIBLE", max(0,s_hi-s_lo)))
    _info("N-band: [{:.0f},{:.0f}] → {}  ({:.0f}mm range)".format(
        n_lo, n_hi, "FEASIBLE" if n_lo<=n_hi else "INFEASIBLE", max(0,n_hi-n_lo)))

    print()

    # Test both S-band and N-band placement
    test_configs = []
    if s_lo <= s_hi:
        mid_s = (s_lo + s_hi) / 2
        sz_s = (0.0, s_lo - BANK_HD, BLDG, s_hi + BANK_HD)
        test_configs.append(("South band", 30000, mid_s, sz_s, ["N","S"]))
    if n_lo <= n_hi:
        mid_n = (n_lo + n_hi) / 2
        sz_n = (0.0, n_lo - BANK_HD, BLDG, n_hi + BANK_HD)
        test_configs.append(("North band", 40000, mid_n, sz_n, ["N","S"]))

    for band_label, cx, cy, snap_zone, allowed_sides in test_configs:
        anchor = (cx - BANK_HW, cy - BANK_HD, cx + BANK_HW, cy + BANK_HD)
        logs = []

        t0 = time.time()
        result = find_layout_for_set(
            anchor_bounds    = anchor,
            fire_lift_size   = (FL_SHAFT_D, FL_SHAFT_D),
            lobby_size       = (EW_LB_DX,   3200),
            staircase_size   = (4050,        SD_NAT),
            footprint_pts    = fp_pts_80,
            footprint_holes  = fp_holes_80,
            allowed_sides    = allowed_sides,
            anchor_snap_zone = snap_zone,
            log_fn           = logs.append,
        )
        elapsed = (time.time() - t0) * 1000

        print("  {} — anchor intent cy={:.0f}mm".format(band_label, cy))
        print("    Snap zone: {}".format(_box_str(snap_zone)))
        print("    Anchor intent: {}".format(_box_str(anchor)))

        if result is None:
            _fail("OR-Tools INFEASIBLE in {:.0f}ms".format(elapsed))
            # Print relevant log lines
            for ln in logs[-10:]:
                print("    LOG: {}".format(ln))
        else:
            _ok("Solved in {:.0f}ms".format(elapsed))
            solved_anchor = result.get("solved_anchor_bounds")
            attach_side   = result.get("attach_side", "?")
            chain_order   = result.get("chain_order", "?")
            fl_bounds     = result.get("fire_lift")
            lb_bounds     = result.get("lobby")
            st_bounds     = result.get("staircase")

            if solved_anchor:
                solved_cy = (solved_anchor[1] + solved_anchor[3]) / 2
                drift = abs(solved_cy - cy)
                print("    Solved anchor:     {}  (drift {:.0f}mm from intent)".format(
                    _box_str(solved_anchor), drift))
                if drift <= 500:
                    _ok("Anchor stayed close to intent position (drift {:.0f}mm)".format(drift))
                else:
                    _info("Anchor drifted {:.0f}mm — OR-Tools repositioned for feasibility".format(drift))

            print("    Attach side:       {}".format(attach_side))
            print("    Chain order:       {}".format(chain_order))
            if fl_bounds:
                print("    Fire lift:         {}".format(_box_str(fl_bounds)))
                # Check fire lift is NOT inside the void
                fl_in_void = not (fl_bounds[2] <= VX1 or fl_bounds[0] >= VX2 or
                                   fl_bounds[3] <= VY1 or fl_bounds[1] >= VY2)
                if fl_in_void:
                    _fail("Fire lift overlaps courtyard void!")
                else:
                    _ok("Fire lift is outside the void")
            if lb_bounds:
                print("    Lobby:             {}".format(_box_str(lb_bounds)))
            if st_bounds:
                print("    Staircase:         {}".format(_box_str(st_bounds)))

            # Verify chain is on the correct side of anchor
            if solved_anchor and fl_bounds:
                anc_cy = (solved_anchor[1] + solved_anchor[3]) / 2
                fl_cy  = (fl_bounds[1] + fl_bounds[3]) / 2
                if attach_side == "S" and fl_cy < anc_cy:
                    _ok("Fire lift is South of anchor ✓")
                elif attach_side == "N" and fl_cy > anc_cy:
                    _ok("Fire lift is North of anchor ✓")
                else:
                    _info("Attach side={} but fire_lift_cy={:.0f} vs anchor_cy={:.0f}".format(
                        attach_side, fl_cy, anc_cy))

            # Verify no module overlaps the void
            modules = {"fire_lift": fl_bounds, "lobby": lb_bounds, "staircase": st_bounds}
            for mname, mb in modules.items():
                if mb is None: continue
                overlaps = not (mb[2] <= VX1 or mb[0] >= VX2 or mb[3] <= VY1 or mb[1] >= VY2)
                if overlaps:
                    _fail("{} overlaps courtyard void at {}".format(mname, _box_str(mb)))
                else:
                    _ok("{} does not overlap void".format(mname))
        print()


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 5 — End-to-end with real building that works (smoke test)
#  Purpose: Confirm the pipeline works when building is large enough.
#  Uses the same prompt scenario but with shell.length=80000mm.
# ═════════════════════════════════════════════════════════════════════════════

def test5_working_scenario():
    _sep("TEST 5 — End-to-end: 80×80m courtyard (should succeed)")

    try:
        from revit_mcp.core_layout_engine import find_layout_for_set
    except ImportError as e:
        _fail("Cannot import core_layout_engine: {}".format(e))
        return

    BLDG = 80000.0
    VY1, VY2 = 30000.0, 50000.0
    VX1, VX2 = 30000.0, 50000.0

    fp_pts = [[0,0],[BLDG,0],[BLDG,BLDG],[0,BLDG]]
    fp_holes = [[[VX1,VY1],[VX2,VY1],[VX2,VY2],[VX1,VY2]]]

    # Anchor at south-band midpoint
    cx = BLDG / 2
    cy_s = (0 + BANK_HD + CHAIN_DEPTH + VY1 - BANK_HD) / 2   # mid of S-band
    anchor = (cx - BANK_HW, cy_s - BANK_HD, cx + BANK_HW, cy_s + BANK_HD)

    snap_s_lo = 0 + BANK_HD + CHAIN_DEPTH
    snap_s_hi = VY1 - BANK_HD
    snap_zone = (0, snap_s_lo - BANK_HD, BLDG, snap_s_hi + BANK_HD)

    _info("80×80m building, void at [{:.0f},{:.0f}]→[{:.0f},{:.0f}]".format(VX1,VY1,VX2,VY2))
    _info("S-band: [{:.0f},{:.0f}] — FEASIBLE ({:.0f}mm range)".format(
        snap_s_lo, snap_s_hi, snap_s_hi-snap_s_lo))
    _info("Anchor intent: {}".format(_box_str(anchor)))

    logs = []
    t0 = time.time()
    result = find_layout_for_set(
        anchor_bounds    = anchor,
        fire_lift_size   = (FL_SHAFT_D, FL_SHAFT_D),
        lobby_size       = (EW_LB_DX, 3200),
        staircase_size   = (4050, SD_NAT),
        footprint_pts    = fp_pts,
        footprint_holes  = fp_holes,
        allowed_sides    = ["N", "S"],
        anchor_snap_zone = snap_zone,
        log_fn           = logs.append,
    )
    elapsed = (time.time() - t0) * 1000

    if result is None:
        _fail("OR-Tools INFEASIBLE in {:.0f}ms — unexpected for 80m building".format(elapsed))
        for ln in logs[-15:]:
            print("    LOG: {}".format(ln))
    else:
        _ok("SOLVED in {:.0f}ms".format(elapsed))
        solved_anchor = result.get("solved_anchor_bounds")
        if solved_anchor:
            solved_cy = (solved_anchor[1]+solved_anchor[3])/2
            print("    Solved anchor cy={:.0f}mm  (intended {:.0f}mm, drift {:.0f}mm)".format(
                solved_cy, cy_s, abs(solved_cy-cy_s)))
        for mname in ("fire_lift","lobby","staircase"):
            mb = result.get(mname)
            if mb: print("    {}: {}".format(mname.ljust(12), _box_str(mb)))
        print("    attach_side={} chain_order={}".format(
            result.get("attach_side"), result.get("chain_order")))

    # Log summary
    _info("Full OR-Tools log ({} lines):".format(len(logs)))
    for ln in logs:
        print("    {}".format(ln))


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 6 — Exact replay of failed 60x60m build (LCB anchor, real snap zone)
#  Anchor from log: (25550,9600,34450,19000) w=8900 d=9400
#  Snap zone from log: (0,7600,60000,20000)
#  Modules from log: fl=3200x3200 lb=5000x4700 st=4050x7600
#  Expected: OR-Tools finds compact rectangle (side=S, all modules below anchor)
# ═════════════════════════════════════════════════════════════════════════════

def test6_exact_failed_scenario():
    _sep("TEST 6 — Exact replay: 60x60m, LCB anchor at (25550,9600,34450,19000)")

    try:
        from revit_mcp.core_layout_engine import find_layout_for_set
    except ImportError as e:
        _fail("Cannot import core_layout_engine: {}".format(e))
        return

    # Exact values from the failed build log
    anchor = (25550.0, 9600.0, 34450.0, 19000.0)   # LCB: w=8900 d=9400
    snap_zone = (0.0, 7600.0, 60000.0, 20000.0)     # S-band snap zone from log
    fp_pts = [[0,0],[60000,0],[60000,60000],[0,60000]]
    fp_holes = [[[20000,20000],[40000,20000],[40000,40000],[20000,40000]]]

    # Modules from log (SCDF RAG: lobby 5000x4700)
    fl_size = (3200, 3200)
    lb_size = (5000, 4700)
    st_size = (4050, 7600)

    _info("Anchor: {}".format(_box_str(anchor)))
    _info("Snap zone: {}".format(_box_str(snap_zone)))
    _info("Modules: fl={}x{} lb={}x{} st={}x{}".format(
        fl_size[0], fl_size[1], lb_size[0], lb_size[1], st_size[0], st_size[1]))
    _info("S-band: anchor_y1 in [{:.0f},{:.0f}]mm — modules go south (below anchor)".format(
        7600, 10600))
    _info("S-side clearance: anchor_y1_min=7600mm, staircase needs 7600mm → staircase_y1=0 (exactly at footprint edge)")

    logs = []
    t0 = time.time()
    result = find_layout_for_set(
        anchor_bounds    = anchor,
        fire_lift_size   = fl_size,
        lobby_size       = lb_size,
        staircase_size   = st_size,
        footprint_pts    = fp_pts,
        footprint_holes  = fp_holes,
        allowed_sides    = ["N", "S"],
        anchor_snap_zone = snap_zone,
        preferred_side   = "S",
        log_fn           = logs.append,
    )
    elapsed = (time.time() - t0) * 1000

    if result is None:
        _fail("OR-Tools returned None — layout INFEASIBLE (this is the bug we are fixing)")
        _info("Full log ({} lines):".format(len(logs)))
        for ln in logs:
            print("    {}".format(ln))
    else:
        _ok("SOLVED in {:.0f}ms — attach_side={}".format(elapsed, result.get("attach_side")))
        solved_anchor = result.get("solved_anchor_bounds")
        if solved_anchor:
            print("    Solved anchor: {}".format(_box_str(solved_anchor)))
        for mname in ("fire_lift", "lobby", "staircase"):
            mb = result.get(mname)
            if mb:
                overlaps_void = not (mb[2] <= 20000 or mb[0] >= 40000 or mb[3] <= 20000 or mb[1] >= 40000)
                out_of_fp = mb[0] < 0 or mb[1] < 0 or mb[2] > 60000 or mb[3] > 60000
                status = "VOID OVERLAP" if overlaps_void else ("OUT OF FP" if out_of_fp else "OK")
                print("    {} : {}  [{}]".format(mname.ljust(12), _box_str(mb), status))
                if overlaps_void: _fail("{} overlaps void".format(mname))
                elif out_of_fp: _fail("{} outside footprint".format(mname))
                else: _ok("{} placement valid".format(mname))
        _info("Log ({} lines):".format(len(logs)))
        for ln in logs:
            print("    {}".format(ln))


# ═════════════════════════════════════════════════════════════════════════════
#  TEST 7 — Centred footprint: [-30000,-30000]→[30000,30000], void [-10000,-10000]→[10000,10000]
#  Exact scenario from second failed log: anchor=(-4450,-19900,4450,-11000)
#  snap_zone=(-30000,-23600,30000,-10000), fl=3200x3200 lb=3200x3200 st=3450x6400
# ═════════════════════════════════════════════════════════════════════════════

def test7_centred_footprint():
    _sep("TEST 7 — Centred footprint [-30000,-30000]→[30000,30000], anchor south band")

    try:
        from revit_mcp.core_layout_engine import find_layout_for_set
    except ImportError as e:
        _fail("Cannot import core_layout_engine: {}".format(e))
        return

    # Exact values from the second failed build log
    anchor    = (-4450.0, -19900.0, 4450.0, -11000.0)   # w=8900 d=8900
    snap_zone = (-30000.0, -23600.0, 30000.0, -10000.0)  # S-band snap zone
    fp_pts    = [[-30000,-30000],[30000,-30000],[30000,30000],[-30000,30000]]
    fp_holes  = [[[-10000,-10000],[10000,-10000],[10000,10000],[-10000,10000]]]

    # Modules from log (no SCDF override this run)
    fl_size = (3200, 3200)
    lb_size = (3200, 3200)
    st_size = (3450, 6400)

    _info("Anchor: {}  w={} d={}".format(_box_str(anchor), int(anchor[2]-anchor[0]), int(anchor[3]-anchor[1])))
    _info("Snap zone: {}".format(_box_str(snap_zone)))
    _info("Modules: fl={}x{} lb={}x{} st={}x{}".format(
        fl_size[0],fl_size[1], lb_size[0],lb_size[1], st_size[0],st_size[1]))
    _info("S-band: anchor y1 in [-23600,-18900] → modules go south below anchor (toward y=-30000)")

    logs = []
    t0 = time.time()
    result = find_layout_for_set(
        anchor_bounds    = anchor,
        fire_lift_size   = fl_size,
        lobby_size       = lb_size,
        staircase_size   = st_size,
        footprint_pts    = fp_pts,
        footprint_holes  = fp_holes,
        allowed_sides    = ["N", "S"],
        anchor_snap_zone = snap_zone,
        preferred_side   = "S",
        log_fn           = logs.append,
    )
    elapsed = (time.time() - t0) * 1000

    if result is None:
        _fail("OR-Tools returned None — INFEASIBLE (Rule 4/4b Y-axis still blocking)")
        _info("Full log ({} lines):".format(len(logs)))
        for ln in logs: print("    {}".format(ln))
    else:
        _ok("SOLVED in {:.0f}ms — attach_side={}".format(elapsed, result.get("attach_side")))
        solved_anchor = result.get("solved_anchor_bounds")
        if solved_anchor:
            print("    Solved anchor: {}".format(_box_str(solved_anchor)))
        for mname in ("fire_lift", "lobby", "staircase"):
            mb = result.get(mname)
            if mb:
                void_ovlp = not (mb[2] <= -10000 or mb[0] >= 10000 or mb[3] <= -10000 or mb[1] >= 10000)
                out_of_fp = mb[0] < -30000 or mb[1] < -30000 or mb[2] > 30000 or mb[3] > 30000
                tag = "VOID" if void_ovlp else ("OUT_FP" if out_of_fp else "OK")
                print("    {} : {}  [{}]".format(mname.ljust(12), _box_str(mb), tag))
                if void_ovlp or out_of_fp:
                    _fail("{} invalid placement".format(mname))
                else:
                    _ok("{} valid".format(mname))
        _info("Log ({} lines):".format(len(logs)))
        for ln in logs: print("    {}".format(ln))


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n" + "#" * 70)
    print("  PLACEMENT PIPELINE DIAGNOSTIC TESTS")
    print("  Scenario: 20-storey 60x60m courtyard, 5 lifts, SCDF RAG active")
    print("  Chain depth = {}mm (fl {} + lobby {} + stair {})".format(
        CHAIN_DEPTH, FL_SHAFT_D, EW_LB_DX, SD_NAT))
    print("  Bank = {}x{}mm".format(BANK_W, BANK_D))
    print("#" * 70)

    test1_gemini_intent()
    test2_snap_zone()
    test3_anchor_freedom()
    test4_module_placement()
    test5_working_scenario()
    test6_exact_failed_scenario()
    test7_centred_footprint()

    print("\n" + "=" * 70)
    print("  DONE — check PASS/FAIL/INFO lines above for diagnosis")
    print("=" * 70 + "\n")
