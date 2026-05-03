"""
Per-OS application launch registry.

Maps canonical app names (and aliases people actually say) to the right
shell command for the host OS. Lets the launcher just work on Windows,
macOS, and Linux without each handler caring about the difference.
"""

import os
import re
import sys
from typing import Optional


# canonical_name -> { aliases: [...], win32: cmd, darwin: cmd, linux: cmd }
KNOWN_APPS = {
    # ----- browsers -----
    "chrome": {
        "aliases": ["google chrome", "gchrome"],
        "win32": "start chrome",
        "darwin": 'open -a "Google Chrome"',
        "linux": "google-chrome",
    },
    "firefox": {
        "aliases": ["mozilla firefox"],
        "win32": "start firefox",
        "darwin": "open -a Firefox",
        "linux": "firefox",
    },
    "edge": {
        "aliases": ["microsoft edge", "msedge"],
        "win32": "start msedge",
        "darwin": 'open -a "Microsoft Edge"',
        "linux": "microsoft-edge",
    },
    "brave": {
        "aliases": ["brave browser"],
        "win32": "start brave",
        "darwin": 'open -a "Brave Browser"',
        "linux": "brave-browser",
    },
    "safari": {
        "aliases": [],
        "win32": None,
        "darwin": "open -a Safari",
        "linux": None,
    },

    # ----- shells / terminals -----
    "cmd": {
        "aliases": ["command prompt"],
        "win32": "start cmd",
        "darwin": "open -a Terminal",
        "linux": "x-terminal-emulator",
    },
    "powershell": {
        "aliases": [],
        "win32": "start powershell",
        "darwin": "open -a Terminal",
        "linux": "pwsh",
    },
    "terminal": {
        "aliases": ["console"],
        "win32": "start cmd",
        "darwin": "open -a Terminal",
        "linux": "x-terminal-emulator",
    },

    # ----- system tools -----
    "file explorer": {
        "aliases": ["explorer", "files", "finder", "file manager", "my computer"],
        "win32": "explorer",
        "darwin": "open -a Finder",
        "linux": "xdg-open .",
    },
    "task manager": {
        "aliases": ["taskmgr"],
        "win32": "taskmgr",
        "darwin": 'open -a "Activity Monitor"',
        "linux": "gnome-system-monitor",
    },
    "control panel": {
        "aliases": [],
        "win32": "control",
        "darwin": "open -a 'System Settings'",
        "linux": "gnome-control-center",
    },
    "settings": {
        "aliases": ["system settings", "preferences"],
        "win32": "start ms-settings:",
        "darwin": "open -a 'System Settings'",
        "linux": "gnome-control-center",
    },

    # ----- editors / IDEs -----
    "notepad": {
        "aliases": [],
        "win32": "notepad",
        "darwin": "open -a TextEdit",
        "linux": "gedit",
    },
    "vscode": {
        "aliases": ["vs code", "visual studio code", "code editor"],
        "win32": "code",
        "darwin": 'open -a "Visual Studio Code"',
        "linux": "code",
    },
    "sublime": {
        "aliases": ["sublime text"],
        "win32": "subl",
        "darwin": 'open -a "Sublime Text"',
        "linux": "subl",
    },

    # ----- utilities -----
    "calculator": {
        "aliases": ["calc"],
        "win32": "calc",
        "darwin": "open -a Calculator",
        "linux": "gnome-calculator",
    },
    "paint": {
        "aliases": ["ms paint", "mspaint"],
        "win32": "mspaint",
        "darwin": "open -a Preview",
        "linux": "gimp",
    },
    "snipping tool": {
        "aliases": ["screenshot", "snip"],
        "win32": "snippingtool",
        "darwin": "open -a 'Screenshot'",
        "linux": "gnome-screenshot",
    },

    # ----- office -----
    "word": {
        "aliases": ["microsoft word", "ms word", "winword"],
        "win32": "start winword",
        "darwin": 'open -a "Microsoft Word"',
        "linux": "libreoffice --writer",
    },
    "excel": {
        "aliases": ["microsoft excel", "ms excel"],
        "win32": "start excel",
        "darwin": 'open -a "Microsoft Excel"',
        "linux": "libreoffice --calc",
    },
    "powerpoint": {
        "aliases": ["microsoft powerpoint", "ms powerpoint", "ppt"],
        "win32": "start powerpnt",
        "darwin": 'open -a "Microsoft PowerPoint"',
        "linux": "libreoffice --impress",
    },
    "outlook": {
        "aliases": ["microsoft outlook"],
        "win32": "start outlook",
        "darwin": "open -a Microsoft\\ Outlook",
        "linux": "thunderbird",
    },

    # ----- chat / media -----
    "spotify": {
        "aliases": [],
        "win32": "start spotify",
        "darwin": "open -a Spotify",
        "linux": "spotify",
    },
    "discord": {
        "aliases": [],
        "win32": "start discord",
        "darwin": "open -a Discord",
        "linux": "discord",
    },
    "slack": {
        "aliases": [],
        "win32": "start slack",
        "darwin": "open -a Slack",
        "linux": "slack",
    },
    "steam": {
        "aliases": [],
        "win32": "start steam",
        "darwin": "open -a Steam",
        "linux": "steam",
    },
    "obs": {
        "aliases": ["obs studio"],
        "win32": "start obs",
        "darwin": 'open -a "OBS"',
        "linux": "obs",
    },
    "vlc": {
        "aliases": ["vlc media player"],
        "win32": "start vlc",
        "darwin": "open -a VLC",
        "linux": "vlc",
    },
}


# Names that *look* like apps but should really open a website.
WEB_SHORTCUTS = {
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "google maps": "https://maps.google.com",
    "google drive": "https://drive.google.com",
    "youtube": "https://www.youtube.com",
    "youtube music": "https://music.youtube.com",
    "twitter": "https://twitter.com",
    "x": "https://x.com",
    "facebook": "https://facebook.com",
    "instagram": "https://instagram.com",
    "reddit": "https://reddit.com",
    "github": "https://github.com",
    "stack overflow": "https://stackoverflow.com",
    "chatgpt": "https://chat.openai.com",
    "claude": "https://claude.ai",
    "netflix": "https://netflix.com",
    "amazon": "https://amazon.com",
    "wikipedia": "https://wikipedia.org",
    "linkedin": "https://linkedin.com",
}


# Filler phrases to strip when extracting an app/url name from natural input.
FILLER_PHRASES = [
    "for me",
    "on this computer",
    "on my computer",
    "right now",
    "right away",
    "real quick",
    "real fast",
    "now",
    "please",
    "thanks",
    "thank you",
    "would you",
    "could you",
    "can you",
]


def strip_fillers(text: str) -> str:
    """Remove filler phrases users sprinkle into commands."""
    out = (text or "").lower().strip()
    # Strip multi-word fillers first (longest first to avoid partial matches).
    for phrase in sorted(FILLER_PHRASES, key=len, reverse=True):
        out = re.sub(r"\b" + re.escape(phrase) + r"\b", " ", out)
    out = re.sub(r"[?!.,]+\s*$", "", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _all_app_terms():
    """Yield (search_term, canonical_name) pairs, longest first."""
    pairs = []
    for canonical, info in KNOWN_APPS.items():
        pairs.append((canonical, canonical))
        for alias in info.get("aliases", []):
            pairs.append((alias, canonical))
    pairs.sort(key=lambda p: -len(p[0]))
    return pairs


def _all_web_terms():
    pairs = [(name, url) for name, url in WEB_SHORTCUTS.items()]
    pairs.sort(key=lambda p: -len(p[0]))
    return pairs


def find_known_app(text: str) -> Optional[str]:
    """
    Search `text` for any known app name or alias.
    Returns the canonical name, or None.  Prefers longer matches.
    """
    text_lower = (text or "").lower()
    for term, canonical in _all_app_terms():
        if re.search(r"\b" + re.escape(term) + r"\b", text_lower):
            return canonical
    return None


def find_web_shortcut(text: str) -> Optional[str]:
    """Return the URL for a known web shortcut found in `text`, or None."""
    text_lower = (text or "").lower()
    for term, url in _all_web_terms():
        if re.search(r"\b" + re.escape(term) + r"\b", text_lower):
            return url
    return None


def get_launch_command(canonical_name: str) -> Optional[str]:
    """OS-specific shell command for a known app, or None."""
    info = KNOWN_APPS.get(canonical_name)
    if not info:
        return None
    if sys.platform == "win32":
        return info.get("win32")
    if sys.platform == "darwin":
        return info.get("darwin")
    return info.get("linux")


# ============================================================
# Special-folder shortcuts (cross-OS)
# ============================================================

# Folder name -> subdirectory under $HOME (empty string == $HOME itself).
SPECIAL_FOLDERS = {
    "downloads": "Downloads",
    "download": "Downloads",
    "desktop": "Desktop",
    "documents": "Documents",
    "docs": "Documents",
    "pictures": "Pictures",
    "photos": "Pictures",
    "music": "Music",
    "videos": "Videos",
    "movies": "Videos",
    "home": "",
    "home folder": "",
    "user folder": "",
    "my folder": "",
}


def resolve_special_path(name: str) -> Optional[str]:
    """Resolve a special-folder name to an absolute path, or None."""
    sub = SPECIAL_FOLDERS.get((name or "").lower().strip())
    if sub is None:
        return None
    home = os.path.expanduser("~")
    return os.path.join(home, sub) if sub else home


def find_special_folder(text: str) -> Optional[str]:
    """Find a special folder reference in `text` (e.g. 'my downloads')."""
    lowered = (text or "").lower()
    # Prefer longest names so 'home folder' beats 'home'.
    for name in sorted(SPECIAL_FOLDERS.keys(), key=len, reverse=True):
        if re.search(rf"\b(?:my\s+|the\s+)?{re.escape(name)}(?:\s+folder|\s+directory)?\b", lowered):
            return name
    return None


# ============================================================
# Drive letter recognition
# ============================================================

# Matches: "disk C", "drive C", "C drive", "C disk", "C:", "C:\".
DRIVE_RX = re.compile(
    r"\b(?:disk|drive|partition)\s+([a-z])\b"
    r"|\b([a-z])\s+(?:drive|disk)\b"
    r"|(?<![a-z])([a-z]):\\?",
    re.IGNORECASE,
)


def find_drive_letter(text: str) -> Optional[str]:
    """Return the upper-case letter a user is referring to, or None."""
    m = DRIVE_RX.search(text or "")
    if not m:
        return None
    letter = next((g for g in m.groups() if g), None)
    return letter.upper() if letter else None


# ============================================================
# Browser-targeted URL commands
# ============================================================

BROWSER_NAMES = {"chrome", "firefox", "edge", "brave", "safari"}


def get_browser_url_command(browser: str, url: str) -> Optional[str]:
    """Return a shell command to open `url` in a specific `browser`."""
    canonical = find_known_app(browser) or browser
    if canonical not in BROWSER_NAMES:
        return None

    # Use double-quotes around URLs in case they contain shell-significant chars.
    quoted = f'"{url}"'
    if sys.platform == "win32":
        win_bin = {
            "chrome": "chrome",
            "firefox": "firefox",
            "edge": "msedge",
            "brave": "brave",
        }.get(canonical)
        if not win_bin:
            return None
        return f"start {win_bin} {quoted}"
    if sys.platform == "darwin":
        mac_app = {
            "chrome": "Google Chrome",
            "firefox": "Firefox",
            "edge": "Microsoft Edge",
            "brave": "Brave Browser",
            "safari": "Safari",
        }.get(canonical)
        return f'open -a "{mac_app}" {quoted}'
    # Linux
    linux_bin = {
        "chrome": "google-chrome",
        "firefox": "firefox",
        "edge": "microsoft-edge",
        "brave": "brave-browser",
    }.get(canonical)
    if not linux_bin:
        return None
    return f"{linux_bin} {quoted}"
