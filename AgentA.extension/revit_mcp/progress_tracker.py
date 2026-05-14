import time
import json
import asyncio
import threading
from collections import deque

# Prefix used to distinguish transient status updates from permanent messages
_STATUS_PREFIX = "\x00STATUS\x00"

class BuildProgressTracker:
    def __init__(self, ctx=None, loop=None, callback=None):
        self.ctx = ctx
        self.loop = loop
        self._callback = callback
        self.start_time = None
        self.end_time = None
        self.elements_planned = {
            "floors": 0,
            "walls": 0,
            "columns": 0,
            "lifts": 0,
            "staircases": 0
        }
        self.elements_created = {
            "levels": 0,
            "walls": 0,
            "floors": 0,
            "columns": 0,
            "lifts": 0,
            "staircases": 0,
            "stair_runs": 0,
        }
        self.adjustments = []
        self.narrative_history = []
        self.goal = ""
        self.detail_level = "standard"
        self.tone = "conversational"
        # Thread-safe queue for messages
        self._msg_queue = deque()
        self._stop_event = threading.Event()
        self._poller_task = None

    def set_status(self, text):
        """Update the transient spinner status line (replaces in-place, disappears when done)."""
        self._deliver(_STATUS_PREFIX + text)

    def start(self):
        self.start_time = time.time()
        from revit_mcp.runner import log
        log("DEBUG: ProgressTracker.start() - Initializing poller.")
        # Start the background poller on the uvicorn loop
        if self.loop and self.ctx:
            self.loop.call_soon_threadsafe(self._start_poller)
        else:
            log(f"DEBUG: ProgressTracker.start() - FAILED: loop={bool(self.loop)}, ctx={bool(self.ctx)}. Streaming disabled.")

        self.set_status("Waiting for AI response...")
        log("ProgressTracker: started, waiting for manifest.")

    def _start_poller(self):
        """Starts the async polling task on the correct loop."""
        from revit_mcp.runner import log
        try:
            if self.loop:
                self._poller_task = self.loop.create_task(self._poll_messages())
                log("DEBUG: ProgressTracker Poller Task CREATED on loop.")
        except Exception as e:
            log(f"DEBUG: ProgressTracker Poller Task FAILED to create: {e}")

    async def _poll_messages(self):
        """Async task that drains the queue and sends updates to the client."""
        from revit_mcp.runner import log
        log("DEBUG: ProgressTracker Poller LOOP ENTERED.")
        try:
            # Continue polling as long as not stopped OR queue is not empty
            while not self._stop_event.is_set() or self._msg_queue:
                while self._msg_queue:
                    msg = self._msg_queue.popleft()
                    if self.ctx:
                        # Log sending for debug
                        log(f"DEBUG: ProgressTracker Sending -> {msg}")
                        try:
                            # Attempt 1: Standard FastMCP info
                            await self.ctx.info(msg)
                            
                            # Attempt 2: Redundant raw notification (for broader client support)
                            try:
                                if hasattr(self.ctx, "session") and self.ctx.session:
                                    await self.ctx.session.send_notification(
                                        "notifications/message",
                                        {"level": "info", "description": msg}
                                    )
                                    log("DEBUG: ProgressTracker Raw Notification SENT.")
                            except Exception as e2:
                                log(f"DEBUG: ProgressTracker Raw Notify FAILED: {e2}")
                        except Exception as e:
                            log(f"DEBUG: ProgressTracker ctx.info FAILED: {e}")
                    else:
                        log("DEBUG: ProgressTracker SKIPPED send - No Context.")
                
                # Check for stop signal after draining
                if self._stop_event.is_set() and not self._msg_queue:
                    break
                    
                await asyncio.sleep(0.05) # Faster polling (20Hz)
            log("DEBUG: ProgressTracker Poller LOOP EXITING (Drained).")
        except Exception as e:
            log(f"DEBUG: ProgressTracker Poller CRASHED: {e}")
                
    def stop(self):
        """Signal the poller to stop and clear the spinner bubble."""
        from revit_mcp.runner import log
        log("DEBUG: ProgressTracker.stop() signaled.")
        # Send a sentinel to tell the UI to remove the spinner bubble
        self._deliver(_STATUS_PREFIX)
        self._stop_event.set()
        self.end_time = time.time()
        
    def analyze_manifest(self, manifest_json):
        if not manifest_json:
            return

        try:
            manifest = json.loads(manifest_json) if isinstance(manifest_json, str) else manifest_json

            # unwrap
            for wrapper in ["orchestrate_build", "edit_entire_building_dimensions"]:
                if wrapper in manifest and len(manifest) == 1:
                    manifest = manifest[wrapper]
                    break

            setup = manifest.get("project_setup", {})
            shell = manifest.get("shell", {})
            levels = int(setup.get("levels", setup.get("storeys", 1)))
            level_h = setup.get("level_height", 4000)
            width = shell.get("width", 0)
            length = shell.get("length", 0)
            col_spacing = shell.get("column_spacing", 10000)
            stair_count = int(manifest.get("staircases", {}).get("count", 2))

            self.elements_planned["floors"] = levels
            if "staircases" in manifest:
                self.elements_planned["staircases"] = stair_count

            # Dynamic time estimate based on actual building complexity
            # Levels: ~0.3s each (create + constraints)
            t_levels = levels * 0.3
            # Shell walls: 4 per level × ~0.2s
            t_walls = levels * 4 * 0.2
            # Floors: 1 per level × ~0.3s (CurveLoop + voids)
            t_floors = levels * 0.3
            # Columns: estimate grid count from footprint and spacing
            if width > 0 and length > 0 and col_spacing > 0:
                nx = max(2, int(width / col_spacing) + 1)
                ny = max(2, int(length / col_spacing) + 1)
                total_cols = nx * ny * levels
                t_cols = total_cols * 0.02
            else:
                total_cols = 0
                t_cols = levels * 2.0
            # Lifts: ~1s per level for wall generation
            t_lifts = levels * 1.0
            # Staircases: ~1.5s per staircase per level (walls + floors + runs)
            t_stairs = stair_count * levels * 1.5
            # Overhead (nuclear lockdown, disjoint, cleanup)
            t_overhead = 3.0

            est_total = t_levels + t_walls + t_floors + t_cols + t_lifts + t_stairs + t_overhead
            est_min = max(8, int(est_total * 0.7))
            est_max = int(est_total * 1.4)

            # Build a descriptive summary
            parts = [f"{levels} levels"]
            if width > 0 and length > 0:
                parts.append(f"{width/1000:.0f}m x {length/1000:.0f}m footprint")
            if level_h:
                parts.append(f"{level_h}mm typical floor height")
            if col_spacing > 0:
                parts.append(f"{col_spacing/1000:.0f}m column grid")
            parts.append(f"{stair_count} staircases")

            from revit_mcp.runner import log as _log
            _log(
                f"Manifest received: {', '.join(parts)}. "
                f"Estimated build time: {est_min}-{est_max}s."
            )

        except Exception:
            pass

    def _deliver(self, msg):
        """Send a message via callback or queue."""
        from revit_mcp.runner import log
        if self._callback:
            try:
                self._callback(msg)
            except Exception as e:
                log(f"PROGRESS CALLBACK ERROR: {e}")
        else:
            self._msg_queue.append(msg)

    def report(self, msg, is_narrative=False):
        """Append a permanent message bubble."""
        from revit_mcp.runner import log
        t_now = time.strftime("%H:%M:%S")
        log(f"PROGRESS: [{t_now}] {msg}")

        if is_narrative:
            self.narrative_history.append(msg)

        self._deliver(msg)

    def record_created(self, category, count=1):
        """Track actual elements created during the build."""
        if category in self.elements_created:
            self.elements_created[category] += count

    def log_adjustment(self, adjustment_msg):
        """Track explicit design adjustments (e.g. floor height changed to fit stairs)."""
        if adjustment_msg not in self.adjustments:
            self.adjustments.append(adjustment_msg)
            self.report(f"Adjustment: {adjustment_msg}")

    def generate_final_report(self, base_summary="Build Completed successfully."):
        self.stop()
        duration = self.end_time - self.start_time if self.start_time else 0

        report = []
        _goal = getattr(self, "goal", "") or ""
        _tone = getattr(self, "tone", "conversational") or "conversational"
        if _goal and _tone == "conversational":
            _goal_clean = _goal.rstrip(".")
            report.append("**{}. Here's what was built:**".format(
                _goal_clean[0].upper() + _goal_clean[1:]
            ))
        else:
            report.append(f"**{base_summary}**")

        # ── Build stats ───────────────────────────────────────────────────────
        report.append("\n| | |")
        report.append("|---|---|")
        report.append(f"| **Status** | {base_summary} |")
        report.append(f"| **Build time** | {duration:.0f}s |")

        # ── High-level building summary from manifest ─────────────────────────
        m = getattr(self, "_last_manifest", None)
        if m:
            setup  = m.get("project_setup", {})
            shell  = m.get("shell", {})
            lifts  = m.get("lifts", {})
            stairs = m.get("staircases", {})

            levels      = int(setup.get("levels", 0))
            storeys     = max(0, levels - 1)   # levels includes roof level
            lh_raw      = setup.get("level_height", 0)
            # level_height may be a per-floor list or a scalar
            if isinstance(lh_raw, list):
                _lh_list_pt = [int(v) for v in lh_raw if v]
                lh_mm = _lh_list_pt[1] if len(_lh_list_pt) > 1 else (_lh_list_pt[0] if _lh_list_pt else 0)
            else:
                _lh_list_pt = None
                lh_mm = lh_raw
            # Height overrides: recalculate total height properly
            h_overrides = setup.get("height_overrides", {})
            total_h_mm  = 0
            for i in range(1, levels):
                if str(i) in h_overrides:
                    _h = h_overrides[str(i)]
                elif _lh_list_pt and i <= len(_lh_list_pt):
                    _h = _lh_list_pt[i - 1]
                else:
                    _h = lh_mm
                total_h_mm += int(_h or lh_mm)

            width_mm  = shell.get("width", 0)
            length_mm = shell.get("length", 0)
            floor_area_m2 = (width_mm / 1000.0) * (length_mm / 1000.0) if width_mm and length_mm else 0
            total_area_m2 = floor_area_m2 * storeys

            lift_count  = lifts.get("count", self.elements_created.get("lifts", 0))
            stair_count = stairs.get("count", self.elements_created.get("staircases", 0))

            brows = []
            if storeys:
                brows.append(("Storeys", str(storeys)))
            if total_h_mm:
                brows.append(("Total height", "{:.1f} m".format(total_h_mm / 1000.0)))
            if floor_area_m2:
                brows.append(("Floor plate", "{:.0f} m²  ({:.0f}m × {:.0f}m)".format(
                    floor_area_m2, width_mm / 1000.0, length_mm / 1000.0)))
            if total_area_m2:
                brows.append(("Total GFA", "{:.0f} m²".format(total_area_m2)))
            if lift_count:
                brows.append(("Lifts", str(lift_count)))
            if stair_count:
                brows.append(("Staircases", str(stair_count)))
            if lh_mm:
                brows.append(("Typical floor-to-floor", "{:.1f} m".format(lh_mm / 1000.0)))

            if brows:
                report.append("\n**Building Summary:**")
                report.append("| | |")
                report.append("|---|---|")
                for k, v in brows:
                    report.append(f"| {k} | {v} |")

        # ── Constructed elements ──────────────────────────────────────────────
        elem_rows = [(k.replace("_", " ").capitalize(), v)
                     for k, v in self.elements_created.items() if v > 0]
        if elem_rows:
            report.append("\n**Constructed Elements:**")
            report.append("| Element | Count |")
            report.append("|---------|-------|")
            for label, val in elem_rows:
                report.append(f"| {label} | {val} |")

        # ── Design adjustments (new info — not shown before) ──────────────────
        if self.adjustments:
            report.append("\n**Design Adjustments:**")
            report.append("| # | Adjustment |")
            report.append("|---|------------|")
            for i, adj in enumerate(self.adjustments, 1):
                report.append(f"| {i} | {adj} |")

        # narrative_history (Architectural Intent, authority codes) was already
        # shown as permanent bubbles during the build — do NOT repeat here.

        return "\n".join(report)

