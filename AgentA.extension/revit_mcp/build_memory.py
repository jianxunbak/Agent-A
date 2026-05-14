# -*- coding: utf-8 -*-
"""
build_memory.py — Building Design Memory / Options System

Persists every successful build as a named Option (and every subsequent edit
as a Revision of that option) in build_options.json. Pure Python — no Revit API.
"""
import json
import os
import re
from datetime import datetime


# ── Module-level logger (writes to fastmcp_server.log via utils.get_log_path) ──

def _log(msg):
    """Write a timestamped log line to fastmcp_server.log. Fails silently."""
    try:
        from revit_mcp.utils import get_log_path
        import time as _t
        ts = _t.strftime("%Y-%m-%d %H:%M:%S")
        line = "[{}] [BuildMemory] {}\n".format(ts, msg)
        with open(get_log_path(), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


class OptionsManager:
    SCHEMA_VERSION = 1

    def __init__(self, store_path):
        """
        store_path: full path to build_options.json
        (resolved from utils.get_log_path() dirname by the module-level singleton).
        """
        self._path = store_path
        self._data = None  # lazy-loaded
        _log("OptionsManager initialised. Store path: {}".format(store_path))

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _load(self):
        """Load from disk. Resets to empty schema on missing file or corrupt JSON."""
        if os.path.exists(self._path):
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                loaded_version = data.get("schema_version", 0)
                option_count = len(data.get("options", []))
                _log("Loaded build_options.json: schema_v={}, {} options, current={}".format(
                    loaded_version, option_count, data.get("current_option_id")))
                if loaded_version < self.SCHEMA_VERSION:
                    _log("Migrating schema from v{} to v{}".format(loaded_version, self.SCHEMA_VERSION))
                    data = self._migrate(data)
                self._data = data
                return
            except Exception as e:
                _log("ERROR loading build_options.json: {} — resetting to empty schema".format(e))
        else:
            _log("build_options.json not found at {} — starting fresh".format(self._path))
        self._data = {
            "schema_version": self.SCHEMA_VERSION,
            "current_option_id": None,
            "current_revision_id": None,
            "options": [],
        }

    def _save(self):
        """Atomic write: write to .tmp then os.replace (MoveFileEx on Windows)."""
        tmp = self._path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self._path)
            option_count = len(self._data.get("options", []))
            _log("Saved build_options.json: {} options, current_option={}, current_revision={}".format(
                option_count,
                self._data.get("current_option_id"),
                self._data.get("current_revision_id")))
        except Exception as e:
            _log("ERROR saving build_options.json: {}".format(e))
            raise

    def _ensure_loaded(self):
        if self._data is None:
            _log("Lazy-loading build_options.json...")
            self._load()

    def _migrate(self, old_data):
        """Forward-migrate old schema versions. Currently no-op at v1."""
        old_data["schema_version"] = self.SCHEMA_VERSION
        if "options" not in old_data:
            old_data["options"] = []
        if "current_option_id" not in old_data:
            old_data["current_option_id"] = None
        if "current_revision_id" not in old_data:
            old_data["current_revision_id"] = None
        _log("Migration complete: schema_v={}".format(self.SCHEMA_VERSION))
        return old_data

    def _next_option_number(self):
        """Return next integer for Opt-NNN sequencing."""
        if not self._data["options"]:
            return 1
        nums = []
        for opt in self._data["options"]:
            m = re.match(r"Opt-(\d+)", opt["id"])
            if m:
                nums.append(int(m.group(1)))
        return max(nums) + 1 if nums else 1

    def _next_revision_number(self, option):
        """Return next integer for Rev0N sequencing within an option."""
        if not option["revisions"]:
            return 1
        nums = []
        for rev in option["revisions"]:
            m = re.search(r"Rev(\d+)", rev["id"])
            if m:
                nums.append(int(m.group(1)))
        return max(nums) + 1 if nums else 1

    def _find_option(self, option_id):
        """Return option dict or None. Accepts 'Opt-001' or '1'."""
        self._ensure_loaded()
        for opt in self._data["options"]:
            if opt["id"] == option_id:
                return opt
        # Numeric shorthand: '1' → 'Opt-001'
        try:
            n = int(option_id)
            padded = "Opt-{:03d}".format(n)
            for opt in self._data["options"]:
                if opt["id"] == padded:
                    return opt
        except (ValueError, TypeError):
            pass
        _log("_find_option: '{}' not found in {} options".format(
            option_id, len(self._data.get("options", []))))
        return None

    def _find_revision(self, option, rev_id):
        """Return revision dict or None. Accepts 'Opt-001-Rev01' or '1'."""
        for rev in option["revisions"]:
            if rev["id"] == rev_id:
                return rev
        try:
            n = int(rev_id)
            padded = "{}-Rev{:02d}".format(option["id"], n)
            for rev in option["revisions"]:
                if rev["id"] == padded:
                    return rev
        except (ValueError, TypeError):
            pass
        _log("_find_revision: '{}' not found in option '{}' ({} revisions)".format(
            rev_id, option["id"], len(option.get("revisions", []))))
        return None

    # ── Auto-naming ───────────────────────────────────────────────────────────

    @staticmethod
    def generate_option_name(option_id, manifest, intent_text=None):
        """
        Build a human-readable name from manifest data + optional Gemini intent.

        Priority 1: First sentence of intent_text (≤60 chars, markdown stripped).
        Priority 2: Derived from manifest fields: "NF Shape Typology WxLm".

        Returns e.g. "Opt-001: 40F Circle Commercial Office 68x68m"
        """
        if intent_text:
            first = re.split(r"[.!?\n]", intent_text.strip())[0].strip()
            first = re.sub(r"[*_`#]", "", first)
            if len(first) > 60:
                first = first[:57] + "..."
            if first:
                name = "{}: {}".format(option_id, first)
                _log("generate_option_name: using intent text → '{}'".format(name))
                return name

        setup = manifest.get("project_setup", {})
        shell = manifest.get("shell", {})
        typology = manifest.get("typology", "building")

        levels = setup.get("levels", 0)
        shape = (shell.get("shape") or "rectangular").capitalize()
        width_mm = shell.get("width") or 0
        length_mm = shell.get("length") or 0
        typology_label = typology.replace("_", " ").title()
        w_m = int(round(width_mm / 1000)) if width_mm else 0
        l_m = int(round(length_mm / 1000)) if length_mm else 0

        parts = []
        if levels:
            parts.append("{}F".format(levels))
        parts.append(shape)
        parts.append(typology_label)
        if w_m and l_m:
            parts.append("{}x{}m".format(w_m, l_m))

        label = " ".join(parts) if parts else "Building"
        name = "{}: {}".format(option_id, label)
        _log("generate_option_name: using manifest fields → '{}'".format(name))
        return name

    @staticmethod
    def generate_description(manifest, intent_text=None):
        """
        One-to-two sentence description for an option.
        Uses intent_text if available, otherwise builds from manifest fields.
        """
        if intent_text:
            # Keep the first two sentences for a complete but concise description
            sentences = re.split(r"(?<=[.!?])\s+", intent_text.strip())
            desc = " ".join(sentences[:2]).strip()
            return desc if desc else intent_text[:400].strip()

        setup = manifest.get("project_setup", {})
        shell = manifest.get("shell", {})
        typology = manifest.get("typology", "building")

        levels = setup.get("levels", "?")
        height = setup.get("level_height", "?")
        shape = (shell.get("shape") or "rectangular")
        width_mm = shell.get("width") or 0
        length_mm = shell.get("length") or 0
        w_m = int(round(width_mm / 1000)) if width_mm else 0
        l_m = int(round(length_mm / 1000)) if length_mm else 0

        parts = ["{}-storey {} {} tower".format(levels, shape, typology.replace("_", " "))]
        if w_m and l_m:
            parts.append("{}m x {}m footprint".format(w_m, l_m))
        if height and height != "?":
            parts.append("{}mm typical floor height".format(height))

        return ", ".join(parts) + "."

    # ── Diff detection ────────────────────────────────────────────────────────

    @staticmethod
    def compute_diff_summary(old_manifest, new_manifest):
        """
        Compute a structured diff between two manifests.
        Returns dict with changed_keys list and numeric deltas.
        """
        changed_keys = []

        def _val(m, *keys):
            d = m
            for k in keys:
                if not isinstance(d, dict):
                    return None
                d = d.get(k)
            return d

        old_levels = _val(old_manifest, "project_setup", "levels") or 0
        new_levels = _val(new_manifest, "project_setup", "levels") or 0
        levels_delta = new_levels - old_levels
        if levels_delta != 0:
            changed_keys.append("project_setup.levels ({:+d})".format(levels_delta))

        old_lh = _val(old_manifest, "project_setup", "level_height") or 0
        new_lh = _val(new_manifest, "project_setup", "level_height") or 0
        if old_lh != new_lh:
            changed_keys.append("project_setup.level_height ({} → {})".format(old_lh, new_lh))

        old_shape = _val(old_manifest, "shell", "shape")
        new_shape = _val(new_manifest, "shell", "shape")
        shape_changed = old_shape != new_shape
        if shape_changed:
            changed_keys.append("shell.shape ({} → {})".format(old_shape, new_shape))

        old_w = _val(old_manifest, "shell", "width") or 0
        new_w = _val(new_manifest, "shell", "width") or 0
        width_delta = new_w - old_w
        if abs(width_delta) > 50:
            changed_keys.append("shell.width ({:+.0f}mm)".format(width_delta))

        old_l = _val(old_manifest, "shell", "length") or 0
        new_l = _val(new_manifest, "shell", "length") or 0
        length_delta = new_l - old_l
        if abs(length_delta) > 50:
            changed_keys.append("shell.length ({:+.0f}mm)".format(length_delta))

        old_fso = _val(old_manifest, "shell", "footprint_scale_overrides")
        new_fso = _val(new_manifest, "shell", "footprint_scale_overrides")
        if old_fso != new_fso:
            changed_keys.append("shell.footprint_scale_overrides")

        old_typ = old_manifest.get("typology")
        new_typ = new_manifest.get("typology")
        typology_changed = old_typ != new_typ
        if typology_changed:
            changed_keys.append("typology ({} → {})".format(old_typ, new_typ))

        old_lifts = _val(old_manifest, "lifts", "count") or 0
        new_lifts = _val(new_manifest, "lifts", "count") or 0
        if old_lifts != new_lifts:
            changed_keys.append("lifts.count ({} → {})".format(old_lifts, new_lifts))

        old_stairs = _val(old_manifest, "staircases", "count") or 0
        new_stairs = _val(new_manifest, "staircases", "count") or 0
        if old_stairs != new_stairs:
            changed_keys.append("staircases.count ({} → {})".format(old_stairs, new_stairs))

        result = {
            "changed_keys": changed_keys,
            "levels_delta": levels_delta,
            "width_delta_mm": width_delta,
            "length_delta_mm": length_delta,
            "shape_changed": shape_changed,
            "typology_changed": typology_changed,
        }
        _log("compute_diff_summary: {} changes detected: {}".format(
            len(changed_keys), changed_keys if changed_keys else "none"))
        return result

    @staticmethod
    def _diff_to_description(diff_summary):
        """Convert diff_summary dict to a human-readable string."""
        keys = diff_summary.get("changed_keys", [])
        if not keys:
            return "Minor adjustments."
        return "Changed: " + "; ".join(keys) + "."

    @staticmethod
    def is_major_change(diff_summary):
        """
        Return True if the diff warrants a new option rather than a revision.
        Major = shape change, typology change, or storey count shift ≥ 5.
        """
        if diff_summary.get("shape_changed"):
            return True
        if diff_summary.get("typology_changed"):
            return True
        if abs(diff_summary.get("levels_delta", 0)) >= 5:
            return True
        return False

    # ── Public write API ──────────────────────────────────────────────────────

    def save_new_option(self, manifest, intent_text=None, duration_s=None,
                        rag_rules=None, compliance_snapshot=None):
        """
        Called after every full new build.
        Creates Opt-NNN, sets it as current, saves file.
        rag_rules: the raw RAG result dict (authority + rules) — cached so recreate skips RAG.
        compliance_snapshot: the full compliance_text string injected into Gemini — for exact replay.
        Returns the new option dict.
        """
        self._ensure_loaded()
        n = self._next_option_number()
        option_id = "Opt-{:03d}".format(n)
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

        _log("save_new_option: creating {} (intent_text={}, duration={}s, rag={})".format(
            option_id, "yes" if intent_text else "no", duration_s, "yes" if rag_rules else "no"))

        option = {
            "id": option_id,
            "name": self.generate_option_name(option_id, manifest, intent_text),
            "description": self.generate_description(manifest, intent_text),
            "typology": manifest.get("typology", ""),
            "created_at": now,
            "duration_s": duration_s,
            "manifest": manifest,
            "rag_rules": rag_rules or {},
            "compliance_snapshot": compliance_snapshot or "",
            "revisions": [],
        }

        self._data["options"].append(option)
        self._data["current_option_id"] = option_id
        self._data["current_revision_id"] = None
        self._save()

        _log("save_new_option: SUCCESS — {} '{}' saved to build_options.json".format(
            option_id, option["name"]))
        return option

    def save_revision(self, manifest, intent_text=None, duration_s=None,
                      rag_rules=None, compliance_snapshot=None):
        """
        Called after every edit that modifies an existing option.
        If no current_option_id exists, falls back to save_new_option().
        Returns the new revision dict.
        """
        self._ensure_loaded()
        current_id = self._data.get("current_option_id")
        _log("save_revision: current_option_id={}".format(current_id))

        option = self._find_option(current_id) if current_id else None

        if option is None:
            _log("save_revision: no current option found — falling back to save_new_option")
            return self.save_new_option(manifest, intent_text, duration_s,
                                        rag_rules=rag_rules, compliance_snapshot=compliance_snapshot)

        rev_n = self._next_revision_number(option)
        rev_id = "{}-Rev{:02d}".format(option["id"], rev_n)
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

        _log("save_revision: creating {} under {} (intent={}, duration={}s, rag={})".format(
            rev_id, option["id"], "yes" if intent_text else "no", duration_s, "yes" if rag_rules else "no"))

        # Diff against parent (base or last revision)
        parent_manifest = option["revisions"][-1]["manifest"] if option["revisions"] else option["manifest"]
        diff = self.compute_diff_summary(parent_manifest, manifest)
        diff_desc = self._diff_to_description(diff)

        # Build revision name from parent option name body
        base_body = option["name"].split(": ", 1)[-1] if ": " in option["name"] else option["name"]
        rev_name = "{}: {} (Rev {})".format(rev_id, base_body, rev_n)

        revision = {
            "id": rev_id,
            "name": rev_name,
            "description": intent_text[:200].strip() if intent_text else diff_desc,
            "created_at": now,
            "duration_s": duration_s,
            "diff_summary": diff,
            "manifest": manifest,
            "rag_rules": rag_rules or {},
            "compliance_snapshot": compliance_snapshot or "",
        }

        option["revisions"].append(revision)
        self._data["current_option_id"] = option["id"]
        self._data["current_revision_id"] = rev_id
        self._save()

        _log("save_revision: SUCCESS — {} saved. Diff: {}".format(rev_id, diff_desc))
        return revision

    def apply_rollback_state(self, option_id, revision_id=None):
        """Update current pointers after a successful rollback."""
        self._ensure_loaded()
        _log("apply_rollback_state: setting current → option={}, revision={}".format(
            option_id, revision_id))
        self._data["current_option_id"] = option_id
        self._data["current_revision_id"] = revision_id
        self._save()
        _log("apply_rollback_state: done")

    def delete_option(self, option_id):
        """
        Delete an entire option and all its revisions.
        Returns (True, message) or (False, error_message).
        """
        self._ensure_loaded()
        _log("delete_option: requested for '{}'".format(option_id))
        option = self._find_option(option_id)
        if option is None:
            msg = "Option '{}' not found.".format(option_id)
            _log("delete_option: FAILED — {}".format(msg))
            return False, msg

        rev_count = len(option.get("revisions", []))
        was_current = (self._data.get("current_option_id") == option["id"])
        _log("delete_option: removing {} (revisions={}, was_current={})".format(
            option["id"], rev_count, was_current))

        self._data["options"] = [o for o in self._data["options"] if o["id"] != option["id"]]

        if was_current:
            if self._data["options"]:
                self._data["current_option_id"] = self._data["options"][-1]["id"]
                self._data["current_revision_id"] = None
                _log("delete_option: current pointer moved to {}".format(self._data["current_option_id"]))
            else:
                self._data["current_option_id"] = None
                self._data["current_revision_id"] = None
                _log("delete_option: no options remain — current pointers cleared")

        self._save()
        msg = "Deleted option '{}'.".format(option["id"])
        _log("delete_option: SUCCESS — {}".format(msg))
        return True, msg

    def delete_all_options(self):
        """Delete every option and reset state. Returns (count_deleted, message)."""
        self._ensure_loaded()
        count = len(self._data["options"])
        _log("delete_all_options: removing {} options".format(count))
        if count == 0:
            return 0, "No options to delete."
        self._data["options"] = []
        self._data["current_option_id"] = None
        self._data["current_revision_id"] = None
        self._save()
        _log("delete_all_options: SUCCESS — cleared all options")
        return count, "Deleted all {} option{}.".format(count, "s" if count != 1 else "")

    def delete_revision(self, option_id, revision_id):
        """
        Delete a specific revision from an option.
        Returns (True, message) or (False, error_message).
        """
        self._ensure_loaded()
        _log("delete_revision: requested option='{}' revision='{}'".format(option_id, revision_id))
        option = self._find_option(option_id)
        if option is None:
            msg = "Option '{}' not found.".format(option_id)
            _log("delete_revision: FAILED — {}".format(msg))
            return False, msg

        rev = self._find_revision(option, revision_id)
        if rev is None:
            msg = "Revision '{}' not found in option '{}'.".format(revision_id, option["id"])
            _log("delete_revision: FAILED — {}".format(msg))
            return False, msg

        rev_id_full = rev["id"]
        was_current_rev = (self._data.get("current_revision_id") == rev_id_full)
        _log("delete_revision: removing {} from {} (was_current_rev={})".format(
            rev_id_full, option["id"], was_current_rev))

        option["revisions"] = [r for r in option["revisions"] if r["id"] != rev_id_full]

        if was_current_rev:
            self._data["current_revision_id"] = None
            _log("delete_revision: current_revision_id cleared (was pointing at deleted rev)")

        self._save()
        msg = "Deleted revision '{}'.".format(rev_id_full)
        _log("delete_revision: SUCCESS — {}".format(msg))
        return True, msg

    def reorder_option(self, option_id, new_position):
        """
        Move an option to a new 1-based position in the list, then renumber all
        option IDs (Opt-001, Opt-002, …) and their revision IDs sequentially.

        option_id:    '3' or 'Opt-003' — the option to move
        new_position: 1-based target position (1 = first)

        Returns (True, message) or (False, error_message).
        """
        self._ensure_loaded()
        _log("reorder_option: moving '{}' to position {}".format(option_id, new_position))

        opt = self._find_option(option_id)
        if opt is None:
            return False, "Option '{}' not found.".format(option_id)

        opts = self._data["options"]
        total = len(opts)
        try:
            pos = int(new_position)
        except (ValueError, TypeError):
            return False, "Invalid position '{}'.".format(new_position)

        if pos < 1 or pos > total:
            return False, "Position {} is out of range (1–{}).".format(pos, total)

        # Remember which logical items are current so we can remap the pointers
        old_current_opt = self._data.get("current_option_id")
        old_current_rev = self._data.get("current_revision_id")

        # ── Reorder the list ──────────────────────────────────────────────────
        opts.remove(opt)
        opts.insert(pos - 1, opt)

        # ── Renumber every option and its revisions in place ──────────────────
        # We need to track what the old IDs were so we can remap current pointers.
        # Build a mapping: old_opt_id → new_opt_id as we go.
        opt_id_map = {}   # old → new option id
        rev_id_map = {}   # old → new revision id (flat, across all options)

        for idx, o in enumerate(opts):
            new_opt_id = "Opt-{:03d}".format(idx + 1)
            opt_id_map[o["id"]] = new_opt_id

            # Rebuild the option name: swap the ID prefix, keep the description body
            if ": " in o["name"]:
                body = o["name"].split(": ", 1)[1]
            else:
                body = o["name"]
            o["id"] = new_opt_id
            o["name"] = "{}: {}".format(new_opt_id, body)

            # Renumber revisions under this option
            for rev_idx, rev in enumerate(o.get("revisions", [])):
                new_rev_id = "{}-Rev{:02d}".format(new_opt_id, rev_idx + 1)
                rev_id_map[rev["id"]] = new_rev_id

                # Rebuild revision name: swap ID prefix, keep body, keep (Rev N) suffix
                if ": " in rev["name"]:
                    rev_body = rev["name"].split(": ", 1)[1]
                    # strip old (Rev N) suffix so we can rewrite it cleanly
                    rev_body = re.sub(r"\s*\(Rev \d+\)\s*$", "", rev_body).strip()
                else:
                    rev_body = body  # fall back to option body
                rev["id"] = new_rev_id
                rev["name"] = "{}: {} (Rev {})".format(new_rev_id, rev_body, rev_idx + 1)

        # ── Remap current pointers ────────────────────────────────────────────
        if old_current_opt and old_current_opt in opt_id_map:
            self._data["current_option_id"] = opt_id_map[old_current_opt]
        if old_current_rev and old_current_rev in rev_id_map:
            self._data["current_revision_id"] = rev_id_map[old_current_rev]

        self._save()

        # Build a short summary of the new order
        order_summary = ", ".join(
            "{} (was {})".format(o["id"], old_id)
            for old_id, new_id in opt_id_map.items()
            for o in [next(x for x in opts if x["id"] == new_id)]
            if old_id != new_id
        )
        msg = "Options reordered. New order: {}.".format(
            " → ".join(o["id"] for o in opts)
        )
        if order_summary:
            msg += " Changed: {}.".format(order_summary)
        _log("reorder_option: SUCCESS — {}".format(msg))
        return True, msg

    def move_to_revision(self, source_option_id, target_option_id, source_revision_id=None):
        """
        Move a top-level option (or one of its revisions) to become a new revision
        under a different option.

        source_option_id:   the option that holds the item to move ('1' or 'Opt-001')
        target_option_id:   the option that will receive the new revision
        source_revision_id: if given, move only that revision; if None, move the base
                            option manifest (the option itself, not one of its revisions)

        After the move:
        - The item is appended as the next revision of the target option.
        - If the source was a base option (no revision specified) AND it has no
          remaining revisions, the source option is deleted entirely.
        - Current pointers are updated to the newly created revision.

        Returns (True, message) or (False, error_message).
        """
        self._ensure_loaded()
        _log("move_to_revision: source_opt='{}' rev='{}' → target_opt='{}'".format(
            source_option_id, source_revision_id, target_option_id))

        src_opt = self._find_option(source_option_id)
        if src_opt is None:
            return False, "Source option '{}' not found.".format(source_option_id)

        tgt_opt = self._find_option(target_option_id)
        if tgt_opt is None:
            return False, "Target option '{}' not found.".format(target_option_id)

        if src_opt["id"] == tgt_opt["id"]:
            return False, "Source and target are the same option."

        # ── Determine what we are moving ──────────────────────────────────────
        if source_revision_id:
            src_rev = self._find_revision(src_opt, source_revision_id)
            if src_rev is None:
                return False, "Revision '{}' not found in option '{}'.".format(
                    source_revision_id, src_opt["id"])
            moving_manifest = src_rev["manifest"]
            moving_description = src_rev.get("description", "")
            moving_created_at = src_rev.get("created_at")
            moving_duration = src_rev.get("duration_s")
            moving_rag = src_rev.get("rag_rules", {})
            moving_compliance = src_rev.get("compliance_snapshot", "")
            moving_diff = src_rev.get("diff_summary", {})
            source_label = src_rev["id"]
        else:
            moving_manifest = src_opt["manifest"]
            moving_description = src_opt.get("description", "")
            moving_created_at = src_opt.get("created_at")
            moving_duration = src_opt.get("duration_s")
            moving_rag = src_opt.get("rag_rules", {})
            moving_compliance = src_opt.get("compliance_snapshot", "")
            moving_diff = {}
            source_label = src_opt["id"]

        # ── Create the new revision under target ──────────────────────────────
        rev_n = self._next_revision_number(tgt_opt)
        new_rev_id = "{}-Rev{:02d}".format(tgt_opt["id"], rev_n)
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

        # Build a name: inherit the target option's body, tag with rev number
        tgt_body = tgt_opt["name"].split(": ", 1)[-1] if ": " in tgt_opt["name"] else tgt_opt["name"]
        new_rev_name = "{}: {} (Rev {})".format(new_rev_id, tgt_body, rev_n)

        new_rev = {
            "id": new_rev_id,
            "name": new_rev_name,
            "description": moving_description,
            "created_at": moving_created_at or now,
            "duration_s": moving_duration,
            "diff_summary": moving_diff,
            "manifest": moving_manifest,
            "rag_rules": moving_rag,
            "compliance_snapshot": moving_compliance,
        }

        tgt_opt["revisions"].append(new_rev)
        _log("move_to_revision: created {} under {}".format(new_rev_id, tgt_opt["id"]))

        # ── Remove from source ────────────────────────────────────────────────
        if source_revision_id:
            old_rev_id = src_rev["id"]
            src_opt["revisions"] = [r for r in src_opt["revisions"] if r["id"] != old_rev_id]
            _log("move_to_revision: removed revision {} from {}".format(old_rev_id, src_opt["id"]))
        else:
            # Moving the base option — delete source option only if no revisions remain
            if not src_opt["revisions"]:
                self._data["options"] = [o for o in self._data["options"] if o["id"] != src_opt["id"]]
                _log("move_to_revision: source option {} deleted (no remaining revisions)".format(src_opt["id"]))
            else:
                # Promote first remaining revision to become the new base of the source option
                promoted = src_opt["revisions"].pop(0)
                src_opt["manifest"] = promoted["manifest"]
                src_opt["description"] = promoted.get("description", src_opt.get("description", ""))
                src_opt["duration_s"] = promoted.get("duration_s", src_opt.get("duration_s"))
                _log("move_to_revision: promoted {} to base of {}".format(promoted["id"], src_opt["id"]))

        # ── Renumber options if source was deleted (keeps IDs sequential) ───────
        # Build a remapping of old → new IDs before saving, so current pointers
        # can be preserved correctly even if the source option was removed.
        old_current_opt = self._data.get("current_option_id")
        old_current_rev = self._data.get("current_revision_id")

        opt_id_map = {}
        rev_id_map = {}
        for idx, o in enumerate(self._data["options"]):
            new_opt_id = "Opt-{:03d}".format(idx + 1)
            opt_id_map[o["id"]] = new_opt_id
            for rev_idx, rev in enumerate(o.get("revisions", [])):
                new_rev_id_r = "{}-Rev{:02d}".format(new_opt_id, rev_idx + 1)
                rev_id_map[rev["id"]] = new_rev_id_r
                rev["id"] = new_rev_id_r
                if ": " in rev.get("name", ""):
                    rev_body = rev["name"].split(": ", 1)[1]
                    rev_body = re.sub(r"\s*\(Rev \d+\)\s*$", "", rev_body).strip()
                    rev["name"] = "{}: {} (Rev {})".format(new_rev_id_r, rev_body, rev_idx + 1)
            o["id"] = new_opt_id
            if ": " in o.get("name", ""):
                body = o["name"].split(": ", 1)[1]
                o["name"] = "{}: {}".format(new_opt_id, body)

        # Remap new_rev_id (created above before renumber) to its final ID
        new_rev_id_final = rev_id_map.get(new_rev_id, new_rev_id)

        # ── Preserve current pointers — user's active option hasn't changed ────
        if old_current_opt and old_current_opt in opt_id_map:
            self._data["current_option_id"] = opt_id_map[old_current_opt]
        if old_current_rev and old_current_rev in rev_id_map:
            self._data["current_revision_id"] = rev_id_map[old_current_rev]

        self._save()

        msg = "Moved {} → {} (now {}).".format(source_label, tgt_opt["id"], new_rev_id_final)
        _log("move_to_revision: SUCCESS — {}".format(msg))
        return True, msg

    # ── Public read API ───────────────────────────────────────────────────────

    def has_options(self):
        self._ensure_loaded()
        count = len(self._data["options"])
        _log("has_options: {} options found".format(count))
        return count > 0

    def list_options(self):
        """
        Returns a markdown-table string listing all options and their revisions.
        Active rows carry / markers so _build_table_grid in script.py
        can highlight them in green.
        """
        self._ensure_loaded()
        opts = self._data["options"]
        count = len(opts)
        _log("list_options: formatting {} options for display".format(count))

        if not count:
            return "No saved options yet. Build a design to create your first option."

        total_revs = sum(len(o.get("revisions", [])) for o in opts)

        current_opt = self._data.get("current_option_id") or "none"
        current_rev = self._data.get("current_revision_id")

        # Validate that current pointers actually refer to existing entries.
        opt_ids = {o["id"] for o in opts}
        if current_opt not in opt_ids:
            current_opt = "none"
            current_rev = None

        #  /  are private-use markers; _build_table_grid in script.py
        # detects them to render the row with a green background and text.
        G = ""
        R = ""

        lines = []
        lines.append("## Saved Options")
        lines.append("**{}** option{}  ·  **{}** revision{}".format(
            count, "s" if count != 1 else "",
            total_revs, "s" if total_revs != 1 else ""))
        lines.append("")
        lines.append("| # | Option | Description | Date & Time | Status |")
        lines.append("|---|--------|-------------|-------------|--------|")

        for i, opt in enumerate(opts):
            opt_id_display = opt["id"].replace("Opt-", "Option-", 1)
            desc_part = (opt.get("description") or "").strip()
            if not desc_part:
                name = opt.get("name", "")
                desc_part = name.split(": ", 1)[1].strip() if ": " in name else "—"

            dt = opt["created_at"][:16]
            datetime_str = "{} {}".format(dt[:10], dt[11:16] if len(dt) >= 16 else "")

            is_active = opt["id"] == current_opt and not current_rev
            status = "{}◄ **active**{}".format(G, R) if is_active else ""
            opt_cell = "{}**{}**{}".format(G, opt_id_display, R) if is_active else "**{}**".format(opt_id_display)

            lines.append("| {} | {} | {} | {} | {} |".format(
                i + 1, opt_cell, desc_part, datetime_str, status))

            for rev in opt.get("revisions", []):
                rev_id_display = rev["id"].replace("Opt-", "Option-", 1)
                rev_desc_part = (rev.get("description") or "").strip()
                if not rev_desc_part:
                    rev_name = rev.get("name", rev["id"])
                    rev_desc_part = rev_name.split(": ", 1)[1].strip() if ": " in rev_name else ""
                    rev_desc_part = re.sub(r"\s*\(Rev \d+\)\s*$", "", rev_desc_part).strip() or "—"

                rev_dt = rev["created_at"][:16]
                rev_datetime_str = "{} {}".format(rev_dt[:10], rev_dt[11:16] if len(rev_dt) >= 16 else "")

                rev_is_active = rev["id"] == current_rev
                rev_status = "{}◄ **active**{}".format(G, R) if rev_is_active else ""
                rev_cell = "{}↳ *{}*{}".format(G, rev_id_display, R) if rev_is_active else "↳ *{}*".format(rev_id_display)

                lines.append("| | {} | {} | {} | {} |".format(
                    rev_cell, rev_desc_part, rev_datetime_str, rev_status))

        return chr(10).join(lines)


    def get_manifest_for_rollback(self, option_id, revision_id=None):
        """
        Retrieve the full manifest dict for a rollback operation.
        Returns (manifest_dict, resolved_opt_id, resolved_rev_id)
        or (None, None, None) if not found.
        """
        self._ensure_loaded()
        _log("get_manifest_for_rollback: option='{}', revision='{}'".format(option_id, revision_id))

        option = self._find_option(option_id)
        if option is None:
            _log("get_manifest_for_rollback: option '{}' not found".format(option_id))
            return None, None, None

        if revision_id is not None:
            rev = self._find_revision(option, revision_id)
            if rev is None:
                _log("get_manifest_for_rollback: revision '{}' not found in {}".format(
                    revision_id, option["id"]))
                return None, None, None
            _log("get_manifest_for_rollback: found {} / {} — manifest keys: {}".format(
                option["id"], rev["id"], list(rev["manifest"].keys())))
            return rev["manifest"], option["id"], rev["id"]

        _log("get_manifest_for_rollback: found {} (base) — manifest keys: {}".format(
            option["id"], list(option["manifest"].keys())))
        return option["manifest"], option["id"], None

    def export_option_json(self, option_id, revision_id=None):
        """
        Return the manifest JSON string for a specific option or revision.
        Returns None if not found.
        """
        _log("export_option_json: option='{}', revision='{}'".format(option_id, revision_id))
        manifest, resolved_opt, resolved_rev = self.get_manifest_for_rollback(option_id, revision_id)
        if manifest is None:
            _log("export_option_json: FAILED — not found")
            return None
        json_str = json.dumps(manifest, indent=2)
        _log("export_option_json: SUCCESS — {} chars for {}/{}".format(
            len(json_str), resolved_opt, resolved_rev))
        return json_str

    def get_new_build_prompt(self):
        """
        Returns a formatted string to show when the user tries to start a new build
        and saved options already exist. Returns None if no options exist.
        """
        self._ensure_loaded()
        count = len(self._data["options"])
        _log("get_new_build_prompt: {} existing options — showing selection menu".format(count))
        if count == 0:
            return None

        lines = [
            "You have {} saved design option{}. Would you like to continue from one, or start fresh?".format(
                count, "s" if count != 1 else ""
            ),
            "",
            self.list_options(),
            "",
            "Reply with:",
            '  \u2022 "use option 1" / "rollback to option 2" \u2014 restore a saved option',
            '  \u2022 "use option 1 revision 2" \u2014 restore a specific revision',
            '  \u2022 "create from scratch" / "start over" \u2014 start a completely new design',
        ]
        return "\n".join(lines)

    def get_cached_compliance(self, option_id, revision_id=None):
        """
        Return (rag_rules, compliance_snapshot) stored for a given option/revision.
        Falls back to the parent option's values if the revision has none.
        Returns (None, None) if not found or nothing was stored.
        """
        self._ensure_loaded()
        option = self._find_option(option_id)
        if option is None:
            return None, None

        target = option
        if revision_id is not None:
            rev = self._find_revision(option, revision_id)
            if rev is not None:
                target = rev

        rag = target.get("rag_rules") or option.get("rag_rules") or None
        snap = target.get("compliance_snapshot") or option.get("compliance_snapshot") or None
        # Treat empty dict/string as absent
        if not rag:
            rag = None
        if not snap:
            snap = None
        _log("get_cached_compliance: option={} rev={} → rag={} snap_len={}".format(
            option_id, revision_id, "yes" if rag else "no", len(snap) if snap else 0))
        return rag, snap

# ── Module-level singleton ────────────────────────────────────────────────────

_options_manager = None
_active_project_path = None  # set by server.py via set_active_project_path()


def set_active_project_path(rvt_path):
    """
    Called by server.set_revit_app() whenever Revit's active document is known.
    rvt_path: the full file path of the .rvt file (doc.PathName), or None/empty
              for an unsaved document.
    Resets the singleton so the next call to get_options_manager() opens the
    correct per-project store file.
    """
    global _active_project_path, _options_manager
    if rvt_path == _active_project_path:
        return  # already scoped to this project — nothing to do
    _active_project_path = rvt_path or None
    _options_manager = None  # force re-creation with the new path
    _log("set_active_project_path: project='{}' — singleton reset".format(rvt_path))


def _project_store_path():
    """
    Return the full path to build_options.json for the currently active project.

    Scoping strategy:
    - All store files live in %APPDATA%\\RevitMCP\\options\\.
    - For a saved project at C:/Work/MyBuilding.rvt the file is named
      build_options_MyBuilding.json  (stem only, no directory, no extension).
    - For an unsaved document the fallback name build_options.json is used.
    """
    import os as _os
    from revit_mcp.utils import get_appdata_path
    options_dir = get_appdata_path("options")

    if _active_project_path:
        stem = _os.path.splitext(_os.path.basename(_active_project_path))[0]
        safe_stem = re.sub(r'[^\w\-]', '_', stem)
        filename = "build_options_{}.json".format(safe_stem)
    else:
        filename = "build_options.json"

    return _os.path.join(options_dir, filename)


def get_options_manager():
    global _options_manager
    if _options_manager is None:
        store_path = _project_store_path()
        _log("get_options_manager: creating singleton, store={}".format(store_path))
        _options_manager = OptionsManager(store_path)
    return _options_manager
