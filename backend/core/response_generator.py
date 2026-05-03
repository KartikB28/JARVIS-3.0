"""
Convert execution results into natural language responses.
Minimal LLM involvement - mostly formatting.
"""

from typing import Dict

from models.llm_handler import OllamaHandler
from utils.logger import setup_logger

logger = setup_logger(__name__)


class ResponseGenerator:
    """Generate conversational responses from execution results."""

    def __init__(self, llm: OllamaHandler, knowledge_base):
        self.llm = llm
        self.kb = knowledge_base

    async def generate(self, execution_result: Dict, knowledge_context: str = "") -> str:
        """
        Convert structured execution result into natural response.
        Only place we use the LLM for text generation (and only for ambiguity).
        """
        if not execution_result.get("success", False) and execution_result.get("action") != "clarification_needed":
            return self._generate_error_response(execution_result)

        action = execution_result.get("action", "unknown")

        if action == "open_app":
            return f"I've opened {execution_result.get('app', 'the application')} for you."

        if action == "open_file":
            return f"Opening {execution_result.get('file', 'your file')} now."

        if action == "open_url":
            return f"Navigating to {execution_result.get('url', 'that website')}..."

        if action == "create_file":
            return f"Created {execution_result.get('file', 'the file')}."

        if action == "delete_file":
            return f"Deleted {execution_result.get('file', 'the file')}."

        if action == "copy_file":
            return f"Copied {execution_result.get('from')} to {execution_result.get('to')}."

        if action == "move_file":
            return f"Moved {execution_result.get('from')} to {execution_result.get('to')}."

        if action == "find_file":
            matches = execution_result.get("matches", [])
            if not matches:
                return "I didn't find any matching files."
            preview = ", ".join(matches[:3])
            extra = f" (and {len(matches) - 3} more)" if len(matches) > 3 else ""
            return f"Found {len(matches)} matching files: {preview}{extra}"

        if action == "search":
            return f"Searching for '{execution_result.get('query', 'that')}'..."

        if action == "learn_preference":
            return (
                f"Got it! I'll remember that your "
                f"{execution_result.get('key')} is {execution_result.get('value')}."
            )

        if action == "query_knowledge":
            return execution_result.get("answer") or execution_result.get(
                "message", "I need more information."
            )

        if action == "schedule_task":
            return execution_result.get("message", "Task scheduled.")

        if action == "system_command":
            output = execution_result.get("output", "Command completed")
            return f"Command output: {output}"

        if action == "execute_code":
            return execution_result.get("message", "Code executed.")

        if action == "clarification_needed":
            return await self._generate_clarification_prompt(execution_result)

        return execution_result.get("message", "Task completed.")

    def _generate_error_response(self, result: Dict) -> str:
        """Generate friendly error message."""
        error = result.get("error", "Unknown error")
        action = result.get("action", "operation")

        canned = {
            "File not found": "I couldn't find that file.",
            "Dangerous operation detected": "That operation seems risky, so I'm blocking it.",
            "Dangerous command blocked": "That command looks dangerous, so I'm not running it.",
            "Command timeout": "That command is taking too long.",
            "Permission denied": "I don't have permission to do that.",
            "No app specified": "Which app would you like me to open?",
        }
        return canned.get(error, f"Error during {action}: {error}")

    async def _generate_clarification_prompt(self, result: Dict) -> str:
        """Use LLM only for ambiguous clarifications."""
        user_input = result.get("original_input", "your request")
        prompt = (
            "The user's intent is unclear. Generate a brief, friendly response asking "
            "for clarification. Be specific about what information you need. "
            "Keep it to 2 sentences max.\n\n"
            f"Original input: {user_input}\n\nResponse:"
        )

        response = await self.llm.generate(
            messages=[{"role": "user", "content": prompt}],
            system="You are CHAPPIE, a helpful AI assistant. Ask clarifying questions concisely.",
            temperature=0.5,
        )

        if not response:
            return (
                f"I'm not sure what you mean by \"{user_input}\". "
                "Could you rephrase that?"
            )
        return response.strip()
