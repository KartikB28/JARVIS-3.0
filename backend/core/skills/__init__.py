"""
Skill plugin system.

Each skill is a small, self-contained Python class that handles one
category of task — writing documents, drafting emails, scheduling events,
taking notes, etc.  The pattern keeps CHAPPIE extensible: adding a new
capability is one new file plus one line in the registry.

A skill provides:
  - `name`            unique slug (used by the LLM planner to route)
  - `description`     human-readable summary the LLM sees in its prompt
  - `triggers`        list of regex patterns for the cheap fast-path
  - `match(text)`     returns parsed params dict if a trigger fits, else None
  - `execute(params, context)`  the actual work; returns a result dict
"""

import re
from typing import Dict, List, Optional, Tuple


class Skill:
    """Base class for CHAPPIE skills."""

    name: str = ""
    description: str = ""
    triggers: List[str] = []

    def __init__(self, kb, config: Dict, llm=None):
        self.kb = kb
        self.config = config
        self.llm = llm

    # --- routing ---

    def match(self, text: str) -> Optional[Dict]:
        """Return parsed params if any trigger matches, else None."""
        for pattern in self.triggers:
            m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if m:
                return self._extract_params(m, text)
        return None

    def _extract_params(self, match: re.Match, text: str) -> Dict:
        """Default: use named groups; fall back to {target: full match group 1}."""
        gd = match.groupdict() or {}
        cleaned = {k: (v or "").strip() for k, v in gd.items() if v}
        if cleaned:
            return cleaned
        if match.groups():
            return {"target": match.group(1).strip()}
        return {"target": text.strip()}

    # --- execution ---

    async def execute(self, params: Dict, context: Optional[Dict] = None) -> Dict:
        raise NotImplementedError(f"Skill {self.name} did not implement execute()")


class SkillRegistry:
    """Holds the active set of Skill instances and dispatches to them."""

    def __init__(self):
        self._skills: Dict[str, Skill] = {}

    def register(self, skill: Skill):
        if not skill.name:
            raise ValueError(f"Skill {type(skill).__name__} has no name")
        self._skills[skill.name] = skill

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def find_match(self, text: str) -> Optional[Tuple[Skill, Dict]]:
        """Linear scan for the first skill whose trigger fits `text`."""
        for skill in self._skills.values():
            params = skill.match(text)
            if params is not None:
                return (skill, params)
        return None

    def list_descriptions(self) -> List[Dict]:
        return [
            {"name": s.name, "description": s.description}
            for s in self._skills.values()
        ]

    def names(self) -> List[str]:
        return list(self._skills.keys())
