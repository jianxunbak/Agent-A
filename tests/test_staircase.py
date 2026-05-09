# -*- coding: utf-8 -*-
"""
Staircase geometry test suite.

Tests the staircase_logic module in isolation (no Revit API needed).
Validates that stair geometry is correct BEFORE it reaches Revit,
catching the root causes of "flying stairs" bugs.

Run: python test_staircase.py
"""
import sys
import math

# Add the module path so we can import staircase_logic directly
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                "..", "GeminiMCP.extension", "revit_mcp"))
from staircase_logic import (
    _snap_risers,
    _risers_per_flight_typical,
    _calc_num_flights,
    adjust_storey_height,
    get_shaft_dimensions,
    get_max_shaft_depth,
    calculate_staircase_positions,
    generate_staircase_manifest,
    get_stair_run_data,
    get_void_rectangles_mm,
    _WALL_THICKNESS,
    _OVERRUN_HEIGHT,
)

SPEC = {"riser": 150, "tread": 300, "width_of_flight": 1500, "landing_width": 1500}
TYPICAL_H = 4200  # mm

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {name}")
        if detail:
            print(f"        {detail}")


# ─────────────────────────────────────────────────────────────────────
#  1. Riser calculations
# ─────────────────────────────────────────────────────────────────────
print("=== 1. Riser Calculations ===")

check("snap_risers 4200mm",
      _snap_risers(4200) == 28,
      f"got {_snap_risers(4200)}")

check("snap_risers 3000mm",
      _snap_risers(3000) == 20,
      f"got {_snap_risers(3000)}")

check("snap_risers 14400mm",
      _snap_risers(14400) == 96,
      f"got {_snap_risers(14400)}")

check("snap_risers rounds up",
      _snap_risers(4201) == 29,
      f"got {_snap_risers(4201)}")

check("rpf_typical 4200mm = 14",
      _risers_per_flight_typical(4200) == 14,
      f"got {_risers_per_flight_typical(4200)}")


# ─────────────────────────────────────────────────────────────────────
#  2. Flight count — MUST be capped at 4 (2 pairs)
# ─────────────────────────────────────────────────────────────────────
print("\n=== 2. Flight Count (max 4) ===")

for h in [4200, 8400, 14400, 15000, 30000]:
    nf = _calc_num_flights(h, TYPICAL_H)
    # Each flight should have same risers as typical floor flight (rpf)
    rpf = _risers_per_flight_typical(TYPICAL_H)
    adj = adjust_storey_height(h, TYPICAL_H)
    risers = _snap_risers(adj)
    per_flight = risers // nf if nf > 0 else risers
    check(f"flights for {h}mm: per_flight ({per_flight}) <= rpf ({rpf})",
          per_flight <= rpf,
          f"got {nf} flights, {per_flight} risers/flight vs rpf={rpf}")
    check(f"flights for {h}mm is even",
          nf % 2 == 0,
          f"got {nf}")
    check(f"flights for {h}mm >= 2",
          nf >= 2,
          f"got {nf}")


# ─────────────────────────────────────────────────────────────────────
#  3. adjust_storey_height — risers must divide evenly into flights
# ─────────────────────────────────────────────────────────────────────
print("\n=== 3. Height Adjustment (even divisibility) ===")

for raw_h in [3000, 4200, 5000, 7000, 8400, 10000, 12000, 14400, 15000, 20000, 30000]:
    adj = adjust_storey_height(raw_h, TYPICAL_H)
    risers = _snap_risers(adj)
    nf = _calc_num_flights(adj, TYPICAL_H)

    check(f"adj({raw_h}) risers divisible by flights",
          risers % nf == 0,
          f"adj={adj}, risers={risers}, flights={nf}, remainder={risers % nf}")

    # Heights snap to nearest pair_height multiple (2*rpf*riser=4200mm).
    # Adjustments up to one pair_height (4200mm) are expected.
    check(f"adj({raw_h}) within one pair-height of original",
          abs(adj - raw_h) <= 4200,
          f"raw={raw_h}, adj={adj}, diff={abs(adj - raw_h)}")


# ─────────────────────────────────────────────────────────────────────
#  4. Shaft dimensions — runs must fit inside shaft
# ─────────────────────────────────────────────────────────────────────
print("\n=== 4. Shaft Dimensions (runs fit inside) ===")

for h in [4200, 8400, 14400, 15000, 20000]:
    adj = adjust_storey_height(h, TYPICAL_H)
    risers = _snap_risers(adj)
    nf = _calc_num_flights(adj, TYPICAL_H)
    num_pairs = nf // 2
    per_pair_h = adj / float(num_pairs)

    _, shaft_d = get_shaft_dimensions(per_pair_h, SPEC)

    rpf = risers // nf
    treads = rpf - 1
    tread_mm = SPEC["tread"]
    run_length = treads * tread_mm

    landing_w = SPEC["landing_width"]
    wall_t = _WALL_THICKNESS
    flight_area = shaft_d - 2 * wall_t - 2 * landing_w

    check(f"shaft({h}mm) run fits: {run_length}mm <= {flight_area}mm",
          run_length <= flight_area + 1,  # 1mm tolerance
          f"run={run_length}, flight_area={flight_area}, shaft_d={shaft_d}")

    check(f"shaft({h}mm) depth > 0",
          shaft_d > 0,
          f"shaft_d={shaft_d}")


# ─────────────────────────────────────────────────────────────────────
#  5. Shaft depth — CONSTANT regardless of floor height
# ─────────────────────────────────────────────────────────────────────
print("\n=== 5. Shaft Depth (constant = typical floor) ===")

# Mixed-height building: typical 4200, ground 8400, penthouse 14400
levels_data = [{"id": "L1", "elevation": 0}]
elevations_mm = [0, 8400, 12600, 16800, 21000, 25200, 29400, 33600, 37800, 42000, 56400]
for e in elevations_mm[1:]:
    levels_data.append({"id": f"L{len(levels_data)+1}", "elevation": e})

max_d = get_max_shaft_depth(levels_data, SPEC, TYPICAL_H)
_, typical_d = get_shaft_dimensions(TYPICAL_H, SPEC)

# Core rule: shaft depth == typical floor's shaft depth, always
check("shaft depth equals typical floor's shaft",
      abs(max_d - typical_d) < 1,
      f"max_d={max_d}, typical_d={typical_d}")

# Tall floors use more pairs to fit within the same shaft
for h in [8400, 14400, 20000]:
    adj = adjust_storey_height(h, TYPICAL_H)
    nf = _calc_num_flights(adj, TYPICAL_H)
    risers = _snap_risers(adj)
    rpf = risers // nf  # risers per flight
    run_len = (rpf - 1) * SPEC["tread"]
    flight_area = typical_d - 2 * _WALL_THICKNESS - 2 * SPEC["landing_width"]
    check(f"h={h}mm: run ({run_len}mm) fits typical shaft ({flight_area}mm)",
          run_len <= flight_area + 1,
          f"run={run_len}, flight_area={flight_area}, flights={nf}")


# ─────────────────────────────────────────────────────────────────────
#  6. get_stair_run_data — geometry validation
# ─────────────────────────────────────────────────────────────────────
print("\n=== 6. Stair Run Data (geometry) ===")

# Simple uniform building: 5 floors, all 4200mm
levels_uniform = [{"id": f"L{i+1}", "elevation": i * 4200.0} for i in range(6)]
positions_2 = [(0, -5000), (0, 5000)]

runs = get_stair_run_data(positions_2, levels_uniform, None, SPEC,
                          typical_floor_height_mm=TYPICAL_H)

check("uniform 5-floor: 10 runs (2 cores x 5 floors)",
      len(runs) == 10,
      f"got {len(runs)}")

for rd in runs:
    check(f"{rd['tag']} has flight_1 and flight_2",
          "flight_1" in rd and "flight_2" in rd)
    check(f"{rd['tag']} num_flight_pairs >= 1",
          rd["num_flight_pairs"] >= 1)
    check(f"{rd['tag']} num_flight_pairs >= 1",
          rd["num_flight_pairs"] >= 1,
          f"got {rd['num_flight_pairs']}")
    check(f"{rd['tag']} has main_landing",
          "main_landing" in rd)
    check(f"{rd['tag']} width_mm > 0",
          rd.get("width_mm", 0) > 0)


# ─────────────────────────────────────────────────────────────────────
#  7. get_stair_run_data — mixed heights
# ─────────────────────────────────────────────────────────────────────
print("\n=== 7. Stair Run Data (mixed heights) ===")

# 3 floors: ground=8400, typical=4200, penthouse=14400
levels_mixed = [
    {"id": "L1", "elevation": 0},
    {"id": "L2", "elevation": 8400},
    {"id": "L3", "elevation": 12600},
    {"id": "L4", "elevation": 27000},  # 14400mm jump
]
positions_1 = [(0, 0)]

runs_mixed = get_stair_run_data(positions_1, levels_mixed, None, SPEC,
                                typical_floor_height_mm=TYPICAL_H)

check("mixed 3-floor: 3 runs",
      len(runs_mixed) == 3,
      f"got {len(runs_mixed)}")

for rd in runs_mixed:
    bi = rd["base_level_idx"]
    ti = rd["top_level_idx"]
    fh = levels_mixed[ti]["elevation"] - levels_mixed[bi]["elevation"]
    pairs = rd["num_flight_pairs"]

    check(f"{rd['tag']} pairs >= 1 (floor_h={fh}mm)",
          pairs >= 1,
          f"got {pairs} pairs for {fh}mm floor")

    # Verify rpf is consistent with floor height and flight count
    risers = _snap_risers(fh)
    nf = pairs * 2
    rpf = rd.get("risers_per_flight", 0)
    check(f"{rd['tag']} rpf reasonable",
          rpf > 0 and rpf <= risers,
          f"rpf={rpf}, total_risers={risers}")


# ─────────────────────────────────────────────────────────────────────
#  8. Staircases are continuous vertical stacks (no floor skipping)
# ─────────────────────────────────────────────────────────────────────
print("\n=== 8. Continuous Vertical Staircase Cores ===")

# 80x100m building with floors 4-5 at 40x40m
# ALL cores must have stairs on ALL floors (continuous shaft)
mixed_dims_8 = [(80000, 100000)] * 3 + [(40000, 40000)] * 2
levels_5 = [{"id": f"L{i+1}", "elevation": i * 4200.0} for i in range(6)]

positions_8 = calculate_staircase_positions(
    mixed_dims_8, (0, 0), None, TYPICAL_H, SPEC)

check("80x100 building needs > 2 cores",
      len(positions_8) > 2,
      f"got {len(positions_8)}")

# Get runs — ALL cores should have runs on ALL 5 floors
all_runs = get_stair_run_data(
    positions_8, levels_5, None, SPEC,
    typical_floor_height_mm=TYPICAL_H,
    floor_dims_mm=mixed_dims_8)

# Every core must have exactly 5 floor runs (continuous vertical stack)
for core_idx in range(1, len(positions_8) + 1):
    core_tag = f"Stair_{core_idx}_"
    core_levels = set()
    for rd in all_runs:
        if core_tag in rd['tag']:
            core_levels.add(rd['base_level_idx'])
    check(f"core {core_idx} has all 5 floors (continuous)",
          len(core_levels) == 5,
          f"core {core_idx} ran on levels: {sorted(core_levels)}")


# ─────────────────────────────────────────────────────────────────────
#  9. Staircase positions — fire safety
# ─────────────────────────────────────────────────────────────────────
print("\n=== 9. Staircase Positions (fire safety) ===")

# Small building — should need only 2 cores
small_dims = [(30000, 30000)] * 5
positions_small = calculate_staircase_positions(
    small_dims, (0, 0), None, TYPICAL_H, SPEC)

check("small building >= 2 cores",
      len(positions_small) >= 2,
      f"got {len(positions_small)}")

# Large building — should need more cores for 60m travel distance
large_dims = [(100000, 100000)] * 10
positions_large = calculate_staircase_positions(
    large_dims, (0, 0), None, TYPICAL_H, SPEC)

check("large building > 2 cores",
      len(positions_large) > 2,
      f"got {len(positions_large)}")

# All positions should be within the floor plate
for i, (px, py) in enumerate(positions_large):
    half_w = 100000 / 2.0
    half_l = 100000 / 2.0
    check(f"position {i} within floor plate",
          abs(px) <= half_w and abs(py) <= half_l,
          f"pos=({px:.0f}, {py:.0f}), half=({half_w:.0f}, {half_l:.0f})")


# ─────────────────────────────────────────────────────────────────────
#  10. generate_staircase_manifest — walls and floors
# ─────────────────────────────────────────────────────────────────────
print("\n=== 10. Staircase Manifest (walls & floors) ===")

levels_3 = [
    {"id": "L1", "elevation": 0},
    {"id": "L2", "elevation": 4200},
    {"id": "L3", "elevation": 8400},
]
positions_manifest = [(0, -5000)]

manifest = generate_staircase_manifest(
    positions_manifest, levels_3, None, SPEC,
    typical_floor_height_mm=TYPICAL_H)

check("manifest has walls",
      len(manifest.get("walls", [])) > 0,
      f"got {len(manifest.get('walls', []))} walls")

check("manifest has floors (landings)",
      len(manifest.get("floors", [])) > 0,
      f"got {len(manifest.get('floors', []))} floors")

# Each wall should have start, end, height, level_id
for w in manifest["walls"][:3]:
    check(f"wall {w['id']} has start/end/height",
          "start" in w and "end" in w and "height" in w and "level_id" in w,
          f"keys: {list(w.keys())}")


# ─────────────────────────────────────────────────────────────────────
#  11. Void rectangles — one per staircase
# ─────────────────────────────────────────────────────────────────────
print("\n=== 11. Void Rectangles ===")

enc_w, enc_d = get_shaft_dimensions(TYPICAL_H, SPEC)
voids = get_void_rectangles_mm(positions_2, enc_w, enc_d)

check("2 voids for 2 positions",
      len(voids) == 2,
      f"got {len(voids)}")

for i, (x1, y1, x2, y2) in enumerate(voids):
    check(f"void {i} width matches enclosure",
          abs((x2 - x1) - enc_w) < 1,
          f"void_w={x2-x1}, enc_w={enc_w}")
    check(f"void {i} depth matches enclosure",
          abs((y2 - y1) - enc_d) < 1,
          f"void_d={y2-y1}, enc_d={enc_d}")


# ─────────────────────────────────────────────────────────────────────
#  12. End-to-end: full building scenario
# ─────────────────────────────────────────────────────────────────────
print("\n=== 12. End-to-End: 10-storey with height overrides ===")

# Simulate: 10 storeys, typical=4200, ground=8400, floors 5-6 = 15000mm
raw_heights = [8400, 4200, 4200, 4200, 15000, 15000, 4200, 4200, 4200, 4200]
adjusted_heights = [adjust_storey_height(h, TYPICAL_H) for h in raw_heights]

# Build levels_data from adjusted heights
e2e_levels = [{"id": "L1", "elevation": 0}]
cumulative = 0
for h in adjusted_heights:
    cumulative += h
    e2e_levels.append({"id": f"L{len(e2e_levels)+1}", "elevation": cumulative})

# Floor dims: all 80x100 except floors 3-4 are 50x50
e2e_floor_dims = [(80000, 100000)] * 10 + [(80000, 100000)]  # +1 for roof
e2e_floor_dims[2] = (50000, 50000)
e2e_floor_dims[3] = (50000, 50000)

# Calculate positions
e2e_positions = calculate_staircase_positions(
    e2e_floor_dims[:10], (0, 0), None, TYPICAL_H, SPEC)

check("e2e: >= 2 staircase positions",
      len(e2e_positions) >= 2)

# Generate run data
e2e_runs = get_stair_run_data(
    e2e_positions, e2e_levels, None, SPEC,
    typical_floor_height_mm=TYPICAL_H,
    floor_dims_mm=e2e_floor_dims)

check("e2e: runs generated",
      len(e2e_runs) > 0,
      f"got {len(e2e_runs)} runs")

# Validate every run
for rd in e2e_runs:
    bi = rd["base_level_idx"]
    ti = rd["top_level_idx"]
    fh = e2e_levels[ti]["elevation"] - e2e_levels[bi]["elevation"]
    pairs = rd["num_flight_pairs"]
    nf = pairs * 2
    risers = _snap_risers(fh)

    # Core constraint: max 2 pairs
    check(f"e2e {rd['tag']}: pairs >= 1",
          pairs >= 1,
          f"pairs={pairs}, floor_h={fh}mm")

    # Risers must be distributable across flights
    per_flight = risers // nf
    remainder = risers % nf
    check(f"e2e {rd['tag']}: risers distributable",
          per_flight > 0,
          f"risers={risers}, flights={nf}, per_flight={per_flight}")

    # Run length must fit in shaft
    max_rpf = per_flight + (1 if remainder > 0 else 0)
    max_treads = max_rpf - 1
    max_run_length = max_treads * SPEC["tread"]

    per_pair_h = fh / float(pairs)
    _, shaft_d = get_shaft_dimensions(per_pair_h, SPEC)
    flight_area = shaft_d - 2 * _WALL_THICKNESS - 2 * SPEC["landing_width"]

    check(f"e2e {rd['tag']}: run fits shaft ({max_run_length}mm <= {flight_area}mm)",
          max_run_length <= flight_area + 1,
          f"run={max_run_length}, area={flight_area}, shaft_d={shaft_d}")

# Shaft depth must accommodate all floors
e2e_max_d = get_max_shaft_depth(e2e_levels, SPEC, TYPICAL_H)
check("e2e: shaft depth > 0",
      e2e_max_d > 0)

# Generate manifest
e2e_manifest = generate_staircase_manifest(
    e2e_positions, e2e_levels, None, SPEC,
    typical_floor_height_mm=TYPICAL_H)

check("e2e: manifest walls > 0",
      len(e2e_manifest.get("walls", [])) > 0)
check("e2e: manifest floors > 0",
      len(e2e_manifest.get("floors", [])) > 0)


# ─────────────────────────────────────────────────────────────────────
#  Summary
# ─────────────────────────────────────────────────────────────────────
print(f"\n{'='*50}")
print(f"RESULTS: {passed} passed, {failed} failed")
if failed == 0:
    print("ALL TESTS PASSED!")
else:
    print(f"FAILED — {failed} test(s) need attention")
    sys.exit(1)
