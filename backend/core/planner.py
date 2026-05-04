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


PLANNER_SYSTEM_PROMPT = """You are CHAPPIE's task planner. Output ONE JSON object. Never wrap in markdown.

Choose ONE shape:
A) {"steps": [...]}                — executable plan
B) {"clarify": "<single question>"} — when the request is ambiguous

When to clarify:
- Ambiguous names (e.g. "BBS" — which person/channel?)
- Vague quantities or units ("1 cr" — per year, per semester, total?)
- Multiple valid interpretations of "newest", "best", "the X video"
- Action with multiple valid forms

Make the question short, conversational, propose specific options when useful.

Available step intents:
- {"intent":"open_app","target":"<app>"}
- {"intent":"open_url","target":"<url>","browser":"<optional>"}
- {"intent":"open_path","target":"<absolute-path>"}
- {"intent":"search","target":"<query>"}
- {"intent":"file_operation","target":"<path>"}
- {"intent":"system_command","target":"<safe-cmd>"}
- {"intent":"learn_preference","source":"<key>","target":"<value>"}
- {"intent":"query_knowledge","target":"<question>"}
- {"intent":"browser_task","task":"youtube_play","query":"..."}
- {"intent":"browser_task","task":"chatgpt_ask","question":"..."}
- {"intent":"greeting"}

Known apps: chrome, firefox, edge, brave, safari, file explorer, notepad, vscode, calculator, cmd, terminal, powershell, word, excel, powerpoint, spotify, discord, slack, steam, vlc, obs.
Web shortcuts: google, youtube, gmail, github, twitter, reddit, netflix, amazon, wikipedia, chatgpt, claude.
Special paths: ~/Downloads, ~/Desktop, ~/Documents, ~/Pictures, ~/Music, ~/Videos, C:\\, D:\\.

Rules:
1. Output ONLY one JSON object. No markdown fences, no explanations.
2. Steps execute top-to-bottom.
3. Refuse destructive commands (rm -rf, format, del C:\\*, shutdown, etc.) — emit {"steps": []}.
4. For multi-step asks, split into atomic steps.
5. Prefer specific intents (browser_task, open_url) over generic ones.
6. When a follow-up message starts with "(clarifications: ...)", combine that
   context with the original request and return steps.

Examples:

"open YouTube and play BBS's newest gaming video"
{"clarify":"Quick check — which BBS? BlackBoxStocks, BeerBiceps, BBS Gaming, or someone else?"}

"open chatgpt and ask about colleges with fees under 1cr"
{"clarify":"Just to confirm — 1 crore total over the program, per year, or per semester?"}

"open chrome and youtube"
{"steps":[{"intent":"open_app","target":"chrome"},{"intent":"open_url","target":"https://youtube.com"}]}

"open YouTube and play the latest BlackBoxStocks gaming video"
{"steps":[{"intent":"browser_task","task":"youtube_play","query":"BlackBoxStocks newest gaming video"}]}

"ask chatgpt about the best private colleges in delhi with fees under 1cr per year"
{"steps":[{"intent":"browser_task","task":"chatgpt_ask","question":"What are the best private colleges in Delhi with fees under 1 crore per year?"}]}

"open YouTube and play BBS's newest gaming video (clarifications: BlackBoxStocks)"
{"steps":[{"intent":"browser_task","task":"youtube_play","query":"BlackBoxStocks newest gaming video"}]}
"""


class Planner:
    """Builds an ordered list of plan steps from natural-language input."""

    def __init__(self, kb, parser: IntentParser, llm: OllamaHandler, skills=None):
        self.kb = kb
        self.parser = parser
        self.llm = llm
        self.skills = skills  # SkillRegistry, optional

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def plan(self, user_input: str, knowledge_context: str = "") -> List[Dict]:
        text = (user_input or "").strip()
        if not text:
            return []

        # Greeting short-circuit (handled by the parser's own fast-path).
        intent = self.parser.parse(text, knowledge_context)
        if intent["type"] == IntentType.GREETING:
            return [self._intent_to_step(intent)]

        # Browser tasks BEFORE chain split so "open youtube and play X"
        # stays whole instead of getting split into [open_url, open_app].
        browser_steps = self._try_browser_task(text)
        if browser_steps:
            return browser_steps

        # Skill match (write a letter, schedule X, take a note, ...)
        skill_step = self._try_skill_match(text)
        if skill_step:
            return [skill_step]

        # Compound chains so "launch vscode and open my downloads" -> 2 steps.
        chain_steps = self._try_chain(text, knowledge_context)
        if chain_steps and len(chain_steps) > 1:
            return chain_steps

        # Single-utterance fast paths.
        single = self._plan_single(text, knowledge_context, fallback_intent=intent)
        if single:
            return single

        # Last resort: LLM planner.
        llm_steps = await self._llm_plan(text, knowledge_context)
        if llm_steps:
            return llm_steps

        # No structured plan possible — fall through to a free conversational
        # reply. CHAPPIE talks; it doesn't just "I don't understand" at people.
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
        prompt = text
        if ctx:
            prompt = f"# Context (for reference):\n{ctx}\n\n# Request:\n{text}"

        response = await self.llm.generate(
            messages=[{"role": "user", "content": prompt}],
            system=PLANNER_SYSTEM_PROMPT,
            temperature=0.1,
        )
        if not response:
            logger.warning("LLM planner returned empty response (Ollama down?)")
            return []

        return self._parse_llm_response(response)

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
