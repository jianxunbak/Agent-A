"""Vertex AI Search via pure httpx REST — zero google-cloud-discoveryengine import.

Previous approach tried to import google.cloud.discoveryengine_v1beta, which
chains into grpc._cython.cygrpc and google._upb._message — both native DLLs that
deadlock indefinitely inside Revit's COM/STA environment.

This implementation calls the Discovery Engine REST API directly with httpx
(already used for all Gemini calls) so there are no C-extension imports and
no startup hang.

Authentication (tried in order):
  1. GOOGLE_ACCESS_TOKEN env var — paste a short-lived token from:
         gcloud auth print-access-token
  2. Service-account JSON key pointed to by GOOGLE_APPLICATION_CREDENTIALS.
     Signs a JWT with cryptography (bundled _rust.pyd) or pure-Python RSA
     fallback (pyasn1, also bundled) if the Rust ext is unavailable.

If neither credential source is present the function returns [] immediately
and Gemini synthesis falls back to its built-in compliance knowledge.
"""

from revit_mcp.config import VERTEX_SERVING_CONFIG

import json
import time
import threading

# ── token cache ───────────────────────────────────────────────────────────────
_token_lock   = threading.Lock()
_cached_token: str | None = None
_token_expiry: float = 0.0


def _log(msg: str) -> None:
    try:
        from revit_mcp.gemini_client import client
        client.log(f"[RAG:vertex] {msg}")
    except Exception:
        pass


# ── _ensure_imported ──────────────────────────────────────────────────────────
# runner.py calls this in a warmup thread.  With the REST implementation there
# is nothing to import, so we return True instantly — no 30-second hang.

def _ensure_imported(_timeout: float = 8.0) -> bool:
    """
    Called at startup in a warmup thread.
    Pre-fetches the OAuth2 access token so it is cached before the first
    real RAG query — eliminates the ~70s cold-start delay at query time.
    """
    _log("REST backend ready (no python-client import needed)")
    _log("[Warmup] Pre-fetching OAuth2 access token...")
    t0 = time.time()
    token = _get_access_token()
    dur = time.time() - t0
    if token:
        _log(f"[Warmup] Token pre-fetch PASS — cached in {dur:.2f}s (valid ~1h)")
    else:
        _log(f"[Warmup] Token pre-fetch FAIL in {dur:.2f}s — will retry at first query")
    return bool(token)


# ── auth helpers ──────────────────────────────────────────────────────────────

def _sign_jwt_cryptography(sa: dict) -> str | None:
    """RS256-sign a service-account JWT using the bundled cryptography library."""
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives import hashes
        import base64

        def _b64url(data: bytes | dict) -> str:
            if isinstance(data, dict):
                data = json.dumps(data, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

        now = int(time.time())
        header  = {"alg": "RS256", "typ": "JWT"}
        payload = {
            "iss":   sa["client_email"],
            "scope": "https://www.googleapis.com/auth/cloud-platform",
            "aud":   "https://oauth2.googleapis.com/token",
            "exp":   now + 3600,
            "iat":   now,
        }
        signing_input = f"{_b64url(header)}.{_b64url(payload)}".encode()
        key = load_pem_private_key(sa["private_key"].encode(), password=None)
        sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input.decode()}.{_b64url(sig)}"
    except Exception as exc:
        _log(f"cryptography JWT sign failed: {exc}")
        return None


def _sign_jwt_pure_python(sa: dict) -> str | None:
    """RS256-sign a service-account JWT using pyasn1 (pure Python, no C ext)."""
    try:
        import base64
        import hashlib
        from pyasn1.codec.der import decoder as _der_decoder
        from pyasn1.type import univ as _univ

        # ── Parse PEM private key (PKCS#8 or PKCS#1) ──────────────────────────
        pem = sa["private_key"].encode()
        b64_body = b"".join(
            line for line in pem.splitlines()
            if line and not line.startswith(b"-----")
        )
        der = base64.b64decode(b64_body)

        key_seq, _ = _der_decoder.decode(der, asn1Spec=_univ.Sequence())
        first_tag = int(key_seq[0])

        if first_tag == 0:
            # PKCS#8: outer SEQUENCE has version=0, algorithm OID, OCTET STRING
            inner_der = bytes(key_seq[2])
            rsa_seq, _ = _der_decoder.decode(inner_der, asn1Spec=_univ.Sequence())
        else:
            # PKCS#1: the sequence IS the RSAPrivateKey
            rsa_seq = key_seq

        n = int(rsa_seq[1])   # modulus
        d = int(rsa_seq[3])   # private exponent
        key_len = (n.bit_length() + 7) // 8

        # ── Build DigestInfo for SHA-256 ───────────────────────────────────────
        SHA256_DIGEST_INFO_PREFIX = bytes.fromhex(
            "3031300d060960864801650304020105000420"
        )

        def _pkcs1v15_sign(msg: bytes) -> bytes:
            digest = hashlib.sha256(msg).digest()
            em_body = SHA256_DIGEST_INFO_PREFIX + digest
            pad_len = key_len - len(em_body) - 3
            if pad_len < 8:
                raise ValueError("RSA key too small for PKCS#1 v1.5")
            em = b"\x00\x01" + b"\xff" * pad_len + b"\x00" + em_body
            m  = int.from_bytes(em, "big")
            s  = pow(m, d, n)
            return s.to_bytes(key_len, "big")

        def _b64url(data: bytes | dict) -> str:
            if isinstance(data, dict):
                data = json.dumps(data, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

        now = int(time.time())
        header  = {"alg": "RS256", "typ": "JWT"}
        payload = {
            "iss":   sa["client_email"],
            "scope": "https://www.googleapis.com/auth/cloud-platform",
            "aud":   "https://oauth2.googleapis.com/token",
            "exp":   now + 3600,
            "iat":   now,
        }
        signing_input = f"{_b64url(header)}.{_b64url(payload)}".encode()
        sig = _pkcs1v15_sign(signing_input)
        return f"{signing_input.decode()}.{_b64url(sig)}"
    except Exception as exc:
        _log(f"pure-python JWT sign failed: {exc}")
        return None


def _get_access_token() -> str | None:
    """Return a valid Bearer token or None if credentials are unavailable."""
    import os
    global _cached_token, _token_expiry

    # 1. Manual short-lived token (gcloud auth print-access-token)
    override = os.environ.get("GOOGLE_ACCESS_TOKEN", "").strip()
    if override:
        return override

    # 2. Cache
    with _token_lock:
        if _cached_token and time.time() < _token_expiry - 60:
            return _cached_token

    # 3. Service account key — GOOGLE_APPLICATION_CREDENTIALS env var, or
    #    service-account.json sitting next to .env in the extension directory.
    key_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not key_path or not os.path.isfile(key_path):
        _bundled = os.path.join(
            os.path.dirname(__file__),   # revit_mcp/rag/
            "..", "..",                  # → GeminiMCP.extension/
            "service-account.json",
        )
        _bundled = os.path.normpath(_bundled)
        if os.path.isfile(_bundled):
            key_path = _bundled
            _log(f"using bundled service-account.json: {_bundled}")
        else:
            return None

    try:
        with open(key_path, "r", encoding="utf-8") as fh:
            sa = json.load(fh)
    except Exception as exc:
        _log(f"cannot read service account key: {exc}")
        return None

    if sa.get("type") != "service_account":
        _log(f"unsupported credential type: {sa.get('type')}")
        return None

    if "YOUR_PROJECT_ID" in sa.get("project_id", ""):
        _log("service-account.json still has placeholder values — fill it in to enable Vertex RAG")
        return None

    # Try cryptography first, fall back to pure Python
    jwt_token = _sign_jwt_cryptography(sa) or _sign_jwt_pure_python(sa)
    if not jwt_token:
        _log("JWT signing failed — no access token available")
        return None

    try:
        import httpx
        resp = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": jwt_token,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        td = resp.json()
        token = td["access_token"]
        now   = int(time.time())
        with _token_lock:
            _cached_token = token
            _token_expiry = now + td.get("expires_in", 3600)
        _log("access token obtained OK")
        return token
    except Exception as exc:
        _log(f"token exchange failed: {exc}")
        return None


# ── main RAG function ─────────────────────────────────────────────────────────

import re as _re

def _extract_clause_refs(text: str) -> list:
    """Extract clause/section reference numbers from verbatim document text.

    Looks for patterns like "3.2.1", "Clause 4.5", "Section 2.3.1" that appear
    in SCDF fire code documents. Returns a deduplicated list preserving order.
    """
    patterns = [
        r'[Cc]lause\s+([\d]+\.[\d]+(?:\.[\d]+)*)',   # "Clause 3.2.1"
        r'[Ss]ection\s+([\d]+\.[\d]+(?:\.[\d]+)*)',  # "Section 4.5"
        r'\b([\d]+\.[\d]+\.[\d]+)\b',                 # "3.2.1" (3-level only to avoid false positives)
    ]
    refs = []
    for pat in patterns:
        for m in _re.finditer(pat, text):
            refs.append(m.group(1))
    seen = set()
    return [r for r in refs if not (r in seen or seen.add(r))]


def _parse_results(data: dict) -> list:
    """Extract result chunks from a Discovery Engine response dict.

    Supports two data store shapes:
      1. Structured (JSONL with structData fields: content, source, clause_number, ...)
         — preferred when document.structData has a 'content' field.
      2. Unstructured PDFs (derivedStructData with extractive segments) — fallback.

    Each chunk includes:
      - 'source_uri' (structData.source for structured, derivedStructData.link for PDFs)
      - 'metadata.clause_refs' list pre-extracted from content text via regex,
         with structData.clause_number prepended when present.
    """
    results = []
    for item in data.get("results", []):
        doc        = item.get("document", {})
        struct     = doc.get("structData", {}) or {}
        derived    = doc.get("derivedStructData", {}) or {}

        # ── Structured data store path (JSONL with structData.content) ────────
        if struct.get("content"):
            content       = struct.get("content", "")
            title         = struct.get("title", "")
            source        = struct.get("source", "")
            chapter       = struct.get("chapter", "")
            clause_number = struct.get("clause_number") or ""
            sub_clause    = struct.get("sub_clause") or ""

            clause_refs = _extract_clause_refs(content)
            if clause_number and clause_number not in clause_refs:
                clause_refs.insert(0, clause_number)

            clause_label = f"Clause {clause_number}" if clause_number else title
            if sub_clause:
                clause_label = f"{clause_label} ({sub_clause})"

            results.append({
                "content":    content,
                "source_uri": source,
                "metadata": {
                    "title":       title,
                    "page":        "",
                    "clause":      clause_label,
                    "clause_refs": clause_refs,
                    "chapter":     chapter,
                },
            })
            continue

        # ── Unstructured PDF path (extractive segments) ───────────────────────
        title      = derived.get("title", "")
        source_uri = derived.get("link", "")

        # Prefer extractive segments (longer), fall back to extractive answers
        segments = []
        for field in ("extractive_segments", "extractiveSegments"):
            raw = derived.get(field)
            if raw:
                segments = raw
                break
        if not segments:
            for field in ("extractive_answers", "extractiveAnswers"):
                raw = derived.get(field)
                if raw:
                    segments = raw
                    break

        # Collect snippets as supplementary context
        snippets = []
        for field in ("snippets",):
            raw = derived.get(field)
            if raw:
                snippets = [s.get("snippet", "") for s in raw if s.get("snippet")]
                break

        if segments:
            for seg in segments:
                page = seg.get("pageNumber") or seg.get("page_number", "")

                # Assemble: previous context + main segment + next context
                prev_segs = seg.get("previous_segments") or seg.get("previousSegments") or []
                next_segs = seg.get("next_segments") or seg.get("nextSegments") or []
                parts = (
                    [p.get("content", "") for p in prev_segs if p.get("content")]
                    + [seg.get("content", "")]
                    + [n.get("content", "") for n in next_segs if n.get("content")]
                )
                content = "\n\n".join(p for p in parts if p)
                if snippets:
                    content += "\n\n" + "\n".join(snippets)

                if content:
                    clause_refs = _extract_clause_refs(content)
                    results.append({
                        "content":    content,
                        "source_uri": source_uri,
                        "metadata": {
                            "title":       title,
                            "page":        page,
                            "clause":      f"{title} p.{page}" if page else title,
                            "clause_refs": clause_refs,
                        },
                    })
        else:
            # Fallback: structData content field
            content = doc.get("structData", {}).get("content", "")
            if snippets:
                content += "\n\n" + "\n".join(snippets)
            if content:
                clause_refs = _extract_clause_refs(content)
                results.append({
                    "content":    content,
                    "source_uri": source_uri,
                    "metadata": {
                        "title":       title,
                        "page":        "",
                        "clause":      title,
                        "clause_refs": clause_refs,
                    },
                })
    return results


def _do_search(httpx_mod, url: str, body: dict, token: str) -> tuple:
    """
    Fire a single Discovery Engine POST.
    Returns (response_obj, duration_s) or raises on network error.
    Polls the cancel flag every 0.5s while the HTTP request is in-flight so
    that clicking Stop aborts the network wait immediately.
    """
    import threading as _threading
    from revit_mcp.cancel_manager import is_cancelled
    if is_cancelled():
        raise RuntimeError("Build cancelled by user.")
    t0 = time.time()
    _result = [None]
    _error  = [None]
    _done   = _threading.Event()

    def _http():
        try:
            _result[0] = httpx_mod.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30.0,
            )
        except Exception as e:
            _error[0] = e
        finally:
            _done.set()

    _threading.Thread(target=_http, daemon=True).start()

    while not _done.wait(0.5):
        if is_cancelled():
            raise RuntimeError("Build cancelled by user.")

    if is_cancelled():
        raise RuntimeError("Build cancelled by user.")
    if _error[0]:
        raise _error[0]
    return _result[0], time.time() - t0


def query_vertex_rag(
    query: str,
    topic_filter: str | None = None,
    top_k: int = 5,
    source_filter: str | None = None,
) -> list:
    """
    Query Vertex AI Search with a two-attempt strategy.

    source_filter: GCS path substring (e.g. "knowledge_base/SCDF") used to
        restrict results to one authority folder.  Vertex AI Search does NOT
        support URI-based filter expressions, so this is enforced as a
        post-filter on derivedStructData.link after each HTTP response.
        Any new PDF uploaded under that folder is automatically included —
        no document-ID maintenance needed.

    Attempt 1 — metadata.topic filtered request, then source post-filtered.
    Attempt 2 — unfiltered request (broader recall), then source post-filtered.
        Source post-filter is always applied when source_filter is given, so
        results from other authorities never reach the caller.
    """
    import httpx
    _log(f"query_vertex_rag START — query='{query[:80]}' topic={topic_filter} source={source_filter}")
    t0 = time.time()

    token = _get_access_token()
    if not token:
        _log("no credentials — returning [] (Gemini synthesis will use built-in knowledge)")
        return []

    # Regional endpoint: us-region datastores require us-discoveryengine.googleapis.com
    from revit_mcp.config import GOOGLE_CLOUD_LOCATION
    _host = "discoveryengine.googleapis.com" if GOOGLE_CLOUD_LOCATION == "global" else f"{GOOGLE_CLOUD_LOCATION}-discoveryengine.googleapis.com"
    url = f"https://{_host}/v1beta/{VERTEX_SERVING_CONFIG}:search"
    # Ask for more results than needed so post-filtering still yields top_k hits.
    # The datastore has ~15 docs; with 1 SCDF doc per topic query, fetch the max
    # (100) when source filtering to ensure we get all SCDF chunks available.
    fetch_size = 100 if source_filter else top_k
    base_body: dict = {
        "query":    query,
        "pageSize": fetch_size,
        "queryExpansionSpec":  {"condition": "AUTO"},
        "contentSearchSpec": {
            "extractiveContentSpec": {
                "maxExtractiveSegmentCount": 10,
                "returnExtractiveSegmentScore": True,
                # Fetch 2 chunks before/after each segment so tables and figures
                # that span page boundaries are retrieved intact
                "numPreviousSegments": 2,
                "numNextSegments": 2,
            },
            "snippetSpec": {
                "returnSnippet": True,
                "maxSnippetCount": 3,
            },
        },
    }

    def _apply_source_filter(results: list) -> list:
        """Keep only chunks whose source_uri contains source_filter substring."""
        if not source_filter:
            return results
        kept = [r for r in results if source_filter in r.get("source_uri", "")]
        dropped = len(results) - len(kept)
        if dropped:
            _log(f"[post-filter] kept {len(kept)}/{len(results)} — dropped {dropped} from other authorities")
        return kept[:top_k]

    # ── Single query: unfiltered request + source post-filter ─────────────────
    # metadata.topic is not indexed in this datastore (returns HTTP 400), so we
    # skip the filtered attempt entirely and rely on natural-language query
    # relevance + post-filtering by source_uri to scope results to the correct
    # authority folder.
    _log(f"[Query] firing: '{query[:100]}'")
    t_query = time.time()
    try:
        resp, dur = _do_search(httpx, url, base_body, token)
        _log(f"[Query] HTTP {resp.status_code} in {dur:.2f}s")
        if resp.status_code < 400:
            results = _apply_source_filter(_parse_results(resp.json()))
            if results:
                _log(f"[Query] PASS — {len(results)} results in {time.time()-t_query:.2f}s")
            else:
                _log(f"[Query] 0 results after post-filter (no matching SCDF chunks)")
            _log(f"query_vertex_rag DONE — {len(results)} results in {time.time()-t0:.2f}s")
            return results
        else:
            _log(f"[Query] FAIL — HTTP {resp.status_code}: {resp.text[:300]}")
    except Exception as exc:
        _log(f"[Query] FAIL — network error: {exc}")

    _log(f"query_vertex_rag DONE — 0 results in {time.time()-t0:.2f}s")
    return []
