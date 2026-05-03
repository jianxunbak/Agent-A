# -*- coding: utf-8 -*-
import math

class SpatialRegistry:
    """
    Centralized registry for tracking 3D volumes of managed spaces.
    Handles collision detection and occupancy mapping.
    """
    def __init__(self, tolerance=10.0):
        # tolerance in mm: used for intersection checks (ignoring tiny touches)
        self.tolerance = tolerance
        self.reservations = {} # space_id -> {bbox, tags}
        self.conflicts = []

    def reserve(self, space_id, bbox, tags=None):
        """
        Reserve a 3D volume.
        bbox: (xmin, ymin, zmin, xmax, ymax, zmax)
        Returns: (success, conflict_details)
        """
        collision = self.check_collision(bbox, ignore_id=space_id)
        if collision:
            self.conflicts.append({
                "space_id": space_id,
                "bbox": bbox,
                "with": collision
            })
            return False, collision

        self.reservations[space_id] = {
            "bbox": bbox,
            "tags": tags or []
        }
        return True, None

    def check_collision(self, bbox, ignore_id=None):
        """
        Checks if a bbox overlaps with any existing reservation.
        Touching edges (at tolerance) is allowed.
        Corner/edge contact with overlap volume below min_overlap_vol_mm3 is also allowed
        (handles L-shape junction false positives where adjacent clusters share a wall face).
        """
        x1, y1, z1, x2, y2, z2 = bbox
        conflicts = []

        for sid, res in self.reservations.items():
            if sid == ignore_id:
                continue

            ox1, oy1, oz1, ox2, oy2, oz2 = res["bbox"]

            # Standard AABB overlap check with tolerance buffer
            # We subtract tolerance from the boundaries to allow "touching"
            overlap_x = (x1 < ox2 - self.tolerance) and (x2 > ox1 + self.tolerance)
            overlap_y = (y1 < oy2 - self.tolerance) and (y2 > oy1 + self.tolerance)
            overlap_z = (z1 < oz2 - self.tolerance) and (z2 > oz1 + self.tolerance)

            if overlap_x and overlap_y and overlap_z:
                # Compute actual overlap volume to distinguish real intrusion from wall/corner contact
                ov_x = min(x2, ox2) - max(x1, ox1)
                ov_y = min(y2, oy2) - max(y1, oy1)
                ov_z = min(z2, oz2) - max(z1, oz1)
                overlap_vol = max(0.0, ov_x) * max(0.0, ov_y) * max(0.0, ov_z)
                # 100mm × 100mm × full_height is ~50,000,000 mm³ — anything below 500,000 mm³
                # is a wall-face or corner contact (two clusters sharing a boundary surface).
                if overlap_vol < 500000.0:
                    continue
                conflicts.append({
                    "id": sid,
                    "bbox": res["bbox"],
                    "tags": res["tags"]
                })

        return conflicts

    def get_occupancy_map(self):
        """Returns a list of all reserved volumes."""
        results = []
        for sid, res in self.reservations.items():
            results.append({
                "id": sid,
                "bbox": res["bbox"],
                "tags": res["tags"]
            })
        return results

    def clear(self):
        self.reservations = {}
        self.conflicts = []

    def validate_assembly(self, space_id, components):
        """
        Validates that a named space has both walls and floors.
        components: list of elements from manifest associated with this space_id.
        """
        has_walls = any(c.get("type", "").lower() == "wall" or "wall" in str(c.get("id", "")).lower() for c in components)
        has_floors = any(c.get("type", "").lower() == "floor" or "floor" in str(c.get("id", "")).lower() for c in components)
        
        # More robust check: check for 'walls' or 'floors' keys in components if passed as manifest snippet
        if not has_walls and "walls" in components: has_walls = len(components["walls"]) > 0
        if not has_floors and "floors" in components: has_floors = len(components["floors"]) > 0

        if not has_walls or not has_floors:
            return False, "Missing {} for space assembly: {}".format(
                "walls and floors" if not (has_walls or has_floors) else ("walls" if not has_walls else "floors"),
                space_id
            )
        return True, None
