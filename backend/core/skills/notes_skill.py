"""
Notes skill: quick capture, persists across sessions.

Notes go into:
  - the SQLite KB (so the planner can recall them later)
  - a single ~/Documents/CHAPPIE/notes.md (so the user can grep them)
"""

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from core.skills import Skill
from utils.logger import setup_logger

logger = setup_logger(__name__)


class NotesSkill(Skill):
    name = "notes"
    description = (
        "Capture a quick note. Persists across sessions and is appended to "
        "~/Documents/CHAPPIE/notes.md."
    )
    triggers = [
        # "note: buy milk" / "note - buy milk"
        r"^(?:note|memo)\s*[:\-]\s*(?P<note>.+?)\s*$",
        # "remember to buy milk" / "remind me to buy milk" (without time)
        r"^(?:remember|jot\s+down|write\s+down)\s+(?:to\s+|that\s+)?(?P<note>.+?)\s*$",
        # "save this note: ..."
        r"^save\s+(?:this\s+)?note\s*[:\-]\s*(?P<note>.+?)\s*$",
        # "make a note (that|to) X"
        r"^(?:make|add|create|take)\s+(?:a\s+)?note\s+(?:that\s+|to\s+)?(?P<note>.+?)\s*$",
    ]

    def __init__(self, kb, config: Dict, llm=None):
        super().__init__(kb, config, llm)
        skills_cfg = config.get("skills", {})
        self.save_dir = os.path.expanduser(
            skills_cfg.get("notes_dir", "~/Documents/CHAPPIE")
        )
        Path(self.save_dir).mkdir(parents=True, exist_ok=True)
        self.notes_file = os.path.join(self.save_dir, "notes.md")

    async def execute(self, params: Dict, context: Optional[Dict] = None) -> Dict:
        text = (params.get("note") or "").strip()
        if not text:
            return {
                "success": False,
                "skill": self.name,
                "message": "What would you like me to note?",
            }

        # Persist in KB with a counter-based key so they don't collide.
        counter_str = self.kb.get_preference("_notes_counter") or "0"
        try:
            n = int(counter_str) + 1
        except ValueError:
            n = 1
        key = f"note_{n:04d}"
        self.kb.learn_preference(key, text, "note", confidence=1.0)
        self.kb.learn_preference("_notes_counter", str(n), "system", confidence=1.0)

        # Append to notes.md
        try:
            with open(self.notes_file, "a", encoding="utf-8") as f:
                f.write(f"\n## {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{text}\n")
        except OSError as exc:
            logger.warning(f"Couldn't append to notes.md: {exc}")

        preview = text if len(text) <= 80 else text[:77] + "..."
        return {
            "success": True,
            "skill": self.name,
            "action": "note_saved",
            "note": text,
            "message": f'Noted: "{preview}"',
        }
