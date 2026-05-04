"""
CHAPPIE - The main orchestrator.
Routes between parser, executor, and response generator.
"""

import uuid
from typing import Dict, List

from core.browser_agent import BrowserAgent
from core.clarification import MAX_CLARIFY_ROUNDS, ClarificationStore
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
        self.clarification_store = ClarificationStore(self.kb)
        self.browser_agent = BrowserAgent(self.kb, config)
        self.llm = OllamaHandler(config)
        self.parser = IntentParser(self.kb)
        self.planner = Planner(self.kb, self.parser, self.llm)
        self.executor = ExecutionEngine(
            self.kb,
            config,
            indexer=self.indexer,
            browser_agent=self.browser_agent,
        )
        self.response_gen = ResponseGenerator(self.llm, self.kb)

        logger.info(f"CHAPPIE initialized (session: {self.session_id})")

    async def process(self, user_input: str) -> str:
        """Plan -> execute -> respond, threading clarifications across turns."""
        logger.info(f"Processing: {user_input}")

        # If there's a pending clarification for this session, treat the
        # incoming message as the user's answer to it and merge before planning.
        pending = self.clarification_store.get(self.session_id)
        if pending:
            return await self._handle_pending_clarification(pending, user_input)

        knowledge_context = self.kb.build_context_prompt(user_input)
        plan = await self.planner.plan(user_input, knowledge_context)
        logger.info(f"Plan: {[s['description'] for s in plan]}")

        # If the planner asked for clarification, store the question and bail early.
        if plan and plan[0]["intent"] == IntentType.CLARIFY:
            question = plan[0]["parameters"].get(
                "question", "Could you give me a bit more detail?"
            )
            self.clarification_store.set(
                self.session_id,
                original_query=user_input,
                answers=[],
                rounds=1,
                last_question=question,
            )
            self._log(user_input, question, ["clarify"], success=True)
            return question

        return await self._execute_and_respond(plan, user_input, knowledge_context)

    async def _handle_pending_clarification(
        self, pending: Dict, user_input: str
    ) -> str:
        """User just answered a clarifying question — merge and replan."""
        new_answers = list(pending["answers"]) + [user_input]
        rounds = pending["rounds"]

        # If we've asked too many rounds, give up clarifying and just try to
        # execute with what we have. Better to do something than loop forever.
        give_up = rounds >= MAX_CLARIFY_ROUNDS

        enriched = self.clarification_store.build_enriched_query(
            pending, latest_answer=user_input
        )
        knowledge_context = self.kb.build_context_prompt(enriched)
        plan = await self.planner.plan(enriched, knowledge_context)
        logger.info(
            f"Re-plan after clarification (round {rounds}): "
            f"{[s['description'] for s in plan]}"
        )

        if not give_up and plan and plan[0]["intent"] == IntentType.CLARIFY:
            new_question = plan[0]["parameters"].get(
                "question", "Could you give me a bit more detail?"
            )
            self.clarification_store.set(
                self.session_id,
                original_query=pending["original_query"],
                answers=new_answers,
                rounds=rounds + 1,
                last_question=new_question,
            )
            self._log(user_input, new_question, ["clarify"], success=True)
            return new_question

        # We have a real plan now (or we're giving up). Either way, clear
        # pending state and execute.
        self.clarification_store.clear(self.session_id)

        if give_up and plan and plan[0]["intent"] == IntentType.CLARIFY:
            # Strip the clarification step; substitute a search as a last resort.
            plan = [
                {
                    "intent": IntentType.SEARCH,
                    "parameters": {"target": pending["original_query"]},
                    "description": "fallback search",
                }
            ]

        return await self._execute_and_respond(
            plan, user_input, knowledge_context, original_query=enriched
        )

    async def _execute_and_respond(
        self,
        plan: List[Dict],
        user_input: str,
        knowledge_context: str,
        original_query: str = None,
    ) -> str:
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

        response = await self.response_gen.generate_multi(
            results, knowledge_context, original_query or user_input
        )

        overall_success = bool(results) and all(r.get("success") for r in results)
        self._log(
            user_input,
            response,
            [r.get("action", "unknown") for r in results],
            success=overall_success,
        )
        for r in results:
            self._extract_and_learn(user_input, r)
        logger.info(f"Response: {response}")
        return response

    def _log(
        self, user_input: str, response: str, tools: List[str], success: bool
    ):
        self.kb.log_interaction(
            user_input=user_input,
            agent_response=response,
            tools_used=tools,
            context_used=self.kb.get_session_context(),
            success=success,
            session_id=self.session_id,
        )

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

    async def async_shutdown(self):
        """Async-safe shutdown that also closes the browser."""
        try:
            await self.browser_agent.close()
        except Exception as exc:
            logger.warning(f"Browser shutdown failed: {exc}")
        self.kb.close()
