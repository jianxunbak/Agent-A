"""
Standalone test for the Vertex AI Ranking API.

Goal: reproduce the chunks returned by the failing 'show me table 2.2a from scdf' query,
pass them through the Ranking API, and verify that the actual Table 2.2A chunk
(currently buried) gets reranked to the top.

Run from project root:
    py test_ranking_api.py

Reuses the existing service-account credentials and httpx client from the extension.
"""
import json
import os
import sys
import time

# Make the extension's revit_mcp importable so we can reuse credentials + httpx
EXT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "GeminiMCP.extension",
)
LIB_PATH = os.path.join(EXT_PATH, "lib")
sys.path.insert(0, LIB_PATH)
sys.path.insert(0, EXT_PATH)

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(EXT_PATH, ".env"))
except ImportError:
    pass

from revit_mcp.rag.vertex_rag import _get_access_token  # noqa: E402

PROJECT  = os.environ.get("GOOGLE_CLOUD_PROJECT")
LOCATION = "global"  # Ranking API is global, not regional

# Test query — the one that failed in your log
QUERY = "show me table 2.2a from scdf"

# The 4 chunks that came back from Vertex for that query (per your STEP 7 log).
# We'll grab their full content from the chunk cache where possible, and add the
# CORRECT chunk (the actual Table 2.2A body, currently in the cache under
# 'travel_distance' index 1) to verify the ranker can identify it.
CACHE_PATH = os.path.expanduser(
    r"~\AppData\Roaming\RevitMCP\cache\chunk_cache.json"
)


def load_cache():
    with open(CACHE_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def first_n(text, n):
    return text[:n].replace("\n", " | ")


def build_test_chunks():
    """
    Build a candidate pool of chunks: the 4 wrong ones from the failing query,
    plus the correct Table 2.2A chunk. If reranker works, the correct one wins.
    """
    cache = load_cache()
    scdf = cache["SCDF"]

    candidates = []

    # The CORRECT chunk — this is what should win
    correct = scdf["travel_distance"][1]  # # Two-way escape arrangement (3.4 KB)
    candidates.append({
        "id": "correct_table_2_2a",
        "content": correct["content"],
        "_first_line": first_n(correct["content"], 80),
    })

    # Wrong chunks: pull a few that we KNOW match the wrong-chunks pattern
    # (from your STEP 7 log: TABLE 1.4B, TABLE 2.3.9k, CLAUSE 6.1, TABLE 1.4B again)
    # Pull from various topics to simulate the failing query's harvested chunks.
    wrongs = [
        ("staircase",       0),  # Likely a clause referencing 2.2A
        ("staircase",       2),  # Another clause
        ("occupant_load",   2),  # TABLE 1.4B
        ("exit_width",      0),  # cites 2.2A
        ("exit_width",      1),  # cites 2.2A
        ("smoke_stop_lobby", 0),
        ("fire_lift_lobby", 0),
    ]
    for topic, idx in wrongs:
        if idx < len(scdf.get(topic, [])):
            ch = scdf[topic][idx]
            candidates.append({
                "id": f"wrong_{topic}_{idx}",
                "content": ch["content"],
                "_first_line": first_n(ch["content"], 80),
            })

    return candidates


def call_ranking_api(query, candidates, model="semantic-ranker-default-004"):
    """
    POST to Vertex AI Ranking API.
    Endpoint:
        https://discoveryengine.googleapis.com/v1/projects/{p}/locations/global
            /rankingConfigs/default_ranking_config:rank
    """
    import httpx

    token = _get_access_token()
    if not token:
        raise RuntimeError("No access token — credentials missing")

    url = (
        f"https://discoveryengine.googleapis.com/v1/projects/{PROJECT}"
        f"/locations/global/rankingConfigs/default_ranking_config:rank"
    )

    # Records: each must have id + (title and/or content). We pass content; some
    # chunks are 11 KB which is fine — Ranking API accepts up to ~512 tokens
    # per record (longer text gets truncated by the model).
    records = [
        {"id": c["id"], "content": c["content"][:8000]}
        for c in candidates
    ]

    body = {
        "model": model,
        "query": query,
        "records": records,
        # ignoreRecordDetailsInResponse=False so we get the records back ranked
        "ignoreRecordDetailsInResponse": False,
    }

    print(f"[ranking] calling Ranking API: model={model} records={len(records)} query={query!r}")
    t0 = time.time()
    resp = httpx.post(
        url,
        json=body,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        timeout=30.0,
    )
    dur = time.time() - t0
    print(f"[ranking] HTTP {resp.status_code} in {dur:.2f}s")
    if resp.status_code >= 400:
        print(f"[ranking] error body: {resp.text[:1500]}")
        return None
    return resp.json()


def main():
    print("=" * 70)
    print(f"PROJECT: {PROJECT}")
    print(f"QUERY:   {QUERY!r}")
    print("=" * 70)

    candidates = build_test_chunks()
    print(f"\n[setup] Built {len(candidates)} candidate chunks:")
    for c in candidates:
        marker = "[CORRECT]" if c["id"] == "correct_table_2_2a" else "  wrong"
        print(f"  {marker:12s} {c['id']:35s} | {c['_first_line']}")

    print("\n[setup] Calling Ranking API…")
    result = call_ranking_api(QUERY, candidates)
    if result is None:
        print("\n*** Ranking API call FAILED — see error above. ***")
        return 1

    records = result.get("records", [])
    print(f"\n[result] {len(records)} reranked records (highest score first):\n")
    for rank, r in enumerate(records, 1):
        rid = r.get("id", "?")
        score = r.get("score", "?")
        marker = "[CORRECT]" if rid == "correct_table_2_2a" else "  wrong"
        first = first_n(r.get("content", ""), 70)
        print(f"  rank {rank:2d} | score {score:.4f} | {marker:12s} | {rid:35s} | {first}")

    # Did the correct chunk land at rank 1?
    if records and records[0].get("id") == "correct_table_2_2a":
        print("\n*** PASS: Ranking API correctly surfaced the Table 2.2A chunk to rank 1. ***")
        return 0
    else:
        print("\n*** FAIL: Correct chunk did NOT land at rank 1. Reranker isn't enough on its own. ***")
        return 2


if __name__ == "__main__":
    sys.exit(main())
