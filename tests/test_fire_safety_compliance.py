# -*- coding: utf-8 -*-
import math
import sys
import os

from revit_mcp.fire_safety_logic import calculate_fire_safety_requirements, _check_radius_coverage
from revit_mcp import staircase_logic

def test_coverage():
    floor_dims = [(60000, 60000)] # 60m x 60m floor plate
    core_center = (0, 0)
    lift_bounds = (-5000, -5000, 5000, 5000) # 10m x 10m lift core
    typical_h = 4000
    preset_fs = {
        "max_travel_distance": 60000,
        "staircase_spec": {"riser": 150, "tread": 300, "width_of_flight": 1500, "landing_width": 1800}
    }
    num_lifts = 4
    lobby_w = 3000
    
    print("Testing 60x60m floor plate...")
    sets = calculate_fire_safety_requirements(floor_dims, core_center, lift_bounds, typical_h, preset_fs, num_lifts, lobby_w)
    
    print("Core Sets placed:")
    for s in sets:
        print("  - Type: {}, Position: {}".format(s["type"], s["pos"]))
        
    # Check Fire Lift Coverage (60m radius)
    fire_lift_pos = [s["pos"] for s in sets if s["type"] == "FIRE_LIFT"]
    fire_lift_ok = _check_radius_coverage(fire_lift_pos, floor_dims, 60000)
    print("Fire Lift Coverage (60m Radius): {}".format("PASS" if fire_lift_ok else "FAIL"))
    
    # Check Staircase Coverage (60m Travel Distance - usually 2 staircases)
    stair_pos = [s["pos"] for s in sets]
    stair_ok = staircase_logic._check_travel_distance(stair_pos, floor_dims, 60000, num_required=2)
    print("Staircase Coverage (60m Travel Dist): {}".format("PASS" if stair_ok else "FAIL"))

    # Test a much larger floor plate
    print("\nTesting 120x120m floor plate...")
    floor_dims_large = [(120000, 120000)]
    sets_large = calculate_fire_safety_requirements(floor_dims_large, core_center, lift_bounds, typical_h, preset_fs, num_lifts, lobby_w)
    
    print("Core Sets placed: {}".format(len(sets_large)))
    for s in sets_large:
         print("  - Type: {}, Position: {}".format(s["type"], s["pos"]))

    fire_lift_pos_large = [s["pos"] for s in sets_large if s["type"] == "FIRE_LIFT"]
    fire_lift_ok_large = _check_radius_coverage(fire_lift_pos_large, floor_dims_large, 60000)
    print("Fire Lift Coverage (60m Radius): {}".format("PASS" if fire_lift_ok_large else "FAIL"))
    
    stair_pos_large = [s["pos"] for s in sets_large]
    stair_ok_large = staircase_logic._check_travel_distance(stair_pos_large, floor_dims_large, 60000, num_required=2)
    print("Staircase Coverage (60m Travel Dist): {}".format("PASS" if stair_ok_large else "FAIL"))

if __name__ == "__main__":
    test_coverage()
