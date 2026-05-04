"""
Email skill: draft an email, drop the body on the clipboard, open the
user's mail client with everything pre-filled via a mailto: link.

If SMTP is configured, can also send directly. Off by default — drafting
is safer and keeps the user in control.
"""

import os
import re
import urllib.parse
import webbrowser
from typing import Dict, Optional

from core.skills import Skill
from utils.logger import setup_logger

logger = setup_logger(__name__)


EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


class EmailSkill(Skill):
    name = "email"
    description = (
        "Compose an email. By default drafts the body and opens the user's "
        "mail client via mailto: pre-filled with To/Subject/Body. Body is "
        "also copied to the clipboard."
    )
    triggers = [
        # "email John about the meeting" / "send John an email about ..."
        r"^(?:send|email)\s+(?P<recipient>\S+@\S+|[A-Za-z][\w\s]+?)"
        r"\s+(?:an?\s+)?(?:email|message)?\s*(?:about\s+(?P<topic>.+?))?\s*$",
        # "write|compose|draft an email to X about Y"
        r"^(?:write|compose|draft)\s+(?:an?\s+)?email"
        r"(?:\s+to\s+(?P<recipient>\S+@\S+|[A-Za-z][\w\s]+?))?"
        r"(?:\s+about\s+(?P<topic>.+?))?\s*$",
    ]

    def __init__(self, kb, config: Dict, llm=None):
        super().__init__(kb, config, llm)
        smtp = config.get("smtp", {})
        self.smtp_enabled = bool(smtp.get("enabled"))
        self.smtp_host = smtp.get("host")
        self.smtp_port = smtp.get("port", 587)
        self.smtp_user = smtp.get("user")
        self.smtp_password = os.environ.get(
            smtp.get("password_env", "SMTP_PASSWORD"), ""
        ) or smtp.get("password", "")
        self.smtp_from = smtp.get("from") or self.smtp_user

    async def execute(self, params: Dict, context: Optional[Dict] = None) -> Dict:
        recipient = (params.get("recipient") or "").strip().rstrip(",.")
        topic = (params.get("topic") or "").strip()
        body_override = (params.get("body") or "").strip()
        send_now = bool(params.get("send"))

        # Resolve recipient from contacts in the KB if it's a name.
        if recipient and "@" not in recipient:
            looked_up = self._lookup_contact(recipient)
            if looked_up:
                recipient = looked_up

        # Generate body if not given
        body = body_override
        if not body and self.llm and topic:
            body = await self._draft_body(recipient, topic)

        # Subject = topic, fallback
        subject = topic[:80] if topic else "Quick note"

        # Try SMTP send if enabled and asked for
        if send_now and self.smtp_enabled and EMAIL_RX.match(recipient or ""):
            sent = self._smtp_send(recipient, subject, body)
            if sent:
                return {
                    "success": True,
                    "skill": self.name,
                    "action": "email_sent",
                    "recipient": recipient,
                    "message": f"Sent to {recipient}.",
                }
            # fall through to draft path

        # Default: draft + mailto
        recipient_for_link = recipient if "@" in (recipient or "") else ""
        mailto = "mailto:" + recipient_for_link
        params_str = urllib.parse.urlencode(
            {"subject": subject, "body": body or ""}, quote_via=urllib.parse.quote
        )
        if params_str:
            mailto += "?" + params_str

        try:
            webbrowser.open(mailto)
        except Exception as exc:
            logger.warning(f"Couldn't open mail client: {exc}")

        clipboard_msg = ""
        if body:
            try:
                import pyperclip  # type: ignore

                pyperclip.copy(body)
                clipboard_msg = " The body is on your clipboard too."
            except ImportError:
                pass

        if recipient and "@" in recipient:
            who = recipient
        elif recipient:
            who = f"{recipient} (couldn't find an email — please add it)"
        else:
            who = "your draft"

        return {
            "success": True,
            "skill": self.name,
            "action": "email_draft",
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "message": f"Drafted email to {who}.{clipboard_msg}",
        }

    async def _draft_body(self, recipient: str, topic: str) -> str:
        prompt = (
            f"Write a brief, professional email body about: {topic}."
        )
        if recipient:
            prompt += f" Addressed to {recipient}."
        try:
            return await self.llm.generate(
                messages=[{"role": "user", "content": prompt}],
                system=(
                    "Write the email body only. No subject line. No "
                    "'Hi [name]' template — use the recipient's actual name "
                    "if given. Be concise and direct. Sign off with "
                    "'Best,' on its own line so the user can fill in their name."
                ),
                temperature=0.5,
            ) or ""
        except Exception as exc:
            logger.warning(f"Email draft failed: {exc}")
            return ""

    def _lookup_contact(self, name: str) -> Optional[str]:
        """Check user_preferences for a 'contact:<name>' or '<name>_email' entry."""
        prefs = self.kb.get_all_preferences()
        # Try common patterns the user might have used:
        candidates = [
            f"contact:{name.lower()}",
            f"{name.lower()}_email",
            f"email:{name.lower()}",
            name.lower(),
        ]
        for c in candidates:
            v = prefs.get(c)
            if v and "@" in v:
                return v
        return None

    def _smtp_send(self, to: str, subject: str, body: str) -> bool:
        try:
            import smtplib
            from email.mime.text import MIMEText

            msg = MIMEText(body or "")
            msg["Subject"] = subject
            msg["From"] = self.smtp_from
            msg["To"] = to

            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=20) as s:
                s.starttls()
                if self.smtp_user and self.smtp_password:
                    s.login(self.smtp_user, self.smtp_password)
                s.send_message(msg)
            logger.info(f"SMTP sent to {to}")
            return True
        except Exception as exc:
            logger.error(f"SMTP send failed: {exc}")
            return False
