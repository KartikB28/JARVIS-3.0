"""
Calendar skill: schedule events as portable .ics calendar files.

We don't tie to one provider — we generate an .ics file the user's OS
opens with their default calendar (Outlook, Google Calendar via browser
import, Apple Calendar, Thunderbird, ...). Works offline, no API keys.

Time parsing uses dateutil's fuzzy parser, which handles "tomorrow 7pm",
"Friday at 6", "Dec 14 noon", etc.
"""

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

from core.skills import Skill
from utils.logger import setup_logger

logger = setup_logger(__name__)


SAFE_NAME_RX = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class CalendarSkill(Skill):
    name = "calendar"
    description = (
        "Schedule an event by generating a .ics file the OS opens in the "
        "user's default calendar. Parameters: event (title), when "
        "(natural-language time), optional duration, optional attendee."
    )
    triggers = [
        # "schedule a meeting with John for tomorrow at 3pm"
        r"^schedule\s+(?:a\s+|an\s+)?(?P<event>.+?)"
        r"(?:\s+with\s+(?P<who>[^,]+?))?"
        r"\s+(?:for|on|at)\s+(?P<when>.+?)\s*$",
        # "remind me to call mom at 6pm tomorrow"
        r"^remind\s+me\s+(?:to\s+)?(?P<event>.+?)"
        r"\s+(?:at|on|in|after)\s+(?P<when>.+?)\s*$",
        # "add a meeting/event/appointment X for tomorrow 5pm"
        r"^add\s+(?:a\s+|an\s+)?(?:meeting|event|appointment|reminder)\s+(?P<event>.+?)"
        r"\s+(?:for|on|at)\s+(?P<when>.+?)\s*$",
        # "plan dinner with X for friday 8pm" / "book a dinner with X for ..."
        r"^(?:plan|book|set\s+up)\s+(?:a\s+|an\s+)?(?P<event>dinner|lunch|breakfast|meeting|call|coffee)"
        r"(?:\s+with\s+(?P<who>[^,]+?))?"
        r"\s+(?:for|on|at)\s+(?P<when>.+?)\s*$",
    ]

    def __init__(self, kb, config: Dict, llm=None):
        super().__init__(kb, config, llm)
        skills_cfg = config.get("skills", {})
        self.save_dir = os.path.expanduser(
            skills_cfg.get("calendar_dir", "~/Documents/CHAPPIE/calendar")
        )
        self.default_duration = skills_cfg.get("default_event_minutes", 60)
        self.auto_open = skills_cfg.get("calendar_auto_open", True)
        Path(self.save_dir).mkdir(parents=True, exist_ok=True)

    async def execute(self, params: Dict, context: Optional[Dict] = None) -> Dict:
        event = (params.get("event") or "Untitled event").strip()
        when_text = (params.get("when") or "").strip()
        who = (params.get("who") or "").strip()
        duration_minutes = int(params.get("duration_minutes", self.default_duration))

        if not when_text:
            return {
                "success": False,
                "skill": self.name,
                "message": "When should I schedule it?",
            }

        event_dt = self._parse_when(when_text)
        if event_dt is None:
            return {
                "success": False,
                "skill": self.name,
                "message": (
                    f"I couldn't parse '{when_text}' as a date/time. "
                    "Try something like 'tomorrow 7pm' or 'Friday at 6'."
                ),
            }

        # Compose summary
        summary = event
        if who and "with" not in summary.lower():
            summary = f"{event} with {who}"

        ics = self._build_ics(summary, event_dt, duration_minutes, attendee=who)
        safe_name = SAFE_NAME_RX.sub("_", summary)[:60].strip().rstrip(".")
        path = os.path.join(self.save_dir, f"{safe_name}.ics")
        with open(path, "w", encoding="utf-8") as f:
            f.write(ics)
        self.kb.track_resource(path, "calendar_event")

        opened = ""
        if self.auto_open:
            opened = self._open_file(path)

        when_pretty = event_dt.strftime("%a %b %d at %I:%M %p")
        return {
            "success": True,
            "skill": self.name,
            "action": "calendar_event",
            "path": path,
            "summary": summary,
            "when": event_dt.isoformat(),
            "message": f"Scheduled '{summary}' for {when_pretty}.{opened}",
        }

    # ------------------------------------------------------------------

    def _parse_when(self, when_text: str) -> Optional[datetime]:
        try:
            from dateutil import parser as date_parser  # lazy import
        except ImportError:
            logger.warning("python-dateutil not installed; falling back to naive parse")
            return self._naive_parse(when_text)

        # dateutil's fuzzy mode handles "tomorrow 7pm", "fri at 6", etc. given
        # a default reference of "now" so missing fields fill from now.
        try:
            now = datetime.now()
            parsed = date_parser.parse(
                when_text,
                fuzzy=True,
                default=now.replace(second=0, microsecond=0),
            )
        except (ValueError, OverflowError):
            return self._naive_parse(when_text)

        # If user said "tomorrow", advance the day.
        wt = when_text.lower()
        if "tomorrow" in wt and parsed.date() == datetime.now().date():
            parsed = parsed + timedelta(days=1)
        elif "next week" in wt and parsed <= datetime.now():
            parsed = parsed + timedelta(weeks=1)

        # If the parsed time is in the past for today, push to tomorrow
        # (e.g., "at 7" said at 9pm probably means 7am tomorrow).
        if parsed < datetime.now() - timedelta(minutes=1):
            parsed = parsed + timedelta(days=1)

        return parsed

    def _naive_parse(self, when_text: str) -> Optional[datetime]:
        """Last-resort: 1 hour from now."""
        return datetime.now() + timedelta(hours=1)

    def _build_ics(
        self, summary: str, dt: datetime, duration_minutes: int, attendee: str = ""
    ) -> str:
        end = dt + timedelta(minutes=duration_minutes)
        uid = f"chappie-{int(dt.timestamp())}@local"
        attendee_line = (
            f"\nATTENDEE;CN={attendee}:mailto:{attendee.replace(' ', '_')}@example.invalid"
            if attendee and "@" not in attendee
            else (f"\nATTENDEE:mailto:{attendee}" if "@" in attendee else "")
        )
        # Escape commas, semicolons, newlines in summary (per RFC 5545)
        esc = lambda s: s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
        return (
            "BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//CHAPPIE//AI Assistant//EN\r\n"
            "BEGIN:VEVENT\r\n"
            f"UID:{uid}\r\n"
            f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}\r\n"
            f"DTSTART:{dt.strftime('%Y%m%dT%H%M%S')}\r\n"
            f"DTEND:{end.strftime('%Y%m%dT%H%M%S')}\r\n"
            f"SUMMARY:{esc(summary)}{attendee_line}\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        )

    def _open_file(self, path: str) -> str:
        try:
            if sys.platform == "win32":
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            return " (Opening in your calendar.)"
        except Exception as exc:
            logger.warning(f"Couldn't auto-open calendar event: {exc}")
            return ""
