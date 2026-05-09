# -*- coding: utf-8 -*-
"""
Tests for the polygon-aware column placement fixes (Option A + B).

These tests exercise the PURE-PYTHON grid logic extracted from
_process_columns_and_grids() in revit_workers.py.  No Revit runtime needed.

Scenarios covered
-----------------
1.  Rectangle — baseline: placement unchanged after refactor
2.  L-shape centred at origin — cut quadrant must be empty
3.  Asymmetric L (entirely in positive quadrant) — old code under-covered the
    upper arm; new code must reach it
4.  U-shape (courtyard) — inner void must be empty; all three arms covered
5.  Plus/cross shape — all four arms must have a column
6.  Narrow arm — arm whose width < span must still get a column via infill
"""

import math
import unittest

# ---------------------------------------------------------------------------
# Standalone re-implementation of the grid logic
# (mirrors revit_workers._process_columns_and_grids, pure Python)
# ---------------------------------------------------------------------------

def mm_to_ft(mm):
    return mm / 304.8

def ft_to_mm(ft):
    return ft * 304.8


def _tessellate_pts(pts):
    """Convert footprint_points (straight edges only — no arcs in tests) to tuples."""
    return [(float(p[0]), float(p[1])) for p in pts]


def _point_in_poly(px, py, poly):
    """Ray-casting point-in-polygon test (same algorithm as revit_workers)."""
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if ((y1 > py) != (y2 > py)) and (px < (x2 - x1) * (py - y1) / (y2 - y1) + x1):
            inside = not inside
    return inside


def _poly_crossed_by(coord_mm, is_x, poly):
    """
    Returns True if ≥2 polygon edges cross the axis-aligned line at coord_mm,
    which means the line passes through the polygon interior.
    (same as Option B _poly_crossed_by in revit_workers)
    """
    crossings = 0
    n = len(poly)
    for j in range(n):
        x1, y1 = poly[j]
        x2, y2 = poly[(j + 1) % n]
        if is_x:
            lo, hi = min(x1, x2), max(x1, x2)
            if lo < coord_mm < hi:
                crossings += 1
        else:
            lo, hi = min(y1, y2), max(y1, y2)
            if lo < coord_mm < hi:
                crossings += 1
    return crossings >= 2


def _infill_axis(offsets, span_mm, is_x, poly, max_passes=6):
    """Insert midpoint grid lines for gaps wider than 1.1× span (Option B)."""
    out = list(offsets)
    for _ in range(max_passes):
        inserted = False
        for i in range(len(out) - 1):
            if (out[i + 1] - out[i]) * 304.8 > span_mm * 1.1:
                mid_ft = (out[i] + out[i + 1]) / 2.0
                if _poly_crossed_by(mid_ft * 304.8, is_x, poly):
                    out.insert(i + 1, mid_ft)
                    inserted = True
                    break
        if not inserted:
            break
    return out


def compute_axis_grid(dim_mm, center_ft, span_ft, offset_from_edge_mm):
    """Symmetric column grid along one axis (same logic as revit_workers)."""
    center_mm = round(center_ft * 304.8)
    span_mm = span_ft * 304.8
    reach_mm = dim_mm / 2.0 - offset_from_edge_mm
    if reach_mm <= 100:
        return [mm_to_ft(center_mm)]
    full_dist = reach_mm * 2.0
    n_spans = max(1, int(math.ceil(full_dist / span_mm - 0.001)))
    raw_spacing = full_dist / n_spans
    spacing = round(raw_spacing / 50.0) * 50.0
    if spacing < 50:
        spacing = 50.0
    positions = set()
    max_steps = int(math.ceil(reach_mm / spacing)) + 1
    for i in range(-max_steps, max_steps + 1):
        pos = center_mm + i * spacing
        if abs(pos - center_mm) <= reach_mm + 1.0:
            positions.add(pos)
    return sorted(mm_to_ft(p) for p in positions)


def simulate_column_grid(shell, span_w_mm=12000, span_l_mm=12000,
                         offset_from_edge_mm=500, center_only=False):
    """
    Full simulation of the new column placement algorithm.

    Returns list of (x_mm, y_mm) column positions that survive all filters
    (inside polygon, not in exclusion zones).  Exclusion zones are omitted here
    since the tests focus on footprint coverage.
    """
    # --- Option A: synthesise footprint_points for rectangular buildings ----
    fp = shell.get("footprint_points")
    if not fp:
        w = float(shell.get("width",  30000))
        l = float(shell.get("length", w))
        hw, hl = w / 2.0, l / 2.0
        fp = [[-hw, -hl], [hw, -hl], [hw, hl], [-hw, hl]]

    # --- Option B part 1: polygon-aware grid anchor -------------------------
    fp_xs = [float(p[0]) for p in fp]
    fp_ys = [float(p[1]) for p in fp]
    grid_dim_w  = max(fp_xs) - min(fp_xs)
    grid_dim_l  = max(fp_ys) - min(fp_ys)
    grid_cx_ft  = mm_to_ft((min(fp_xs) + max(fp_xs)) / 2.0)
    grid_cy_ft  = mm_to_ft((min(fp_ys) + max(fp_ys)) / 2.0)

    x_offsets = compute_axis_grid(grid_dim_w, grid_cx_ft,
                                  mm_to_ft(span_w_mm), offset_from_edge_mm)
    y_offsets = compute_axis_grid(grid_dim_l, grid_cy_ft,
                                  mm_to_ft(span_l_mm), offset_from_edge_mm)

    if center_only:
        x_offsets = [o for o in x_offsets if abs(o - grid_cx_ft) < mm_to_ft(grid_dim_w) / 2.0 - 0.1]
        y_offsets = [o for o in y_offsets if abs(o - grid_cy_ft) < mm_to_ft(grid_dim_l) / 2.0 - 0.1]

    # Tessellate base polygon
    base_poly = _tessellate_pts(fp)

    # --- Option B part 2: gap infill ----------------------------------------
    if not center_only:
        x_offsets = _infill_axis(x_offsets, span_w_mm, True,  base_poly)
        y_offsets = _infill_axis(y_offsets, span_l_mm, False, base_poly)

    # --- Containment filter -------------------------------------------------
    positions = []
    for ox in x_offsets:
        for oy in y_offsets:
            px_mm = ft_to_mm(ox)
            py_mm = ft_to_mm(oy)
            if _point_in_poly(px_mm, py_mm, base_poly):
                positions.append((px_mm, py_mm))

    return positions


def _max_uncovered_span(positions, poly, sample_step_mm=3000):
    """
    Walk the polygon interior on a fine grid; for each sample point find the
    distance to the nearest column.  Returns the maximum such distance in mm.
    Used to verify no large uncovered zone exists inside the slab.
    """
    if not positions:
        return float("inf")
    fp_xs = [p[0] for p in poly]
    fp_ys = [p[1] for p in poly]
    x_lo, x_hi = min(fp_xs), max(fp_xs)
    y_lo, y_hi = min(fp_ys), max(fp_ys)

    max_dist = 0.0
    x = x_lo + sample_step_mm / 2.0
    while x < x_hi:
        y = y_lo + sample_step_mm / 2.0
        while y < y_hi:
            if _point_in_poly(x, y, poly):
                nearest = min(
                    math.sqrt((x - cx) ** 2 + (y - cy) ** 2)
                    for cx, cy in positions
                )
                if nearest > max_dist:
                    max_dist = nearest
            y += sample_step_mm
        x += sample_step_mm

    return max_dist


# ---------------------------------------------------------------------------
# Shared polygon helpers
# ---------------------------------------------------------------------------

def _rect(w, l):
    hw, hl = w / 2.0, l / 2.0
    return [[-hw, -hl], [hw, -hl], [hw, hl], [-hw, hl]]


def _l_shape_centred(full_w=60000, full_l=60000):
    """L-shape centred at origin; top-right quadrant is cut out."""
    hw, hl = full_w / 2.0, full_l / 2.0
    return [
        [-hw, -hl],
        [ hw, -hl],
        [ hw,  0.0],
        [ 0.0,  0.0],
        [ 0.0,  hl],
        [-hw,  hl],
    ]


def _l_shape_positive_quadrant(full_w=30000, full_l=30000):
    """L-shape entirely in positive quadrant (origin at SW corner).
    Horizontal arm: x=[0,full_w], y=[0,full_l/2]
    Vertical arm:   x=[0,full_w/2], y=[0,full_l]
    """
    hw = full_w
    hl = full_l
    # Vertices CCW
    return [
        [0,        0       ],
        [hw,       0       ],
        [hw,       hl / 2  ],
        [hw / 2,   hl / 2  ],
        [hw / 2,   hl      ],
        [0,        hl      ],
    ]


def _u_shape(outer_w=40000, outer_l=40000, courtyard_w=20000, courtyard_l=20000):
    """U-shape: outer rectangle with a courtyard cut from the top centre."""
    hw, hl = outer_w / 2.0, outer_l / 2.0
    cw, cl = courtyard_w / 2.0, courtyard_l
    # CCW, starting from SW
    return [
        [-hw,  -hl],
        [ hw,  -hl],
        [ hw,   hl],
        [ cw,   hl],
        [ cw,   hl - cl],
        [-cw,   hl - cl],
        [-cw,   hl],
        [-hw,   hl],
    ]


def _cross_shape(arm_half_w=5000, arm_half_l=25000):
    """Plus/cross shape: two overlapping rectangles."""
    return [
        [-arm_half_w, -arm_half_l],
        [ arm_half_w, -arm_half_l],
        [ arm_half_w, -arm_half_w],
        [ arm_half_l, -arm_half_w],
        [ arm_half_l,  arm_half_w],
        [ arm_half_w,  arm_half_w],
        [ arm_half_w,  arm_half_l],
        [-arm_half_w,  arm_half_l],
        [-arm_half_w,  arm_half_w],
        [-arm_half_l,  arm_half_w],
        [-arm_half_l, -arm_half_w],
        [-arm_half_w, -arm_half_w],
    ]


def _narrow_arm_shape(main_w=30000, main_l=20000,
                      arm_w=6000, arm_l=20000):
    """Rectangle with a narrow arm extending to the right."""
    hw, hl = main_w / 2.0, main_l / 2.0
    arm_y_half = arm_w / 2.0
    # CCW
    return [
        [-hw,        -hl       ],
        [ hw,        -hl       ],
        [ hw,        -arm_y_half],
        [ hw + arm_l,-arm_y_half],
        [ hw + arm_l, arm_y_half],
        [ hw,         arm_y_half],
        [ hw,         hl       ],
        [-hw,         hl       ],
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestColumnPlacementRectangle(unittest.TestCase):
    """Sanity check: rectangular building behaviour is unchanged."""

    SPAN = 12000
    POLY = _rect(30000, 20000)

    def setUp(self):
        self.shell = {"width": 30000, "length": 20000}
        self.positions = simulate_column_grid(self.shell, self.SPAN, self.SPAN)
        self.base_poly = _tessellate_pts(self.POLY)

    def test_at_least_one_column(self):
        self.assertGreater(len(self.positions), 0,
                           "Rectangle must produce columns")

    def test_all_inside_polygon(self):
        outside = [(x, y) for x, y in self.positions
                   if not _point_in_poly(x, y, self.base_poly)]
        self.assertEqual(outside, [],
                         "All columns must be inside the rectangle; got outside: {}".format(outside))

    def test_max_uncovered_span(self):
        max_d = _max_uncovered_span(self.positions, self.base_poly)
        self.assertLess(max_d, self.SPAN * 1.5,
                        "Max uncovered distance {:.0f}mm exceeds 1.5× span".format(max_d))


class TestColumnPlacementLShapeCentred(unittest.TestCase):
    """L-shape centred at origin: cut quadrant empty, three arms covered."""

    SPAN = 12000
    FULL_W = 60000
    FULL_L = 60000

    def setUp(self):
        fp = _l_shape_centred(self.FULL_W, self.FULL_L)
        self.shell = {"footprint_points": fp}
        self.positions = simulate_column_grid(self.shell, self.SPAN, self.SPAN)
        self.base_poly = _tessellate_pts(fp)

    def test_at_least_one_column(self):
        self.assertGreater(len(self.positions), 0)

    def test_all_inside_polygon(self):
        outside = [(x, y) for x, y in self.positions
                   if not _point_in_poly(x, y, self.base_poly)]
        self.assertEqual(outside, [],
                         "Columns outside L-shape: {}".format(outside))

    def test_cut_quadrant_empty(self):
        """No column should land in the top-right cut quadrant (x>0 AND y>0)."""
        in_cut = [(x, y) for x, y in self.positions if x > 500 and y > 500]
        self.assertEqual(in_cut, [],
                         "Columns found in cut quadrant: {}".format(in_cut))

    def test_bottom_right_arm_covered(self):
        """Bottom-right arm: x>0, y<0."""
        in_arm = [p for p in self.positions if p[0] > 500 and p[1] < -500]
        self.assertGreater(len(in_arm), 0, "Bottom-right arm has no columns")

    def test_top_left_arm_covered(self):
        """Top-left arm: x<0, y>0."""
        in_arm = [p for p in self.positions if p[0] < -500 and p[1] > 500]
        self.assertGreater(len(in_arm), 0, "Top-left arm has no columns")

    def test_bottom_left_arm_covered(self):
        """Bottom-left quadrant (spine of L)."""
        in_arm = [p for p in self.positions if p[0] < -500 and p[1] < -500]
        self.assertGreater(len(in_arm), 0, "Bottom-left arm has no columns")

    def test_max_uncovered_span(self):
        max_d = _max_uncovered_span(self.positions, self.base_poly)
        self.assertLess(max_d, self.SPAN * 1.5,
                        "Max uncovered dist {:.0f}mm > 1.5× span in L-shape".format(max_d))


class TestColumnPlacementAsymmetricL(unittest.TestCase):
    """
    L-shape entirely in the positive quadrant.

    OLD behaviour (grid centred at shell.position = origin): the grid reached
    from x≈-14500 to x≈+14500.  With L vertices at [0..30000], grid lines at
    x=-12000 and y=-12000 were outside the polygon — leaving the upper arm
    (y>12000) with no columns.

    NEW behaviour (Option B anchor): grid is centred at the polygon's own
    bounding-box midpoint (15000, 15000), so every arm is reached.
    """

    SPAN = 12000

    def setUp(self):
        fp = _l_shape_positive_quadrant(30000, 30000)
        self.shell = {"footprint_points": fp}
        self.positions = simulate_column_grid(self.shell, self.SPAN, self.SPAN)
        self.base_poly = _tessellate_pts(fp)

    def test_all_inside_polygon(self):
        outside = [(x, y) for x, y in self.positions
                   if not _point_in_poly(x, y, self.base_poly)]
        self.assertEqual(outside, [], "Columns outside asymmetric L: {}".format(outside))

    def test_upper_vertical_arm_covered(self):
        """Vertical arm: x < 15000, y > 15000."""
        in_arm = [p for p in self.positions if p[0] < 14500 and p[1] > 15500]
        self.assertGreater(len(in_arm), 0,
                           "Upper vertical arm has no columns (asymmetric L regression)")

    def test_right_horizontal_arm_covered(self):
        """Horizontal arm: x > 15000, y < 15000."""
        in_arm = [p for p in self.positions if p[0] > 15500 and p[1] < 14500]
        self.assertGreater(len(in_arm), 0,
                           "Right horizontal arm has no columns (asymmetric L regression)")

    def test_max_uncovered_span(self):
        max_d = _max_uncovered_span(self.positions, self.base_poly)
        self.assertLess(max_d, self.SPAN * 1.5,
                        "Max uncovered dist {:.0f}mm > 1.5× span in asymmetric L".format(max_d))


class TestColumnPlacementUShape(unittest.TestCase):
    """U-shape (courtyard): inner void empty, all three arms covered."""

    SPAN = 12000

    def setUp(self):
        fp = _u_shape(outer_w=40000, outer_l=40000,
                      courtyard_w=20000, courtyard_l=20000)
        self.shell = {"footprint_points": fp}
        self.positions = simulate_column_grid(self.shell, self.SPAN, self.SPAN)
        self.base_poly = _tessellate_pts(fp)

    def test_all_inside_polygon(self):
        outside = [(x, y) for x, y in self.positions
                   if not _point_in_poly(x, y, self.base_poly)]
        self.assertEqual(outside, [], "Columns outside U-shape: {}".format(outside))

    def test_courtyard_void_empty(self):
        """No column in central courtyard (x ∈ [-8000,8000], y ∈ [0,20000])."""
        in_void = [
            (x, y) for x, y in self.positions
            if -8000 < x < 8000 and 500 < y < 19500
        ]
        self.assertEqual(in_void, [],
                         "Column(s) found inside courtyard void: {}".format(in_void))

    def test_left_arm_covered(self):
        in_arm = [p for p in self.positions if p[0] < -1000 and p[1] > 1000]
        self.assertGreater(len(in_arm), 0, "Left arm of U-shape has no columns")

    def test_right_arm_covered(self):
        in_arm = [p for p in self.positions if p[0] > 1000 and p[1] > 1000]
        self.assertGreater(len(in_arm), 0, "Right arm of U-shape has no columns")

    def test_bottom_base_covered(self):
        in_arm = [p for p in self.positions if p[1] < -1000]
        self.assertGreater(len(in_arm), 0, "Bottom base of U-shape has no columns")

    def test_max_uncovered_span(self):
        max_d = _max_uncovered_span(self.positions, self.base_poly)
        self.assertLess(max_d, self.SPAN * 1.5,
                        "Max uncovered dist {:.0f}mm > 1.5× span in U-shape".format(max_d))


class TestColumnPlacementCrossShape(unittest.TestCase):
    """Plus/cross shape: all four arms must have at least one column."""

    SPAN = 12000

    def setUp(self):
        fp = _cross_shape(arm_half_w=5000, arm_half_l=25000)
        self.shell = {"footprint_points": fp}
        self.positions = simulate_column_grid(self.shell, self.SPAN, self.SPAN)
        self.base_poly = _tessellate_pts(fp)

    def test_all_inside_polygon(self):
        outside = [(x, y) for x, y in self.positions
                   if not _point_in_poly(x, y, self.base_poly)]
        self.assertEqual(outside, [], "Columns outside cross: {}".format(outside))

    def test_top_arm_covered(self):
        in_arm = [p for p in self.positions if p[1] > 6000]
        self.assertGreater(len(in_arm), 0, "Top arm of cross has no columns")

    def test_bottom_arm_covered(self):
        in_arm = [p for p in self.positions if p[1] < -6000]
        self.assertGreater(len(in_arm), 0, "Bottom arm of cross has no columns")

    def test_left_arm_covered(self):
        in_arm = [p for p in self.positions if p[0] < -6000]
        self.assertGreater(len(in_arm), 0, "Left arm of cross has no columns")

    def test_right_arm_covered(self):
        in_arm = [p for p in self.positions if p[0] > 6000]
        self.assertGreater(len(in_arm), 0, "Right arm of cross has no columns")

    def test_max_uncovered_span(self):
        max_d = _max_uncovered_span(self.positions, self.base_poly)
        self.assertLess(max_d, self.SPAN * 1.5,
                        "Max uncovered dist {:.0f}mm > 1.5× span in cross".format(max_d))


class TestColumnPlacementNarrowArm(unittest.TestCase):
    """
    Narrow arm (6 m wide) extending right from a 30×20m main body.
    The arm width (6 m) is less than the column span (12 m) so the initial
    rectangular grid might produce no X-line through the arm.
    Option B infill must add one.
    """

    SPAN = 12000

    def setUp(self):
        fp = _narrow_arm_shape(main_w=30000, main_l=20000,
                               arm_w=6000, arm_l=20000)
        self.shell = {"footprint_points": fp}
        self.positions = simulate_column_grid(self.shell, self.SPAN, self.SPAN)
        self.base_poly = _tessellate_pts(fp)

    def test_all_inside_polygon(self):
        outside = [(x, y) for x, y in self.positions
                   if not _point_in_poly(x, y, self.base_poly)]
        self.assertEqual(outside, [], "Columns outside narrow-arm shape: {}".format(outside))

    def test_narrow_arm_covered(self):
        """At least one column must land in the narrow arm (x > 15500)."""
        in_arm = [p for p in self.positions if p[0] > 15500]
        self.assertGreater(len(in_arm), 0,
                           "Narrow arm has no columns — infill did not fire")

    def test_main_body_covered(self):
        in_body = [p for p in self.positions if p[0] < 14500]
        self.assertGreater(len(in_body), 0, "Main body has no columns")


class TestOptionAFallback(unittest.TestCase):
    """Option A: a plain rectangular shell (no footprint_points) must still
    produce correct column coverage via the synthesised polygon path."""

    SPAN = 12000

    def test_plain_rectangle_no_footprint_pts(self):
        shell = {"width": 24000, "length": 18000}
        positions = simulate_column_grid(shell, self.SPAN, self.SPAN)
        poly = _tessellate_pts(_rect(24000, 18000))
        self.assertGreater(len(positions), 0)
        outside = [(x, y) for x, y in positions if not _point_in_poly(x, y, poly)]
        self.assertEqual(outside, [], "Option A fallback produced out-of-bounds columns")

    def test_square_no_footprint_pts(self):
        shell = {"width": 40000}
        positions = simulate_column_grid(shell, self.SPAN, self.SPAN)
        self.assertGreater(len(positions), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
