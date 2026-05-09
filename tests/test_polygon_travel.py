# -*- coding: utf-8 -*-
"""
Tests for polygon-aware travel distance fix.

The original bug
----------------
When a user asked for an L-shape / H-shape building, the engine still held
stale floor_dims from the *previous* rectangular build (e.g. 51143 × 80000 mm
from old model walls).  _check_travel_distance sampled 12 rectangular grid
points, including the bounding-box south midpoint (0, -40 000).  That point is
OUTSIDE the actual L-shape polygon but was treated as valid, and at 48 000 mm
from the north stair it exceeded the 45 000 mm travel limit — triggering spurious
perimeter (SMOKE_STOP) staircases that don't fit inside the small floor plate.

The two-part fix
----------------
1. revit_workers.py: when shell["footprint_points"] is present, derive
   _stair_floor_dims from the actual polygon bounding box instead of old walls.
2. staircase_logic._check_travel_distance: when footprint_pts is supplied, replace
   the 12-point rectangular grid with an 8×8 cell-centre grid filtered to the
   polygon interior, so points outside the shape are never tested.

Test structure
--------------
TestPointInPolygon           — _point_in_polygon correctness
TestCheckTravelDistance      — low-level _check_travel_distance unit tests
TestFireSafetyRequirements   — end-to-end calculate_fire_safety_requirements tests
"""
import math
import sys
import os
import unittest

from revit_mcp import staircase_logic
from revit_mcp.staircase_logic import _point_in_polygon, _check_travel_distance
from revit_mcp.fire_safety_logic import calculate_fire_safety_requirements

# ---------------------------------------------------------------------------
# Shared geometry helpers
# ---------------------------------------------------------------------------

def _l_shape(full_w=60000, full_l=60000):
    """L-shape: 'full_w × full_l' bounding box with the top-right quadrant cut.

    Points are CCW, centred at origin.  The inner corner is at (0, 0).
    """
    hw, hl = full_w / 2.0, full_l / 2.0
    return [
        [-hw, -hl],   # SW
        [ hw, -hl],   # SE
        [ hw,  0.0],  # right cut edge
        [ 0.0,  0.0], # inner corner
        [ 0.0,  hl],  # top inner
        [-hw,  hl],   # NW
    ]


def _simple_h_shape(full_w=60000, full_l=60000):
    """H-shape: two vertical legs connected by a central crossbar.

    Leg width  = full_w / 5
    Bar height = full_l / 4  (centred in Y)
    """
    hw = full_w / 2.0
    hl = full_l / 2.0
    leg_w = full_w / 5.0
    bar_half = full_l / 8.0
    return [
        (-hw,        -hl),
        (-hw + leg_w,-hl),
        (-hw + leg_w,-bar_half),
        ( hw - leg_w,-bar_half),
        ( hw - leg_w,-hl),
        ( hw,        -hl),
        ( hw,         hl),
        ( hw - leg_w, hl),
        ( hw - leg_w, bar_half),
        (-hw + leg_w, bar_half),
        (-hw + leg_w, hl),
        (-hw,         hl),
    ]


# ---------------------------------------------------------------------------
# TestPointInPolygon
# ---------------------------------------------------------------------------

class TestPointInPolygon(unittest.TestCase):

    def test_centre_inside_square(self):
        sq = [[-10, -10], [10, -10], [10, 10], [-10, 10]]
        self.assertTrue(_point_in_polygon(0, 0, sq))

    def test_far_point_outside_square(self):
        sq = [[-10, -10], [10, -10], [10, 10], [-10, 10]]
        self.assertFalse(_point_in_polygon(20, 20, sq))

    def test_inside_l_shape(self):
        fp = _l_shape(full_w=60000, full_l=60000)
        self.assertTrue(_point_in_polygon(-15000, -15000, fp))  # bottom-left arm
        self.assertTrue(_point_in_polygon(-15000,  15000, fp))  # top-left arm
        self.assertTrue(_point_in_polygon( 15000, -15000, fp))  # bottom-right arm

    def test_cut_corner_outside_l_shape(self):
        fp = _l_shape(full_w=60000, full_l=60000)
        self.assertFalse(_point_in_polygon(15000, 15000, fp))   # in cut quadrant
        self.assertFalse(_point_in_polygon(25000, 25000, fp))

    def test_inside_h_shape(self):
        fp = _simple_h_shape(full_w=60000, full_l=60000)
        self.assertTrue(_point_in_polygon(  0, 0, fp))   # central bar
        self.assertTrue(_point_in_polygon(-22000, 0, fp))  # left leg
        self.assertTrue(_point_in_polygon( 22000, 0, fp))  # right leg

    def test_openings_outside_h_shape(self):
        fp = _simple_h_shape(full_w=60000, full_l=60000)
        # Openings above/below the bar, between the legs
        self.assertFalse(_point_in_polygon(0,  20000, fp))
        self.assertFalse(_point_in_polygon(0, -20000, fp))


# ---------------------------------------------------------------------------
# TestCheckTravelDistance
# ---------------------------------------------------------------------------

class TestCheckTravelDistance(unittest.TestCase):
    """Validate that _check_travel_distance correctly handles polygon shapes.

    Key scenario: stale floor_dims from old rectangular model walls give a
    bounding box of 80 × 80 m, while the actual building is a 40 × 40 m L-shape.
    Without footprint_pts the stale test-point (0, -40000) is > 45000 mm from
    the north stair and incorrectly triggers a failure.  With footprint_pts the
    point is filtered out because it lies outside the actual polygon.
    """

    # Two central stairs at the lift-core boundaries (typical core layout)
    CORE_STAIRS = [(0, -8000), (0, 8000)]
    MAX_DIST = 45000  # 45 m — typical SCDF sprinklered limit

    def test_small_rectangle_passes(self):
        """40×40m rectangle: central stairs satisfy 45m rule."""
        dims = [(40000, 40000)]
        self.assertTrue(_check_travel_distance(self.CORE_STAIRS, dims, self.MAX_DIST))

    def test_large_rectangle_fails(self):
        """80×80m rectangle: central stairs cannot cover all corners within 45m."""
        dims = [(80000, 80000)]
        self.assertFalse(_check_travel_distance(self.CORE_STAIRS, dims, self.MAX_DIST))

    def test_l_shape_stale_dims_fails_old_behaviour(self):
        """Old bug: stale 80×80m dims give a test point at (0,-40000)
        that is 48000mm from the north stair — triggers false failure even though
        that point is outside the actual 40×40m L-shape polygon."""
        stale_dims = [(80000, 80000)]
        old_result = _check_travel_distance(self.CORE_STAIRS, stale_dims, self.MAX_DIST)
        self.assertFalse(old_result, "Old code should fail with stale 80m dims")

    def test_l_shape_polygon_filter_corrects_stale_dims(self):
        """Fix: supplying the actual 40×40m L-shape polygon filters out the
        stale test points that lie outside the shape.  All interior grid
        samples are well within 45m of the central stairs → passes."""
        stale_dims = [(80000, 80000)]  # still the wrong dims — polygon fixes it
        fp = _l_shape(full_w=40000, full_l=40000)
        new_result = _check_travel_distance(
            self.CORE_STAIRS, stale_dims, self.MAX_DIST, footprint_pts=fp
        )
        print("\n[L-shape fix] stale_dims+footprint_pts: {}".format(
            "PASS" if new_result else "FAIL"))
        self.assertTrue(new_result,
                        "Polygon-aware check with actual 40m L-shape should pass at 45m")

    def test_h_shape_polygon_filter_corrects_stale_dims(self):
        """Same fix for an H-shape with stale 80×80m dims."""
        stale_dims = [(80000, 80000)]
        fp = _simple_h_shape(full_w=40000, full_l=40000)
        new_result = _check_travel_distance(
            self.CORE_STAIRS, stale_dims, self.MAX_DIST, footprint_pts=fp
        )
        print("\n[H-shape fix] stale_dims+footprint_pts: {}".format(
            "PASS" if new_result else "FAIL"))
        self.assertTrue(new_result,
                        "Polygon-aware check with actual 40m H-shape should pass at 45m")

    def test_large_l_shape_still_fails_when_too_big(self):
        """A genuinely oversized L-shape (120×120m) should still fail the check."""
        large_fp = _l_shape(full_w=120000, full_l=120000)
        dims = [(120000, 120000)]
        result = _check_travel_distance(
            self.CORE_STAIRS, dims, self.MAX_DIST, footprint_pts=large_fp
        )
        self.assertFalse(result, "120m L-shape corners ARE too far away — must fail")


# ---------------------------------------------------------------------------
# TestFireSafetyRequirements
# ---------------------------------------------------------------------------

class TestFireSafetyRequirements(unittest.TestCase):
    """End-to-end tests for calculate_fire_safety_requirements.

    Stair positions inside this function are pushed outward from the lift core
    boundary by ~cluster_d/2 ≈ 3500mm (for 3500mm floor height), so the actual
    stair centres are at approximately (0, ±11500).  All distance expectations
    in these tests are computed relative to those actual positions.
    """

    PRESET = {
        "staircase_spec": {"riser": 150, "tread": 300,
                           "width_of_flight": 1500, "landing_width": 1800}
    }
    LIFT_BOUNDS = (-6000, -8000, 6000, 8000)  # lift core: 12×16m centred at origin
    CORE_CENTER = (0, 0)
    TYPICAL_H = 3500  # → stairs at (0, ±11500) approximately

    def _count_perimeter(self, sets):
        return sum(1 for s in sets if s.get("is_perimeter"))

    # ── Scenario 1: stale dims (old bug) ────────────────────────────────────

    def test_stale_dims_causes_spurious_perimeter_stairs(self):
        """OLD BUG reproduced: stale 80×80m dims from previous rectangular build
        → test point (0, -40000) is 51500mm from north stair → spurious
        SMOKE_STOP set added."""
        stale_dims = [(80000, 80000)] * 10
        overrides = {"max_travel_distance_mm": 45000}

        sets = calculate_fire_safety_requirements(
            stale_dims, self.CORE_CENTER, self.LIFT_BOUNDS,
            self.TYPICAL_H, self.PRESET, 4,
            compliance_overrides=overrides,
            footprint_pts=None  # old code path
        )
        n_perim = self._count_perimeter(sets)
        print("\n[OLD BUG] stale 80m dims, no polygon: {} perimeter sets".format(n_perim))
        self.assertGreater(n_perim, 0,
                           "Stale 80m dims must produce perimeter stairs (bug reproduced)")

    # ── Scenario 2: L-shape fix ─────────────────────────────────────────────

    def test_l_shape_corrected_dims_no_perimeter_stairs(self):
        """FIX: floor_dims corrected to actual 40×40m footprint.
        All real L-shape corners are within 45m of the central stairs
        → no perimeter stairs needed."""
        correct_dims = [(40000, 40000)] * 10   # derived from footprint_points BBox
        fp = _l_shape(full_w=40000, full_l=40000)
        overrides = {"max_travel_distance_mm": 45000}

        sets = calculate_fire_safety_requirements(
            correct_dims, self.CORE_CENTER, self.LIFT_BOUNDS,
            self.TYPICAL_H, self.PRESET, 4,
            compliance_overrides=overrides,
            footprint_pts=fp
        )
        n_perim = self._count_perimeter(sets)
        print("\n[FIX L-shape] correct 40m dims + polygon: {} perimeter sets".format(n_perim))
        for s in sets:
            print("  {}  {}".format(s["type"], s["pos"]))
        self.assertEqual(n_perim, 0,
                         "L-shape 40m should NOT need perimeter stairs; got {}".format(n_perim))

    # ── Scenario 3: H-shape fix ─────────────────────────────────────────────

    def test_h_shape_corrected_dims_no_perimeter_stairs(self):
        """FIX: H-shape 40×40m — central stairs must satisfy travel distance."""
        correct_dims = [(40000, 40000)] * 10
        fp = _simple_h_shape(full_w=40000, full_l=40000)
        overrides = {"max_travel_distance_mm": 45000}

        sets = calculate_fire_safety_requirements(
            correct_dims, self.CORE_CENTER, self.LIFT_BOUNDS,
            self.TYPICAL_H, self.PRESET, 4,
            compliance_overrides=overrides,
            footprint_pts=fp
        )
        n_perim = self._count_perimeter(sets)
        print("\n[FIX H-shape] correct 40m dims + polygon: {} perimeter sets".format(n_perim))
        for s in sets:
            print("  {}  {}".format(s["type"], s["pos"]))
        self.assertEqual(n_perim, 0,
                         "H-shape 40m should NOT need perimeter stairs; got {}".format(n_perim))

    # ── Scenario 4: large rectangle still gets perimeter stairs ─────────────

    def test_large_rectangle_correctly_gets_perimeter_stairs(self):
        """Regression guard: 120×120m rectangle at 60m limit must still add
        perimeter stairs (genuine compliance requirement)."""
        dims = [(120000, 120000)] * 10
        overrides = {"max_travel_distance_mm": 60000}

        sets = calculate_fire_safety_requirements(
            dims, self.CORE_CENTER, self.LIFT_BOUNDS,
            self.TYPICAL_H, self.PRESET, 4,
            compliance_overrides=overrides
        )
        n_perim = self._count_perimeter(sets)
        print("\n[120m rect] {} perimeter sets".format(n_perim))
        self.assertGreater(n_perim, 0,
                           "Large 120m rectangle must still require perimeter stairs")

    # ── Scenario 5: normal-size rectangle needs NO perimeter stairs ──────────

    def test_60x60_rect_at_60m_default_no_perimeter(self):
        """Standard 60×60m rectangular building at the 60m default travel
        distance (sprinklered): central stairs alone should suffice."""
        dims = [(60000, 60000)] * 10
        overrides = {"max_travel_distance_mm": 60000}

        sets = calculate_fire_safety_requirements(
            dims, self.CORE_CENTER, self.LIFT_BOUNDS,
            self.TYPICAL_H, self.PRESET, 4,
            compliance_overrides=overrides
        )
        n_perim = self._count_perimeter(sets)
        print("\n[60m rect @ 60m limit] {} perimeter sets".format(n_perim))
        self.assertEqual(n_perim, 0,
                         "60×60m rectangle should not need perimeter stairs at 60m limit; "
                         "got {}".format(n_perim))


if __name__ == "__main__":
    unittest.main(verbosity=2)
