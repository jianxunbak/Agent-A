"""
Standalone RAG test — run from project root:
  python test_rag.py
"""
import sys
import os

EXT_DIR = os.path.join(os.path.dirname(__file__), "..", "GeminiMCP.extension")

# Add extension root and lib/ (where google-cloud-discoveryengine is vendored)
sys.path.insert(0, EXT_DIR)
sys.path.insert(0, os.path.join(EXT_DIR, "lib"))

# Manually load .env since dotenv may not be available in this Python
_env_path = os.path.join(EXT_DIR, ".env")
with open(_env_path) as _f:
    for line in _f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# Now import after env is loaded
GOOGLE_CLOUD_PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
VERTEX_DATASTORE_ID  = os.environ["VERTEX_DATASTORE_ID"]
GOOGLE_CLOUD_LOCATION = "global"
VERTEX_SERVING_CONFIG = (
    f"projects/{GOOGLE_CLOUD_PROJECT}/locations/{GOOGLE_CLOUD_LOCATION}"
    f"/collections/default_collection/dataStores/{VERTEX_DATASTORE_ID}"
    f"/servingConfigs/default_config"
)

TOPIC_QUERIES = {
    "staircase":       "staircase minimum width count requirements fire escape pressurisation",
    "fire_lift":       "fire lift dimensions car size door width load capacity requirements",
    "fire_lift_lobby": "fire lift lobby minimum area dimensions pressurisation smoke barrier",
}
TEST_INTENT = {"building_type": "commercial_office", "storeys": 15}

print(f"Project       : {GOOGLE_CLOUD_PROJECT}")
print(f"Datastore     : {VERTEX_DATASTORE_ID}")
print(f"Serving config: {VERTEX_SERVING_CONFIG}")
print("-" * 60)

try:
    from google.cloud import discoveryengine_v1beta as discoveryengine
    print("google-cloud-discoveryengine: OK\n")
except ImportError as e:
    print(f"IMPORT ERROR: {e}")
    print("Make sure google-cloud-discoveryengine is vendored into lib/")
    sys.exit(1)

try:
    from google.protobuf.json_format import MessageToDict
except ImportError:
    # Fallback if protobuf not available
    def MessageToDict(s):
        return dict(s)

all_ok = True
search_client = discoveryengine.SearchServiceClient()

for topic, base_query in TOPIC_QUERIES.items():
    intent = TEST_INTENT
    query = f"{base_query} {intent['building_type']} {intent['storeys']} storey Singapore"
    print(f"[TOPIC] {topic.upper()}")
    print(f"  Query: {query}")
    try:
        request = discoveryengine.SearchRequest(
            serving_config=VERTEX_SERVING_CONFIG,
            query=query,
            page_size=3,
        )
        response = search_client.search(request)
        results = list(response.results)
        if not results:
            print("  No results returned.")
        else:
            print(f"  Got {len(results)} chunk(s):")
            for i, r in enumerate(results, 1):
                doc = r.document
                try:
                    data = MessageToDict(doc.struct_data) if doc.struct_data else {}
                except Exception:
                    data = {}
                content  = data.get("content", doc.name)[:120].replace("\n", " ")
                metadata = data.get("metadata", {})
                clause   = metadata.get("clause", "—")
                print(f"    [{i}] clause={clause} | {content}...")
    except Exception as e:
        print(f"  ERROR: {e}")
        all_ok = False
    print()

print("=" * 60)
print("RAG OK — datastore reachable." if all_ok else "FAILED — check errors above.")
