import re

_BUILDING_TYPES = {
    "commercial_office": ["office", "commercial", "corp", "corporate", "business"],
    "residential":       ["residential", "apartment", "flat", "condo", "housing", "home", "dwelling"],
    "mixed_use":         ["mixed", "mixed-use", "mixed use"],
    "industrial":        ["industrial", "warehouse", "factory", "logistics"],
    "hotel":             ["hotel", "hospitality", "resort"],
    "retail":            ["retail", "mall", "shop", "shopping"],
}


def extract_intent(user_prompt: str) -> dict:
    """Parse building intent from the user prompt using regex — no LLM call needed.

    Any building above 4 storeys always requires staircase, fire_lift, fire_lift_lobby
    per SCDF rules, so we can determine topics purely from storey count.
    """
    text = user_prompt.lower()

    # --- Storey count ---
    # Matches: "10 storey", "10-storey", "10 story", "10 floor", "10 level", "g+9"
    storey = 1
    m = re.search(r'(\d+)\s*[-\s]?\s*(?:storey|story|floor|level|fl)', text)
    if m:
        storey = int(m.group(1))
    else:
        # "g+9" style (ground + upper floors)
        m = re.search(r'g\s*\+\s*(\d+)', text)
        if m:
            storey = int(m.group(1)) + 1

    # --- Building type ---
    building_type = "commercial_office"  # safe default for Singapore high-rise
    for btype, keywords in _BUILDING_TYPES.items():
        if any(kw in text for kw in keywords):
            building_type = btype
            break

    # --- Topics — always retrieve occupant load, exit width, corridor, travel distance for any building.
    # SCDF mandates fire safety systems above 4 storeys.
    topics = ["staircase", "occupant_load", "exit_width", "travel_distance", "corridor", "smoke_stop_lobby"]
    if storey > 4:
        topics += ["fire_lift", "fire_lift_lobby"]

    return {"topics": topics, "building_type": building_type, "storeys": storey}


# ── Richer intent extraction (LLM-driven) ─────────────────────────────────────
# Used by the RAG sub-agent so synthesis knows the occupancy class, sprinkler
# status, and height band of the building. This lets the synthesizer exclude
# rules that apply to other occupancies (e.g. residential PG II clauses leaking
# into a commercial office build).
#
# This is additive — extract_intent() above is unchanged. enrich_intent() takes
# the regex output and asks Gemini to fill in the qualitative fields. On any
# failure (network, parse, etc.) it returns a safe default derived from the
# regex intent so the RAG flow never blocks.

# SCDF Purpose Group reference:
#   PG I    — Residential houses (detached, semi-detached, terrace)
#   PG II   — Residential flats / apartments / maisonettes
#   PG III  — Institutional (hospitals, nursing homes)
#   PG IV   — Educational
#   PG V    — Office / commercial / retail
#   PG VI   — Industrial
#   PG VII  — Hotel / hostel / dormitory
#   PG VIII — Place of public resort (cinema, restaurant, place of worship)
_PG_BY_BUILDING_TYPE = {
    "commercial_office": "PG V",
    "residential":       "PG II",
    "industrial":        "PG VI",
    "hotel":             "PG VII",
    "retail":            "PG V",
    "mixed_use":         "PG V/VII (mixed)",
}


def _height_band(storeys: int) -> str:
    if storeys <= 4:
        return "low_rise"
    if storeys <= 24:
        return "mid_rise"  # roughly <24m habitable height with 4m floor-to-floor
    if storeys <= 40:
        return "high_rise"
    return "super_high_rise"


def _fallback_enrichment(base_intent: dict) -> dict:
    """Build a safe enriched intent from the regex output. Used when LLM enrichment fails."""
    btype   = base_intent.get("building_type", "commercial_office")
    storeys = int(base_intent.get("storeys", 1) or 1)
    return {
        **base_intent,
        "purpose_group":    _PG_BY_BUILDING_TYPE.get(btype, "PG V"),
        "occupancy_class":  btype.replace("_", " "),
        "height_band":      _height_band(storeys),
        "sprinklered":      True,   # default assumption for >4 storey new builds
        "mixed_use":        btype == "mixed_use",
        "exclude_pg":       [],
        "scope_summary":    "{} building, {} storeys ({})".format(
            btype.replace("_", " "), storeys, _height_band(storeys)),
        "_enrichment_source": "fallback",
    }


_ENRICH_PROMPT = """You are a building code intent classifier for the SCDF Fire Code (Singapore).

Given the user's building request and a regex pre-extraction, produce a richer intent description that downstream RAG synthesis will use to scope which code rules apply.

USER PROMPT:
{user_prompt}

REGEX PRE-EXTRACTION:
{regex_intent}

Return a JSON object with EXACTLY these fields:
{{
  "purpose_group":    "<SCDF Purpose Group: 'PG I', 'PG II', 'PG III', 'PG IV', 'PG V', 'PG VI', 'PG VII', 'PG VIII', or 'PG V/VII (mixed)' for mixed-use>",
  "occupancy_class":  "<plain-English occupancy, e.g. 'commercial office', 'residential apartments', 'hotel'>",
  "height_band":      "<one of: 'low_rise' (<=4 storeys), 'mid_rise' (5-24), 'high_rise' (25-40), 'super_high_rise' (>40)>",
  "sprinklered":      <true|false — assume true for any building above 4 storeys unless user explicitly says otherwise>,
  "mixed_use":        <true|false>,
  "exclude_pg":       [<list of PG codes whose rules MUST be excluded for this building, e.g. ["PG II", "PG VII"] for an office>],
  "scope_summary":    "<one-sentence summary used in synthesis prompt, e.g. 'PG V commercial office, 30 storeys, sprinklered, super-high-rise (>24m habitable height)'>"
}}

Rules:
- Be decisive — if the user said 'office', the purpose_group is PG V; do not hedge.
- exclude_pg should list every PG that is NOT this building's PG. For an office (PG V), exclude_pg should include PG I, PG II, PG III, PG IV, PG VI, PG VII, PG VIII (residential, institutional, industrial, etc.) — synthesis will use this to drop chunks that mention only those PGs.
- If the user describes a mixed-use building, list applicable PGs in purpose_group separated by '/' and only exclude PGs that are clearly absent.
- Output ONLY the raw JSON object. No markdown, no explanation.
"""


def enrich_intent(base_intent: dict, user_prompt: str, log_fn=None) -> dict:
    """Take the regex intent dict and ask Gemini to add occupancy_class, purpose_group, etc.

    Returns a dict with all the original keys plus the enrichment fields. On any failure
    (network, parse, missing fields) returns a safe default derived from the regex intent.
    Never raises.
    """
    try:
        from revit_mcp.gemini_client import client
    except Exception as e:
        if log_fn:
            log_fn(f"[enrich_intent] gemini_client import failed: {e} — using fallback")
        return _fallback_enrichment(base_intent)

    import json as _json
    prompt = _ENRICH_PROMPT.format(
        user_prompt=user_prompt[:1000],
        regex_intent=_json.dumps(base_intent, indent=2),
    )

    try:
        raw = client.generate_content(prompt, thinking_budget=0, temperature=0.1)
    except Exception as e:
        if log_fn:
            log_fn(f"[enrich_intent] Gemini call FAILED: {e} — using fallback")
        return _fallback_enrichment(base_intent)

    # Strip markdown fences if Gemini wrapped the JSON
    text = raw.strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            if text.startswith("json"):
                text = text[4:]
    text = text.strip()

    try:
        enriched = _json.loads(text)
    except Exception as e:
        if log_fn:
            log_fn(f"[enrich_intent] JSON parse FAILED: {e} | raw={raw[:200]!r} — using fallback")
        return _fallback_enrichment(base_intent)

    # Validate required fields — fall back if any are missing
    required = {"purpose_group", "occupancy_class", "height_band", "sprinklered", "mixed_use", "exclude_pg", "scope_summary"}
    if not required.issubset(enriched.keys()):
        missing = required - enriched.keys()
        if log_fn:
            log_fn(f"[enrich_intent] response missing fields {missing} — using fallback")
        return _fallback_enrichment(base_intent)

    result = {**base_intent, **enriched, "_enrichment_source": "gemini"}
    if log_fn:
        log_fn(f"[enrich_intent] OK — {result['scope_summary']}")
    return result
