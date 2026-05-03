"""
CHAPPIE's long-term memory system.
Persists across all sessions and learns from interactions.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from utils.logger import setup_logger

logger = setup_logger(__name__)


class KnowledgeBase:
    """Persistent, queryable knowledge system."""

    def __init__(self, db_path: str = "data/chappie.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False so the same connection works across the
        # FastAPI threadpool / asyncio executor. Writes are serialized by
        # SQLite's own locking.
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self):
        """Initialize database schema."""
        cursor = self.conn.cursor()

        # User preferences & learning
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS user_preferences (
                key TEXT PRIMARY KEY,
                value TEXT,
                category TEXT,
                learned_at TIMESTAMP,
                confidence FLOAT
            )
            """
        )

        # Conversation history (indexed for fast retrieval)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY,
                timestamp TIMESTAMP,
                user_input TEXT,
                agent_response TEXT,
                tools_used TEXT,
                context_used TEXT,
                success INTEGER,
                session_id TEXT
            )
            """
        )

        # Application patterns (learn what user does with each app)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS app_patterns (
                app_name TEXT,
                action TEXT,
                frequency INTEGER,
                success_rate FLOAT,
                last_used TIMESTAMP,
                PRIMARY KEY (app_name, action)
            )
            """
        )

        # URLs and frequently accessed resources
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS frequent_resources (
                resource TEXT PRIMARY KEY,
                resource_type TEXT,
                access_count INTEGER,
                last_accessed TIMESTAMP,
                category TEXT
            )
            """
        )

        # Skills/capabilities CHAPPIE has learned
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS learned_skills (
                skill_name TEXT PRIMARY KEY,
                description TEXT,
                tool_sequence TEXT,
                success_count INTEGER,
                failure_count INTEGER,
                last_used TIMESTAMP
            )
            """
        )

        # Context cache (current session memory)
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS context_cache (
                key TEXT PRIMARY KEY,
                value TEXT,
                timestamp TIMESTAMP,
                ttl INTEGER
            )
            """
        )

        # Indexes for performance
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON conversations(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_app ON app_patterns(app_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_session ON conversations(session_id)")

        self.conn.commit()
        logger.info("Knowledge base initialized")

    # ==================== USER PREFERENCES ====================

    def learn_preference(self, key: str, value: str, category: str, confidence: float = 0.9):
        """Learn user preferences with confidence scoring."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO user_preferences
            (key, value, category, learned_at, confidence)
            VALUES (?, ?, ?, ?, ?)
            """,
            (key, value, category, datetime.now(), confidence),
        )
        self.conn.commit()
        logger.info(f"Learned preference: {key} = {value} (conf: {confidence})")

    def get_preference(self, key: str) -> Optional[str]:
        """Retrieve learned preference."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT value FROM user_preferences WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else None

    def get_all_preferences(self, category: Optional[str] = None) -> Dict[str, str]:
        """Get all learned preferences, optionally filtered by category."""
        cursor = self.conn.cursor()
        if category:
            cursor.execute(
                "SELECT key, value FROM user_preferences WHERE category = ?", (category,)
            )
        else:
            cursor.execute("SELECT key, value FROM user_preferences")
        return {row["key"]: row["value"] for row in cursor.fetchall()}

    # ==================== CONTEXT WINDOW MANAGEMENT ====================

    def add_to_context(self, key: str, value: str, ttl: int = 3600):
        """
        Add to current session context.
        TTL: time-to-live in seconds (default 1 hour).
        """
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO context_cache
            (key, value, timestamp, ttl)
            VALUES (?, ?, ?, ?)
            """,
            (key, value, datetime.now(), ttl),
        )
        self.conn.commit()

    def get_context(self, key: str) -> Optional[str]:
        """Retrieve from context cache (None if expired)."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT value FROM context_cache
            WHERE key = ?
              AND datetime(timestamp, '+' || ttl || ' seconds') > datetime('now')
            """,
            (key,),
        )
        row = cursor.fetchone()
        return row["value"] if row else None

    def get_session_context(self) -> Dict[str, str]:
        """Get all valid (unexpired) context for current session."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT key, value FROM context_cache
            WHERE datetime(timestamp, '+' || ttl || ' seconds') > datetime('now')
            """
        )
        return {row["key"]: row["value"] for row in cursor.fetchall()}

    def clear_expired_context(self):
        """Clean up expired context entries."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            DELETE FROM context_cache
            WHERE datetime(timestamp, '+' || ttl || ' seconds') <= datetime('now')
            """
        )
        self.conn.commit()

    # ==================== CONVERSATION HISTORY ====================

    def log_interaction(
        self,
        user_input: str,
        agent_response: str,
        tools_used: List[str],
        context_used: Dict,
        success: bool,
        session_id: str,
    ):
        """Log interaction for learning."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO conversations
            (timestamp, user_input, agent_response, tools_used, context_used, success, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(),
                user_input,
                agent_response,
                json.dumps(tools_used),
                json.dumps(context_used, default=str),
                int(success),
                session_id,
            ),
        )
        self.conn.commit()

    def get_relevant_history(self, query: str, limit: int = 5) -> List[Dict]:
        """
        Retrieve relevant past interactions.
        Used to inject into context window for pattern recognition.
        """
        cursor = self.conn.cursor()
        keywords = [k for k in query.lower().split() if k]
        if not keywords:
            return []

        # Build a dynamic OR-clause across keywords (capped to avoid huge queries).
        keywords = keywords[:5]
        like_clause = " OR ".join(["user_input LIKE ?"] * len(keywords))
        params = [f"%{k}%" for k in keywords] + [limit]

        cursor.execute(
            f"""
            SELECT user_input, agent_response, tools_used, success
            FROM conversations
            WHERE {like_clause}
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            params,
        )
        return [dict(row) for row in cursor.fetchall()]

    # ==================== APP PATTERN LEARNING ====================

    def learn_app_pattern(self, app_name: str, action: str, success: bool):
        """Learn what actions work with which apps."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT frequency, success_rate FROM app_patterns
            WHERE app_name = ? AND action = ?
            """,
            (app_name, action),
        )
        existing = cursor.fetchone()

        if existing:
            freq = existing["frequency"] + 1
            old_rate = existing["success_rate"]
            new_rate = (old_rate * (freq - 1) + int(success)) / freq

            cursor.execute(
                """
                UPDATE app_patterns
                SET frequency = ?, success_rate = ?, last_used = ?
                WHERE app_name = ? AND action = ?
                """,
                (freq, new_rate, datetime.now(), app_name, action),
            )
        else:
            cursor.execute(
                """
                INSERT INTO app_patterns
                (app_name, action, frequency, success_rate, last_used)
                VALUES (?, ?, 1, ?, ?)
                """,
                (app_name, action, float(int(success)), datetime.now()),
            )
        self.conn.commit()
        logger.info(f"Learned app pattern: {app_name} -> {action} (success: {success})")

    def get_app_expertise(self, app_name: str) -> Dict:
        """Get what CHAPPIE has learned about an app."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT action, frequency, success_rate
            FROM app_patterns
            WHERE app_name = ?
            ORDER BY frequency DESC
            """,
            (app_name,),
        )
        return {
            row["action"]: {
                "frequency": row["frequency"],
                "success_rate": row["success_rate"],
            }
            for row in cursor.fetchall()
        }

    # ==================== RESOURCE TRACKING ====================

    def track_resource(self, resource: str, resource_type: str, category: Optional[str] = None):
        """Track frequently accessed files/URLs."""
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT access_count FROM frequent_resources WHERE resource = ?", (resource,)
        )
        existing = cursor.fetchone()

        if existing:
            cursor.execute(
                """
                UPDATE frequent_resources
                SET access_count = access_count + 1, last_accessed = ?
                WHERE resource = ?
                """,
                (datetime.now(), resource),
            )
        else:
            cursor.execute(
                """
                INSERT INTO frequent_resources
                (resource, resource_type, access_count, last_accessed, category)
                VALUES (?, ?, 1, ?, ?)
                """,
                (resource, resource_type, datetime.now(), category),
            )
        self.conn.commit()

    def get_frequent_resources(self, limit: int = 10) -> List[Dict]:
        """Get most frequently accessed resources."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT resource, resource_type, access_count, category
            FROM frequent_resources
            ORDER BY access_count DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

    # ==================== SKILL LEARNING ====================

    def register_learned_skill(
        self, skill_name: str, description: str, tool_sequence: List[str]
    ):
        """Register a new skill CHAPPIE has learned."""
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO learned_skills
            (skill_name, description, tool_sequence, success_count, failure_count, last_used)
            VALUES (?, ?, ?, 0, 0, ?)
            """,
            (skill_name, description, json.dumps(tool_sequence), datetime.now()),
        )
        self.conn.commit()
        logger.info(f"Registered learned skill: {skill_name}")

    def update_skill_performance(self, skill_name: str, success: bool):
        """Update success/failure counts for learned skills."""
        cursor = self.conn.cursor()
        if success:
            cursor.execute(
                """
                UPDATE learned_skills
                SET success_count = success_count + 1, last_used = ?
                WHERE skill_name = ?
                """,
                (datetime.now(), skill_name),
            )
        else:
            cursor.execute(
                """
                UPDATE learned_skills
                SET failure_count = failure_count + 1, last_used = ?
                WHERE skill_name = ?
                """,
                (datetime.now(), skill_name),
            )
        self.conn.commit()

    def get_learned_skills(self) -> Dict[str, Dict]:
        """Get all skills CHAPPIE has learned."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM learned_skills ORDER BY success_count DESC")

        skills = {}
        for row in cursor.fetchall():
            total = row["success_count"] + row["failure_count"]
            skills[row["skill_name"]] = {
                "description": row["description"],
                "tools": json.loads(row["tool_sequence"]),
                "success_count": row["success_count"],
                "failure_count": row["failure_count"],
                "success_rate": row["success_count"] / total if total > 0 else 0.0,
            }
        return skills

    # ==================== CONTEXT INJECTION ====================

    def build_context_prompt(self, user_input: str) -> str:
        """
        Build a comprehensive context string for the LLM.
        Injected into prompts so CHAPPIE makes better decisions.
        """
        parts: List[str] = []

        # 1. User preferences
        prefs = self.get_all_preferences()
        if prefs:
            parts.append("## KNOWN USER PREFERENCES:")
            for key, value in list(prefs.items())[:5]:
                parts.append(f"- {key}: {value}")

        # 2. Current session context
        session_context = self.get_session_context()
        if session_context:
            parts.append("\n## CURRENT SESSION CONTEXT:")
            for key, value in session_context.items():
                parts.append(f"- {key}: {value}")

        # 3. Relevant past interactions
        history = self.get_relevant_history(user_input, limit=3)
        if history:
            parts.append("\n## RELEVANT PAST INTERACTIONS:")
            for i, interaction in enumerate(history, 1):
                snippet = interaction["user_input"][:100]
                parts.append(f"{i}. User asked: {snippet}...")
                parts.append(f"   Used tools: {interaction['tools_used']}")
                parts.append(f"   Success: {'yes' if interaction['success'] else 'no'}")

        # 4. App expertise (extract candidate app from input)
        common_apps = [
            "chrome", "firefox", "vscode", "notepad", "excel", "word",
            "git", "docker", "slack", "discord",
        ]
        relevant_app = None
        lowered = user_input.lower()
        for app in common_apps:
            if app in lowered:
                relevant_app = app
                break

        if relevant_app:
            expertise = self.get_app_expertise(relevant_app)
            if expertise:
                parts.append(f"\n## EXPERTISE WITH {relevant_app.upper()}:")
                for action, stats in expertise.items():
                    parts.append(
                        f"- {action}: success rate {stats['success_rate']:.1%}"
                    )

        # 5. Learned skills
        skills = self.get_learned_skills()
        if skills:
            parts.append("\n## LEARNED SKILLS:")
            for skill_name, skill_info in list(skills.items())[:3]:
                parts.append(
                    f"- {skill_name}: {skill_info['success_rate']:.1%} success rate"
                )

        return "\n".join(parts)

    def close(self):
        """Close database connection."""
        try:
            self.conn.close()
        except Exception:
            pass
