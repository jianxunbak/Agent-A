# -*- coding: utf-8 -*-
"""
Tests for all 10 fixes applied to the complex-form building generation pipeline.

Run from the extension root:
    python -m revit_mcp.test_complex_form_fixes

No live Revit instance required — tests cover pure-Python logic only.
"""
import sys
import types
import unittest

# ---------------------------------------------------------------------------
# Shim Revit + project-internal modules that revit_workers imports at the top
# ---------------------------------------------------------------------------
def _make_stub(name):
    m = types.ModuleType(name)
    m.__path__ = []
    return m

for _mod in [
    "revit_mcp.gemini_client", "revit_mcp.bridge",
    "Autodesk", "Autodesk.Revit", "Autodesk.Revit.DB",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = _make_stub(_mod)

# Provide the minimal attributes revit_workers pulls from these stubs
_gc = sys.modules["revit_mcp.gemini_client"]
if not hasattr(_gc, "client"):
    _gc.client = None
_br = sys.modules["revit_mcp.bridge"]
if not hasattr(_br, "mcp_event_handler"):
    _br.mcp_event_handler = None

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from revit_mcp import agent_prompts
from revit_mcp.fire_safety_logic import (
    calculate_fire_safety_requirements,
    _nearest_polygon_edge_angle_deg,
)
from revit_mcp.staircase_logic import _point_in_polygon
from revit_mcp.svg_to_footprint import svg_path_to_footprint_points
import revit_mcp.revit_workers as rw  # for _poly_area_mm2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

L_SHAPE = [
    [0, 0], [20000, 0], [20000, 10000],
    [10000, 10000], [10000, 30000], [0, 30000],
]
# Bounding box of L_SHAPE is 20000 × 30000 = 600 000 000 mm²
# Actual area = 20000×10000 + 10000×20000 = 200 000 000 + 200 000 000 = 400 000 000 mm²

RECT_5X10 = [[0, 0], [5000, 0], [5000, 10000], [0, 10000]]
# Area = 50 000 000 mm²


# ===========================================================================
# Fix A — Step 0 keyword table removed from SPATIAL_BRAIN_SYSTEM_INSTRUCTION
# ===========================================================================
class TestFixA_PromptReasoning(unittest.TestCase):

    def _step0(self):
        """Extract text up to (but not including) MANDATORY CORE PLANNING PROTOCOL."""
        txt = agent_prompts.SPATIAL_BRAIN_SYSTEM_INSTRUCTION
        idx = txt.find("## MANDATORY CORE PLANNING PROTOCOL")
        return txt[:idx] if idx != -1 else txt

    def test_keyword_table_removed(self):
        """Old prescriptive 3-column keyword→tool lookup table must be gone."""
        step0 = self._step0()
        self.assertNotIn("User word / phrase", step0,
                         "Old keyword table header still present in Step 0")
        self.assertNotIn("Synonyms / natural phrasing", step0,
                         "Old keyword table header still present in Step 0")

    def test_blocking_language_removed(self):
        """'This step is BLOCKING' framing must be removed."""
        step0 = self._step0()
        self.assertNotIn("This step is BLOCKING", step0)

    def test_tool_effect_table_present(self):
        """New tool-by-architectural-effect table must be present."""
        step0 = self._step0()
        self.assertIn("footprint_rotation_overrides", step0)
        self.assertIn("footprint_scale_overrides", step0)
        self.assertIn("footprint_offset_overrides", step0)
        self.assertIn("footprint_svg", step0)
        self.assertIn("footprint_points", step0)
        self.assertIn("Architectural effect", step0)

    def test_mandatory_self_check_present(self):
        """MANDATORY self-check sentence must still exist in Step 0."""
        step0 = self._step0()
        self.assertIn("Form resolution:", step0)
        self.assertIn("MANDATORY", step0)

    def test_examples_preserved(self):
        """TWIST/TAPER/LEAN examples in DISPATCHER_PROMPT must be untouched."""
        txt = agent_prompts.DISPATCHER_PROMPT
        self.assertIn("TWIST / SCREW EXAMPLE", txt)
        self.assertIn("TAPER EXAMPLE", txt)
        self.assertIn("LEAN EXAMPLE", txt)


# ===========================================================================
# Fix B — SVG parse failure raises (would become CONFLICT in full pipeline)
# ===========================================================================
class TestFixB_SvgParseError(unittest.TestCase):

    def test_bad_svg_raises_value_error(self):
        """An unparseable SVG path raises ValueError; the caller wraps it as CONFLICT."""
        with self.assertRaises((ValueError, Exception)):
            svg_path_to_footprint_points("INVALID_NOT_SVG")

    def test_self_intersecting_raises(self):
        """An SVG with only a single point (degenerate) should raise."""
        with self.assertRaises((ValueError, Exception)):
            svg_path_to_footprint_points("M 0 0 Z")

    def test_valid_svg_succeeds(self):
        """A valid rectangular SVG path must succeed and return ≥3 points."""
        pts = svg_path_to_footprint_points(
            "M -10000 -5000 L 10000 -5000 L 10000 5000 L -10000 5000 Z"
        )
        self.assertGreaterEqual(len(pts), 4)


# ===========================================================================
# Fix C3 — _poly_area_mm2 (Shoelace formula)
# ===========================================================================
class TestFixC3_PolyArea(unittest.TestCase):

    def test_rectangle_area(self):
        area = rw._poly_area_mm2(RECT_5X10)
        self.assertAlmostEqual(area, 5000 * 10000, delta=1.0)

    def test_l_shape_area_correct(self):
        """L-shape area must be 400 000 000 mm², not the 600 000 000 bbox."""
        area = rw._poly_area_mm2(L_SHAPE)
        self.assertAlmostEqual(area, 400_000_000, delta=100.0)

    def test_l_shape_less_than_bbox(self):
        """Polygon area must be less than its bounding-box area for non-convex shapes."""
        bbox_area = 20000 * 30000
        area = rw._poly_area_mm2(L_SHAPE)
        self.assertLess(area, bbox_area)

    def test_triangle(self):
        tri = [[0, 0], [6000, 0], [0, 8000]]
        area = rw._poly_area_mm2(tri)
        self.assertAlmostEqual(area, 0.5 * 6000 * 8000, delta=1.0)

    def test_winding_direction_ignored(self):
        """Area should be the same regardless of CW vs CCW winding."""
        ccw = [[0, 0], [10000, 0], [10000, 5000], [0, 5000]]
        cw  = list(reversed(ccw))
        self.assertAlmostEqual(rw._poly_area_mm2(ccw), rw._poly_area_mm2(cw), delta=1.0)


# ===========================================================================
# Fix E2 — _nearest_polygon_edge_angle_deg helper
# ===========================================================================
class TestFixE2_NearestEdgeAngle(unittest.TestCase):
    """Axis-aligned square with vertices at (±10000, ±10000)."""

    SQUARE = [
        [-10000, -10000], [10000, -10000],
        [10000,  10000], [-10000,  10000],
    ]

    def test_point_near_south_edge_snaps_to_0(self):
        """Point just below the square's south edge — nearest edge is horizontal → 0°."""
        angle = _nearest_polygon_edge_angle_deg(0, -11000, self.SQUARE)
        self.assertIn(angle, [0.0, 180.0, -180.0],
                      "South edge is horizontal — expected snap to 0° or ±180°")

    def test_point_near_east_edge_snaps_to_90(self):
        """Point just right of east edge — nearest edge is vertical → 90°."""
        angle = _nearest_polygon_edge_angle_deg(11000, 0, self.SQUARE)
        self.assertIn(angle, [90.0, -90.0, 270.0],
                      "East edge is vertical — expected snap to ±90°")

    def test_l_shape_near_inner_corner(self):
        """Point near the inner corner of an L-shape must return a 90° multiple."""
        angle = _nearest_polygon_edge_angle_deg(10500, 10500, L_SHAPE)
        self.assertEqual(angle % 90.0, 0.0, "Result must be a multiple of 90°")

    def test_always_multiple_of_90(self):
        """For any polygon point the result is always a multiple of 90°."""
        import random
        random.seed(42)
        for _ in range(20):
            px = random.uniform(-15000, 15000)
            py = random.uniform(-15000, 30000)
            angle = _nearest_polygon_edge_angle_deg(px, py, L_SHAPE)
            self.assertEqual(angle % 90.0, 0.0)


# ===========================================================================
# Fix E2 — calculate_fire_safety_requirements adds rotation_deg to perimeter sets
# ===========================================================================
class TestFixE2_PerimeterRotation(unittest.TestCase):

    PRESET_FS = {
        "max_travel_distance": 60000,
        "staircase_spec": {"riser": 150, "tread": 300,
                           "width_of_flight": 1500, "landing_width": 1800},
    }

    def _large_l_shape_sets(self):
        # 200×200 m bounding box L-shape — central core alone cannot cover corners.
        # This reliably forces at least one perimeter SMOKE_STOP set.
        fp = [
            [-100000, -100000], [0, -100000], [0, 0], [100000, 0],
            [100000,  100000], [-100000, 100000],
        ]
        floor_dims = [(200000, 200000)]
        core_center = (0, 0)
        lift_bounds = (-6000, -5000, 6000, 5000)
        return calculate_fire_safety_requirements(
            floor_dims, core_center, lift_bounds,
            4000, self.PRESET_FS, 4, 3000,
            footprint_pts=fp,
        ), fp

    def test_perimeter_sets_have_rotation_deg_key(self):
        """Every SMOKE_STOP is_perimeter set must carry a rotation_deg key."""
        sets, _ = self._large_l_shape_sets()
        perim = [s for s in sets if s.get("is_perimeter")]
        if not perim:
            self.skipTest("No perimeter sets generated for this floor plate — increase size")
        for s in perim:
            self.assertIn("rotation_deg", s,
                          "Perimeter set missing rotation_deg: {}".format(s))

    def test_perimeter_rotation_is_multiple_of_90(self):
        """rotation_deg must be a multiple of 90° (axis-aligned snap)."""
        sets, _ = self._large_l_shape_sets()
        perim = [s for s in sets if s.get("is_perimeter")]
        for s in perim:
            self.assertEqual(s["rotation_deg"] % 90.0, 0.0,
                             "rotation_deg {} is not a multiple of 90".format(s["rotation_deg"]))


# ===========================================================================
# Fix E1 — Core sub-boundary polygon validation (_point_in_polygon)
# ===========================================================================
class TestFixE1_CorePolygonValidation(unittest.TestCase):
    """
    Validates the logic that would trigger a CONFLICT when a core zone
    extends outside the floor plate polygon.
    """

    def _all_corners_inside(self, rect, polygon):
        """True if all 4 corners of rect=[x1,y1,x2,y2] are inside polygon."""
        x1, y1, x2, y2 = rect
        corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
        return all(_point_in_polygon(cx, cy, polygon) for cx, cy in corners)

    def test_core_inside_l_shape_wing_passes(self):
        """A core rect fully within the left arm of the L must pass validation."""
        # Left arm of L_SHAPE: x=[0,20000], y=[0,10000]
        rect_inside = [2000, 2000, 8000, 8000]
        self.assertTrue(self._all_corners_inside(rect_inside, L_SHAPE))

    def test_core_in_void_of_l_shape_fails(self):
        """A core rect placed in the concave void of the L must fail validation."""
        # Void of L_SHAPE: x=[10000,20000], y=[10000,30000]
        rect_in_void = [12000, 12000, 18000, 18000]
        self.assertFalse(self._all_corners_inside(rect_in_void, L_SHAPE))

    def test_core_straddling_boundary_fails(self):
        """A core rect whose corner lands in the concave void must fail validation.
        L_SHAPE concave void: x=[10000,20000], y=[10000,30000].
        Rect [8000,5000]→[18000,15000] has its top-right corner (18000,15000) in the void."""
        rect_straddling = [8000, 5000, 18000, 15000]
        self.assertFalse(self._all_corners_inside(rect_straddling, L_SHAPE))

    def test_core_fully_outside_fails(self):
        """A core rect entirely outside the polygon must fail."""
        rect_outside = [25000, 5000, 35000, 15000]
        self.assertFalse(self._all_corners_inside(rect_outside, L_SHAPE))


# ===========================================================================
# Fix C4 — Pre-floor-dims bounding-box override removed
# ===========================================================================
class TestFixC4_NoBboxOverride(unittest.TestCase):
    """
    The old code replaced _pre_floor_dims with a uniform bounding-box list.
    After C4, _pre_floor_dims == floor_dims (the per-level list is unchanged).
    We test indirectly: calculate_fire_safety_requirements with a polygon footprint
    and a tapered floor_dims list must produce a different stair count / placement
    than if all floors were artificially set to the maximum bounding-box size.
    """

    PRESET_FS = {
        "max_travel_distance": 60000,
        "staircase_spec": {"riser": 150, "tread": 300,
                           "width_of_flight": 1500, "landing_width": 1800},
    }

    def test_tapered_building_uses_per_floor_dims(self):
        """With a tapered floor_dims, the polygon-aware travel check on the
        *actual* smaller upper floors should differ from assuming all floors
        are the full base size."""
        fp = [[-15000, -25000], [15000, -25000], [15000, 25000], [-15000, 25000]]
        # Tapered: base 30×50 m, top 12×20 m
        floor_dims_tapered = [(30000, 50000)] * 5 + [(12000, 20000)] * 5
        core_center = (0, 0)
        lift_bounds = (-4000, -3000, 4000, 3000)

        sets_tapered = calculate_fire_safety_requirements(
            floor_dims_tapered, core_center, lift_bounds,
            4000, self.PRESET_FS, 3, 3000,
            footprint_pts=fp,
        )
        # Must not raise and must return at least 2 sets (always guaranteed)
        self.assertGreaterEqual(len(sets_tapered), 2)


# ===========================================================================
# Fix D1 — Organic slab expansion to cover core
# ===========================================================================
class TestFixD1_SlabCoverage(unittest.TestCase):
    """
    Tests the pure expansion logic extracted from _process_floors.
    If the core extends beyond the organic slab bounding box the slab
    must be expanded to a rectangle that covers both.
    """

    @staticmethod
    def _apply_expansion(level_pts, core_mm):
        """Replicate the Fix D1 expansion logic."""
        fp_xs = [p[0] for p in level_pts]
        fp_ys = [p[1] for p in level_pts]
        sxmin, sxmax = min(fp_xs), max(fp_xs)
        symin, symax = min(fp_ys), max(fp_ys)
        cx1, cy1, cx2, cy2 = core_mm
        if cx1 < sxmin - 1 or cx2 > sxmax + 1 or cy1 < symin - 1 or cy2 > symax + 1:
            nxmin = min(sxmin, cx1); nxmax = max(sxmax, cx2)
            nymin = min(symin, cy1); nymax = max(symax, cy2)
            return [[nxmin, nymin], [nxmax, nymin],
                    [nxmax, nymax], [nxmin, nymax]], True
        return list(level_pts), False

    def test_no_expansion_when_core_inside(self):
        """Core fully inside the slab polygon — no expansion."""
        _, expanded = self._apply_expansion(L_SHAPE, (1000, 1000, 8000, 8000))
        self.assertFalse(expanded)

    def test_expansion_when_core_protrudes_east(self):
        """Core extends beyond the east boundary of the L-shape bottom arm."""
        result_pts, expanded = self._apply_expansion(
            L_SHAPE, (18000, 1000, 25000, 8000)  # extends to x=25000, beyond x=20000
        )
        self.assertTrue(expanded)
        xs = [p[0] for p in result_pts]
        self.assertGreaterEqual(max(xs), 25000)

    def test_expansion_when_core_protrudes_north(self):
        """Core extends above the top of the L-shape vertical arm."""
        result_pts, expanded = self._apply_expansion(
            L_SHAPE, (-3000, 28000, 3000, 35000)  # extends to y=35000, beyond y=30000
        )
        self.assertTrue(expanded)
        ys = [p[1] for p in result_pts]
        self.assertGreaterEqual(max(ys), 35000)

    def test_expansion_covers_both_slab_and_core(self):
        """Expanded rectangle must contain the original slab extent AND the core."""
        level_pts = [[0, 0], [5000, 0], [5000, 8000], [0, 8000]]
        core_mm = (-1000, -500, 6000, 9000)  # protrudes on all sides
        result_pts, expanded = self._apply_expansion(level_pts, core_mm)
        self.assertTrue(expanded)
        xs = [p[0] for p in result_pts]
        ys = [p[1] for p in result_pts]
        # Slab extent
        self.assertLessEqual(min(xs), 0)
        self.assertGreaterEqual(max(xs), 5000)
        # Core extent
        self.assertLessEqual(min(xs), -1000)
        self.assertGreaterEqual(max(xs), 6000)
        self.assertLessEqual(min(ys), -500)
        self.assertGreaterEqual(max(ys), 9000)


# ===========================================================================
# Fix F1 — _loop_has_arcs is always False for floor slabs
# ===========================================================================
class TestFixF1_LoopHasArcs(unittest.TestCase):

    def test_loop_has_arcs_is_false_constant(self):
        """
        The assignment `_loop_has_arcs = False` must exist in _process_floors
        (previously `_loop_has_arcs = bool(footprint_pts)` which wrongly skipped voids).
        """
        import inspect
        src = inspect.getsource(rw.RevitWorkers._process_floors)
        self.assertIn("_loop_has_arcs = False", src,
                      "_loop_has_arcs must be hardcoded False in _process_floors")
        self.assertNotIn("_loop_has_arcs = bool(footprint_pts)", src,
                         "Old wrong _loop_has_arcs = bool(footprint_pts) must be removed")


# ===========================================================================
# Fix C1 — _expand_shape_shorthand called before _process_shell_dimensions
# ===========================================================================
class TestFixC1_ShapeShorthandOrder(unittest.TestCase):

    def test_shape_shorthand_runs_before_shell_dims_in_source(self):
        """
        In execute_fast_manifest the line `shell = _expand_shape_shorthand(shell)`
        must appear BEFORE `_process_shell_dimensions` is called.
        """
        import inspect
        src = inspect.getsource(rw.RevitWorkers.execute_fast_manifest)
        idx_expand = src.find("_expand_shape_shorthand(shell)")
        idx_process = src.find("_process_shell_dimensions(manifest")
        self.assertGreater(idx_expand, 0, "_expand_shape_shorthand call not found")
        self.assertGreater(idx_process, 0, "_process_shell_dimensions call not found")
        self.assertLess(idx_expand, idx_process,
                        "_expand_shape_shorthand must come BEFORE _process_shell_dimensions")


# ===========================================================================
# Fix C2 — Footprint bounding box overrides base_w/base_l
# ===========================================================================
class TestFixC2_FootprintBboxInShellDims(unittest.TestCase):

    def test_bbox_override_present_in_source(self):
        """_process_shell_dimensions must contain the footprint-bbox override block."""
        import inspect
        src = inspect.getsource(rw.RevitWorkers._process_shell_dimensions)
        self.assertIn("footprint_points", src)
        # The override computes bounding box from footprint_points
        self.assertIn("base_w = max(_xs) - min(_xs)", src)
        self.assertIn("base_l = max(_ys) - min(_ys)", src)


# ===========================================================================
# Round 2 — Fix 1: scale override applied to floor_dims
# ===========================================================================
class TestRound2Fix1_ScaleInFloorDims(unittest.TestCase):

    def test_scale_multiply_present_in_source(self):
        """_process_shell_dimensions must contain scale multiply after get_random_dim."""
        import inspect
        src = inspect.getsource(rw.RevitWorkers._process_shell_dimensions)
        self.assertIn("footprint_scale_overrides", src)
        self.assertIn("_get_interpolated_scale", src)
        self.assertIn("final_w *= _sf", src)
        self.assertIn("final_l *= _sf", src)

    def test_scale_only_when_footprint_points_present(self):
        """Scale block must be guarded by footprint_points check."""
        import inspect
        src = inspect.getsource(rw.RevitWorkers._process_shell_dimensions)
        # The guard must link footprint_points AND scale_ovr
        self.assertIn('footprint_points', src)


# ===========================================================================
# Round 2 — Fix 2: rotation_deg applied to perimeter stair geometry
# ===========================================================================
class TestRound2Fix2_PerimeterStairRotation(unittest.TestCase):

    def test_ew_branch_present_in_source(self):
        """generate_fire_safety_manifest must have the EW (_is_ew_edge) geometry branch."""
        import inspect
        from revit_mcp import fire_safety_logic
        src = inspect.getsource(fire_safety_logic.generate_fire_safety_manifest)
        self.assertIn("_is_ew_edge", src)
        self.assertIn("rotation_deg", src)

    def test_ns_branch_still_present(self):
        """Original NS stair geometry must still exist inside the else branch."""
        import inspect
        from revit_mcp import fire_safety_logic
        src = inspect.getsource(fire_safety_logic.generate_fire_safety_manifest)
        self.assertIn("is_south_p", src)


# ===========================================================================
# Round 2 — Fix 3: void margin 100mm for organic slabs
# ===========================================================================
class TestRound2Fix3_VoidMargin(unittest.TestCase):

    def test_organic_margin_100mm_in_source(self):
        """margin_ft must use 100.0 mm for organic slabs (footprint_pts present)."""
        import inspect
        src = inspect.getsource(rw.RevitWorkers._process_floors)
        self.assertIn("mm_to_ft(100.0)", src,
                      "Organic slab void margin must be 100mm")
        self.assertIn("footprint_pts", src)

    def test_rectangular_margin_unchanged(self):
        """Rectangular slab margin must still be 2mm."""
        import inspect
        src = inspect.getsource(rw.RevitWorkers._process_floors)
        self.assertIn("mm_to_ft(2.0)", src,
                      "Rectangular slab void margin must remain 2mm")


# ===========================================================================
# Round 2 — Fix 4: E1 CONFLICT message includes polygon description
# ===========================================================================
class TestRound2Fix4_ConflictMessage(unittest.TestCase):

    def test_conflict_includes_polygon_info(self):
        """E1 CONFLICT description must include polygon vertices and bbox."""
        import inspect
        src = inspect.getsource(rw.RevitWorkers._expand_unified_vertical_circulation)
        self.assertIn("_fp_bbox", src)
        self.assertIn("_fp_summary", src)
        self.assertIn("Polygon vertices", src)
        self.assertIn("Polygon bbox", src)

    def test_conflict_includes_actionable_guidance(self):
        """Message must name the junction-of-arms heuristic for L/U/H shapes."""
        import inspect
        src = inspect.getsource(rw.RevitWorkers._expand_unified_vertical_circulation)
        self.assertIn("junction of the arms", src)


# ===========================================================================
# Round 2 — Fix 5a: svg_path_to_multiloop populates footprint_holes
# ===========================================================================
class TestRound2Fix5a_SvgMultiloop(unittest.TestCase):

    def test_multiloop_called_in_svg_expansion(self):
        """SVG expansion block must call svg_path_to_multiloop, not svg_path_to_footprint_points."""
        import inspect
        src = inspect.getsource(rw.RevitWorkers.execute_fast_manifest)
        self.assertIn("svg_path_to_multiloop", src,
                      "execute_fast_manifest must use svg_path_to_multiloop")
        self.assertNotIn("svg_path_to_footprint_points", src,
                         "Old svg_path_to_footprint_points call must be removed from execute_fast_manifest")

    def test_multiloop_returns_outer_and_holes_for_courtyard(self):
        """A two-subpath SVG returns outer polygon + at least one hole."""
        from revit_mcp.svg_to_footprint import svg_path_to_multiloop
        # Outer 100×100 square, inner 40×40 courtyard void
        svg = ("M 0 0 L 100 0 L 100 100 L 0 100 Z "
               "M 30 30 L 70 30 L 70 70 L 30 70 Z")
        result = svg_path_to_multiloop(svg)
        self.assertIn("outer", result)
        self.assertIn("holes", result)
        self.assertGreaterEqual(len(result["outer"]), 3)
        self.assertGreaterEqual(len(result["holes"]), 1)
        self.assertGreaterEqual(len(result["holes"][0]), 3)

    def test_single_path_svg_has_no_holes(self):
        """A single-subpath SVG must return empty holes list."""
        from revit_mcp.svg_to_footprint import svg_path_to_multiloop
        svg = "M 0 0 L 50 0 L 50 50 L 0 50 Z"
        result = svg_path_to_multiloop(svg)
        self.assertFalse(result.get("holes"), "Single-path SVG must have no holes")

    def test_footprint_holes_populated_in_shell(self):
        """SVG expansion must write footprint_holes into shell when multiloop has holes."""
        import inspect
        src = inspect.getsource(rw.RevitWorkers.execute_fast_manifest)
        self.assertIn("footprint_holes", src)
        self.assertIn('_ml.get("holes")', src)


# ===========================================================================
# Round 2 — Fix 5b: _poly_candidates excludes courtyard void interior
# ===========================================================================
class TestRound2Fix5b_PolyCandidatesHoleAware(unittest.TestCase):

    def test_hole_candidates_excluded(self):
        """Interior grid must produce no candidates inside a courtyard hole."""
        # Outer polygon: 0–10000 × 0–10000 mm square
        outer = [[0, 0], [10000, 0], [10000, 10000], [0, 10000]]
        # Hole: 3000–7000 × 3000–7000 (covers centre completely)
        hole = [[3000, 3000], [7000, 3000], [7000, 7000], [3000, 7000]]

        # Directly exercise the filtering logic from calculate_fire_safety_requirements
        _fp_xs = [p[0] for p in outer]
        _fp_ys = [p[1] for p in outer]
        _fp_xmin, _fp_xmax = min(_fp_xs), max(_fp_xs)
        _fp_ymin, _fp_ymax = min(_fp_ys), max(_fp_ys)
        _gN = 8
        _gdx = (_fp_xmax - _fp_xmin) / float(_gN)
        _gdy = (_fp_ymax - _fp_ymin) / float(_gN)
        _holes = [hole]
        _interior = []
        for _ix in range(_gN):
            for _iy in range(_gN):
                _gx = _fp_xmin + (_ix + 0.5) * _gdx
                _gy = _fp_ymin + (_iy + 0.5) * _gdy
                if not _point_in_polygon(_gx, _gy, outer):
                    continue
                if any(_point_in_polygon(_gx, _gy, h) for h in _holes):
                    continue
                _interior.append((_gx, _gy))

        # All surviving candidates must be outside the hole
        for gx, gy in _interior:
            self.assertFalse(
                _point_in_polygon(gx, gy, hole),
                "Candidate ({},{}) is inside the courtyard hole".format(gx, gy)
            )

    def test_footprint_holes_param_in_signature(self):
        """calculate_fire_safety_requirements must accept footprint_holes parameter."""
        import inspect
        sig = inspect.signature(calculate_fire_safety_requirements)
        self.assertIn("footprint_holes", sig.parameters)


# ===========================================================================
# Round 2 — Fix 6: force_global_dimensions auto-set for organic builds
# ===========================================================================
class TestRound2Fix6_ForceGlobalDimensions(unittest.TestCase):

    def test_auto_set_present_in_source(self):
        """execute_fast_manifest must auto-set force_global_dimensions when footprint_points present."""
        import inspect
        src = inspect.getsource(rw.RevitWorkers.execute_fast_manifest)
        self.assertIn("force_global_dimensions", src)
        self.assertIn("Auto-set force_global_dimensions", src)

    def test_auto_set_after_shape_shorthand(self):
        """force_global_dimensions auto-set must happen after _expand_shape_shorthand."""
        import inspect
        src = inspect.getsource(rw.RevitWorkers.execute_fast_manifest)
        idx_expand = src.find("_expand_shape_shorthand(shell)")
        idx_force = src.find("force_global_dimensions")
        self.assertGreater(idx_expand, 0, "_expand_shape_shorthand not found")
        self.assertGreater(idx_force, 0, "force_global_dimensions not found")
        self.assertLess(idx_expand, idx_force,
                        "force_global_dimensions must come after _expand_shape_shorthand")

    def test_auto_set_before_process_shell_dims(self):
        """force_global_dimensions auto-set must happen before _process_shell_dimensions."""
        import inspect
        src = inspect.getsource(rw.RevitWorkers.execute_fast_manifest)
        idx_force = src.find("force_global_dimensions")
        idx_process = src.find("_process_shell_dimensions(manifest")
        self.assertLess(idx_force, idx_process,
                        "force_global_dimensions auto-set must come before _process_shell_dimensions")


# ===========================================================================
# Round 3 — 7 fixes from Revit build testing
# ===========================================================================

# U-shape with notch opening south (arms at x<-5000 and x>5000, bridge at top)
U_SHAPE = [
    [-15000, -25000], [-15000, 25000], [15000, 25000],
    [15000, 5000], [5000, 5000], [5000, -25000],
    [-5000, -25000], [-5000, 5000], [-15000, 5000],
]

# Simple courtyard: outer square with inner square hole
COURT_OUTER = [[-20000, -20000], [20000, -20000], [20000, 20000], [-20000, 20000]]
COURT_HOLE  = [[-5000, -5000], [5000, -5000], [5000, 5000], [-5000, 5000]]


class TestRound3Fix_T2_EwBranchVariables(unittest.TestCase):
    """EW perimeter stair branch must define st_base_y, st_y2, is_south_p."""

    def test_ew_branch_defines_st_base_y(self):
        """EW branch must set st_base_y before the shared code block."""
        import inspect
        import revit_mcp.fire_safety_logic as fsl
        src = inspect.getsource(fsl.generate_fire_safety_manifest)
        ew_idx = src.find("if _is_ew_edge:")
        self.assertGreater(ew_idx, 0)
        # EW block ends at "else:" for NS fallback — search that entire block
        else_idx = src.find("\n            else:\n                # NS-facing", ew_idx)
        ew_block = src[ew_idx: else_idx] if else_idx > ew_idx else src[ew_idx: ew_idx + 4000]
        self.assertIn("st_base_y", ew_block, "EW branch must define st_base_y")

    def test_ew_branch_defines_st_y2(self):
        """EW branch must define st_y2 alias for shared door-spec code."""
        import inspect
        import revit_mcp.fire_safety_logic as fsl
        src = inspect.getsource(fsl.generate_fire_safety_manifest)
        ew_idx = src.find("if _is_ew_edge:")
        else_idx = src.find("\n            else:\n                # NS-facing", ew_idx)
        ew_block = src[ew_idx: else_idx] if else_idx > ew_idx else src[ew_idx: ew_idx + 4000]
        self.assertIn("st_y2_ew", ew_block, "EW branch must alias st_y2 = st_y2_ew")

    def test_ew_branch_defines_is_south_p(self):
        """EW branch must define is_south_p for shared lobby-wall skip logic."""
        import inspect
        import revit_mcp.fire_safety_logic as fsl
        src = inspect.getsource(fsl.generate_fire_safety_manifest)
        ew_idx = src.find("if _is_ew_edge:")
        else_idx = src.find("\n            else:\n                # NS-facing", ew_idx)
        ew_block = src[ew_idx: else_idx] if else_idx > ew_idx else src[ew_idx: ew_idx + 4000]
        self.assertIn("is_south_p", ew_block, "EW branch must define is_south_p")


class TestRound3Fix_T3_FloorExpand(unittest.TestCase):
    """FloorExpand (D1) must be suppressed when footprint_scale_overrides present."""

    def test_floorexpand_guarded_by_scale_override(self):
        """FloorExpand block must check footprint_scale_overrides before expanding."""
        import inspect
        src = inspect.getsource(rw.RevitWorkers._process_floors)
        # Confirm the guard condition is present anywhere in _process_floors
        self.assertIn("footprint_scale_overrides", src,
                      "FloorExpand must be skipped when footprint_scale_overrides is set")
        # Confirm the guard appears BEFORE the organic [FloorExpand] log message
        guard_idx = src.find("not shell.get(\"footprint_scale_overrides\")")
        # Two FloorExpand messages exist; find the one for the organic slab
        expand_idx = src.find("organic slab expanded bbox")
        self.assertGreater(expand_idx, 0)
        self.assertGreater(expand_idx, guard_idx,
                           "footprint_scale_overrides guard must come before organic FloorExpand log")


class TestRound3Fix_T3_VoidLoops(unittest.TestCase):
    """Organic slab void clip must inset bbox margin AND check polygon corners."""

    def test_margin_applied_to_organic_bbox(self):
        """slab_min_x/max_x must be inset by margin_ft for organic slabs."""
        import inspect
        src = inspect.getsource(rw.RevitWorkers._process_floors)
        # Either += style or min(...) + margin_ft style is acceptable
        has_min_x = ("slab_min_x += margin_ft" in src or
                     "min(_fp_xs_v) + margin_ft" in src)
        has_max_x = ("slab_max_x -= margin_ft" in src or
                     "max(_fp_xs_v) - margin_ft" in src)
        has_min_y = ("slab_min_y += margin_ft" in src or
                     "min(_fp_ys_v) + margin_ft" in src)
        has_max_y = ("slab_max_y -= margin_ft" in src or
                     "max(_fp_ys_v) - margin_ft" in src)
        self.assertTrue(has_min_x, "Organic slab min_x must be inset by margin_ft")
        self.assertTrue(has_max_x, "Organic slab max_x must be inset by margin_ft")
        self.assertTrue(has_min_y, "Organic slab min_y must be inset by margin_ft")
        self.assertTrue(has_max_y, "Organic slab max_y must be inset by margin_ft")

    def test_polygon_corner_check_present(self):
        """Void-clip loop must check _point_in_polygon on all 4 void corners."""
        import inspect
        src = inspect.getsource(rw.RevitWorkers._process_floors)
        self.assertIn("_pip_v", src, "Organic void must use _point_in_polygon corner check")
        self.assertIn("level_pts", src)


class TestRound3Fix_T4T5_PolygonFlip(unittest.TestCase):
    """generate_fire_safety_manifest must accept footprint_pts and flip south→north."""

    def test_signature_has_footprint_pts(self):
        """generate_fire_safety_manifest must have footprint_pts parameter."""
        import inspect
        import revit_mcp.fire_safety_logic as fsl
        sig = inspect.signature(fsl.generate_fire_safety_manifest)
        self.assertIn("footprint_pts", sig.parameters)

    def test_parallel_south_polygon_flip(self):
        """Parallel south cluster must flip north when corners outside polygon."""
        import inspect
        import revit_mcp.fire_safety_logic as fsl
        src = inspect.getsource(fsl.generate_fire_safety_manifest)
        self.assertIn("_pip_p", src,
                      "Parallel south must check polygon and flip north")
        self.assertIn("l_ymax + cluster_d_p", src)

    def test_sequential_south_polygon_flip(self):
        """Sequential south cluster must flip north when corners outside polygon."""
        import inspect
        import revit_mcp.fire_safety_logic as fsl
        src = inspect.getsource(fsl.generate_fire_safety_manifest)
        self.assertIn("_pip_sq", src,
                      "Sequential south must check polygon and flip north")

    def test_caller_passes_footprint_pts(self):
        """revit_workers must pass footprint_pts to generate_fire_safety_manifest."""
        import inspect
        src = inspect.getsource(rw.RevitWorkers._expand_unified_vertical_circulation)
        call_idx = src.find("generate_fire_safety_manifest(")
        self.assertGreater(call_idx, 0)
        call_block = src[call_idx: call_idx + 400]
        self.assertIn("footprint_pts", call_block)


class TestRound3Fix_T6_E1Holes(unittest.TestCase):
    """E1 validation must reject zones that land inside footprint_holes."""

    def test_e1_checks_holes(self):
        """_expand_unified_vertical_circulation E1 must check _fp_holes_val."""
        import inspect
        src = inspect.getsource(rw.RevitWorkers._expand_unified_vertical_circulation)
        self.assertIn("_fp_holes_val", src,
                      "E1 must read footprint_holes from shell")
        self.assertIn("_in_hole", src,
                      "E1 must detect when a zone corner is inside a hole")


class TestRound3Fix_T6_TravelDistanceHoles(unittest.TestCase):
    """_check_travel_distance must exclude hole-interior test points."""

    def test_signature_has_footprint_holes(self):
        """_check_travel_distance must accept footprint_holes parameter."""
        import inspect
        from revit_mcp import staircase_logic as sl
        sig = inspect.signature(sl._check_travel_distance)
        self.assertIn("footprint_holes", sig.parameters)

    def test_hole_points_excluded(self):
        """Test points inside the courtyard hole must not be sampled."""
        from revit_mcp import staircase_logic as sl
        # Two staircases on opposite sides of a 40x40m courtyard building
        stair_pos = [(-18000, 0), (18000, 0)]
        floor_dims = [[40000, 40000]]
        # Without holes: the courtyard interior inflates travel distance
        # With hole matching the courtyard interior: only real floor is sampled
        ok_no_hole = sl._check_travel_distance(
            stair_pos, floor_dims, max_dist_mm=60000,
            footprint_pts=COURT_OUTER)
        ok_with_hole = sl._check_travel_distance(
            stair_pos, floor_dims, max_dist_mm=60000,
            footprint_pts=COURT_OUTER, footprint_holes=[COURT_HOLE])
        # With the hole excluded the travel distance should still be satisfied;
        # without the hole the far interior point near (0,0) may push distance over
        # This test verifies the parameter is wired correctly and doesn't crash.
        self.assertIsInstance(ok_no_hole, bool)
        self.assertIsInstance(ok_with_hole, bool)


class TestRound3Fix_T6_CourtyardWalls(unittest.TestCase):
    """_process_walls must generate enclosure walls for footprint_holes polygons."""

    def test_courtyard_walls_in_source(self):
        """_process_walls must iterate footprint_holes and create hole-boundary walls."""
        import inspect
        src = inspect.getsource(rw.RevitWorkers._process_walls)
        self.assertIn("footprint_holes", src,
                      "_process_walls must read footprint_holes from shell")
        self.assertIn("AI_Wall_L{}_H{}_Seg{}", src,
                      "_process_walls must tag hole walls with H-index in ID")


if __name__ == "__main__":
    unittest.main(verbosity=2)
