# -*- coding: utf-8 -*-
"""External-agent registry + dispatch helper.

Lets Agent A's chat forward requests to external pyRevit agents (Agent D today,
others later) that expose a small JSON-over-HTTP endpoint. New agents are added
purely by appending to `external_agents.json` — no code changes here.

Public surface:
    load_registry()              -> dict (cached, hot-reloads if file mtime changes)
    registry_prompt_block()      -> str  (injected into the classifier prompt)
    dispatch(classified, tracker) -> str (called by dispatcher when intent == "external_agent")
"""
import json
import os
import re
import time

try:
    import httpx
except ImportError:
    # Mirror gemini_client.py — pyRevit may import us before script.py extends sys.path
    import sys
    _cur = os.path.dirname(os.path.abspath(__file__))
    _lib = os.path.join(os.path.dirname(_cur), "lib")
    if _lib not in sys.path:
        sys.path.append(_lib)
    import httpx

from revit_mcp.cancel_manager import is_cancelled
from revit_mcp.gemini_client import client as _gemini_client


_REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "external_agents.json")
_registry_cache = {"mtime": 0.0, "data": None}


def _log(msg):
    try:
        _gemini_client.log("[external_agents] " + msg)
    except Exception:
        pass


def load_registry():
    """Read external_agents.json. Hot-reloads when the file changes on disk."""
    try:
        mtime = os.path.getmtime(_REGISTRY_PATH)
    except OSError:
        _log("registry file missing at {}".format(_REGISTRY_PATH))
        return {"agents": []}

    if _registry_cache["data"] is None or mtime != _registry_cache["mtime"]:
        try:
            with open(_REGISTRY_PATH, "r", encoding="utf-8") as f:
                _registry_cache["data"] = json.load(f)
            _registry_cache["mtime"] = mtime
            agents = _registry_cache["data"].get("agents", [])
            _log("loaded registry: {} agent(s) — {}".format(
                len(agents), ", ".join(a.get("name", "?") for a in agents)
            ))
        except Exception as e:
            _log("failed to parse registry: {}".format(e))
            return {"agents": []}

    return _registry_cache["data"]


def get_agent(name):
    for agent in load_registry().get("agents", []):
        if agent.get("name") == name:
            return agent
    return None


def _resolve_url(agent):
    """Apply a runtime port override to the registry URL when the agent has a
    `port_file` entry pointing at a file that exists on disk.

    Agent D writes %TEMP%/agentd_bridge.port containing the port its
    HttpListener landed on (8101 by default; falls back through 8110 if busy).
    This lets us follow the bridge to whichever port it actually grabbed,
    instead of hard-failing on 8101.

    Returns the (possibly rewritten) URL string. Never raises.
    """
    url = agent.get("url", "")
    port_file = agent.get("port_file", "")
    if not url or not port_file:
        return url

    # Expand %TEMP% / $TEMP / ~ so the registry can stay portable
    path = os.path.expandvars(os.path.expanduser(port_file))
    if not os.path.isfile(path):
        return url

    try:
        with open(path, "r") as f:
            raw = f.read().strip()
    except Exception:
        return url

    try:
        port = int(raw)
    except (TypeError, ValueError):
        return url

    if port <= 0 or port > 65535:
        return url

    # Swap the port component in the URL. Tolerant of URLs with or without an
    # explicit port already present.
    new_url, n = re.subn(r"(?P<scheme>https?://[^/:]+)(?::\d+)?",
                        r"\g<scheme>:" + str(port), url, count=1)
    if n == 0:
        return url
    if new_url != url:
        _log("{}: port file -> overriding URL port to {} ({})".format(
            agent.get("name", "?"), port, new_url))
    return new_url


def health_check(name, timeout=0.5):
    """Probe an external agent's URL. Returns True iff the TCP connect succeeds
    and a response (any status code) comes back within `timeout` seconds.

    Used to detect whether the agent's bridge is already listening so the chat
    can nag the user with a helpful hint if it isn't. Never raises.
    """
    agent = get_agent(name)
    if agent is None:
        return False
    url = _resolve_url(agent)
    if not url:
        return False
    try:
        # HEAD avoids triggering any actual action even if the endpoint forgets
        # to gate by method; if the server doesn't support HEAD it'll 405, which
        # still means it's alive.
        with httpx.Client(timeout=timeout) as c:
            c.request("HEAD", url)
        return True
    except Exception:
        return False


def startup_report(tracker_callback=None):
    """Run a health check on every registered agent. Returns a list of
    {"name", "display_name", "ok"} dicts. Safe to call from any thread.

    `tracker_callback`: optional one-arg callable invoked with each agent's
    status line — useful for logging during chat-window startup.
    """
    results = []
    for agent in load_registry().get("agents", []):
        name = agent.get("name", "?")
        display = agent.get("display_name", name)
        ok = health_check(name)
        results.append({"name": name, "display_name": display, "ok": ok})
        if tracker_callback:
            try:
                tracker_callback("{}: {}".format(display, "online" if ok else "offline"))
            except Exception:
                pass
    return results


def registry_prompt_block():
    """Build the EXTERNAL AGENTS block that's injected into the classifier prompt.

    Kept short so it doesn't bloat the prompt; only names + descriptions + actions.
    """
    agents = load_registry().get("agents", [])
    if not agents:
        return ""

    lines = ["EXTERNAL AGENTS (delegate via intent 'external_agent'):"]
    for a in agents:
        name = a.get("name", "?")
        desc = a.get("description", "").strip()
        lines.append("- {}: {}".format(name, desc))
        for act in a.get("actions", []):
            lines.append("    action '{}' — {}".format(act.get("name"), act.get("description", "")))
    return "\n".join(lines)


def dispatch(classified, tracker=None):
    """Forward a classified `external_agent` intent to its HTTP endpoint.

    classified is the intent dict from gemini_client.classify_intent, expected
    to contain at minimum:
        {"intent": "external_agent",
         "agent_name": "agent_d",
         "agent_action": "fill_data",
         "schedule_name": "...",
         "parameter_name": "..."}

    Returns a markdown string suitable for the chat window.
    """
    if classified is None:
        return "I couldn't work out which external agent to call — please rephrase."

    agent_name = classified.get("agent_name") or ""
    action = classified.get("agent_action") or ""
    agent = get_agent(agent_name)

    if agent is None:
        known = [a.get("name") for a in load_registry().get("agents", [])]
        return ("I don't have an external agent named **{}**. Known agents: {}."
                .format(agent_name or "(none)", ", ".join(known) if known else "(none registered)"))

    actions = [a.get("name") for a in agent.get("actions", [])]
    if action not in actions:
        return ("Agent **{}** doesn't support action **{}**. Available actions: {}."
                .format(agent_name, action or "(none)", ", ".join(actions)))

    payload = {"action": action}
    for k, v in classified.items():
        if k in ("intent", "agent_name", "agent_action", "goal", "detail_level", "tone"):
            continue
        if v is None or v == "":
            continue
        payload[k] = v

    url = _resolve_url(agent)
    timeout = float(agent.get("timeout_seconds", 600))
    display = agent.get("display_name", agent_name)

    if tracker:
        tracker.set_status("Routing to {} → {}...".format(display, action))

    _log("dispatching {}.{} payload={}".format(agent_name, action, json.dumps(payload)[:200]))

    started = time.time()
    try:
        with httpx.stream("POST", url, json=payload, timeout=timeout) as resp:
            resp.raise_for_status()
            content_type = (resp.headers.get("content-type") or "").lower()

            if "text/event-stream" in content_type:
                return _consume_stream(resp, tracker, agent_name, display, action)

            body = resp.read().decode("utf-8", errors="replace")
            _log("{}.{} returned {} bytes in {:.1f}s".format(
                agent_name, action, len(body), time.time() - started))
            try:
                payload_out = json.loads(body)
            except Exception:
                return "**{} → {}** returned a non-JSON response:\n\n```\n{}\n```".format(
                    display, action, body[:1500])
            return _format_result(display, action, payload_out)

    except httpx.ConnectError:
        return ("I couldn't reach **{}** at {}.\n\n"
                "Click the **Start Bridge** button under the {} ribbon tab to start it, "
                "then try again. (If auto-start is configured, restart Revit.)"
                .format(display, url, display))
    except httpx.TimeoutException:
        return "**{} → {}** timed out after {:.0f}s.".format(display, action, timeout)
    except httpx.HTTPStatusError as e:
        return "**{} → {}** returned HTTP {}: {}".format(display, action, e.response.status_code, e.response.text[:500])
    except Exception as e:
        _log("dispatch crashed: {}: {}".format(type(e).__name__, e))
        return "**{} → {}** failed: {}: {}".format(display, action, type(e).__name__, e)


def _consume_stream(resp, tracker, agent_name, display, action):
    """Read SSE 'data: <json>' lines until a {"type":"result"} arrives."""
    final_payload = None
    buffer = ""
    for raw_chunk in resp.iter_text():
        if is_cancelled():
            _log("{}.{} cancelled mid-stream".format(agent_name, action))
            return "Cancelled."
        if not raw_chunk:
            continue
        buffer += raw_chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r").strip()
            if not line or not line.startswith("data:"):
                continue
            try:
                msg = json.loads(line[5:].strip())
            except Exception:
                continue
            mtype = msg.get("type")
            if mtype == "status" and tracker:
                tracker.set_status(msg.get("text", ""))
            elif mtype == "result":
                final_payload = msg.get("payload")

    if final_payload is None:
        return "**{} → {}** ended without a result payload.".format(display, action)
    return _format_result(display, action, final_payload)


def _format_result(display, action, payload):
    """Render Agent D's JSON result as markdown for the chat window.

    Accepts the shape documented in integration_AgentD.md §5 — falls back to a
    pretty-printed JSON dump when the shape isn't recognised.
    """
    status = (payload or {}).get("status", "unknown")

    if status == "error":
        reason = payload.get("reason", "unknown_error")
        message = payload.get("message", "")
        return "**{} → {} failed** ({})\n\n{}".format(display, action, reason, message)

    lines = ["**{} → {}** — {}".format(display, action, status)]

    stats = payload.get("statistics")
    if isinstance(stats, dict) and stats:
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        for k, v in stats.items():
            if isinstance(v, (str, int, float)):
                lines.append("| {} | {} |".format(k.replace("_", " ").capitalize(), v))

    target = payload.get("target_parameter") or payload.get("parameter")
    if target:
        lines.append("\n_Parameter:_ `{}`".format(target))

    insights = payload.get("ai_sanity_check_insights")
    if isinstance(insights, list) and insights:
        lines.append("\n**AI sanity check:**")
        for item in insights:
            lines.append("- {}".format(item))

    if len(lines) == 1:
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(payload, indent=2)[:2000])
        lines.append("```")

    return "\n".join(lines)
