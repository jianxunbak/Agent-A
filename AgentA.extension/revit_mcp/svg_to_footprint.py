# -*- coding: utf-8 -*-
"""
svg_to_footprint.py
Converts an SVG path string into the footprint_points format used by revit_workers.py.

footprint_points format:
  [[x, y], ...]  — straight segment vertex
  [[x, y, {"mid_x": mx, "mid_y": my}], ...]  — arc segment vertex (mid point ON the arc)

SVG commands supported:
  M / m  — moveto
  L / l  — lineto
  H / h  — horizontal lineto
  V / v  — vertical lineto
  A / a  — elliptical arc (converted to 3-point circular arc approximation)
  C / c  — cubic bezier (approximated as chain of circular arcs)
  S / s  — smooth cubic bezier (approximated)
  Q / q  — quadratic bezier (approximated)
  T / t  — smooth quadratic bezier (approximated)
  Z / z  — closepath (ignored; polygon is always closed)

All SVG coordinates are treated as millimetres and re-centred on [0, 0].

Usage:
  from revit_mcp.svg_to_footprint import svg_path_to_footprint_points
  pts = svg_path_to_footprint_points("M -15000 -20000 A 28000 28000 0 0 1 15000 -20000 L 15000 20000 L -15000 20000 Z")
"""

import math
import re


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def svg_path_to_multiloop(svg_path, arc_segments=8):
    """
    Parse an SVG path that may contain multiple subpaths (outer boundary + inner voids).

    Returns {"outer": [...footprint_points...], "holes": [[...], ...]}

    First M...Z subpath  → outer boundary (CCW winding, Revit convention).
    Subsequent M...Z subpaths → inner void/hole loops (CW winding so Revit
      reads them as holes when passed as additional CurveLoops to Floor.Create).

    Single-subpath paths return {"outer": [...], "holes": []}.
    Raises ValueError if any subpath produces a self-intersecting polygon.
    """
    tokens = _tokenise(svg_path)
    token_groups = _split_subpath_tokens(tokens)

    if not token_groups:
        raise ValueError("SVG path produced no usable subpaths")

    # Parse each subpath preserving original SVG coordinates
    raw_loops = []
    for sub_tokens in token_groups:
        raw_segments = _parse_commands(sub_tokens)
        points = _segments_to_footprint(raw_segments, arc_segments)
        if points:
            raw_loops.append(points)

    if not raw_loops:
        raise ValueError("SVG path produced no valid subpaths")

    # Compute offset from OUTER path bounding box; apply same shift to ALL loops
    outer_xs = [p[0] for p in raw_loops[0]]
    outer_ys = [p[1] for p in raw_loops[0]]
    dx = (max(outer_xs) + min(outer_xs)) / 2.0
    dy = (max(outer_ys) + min(outer_ys)) / 2.0
    centred_loops = [_shift_points(lp, dx, dy) for lp in raw_loops]

    # Wind outer CCW, inner loops CW; validate each
    result_loops = []
    for i, lp in enumerate(centred_loops):
        lp = _ensure_ccw(lp) if i == 0 else _ensure_cw(lp)
        xy = [(p[0], p[1]) for p in lp]
        if _polygon_self_intersects(xy):
            raise ValueError(
                "footprint_svg subpath {} produces a self-intersecting polygon — "
                "each subpath must be a simple closed outline with no crossing edges.".format(i + 1)
            )
        result_loops.append(lp)

    return {"outer": result_loops[0], "holes": result_loops[1:], "offset": [dx, dy]}


def svg_path_to_footprint_points(svg_path, arc_segments=8):
    """
    Parse an SVG path and return footprint_points.

    arc_segments: number of circular-arc sub-segments used to approximate
                  each bezier curve.  Higher = smoother, more wall elements.
                  8 is a good default for architecture.

    Returns list of [x, y] or [x, y, {"mid_x": mx, "mid_y": my}] vertices,
    centred on [0, 0], counter-clockwise winding (Revit convention).

    Raises ValueError if the resulting polygon is self-intersecting (Revit
    Floor.Create requires a simple closed polygon).
    """
    tokens = _tokenise(svg_path)
    raw_segments = _parse_commands(tokens)          # list of Segment namedtuples
    points = _segments_to_footprint(raw_segments, arc_segments)
    if not points:
        raise ValueError("SVG path produced no usable segments")
    points = _centre_footprint(points)
    points = _ensure_ccw(points)

    # Validate: Revit requires a simple (non-self-intersecting) polygon.
    # Linearise arc vertices for the intersection check.
    xy = [(p[0], p[1]) for p in points]
    if _polygon_self_intersects(xy):
        raise ValueError(
            "footprint_svg produces a self-intersecting polygon — Revit floors "
            "require a simple closed outline with no edges crossing each other. "
            "For an S-shape use two separate offset rectangles/ovals (volumes[]) "
            "or draw a single continuous outer silhouette that traces the S perimeter "
            "without the path crossing back through itself."
        )

    return points


def _seg_intersect(p1, p2, p3, p4):
    """Return True if segment p1-p2 properly intersects p3-p4 (shared endpoints excluded)."""
    def cross(o, a, b):
        return (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])
    d1 = cross(p3, p4, p1)
    d2 = cross(p3, p4, p2)
    d3 = cross(p1, p2, p3)
    d4 = cross(p1, p2, p4)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True
    return False


def _polygon_self_intersects(xy):
    """
    Check whether a polygon (list of (x,y) tuples) has any self-intersecting edges
    OR any duplicate (nearly-coincident) non-adjacent vertices.

    Duplicate vertices indicate the path visits the same point twice — a sign of
    an S-shape or figure-8 that folds back through itself.  These pass an edge-only
    intersection check but still produce invalid Revit CurveLoops.
    """
    n = len(xy)
    if n < 4:
        return False

    # Check for near-duplicate non-adjacent vertices (tolerance 50 mm).
    # Two vertices being near-identical at non-adjacent positions means the
    # path doubles back, which Revit treats as self-intersecting.
    for i in range(n):
        for j in range(i + 2, n):
            if j == n - 1 and i == 0:
                continue  # last and first share the closing edge
            dx = xy[i][0] - xy[j][0]
            dy = xy[i][1] - xy[j][1]
            if dx*dx + dy*dy < 50*50:
                return True

    # Edge intersection check
    for i in range(n):
        p1, p2 = xy[i], xy[(i+1) % n]
        for j in range(i+2, n):
            if j == n-1 and i == 0:
                continue
            p3, p4 = xy[j], xy[(j+1) % n]
            if _seg_intersect(p1, p2, p3, p4):
                return True
    return False


# ---------------------------------------------------------------------------
# Tokeniser
# ---------------------------------------------------------------------------

_CMD_RE = re.compile(r"([MmLlHhVvAaCcSsQqTtZz])|([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")

def _tokenise(path):
    return [m.group() for m in _CMD_RE.finditer(path)]


# ---------------------------------------------------------------------------
# Command parser  →  list of Segment objects
# ---------------------------------------------------------------------------

class _Seg:
    __slots__ = ("kind", "p1", "p2", "mid")
    def __init__(self, kind, p1, p2, mid=None):
        self.kind = kind   # "line" or "arc"
        self.p1 = p1       # (x, y)
        self.p2 = p2       # (x, y)
        self.mid = mid     # (mx, my) or None  — only for "arc"


def _parse_commands(tokens):
    """Walk SVG tokens and produce a flat list of _Seg objects."""
    segments = []
    idx = 0
    n = len(tokens)
    cmd = None
    cur = (0.0, 0.0)
    start = (0.0, 0.0)

    def read_float():
        nonlocal idx
        v = float(tokens[idx]); idx += 1; return v

    def is_num():
        return idx < n and tokens[idx] not in "MmLlHhVvAaCcSsQqTtZz"

    while idx < n:
        t = tokens[idx]
        if t in "MmLlHhVvAaCcSsQqTtZz":
            cmd = t; idx += 1
            # First M/m is implicit moveto; subsequent coords are lineto
        else:
            pass  # fall through to number consumption

        if cmd in ("M", "m"):
            x = read_float(); y = read_float()
            if cmd == "m": x += cur[0]; y += cur[1]
            cur = (x, y); start = cur
            cmd = "L" if cmd == "M" else "l"  # subsequent coords → lineto

        elif cmd in ("L", "l"):
            while is_num():
                x = read_float(); y = read_float()
                if cmd == "l": x += cur[0]; y += cur[1]
                segments.append(_Seg("line", cur, (x, y)))
                cur = (x, y)

        elif cmd in ("H", "h"):
            while is_num():
                x = read_float()
                if cmd == "h": x += cur[0]
                nx, ny = (x, cur[1])
                segments.append(_Seg("line", cur, (nx, ny)))
                cur = (nx, ny)

        elif cmd in ("V", "v"):
            while is_num():
                y = read_float()
                if cmd == "v": y += cur[1]
                nx, ny = (cur[0], y)
                segments.append(_Seg("line", cur, (nx, ny)))
                cur = (nx, ny)

        elif cmd in ("A", "a"):
            while is_num():
                rx = read_float(); ry = read_float()
                x_rot = read_float()
                large = int(read_float()); sweep = int(read_float())
                ex = read_float(); ey = read_float()
                if cmd == "a": ex += cur[0]; ey += cur[1]
                arc_segs = _svg_arc_to_segments(cur, rx, ry, x_rot, large, sweep, (ex, ey))
                segments.extend(arc_segs)
                cur = (ex, ey)

        elif cmd in ("C", "c"):
            while is_num():
                x1 = read_float(); y1 = read_float()
                x2 = read_float(); y2 = read_float()
                ex = read_float(); ey = read_float()
                if cmd == "c":
                    x1 += cur[0]; y1 += cur[1]
                    x2 += cur[0]; y2 += cur[1]
                    ex += cur[0]; ey += cur[1]
                segs = _cubic_bezier_to_segments(cur, (x1,y1), (x2,y2), (ex,ey))
                segments.extend(segs)
                cur = (ex, ey)

        elif cmd in ("S", "s"):
            prev_cp = cur
            while is_num():
                x2 = read_float(); y2 = read_float()
                ex = read_float(); ey = read_float()
                if cmd == "s":
                    x2 += cur[0]; y2 += cur[1]
                    ex += cur[0]; ey += cur[1]
                # reflect previous control point
                x1 = 2*cur[0] - prev_cp[0]; y1 = 2*cur[1] - prev_cp[1]
                segs = _cubic_bezier_to_segments(cur, (x1,y1), (x2,y2), (ex,ey))
                segments.extend(segs)
                prev_cp = (x2, y2); cur = (ex, ey)

        elif cmd in ("Q", "q"):
            while is_num():
                x1 = read_float(); y1 = read_float()
                ex = read_float(); ey = read_float()
                if cmd == "q":
                    x1 += cur[0]; y1 += cur[1]
                    ex += cur[0]; ey += cur[1]
                # elevate quadratic → cubic
                cx1 = cur[0] + 2/3*(x1-cur[0]); cy1 = cur[1] + 2/3*(y1-cur[1])
                cx2 = ex + 2/3*(x1-ex);         cy2 = ey + 2/3*(y1-ey)
                segs = _cubic_bezier_to_segments(cur, (cx1,cy1), (cx2,cy2), (ex,ey))
                segments.extend(segs)
                cur = (ex, ey)

        elif cmd in ("T", "t"):
            prev_cp = cur
            while is_num():
                ex = read_float(); ey = read_float()
                if cmd == "t": ex += cur[0]; ey += cur[1]
                x1 = 2*cur[0] - prev_cp[0]; y1 = 2*cur[1] - prev_cp[1]
                cx1 = cur[0] + 2/3*(x1-cur[0]); cy1 = cur[1] + 2/3*(y1-cur[1])
                cx2 = ex + 2/3*(x1-ex);         cy2 = ey + 2/3*(y1-ey)
                segs = _cubic_bezier_to_segments(cur, (cx1,cy1), (cx2,cy2), (ex,ey))
                segments.extend(segs)
                prev_cp = (x1, y1); cur = (ex, ey)

        elif cmd in ("Z", "z"):
            if cur != start:
                segments.append(_Seg("line", cur, start))
            cur = start
        else:
            idx += 1  # skip unknown

    return segments


# ---------------------------------------------------------------------------
# SVG arc  →  _Seg list  (each segment is a 3-point circular arc)
# ---------------------------------------------------------------------------

def _svg_arc_to_segments(p1, rx, ry, x_rot_deg, large_arc, sweep, p2):
    """
    Convert an SVG elliptical arc to a list of _Seg(arc) objects.
    Decomposes into at most 4 quarter-arcs so each piece maps cleanly to
    a Revit 3-point circular arc.  Non-circular (rx≠ry) arcs are approximated
    by using the average radius.
    """
    x1, y1 = p1; x2, y2 = p2
    if abs(x1-x2) < 0.1 and abs(y1-y2) < 0.1:
        return []

    r = (abs(rx) + abs(ry)) / 2.0  # average radius for approximation
    if r < 1.0:
        return [_Seg("line", p1, p2)]

    # Find arc centre using SVG spec (simplified for circular case)
    cx, cy, theta1, dtheta = _svg_arc_centre(x1, y1, r, r, x_rot_deg, large_arc, sweep, x2, y2)

    # Split into sub-arcs of at most 90°
    max_arc = math.pi / 2
    n_segs = max(1, int(math.ceil(abs(dtheta) / max_arc)))
    segs = []
    t0 = theta1
    for i in range(n_segs):
        t1 = theta1 + dtheta * (i+1) / n_segs
        # Arc mid-point at half-angle
        tm = (t0 + t1) / 2.0
        ax = cx + r * math.cos(t0); ay = cy + r * math.sin(t0)
        bx = cx + r * math.cos(t1); by = cy + r * math.sin(t1)
        mx = cx + r * math.cos(tm); my = cy + r * math.sin(tm)
        segs.append(_Seg("arc", (ax, ay), (bx, by), (mx, my)))
        t0 = t1
    return segs


def _svg_arc_centre(x1, y1, rx, _ry, phi_deg, fa, fs, x2, y2):
    """SVG arc parameterisation → centre + angles (circular approximation)."""
    phi = math.radians(phi_deg)
    cos_phi = math.cos(phi); sin_phi = math.sin(phi)
    dx = (x1-x2)/2; dy = (y1-y2)/2
    x1p =  cos_phi*dx + sin_phi*dy
    y1p = -sin_phi*dx + cos_phi*dy
    r = rx  # circular approximation

    r_sq = r*r; x1p_sq = x1p*x1p; y1p_sq = y1p*y1p
    num = max(0.0, r_sq*(r_sq - x1p_sq - y1p_sq))
    den = r_sq*x1p_sq + r_sq*y1p_sq
    if den < 1e-10: den = 1e-10
    sq = math.sqrt(num/den)
    if fa == fs: sq = -sq
    cxp =  sq * r * x1p / r
    cyp = -sq * r * y1p / r
    cx = cos_phi*cxp - sin_phi*cyp + (x1+x2)/2
    cy = sin_phi*cxp + cos_phi*cyp + (y1+y2)/2

    def angle(ux, uy, vx, vy):
        dot = ux*vx + uy*vy
        mag = math.sqrt((ux*ux+uy*uy)*(vx*vx+vy*vy))
        if mag < 1e-10: return 0.0
        a = math.acos(max(-1.0, min(1.0, dot/mag)))
        if ux*vy - uy*vx < 0: a = -a
        return a

    theta1 = angle(1,0, (x1p-cxp)/r, (y1p-cyp)/r)
    dtheta = angle((x1p-cxp)/r, (y1p-cyp)/r, (-x1p-cxp)/r, (-y1p-cyp)/r)
    if not fs and dtheta > 0: dtheta -= 2*math.pi
    if fs and dtheta < 0:     dtheta += 2*math.pi
    return cx, cy, theta1, dtheta


# ---------------------------------------------------------------------------
# Cubic Bezier  →  _Seg list  (split into circular-arc approximations)
# ---------------------------------------------------------------------------

def _cubic_bezier_to_segments(p0, p1, p2, p3, depth=0, max_depth=4):
    """
    Recursively subdivide a cubic bezier until each piece is close enough to a
    circular arc, then convert each piece to a _Seg(arc).
    """
    # Flatness test: if the curve is nearly a straight line, use a line
    def _dist_sq(a, b): return (a[0]-b[0])**2 + (a[1]-b[1])**2
    chord_sq = _dist_sq(p0, p3)
    if chord_sq < 1.0:  # < 1 mm chord — degenerate
        return [_Seg("line", p0, p3)]

    # Compute max deviation of control points from chord
    def _point_to_line_sq(pt, a, b):
        dx = b[0]-a[0]; dy = b[1]-a[1]
        d2 = dx*dx+dy*dy
        if d2 < 1e-10: return _dist_sq(pt, a)
        t = ((pt[0]-a[0])*dx + (pt[1]-a[1])*dy) / d2
        t = max(0.0, min(1.0, t))
        return _dist_sq(pt, (a[0]+t*dx, a[1]+t*dy))

    dev = max(_point_to_line_sq(p1, p0, p3), _point_to_line_sq(p2, p0, p3))

    # Tolerance: 50 mm deviation is fine for architecture
    if dev < 50**2 or depth >= max_depth:
        mid = _cubic_bezier_point(p0, p1, p2, p3, 0.5)
        return [_Seg("arc", p0, p3, mid)]

    # Subdivide at t=0.5
    q0, q1, q2, q3, q4 = _cubic_bezier_split(p0, p1, p2, p3)
    left  = _cubic_bezier_to_segments(p0, q0, q1, q2, depth+1, max_depth)
    right = _cubic_bezier_to_segments(q2, q3, q4, p3, depth+1, max_depth)
    return left + right


def _cubic_bezier_point(p0, p1, p2, p3, t):
    mt = 1-t
    x = mt**3*p0[0] + 3*mt**2*t*p1[0] + 3*mt*t**2*p2[0] + t**3*p3[0]
    y = mt**3*p0[1] + 3*mt**2*t*p1[1] + 3*mt*t**2*p2[1] + t**3*p3[1]
    return (x, y)


def _cubic_bezier_split(p0, p1, p2, p3):
    """De Casteljau split at t=0.5, returns 5 new control points."""
    def mid(a, b): return ((a[0]+b[0])/2, (a[1]+b[1])/2)
    q0 = mid(p0, p1); q1 = mid(p1, p2); q2 = mid(p2, p3)
    r0 = mid(q0, q1); r1 = mid(q1, q2)
    s  = mid(r0, r1)
    return q0, r0, s, r1, q2


# ---------------------------------------------------------------------------
# Segment list  →  footprint_points
# ---------------------------------------------------------------------------

def _segments_to_footprint(segments, _arc_segments_unused=8):
    """
    Convert _Seg list to footprint_points.
    Consecutive segments are chained by their shared endpoints.
    Each vertex carries an arc mid-point dict if the outgoing segment is an arc.
    """
    if not segments:
        return []

    points = []
    for seg in segments:
        if seg.kind == "arc" and seg.mid is not None:
            points.append([seg.p1[0], seg.p1[1], {"mid_x": seg.mid[0], "mid_y": seg.mid[1]}])
        else:
            points.append([seg.p1[0], seg.p1[1]])

    return points


# ---------------------------------------------------------------------------
# Post-processing helpers
# ---------------------------------------------------------------------------

def _centre_footprint(points):
    """Shift footprint so its centroid is at [0, 0]."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    cx = (max(xs) + min(xs)) / 2
    cy = (max(ys) + min(ys)) / 2
    result = []
    for p in points:
        if len(p) == 3:
            d = p[2]
            result.append([p[0]-cx, p[1]-cy, {"mid_x": d["mid_x"]-cx, "mid_y": d["mid_y"]-cy}])
        else:
            result.append([p[0]-cx, p[1]-cy])
    return result


def _signed_area(points):
    """Shoelace formula — positive = CCW."""
    n = len(points)
    area = 0.0
    for i in range(n):
        x1, y1 = points[i][0], points[i][1]
        x2, y2 = points[(i+1) % n][0], points[(i+1) % n][1]
        area += (x1 * y2 - x2 * y1)
    return area / 2.0


def _split_subpath_tokens(tokens):
    """Split a flat SVG token list into per-subpath lists at Z...M boundaries."""
    subpaths = []
    current = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        current.append(t)
        if t in ("Z", "z"):
            # Look ahead: if the next command token is M/m, start a new subpath
            j = i + 1
            while j < len(tokens) and tokens[j] not in "MmLlHhVvAaCcSsQqTtZz":
                j += 1
            if j < len(tokens) and tokens[j] in ("M", "m"):
                subpaths.append(current)
                current = []
        i += 1
    if current:
        subpaths.append(current)
    return [s for s in subpaths if s]


def _shift_points(points, dx, dy):
    """Translate all points (and arc mid-points) by (-dx, -dy)."""
    result = []
    for p in points:
        if len(p) == 3:
            d = p[2]
            result.append([p[0] - dx, p[1] - dy,
                            {"mid_x": d["mid_x"] - dx, "mid_y": d["mid_y"] - dy}])
        else:
            result.append([p[0] - dx, p[1] - dy])
    return result


def _ensure_cw(points):
    """Reverse winding to CW (used for inner void/hole loops)."""
    if _signed_area(points) > 0:  # positive = CCW → reverse to CW
        rev = list(reversed(points))
        result = [[p[0], p[1]] for p in rev]
        n = len(result)
        for i in range(n):
            p_orig = points[n - 1 - i]
            if len(p_orig) == 3:
                prev_idx = (i - 1) % n
                result[prev_idx] = [result[prev_idx][0], result[prev_idx][1],
                                    {"mid_x": p_orig[2]["mid_x"], "mid_y": p_orig[2]["mid_y"]}]
        return result
    return points


def _ensure_ccw(points):
    """Reverse winding if CW (Revit expects CCW for floor/wall loops)."""
    if _signed_area(points) < 0:
        # Reverse list; arc mid-points belong to the start vertex — swap to new start
        rev = list(reversed(points))
        result = []
        n = len(rev)
        for i in range(n):
            p = rev[i]
            # The arc mid for this vertex originally pointed to the NEXT vertex.
            # After reversal the next vertex is now the previous one in the original list.
            # Move the mid from the current vertex to the previous vertex in the new list.
            result.append([p[0], p[1]])  # strip mids first
        # Reattach mids: original mid at index i goes to (n-1-i-1) in reversed = (n-i-2)
        for i in range(n):
            p_orig = points[n-1-i]  # original index before this one in reversed
            if len(p_orig) == 3:
                prev_idx = (i - 1) % n
                result[prev_idx] = [result[prev_idx][0], result[prev_idx][1],
                                    {"mid_x": p_orig[2]["mid_x"], "mid_y": p_orig[2]["mid_y"]}]
        return result
    return points
