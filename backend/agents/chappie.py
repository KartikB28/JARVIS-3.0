"""
CHAPPIE - The main orchestrator.
Routes between parser, executor, and response generator.
"""

import uuid
from typing import Dict

from core.execution_engine import ExecutionEngine
from core.intent_parser import IntentParser
from core.knowledge_base import KnowledgeBase
from core.response_generator import ResponseGenerator
from models.llm_handler import OllamaHandler
from utils.logger import setup_logger

logger = setup_logger(__name__)


class CHAPPIE:
    """
    Main AI agent orchestrator.

    Architecture:
        1. User Input
        2. LLM translates to intent (lightweight; only when parser is unsure)
        3. Parser extracts structured intent (Python)
        4. Executor runs tools (Python)
        5. Response generator formats output (minimal LLM)
        6. Store in knowledge base (learn)
    """

    def __init__(self, config: Dict):
        self.config = config
        self.session_id = str(uuid.uuid4())

        kb_cfg = config.get("knowledge_base", {})
        db_path = kb_cfg.get("db_path", "data/chappie.db")

        self.kb = KnowledgeBase(db_path=db_path)
        self.llm = OllamaHandler(config)
        self.parser = IntentParser(self.kb)
        self.executor = ExecutionEngine(self.kb, config)
        self.response_gen = ResponseGenerator(self.llm, self.kb)

        logger.info(f"CHAPPIE initialized (session: {self.session_id})")

    async def process(self, user_input: str) -> str:
        """Main processing loop. Minimal LLM usage for translation only."""
        logger.info(f"Processing: {user_input}")

        # 1. Pull context from the knowledge base
        knowledge_context = self.kb.build_context_prompt(user_input)

        # 2. Parse intent (Python-heavy)
        intent = self.parser.parse(user_input, knowledge_context)

        # 3. Execute (100% Python)
        execution_result = await self.executor.execute(intent)
        execution_result.setdefault("original_input", user_input)

        # 4. Generate response (minimal LLM)
        response = await self.response_gen.generate(execution_result, knowledge_context)

        # 5. Log interaction for learning
        self.kb.log_interaction(
            user_input=user_input,
            agent_response=response,
            tools_used=[execution_result.get("action", "unknown")],
            context_used=self.kb.get_session_context(),
            success=execution_result.get("success", False),
            session_id=self.session_id,
        )

        # 6. Auto-extract patterns
        self._extract_and_learn(user_input, execution_result)

        logger.info(f"Response: {response}")
        return response

    def _extract_and_learn(self, user_input: str, execution_result: Dict):
        """Auto-extract preferences and patterns from interactions."""
        action = execution_result.get("action")

        if action == "open_file":
            self.kb.track_resource(execution_result.get("file", ""), "file")
        elif action == "open_app":
            self.kb.learn_app_pattern(
                execution_result.get("app", ""),
                "open",
                execution_result.get("success", False),
            )
        elif action == "open_url":
            self.kb.track_resource(execution_result.get("url", ""), "url")

    def get_status(self) -> Dict:
        """Get CHAPPIE's current status."""
        preferences = self.kb.get_all_preferences()
        skills = self.kb.get_learned_skills()
        resources = self.kb.get_frequent_resources(limit=5)

        return {
            "session_id": self.session_id,
            "preferences_learned": len(preferences),
            "skills_learned": len(skills),
            "frequent_resources": [r["resource"] for r in resources],
            "top_skills": list(skills.keys())[:3] if skills else [],
        }

    def shutdown(self):
        """Clean up resources."""
        self.kb.close()
