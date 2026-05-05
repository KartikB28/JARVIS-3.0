"""
Multi-step task planner.

Converts natural-language requests into an ordered list of executable
plan steps. Tries cheap Python patterns first; falls back to the LLM
only when nothing else fits — keeping CHAPPIE's "code-heavy, low LLM"
philosophy intact for common tasks while still handling open-ended
requests.

A plan step is a dict:
    {
        "intent": IntentType,
        "parameters": {...},
        "description": str,        # short user-facing label
    }
"""

import json
import re
from typing import Dict, List, Optional

from core.app_registry import (
    BROWSER_NAMES,
    find_drive_letter,
    find_known_app,
    find_special_folder,
    find_web_shortcut,
    resolve_special_path,
    strip_fillers,
)
from core.intent_parser import OPEN_VERBS, IntentParser, IntentType
from models.llm_handler import OllamaHandler
from utils.logger import setup_logger

logger = setup_logger(__name__)


# Conjunctions that split a request into multiple parts.
CHAIN_SPLIT_RX = re.compile(
    r"\s+(?:and\s+then|and\s+also|and|then|,\s*then|;|,)\s+",
    re.IGNORECASE,
)

# "<verb> X in <browser>" / "<verb> X using <browser>".
IN_PATTERN_RX = re.compile(
    rf"^{OPEN_VERBS}\s+(.+?)\s+(?:in|using|on|with)\s+(.+?)\s*$",
    re.IGNORECASE,
)

# "search [for] X in/on/using <browser>".
SEARCH_IN_RX = re.compile(
    r"^(?:search|google|find|look\s+up)\s+(?:for\s+)?(.+?)\s+(?:in|on|using|with)\s+(.+?)\s*$",
    re.IGNORECASE,
)

# "[open] youtube [and] {play|put|find|search [for]} <query>"
YOUTUBE_PLAY_RX = re.compile(
    r"^(?:open\s+)?(?:youtube|yt)\s*[,]?\s+"
    r"(?:and\s+)?(?:play|put\s+on|put|search\s+(?:for\s+)?|find|show|watch)\s+"
    r"(.+?)\s*$",
    re.IGNORECASE,
)

# "[open] chatgpt [and] {ask [it] [about] | tell me about | about} <question>"
CHATGPT_ASK_RX = re.compile(
    r"^(?:open\s+)?(?:chat\s*gpt|chatgpt|gpt)\s*[,]?\s+"
    r"(?:and\s+)?"
    r"(?:ask\s+(?:it\s+)?(?:about\s+)?|tell\s+me\s+about\s+|about\s+|"
    r"to\s+(?:tell|explain)\s+(?:me\s+)?(?:about\s+)?)?"
    r"(.+?)\s*$",
    re.IGNORECASE,
)

# "ask chatgpt [about] X"
ASK_CHATGPT_RX = re.compile(
    r"^ask\s+(?:chat\s*gpt|chatgpt|gpt)\s+(?:about\s+|to\s+(?:tell|explain)\s+(?:me\s+)?(?:about\s+)?)?"
    r"(.+?)\s*$",
    re.IGNORECASE,
)

# "<verb> ... folder/directory/drive/disk ..." — signals OPEN_PATH.
PATH_VERBS = r"(?:open|show|browse|view|go\s+to|take\s+me\s+to)"


PLANNER_SYSTEM_PROMPT = """You are CHAPPIE's planning brain — JARVIS-grade. Your job: convert what the user said into a concrete JSON plan that the executor can run.

Output ONE JSON object only. Never wrap in markdown. No commentary.

Two output shapes:
A) {"steps": [<step>, <step>, ...]}     — executable plan, top to bottom
B) {"clarify": "<one short question>"}  — when the request is genuinely ambiguous

==================== TOOLS ====================

CORE
- {"intent":"open_app","target":"<app-name>"}
    Launches an app by name. The agent has a fuzzy file-index covering the
    user's Start Menu / Applications / installed games — so unusual or
    branded names ("valorant", "league of legends", "blender") work.
    Examples of canonical names it knows directly: chrome, firefox, edge,
    brave, file explorer, vscode, notepad, calculator, cmd, terminal,
    powershell, word, excel, powerpoint, spotify, discord, slack, steam.

- {"intent":"open_path","target":"<absolute-path-or-special-folder>"}
    Opens a folder in the file manager.
    Special-folder names: Downloads, Desktop, Documents, Pictures, Music, Videos.
    Drive letters: C:\\, D:\\, E:\\.

- {"intent":"open_url","target":"<url>","browser":"<optional>"}
    Open a URL. Add "browser" to use a specific one (chrome/firefox/edge/brave).

- {"intent":"open_file","target":"<filename-or-fragment>"}
    Open a specific file. Falls back to fuzzy index search by name.

WEB AUTOMATION
- {"intent":"browser_task","task":"youtube_play","query":"..."}
    Searches YouTube and plays the first result via Playwright.

- {"intent":"browser_task","task":"chatgpt_ask","question":"..."}
    Opens ChatGPT and submits the question.

GAMES
- For Steam games, prefer:
    {"intent":"open_url","target":"steam://rungameid/<appid>"}
  if you know the appid; otherwise use open_app with the game name —
  the index will find the Steam/Riot/Epic shortcut.
- Common appids you may use: 730 (CS2), 271590 (GTA V), 252490 (Rust),
  578080 (PUBG), 1172470 (Apex Legends), 1086940 (Baldur's Gate 3),
  1245620 (Elden Ring), 359550 (Rainbow Six Siege).

SKILLS
- {"intent":"skill","skill":"document","params":{"doc_type":"letter|memo|essay|report","recipient":"<name>","topic":"<topic>"}}
    Generates a Word document. The skill drafts the body via the LLM and
    saves to ~/Documents/CHAPPIE.

- {"intent":"skill","skill":"email","params":{"recipient":"<name-or-email>","topic":"<topic>"}}
    Drafts an email body and opens the user's mail client pre-filled.

- {"intent":"skill","skill":"calendar","params":{"event":"<title>","when":"<natural-time>","who":"<optional>"}}
    Schedules a calendar event as an .ics file the OS opens in the user's
    default calendar. "when" can be natural ("tomorrow 7pm", "Friday at 6").

- {"intent":"skill","skill":"notes","params":{"note":"<text>"}}
    Saves a quick note to KB + ~/Documents/CHAPPIE/notes.md.

KNOWLEDGE
- {"intent":"learn_preference","source":"<key>","target":"<value>"}
    Remember a fact about the user. Keys: name, favorite_<x>, contact:<name>.

- {"intent":"query_knowledge","target":"<question>"}
    Look up something the user previously told CHAPPIE.

CONVERSATION
- {"intent":"converse","prompt":"<the user's input>"}
    Use this when the user is asking a general question, chatting, or wants
    information rather than an action. CHAPPIE will reply with the JARVIS persona.

- {"intent":"greeting"}  (no params)
    Hi/hello/thanks/bye etc.

==================== RULES ====================

1. Multi-step requests get multi-step plans. "Open Spotify and play Despacito"
   → 2 steps. "Write a letter to my landlord then schedule a meeting" → 2 steps.

2. For "open <thing>" requests where the thing might be an app, file, or
   folder on the user's machine: emit ONE open_app step with just the name.
   The executor's file index handles the resolution. Don't try to guess paths.

3. Use clarify ONLY when the request is genuinely ambiguous — vague unit
   ("1 cr per year or total?"), an ambiguous proper noun ("which Sarah?"),
   or multiple equally plausible interpretations. Do NOT clarify just
   because the request is short — short and clear is fine.

4. For info questions ("what's the weather", "what time is it",
   "tell me about quantum mechanics"), use converse. Do NOT use
   browser_task chatgpt_ask just because something is a question.

5. For "play <X>" without context: if X looks like a game, use open_app(X).
   If X looks like a song/video, use browser_task youtube_play.

6. Refuse destructive system commands (rm -rf, del C:\\*, format, shutdown)
   by emitting {"steps":[]}.

7. Output JSON. Just JSON. Nothing else.

==================== EXAMPLES ====================

User: "open valorant"
{"steps":[{"intent":"open_app","target":"valorant"}]}

User: "play valorant"
{"steps":[{"intent":"open_app","target":"valorant"}]}

User: "let me play CS2 on steam"
{"steps":[{"intent":"open_url","target":"steam://rungameid/730"}]}

User: "open spotify and play taylor swift's anti-hero"
{"steps":[{"intent":"open_app","target":"spotify"},{"intent":"browser_task","task":"youtube_play","query":"Taylor Swift Anti-Hero"}]}

User: "write a quick letter to my landlord about a leaky faucet, then schedule a maintenance call for tomorrow at 4pm"
{"steps":[
  {"intent":"skill","skill":"document","params":{"doc_type":"letter","recipient":"landlord","topic":"leaky faucet repair request"}},
  {"intent":"skill","skill":"calendar","params":{"event":"Faucet maintenance call","when":"tomorrow at 4pm","who":"landlord"}}
]}

User: "open chrome, go to youtube, then play 5 minute crafts newest"
{"steps":[
  {"intent":"open_app","target":"chrome"},
  {"intent":"browser_task","task":"youtube_play","query":"5 minute crafts newest"}
]}

User: "what's the weather like"
{"steps":[{"intent":"converse","prompt":"what's the weather like"}]}

User: "what time is it"
{"steps":[{"intent":"converse","prompt":"what time is it"}]}

User: "open my downloads folder"
{"steps":[{"intent":"open_path","target":"Downloads"}]}

User: "open disk D"
{"steps":[{"intent":"open_path","target":"D:\\\\"}]}

User: "play BBS's newest gaming video"
{"clarify":"Quick check — which BBS? BlackBoxStocks, BeerBiceps, BBS Gaming, or someone else?"}

User: "best private colleges in delhi with fees under 1cr"
{"clarify":"Just to confirm — 1 crore total over the program, per year, or per semester?"}

User: "remember my email is kartik@example.com"
{"steps":[{"intent":"learn_preference","source":"email","target":"kartik@example.com"}]}

User: "take a note: pick up groceries on the way home"
{"steps":[{"intent":"skill","skill":"notes","params":{"note":"pick up groceries on the way home"}}]}

User: "thanks chappie"
{"steps":[{"intent":"greeting"}]}

User: "open chatgpt and ask about the best AI model for coding"
{"steps":[{"intent":"browser_task","task":"chatgpt_ask","question":"What's the best AI model for coding right now?"}]}
"""


class Planner:
    """Builds an ordered list of plan steps from natural-language input."""

    def __init__(
        self,
        kb,
        parser: IntentParser,
        llm: OllamaHandler,
        skills=None,
        llm_first: bool = True,
    ):
        self.kb = kb
        self.parser = parser
        self.llm = llm
        self.skills = skills  # SkillRegistry, optional
        self.llm_first = llm_first
        # Diagnostics — overwrite per call so /debug/last-plan can read it.
        self.last_plan_meta: Dict = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def plan(self, user_input: str, knowledge_context: str = "") -> List[Dict]:
        """
        Default flow with llm_first=True (recommended):
            1. Trivial fast-paths (greeting, high-confidence preference learn)
            2. LLM planner with full tool catalog and recent history
            3. Regex fast-paths as offline fallback
            4. CONVERSE for anything else

        With llm_first=False (older behaviour):
            1. All regex fast-paths first
            2. LLM as last resort
        """
        text = (user_input or "").strip()
        if not text:
            return []

        meta: Dict = {"input": text, "path": []}

        # Trivial fast-paths that don't need the LLM (greeting, high-confidence
        # preference learning). Anything subtler goes to the LLM.
        intent = self.parser.parse(text, knowledge_context)
        if intent["type"] == IntentType.GREETING:
            meta["path"].append("fastpath:greeting")
            self.last_plan_meta = meta
            return [self._intent_to_step(intent)]
        if (
            intent["type"] == IntentType.LEARN_PREFERENCE
            and intent["confidence"] >= 0.96
        ):
            meta["path"].append("fastpath:learn_preference")
            self.last_plan_meta = meta
            return [self._intent_to_step(intent)]

        # PRIMARY path: ask the LLM. It sees the full tool catalog and
        # recent context, and can produce multi-step plans naturally.
        if self.llm_first:
            llm_steps = await self._llm_plan(text, knowledge_context)
            if llm_steps:
                meta["path"].append("llm_first")
                meta["steps"] = [s.get("description") for s in llm_steps]
                self.last_plan_meta = meta
                return llm_steps
            meta["path"].append("llm_first:empty")

        # FALLBACK path: regex fast-paths (works offline / without an LLM).
        browser_steps = self._try_browser_task(text)
        if browser_steps:
            meta["path"].append("regex:browser_task")
            self.last_plan_meta = meta
            return browser_steps

        skill_step = self._try_skill_match(text)
        if skill_step:
            meta["path"].append("regex:skill")
            self.last_plan_meta = meta
            return [skill_step]

        chain_steps = self._try_chain(text, knowledge_context)
        if chain_steps and len(chain_steps) > 1:
            meta["path"].append("regex:chain")
            self.last_plan_meta = meta
            return chain_steps

        single = self._plan_single(text, knowledge_context, fallback_intent=intent)
        if single:
            meta["path"].append("regex:single")
            self.last_plan_meta = meta
            return single

        # If we didn't try the LLM yet (llm_first=False), try it now.
        if not self.llm_first:
            llm_steps = await self._llm_plan(text, knowledge_context)
            if llm_steps:
                meta["path"].append("llm_fallback")
                self.last_plan_meta = meta
                return llm_steps

        # Truly nothing matched — fall through to a free conversational reply.
        meta["path"].append("converse")
        self.last_plan_meta = meta
        return [
            {
                "intent": IntentType.CONVERSE,
                "parameters": {"prompt": text},
                "description": "converse",
            }
        ]

    def _plan_single(
        self,
        text: str,
        ctx: str,
        fallback_intent: Optional[Dict] = None,
    ) -> Optional[List[Dict]]:
        """Plan a single utterance via the cheap fast-paths only."""
        # High-level browser tasks (youtube_play, chatgpt_ask).
        browser_steps = self._try_browser_task(text)
        if browser_steps:
            return browser_steps

        # "<X> in <browser>" (open or search variants).
        in_steps = self._try_in_pattern(text)
        if in_steps:
            return in_steps

        # Special folders / drives.
        path_step = self._try_open_path(text)
        if path_step:
            return [path_step]

        # Single intent via regex parser.
        intent = fallback_intent if fallback_intent is not None else self.parser.parse(text, ctx)
        if intent["type"] != IntentType.UNKNOWN:
            return [self._intent_to_step(intent)]

        return None

    def _try_skill_match(self, text: str) -> Optional[Dict]:
        """If a registered skill claims this input, return a single SKILL step."""
        if not self.skills:
            return None
        match = self.skills.find_match(text)
        if not match:
            return None
        skill, params = match
        return {
            "intent": IntentType.SKILL,
            "parameters": {"skill": skill.name, "params": params},
            "description": f"skill:{skill.name}",
        }

    def _try_browser_task(self, text: str) -> Optional[List[Dict]]:
        """Recognize "open youtube and play X" / "ask chatgpt X" patterns."""
        stripped = text.strip()

        m = YOUTUBE_PLAY_RX.search(stripped)
        if m:
            query = m.group(1).strip().rstrip("?.!,")
            if query:
                return [
                    {
                        "intent": IntentType.BROWSER_TASK,
                        "parameters": {"task": "youtube_play", "query": query},
                        "description": f"youtube: play '{query[:40]}'",
                    }
                ]

        m = ASK_CHATGPT_RX.search(stripped) or CHATGPT_ASK_RX.search(stripped)
        if m:
            question = m.group(1).strip().rstrip(".!")
            # Strip filler so "open chatgpt please" / "now" don't become
            # questions, but real one-word questions ("photosynthesis") pass.
            cleaned = strip_fillers(question).strip()
            if cleaned and len(cleaned) >= 3:
                return [
                    {
                        "intent": IntentType.BROWSER_TASK,
                        "parameters": {"task": "chatgpt_ask", "question": question},
                        "description": f"chatgpt: '{question[:40]}'",
                    }
                ]

        return None

    # ------------------------------------------------------------------
    # Fast-paths
    # ------------------------------------------------------------------

    def _try_open_path(self, text: str) -> Optional[Dict]:
        lowered = text.lower()
        if not re.search(rf"\b{PATH_VERBS}\b", lowered):
            return None

        folder_name = find_special_folder(lowered)
        if folder_name:
            path = resolve_special_path(folder_name)
            if path:
                return {
                    "intent": IntentType.OPEN_PATH,
                    "parameters": {"target": path},
                    "description": f"open {folder_name}",
                }

        drive = find_drive_letter(lowered)
        if drive:
            return {
                "intent": IntentType.OPEN_PATH,
                "parameters": {"target": f"{drive}:\\"},
                "description": f"open drive {drive}",
            }
        return None

    def _try_in_pattern(self, text: str) -> Optional[List[Dict]]:
        # Try "search ... in <browser>" first since it's a different verb set.
        m = SEARCH_IN_RX.search(text)
        as_search = bool(m)
        if not m:
            m = IN_PATTERN_RX.search(text)
        if not m:
            return None

        target_phrase = strip_fillers(m.group(1))
        container_phrase = strip_fillers(m.group(2))

        container_app = find_known_app(container_phrase)
        if container_app not in BROWSER_NAMES:
            return None  # only browsers act as URL containers

        if as_search:
            return [
                {
                    "intent": IntentType.OPEN_URL,
                    "parameters": {
                        "target": f"https://www.google.com/search?q={target_phrase.replace(' ', '+')}",
                        "browser": container_app,
                    },
                    "description": f"search '{target_phrase}' in {container_app}",
                }
            ]

        url = find_web_shortcut(target_phrase)
        if url:
            return [
                {
                    "intent": IntentType.OPEN_URL,
                    "parameters": {"target": url, "browser": container_app},
                    "description": f"open {target_phrase} in {container_app}",
                }
            ]

        # Fallback: treat the inside as a search query.
        return [
            {
                "intent": IntentType.OPEN_URL,
                "parameters": {
                    "target": f"https://www.google.com/search?q={target_phrase.replace(' ', '+')}",
                    "browser": container_app,
                },
                "description": f"search '{target_phrase}' in {container_app}",
            }
        ]

    def _try_chain(self, text: str, ctx: str) -> Optional[List[Dict]]:
        parts = [p.strip() for p in CHAIN_SPLIT_RX.split(text) if p.strip()]
        if len(parts) <= 1:
            return None

        # Inherit a leading verb so bare targets in later clauses still parse:
        # "open chrome and youtube" -> ("open chrome", "open youtube").
        first_lower = parts[0].lower()
        leading_verb = None
        m = re.search(r"\b(open|launch|start|run|show|play|go\s+to|search\s+for)\b", first_lower)
        if m:
            leading_verb = m.group(1)

        steps: List[Dict] = []
        for part in parts:
            sub = self._plan_single(part, ctx)

            # If a part didn't resolve and we have a leading verb, retry with it prepended.
            if not sub and leading_verb and not re.match(
                rf"^\s*{re.escape(leading_verb.split()[0])}\b", part, re.I
            ):
                sub = self._plan_single(f"{leading_verb} {part}", ctx)

            if sub:
                steps.extend(sub)

        return steps if steps else None

    # ------------------------------------------------------------------
    # LLM fallback
    # ------------------------------------------------------------------

    async def _llm_plan(self, text: str, ctx: str) -> List[Dict]:
        # Pull a few recent exchanges so the LLM can resolve pronouns
        # ("open it again", "no the other one").
        history_msgs = self._recent_history_messages(limit=4)

        # The user's actual request, with KB context as a prefix the LLM can
        # consult but won't echo.
        request = text
        if ctx:
            request = (
                "# What CHAPPIE knows about this user (reference only):\n"
                f"{ctx}\n\n# Current request:\n{text}"
            )

        messages = history_msgs + [{"role": "user", "content": request}]

        response = await self.llm.generate(
            messages=messages,
            system=PLANNER_SYSTEM_PROMPT,
            temperature=0.1,
        )
        if not response:
            logger.warning("LLM planner returned empty response — is the LLM running?")
            return []

        return self._parse_llm_response(response)

    def _recent_history_messages(self, limit: int = 4) -> List[Dict]:
        """Return last <limit> exchanges as alternating user/assistant messages."""
        try:
            cursor = self.kb.conn.cursor()
            cursor.execute(
                """
                SELECT user_input, agent_response
                FROM conversations
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = list(reversed(cursor.fetchall()))
        except Exception:
            return []

        msgs: List[Dict] = []
        for row in rows:
            if row["user_input"]:
                msgs.append({"role": "user", "content": row["user_input"]})
            if row["agent_response"]:
                msgs.append({"role": "assistant", "content": row["agent_response"]})
        return msgs

    def _parse_llm_response(self, response: str) -> List[Dict]:
        """
        Extract a plan or a clarification request from the LLM output.

        Accepts any of:
          {"clarify": "<question>"}
          {"steps": [<step>, ...]}
          [<step>, ...]              (legacy bare list)

        Clarification requests come back as a single CLARIFY step.
        """
        cleaned = re.sub(
            r"^```(?:json)?|```$", "", response.strip(), flags=re.MULTILINE
        ).strip()

        # Try object-shaped output first (preferred).
        obj_match = re.search(r"\{[\s\S]*\}", cleaned)
        if obj_match:
            try:
                obj = json.loads(obj_match.group(0))
            except json.JSONDecodeError:
                obj = None

            if isinstance(obj, dict):
                if "clarify" in obj and isinstance(obj["clarify"], str):
                    return [
                        {
                            "intent": IntentType.CLARIFY,
                            "parameters": {"question": obj["clarify"].strip()},
                            "description": "clarify",
                        }
                    ]
                if "steps" in obj and isinstance(obj["steps"], list):
                    return self._convert_steps(obj["steps"])

        # Fall back to a bare JSON array.
        arr_match = re.search(r"\[[\s\S]*\]", cleaned)
        if arr_match:
            try:
                arr = json.loads(arr_match.group(0))
                if isinstance(arr, list):
                    return self._convert_steps(arr)
            except json.JSONDecodeError:
                pass

        logger.warning(f"Unparseable LLM planner response: {response[:200]}")
        return []

    @staticmethod
    def _convert_steps(raw_steps: List) -> List[Dict]:
        steps: List[Dict] = []
        for raw in raw_steps:
            if not isinstance(raw, dict):
                continue
            intent_str = raw.get("intent")
            if not intent_str:
                continue
            try:
                intent_type = IntentType(intent_str)
            except ValueError:
                logger.warning(f"LLM proposed unknown intent: {intent_str}")
                continue

            params = {k: v for k, v in raw.items() if k != "intent"}
            steps.append(
                {
                    "intent": intent_type,
                    "parameters": params,
                    "description": intent_str,
                }
            )
        return steps

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _intent_to_step(intent: Dict) -> Dict:
        return {
            "intent": intent["type"],
            "parameters": dict(intent.get("parameters", {})),
            "description": intent["type"].value,
        }
