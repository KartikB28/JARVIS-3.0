"""
Parse user intent using pattern matching, not LLM.
This is where Python does heavy lifting.
"""

import re
from enum import Enum
from typing import Dict, List, Optional, Tuple

from core.app_registry import (
    find_known_app,
    find_web_shortcut,
    strip_fillers,
)
from utils.logger import setup_logger

logger = setup_logger(__name__)


# Greeting / small-talk patterns. Anchored to the *whole* input so that
# "hi can you open chrome" still routes to OPEN_APP, but "hi", "hello!",
# "good morning", "how are you today" all resolve instantly.
GREETING_PATTERNS = [
    r"^(hi|hello|hey|yo|sup|hiya|howdy|hola)[\s!.?]*$",
    r"^(hi|hello|hey)\s+(chappie|there|bot|friend|buddy)[\s!.?]*$",
    r"^good\s+(morning|afternoon|evening|night)[\s!.?]*$",
    r"^(thanks|thank\s+you|thx|ty|cheers)[\s!.?]*$",
    r"^(bye|goodbye|see\s+you|cya|later|good\s*night)[\s!.?]*$",
    r"^how\s+(are\s+you|are\s+things|'?s\s+it\s+going|s\s+it\s+going)(\s+today|\s+doing)?[\s!.?]*$",
    r"^what'?s\s+up[\s!.?]*$",
    r"^(nice\s+to\s+meet\s+you|pleased\s+to\s+meet\s+you)[\s!.?]*$",
]

# Verbs that signal "open / launch this thing".
OPEN_VERBS = r"(?:open(?:\s+up)?|launch|start|run|fire\s+up|boot|bring\s+up)"


class IntentType(Enum):
    """All possible intents CHAPPIE can handle."""

    OPEN_APP = "open_app"
    OPEN_FILE = "open_file"
    OPEN_PATH = "open_path"
    OPEN_URL = "open_url"
    EXECUTE_CODE = "execute_code"
    SEARCH = "search"
    FILE_OPERATION = "file_operation"
    SYSTEM_COMMAND = "system_command"
    LEARN_PREFERENCE = "learn_preference"
    QUERY_KNOWLEDGE = "query_knowledge"
    SCHEDULE_TASK = "schedule_task"
    BROWSER_ACTION = "browser_action"
    BROWSER_TASK = "browser_task"
    TEXT_GENERATION = "text_generation"
    DATA_ANALYSIS = "data_analysis"
    GREETING = "greeting"
    CLARIFY = "clarify"
    UNKNOWN = "unknown"


class IntentParser:
    """
    Heavy pattern-matching system.
    Uses regex, keyword matching, and context to determine intent.
    Minimal LLM involvement.
    """

    def __init__(self, knowledge_base):
        self.kb = knowledge_base
        self.intent_patterns: Dict[IntentType, List[Tuple[str, int]]] = self._build_patterns()

    def _build_patterns(self) -> Dict[IntentType, List[Tuple[str, int]]]:
        """
        Build intent patterns with priorities.
        Format: (regex_pattern, priority). Higher priority is checked first.
        """
        return {
            # Preferences need to be checked BEFORE generic queries / opens.
            IntentType.LEARN_PREFERENCE: [
                (
                    r"remember\s+(?:that\s+)?(?:my\s+|i\s+)(?:favorite|preferred|default|main)\s+(\w+)\s+is\s+(.*?)\s*$",
                    98,
                ),
                (
                    r"(?:^|\s)(?:my\s+|i\s+)(?:favorite|preferred|default|main)\s+(\w+)\s+is\s+(.*?)\s*$",
                    96,
                ),
                (
                    r"(?:set|save|store)\s+(?:my\s+)?(?:preference|setting)\s+(\w+)\s*=\s*(.*?)\s*$",
                    94,
                ),
            ],
            IntentType.QUERY_KNOWLEDGE: [
                (
                    r"(?:what\s+(?:do\s+)?you\s+(?:know|remember)\s+about|tell\s+me\s+(?:about|what\s+you\s+know\s+about))\s+(.*?)\s*$",
                    97,
                ),
                (r"(?:do\s+)?you\s+remember\s+(?:when|if)\s+(.*?)\s*$", 93),
                (r"(?:what'?s|what\s+is)\s+my\s+(.+?)\s*\??\s*$", 92),
            ],
            IntentType.OPEN_URL: [
                (
                    r"(?:open|go to|visit|browse)\s+((?:https?://)?[\w-]+\.[\w.-]+(?:/\S*)?)\s*$",
                    96,
                ),
                (r"(?:search|google)\s+(?:for\s+)?(.*?)\s+on\s+(.*?)\s*$", 86),
            ],
            IntentType.OPEN_FILE: [
                # Require the target to look like a real path / extension so
                # "open file explorer" never matches here.
                (
                    r"(?:open|show|load)\s+(?:file|document|project)\s+(?:named\s+|called\s+)?"
                    r"([A-Za-z]:[\\/][^\s]+|[~/][^\s]+|[^\s]+\.[A-Za-z0-9]{1,6})\s*$",
                    95,
                ),
                (r"(?:open|show|load|edit)\s+(\S+\.\w{1,6})\s*$", 91),
            ],
            IntentType.OPEN_APP: [
                (r"(?:open up|fire up|boot|bring up)\s+(.*?)\s*$", 90),
                (r"(?:open|launch|start|run)\s+(?:the\s+)?(.+?)\s*$", 88),
                (r"^(chrome|firefox|vscode|notepad|excel|word|slack|discord)\b", 85),
            ],
            IntentType.FILE_OPERATION: [
                (
                    r"(?:create|make|new)\s+(?:file|folder|directory)\s+(?:named\s+)?(.+?)\s*$",
                    95,
                ),
                (r"(?:delete|remove|trash)\s+(?:file\s+)?(.+?)\s*$", 90),
                (r"(?:copy|move|rename)\s+(.+?)\s+to\s+(.+?)\s*$", 88),
                (
                    r"(?:find|locate)\s+(?:file|folder)\s+(?:named\s+)?(.+?)\s*$",
                    82,
                ),
            ],
            IntentType.EXECUTE_CODE: [
                (
                    r"(?:run|execute)\s+(?:the\s+)?(?:code|script|program|file)\s+(.+?)\s*$",
                    95,
                ),
                (
                    r"(?:write|create|make)\s+(?:a\s+)?(?:python|javascript|java|c\+\+)\s+(?:script|program)\s*(?:that\s+)?(.+?)\s*$",
                    90,
                ),
                (r"(?:install|pip\s+install)\s+(.+?)\s*$", 85),
            ],
            IntentType.SEARCH: [
                (
                    r"(?:search|google|find)\s+(?:for\s+)?(.+?)\s+(?:on|using)\s+(google|bing|duckduckgo)\s*$",
                    93,
                ),
                (
                    r"(?:what\s+is|who\s+is|when\s+is|where\s+is)\s+(.+?)\s*\??\s*$",
                    87,
                ),
            ],
            IntentType.SCHEDULE_TASK: [
                (
                    r"(?:remind|tell)\s+me\s+(?:to\s+)?(.+?)\s+(?:at|in|after)\s+(.+?)\s*$",
                    95,
                ),
                (r"(?:schedule|plan|set\s+up)\s+(.+?)\s+for\s+(.+?)\s*$", 90),
            ],
            IntentType.SYSTEM_COMMAND: [
                (
                    r"(?:run|execute)\s+(?:the\s+)?(?:command|cmd)\s+[\"']?(.+?)[\"']?\s*$",
                    95,
                ),
                (
                    r"(?:check|show|list)\s+(?:my\s+)?(?:processes|files|folders|ports)\s*$",
                    85,
                ),
            ],
        }

    @staticmethod
    def _match_greeting(text: str) -> Optional[str]:
        """Return the matched greeting phrase, or None."""
        for pattern in GREETING_PATTERNS:
            m = re.search(pattern, text)
            if m:
                return m.group(0).strip()
        return None

    def _make_intent(
        self,
        intent_type: IntentType,
        params: Dict[str, str],
        original_input: str,
        knowledge_context: str,
        confidence: float,
    ) -> Dict:
        return {
            "type": intent_type,
            "confidence": confidence,
            "original_input": original_input,
            "parameters": params,
            "knowledge_context": knowledge_context,
            "requires_llm_clarification": False,
        }

    def parse(self, user_input: str, knowledge_context: str = "") -> Dict:
        """
        Parse intent with minimal LLM.
        Returns structured intent that code can execute.
        """
        normalized = user_input.lower().strip()
        self.kb.clear_expired_context()

        # Fast-path 1: greetings & small talk -> instant Python reply.
        greeting = self._match_greeting(normalized)
        if greeting:
            logger.info(f"Parsed intent: greeting (phrase: {greeting!r})")
            return self._make_intent(
                IntentType.GREETING,
                {"phrase": greeting},
                user_input,
                knowledge_context,
                confidence=0.95,
            )

        # Fast-path 2: "<open-verb> <known app>"  -> OPEN_APP.
        # Apps win over web shortcuts so "open google chrome" launches the
        # browser, but "open google" still falls through to the URL.
        if re.search(rf"\b{OPEN_VERBS}\b", normalized):
            cleaned = strip_fillers(normalized)
            canonical = find_known_app(cleaned)
            if canonical:
                logger.info(f"Parsed intent: open_app via registry -> {canonical}")
                return self._make_intent(
                    IntentType.OPEN_APP,
                    {"target": canonical},
                    user_input,
                    knowledge_context,
                    confidence=0.95,
                )

            # Fast-path 3: "<open-verb> <known web shortcut>"  -> URL.
            web_url = find_web_shortcut(cleaned)
            if web_url:
                logger.info(f"Parsed intent: open_url via web-shortcut -> {web_url}")
                return self._make_intent(
                    IntentType.OPEN_URL,
                    {"target": web_url},
                    user_input,
                    knowledge_context,
                    confidence=0.95,
                )

        # ----- Standard regex pass -----
        best_intent = IntentType.UNKNOWN
        best_priority = -1
        best_match: Optional[re.Match] = None

        for intent_type, patterns in self.intent_patterns.items():
            for pattern, priority in patterns:
                match = re.search(pattern, normalized)
                if match and priority > best_priority:
                    best_intent = intent_type
                    best_priority = priority
                    best_match = match

        # Extract parameters from matched groups
        params: Dict[str, str] = {}
        if best_match:
            groups = [g for g in best_match.groups() if g is not None]
            if len(groups) == 1:
                params["target"] = groups[0].strip()
            elif len(groups) >= 2:
                params["source"] = groups[0].strip()
                params["target"] = groups[1].strip()

        # If we landed on OPEN_APP via regex, scrub filler words off the target.
        if best_intent == IntentType.OPEN_APP and "target" in params:
            cleaned_target = strip_fillers(params["target"])
            canonical = find_known_app(cleaned_target)
            params["target"] = canonical or cleaned_target

        intent_result = {
            "type": best_intent,
            "confidence": max(best_priority, 0) / 100.0,
            "original_input": user_input,
            "parameters": params,
            "knowledge_context": knowledge_context,
            "requires_llm_clarification": best_intent == IntentType.UNKNOWN
            or best_priority < 60,
        }

        logger.info(
            f"Parsed intent: {best_intent.value} (confidence: {max(best_priority, 0)}%)"
        )
        return intent_result
