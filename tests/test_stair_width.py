# -*- coding: utf-8 -*-
"""
Test: staircase count (travel distance) + width calculation (SCDF Table 2.2A)
for an 80x100m, 30-storey commercial office building.
"""
import sys
import math
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                "..", "GeminiMCP.extension"))

from revit_mcp import fire_safety_logic, lift_logic, staircase_logic

# ── Building inputs ────────────────────────────────────────────────────────────
FLOOR_W_MM    = 80_000
FLOOR_L_MM    = 100_000
NUM_STOREYS   = 30
TYPICAL_H_MM  = 4200   # commercial_office preset
LOBBY_W       = 3000
NUM_LIFTS     = 8      # representative for 30-storey; exact value doesn't change stair count

# RAG compliance_parameters (as returned by Gemini for SCDF)
CP = {
    "occupant_load_factor_m2":  10.0,
    "persons_per_unit_width":   60,
    "exit_width_per_unit_mm":   500,
    "stair_min_flight_width_mm": 1200,
}

CORE_AREA_RATIO = [0.20, 0.25]  # commercial_office preset

# ── Step 1: compute lift core bounds ──────────────────────────────────────────
layout = lift_logic.get_total_core_layout(NUM_LIFTS, lobby_width=LOBBY_W)
lift_bounds = (
    -layout["total_w"] / 2.0,
    -layout["total_d"] / 2.0,
     layout["total_w"] / 2.0,
     layout["total_d"] / 2.0,
)
print("Lift core bounds (mm): xmin={:.0f}  ymin={:.0f}  xmax={:.0f}  ymax={:.0f}".format(*lift_bounds))

# ── Step 2: determine staircase count from travel distance ────────────────────
floor_dims = [(FLOOR_W_MM, FLOOR_L_MM)] * NUM_STOREYS
safety_sets = fire_safety_logic.calculate_fire_safety_requirements(
    floor_dims,
    core_center_mm=[0.0, 0.0],
    lift_core_bounds_mm=lift_bounds,
    typical_floor_height_mm=TYPICAL_H_MM,
    _preset_fs={},
    num_lifts=NUM_LIFTS,
    lobby_width=LOBBY_W,
    compliance_overrides={}
)
num_stairs = len(safety_sets)
print("\nStaircase sets required: {}".format(num_stairs))
for i, s in enumerate(safety_sets):
    print("  Stair {}: pos=({:.0f}, {:.0f})  type={}  perimeter={}".format(
        i + 1, s["pos"][0], s["pos"][1], s["type"], s.get("is_perimeter", False)))

# ── Step 3: width calculation (SCDF Table 2.2A) ───────────────────────────────
occupant_load_factor = CP["occupant_load_factor_m2"]
persons_per_unit     = CP["persons_per_unit_width"]
exit_width_per_unit  = CP["exit_width_per_unit_mm"]
min_flight_width     = CP["stair_min_flight_width_mm"]

largest_area_m2  = (FLOOR_W_MM * FLOOR_L_MM) / 1e6
core_ratio       = sum(CORE_AREA_RATIO) / 2.0
functional_area  = largest_area_m2 * (1.0 - core_ratio)
total_occupancy  = functional_area / occupant_load_factor
total_unit_w     = math.ceil(total_occupancy / persons_per_unit)
units_per_stair  = math.ceil(total_unit_w / num_stairs)
calc_width       = int(units_per_stair * exit_width_per_unit)
final_width      = max(calc_width, int(min_flight_width), 1000)

print("\nWidth calculation:")
print("  Floor area:        {:.0f} m²".format(largest_area_m2))
print("  Core ratio:        {:.1%}".format(core_ratio))
print("  Functional area:   {:.0f} m²".format(functional_area))
print("  Total occupancy:   {:.0f} persons".format(total_occupancy))
print("  Total unit widths: {}".format(total_unit_w))
print("  Num stairs:        {}".format(num_stairs))
print("  Units per stair:   {}".format(units_per_stair))
print("  Calc width:        {} mm".format(calc_width))
print("  Final width:       {} mm  (after clamp to min {})".format(final_width, min_flight_width))

# ── Assertions ────────────────────────────────────────────────────────────────
print("\n--- Assertions ---")

assert num_stairs == 4, "Expected 4 staircases (2 central + 2 perimeter), got {}".format(num_stairs)
print("PASS  staircase count = 4 (2 central + 2 perimeter)")

assert final_width == 1500, "Expected 1500mm flight width, got {}mm".format(final_width)
print("PASS  flight width = 1500mm")

print("\nAll tests passed.")
