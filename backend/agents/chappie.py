"""
CHAPPIE - The main orchestrator.
Routes between parser, executor, and response generator.
"""

import uuid
from typing import Dict, List

from core.execution_engine import ExecutionEngine
from core.file_indexer import FileIndexer
from core.intent_parser import IntentParser, IntentType
from core.knowledge_base import KnowledgeBase
from core.planner import Planner
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
        self.indexer = FileIndexer(self.kb)
        self.llm = OllamaHandler(config)
        self.parser = IntentParser(self.kb)
        self.planner = Planner(self.kb, self.parser, self.llm)
        self.executor = ExecutionEngine(self.kb, config, indexer=self.indexer)
        self.response_gen = ResponseGenerator(self.llm, self.kb)

        logger.info(f"CHAPPIE initialized (session: {self.session_id})")

    async def process(self, user_input: str) -> str:
        """Plan -> execute -> respond. LLM only used for ambiguous fallbacks."""
        logger.info(f"Processing: {user_input}")

        knowledge_context = self.kb.build_context_prompt(user_input)

        # 1. Build a plan (1 to N steps).
        plan = await self.planner.plan(user_input, knowledge_context)
        logger.info(f"Plan: {[s['description'] for s in plan]}")

        # 2. Execute each step in order. Stop the chain on a hard failure
        #    (but still let clarifications flow through).
        results: List[Dict] = []
        for step in plan:
            intent_dict = {
                "type": step["intent"],
                "parameters": dict(step.get("parameters", {})),
                "original_input": user_input,
                "knowledge_context": knowledge_context,
            }
            result = await self.executor.execute(intent_dict)
            result.setdefault("original_input", user_input)
            results.append(result)

            if (
                not result.get("success")
                and result.get("action") != "clarification_needed"
            ):
                break

        # 3. Generate a single user-facing response.
        response = await self.response_gen.generate_multi(
            results, knowledge_context, user_input
        )

        # 4. Log + learn.
        overall_success = bool(results) and all(r.get("success") for r in results)
        self.kb.log_interaction(
            user_input=user_input,
            agent_response=response,
            tools_used=[r.get("action", "unknown") for r in results],
            context_used=self.kb.get_session_context(),
            success=overall_success,
            session_id=self.session_id,
        )
        for r in results:
            self._extract_and_learn(user_input, r)

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
