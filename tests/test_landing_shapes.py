import unittest
from revit_mcp.staircase_logic import generate_staircase_manifest

class TestLandingShapes(unittest.TestCase):
    def test_middle_floor_landing_rectangular(self):
        # 3 floors, typical height 4000. L2 is Middle Floor.
        levels_data = [
            {"id": "L1", "elevation": 0.0},
            {"id": "L2", "elevation": 4000.0},
            {"id": "L3", "elevation": 8000.0}
        ]
        manifest = generate_staircase_manifest([(0, 0)], levels_data, typical_floor_height_mm=4000)
        
        # Intermediate landings (i=1 for L2)
        landings = [f for f in manifest["floors"] if "_L2_MainLanding" in f["id"]]
        self.assertEqual(len(landings), 1)
        pts = landings[0]["points"]
        # Rectangular landing should have 4 points
        self.assertEqual(len(pts), 4, f"L2 landing should be rectangular (4 pts), but got {len(pts)}")

    def test_last_floor_landing_rectangular_when_aligned(self):
        # 3 floors, but lets say L1-L2 is 5000, L2-L3 (roof) is 4000.
        # BUT the logic forces L2-L3 1st flight to match typical (4000).
        # Actually, if L2-L3 is 4000 and typical is 4000, they should align.
        levels_data = [
            {"id": "L1", "elevation": 0.0},
            {"id": "L2", "elevation": 5000.0},
            {"id": "L3", "elevation": 9000.0} # Roof
        ]
        manifest = generate_staircase_manifest([(0, 0)], levels_data, typical_floor_height_mm=4000)
        
        # L2 landing: arriving from (L1-L2, typical 4000) vs departing to (L2-L3, typical 4000)
        landings = [f for f in manifest["floors"] if "_L2_MainLanding" in f["id"]]
        self.assertEqual(len(landings), 1)
        pts = landings[0]["points"]
        # Should be rectangular now because both use typical-aligned first flight (rpf=14 for 4000mm)
        self.assertEqual(len(pts), 4, f"L2 landing (last floor) should be rectangular (4 pts), but got {len(pts)}")

    def test_roof_landing_geometry(self):
        # 3 floors, L3 is roof.
        levels_data = [
            {"id": "L1", "elevation": 0.0},
            {"id": "L2", "elevation": 4000.0},
            {"id": "L3", "elevation": 8000.0}
        ]
        manifest = generate_staircase_manifest([(0, 0)], levels_data, typical_floor_height_mm=4000)
        
        # Roof landing (i=2 for L3)
        landings = [f for f in manifest["floors"] if "_L3_MainLanding" in f["id"]]
        self.assertEqual(len(landings), 1)
        pts = landings[0]["points"]
        # Roof landing should match arrival. If balanced dogleg, it should be rectangular too.
        # But if it was asymmetric, it would be L-shape.
        # My change for roof: req_l, req_r = arr_l, arr_r.
        # For [14, 14], arr_l = arr_r = fy_s. So rectangle.
        self.assertEqual(len(pts), 4)

    def test_roof_landing_l_shape_when_asymmetric(self):
        # 3 floors, L1-L2 is 4000. L2-L3 (roof) is 3000 (shorter).
        # rpf_typical for 4000 is 14.
        # L2-L3 is 3000 -> 20 risers.
        # Since it's top floor, it forces 1st flight to 14. 2nd flight to 6.
        # ARR_L = fy_s. ARR_R = fy_s + (14-6-1)*300 - 50 = fy_s + 2100 - 50 = fy_s + 2050
        # This SHOULD be an L-shape at the roof!
        levels_data = [
            {"id": "L1", "elevation": 0.0},
            {"id": "L2", "elevation": 4000.0},
            {"id": "L3", "elevation": 7000.0}
        ]
        manifest = generate_staircase_manifest([(0, 0)], levels_data, typical_floor_height_mm=4000)
        
        landings = [f for f in manifest["floors"] if "_L3_MainLanding" in f["id"]]
        self.assertEqual(len(landings), 1)
        pts = landings[0]["points"]
        # Should be L-shape (6 pts)
        self.assertEqual(len(pts), 6, f"Roof landing should be L-shape (6 pts) for asymmetric arrival, but got {len(pts)}")

if __name__ == '__main__':
    unittest.main()
