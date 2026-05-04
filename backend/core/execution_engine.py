"""
Execute parsed intents.
This is 100% Python-based, no LLM involved in execution.
"""

import os
import random
import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Dict

from core.app_registry import (
    find_known_app,
    find_web_shortcut,
    get_launch_command,
    strip_fillers,
)
from core.intent_parser import IntentType
from utils.logger import setup_logger

logger = setup_logger(__name__)


def _platform_opener() -> str:
    """Return the right shell command to open files/URLs on this OS."""
    if sys.platform == "darwin":
        return "open"
    return "xdg-open"


class ExecutionEngine:
    """Executes parsed intents using Python."""

    def __init__(
        self,
        knowledge_base,
        config: Dict,
        indexer=None,
        browser_agent=None,
        skills=None,
        llm=None,
        persona_provider=None,
    ):
        self.kb = knowledge_base
        self.config = config
        self.indexer = indexer  # optional FileIndexer for app/file search
        self.browser_agent = browser_agent  # optional BrowserAgent for web tasks
        self.skills = skills  # optional SkillRegistry
        self.llm = llm  # optional LLM (for CONVERSE)
        self.persona_provider = persona_provider  # callable -> persona prompt
        self.execution_history = []
        self.scheduled_tasks = []

        sec = config.get("security", {})
        self.dangerous_keywords = sec.get(
            "dangerous_keywords", ["__import__", "eval", "rm -rf", "sudo"]
        )

    async def execute(self, intent: Dict) -> Dict:
        """Route a parsed intent to its handler."""
        intent_type = intent["type"]
        params = intent["parameters"]

        logger.info(f"Executing: {intent_type.value} with params: {params}")

        try:
            if intent_type == IntentType.GREETING:
                return await self._handle_greeting(intent)
            if intent_type == IntentType.OPEN_APP:
                return await self._handle_open_app(params.get("target", ""))
            if intent_type == IntentType.OPEN_FILE:
                return await self._handle_open_file(params.get("target", ""))
            if intent_type == IntentType.OPEN_PATH:
                return await self._handle_open_path(params.get("target", ""))
            if intent_type == IntentType.OPEN_URL:
                return await self._handle_open_url(
                    params.get("target", ""),
                    browser=params.get("browser"),
                )
            if intent_type == IntentType.FILE_OPERATION:
                return await self._handle_file_operation(intent)
            if intent_type == IntentType.EXECUTE_CODE:
                return await self._handle_execute_code(params.get("target", ""))
            if intent_type == IntentType.SEARCH:
                return await self._handle_search(params.get("target", ""))
            if intent_type == IntentType.LEARN_PREFERENCE:
                return await self._handle_learn_preference(intent)
            if intent_type == IntentType.QUERY_KNOWLEDGE:
                return await self._handle_query_knowledge(params.get("target", ""))
            if intent_type == IntentType.SCHEDULE_TASK:
                return await self._handle_schedule_task(intent)
            if intent_type == IntentType.SYSTEM_COMMAND:
                return await self._handle_system_command(params.get("target", ""))
            if intent_type == IntentType.BROWSER_TASK:
                return await self._handle_browser_task(params)
            if intent_type == IntentType.CLARIFY:
                return await self._handle_clarify(params)
            if intent_type == IntentType.SKILL:
                return await self._handle_skill(params)
            if intent_type == IntentType.CONVERSE:
                return await self._handle_converse(params, intent)

            return {
                "success": False,
                "action": "clarification_needed",
                "original_input": intent.get("original_input", ""),
                "message": "I need clarification on what you want to do.",
                "requires_llm": True,
            }
        except Exception as exc:
            logger.error(f"Execution error: {exc}")
            return {
                "success": False,
                "error": str(exc),
                "message": f"Error executing task: {exc}",
            }

    # ==================== HANDLER METHODS ====================

    async def _handle_greeting(self, intent: Dict) -> Dict:
        """Reply to small talk instantly, no LLM."""
        phrase = (intent.get("parameters", {}).get("phrase") or "").lower()

        if any(k in phrase for k in ("thank", "thx", "ty", "cheers")):
            choices = ["You're welcome!", "Anytime.", "Glad I could help."]
        elif any(k in phrase for k in ("bye", "goodbye", "see you", "cya", "later")):
            choices = ["Goodbye!", "See you later.", "Take care."]
        elif "good morning" in phrase:
            choices = ["Good morning! What can I help with?", "Morning! Ready to go."]
        elif "good afternoon" in phrase:
            choices = ["Good afternoon! What's on the agenda?"]
        elif "good evening" in phrase or "good night" in phrase:
            choices = ["Good evening!", "Hope your day went well."]
        elif any(k in phrase for k in ("how are you", "how's it going", "hows it going", "what's up", "whats up")):
            choices = [
                "I'm running smoothly. What can I do for you?",
                "All systems good. What's up?",
                "Doing great. How can I help?",
            ]
        else:
            choices = [
                "Hi! What can I help with?",
                "Hey! How can I help?",
                "Hello! What can I do for you?",
            ]

        return {
            "success": True,
            "action": "greeting",
            "message": random.choice(choices),
        }

    async def _handle_open_app(self, app_name: str) -> Dict:
        """Open an application: registry -> web shortcut -> file index -> blind launch."""
        cleaned = strip_fillers(app_name or "")
        if not cleaned:
            return {
                "success": False,
                "action": "open_app",
                "error": "No app specified",
                "message": "Which app would you like me to open?",
            }

        # 1. Curated app registry (fastest, always-correct mapping per-OS).
        canonical = find_known_app(cleaned)
        if canonical:
            return await self._launch_registered(canonical)

        # 2. Known web shortcut (e.g. user said "google" -> google.com).
        web_url = find_web_shortcut(cleaned)
        if web_url:
            return await self._handle_open_url(web_url)

        # 3. Search the file index for a matching app/file/folder.
        if self.indexer:
            matches = self.indexer.search(cleaned, limit=5)
            if matches:
                return await self._open_indexed(matches[0])

        # 4. Last resort: blind launch via the shell.
        return await self._launch_registered(cleaned)

    async def _launch_registered(self, canonical: str) -> Dict:
        """Launch a name through the OS shell (uses registry command if known)."""
        cmd = get_launch_command(canonical)
        try:
            if cmd:
                subprocess.Popen(cmd, shell=True)
            else:
                if sys.platform == "win32":
                    subprocess.Popen(f"start {canonical}", shell=True)
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", "-a", canonical])
                else:
                    subprocess.Popen([canonical])

            self.kb.learn_app_pattern(canonical, "open", True)
            self.kb.track_resource(canonical, "application")

            return {
                "success": True,
                "action": "open_app",
                "app": canonical,
                "message": f"Opening {canonical}...",
            }
        except FileNotFoundError:
            self.kb.learn_app_pattern(canonical, "open", False)
            return {
                "success": False,
                "action": "open_app",
                "app": canonical,
                "error": "app_not_installed",
                "message": f"I couldn't find '{canonical}' on your system. Is it installed?",
            }
        except Exception as exc:
            self.kb.learn_app_pattern(canonical, "open", False)
            return {
                "success": False,
                "action": "open_app",
                "app": canonical,
                "error": str(exc),
                "message": f"Could not open {canonical}.",
            }

    async def _open_indexed(self, entry) -> Dict:
        """Open an item that came back from the FileIndexer."""
        path = entry.path
        if not os.path.exists(path):
            return {
                "success": False,
                "action": "open_indexed",
                "error": "Path missing",
                "message": f"I had '{entry.name}' indexed but the file is gone now.",
            }

        try:
            if sys.platform == "win32":
                # os.startfile handles .lnk shortcuts, .exe, documents, folders.
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                if entry.ext == ".desktop":
                    subprocess.Popen(["gtk-launch", os.path.basename(path)])
                else:
                    subprocess.Popen(["xdg-open", path])

            self.kb.track_resource(path, entry.kind)
            self.kb.learn_app_pattern(entry.name.lower(), "open", True)

            return {
                "success": True,
                "action": "open_indexed",
                "name": entry.name,
                "path": path,
                "kind": entry.kind,
                "location": entry.location,
                "message": f"Opening {entry.name} from {entry.location}...",
            }
        except Exception as exc:
            return {
                "success": False,
                "action": "open_indexed",
                "name": entry.name,
                "path": path,
                "error": str(exc),
                "message": f"Could not open {entry.name}.",
            }

    async def _handle_open_file(self, file_path: str) -> Dict:
        """Open a file by literal path; if not found, search the index by name."""
        try:
            expanded = os.path.expanduser(file_path)

            if os.path.exists(expanded):
                if sys.platform == "win32":
                    os.startfile(expanded)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen([_platform_opener(), expanded])

                self.kb.track_resource(expanded, "file")

                return {
                    "success": True,
                    "action": "open_file",
                    "file": expanded,
                    "message": f"Opening {file_path}...",
                }

            # Path not found literally — try the index by name.
            if self.indexer:
                matches = self.indexer.search(file_path, kind="file", limit=5)
                if matches:
                    return await self._open_indexed(matches[0])
                # Maybe the user meant a folder; widen the search.
                folder_matches = self.indexer.search(
                    file_path, kind="folder", limit=5
                )
                if folder_matches:
                    return await self._open_indexed(folder_matches[0])

            return {
                "success": False,
                "action": "open_file",
                "error": "File not found",
                "message": f"Could not find {file_path}",
            }
        except Exception as exc:
            return {"success": False, "action": "open_file", "error": str(exc)}

    async def _handle_open_url(self, url: str, browser: str = None) -> Dict:
        """Open URL in default browser, or in a specific browser if given."""
        try:
            url = (url or "").strip()
            if not url.startswith(("http://", "https://")):
                url = "https://" + url

            if browser:
                from core.app_registry import get_browser_url_command

                cmd = get_browser_url_command(browser, url)
                if cmd:
                    subprocess.Popen(cmd, shell=True)
                else:
                    webbrowser.open(url)
            else:
                webbrowser.open(url)

            self.kb.track_resource(url, "url")

            return {
                "success": True,
                "action": "open_url",
                "url": url,
                "browser": browser,
                "message": (
                    f"Opening {url} in {browser}..." if browser else f"Opening {url}..."
                ),
            }
        except Exception as exc:
            return {"success": False, "action": "open_url", "error": str(exc)}

    async def _handle_open_path(self, path: str) -> Dict:
        """Open a folder/path in the system file manager."""
        try:
            expanded = os.path.expanduser(os.path.expandvars(path or ""))
            if not expanded:
                return {
                    "success": False,
                    "action": "open_path",
                    "error": "No path specified",
                    "message": "Which folder would you like me to open?",
                }
            if not os.path.exists(expanded):
                return {
                    "success": False,
                    "action": "open_path",
                    "error": "Path not found",
                    "message": f"I couldn't find {path}.",
                }

            if sys.platform == "win32":
                subprocess.Popen(f'explorer "{expanded}"', shell=True)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", expanded])
            else:
                subprocess.Popen(["xdg-open", expanded])

            self.kb.track_resource(expanded, "folder")
            return {
                "success": True,
                "action": "open_path",
                "path": expanded,
                "message": f"Opening {expanded}...",
            }
        except Exception as exc:
            return {"success": False, "action": "open_path", "error": str(exc)}

    async def _handle_file_operation(self, intent: Dict) -> Dict:
        """Handle file create/delete/move/copy operations."""
        text = intent["original_input"].lower()
        target = intent["parameters"].get("target", "")
        source = intent["parameters"].get("source", "")

        try:
            if "create" in text or "new" in text or "make" in text:
                file_path = os.path.expanduser(target)
                parent = os.path.dirname(file_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                Path(file_path).touch()
                return {
                    "success": True,
                    "action": "create_file",
                    "file": file_path,
                    "message": f"Created {file_path}",
                }

            if "delete" in text or "remove" in text or "trash" in text:
                file_path = os.path.expanduser(target)
                if not os.path.exists(file_path):
                    return {
                        "success": False,
                        "action": "delete_file",
                        "error": "File not found",
                        "message": f"Could not find {file_path}",
                    }
                if os.path.isfile(file_path):
                    os.remove(file_path)
                else:
                    shutil.rmtree(file_path)
                return {
                    "success": True,
                    "action": "delete_file",
                    "file": file_path,
                    "message": f"Deleted {file_path}",
                }

            if "copy" in text:
                src = os.path.expanduser(source)
                dst = os.path.expanduser(target)
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
                return {
                    "success": True,
                    "action": "copy_file",
                    "from": src,
                    "to": dst,
                    "message": f"Copied {src} -> {dst}",
                }

            if "move" in text or "rename" in text:
                src = os.path.expanduser(source)
                dst = os.path.expanduser(target)
                shutil.move(src, dst)
                return {
                    "success": True,
                    "action": "move_file",
                    "from": src,
                    "to": dst,
                    "message": f"Moved {src} -> {dst}",
                }

            if "find" in text or "locate" in text or "search" in text:
                search_root = os.path.expanduser("~")
                matches = []
                for root, _dirs, files in os.walk(search_root):
                    # Skip dotfile-heavy dirs to keep this fast.
                    if any(part.startswith(".") for part in Path(root).parts):
                        continue
                    for fname in files:
                        if target.lower() in fname.lower():
                            matches.append(os.path.join(root, fname))
                            if len(matches) >= 5:
                                break
                    if len(matches) >= 5:
                        break
                return {
                    "success": True,
                    "action": "find_file",
                    "matches": matches,
                    "message": f"Found {len(matches)} files matching {target}",
                }

            return {
                "success": False,
                "action": "file_operation",
                "error": "Could not determine file operation",
            }
        except Exception as exc:
            return {"success": False, "action": "file_operation", "error": str(exc)}

    async def _handle_execute_code(self, code_snippet: str) -> Dict:
        """Execute (sandboxed) Python code with safety checks."""
        try:
            if any(kw in code_snippet for kw in self.dangerous_keywords):
                return {
                    "success": False,
                    "action": "execute_code",
                    "error": "Dangerous operation detected",
                }

            namespace = {
                "print": print,
                "len": len,
                "range": range,
                "sum": sum,
                "min": min,
                "max": max,
            }
            exec(code_snippet, namespace)  # noqa: S102 — gated by keyword check

            return {
                "success": True,
                "action": "execute_code",
                "message": "Code executed successfully",
            }
        except Exception as exc:
            return {"success": False, "action": "execute_code", "error": str(exc)}

    async def _handle_search(self, query: str) -> Dict:
        """Search the web in the default browser."""
        try:
            query = (query or "").strip()
            search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
            webbrowser.open(search_url)
            return {
                "success": True,
                "action": "search",
                "query": query,
                "message": f"Searching for '{query}'...",
            }
        except Exception as exc:
            return {"success": False, "action": "search", "error": str(exc)}

    async def _handle_learn_preference(self, intent: Dict) -> Dict:
        """Learn and store user preference."""
        try:
            params = intent["parameters"]
            text = (intent.get("original_input") or "").lower()

            # Detect name-learning patterns and store under "name" specifically.
            if (
                "my name is" in text
                or text.startswith("call me ")
                or text.startswith("i'm ")
                or text.startswith("i am ")
            ):
                key = "name"
                # In these patterns the regex captured a single group → 'target'.
                # The parser lower-cased the input, so title-case for a clean
                # display (handles 'o'brien' -> "O'Brien" via str.title).
                raw_value = (
                    params.get("target") or params.get("source") or ""
                ).strip()
                value = raw_value.title() if raw_value else raw_value
                self.kb.learn_preference(key, value, "identity", confidence=0.99)
                return {
                    "success": True,
                    "action": "learn_preference",
                    "key": key,
                    "value": value,
                    "message": f"Got it, {value}. I'll remember.",
                }

            key = params.get("source") or "preference"
            value = params.get("target", "")
            self.kb.learn_preference(key, value, "user_preference", confidence=0.95)

            return {
                "success": True,
                "action": "learn_preference",
                "key": key,
                "value": value,
                "message": f"Remembered: your {key} is {value}",
            }
        except Exception as exc:
            return {"success": False, "action": "learn_preference", "error": str(exc)}

    async def _handle_query_knowledge(self, query: str) -> Dict:
        """Query learned knowledge."""
        try:
            query_lower = query.lower().strip()

            # Direct preference lookup
            for key, value in self.kb.get_all_preferences().items():
                if key.lower() in query_lower or query_lower in key.lower():
                    return {
                        "success": True,
                        "action": "query_knowledge",
                        "answer": f"I remember that your {key} is {value}",
                        "type": "preference",
                    }

            # Past conversation
            history = self.kb.get_relevant_history(query, limit=1)
            if history:
                return {
                    "success": True,
                    "action": "query_knowledge",
                    "answer": f"I recall we discussed: {history[0]['user_input']}",
                    "type": "history",
                }

            return {
                "success": False,
                "action": "query_knowledge",
                "message": "I do not have that information in my memory yet.",
            }
        except Exception as exc:
            return {"success": False, "action": "query_knowledge", "error": str(exc)}

    async def _handle_schedule_task(self, intent: Dict) -> Dict:
        """Schedule a task (in-memory; persistence is a Phase 2 enhancement)."""
        try:
            params = intent["parameters"]
            task = params.get("source") or "task"
            time_str = params.get("target") or "later"

            self.scheduled_tasks.append(
                {
                    "task": task,
                    "scheduled_time": datetime.now().isoformat(),
                    "when": time_str,
                }
            )

            return {
                "success": True,
                "action": "schedule_task",
                "task": task,
                "when": time_str,
                "message": f"Scheduled: {task} for {time_str}",
            }
        except Exception as exc:
            return {"success": False, "action": "schedule_task", "error": str(exc)}

    async def _handle_browser_task(self, params: Dict) -> Dict:
        """High-level browser automation (YouTube, ChatGPT, ...)."""
        task = (params.get("task") or "").strip()

        if not self.browser_agent:
            return {
                "success": False,
                "action": "browser_task",
                "task": task,
                "error": "browser_agent_unavailable",
                "message": (
                    "Browser automation isn't initialized. Install Playwright "
                    "(pip install playwright && playwright install chromium) "
                    "and restart CHAPPIE."
                ),
            }

        if task == "youtube_play":
            result = await self.browser_agent.youtube_search_and_play(
                params.get("query", "")
            )
        elif task == "chatgpt_ask":
            result = await self.browser_agent.chatgpt_ask(params.get("question", ""))
        else:
            return {
                "success": False,
                "action": "browser_task",
                "task": task,
                "error": "unknown_task",
                "message": f"I don't know how to do '{task}' yet.",
            }

        result.setdefault("action", "browser_task")
        if result.get("success"):
            target = (
                params.get("query") or params.get("question") or task
            )
            self.kb.track_resource(target[:120], "browser_task")
        return result

    async def _handle_clarify(self, params: Dict) -> Dict:
        """The orchestrator handles the conversational state. We just emit the question."""
        question = params.get("question") or "Could you give me a bit more detail?"
        return {
            "success": True,
            "action": "clarify",
            "question": question,
            "message": question,
        }

    async def _handle_skill(self, params: Dict) -> Dict:
        """Dispatch to a registered skill by name."""
        skill_name = (params.get("skill") or "").strip()
        skill_params = params.get("params") or {}

        if not self.skills:
            return {
                "success": False,
                "action": "skill",
                "error": "no_skill_registry",
                "message": "Skills aren't initialized.",
            }
        skill = self.skills.get(skill_name)
        if skill is None:
            return {
                "success": False,
                "action": "skill",
                "skill": skill_name,
                "error": "unknown_skill",
                "message": f"I don't have a '{skill_name}' skill.",
            }
        try:
            result = await skill.execute(skill_params)
            result.setdefault("action", "skill")
            result.setdefault("skill", skill_name)
            return result
        except Exception as exc:
            logger.error(f"Skill {skill_name} crashed: {exc}")
            return {
                "success": False,
                "action": "skill",
                "skill": skill_name,
                "error": str(exc),
                "message": f"Something went wrong running the {skill_name} skill: {exc}",
            }

    async def _handle_converse(self, params: Dict, intent: Dict) -> Dict:
        """Freeform conversational reply with the JARVIS persona prompt."""
        prompt = (params.get("prompt") or intent.get("original_input") or "").strip()
        if not prompt:
            return {
                "success": True,
                "action": "converse",
                "message": "I'm here. What's on your mind?",
            }
        if not self.llm:
            return {
                "success": True,
                "action": "converse",
                "message": "I'm not sure how to handle that yet.",
            }

        persona = ""
        if callable(self.persona_provider):
            try:
                persona = self.persona_provider() or ""
            except Exception:
                persona = ""

        try:
            text = await self.llm.generate(
                messages=[{"role": "user", "content": prompt}],
                system=persona or None,
                temperature=0.7,
            )
        except Exception as exc:
            logger.error(f"Converse LLM failed: {exc}")
            text = ""

        if not text:
            text = "I heard you, but my brain's offline at the moment — try again in a sec."

        return {
            "success": True,
            "action": "converse",
            "message": text,
        }

    async def _handle_system_command(self, command: str) -> Dict:
        """Execute system command with safety checks."""
        try:
            blocked = ["rm -rf", "sudo", "format", "mkfs"]
            if any(b in command.lower() for b in blocked):
                return {
                    "success": False,
                    "action": "system_command",
                    "error": "Dangerous command blocked",
                }

            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = (result.stdout or result.stderr or "").strip()[:500]

            return {
                "success": result.returncode == 0,
                "action": "system_command",
                "output": output,
                "message": output or "Command executed",
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "action": "system_command", "error": "Command timeout"}
        except Exception as exc:
            return {"success": False, "action": "system_command", "error": str(exc)}
