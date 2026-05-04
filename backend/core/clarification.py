"""
Per-session pending clarifications.

When the planner can't fully resolve a request, it returns a clarifying
question instead of a plan. We remember the original query plus any
answers the user has given so far, and on the next turn we treat the
user's input as the answer, append it, and replan.
"""

import json
from datetime import datetime
from typing import Dict, List, Optional


MAX_CLARIFY_ROUNDS = 3


class ClarificationStore:
    """SQLite-backed pending-clarification tracker (one row per session)."""

    def __init__(self, kb):
        self.kb = kb
        self._init_schema()

    def _init_schema(self):
        cursor = self.kb.conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_clarifications (
                session_id TEXT PRIMARY KEY,
                original_query TEXT NOT NULL,
                accumulated_answers TEXT NOT NULL,
                rounds INTEGER NOT NULL,
                last_question TEXT,
                created_at TIMESTAMP
            )
            """
        )
        self.kb.conn.commit()

    def get(self, session_id: str) -> Optional[Dict]:
        cursor = self.kb.conn.cursor()
        cursor.execute(
            "SELECT * FROM pending_clarifications WHERE session_id = ?",
            (session_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "session_id": row["session_id"],
            "original_query": row["original_query"],
            "answers": json.loads(row["accumulated_answers"] or "[]"),
            "rounds": row["rounds"],
            "last_question": row["last_question"],
            "created_at": row["created_at"],
        }

    def set(
        self,
        session_id: str,
        original_query: str,
        answers: List[str],
        rounds: int,
        last_question: str,
    ):
        cursor = self.kb.conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO pending_clarifications
            (session_id, original_query, accumulated_answers, rounds, last_question, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                original_query,
                json.dumps(answers),
                rounds,
                last_question,
                datetime.now(),
            ),
        )
        self.kb.conn.commit()

    def clear(self, session_id: str):
        cursor = self.kb.conn.cursor()
        cursor.execute(
            "DELETE FROM pending_clarifications WHERE session_id = ?",
            (session_id,),
        )
        self.kb.conn.commit()

    def build_enriched_query(self, pending: Dict, latest_answer: str = "") -> str:
        """Combine the original query with all accumulated answers."""
        all_answers = list(pending["answers"])
        if latest_answer:
            all_answers.append(latest_answer)
        if not all_answers:
            return pending["original_query"]
        suffix = " (clarifications: " + "; ".join(all_answers) + ")"
        return pending["original_query"] + suffix
