import os
import json
import threading
import time
import datetime

try:
    import httpx
except ImportError:
    # Fallback to sys.path if lib not yet recognized
    import sys
    _cur = os.path.dirname(os.path.abspath(__file__))
    lib = os.path.join(os.path.dirname(_cur), "lib")
    if lib not in sys.path: sys.path.append(lib)
    import httpx

from revit_mcp.tool_definitions import TOOL_DECLARATIONS

class GeminiClient:
    def __init__(self):
        self._load_config()
        self.lock = threading.Lock()
        self._init_session()
        self.log("GeminiClient: Persistent httpx session initialized.")

    def _init_session(self):
        """Initializes or resets the persistent httpx session."""
        if hasattr(self, "session") and self.session:
            try: self.session.close()
            except: pass
        self.session = httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=180.0, write=15.0, pool=5.0),
            headers={"User-Agent": "Revit-MCP-Httpx/3.0"},
            follow_redirects=True,
            verify=False
        )

    def _test_connectivity(self):
        """Minimal ping to verify internet access and API key validity."""
        try:
            self.log("DEBUG: Testing Google API connectivity...")
            url = "https://generativelanguage.googleapis.com/v1beta/models?key={}".format(self.api_key)
            with urllib.request.urlopen(url, timeout=10) as f:
                self.log("DEBUG: Google API Connectivity Check: SUCCESS (Model List Accessible)")
        except Exception as e:
            self.log("DEBUG: Google API Connectivity Check: FAILED - {}".format(str(e)))

    def _load_config(self):
        self.api_key = None
        self.model = "gemini-2.0-flash-exp" # High-performance model
        
        # Consistent path discovery (same level as GeminiMCP.extension/)
        # Using abspath for absolute reliability
        _cur_dir = os.path.dirname(os.path.abspath(__file__))
        env_path = os.path.join(os.path.dirname(_cur_dir), ".env")
        
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"): continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip() == "GEMINI_API_KEY": self.api_key = v.strip().strip('"').strip("'")
                        if k.strip() == "GEMINI_MODEL": self.model = v.strip().strip('"').strip("'")
        
        # Final safety check
        if not self.api_key:
            from dotenv import load_dotenv # type: ignore
            load_dotenv(env_path)
            self.api_key = os.getenv("GEMINI_API_KEY")
            
        if not self.api_key:
            print("GeminiClient: CRITICAL ERROR - GEMINI_API_KEY not found in " + env_path)

    def log(self, msg):
        import os.path as op
        import datetime
        from revit_mcp.utils import get_log_path
        log_path = get_log_path()
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_msg = "[{}] {}\n".format(timestamp, msg)
        
        try:
            with open(log_path, "a") as f:
                f.write(log_msg)
                f.flush()
        except: pass

    def get_tools(self):
        return TOOL_DECLARATIONS

    def execute_tool(self, name, args):
        import revit_mcp.server as server
        import traceback
        # Redundant log removed (already logged in chat loop)
        try:
            func = getattr(server, name)
            return func(**args)
        except Exception as e:
            tb = traceback.format_exc()
            self.log("Tool Execution Error: " + tb)
            return json.dumps({"error": str(e), "traceback": tb})

    def chat(self, prompt, history=None):
        """High-performance chat routing to Orchestrator."""
        self.log("--- GeminiClient: chat START ---")
        try:
            from revit_mcp.dispatcher import orchestrator
            # We need the uiapp - luckily we stored it in the bridge
            from revit_mcp.bridge import _uiapp
            if not _uiapp:
                from .runner import log as server_log
                server_log("GeminiClient: UIApp missing in bridge, cannot orchestrate.")
                return "Error: Revit connection lost. Please restart the MCP server."
            
            self.log("GeminiClient: Routing to Orchestrator...")
            res = orchestrator.run_full_stack(_uiapp, prompt, history=history)
            self.log("GeminiClient: Orchestration complete.")
            return res
        except Exception as e:
            import traceback
            err = "Chat Error: {}\n{}".format(str(e), traceback.format_exc())
            self.log(err)
            return err

    def classify_intent(self, user_prompt, conversation_context=""):
        """Classify user prompt into a structured intent, and also decide temperature
        and thinking_budget for the main build call.

        Uses a fast, no-thinking call (temp=0, minimal tokens).
        Returns a dict like:
            {"intent": "new_build", "temperature": 0.7, "thinking_budget": 16384}
            {"intent": "build",     "temperature": 0.4, "thinking_budget": 8192}
            {"intent": "query",     "temperature": 0.1, "thinking_budget": 8192}
            {"intent": "use_option", "option": 2}
            ...etc
        Returns None on failure (caller falls back to regex).
        """
        ctx_block = ""
        if conversation_context:
            ctx_block = (
                "Recent conversation (for context only — classify the LATEST user message below):\n"
                "{}\n\n"
            ).format(conversation_context)

        # Inject the external-agent registry so the classifier learns about new agents
        # purely from `external_agents.json` — no prompt edits needed for future agents.
        try:
            from revit_mcp.external_agents import registry_prompt_block
            _ext_agents_block = registry_prompt_block()
        except Exception as _e:
            self.log("classify_intent: external_agents registry unavailable — {}".format(_e))
            _ext_agents_block = ""
        if _ext_agents_block:
            _ext_agents_block = "\n" + _ext_agents_block + "\n"

        classification_prompt = ctx_block + _ext_agents_block + (
            "You are an intent classifier for a BIM (Building Information Modeling) assistant. "
            "The user message may request ONE OR MORE actions. "
            "Return ONLY a valid JSON object with an 'intents' array, where each element is one action. "
            "No explanation, no markdown, no code fences.\n\n"
            "INTENT LIST:\n"
            "- list_options: user wants to see/list saved design options or builds\n"
            "- use_option: user wants to switch to / apply / load a specific option number (NOT reorder)\n"
            "- rollback: user wants to revert to a specific option and optionally a revision\n"
            "- reorder_option: user wants to change the position/number of an option (e.g. 'make option 2 option 1', 'move option 3 to position 1')\n"
            "- delete_option: user wants to delete a whole option\n"
            "- delete_revision: user wants to delete a specific revision of an option\n"
            "- delete_all_options: user wants to delete all saved design options/memory (NOT model elements)\n"
            "- recreate_option: user wants to rebuild/regenerate a saved option\n"
            "- export_option: user wants to export an option's manifest as JSON\n"
            "- move_to_revision: user wants to move an option under another option as a revision\n"
            "- new_build: user wants to create/generate a new building\n"
            "- delete_elements: user wants to delete Revit model elements (walls, floors, columns, etc.) OR clear the entire model/building\n"
            "- clear_chat: user wants to clear/reset the chat history or conversation (e.g. 'clear the chat', 'clear chat', 'reset conversation', 'new conversation', 'start fresh chat')\n"
            "- query: user is asking a question about the current model state\n"
            "- authority_query: user is asking about building codes, regulations, or authority requirements (SCDF, URA, LTA, NEA, NPARKS, PUB, or any authority) — NOT about the current model state\n"
            "- clarify: the user message is too vague, ambiguous, or incomplete to act on — a clarifying question is needed before proceeding. Use this ONLY when the intent is genuinely unclear and cannot be reasonably inferred. Do NOT use this for normal building requests.\n"
            "- external_agent: delegate to one of the EXTERNAL AGENTS listed above. Use this when the user's request matches an external agent's description (e.g. auditing element data, filling missing parameter values across a Revit category). Include fields agent_name (one of the names in EXTERNAL AGENTS), agent_action (one of that agent's actions), plus any input fields listed for the agent (e.g. category_name, parameter_name). For agent_d, category_name is the Revit Category name (e.g. \"Walls\", \"Doors\", \"Windows\") — NOT a schedule name and NOT just \"category\".\n"
            "- build: user wants to modify/edit the existing building (not a new build)\n\n"
            "FIELDS (include only what applies to each intent object):\n"
            "  option (int): the option number referenced as the subject\n"
            "  revision (int or null): revision number if mentioned\n"
            "  tgt (int): target position/number for reorder\n"
            "  src_option (int): source option for move_to_revision\n"
            "  tgt_option (int): target option for move_to_revision\n"
            "For intent 'delete_elements' ONLY, also include:\n"
            "  scope (str): one of 'all' (delete whole model/everything), 'category' (specific element type), 'level' (everything on a floor)\n"
            "  category (str or null): element type to delete — e.g. 'walls', 'floors', 'columns', 'grids'. null when scope is 'all' or 'level'\n"
            "  level_start (int or null): 1-based floor number to delete from. null when scope is 'all'\n"
            "  level_end (int or null): 1-based floor number to delete to (inclusive). null when only one floor\n\n"
            "For intents 'new_build' and 'build' ONLY, also include:\n"
            "  specific (bool): true if the user described a specific building (typology, storeys, shape,\n"
            "    dimensions, aesthetic intent, or ANY design detail). false only if the request is a bare\n"
            "    generic prompt with no design specifics at all (e.g. 'build something', 'create a building').\n"
            "    When in doubt, set true — it is better to build than to ask.\n"
            "  temperature (float 0.0-1.0): how creatively Gemini should generate the building manifest.\n"
            "    Use high values (0.8-1.0) when the user asks for randomised, varied, organic, dramatic,\n"
            "    sleek, modern, unique, or any expressive aesthetic quality.\n"
            "    Use mid values (0.4-0.7) for normal builds with some design intent but no explicit creativity ask.\n"
            "    Use low values (0.1-0.3) for precise structural edits: exact dimensions, storey counts,\n"
            "    compliance changes, or any edit where correctness matters more than variety.\n"
            "    IMPORTANT: regardless of temperature, the output must always be a buildable, structurally\n"
            "    logical building — temperature only affects design variety, not correctness.\n"
            "  thinking_budget (int): thinking tokens for the build call.\n"
            "    16384 for new buildings, complex multi-constraint requests, or creative facade work.\n"
            "    8192 for simple edits (change a dimension, adjust a count, minor override).\n"
            "  fresh_build (bool, default false): set true when the user wants a completely new building\n"
            "    form — i.e. the CURRENT message is a retry/redo of a previous form that failed or produced\n"
            "    the wrong result, OR the user explicitly wants to start fresh with a new design.\n"
            "    Use the conversation context to decide: if the last few turns show a failed or unsatisfactory\n"
            "    attempt to build a specific form, and the current message is 'try again' / 'redo' / 'try\n"
            "    once more' / 'another attempt' etc., set fresh_build=true so the engine discards the\n"
            "    previous build's saved parameters and regenerates from scratch.\n"
            "    Set false for incremental edits to the CURRENT successful building (e.g. 'make it taller').\n"
            "  footprint_complexity (str): classify the building footprint shape the user is requesting.\n"
            "    'simple'  — rectangular or near-rectangular, no notches, voids, or non-orthogonal wings.\n"
            "                Examples: standard office tower, square building, rectangular base.\n"
            "    'complex' — non-rectangular: L-shape, U-shape, H-shape, T-shape, courtyard, atrium,\n"
            "                central void, irregular, organic, boomerang, split, two wings, re-entrant.\n"
            "    When in doubt or when the user describes any special footprint shape, use 'complex'.\n\n"
            "For ALL intents, also include:\n"
            "  goal (str): A 1-sentence inference of WHY the user is making this request — their underlying purpose, not just the literal action. E.g. 'clearing the model to start a fresh design', 'checking stair compliance before finalising drawings', 'exploring design alternatives for a client presentation'.\n"
            "  detail_level (str): One of 'brief', 'standard', 'detailed'. Infer from how the user phrased the request: 'brief' for simple direct questions or one-line commands; 'detailed' when the user says 'full', 'complete', 'all', 'breakdown', 'everything about'; 'standard' otherwise.\n"
            "  tone (str): One of 'technical' or 'conversational'. Match the user's register: 'technical' when they use precise jargon, measurements, or clause/table references; 'conversational' for everyday natural language.\n"
            "For intent 'clarify', write a warm, contextual 'question' field that references what the AI thinks the user might be trying to do and offers 2-3 concrete options. Never use a generic 'I don't understand' — always make an informed guess at the intent and ask to confirm. Match the inferred tone.\n\n"
            "CRITICAL DISTINCTIONS:\n"
            "  - 'clear the chat' / 'clear chat' / 'reset conversation' = clear_chat (NOT delete_elements)\n"
            "  - 'clear the building' / 'clear the model' / 'delete everything' = delete_elements scope=all\n"
            "  - 'delete all options' / 'clear all options' = delete_all_options (NOT delete_elements)\n"
            "  - 'delete the model AND delete all options' = TWO intents: delete_elements + delete_all_options\n\n"
            "EXAMPLES (single intent):\n"
            '  "make option 2 as option 1"                                          -> {{"intents":[{{"intent":"reorder_option","option":2,"tgt":1}}]}}\n'
            '  "use option 2"                                                        -> {{"intents":[{{"intent":"use_option","option":2}}]}}\n'
            '  "switch to option 3"                                                  -> {{"intents":[{{"intent":"rollback","option":3}}]}}\n'
            '  "list my options"                                                     -> {{"intents":[{{"intent":"list_options"}}]}}\n'
            '  "delete option 1 revision 2"                                         -> {{"intents":[{{"intent":"delete_revision","option":1,"revision":2}}]}}\n'
            '  "put option 2 under option 1"                                        -> {{"intents":[{{"intent":"move_to_revision","src_option":2,"tgt_option":1}}]}}\n'
            '  "delete everything"                                                   -> {{"intents":[{{"intent":"delete_elements","scope":"all"}}]}}\n'
            '  "delete all model"                                                    -> {{"intents":[{{"intent":"delete_elements","scope":"all"}}]}}\n'
            '  "clear the building"                                                  -> {{"intents":[{{"intent":"delete_elements","scope":"all"}}]}}\n'
            '  "delete all walls"                                                    -> {{"intents":[{{"intent":"delete_elements","scope":"category","category":"walls"}}]}}\n'
            '  "remove columns on floor 3"                                           -> {{"intents":[{{"intent":"delete_elements","scope":"category","category":"columns","level_start":3}}]}}\n'
            '  "delete floors 5 to 10"                                              -> {{"intents":[{{"intent":"delete_elements","scope":"category","category":"floors","level_start":5,"level_end":10}}]}}\n'
            '  "delete everything on level 6"                                       -> {{"intents":[{{"intent":"delete_elements","scope":"level","level_start":6}}]}}\n'
            '  "wipe floors 3 to 7"                                                 -> {{"intents":[{{"intent":"delete_elements","scope":"level","level_start":3,"level_end":7}}]}}\n'
            '  "clear the chat"                                                      -> {{"intents":[{{"intent":"clear_chat"}}]}}\n'
            '  "clear chat"                                                          -> {{"intents":[{{"intent":"clear_chat"}}]}}\n'
            '  "reset the conversation"                                              -> {{"intents":[{{"intent":"clear_chat"}}]}}\n'
            '  "how many floors are there"                                           -> {{"intents":[{{"intent":"query","goal":"checking current model state","detail_level":"brief","tone":"conversational"}}]}}\n'
            '  "what is the minimum stair width per SCDF"                           -> {{"intents":[{{"intent":"authority_query","goal":"verifying a code requirement for a design decision","detail_level":"standard","tone":"technical"}}]}}\n'
            '  "give me the full breakdown of Table 2.2A with all occupancy types"  -> {{"intents":[{{"intent":"authority_query","goal":"needs complete reference material for a detailed compliance review","detail_level":"detailed","tone":"technical"}}]}}\n'
            '  "audit doors for missing Fire Rating values"                       -> {{"intents":[{{"intent":"external_agent","agent_name":"agent_d","agent_action":"audit_data","category_name":"Doors","parameter_name":"Fire Rating","goal":"checking door data completeness","detail_level":"standard","tone":"technical"}}]}}\n'
            '  "use the data agent to fill missing Fire Rating in doors"          -> {{"intents":[{{"intent":"external_agent","agent_name":"agent_d","agent_action":"fill_data","category_name":"Doors","parameter_name":"Fire Rating","goal":"populating missing door fire ratings via AI","detail_level":"standard","tone":"technical"}}]}}\n'
            '  "fill in description for all walls in model"                       -> {{"intents":[{{"intent":"external_agent","agent_name":"agent_d","agent_action":"fill_data","category_name":"Walls","parameter_name":"Description","goal":"populating missing wall descriptions via AI","detail_level":"standard","tone":"conversational"}}]}}\n'
            '  "run agent D to check and fill mark on windows"                    -> {{"intents":[{{"intent":"external_agent","agent_name":"agent_d","agent_action":"start_pipeline","category_name":"Windows","parameter_name":"Mark","goal":"end-to-end audit and fill of Mark for windows","detail_level":"standard","tone":"conversational"}}]}}\n'
            '  "how many doors per level in the cloud model"                       -> {{"intents":[{{"intent":"external_agent","agent_name":"agent_i","agent_action":"query_revit_data","query":"How many doors on each level?","goal":"querying cloud-uploaded Revit data","detail_level":"standard","tone":"conversational"}}]}}\n'
            '  "ask bot-i to summarise the uploaded model"                         -> {{"intents":[{{"intent":"external_agent","agent_name":"agent_i","agent_action":"query_revit_data","query":"Summarise the uploaded Revit model.","goal":"summarising cloud model contents","detail_level":"standard","tone":"conversational"}}]}}\n'
            '  "push the doors to a new notion database"                           -> {{"intents":[{{"intent":"external_agent","agent_name":"agent_i","agent_action":"push_to_notion","mode":"new_database","category":"Door","goal":"exporting filtered elements to Notion","detail_level":"standard","tone":"conversational"}}]}}\n'
            '  "push the current model to notion as a new version"                 -> {{"intents":[{{"intent":"external_agent","agent_name":"agent_i","agent_action":"push_to_notion","mode":"versioned","goal":"versioned Notion push of current cloud data","detail_level":"standard","tone":"conversational"}}]}}\n'
            '  "what changed since the last upload"                                -> {{"intents":[{{"intent":"external_agent","agent_name":"agent_i","agent_action":"compare_snapshots","goal":"diffing the last two cloud snapshots","detail_level":"standard","tone":"conversational"}}]}}\n'
            '  "push the change report to notion"                                  -> {{"intents":[{{"intent":"external_agent","agent_name":"agent_i","agent_action":"compare_snapshots","push_report":true,"goal":"diff and publish change report to Notion","detail_level":"standard","tone":"conversational"}}]}}\n'
            '  "do something cool"                                                   -> {{"intents":[{{"intent":"clarify","question":"I\'d love to help — what did you have in mind? I can design a new building, give the current one a dramatic form, or look up a code requirement.","goal":"user wants something creative but hasn\'t specified","detail_level":"standard","tone":"conversational"}}]}}\n'
            '  "update it"                                                           -> {{"intents":[{{"intent":"clarify","question":"What would you like me to update? If you mean the building, I can change the dimensions, floor count, shape, or a specific element — just let me know.","goal":"user wants to modify something but hasn\'t specified what","detail_level":"standard","tone":"conversational"}}]}}\n'
            '  "create a building"                                                   -> {{"intents":[{{"intent":"new_build","specific":false,"temperature":0.4,"thinking_budget":16384,"footprint_complexity":"simple","goal":"creating a new building design","detail_level":"standard","tone":"conversational"}}]}}\n'
            '  "create a 10 storey office"                                           -> {{"intents":[{{"intent":"new_build","specific":true,"temperature":0.4,"thinking_budget":16384,"footprint_complexity":"simple","goal":"generating a 10-storey commercial office tower","detail_level":"standard","tone":"conversational"}}]}}\n'
            '  "create a 25 storey U-shape building"                                 -> {{"intents":[{{"intent":"new_build","specific":true,"temperature":0.4,"thinking_budget":16384,"footprint_complexity":"complex","goal":"generating a U-shaped commercial tower","detail_level":"standard","tone":"conversational"}}]}}\n'
            '  "design an L-shaped office tower with a courtyard"                    -> {{"intents":[{{"intent":"new_build","specific":true,"temperature":0.5,"thinking_budget":16384,"footprint_complexity":"complex","goal":"creating an L-shaped office tower with internal courtyard","detail_level":"standard","tone":"conversational"}}]}}\n'
            '  "make the building wider"                                             -> {{"intents":[{{"intent":"build","specific":true,"temperature":0.1,"thinking_budget":8192,"goal":"adjusting the building footprint dimensions","detail_level":"brief","tone":"conversational"}}]}}\n'
            '  "change floor 3 height to 4000mm"                                    -> {{"intents":[{{"intent":"build","specific":true,"temperature":0.1,"thinking_budget":8192,"goal":"making a precise dimensional adjustment to a specific floor","detail_level":"brief","tone":"technical"}}]}}\n'
            '  "delete everything"                                                   -> {{"intents":[{{"intent":"delete_elements","scope":"all","goal":"clearing the model, likely to start fresh","detail_level":"brief","tone":"conversational"}}]}}\n'
            "  [context: user asked for S-shape tower, got wrong result]\n"
            '  "try again"                                                           -> {{"intents":[{{"intent":"new_build","specific":true,"temperature":0.8,"thinking_budget":16384,"fresh_build":true}}]}}\n'
            "  [context: user asked for organic tower, build failed]\n"
            '  "try once more"                                                       -> {{"intents":[{{"intent":"new_build","specific":true,"temperature":0.8,"thinking_budget":16384,"fresh_build":true}}]}}\n'
            "  [context: normal conversation, no prior failure]\n"
            '  "try again"                                                           -> {{"intents":[{{"intent":"build","specific":false,"temperature":0.4,"thinking_budget":8192,"fresh_build":false}}]}}\n'
            "  [context: user asked an authority code question, got a wrong/incomplete answer]\n"
            '  "try again"                                                           -> {{"intents":[{{"intent":"authority_query"}}]}}\n'
            '  "that\'s wrong, redo"                                                 -> {{"intents":[{{"intent":"authority_query"}}]}}\n\n'
            "EXAMPLES (multiple intents — return ALL as separate objects in the array):\n"
            '  "delete the model and delete all options"                             -> {{"intents":[{{"intent":"delete_elements","scope":"all"}},{{"intent":"delete_all_options"}}]}}\n'
            '  "clear the model and clear all options"                               -> {{"intents":[{{"intent":"delete_elements","scope":"all"}},{{"intent":"delete_all_options"}}]}}\n'
            '  "wipe the building and reset all saved designs"                       -> {{"intents":[{{"intent":"delete_elements","scope":"all"}},{{"intent":"delete_all_options"}}]}}\n'
            '  "delete all walls and delete all options"                             -> {{"intents":[{{"intent":"delete_elements","scope":"category","category":"walls"}},{{"intent":"delete_all_options"}}]}}\n'
            '  "list options and query the model"                                    -> {{"intents":[{{"intent":"list_options"}},{{"intent":"query"}}]}}\n\n'
            "User message: {}\n\n"
            "JSON:"
        ).format(user_prompt)

        # Use flash model (no thinking, temp=0, tiny output)
        flash_model = "gemini-2.5-flash"
        url = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent?key={}".format(
            flash_model, self.api_key
        )
        data = {
            "contents": [{"role": "user", "parts": [{"text": classification_prompt}]}],
            "generationConfig": {
                "temperature": 0.0,
                "maxOutputTokens": 512,
                "thinkingConfig": {"thinkingBudget": 0},
            }
        }
        try:
            resp = self._make_request(url, data)
            if not resp or "error" in resp:
                self.log("classify_intent: API error — {}".format(resp))
                return None
            parts = resp["candidates"][0]["content"]["parts"]
            raw = "".join(p.get("text", "") for p in parts if not p.get("thought", False)).strip()
            # Strip markdown fences if model ignores instructions
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.lower().startswith("json"):
                    raw = raw[4:]
            raw = raw.strip()
            result = json.loads(raw)
            # Normalise: always return {"intents": [...]}
            if "intents" not in result and "intent" in result:
                result = {"intents": [result]}
            self.log("classify_intent: '{}' -> {}".format(user_prompt[:60], result))
            return result
        except Exception as e:
            self.log("classify_intent FAILED: {} — {}".format(type(e).__name__, e))
            return None

    def generate_content(self, prompt, thinking_budget=16384, temperature=0.1):
        """Pure text generation for internal agent manifest generation.

        thinking_budget: number of thinking tokens allowed (0=off, 1024=fast, 16384=default).
        temperature: 0.1 for precise structural edits, 1.0 for creative/design calls.
        """
        self.log("generate_content() entered with model: {} | thinking_budget: {} | temperature: {}".format(self.model, thinking_budget, temperature))
        url = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent?key={}".format(self.model, self.api_key)
        data = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                # Thinking tokens count against maxOutputTokens in Gemini 2.5 Flash.
                # Reserve thinking_budget for reasoning + 8192 tokens for the JSON output.
                "maxOutputTokens": thinking_budget + 8192,
                "thinkingConfig": {"thinkingBudget": thinking_budget},
            }
        }

        try:
            response_json = self._make_request(url, data)
            if not response_json or "error" in response_json:
                err = response_json.get("error", "Unknown API Error") if response_json else "Empty Response"
                self.log("Error in generate_content: " + str(err))
                return "Error: " + str(err)

            # Gemini 2.5 thinking models can return multiple parts:
            #   [{"thought": True, "text": "<thinking tokens>"},
            #    {"text": "<architectural_intent>...</architectural_intent>"},
            #    {"text": "```json\n{...}\n```"}]
            # We must skip thought parts and JOIN all remaining text parts so both
            # the narrative and the JSON block are present in the result.
            parts = response_json['candidates'][0]['content']['parts']
            text_parts = [p['text'] for p in parts if 'text' in p and not p.get('thought', False)]
            if not text_parts:
                self.log("generate_content: no non-thought text parts in response")
                return "Error: No text in response"
            result = '\n'.join(text_parts)
            self.log("generate_content successful. Parts: {} | Result length: {}".format(len(text_parts), len(result)))
            return result
        except Exception as e:
            self.log("generate_content CRASH: " + str(e))
            return "Error: " + str(e)

    def _make_request(self, url, data, max_retries=3):
        """Optimized httpx request with connection pooling and smart retry/recovery.

        Retry policy:
          - HTTP error (4xx/5xx): retry with exponential backoff, no session reset
          - Timeout: retry with backoff, no session reset (connection pool is fine)
          - Network error (DNS, refused, reset): reset session then retry

        Cancellation: uses streaming so the cancel flag is polled while the response
        body arrives, and the connection is aborted immediately on cancel.
        """
        from revit_mcp.cancel_manager import is_cancelled
        safe_url = url.split("key=")[0] + "key=********" if "key=" in url else url
        self.log("Requesting (httpx): " + safe_url)

        last_err = None
        for attempt in range(max_retries):
            if is_cancelled():
                self.log("_make_request: cancelled before attempt {}".format(attempt + 1))
                raise RuntimeError("Build cancelled by user.")

            try:
                start_time = time.time()
                # Stream the response so we can abort mid-download if cancelled.
                with self.session.stream("POST", url, json=data) as resp:
                    if is_cancelled():
                        self.log("_make_request: cancelled after headers received")
                        raise RuntimeError("Build cancelled by user.")

                    duration_headers = time.time() - start_time
                    self.log("Network: Headers {} in {:.2f}s".format(resp.status_code, duration_headers))

                    if resp.status_code != 200:
                        body = resp.read().decode("utf-8", errors="replace")[:200]
                        last_err = "HTTP {}".format(resp.status_code)
                        self.log("API Error ({}/{}): {}".format(attempt + 1, max_retries, body))
                        if attempt < max_retries - 1:
                            _sleep_interruptible(2 ** attempt, is_cancelled)
                        continue

                    # Read body in chunks, checking cancel between each chunk.
                    chunks = []
                    for chunk in resp.iter_bytes(chunk_size=65536):
                        if is_cancelled():
                            self.log("_make_request: cancelled mid-stream — aborting connection")
                            raise RuntimeError("Build cancelled by user.")
                        chunks.append(chunk)

                duration = time.time() - start_time
                raw = b"".join(chunks)
                self.log("Network: Response 200 | Time: {:.2f}s | Size: {} bytes".format(duration, len(raw)))
                return json.loads(raw)

            except RuntimeError:
                raise  # propagate cancellation immediately

            except httpx.TimeoutException as e:
                last_err = "Timeout: {}".format(str(e))
                self.log("Network TIMEOUT (Attempt {}/{}): {}".format(attempt + 1, max_retries, last_err))
                if attempt < max_retries - 1:
                    _sleep_interruptible(2 ** attempt, is_cancelled)

            except Exception as e:
                last_err = str(e)
                self.log("Network ERROR (Attempt {}/{}): {}".format(attempt + 1, max_retries, last_err))
                self._init_session()
                if attempt < max_retries - 1:
                    _sleep_interruptible(2 ** attempt, is_cancelled)

        return {"error": last_err}


def _sleep_interruptible(seconds, is_cancelled_fn, interval=0.1):
    """Sleep for `seconds` but wake up early if cancellation is requested."""
    elapsed = 0.0
    while elapsed < seconds:
        if is_cancelled_fn():
            raise RuntimeError("Build cancelled by user.")
        time.sleep(interval)
        elapsed += interval

client = GeminiClient()
