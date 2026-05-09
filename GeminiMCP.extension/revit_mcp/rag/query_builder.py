# RAG query builder.
#
# Primary path: Gemini expands each topic into 3 focused Vertex search queries based
# on the user's building intent. No clause numbers are hardcoded, so the system survives
# code renumbering by the authority.
#
# Fallback path: if Gemini expansion fails (network error, parse error, etc.), we fall
# back to the legacy hardcoded TOPIC_QUERIES below. These are also used as a HINT in the
# expansion prompt so Gemini has a sensible starting point but is free to override.

import json
import re
import threading

# Topic descriptions used in the Gemini expansion prompt — describe what each topic
# means in domain terms so Gemini can write good queries even if it doesn't know the
# topic key. Free-form, not hardcoded clauses.
TOPIC_DESCRIPTIONS = {
    "staircase":        "Exit staircase requirements: minimum number per storey, flight width, riser height, tread depth, headroom, landing dimensions.",
    "fire_lift":        "Fire lift requirements: minimum number, car platform dimensions, door clear width, load capacity, speed, travel distance to staircase.",
    "fire_lift_lobby":  "Fire lift lobby requirements: minimum floor area, minimum clear width, minimum depth.",
    "smoke_stop_lobby": "Smoke stop / smoke-free lobby requirements: minimum floor area, minimum clear width, ventilation.",
    "occupant_load":    "Occupant load factor — floor area per person — for the building's occupancy class.",
    "exit_width":       "Exit width capacity: persons per unit width for staircases and exit passageways; the unit-width definition (typically 500mm).",
    "travel_distance":  "Maximum travel distance to an exit (one-way and two-way), sprinklered and non-sprinklered.",
    "corridor":         "Minimum corridor / exit-access passageway clear width.",
}

# Legacy hardcoded queries — used as a hint in Gemini's expansion prompt and as a
# fallback when Gemini expansion fails. Clause numbers here will go stale if the
# authority renumbers the code; treat as best-effort guidance only.
TOPIC_QUERIES = {
    "staircase": [
        "SCDF Clause 2.2.15 exit staircase minimum flight width riser tread headroom mm",
        "SCDF Clause 2.2.15d riser tread headroom exit staircase dimensions mm",
        "SCDF minimum headroom clearance exit staircase overrun mm Clause 2.2.15",
        "SCDF minimum number of exit staircases required Clause 2.2.11 {building_type}",
        "SCDF Clause 2.2.11 number of exits required {building_type}",
    ],
    "fire_lift": [
        "SCDF Clause 6.6 fire lift minimum car platform size width depth mm office building",
        "SCDF Clause 6.6.2 fire lift car platform minimum dimensions mm",
        "SCDF fire lift minimum door clear width load capacity speed Clause 6.6",
    ],
    "fire_lift_lobby": [
        "SCDF Clause 2.2.13b fire lift lobby minimum floor area m2 minimum clear width mm",
        "SCDF smoke-free lobby also serves fire lift lobby floor area 6m2 minimum width 2m Clause 2.2.13b",
        "SCDF fire lift lobby minimum size area width Clause 2.2.13",
    ],
    "smoke_stop_lobby": [
        "SCDF Clause 2.2.13b smoke-free lobby minimum floor area 3m2 minimum clear width 1.2m",
        "SCDF smoke-free lobby minimum size area width Clause 2.2.13",
        "SCDF smoke stop lobby minimum floor area minimum clear width mm Clause 2.2.13b",
    ],
    "occupant_load": [
        "SCDF Table 2.2A occupant load factor m2 per person office {building_type}",
        "SCDF Table occupancy load factor floor area per person office admin general",
        "SCDF Clause 2.2.4 occupant load calculation floor area per person {building_type}",
    ],
    "exit_width": [
        "SCDF Table 2.2A persons per unit width staircase exit passageway {building_type} non-sprinklered",
        "SCDF Clause 2.2.5 capacity exits unit width 500mm persons per unit staircase {building_type}",
        "SCDF Table 2.2A column 7 staircase exit passageway persons per unit non-sprinklered offices",
    ],
    "travel_distance": [
        "SCDF Table 2.2A maximum travel distance {building_type} two-way non-sprinklered sprinklered metres",
        "SCDF Table 2.2A offices two-way travel distance non-sprinklered 45m sprinklered 75m",
        "SCDF Clause 2.2.6 maximum travel distance {building_type} Table 2.2A metres",
    ],
    "corridor": [
        "SCDF minimum corridor width mm exit access {building_type} Clause 2.2",
        "SCDF minimum internal corridor width exit access means of escape mm",
        "SCDF access corridor minimum clear width mm Clause 2.3",
    ],
}

_TYPE_LABELS = {
    "commercial_office": "commercial office",
    "residential":       "residential",
    "mixed_use":         "mixed-use",
    "industrial":        "industrial",
    "hotel":             "hotel",
    "retail":            "retail",
}


# Process-wide cache: (topic, building_type, storey_bucket) -> [queries]
# Avoids re-asking Gemini to expand the same intent inside one session. Storeys are
# bucketed (low/mid/high) so minor variations don't bust the cache.
_expansion_cache: dict = {}
_expansion_cache_lock = threading.Lock()


def _storey_bucket(storeys) -> str:
    try:
        n = int(storeys)
    except (TypeError, ValueError):
        return "unknown"
    if n <= 4:
        return "low_rise"
    if n <= 24:
        return "mid_rise"
    if n <= 40:
        return "high_rise"
    return "super_high_rise"


def _legacy_queries(topic: str, intent: dict) -> list:
    """Render the hardcoded TOPIC_QUERIES with intent substitution. Used as fallback."""
    templates = TOPIC_QUERIES.get(topic, [f"SCDF {topic} requirements"])
    building_type = _TYPE_LABELS.get(intent.get("building_type", ""), intent.get("building_type", "commercial office"))
    storeys = intent.get("storeys", "")
    storey_suffix = f" {storeys} storeys" if storeys else ""
    return [t.format(building_type=building_type) + storey_suffix for t in templates]


_EXPANSION_PROMPT = """You are a search query expert for the SCDF Fire Code (Singapore Civil Defence Force, Code of Practice for Fire Precautions in Buildings).

A building designer is querying the code for the following topic:

TOPIC: {topic}
TOPIC MEANING: {topic_description}

Building intent:
- Building type: {building_type}
- Occupancy class: {occupancy_class}
- Purpose group: {purpose_group}
- Number of storeys: {storeys} ({storey_bucket})
- Sprinklered: {sprinklered}

Your task: generate exactly 3 focused search queries to retrieve the most relevant SCDF Fire Code passages from a vector database for THIS specific topic and THIS specific building.

Rules:
- Each query must be ABOUT THE SCDF FIRE CODE (do not invent queries for other authorities).
- Use the OCCUPANCY CLASS as the authoritative occupant-class label. When the topic is occupancy-dependent (occupant load, exit width, travel distance, corridor, staircase count), include the occupancy class verbatim in at least one of the queries. Different occupancy classes have different code values, and Vertex will pick the wrong row if the class is not specified.
- Use the PURPOSE GROUP only if you are confident it appears in the SCDF code (e.g. "PG II super-high-rise residential"). When unsure, prefer the occupancy class.
- For occupant_load specifically: include the occupancy class explicitly so Vertex retrieves the right table row (e.g. for an Office occupancy include "Office occupant load factor m2 per person", not "occupant load factor for buildings").
- Do NOT mention purpose groups or occupancy classes that are not THIS building's. If this building is an Office, do not mention "residential", "industrial", "assembly", or "building without commercial activities" anywhere in the queries.
- Be specific about the dimension or requirement being sought (e.g. "minimum staircase flight width in mm", "occupant load factor in m2 per person").
- Mention the relevant clause/table reference if you know it confidently. If unsure, omit the clause reference rather than guess — Vertex will still find the right page from semantic content.
- Vary phrasing across the 3 queries to maximise recall (one clause-focused, one dimension-focused, one occupancy-focused).
- Do NOT include phrases like "amendment", "circular", "effective date".

For reference, here are some example queries that have worked in the past for this topic — you may use these as inspiration but feel free to write better ones, especially if you think the clause numbers may have shifted:
{legacy_hint}

Return ONLY a JSON array of exactly 3 strings, no explanation:
["query 1", "query 2", "query 3"]
"""


def _expand_with_gemini(topic: str, intent: dict, log_fn=None) -> list | None:
    """Ask Gemini to generate 3 search queries for (topic, intent). Returns None on failure."""
    try:
        from revit_mcp.gemini_client import client
    except Exception as e:
        if log_fn:
            log_fn(f"[query_builder] gemini_client import failed: {e}")
        return None

    building_type = _TYPE_LABELS.get(intent.get("building_type", ""), intent.get("building_type", "commercial office"))
    storeys = intent.get("storeys", "")
    storey_bucket = _storey_bucket(storeys)
    occupancy_class = intent.get("occupancy_class") or "(infer from building type)"
    purpose_group = intent.get("purpose_group") or "(infer from building type)"
    sprinklered = intent.get("sprinklered")
    if sprinklered is True:
        sprinklered_str = "yes"
    elif sprinklered is False:
        sprinklered_str = "no"
    else:
        sprinklered_str = "unspecified"
    topic_description = TOPIC_DESCRIPTIONS.get(topic, f"Requirements relating to {topic.replace('_', ' ')}.")
    legacy_hint = "\n".join(f"  - {q}" for q in _legacy_queries(topic, intent)[:3])

    prompt = _EXPANSION_PROMPT.format(
        topic=topic,
        topic_description=topic_description,
        building_type=building_type,
        occupancy_class=occupancy_class,
        purpose_group=purpose_group,
        sprinklered=sprinklered_str,
        storeys=storeys or "unspecified",
        storey_bucket=storey_bucket,
        legacy_hint=legacy_hint,
    )

    try:
        raw = client.generate_content(prompt, thinking_budget=0, temperature=0.2)
    except Exception as e:
        if log_fn:
            log_fn(f"[query_builder] Gemini expansion FAILED for {topic}: {e}")
        return None

    match = re.search(r'\[.*?\]', raw, re.DOTALL)
    if not match:
        if log_fn:
            log_fn(f"[query_builder] no JSON array in expansion response for {topic}: {raw[:200]!r}")
        return None
    try:
        expanded = json.loads(match.group())
    except Exception as e:
        if log_fn:
            log_fn(f"[query_builder] JSON parse failed for {topic}: {e} | raw={match.group()[:200]!r}")
        return None

    queries = [q.strip() for q in expanded if isinstance(q, str) and q.strip()]
    if not queries:
        if log_fn:
            log_fn(f"[query_builder] expansion returned no usable queries for {topic}")
        return None

    if log_fn:
        log_fn(f"[query_builder] Gemini expanded {topic} ({building_type}, {occupancy_class}, {storey_bucket}) -> {queries}")
    return queries


def build_queries(topic: str, intent: dict) -> list:
    """Return a list of search queries for the given topic.

    Primary: legacy TOPIC_QUERIES with intent substitution (fast, no LLM call).
    Fallback: Gemini expansion only for topics with no legacy template — preserves
    flexibility for unknown topics without paying the LLM latency on every build.
    """
    try:
        from revit_mcp.gemini_client import client as _client
        log_fn = lambda m: _client.log(f"[RAG] {m}")
    except Exception:
        log_fn = None

    if topic in TOPIC_QUERIES:
        return _legacy_queries(topic, intent)

    cache_key = (topic, intent.get("building_type", ""), _storey_bucket(intent.get("storeys", "")))
    with _expansion_cache_lock:
        cached = _expansion_cache.get(cache_key)
    if cached:
        if log_fn:
            log_fn(f"[query_builder] expansion cache HIT for {cache_key} -> {len(cached)} queries")
        return list(cached)

    expanded = _expand_with_gemini(topic, intent, log_fn=log_fn)
    if expanded:
        with _expansion_cache_lock:
            _expansion_cache[cache_key] = list(expanded)
        return expanded

    if log_fn:
        log_fn(f"[query_builder] no legacy template and Gemini expansion failed for {topic} — using generic fallback")
    return _legacy_queries(topic, intent)


def build_query(topic: str, intent: dict) -> str:
    """Legacy single-query interface — returns the first sub-query."""
    return build_queries(topic, intent)[0]
