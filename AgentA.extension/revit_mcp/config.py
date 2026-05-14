import os

# Attempt to load from .env file (same pattern as gemini_client.py)
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
    load_dotenv(_env_path)
except Exception:
    pass

GOOGLE_CLOUD_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "your-project-id")
GOOGLE_CLOUD_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us")
VERTEX_DATASTORE_ID = os.environ.get("VERTEX_DATASTORE_ID", "scdf-fire-codes-poc")

# Full Vertex AI Search serving config path
VERTEX_SERVING_CONFIG = (
    f"projects/{GOOGLE_CLOUD_PROJECT}/locations/{GOOGLE_CLOUD_LOCATION}"
    f"/collections/default_collection/dataStores/{VERTEX_DATASTORE_ID}"
    f"/servingConfigs/default_config"
)

# Set to False to skip RAG and use static compliance rules only
_rag_env = os.environ.get("RAG_ENABLED", "false").strip().lower()
RAG_ENABLED = _rag_env in ("1", "true", "yes")
