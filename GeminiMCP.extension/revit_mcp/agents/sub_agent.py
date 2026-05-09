import asyncio
import json
import os
import time
import threading
import concurrent.futures
from revit_mcp.gemini_client import client
from revit_mcp.rag.vertex_rag import query_vertex_rag
from revit_mcp.rag.query_builder import build_queries

# Maps each RAG topic to the authority tag stored in structData.source of the
# structured datastore (e.g. "SCDF"). Matches the same convention used by the
# authority-query path in dispatcher._answer_authority_query.
_TOPIC_SOURCE: dict[str, str] = {
    "staircase":        "SCDF",
    "fire_lift":        "SCDF",
    "fire_lift_lobby":  "SCDF",
    "smoke_stop_lobby": "SCDF",
    "occupant_load":    "SCDF",
    "exit_width":       "SCDF",
    "travel_distance":  "SCDF",
    "corridor":         "SCDF",
}

# ── Chunk cache ────────────────────────────────────────────────────────────────
# Persists raw Vertex AI chunks to disk so subsequent builds skip Vertex entirely.
# Structure: { "SCDF": { "staircase": [ {chunk}, ... ], ... }, "BCA": { ... } }
# Key: (authority, topic) — authority derived from _TOPIC_SOURCE folder name.
# Building type is NOT part of the key: raw PDF text is the same regardless of
# building type; only Gemini synthesis (handled upstream) varies by building type.

from revit_mcp.utils import get_appdata_path as _get_appdata_path
_CHUNK_CACHE_PATH = os.path.join(_get_appdata_path("cache"), "chunk_cache.json")
_chunk_cache: dict = {}
_chunk_cache_lock = threading.Lock()


def _authority_from_source(source: str) -> str:
    """Extract authority name from GCS folder path, e.g. 'knowledge_base/SCDF' → 'SCDF'."""
    return source.rstrip("/").split("/")[-1] if source else "UNKNOWN"


def _load_chunk_cache() -> None:
    global _chunk_cache
    try:
        if os.path.isfile(_CHUNK_CACHE_PATH):
            with open(_CHUNK_CACHE_PATH, "r", encoding="utf-8") as fh:
                _chunk_cache = json.load(fh)
            total = sum(len(chunks) for auth in _chunk_cache.values() for chunks in auth.values())
            _log(f"[ChunkCache] Loaded {total} chunks from {_CHUNK_CACHE_PATH}")
        else:
            _chunk_cache = {}
            _log(f"[ChunkCache] No cache file found — starting fresh")
    except Exception as e:
        _chunk_cache = {}
        _log(f"[ChunkCache] Load failed ({e}) — starting fresh")


def _save_chunk_cache() -> None:
    try:
        with open(_CHUNK_CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(_chunk_cache, fh, indent=2, ensure_ascii=False)
        _log(f"[ChunkCache] Saved to {_CHUNK_CACHE_PATH}")
    except Exception as e:
        _log(f"[ChunkCache] Save failed: {e}")


def _get_cached_chunks(authority: str, topic: str) -> list | None:
    """Return cached chunks for (authority, topic), or None if not cached."""
    return _chunk_cache.get(authority, {}).get(topic)


def _merge_and_cache_chunks(authority: str, topic: str, new_chunks: list) -> list:
    """Merge new_chunks into cache for (authority, topic), dedup, save, return merged list."""
    with _chunk_cache_lock:
        existing = _chunk_cache.setdefault(authority, {}).get(topic, [])
        seen = {c["content"][:120] for c in existing if c.get("content")}
        added = 0
        for chunk in new_chunks:
            key = chunk.get("content", "")[:120]
            if key and key not in seen:
                seen.add(key)
                existing.append(chunk)
                added += 1
        _chunk_cache[authority][topic] = existing
        _log(f"[ChunkCache] {authority}/{topic}: +{added} new chunks ({len(existing)} total)")
        _save_chunk_cache()
        return existing


SUB_AGENT_PROMPT = """
You are an SCDF fire code specialist. Extract spatial requirements from the RETRIEVED EXCERPTS ONLY, scoped to the building described in OCCUPANCY SCOPE.

OCCUPANCY SCOPE (THIS IS THE ONLY BUILDING YOU ARE EXTRACTING RULES FOR):
- Purpose Group: {purpose_group}
- Occupancy class: {occupancy_class}
- Height band: {height_band} ({storeys} storeys)
- Sprinklered: {sprinklered}
- Mixed-use: {mixed_use}
- Scope summary: {scope_summary}
- EXCLUDE rules that apply ONLY to these Purpose Groups: {exclude_pg}

OUTPUT TOPICS (you MUST output exactly these topic keys, no more, no fewer):
{topics_csv}

STRICT RULES:
1. Use ONLY the text in the excerpts below. Do NOT use outside knowledge.
2. Do NOT invent or guess any value. If a value is not stated in the excerpts, omit that key entirely.
3. Do NOT invent clause numbers. Extract the exact clause/section reference that appears in the text (e.g. "3.2.1", "Section 4.5.2"). Pre-extracted clause refs are listed in [refs: ...] after each excerpt — use them.
4. If a value conflicts between excerpts, use the STRICTER requirement (larger minimum, smaller maximum).
5. Each dimension value MUST be an object: {{"dimension": <number>, "clause": "<ref or null>"}}.
6. Booleans also use the same format: {{"dimension": true, "clause": "<ref>"}}.
7. Each topic must have a "source" key naming the document and version (e.g. "SCDF Fire Code 2023").
8. SCOPE FILTER: OMIT a rule ONLY when the excerpt CLEARLY restricts it to a Purpose Group listed in EXCLUDE, AND no other clause restates the rule in a generally-applicable way. Examples for an office (PG V) build:
   - Clause 9.2.1a explicitly says "for residential apartments or maisonettes" — OMIT.
   - Clause 9.4.1a "office floor in a PG II residential building" — OMIT (it's about ancillary office in a residential block, not a standalone PG V office).
   - "PG II super-high-rise residential >40 storeys" — OMIT for PG V.
   - Hotel-only (PG VII) clauses — OMIT for PG V.
   IMPORTANT — do NOT over-omit. Many SCDF rules are general (apply to all PGs) and will appear in chunks without naming a specific PG; you MUST extract those even if surrounding chunks talk about other PGs. When a rule is general (e.g. "all exit staircases shall be at least 1000mm wide", "minimum corridor width per Table 2.2A occupancy row"), extract it. The default is INCLUDE; only OMIT when you can point to explicit PG-restriction language in the excerpt.
9. PICK THE RIGHT VALUE FOR THIS BUILDING. Many tables have multiple columns/rows for the same parameter (sprinklered vs non-sprinklered, exit-to-outdoors vs staircase, different occupancy classes). You MUST pick the value that matches THIS building's OCCUPANCY SCOPE:
   - Sprinklered = {sprinklered}. When a table has sprinklered/non-sprinklered columns, pick the column matching this flag. If you output BOTH (because the build pipeline expects both — e.g. travel_distance), label them clearly.
   - Occupancy row = "{occupancy_class}". Use the row that matches this occupancy. Do NOT pick "building-to-building", "without commercial activities", or any unrelated row.
   - For staircase/exit-passageway capacity (persons_per_unit_width): use the STAIRCASES column, NOT the door-to-outdoors-at-ground-level column. The capacity calculation governs upper-storey egress through staircases, not ground-floor doors.
10. CONSOLIDATE context-variants into a SINGLE key. After applying the SCOPE FILTER, if the same dimension still appears under multiple conditions (e.g. different height thresholds), output ONE key using the value applicable to THIS building, and note the qualifying condition inside the clause field, e.g. {{"dimension": 2, "clause": "6.6.4b.(1) (habitable height > 24m, applies)"}}. Do NOT create suffix-variant keys like "min_count_super_high_rise_residential_gt_40_storeys".
11. STAY IN TOPIC SCOPE. Do NOT introduce new top-level topics that are not in OUTPUT TOPICS above. If you find a relevant rule that doesn't fit any listed topic, omit it.
12. Return ONLY a raw JSON object. No markdown, no explanation.

UNIT RULES (strictly enforced):
- Lengths, widths, heights, depths: always mm. Convert m → mm by multiplying by 1000.
- Areas: always mm2. Convert m2 → mm2 by multiplying by 1,000,000. Example: 4m × 2m lobby = 8,000,000mm2. Example: 6m2 lobby = 6,000,000mm2.
- Speeds: m/s. Loads: kg.

SCDF TABLE 2.2A COLUMN ORDER (left to right, 12 data columns after the occupancy name):
Col 1: One-way travel non-spr (m)
Col 2: One-way travel spr (m)
Col 3: Two-way travel non-spr (m)
Col 4: Two-way travel spr (m)
Col 5: Persons/unit — door opening exiting to OUTDOORS at ground level, non-spr
Col 6: Persons/unit — door opening exiting to OUTDOORS at ground level, spr
Col 7: Persons/unit — STAIRCASES and exit passageways, non-spr
Col 8: Persons/unit — STAIRCASES and exit passageways, spr
Col 9: Persons/unit — Ramps, corridors, other exits
Col 10: Min width (m)
Col 11: Max dead end non-spr (m)
Col 12: Max dead end spr (m)

Example row from the table: "| Offices | 15 | 30 | 45 | 75 | 100 | 80 | 60 | 100 | 1 | 1.2 | 15 | 20 |"

DECISION TREE — for THIS building (sprinklered={sprinklered}, occupancy="{occupancy_class}"), use this row to pick column values:
- max_travel_distance_mm           → if sprinklered=True use Col 4 × 1000, else use Col 3 × 1000.
                                     Also output the OTHER variant under max_travel_distance_sprinklered_mm so both keys are populated.
                                     For the Offices row: Col 3=45, Col 4=75 → if sprinklered: max_travel_distance_mm=75000 AND max_travel_distance_sprinklered_mm=75000 with clause noting "Two-way travel sprinklered (applies to this building)".
                                     If not sprinklered: max_travel_distance_mm=45000, max_travel_distance_sprinklered_mm=75000 (both reported).
- persons_per_unit_width           → use STAIRCASES column matching sprinkler flag. If sprinklered=True use Col 8, else Col 7. NEVER use Col 5/6 (those are door-to-outdoors at ground level — wrong for upper-storey staircase egress).
                                     For the Offices row sprinklered: Col 8=100. Non-sprinklered: Col 7=60.
- exit_width_per_unit_mm           → per Cl.2.2.5a one unit of width = 500mm. Always output 500.
- min_corridor_width_mm (corridor topic) → use Col 10 (Min width) × 1000. For Offices row: 1.2m × 1000 = 1200mm.
- min_flight_width_mm (staircase)  → also Col 10 (Min width) × 1000 — the table's minimum exit width applies to staircases too unless a stricter clause overrides.

For occupant_load:
- occupant_load_factor_m2 is m2 per person from the SCDF Occupancy Load table (typically Table 2.2C or in Clause 1.4 definitions, NOT Table 2.2A).
- Match the row labelled for THIS occupancy: "{occupancy_class}". For an office, look for "Offices", "Office area", or "Business centre/office (general)" rows — these typically give 9–10 m2/person.
- IGNORE rows labelled "building-to-building", "without commercial activities", or any clearly different occupancy. If the only office-row value you can find is in the chunks, use it. If no office-applicable value is in the chunks at all, omit the key.

Retrieved Excerpts (with pre-extracted clause refs):
{chunks_text}

Required JSON format:
{{
  "authority": "SCDF",
  "rules": {{
    "staircase": {{
      "min_flight_width_mm":      {{"dimension": <number>, "clause": "<ref or null>"}},
      "min_landing_width_mm":     {{"dimension": <number>, "clause": "<ref or null>"}},
      "max_riser_mm":             {{"dimension": <number>, "clause": "<ref or null>"}},
      "min_tread_mm":             {{"dimension": <number>, "clause": "<ref or null>"}},
      "min_headroom_mm":          {{"dimension": <number>, "clause": "<ref or null>"}},
      "min_overrun_mm":           {{"dimension": <number>, "clause": "<ref or null>"}},
      "max_travel_distance_mm":   {{"dimension": <number>, "clause": "<ref or null>"}},
      "max_travel_distance_sprinklered_mm": {{"dimension": <number>, "clause": "<ref or null>"}},
      "min_count":                {{"dimension": <number>, "clause": "<ref or null>"}},
      "source": "<document name and version>"
    }},
    "fire_lift": {{
      "<rule_key>": {{"dimension": <number_or_bool>, "clause": "<ref or null>"}},
      "source": "<document name and version>"
    }},
    "fire_lift_lobby": {{
      "min_area_mm2":  {{"dimension": <number>, "clause": "<ref or null>"}},
      "min_width_mm":  {{"dimension": <number>, "clause": "<ref or null>"}},
      "source": "<document name and version>"
    }},
    "smoke_stop_lobby": {{
      "min_area_mm2":      {{"dimension": <number>, "clause": "<ref or null>"}},
      "min_width_mm":      {{"dimension": <number>, "clause": "<ref or null>"}},
      "min_clear_depth_mm": {{"dimension": <number>, "clause": "<ref or null>"}},
      "source": "<document name and version>"
    }},
    "occupant_load": {{
      "occupant_load_factor_m2":  {{"dimension": <number>, "clause": "<ref or null>"}},
      "source": "<document name and version>"
    }},
    "exit_width": {{
      "persons_per_unit_width":   {{"dimension": <number>, "clause": "<ref or null>"}},
      "exit_width_per_unit_mm":   {{"dimension": <number>, "clause": "<ref or null>"}},
      "source": "<document name and version>"
    }},
    "corridor": {{
      "min_corridor_width_mm":    {{"dimension": <number>, "clause": "<ref or null>"}},
      "source": "<document name and version>"
    }}
  }}
}}
"""


def _log(msg):
    client.log(f"[RAG] {msg}")


# Load cache at module import time (after _log is defined)
_load_chunk_cache()


async def _fetch_topic(topic: str, intent: dict) -> dict:
    from revit_mcp.cancel_manager import check_cancelled
    check_cancelled("RAG fetch {}".format(topic))
    t0 = time.time()
    source = _TOPIC_SOURCE.get(topic)
    authority = _authority_from_source(source)
    _log(f"_fetch_topic START: {topic} (authority={authority})")

    # ── Cache hit: skip Vertex entirely ───────────────────────────────────────
    cached = _get_cached_chunks(authority, topic)
    if cached:
        _log(f"_fetch_topic CACHE HIT: {authority}/{topic} — {len(cached)} chunks (skipping Vertex) in {time.time()-t0:.2f}s")
        return {"topic": topic, "results": cached}

    # ── Cache miss: query Vertex and update cache ──────────────────────────────
    _log(f"_fetch_topic CACHE MISS: {authority}/{topic} — querying Vertex AI")
    queries = build_queries(topic, intent)
    # Topic-specific top_k overrides — bumped for topics where the right table
    # row can be split across multiple chunks (Table 1.4B Office row was missed
    # at top_k=2 because the chunk containing "Pedestrian linkways/MRT" came
    # back instead).
    _topic_top_k = {"occupant_load": 5, "exit_width": 4}.get(topic, 2)
    _log(f"_fetch_topic {len(queries)} sub-queries for {topic} | source_filter={source} | top_k={_topic_top_k}")
    loop = asyncio.get_running_loop()

    async def _run_one(q):
        try:
            return await loop.run_in_executor(
                None, lambda: query_vertex_rag(query=q, top_k=_topic_top_k, source_filter=source)
            )
        except Exception as e:
            _log(f"_fetch_topic sub-query ERROR ({q[:60]}): {e}")
            return []

    all_batches = await asyncio.gather(*[_run_one(q) for q in queries])

    # Deduplicate within this fetch by content prefix
    seen_content = set()
    fresh_chunks = []
    for batch in all_batches:
        for chunk in batch:
            key = chunk.get("content", "")[:120]
            if key and key not in seen_content:
                seen_content.add(key)
                fresh_chunks.append(chunk)

    # Merge into persistent cache (handles dedup against previously cached chunks)
    results = _merge_and_cache_chunks(authority, topic, fresh_chunks)

    _log(f"_fetch_topic DONE: {topic} — {len(results)} chunks ({len(fresh_chunks)} fresh) in {time.time()-t0:.2f}s")
    return {"topic": topic, "results": results}


async def retrieve_rules(intent: dict, report=None, set_status=None) -> dict:
    t0 = time.time()
    topics = intent.get("topics", [])
    _log(f"retrieve_rules START — intent={intent}")

    if not topics:
        _log("retrieve_rules: no topics in intent, skipping")
        return {}

    _log(f"retrieve_rules: querying Vertex AI RAG for: {', '.join(topics)}")
    if set_status:
        set_status("Sub-agent: searching authority code library for {} topic(s)...".format(len(topics)))

    _log(f"retrieve_rules: firing {len(topics)} parallel Vertex queries...")
    t_vertex = time.time()
    raw_results = await asyncio.gather(*[_fetch_topic(t, intent) for t in topics])
    _t_v = time.time() - t_vertex
    _log(f"retrieve_rules: all Vertex queries done in {_t_v:.2f}s")
    if set_status:
        set_status("Sub-agent: {} code excerpts retrieved in {:.0f}s — synthesising rules...".format(
            sum(len(r["results"]) for r in raw_results), _t_v))

    chunks_text = ""
    retrieved_summary = []
    for item in raw_results:
        topic_label = item["topic"].replace("_", " ").title()
        chunks_text += f"\n\n--- {item['topic'].upper()} ---\n"
        top_chunks = []
        for chunk in item["results"]:
            meta       = chunk["metadata"]
            clause     = meta.get("clause", "unknown clause")
            clause_refs = meta.get("clause_refs", [])
            content    = chunk["content"]
            source_uri = chunk.get("source_uri", "")
            # Include pre-extracted clause refs inline so synthesis can use them
            refs_str = f" [refs: {', '.join(clause_refs)}]" if clause_refs else ""
            chunks_text += f"[{clause}{refs_str}] {content}\n"
            _log(f"  chunk [{clause}] refs={clause_refs} source={source_uri} | content={content}")
            if content:
                top_chunks.append(f"_{clause}_: {content}")
        if top_chunks:
            retrieved_summary.append(f"**{topic_label}**\n" + "\n".join(f"  • {c}" for c in top_chunks[:2]))

    # Raw excerpts are intentionally NOT reported to the user — they are verbose
    # internal data. The clean synthesised summary is reported after Gemini synthesis.

    _log(f"=== RAW CHUNKS SENT TO SYNTHESIS ({len(chunks_text)} chars) ===\n{chunks_text}\n=== END CHUNKS ===")
    _log(f"retrieve_rules: chunks assembled ({len(chunks_text)} chars), calling Gemini for synthesis...")

    # Pull scope fields from intent — fall back to permissive defaults if enrichment was skipped
    _topics = intent.get("topics", []) or list(raw_results and [r["topic"] for r in raw_results])
    _exclude_pg = intent.get("exclude_pg") or []
    _exclude_str = ", ".join(_exclude_pg) if _exclude_pg else "(none specified — apply your judgement based on Purpose Group)"
    prompt = SUB_AGENT_PROMPT.format(
        purpose_group=intent.get("purpose_group", "unspecified"),
        occupancy_class=intent.get("occupancy_class", intent.get("building_type", "building")),
        height_band=intent.get("height_band", "unspecified"),
        storeys=intent.get("storeys", "?"),
        sprinklered=intent.get("sprinklered", True),
        mixed_use=intent.get("mixed_use", False),
        scope_summary=intent.get("scope_summary", "{} building".format(intent.get("building_type", ""))),
        exclude_pg=_exclude_str,
        topics_csv=", ".join(_topics) if _topics else "(none — output the topics implied by the chunks)",
        chunks_text=chunks_text,
    )

    t_gemini = time.time()
    try:
        raw_text = client.generate_content(prompt, thinking_budget=0, temperature=0.3)
        _t_g = time.time() - t_gemini
        _log(f"retrieve_rules: Gemini synthesis done in {_t_g:.2f}s, response len={len(raw_text)}")
        if set_status:
            set_status("Sub-agent: rules synthesised in {:.0f}s — passing to main agent...".format(_t_g))
    except Exception as e:
        _log(f"retrieve_rules: Gemini synthesis FAILED in {time.time()-t_gemini:.2f}s — {e}")
        if report:
            report("⚠️ Authority Code Sub-Agent: Gemini synthesis failed, using static fallback.")
        return {}

    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]

    try:
        rules = json.loads(raw_text.strip())

        # Sanity-check area values — if any _mm2 key is unreasonably small it was
        # returned in m2 instead of mm2. Threshold: anything < 100,000 mm2 (0.1 m2)
        # for a lobby/room is physically impossible, so multiply by 1,000,000.
        _AREA_KEYS = {"min_area_mm2", "min_floor_area_mm2"}
        for topic_vals in rules.get("rules", {}).values():
            for k, v in topic_vals.items():
                if k in _AREA_KEYS and isinstance(v, dict) and "dimension" in v:
                    dim = v["dimension"]
                    if isinstance(dim, (int, float)) and 0 < dim < 100000:
                        corrected = int(dim * 1_000_000)
                        _log(f"[AreaFix] {k}={dim} looks like m2 — converting to {corrected} mm2")
                        v["dimension"] = corrected

        # Soft sanity check: occupant_load_factor for office buildings should be ~10 m²/p.
        # Anything below 3 strongly suggests Vertex returned the wrong table row
        # (e.g. "building without commercial activities" = 5 m²/p, or "assembly" rows
        # that are 1-2 m²/p). Log a warning so it's visible in fastmcp_server.log.
        _occ_topic = rules.get("rules", {}).get("occupant_load", {})
        _occ_factor = _occ_topic.get("occupant_load_factor_m2")
        if isinstance(_occ_factor, dict):
            _occ_factor = _occ_factor.get("dimension")
        _occ_class = (intent.get("occupancy_class") or "").lower()
        if isinstance(_occ_factor, (int, float)) and _occ_factor < 3 and "office" in _occ_class:
            _log(f"[RAG] WARNING: occupant_load_factor_m2={_occ_factor} for {_occ_class} "
                 f"looks suspicious (offices typically ~10 m²/p) — Vertex may have "
                 f"returned the wrong table row. Continuing with this value.")

        _log(f"retrieve_rules: JSON parsed OK — topics={list(rules.get('rules', {}).keys())} total={time.time()-t0:.2f}s")
        _log(f"=== SUB-AGENT OUTPUT (passed to main AI) ===\n{json.dumps(rules, indent=2)}\n=== END SUB-AGENT OUTPUT ===")
        return rules
    except Exception as e:
        _log(f"retrieve_rules: JSON parse FAILED — {e} | raw={raw_text[:200]}")
        if report:
            report("⚠️ Authority Code Sub-Agent: failed to parse rules, using static fallback.")
        return {}


def _run_in_new_loop(coro):
    _log("_run_in_new_loop: creating fresh event loop")
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(coro)
        _log("_run_in_new_loop: event loop completed OK")
        return result
    except Exception as e:
        _log(f"_run_in_new_loop: event loop raised {e}")
        raise
    finally:
        loop.close()


def run_retrieve_rules(intent: dict, report=None, set_status=None) -> dict:
    """Synchronous entry point for dispatcher.py."""
    from revit_mcp.cancel_manager import is_cancelled
    t0 = time.time()
    _log(f"run_retrieve_rules START — intent={intent}")
    # Use explicit executor (not context manager) so shutdown(wait=False) can be
    # called immediately on cancel without waiting for the running thread to finish.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        _log("run_retrieve_rules: submitting to ThreadPoolExecutor...")
        future = executor.submit(_run_in_new_loop, retrieve_rules(intent, report=report, set_status=set_status))
        _log("run_retrieve_rules: waiting for result (timeout=150s)...")
        deadline = time.time() + 150
        while True:
            try:
                result = future.result(timeout=0.5)
                _log(f"run_retrieve_rules DONE in {time.time()-t0:.2f}s")
                return result
            except concurrent.futures.TimeoutError:
                if is_cancelled():
                    raise RuntimeError("Build cancelled by user.")
                if time.time() >= deadline:
                    _log(f"run_retrieve_rules TIMEOUT after {time.time()-t0:.2f}s — falling back to static rules")
                    if report:
                        report("⚠️ Authority code retrieval timed out (>150s), using static rules.")
                    return {}
    except RuntimeError as e:
        if "cancelled" in str(e).lower():
            raise
        _log(f"run_retrieve_rules ERROR after {time.time()-t0:.2f}s — {e}")
        if report:
            report(f"⚠️ Authority code retrieval error: {e}")
        return {}
    except Exception as e:
        _log(f"run_retrieve_rules ERROR after {time.time()-t0:.2f}s — {e}")
        if report:
            report(f"⚠️ Authority code retrieval error: {e}")
        return {}
    finally:
        executor.shutdown(wait=False)  # don't block on in-flight thread after cancel


def format_rules_for_display(rules: dict) -> str:
    """Build a markdown table string from the final merged rules dict. Returns '' if no rows."""
    _TOPIC_ORDER = ["occupant_load", "exit_width", "staircase", "smoke_stop_lobby", "fire_lift_lobby", "fire_lift", "corridor"]
    _TOPIC_LABELS = {
        "staircase":        "Staircases",
        "fire_lift_lobby":  "Fire Lift Lobby",
        "smoke_stop_lobby": "Smoke Stop Lobby",
        "fire_lift":        "Fire Lift",
        "occupant_load":    "Occupant Load",
        "exit_width":       "Exit Width",
        "corridor":         "Corridor",
    }
    _KEY_LABELS = {
        "min_flight_width_mm":                "Min flight width",
        "min_landing_width_mm":               "Min landing width",
        "max_riser_mm":                       "Max riser",
        "min_tread_mm":                       "Min tread",
        "min_headroom_mm":                    "Min headroom",
        "min_overrun_mm":                     "Min overrun",
        "max_travel_distance_mm":             "Max travel distance",
        "max_travel_distance_sprinklered_mm": "Max travel distance (sprinklered)",
        "min_count":                          "Min staircase count",
        "min_car_width_mm":                   "Min car width",
        "min_car_depth_mm":                   "Min car depth",
        "min_car_size_mm":                    "Min car size",
        "min_door_width_mm":                  "Min door width",
        "min_load_kg":                        "Min load",
        "min_speed_m_s":                      "Min speed",
        "min_area_mm2":                       "Min area",
        "min_width_mm":                       "Min width",
        "min_depth_mm":                       "Min depth",
        "min_clear_depth_mm":                 "Min clear depth",
        "pressurisation_required":            "Pressurisation required",
        "occupant_load_factor_m2":            "Occupant load factor",
        "persons_per_unit_width":             "Persons per unit width",
        "exit_width_per_unit_mm":             "Exit width per unit",
        "min_corridor_width_mm":              "Min corridor width",
    }
    all_rules = rules.get("rules", {})
    if not all_rules:
        return ""
    rows = []
    ordered = [t for t in _TOPIC_ORDER if t in all_rules]
    ordered += [t for t in all_rules if t not in ordered]
    for topic in ordered:
        vals   = all_rules[topic]
        label  = _TOPIC_LABELS.get(topic, topic.replace("_", " ").title())
        source = vals.get("source", "")
        first  = True
        for key, val in vals.items():
            if key == "source" or val is None:
                continue
            pretty = _KEY_LABELS.get(key, key.replace("_", " ").capitalize())
            if isinstance(val, dict):
                dim    = val.get("dimension", "")
                clause = val.get("clause") or ""
                if isinstance(dim, bool):
                    dim_str = "Yes" if dim else "No"
                elif isinstance(dim, (int, float)):
                    dim_str = str(int(dim)) if float(dim) == int(dim) else str(dim)
                else:
                    dim_str = str(dim)
                clause_str = f"Cl. {clause}" if clause else "—"
            else:
                dim_str    = str(val)
                clause_str = "—"
            topic_cell  = f"**{label}**" if first else ""
            source_cell = source if first else ""
            rows.append(f"| {topic_cell} | {pretty} | {dim_str} | {clause_str} | {source_cell} |")
            first = False
    if not rows:
        return ""
    header = "| Topic | Parameter | Value | Clause | Source |\n| --- | --- | --- | --- | --- |"
    return header + "\n" + "\n".join(rows)
