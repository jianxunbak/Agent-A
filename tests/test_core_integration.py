# -*- coding: utf-8 -*-
import unittest
import sys
import os

from revit_mcp.fire_safety_logic import generate_fire_safety_manifest, calculate_fire_safety_requirements
from revit_mcp.staircase_logic import get_stair_run_data

class TestCoreIntegration(unittest.TestCase):
    def test_full_core_generation_pipeline(self):
        # Setup mock data for a standard 10-storey building
        floor_dims = [(50000, 50000)]
        core_center = (0, 0)
        lift_bounds = (-6000, -4000, 6000, 4000) # 12x8m lift core
        typical_h = 4200
        preset_fs = {
            "max_travel_distance": 60000,
            "staircase_spec": {"riser": 150, "tread": 300, "width_of_flight": 1500, "landing_width": 1800}
        }
        num_lifts = 6
        lobby_w = 3000
        
        # 1. Calculate Requirements
        safety_sets = calculate_fire_safety_requirements(
            floor_dims, core_center, lift_bounds, typical_h, preset_fs, num_lifts, lobby_w
        )
        self.assertGreater(len(safety_sets), 0)
        
        # 2. Generate Manifest (This was where NameError/TypeError occurred)
        levels_manifest = [{"id": "L1", "elevation": 0.0}, {"id": "L2", "elevation": 4200.0}]
        stair_spec = preset_fs["staircase_spec"]
        
        try:
            manifest = generate_fire_safety_manifest(
                safety_sets, levels_manifest, stair_spec, typical_h, preset_fs, lift_bounds, num_lifts, lobby_w
            )
        except Exception as e:
            self.fail("generate_fire_safety_manifest failed with: {}".format(e))
            
        self.assertIn("walls", manifest)
        self.assertIn("sub_boundaries", manifest)
        self.assertIn("stair_overrides", manifest)
        
        # 3. Verify Stair Run Geometry Integration
        positions = [(sc[0], sc[1]) for sc in manifest["stair_centers"]]
        rotated_indices = [i for i, sc in enumerate(manifest["stair_centers"]) if sc[2]]
        stair_overrides = manifest["stair_overrides"]
        
        try:
            run_data = get_stair_run_data(
                positions, levels_manifest, 4000, stair_spec, typical_h, lift_bounds,
                floor_dims_mm=floor_dims, num_lifts=num_lifts, lobby_width=lobby_w,
                rotated_indices=rotated_indices, base_y_overrides=stair_overrides
            )
        except Exception as e:
            self.fail("get_stair_run_data failed with: {}".format(e))
            
        self.assertEqual(len(run_data), len(positions))

if __name__ == "__main__":
    unittest.main()
