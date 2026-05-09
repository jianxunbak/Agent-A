import sys
import unittest
from revit_mcp.staircase_logic import get_stair_run_data, _snap_risers, _calc_num_flights, get_void_rectangles_mm

class TestStaircaseLogic(unittest.TestCase):
    def test_typical_floor_risers(self):
        # FIXED: _snap_risers now uses floor() so stairs NEVER overshoot.
        # 4200mm / 150mm = 28.0 exactly -> 28 risers -> [14, 14] per flight
        # This is the standard case (typical office floor = 4200mm)
        levels_data = [
            {"id": "L1", "elevation": 0.0},
            {"id": "L2", "elevation": 4200.0}
        ]
        typical_h = 4200.0
        runs = get_stair_run_data([(0, 0)], levels_data, 3000, {"riser": 150}, typical_h)
        self.assertEqual(len(runs), 1)
        run = runs[0]
        # 4200 / 150 = 28 risers exactly -> [14, 14]
        self.assertEqual(run["flight_list"], [14, 14])
        self.assertEqual(run["num_flight_pairs"], 1)
        # Verify zero overshoot: 28 * actual_riser = 4200mm
        self.assertAlmostEqual(run["actual_riser_height_mm"] * 28, 4200.0, places=1)

    def test_non_typical_floor_risers(self):
        # 4000mm floor / 150mm riser = 26.67 -> floor() = 26 risers
        # 26 risers / 2 flights = [13, 13] 
        # actual_riser = 4000 / 26 = 153.8mm (compliant, within +5mm tolerance)
        levels_data = [
            {"id": "L1", "elevation": 0.0},
            {"id": "L2", "elevation": 4000.0}
        ]
        typical_h = 4000.0
        runs = get_stair_run_data([(0, 0)], levels_data, 3000, {"riser": 150}, typical_h)
        self.assertEqual(len(runs), 1)
        run = runs[0]
        # 4000 / 150 = 26.67 -> floor = 26 -> [13, 13]
        self.assertEqual(run["flight_list"], [13, 13])
        self.assertEqual(run["num_flight_pairs"], 1)
        # Verify zero overshoot: 26 * actual_riser must equal floor height exactly
        self.assertAlmostEqual(run["actual_riser_height_mm"] * 26, 4000.0, places=1)

    def test_first_floor_tall(self):
        # first floor 8400mm (double storey). typical floor 4200mm. 
        # snap_risers(8400, 150) = 56 risers (8400/150=56.0 exactly)
        # typical rpf = 14. num_flights = ceil(56/14) = 4 (even)
        # 56 / 4 = 14 per flight: [14, 14, 14, 14]
        levels_data = [
            {"id": "L1", "elevation": 0.0},
            {"id": "L2", "elevation": 8400.0}
        ]
        typical_h = 4200.0
        runs = get_stair_run_data([(0, 0)], levels_data, 3000, {"riser": 150}, typical_h)
        self.assertEqual(len(runs), 1)
        run = runs[0]
        # Updated Rule: Top floor (leading to roof) forces 2 flights to avoid headroom issues,
        # and aligns the first flight with typical rpf (14).
        # floor_risers(8400, 150) = 56. flight_list = [14, 42]
        self.assertEqual(run["flight_list"], [14, 42])
        self.assertEqual(run["num_flight_pairs"], 1)
        # intermediate heights: none for 1-pair stairs
        self.assertEqual(len(run["intermediate_heights_mm"]), 0)

    def test_void_rectangles(self):
        # 100mm offset inside
        positions = [(10000, 10000)]
        enc_w = 4000
        enc_d = 6000
        voids = get_void_rectangles_mm(positions, enc_w, enc_d)
        v = voids[0]
        # hw = 4000/2 - 100 = 1900
        # hd = 6000/2 - 100 = 2900
        # cx-hw = 10000 - 1900 = 8100
        self.assertEqual(v, (8100.0, 7100.0, 11900.0, 12900.0))

if __name__ == '__main__':
    unittest.main()
