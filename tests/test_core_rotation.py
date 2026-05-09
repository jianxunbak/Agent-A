# -*- coding: utf-8 -*-
"""Tests for lifts.rotation_deg — core assembly rotation.

All tests are pure-Python (no Revit API).  They exercise:
  1. Rotation math helpers (_crot_xy_mm / _rp pattern)
  2. Wall start/end point rotation in the manifest post-pass
  3. Floor polygon point rotation
  4. Void rect → rotated polygon conversion
  5. _stair_run_data stamping
  6. _build_dogleg_in_scope-style _rp() rotation via mock DB.XYZ
"""

import math
import unittest


# ── Minimal mock for Autodesk.Revit.DB.XYZ ────────────────────────────────────

class _XYZ:
    def __init__(self, x, y, z=0.0):
        self.X = float(x)
        self.Y = float(y)
        self.Z = float(z)

    def DistanceTo(self, other):
        return math.sqrt((self.X-other.X)**2 + (self.Y-other.Y)**2 + (self.Z-other.Z)**2)

    def __repr__(self):
        return f"XYZ({self.X:.3f}, {self.Y:.3f}, {self.Z:.3f})"


# ── Standalone rotation helpers (mirrors revit_workers.py logic) ───────────────

def _make_rot_xy_mm(deg, cx, cy):
    rad = math.radians(deg)
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    def _rot(x, y):
        dx = x - cx; dy = y - cy
        return (cx + dx*cos_r - dy*sin_r, cy + dx*sin_r + dy*cos_r)
    return _rot

def _make_rp(deg, cx_ft, cy_ft):
    """Returns the _rp() closure used inside _build_dogleg_in_scope."""
    if not deg:
        return lambda pt: pt
    rad = math.radians(deg)
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    def _rp(pt):
        dx = pt.X - cx_ft; dy = pt.Y - cy_ft
        return _XYZ(cx_ft + dx*cos_r - dy*sin_r, cy_ft + dx*sin_r + dy*cos_r, pt.Z)
    return _rp


def _simulate_rotation_pass(manifest, rotation_deg, center_mm):
    """Simulate the 6b rotation pass from _expand_unified_vertical_circulation."""
    if not rotation_deg:
        return manifest, []

    _rot = _make_rot_xy_mm(rotation_deg, center_mm[0], center_mm[1])

    for w in manifest.get("walls", []):
        if "start" in w:
            nx, ny = _rot(w["start"][0], w["start"][1])
            w["start"] = [nx, ny, w["start"][2]]
        if "end" in w:
            nx, ny = _rot(w["end"][0], w["end"][1])
            w["end"] = [nx, ny, w["end"][2]]

    for f in manifest.get("floors", []):
        if "points" in f:
            f["points"] = [list(_rot(p[0], p[1])) for p in f["points"]]

    # Simulate void polygon conversion (ft = mm / 304.8)
    MM = 304.8
    def _rot_ft(x, y):
        return _rot(x * MM, y * MM)  # convert to mm, rotate, return mm

    void_polys = []
    for (vx1, vy1, vx2, vy2) in manifest.get("_voids_mm", []):
        corners = [_rot(vx1, vy1), _rot(vx2, vy1), _rot(vx2, vy2), _rot(vx1, vy2)]
        void_polys.append(corners)

    return manifest, void_polys


# ─────────────────────────────────────────────────────────────────────────────

class TestRotationMath(unittest.TestCase):

    def test_90deg_rotation_around_origin(self):
        rot = _make_rot_xy_mm(90, 0, 0)
        rx, ry = rot(1000, 0)
        self.assertAlmostEqual(rx, 0,    places=3)
        self.assertAlmostEqual(ry, 1000, places=3)

    def test_180deg_rotation_around_origin(self):
        rot = _make_rot_xy_mm(180, 0, 0)
        rx, ry = rot(5000, 3000)
        self.assertAlmostEqual(rx, -5000, places=3)
        self.assertAlmostEqual(ry, -3000, places=3)

    def test_45deg_rotation_around_origin(self):
        rot = _make_rot_xy_mm(45, 0, 0)
        rx, ry = rot(1000, 0)
        self.assertAlmostEqual(rx,  1000 * math.cos(math.radians(45)), places=2)
        self.assertAlmostEqual(ry,  1000 * math.sin(math.radians(45)), places=2)

    def test_rotation_around_non_origin_centre(self):
        cx, cy = 10000, 5000
        rot = _make_rot_xy_mm(90, cx, cy)
        # point directly to the right of centre
        rx, ry = rot(cx + 3000, cy)
        # after 90° CCW it should be directly above centre
        self.assertAlmostEqual(rx, cx,        places=2)
        self.assertAlmostEqual(ry, cy + 3000, places=2)

    def test_zero_rotation_is_identity(self):
        rot = _make_rot_xy_mm(0, 5000, 5000)
        rx, ry = rot(12000, -3000)
        self.assertAlmostEqual(rx, 12000, places=6)
        self.assertAlmostEqual(ry, -3000, places=6)

    def test_rotation_preserves_distance_from_centre(self):
        cx, cy = 0, 0
        rot = _make_rot_xy_mm(37, cx, cy)
        x0, y0 = 4000, 7000
        rx, ry = rot(x0, y0)
        d_before = math.sqrt(x0**2 + y0**2)
        d_after  = math.sqrt(rx**2 + ry**2)
        self.assertAlmostEqual(d_before, d_after, places=3)


class TestWallRotation(unittest.TestCase):

    def _make_manifest_with_core_walls(self):
        return {
            "walls": [
                {"id": "AI_FL_S_L1", "start": [2000, -3000, 0], "end": [-2000, -3000, 0],
                 "level_id": 1, "height": 3500},
                {"id": "AI_FL_N_L1", "start": [-2000, 3000, 0], "end": [2000, 3000, 0],
                 "level_id": 1, "height": 3500},
            ],
            "floors": [],
            "_voids_mm": [],
        }

    def test_90deg_wall_rotation_around_origin(self):
        m = self._make_manifest_with_core_walls()
        _simulate_rotation_pass(m, 90, [0, 0])
        # South wall: start was [2000,-3000,0] → after 90° CCW → [3000, 2000, 0]
        sx, sy = m["walls"][0]["start"][0], m["walls"][0]["start"][1]
        self.assertAlmostEqual(sx,  3000, places=1)
        self.assertAlmostEqual(sy,  2000, places=1)

    def test_rotation_does_not_change_z(self):
        m = self._make_manifest_with_core_walls()
        _simulate_rotation_pass(m, 45, [0, 0])
        for w in m["walls"]:
            self.assertEqual(w["start"][2], 0)
            self.assertEqual(w["end"][2],   0)

    def test_zero_rotation_leaves_walls_unchanged(self):
        m = self._make_manifest_with_core_walls()
        orig_start = list(m["walls"][0]["start"])
        _simulate_rotation_pass(m, 0, [0, 0])
        self.assertEqual(m["walls"][0]["start"], orig_start)

    def test_wall_length_preserved_after_rotation(self):
        m = self._make_manifest_with_core_walls()
        w = m["walls"][0]
        def _len(w):
            dx = w["end"][0] - w["start"][0]
            dy = w["end"][1] - w["start"][1]
            return math.sqrt(dx*dx + dy*dy)
        L_before = _len(w)
        _simulate_rotation_pass(m, 63, [0, 0])
        L_after = _len(m["walls"][0])
        self.assertAlmostEqual(L_before, L_after, places=3)


class TestFloorRotation(unittest.TestCase):

    def test_floor_polygon_rotated_correctly(self):
        m = {
            "walls": [],
            "floors": [
                {"id": "AI_FL_TOPCAP", "level_id": 1,
                 "points": [[2000, 1500], [-2000, 1500], [-2000, -1500], [2000, -1500]]}
            ],
            "_voids_mm": [],
        }
        _simulate_rotation_pass(m, 90, [0, 0])
        pts = m["floors"][0]["points"]
        # [2000, 1500] → 90° CCW → [-1500, 2000]
        self.assertAlmostEqual(pts[0][0], -1500, places=1)
        self.assertAlmostEqual(pts[0][1],  2000, places=1)

    def test_floor_polygon_vertex_count_preserved(self):
        m = {
            "walls": [],
            "floors": [
                {"id": "AF", "level_id": 1,
                 "points": [[1,1],[2,1],[2,2],[1,2]]}
            ],
            "_voids_mm": [],
        }
        _simulate_rotation_pass(m, 45, [0, 0])
        self.assertEqual(len(m["floors"][0]["points"]), 4)


class TestVoidRotation(unittest.TestCase):

    def test_void_polygon_has_4_corners(self):
        m = {
            "walls": [], "floors": [],
            "_voids_mm": [(-2000, -1500, 2000, 1500)]
        }
        _, polys = _simulate_rotation_pass(m, 45, [0, 0])
        self.assertEqual(len(polys), 1)
        self.assertEqual(len(polys[0]), 4)

    def test_void_polygon_preserves_area(self):
        """Rotating a rect must preserve its area."""
        w, h = 4000.0, 3000.0
        m = {
            "walls": [], "floors": [],
            "_voids_mm": [(-w/2, -h/2, w/2, h/2)]
        }
        _, polys = _simulate_rotation_pass(m, 37, [0, 0])
        pts = polys[0]
        # Shoelace formula
        n = len(pts)
        area = abs(sum(pts[i][0]*pts[(i+1)%n][1] - pts[(i+1)%n][0]*pts[i][1]
                       for i in range(n))) / 2.0
        self.assertAlmostEqual(area, w * h, places=0)

    def test_multiple_voids_all_rotated(self):
        m = {
            "walls": [], "floors": [],
            "_voids_mm": [
                (-1000, -1000, 1000, 1000),
                (5000, -500, 7000, 500),
            ]
        }
        _, polys = _simulate_rotation_pass(m, 30, [0, 0])
        self.assertEqual(len(polys), 2)


class TestRpHelper(unittest.TestCase):
    """Test the _rp() closure that _build_dogleg_in_scope uses."""

    def test_rp_identity_when_zero_deg(self):
        _rp = _make_rp(0, 0, 0)
        pt = _XYZ(5, 3, 10)
        out = _rp(pt)
        self.assertAlmostEqual(out.X, 5, places=6)
        self.assertAlmostEqual(out.Y, 3, places=6)
        self.assertAlmostEqual(out.Z, 10, places=6)

    def test_rp_90deg_around_origin(self):
        _rp = _make_rp(90, 0, 0)
        pt = _XYZ(10, 0, 5)
        out = _rp(pt)
        self.assertAlmostEqual(out.X, 0,  places=4)
        self.assertAlmostEqual(out.Y, 10, places=4)
        self.assertAlmostEqual(out.Z, 5,  places=6)

    def test_rp_preserves_z(self):
        _rp = _make_rp(45, 0, 0)
        for z in [0, 3.5, 10.1, -1.0]:
            out = _rp(_XYZ(1, 0, z))
            self.assertAlmostEqual(out.Z, z, places=6)

    def test_rp_run_length_preserved(self):
        """After rotation a stair run start→end must keep its length."""
        _rp = _make_rp(37, 5, 5)
        p_start = _XYZ(0, 0, 0)
        p_end   = _XYZ(0, 10, 0)  # run of length 10 (ft)
        rp_s = _rp(p_start)
        rp_e = _rp(p_end)
        length = rp_s.DistanceTo(rp_e)
        self.assertAlmostEqual(length, 10.0, places=4)

    def test_rp_landing_remains_rectangular(self):
        """After rotation a rectangular landing must still have right-angle corners."""
        _rp = _make_rp(23, 0, 0)
        lp1 = _rp(_XYZ(-2, 0, 5))
        lp2 = _rp(_XYZ( 2, 0, 5))
        lp3 = _rp(_XYZ( 2, 1.5, 5))
        lp4 = _rp(_XYZ(-2, 1.5, 5))
        # Check opposite sides are equal length
        side_a = lp1.DistanceTo(lp2)  # bottom
        side_b = lp4.DistanceTo(lp3)  # top (same width)
        self.assertAlmostEqual(side_a, side_b, places=4)
        side_c = lp2.DistanceTo(lp3)  # right
        side_d = lp1.DistanceTo(lp4)  # left (same depth)
        self.assertAlmostEqual(side_c, side_d, places=4)

    def test_rp_stair_run_direction_angle(self):
        """After 30° rotation, the run direction should be 30° from original."""
        deg = 30
        _rp = _make_rp(deg, 0, 0)
        p_s = _rp(_XYZ(5, 0, 0))    # run starts at (5, 0) in unrotated frame
        p_e = _rp(_XYZ(5, 10, 0))   # run ends at (5, 10) — along +Y
        dx = p_e.X - p_s.X
        dy = p_e.Y - p_s.Y
        angle_actual = math.degrees(math.atan2(dy, dx))
        # Original direction was 90° (along +Y); after 30° CCW it should be 120°
        self.assertAlmostEqual(angle_actual, 90 + deg, places=3)


class TestStairRunDataStamping(unittest.TestCase):
    """Validate that rotation params are stamped onto run_data entries."""

    def _make_run_data(self):
        return [
            {"tag": "AI_Stair_1_L1_Run", "base_level_idx": 0, "top_level_idx": 1,
             "flight_1": {"start": [0, 0], "end": [0, 4000]},
             "flight_2": {"start": [0, 4000], "end": [0, 0]}},
            {"tag": "AI_Stair_2_L1_Run", "base_level_idx": 0, "top_level_idx": 1,
             "flight_1": {"start": [0, 0], "end": [0, 4000]},
             "flight_2": {"start": [0, 4000], "end": [0, 0]}},
        ]

    def _stamp(self, run_data, rotation_deg, cx_ft, cy_ft):
        """Mirrors the stamping logic in revit_workers.py."""
        if rotation_deg:
            rad = math.radians(rotation_deg)
            for rd in run_data:
                rd['_global_rotation_rad'] = rad
                rd['_rotation_cx_ft']      = cx_ft
                rd['_rotation_cy_ft']      = cy_ft

    def test_stamp_adds_rotation_keys(self):
        rd_list = self._make_run_data()
        self._stamp(rd_list, 45, 10.0, 5.0)
        for rd in rd_list:
            self.assertIn('_global_rotation_rad', rd)
            self.assertAlmostEqual(rd['_global_rotation_rad'], math.radians(45))
            self.assertEqual(rd['_rotation_cx_ft'], 10.0)
            self.assertEqual(rd['_rotation_cy_ft'],  5.0)

    def test_zero_rotation_does_not_stamp(self):
        rd_list = self._make_run_data()
        self._stamp(rd_list, 0, 0, 0)
        for rd in rd_list:
            self.assertNotIn('_global_rotation_rad', rd)

    def test_rp_uses_stamped_params(self):
        rd = self._make_run_data()[0]
        self._stamp([rd], 90, 0, 0)
        _rp = _make_rp(
            math.degrees(rd['_global_rotation_rad']),
            rd['_rotation_cx_ft'],
            rd['_rotation_cy_ft']
        )
        pt = _rp(_XYZ(1, 0, 3))
        self.assertAlmostEqual(pt.X, 0,  places=4)
        self.assertAlmostEqual(pt.Y, 1,  places=4)
        self.assertAlmostEqual(pt.Z, 3,  places=6)


if __name__ == "__main__":
    import sys
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
