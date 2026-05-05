"""
File / app indexer.

Scans the user's machine for installed apps, common documents, and folders,
and stores a searchable index in the same SQLite DB as the knowledge base.
The execution engine queries this index when the user refers to something
by name that isn't in the curated app registry — e.g. "open photoshop",
"open my resume", "open the python project".

Build runs in a background thread and is bounded by depth and entry caps
so it stays fast even on machines with huge Documents folders.
"""

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from utils.logger import setup_logger

logger = setup_logger(__name__)


# ----- Build limits (keep the index small, fast, and signal-rich) -----
MAX_TOTAL_ENTRIES = 50_000
MAX_PER_LOCATION = 5_000
MAX_DEPTH_DEFAULT = 3

# Skip these directory names anywhere we encounter them.
SKIP_DIR_NAMES = {
    "node_modules", ".git", ".hg", ".svn",
    "venv", ".venv", "env", ".env",
    "__pycache__", ".pytest_cache",
    ".cache", ".npm", ".gradle", ".m2",
    "AppData", "$Recycle.Bin", "System Volume Information",
    "build", "dist", "target", "out",
}

# Skip files with these extensions — they're rarely what a user means.
SKIP_EXTENSIONS = {
    ".tmp", ".log", ".bak", ".swp", ".swo",
    ".cache", ".lock",
    ".db", ".db-wal", ".db-shm", ".db-journal",
    ".pyc", ".pyo", ".class", ".o",
}


@dataclass
class IndexEntry:
    path: str
    name: str
    kind: str       # 'app' | 'file' | 'folder'
    location: str   # 'start_menu' | 'desktop' | 'documents' | ...
    ext: str
    score: float = 0.0


class FileIndexer:
    """Builds and queries the on-disk index of apps, files, and folders."""

    def __init__(self, kb):
        self.kb = kb
        self._init_schema()
        self._building = False

    # ------------------------------------------------------------------
    # Schema / status
    # ------------------------------------------------------------------

    def _init_schema(self):
        cursor = self.kb.conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS file_index (
                path TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                name_lower TEXT NOT NULL,
                kind TEXT NOT NULL,
                location TEXT,
                ext TEXT,
                indexed_at TIMESTAMP
            )
            """
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_fi_name_lower ON file_index(name_lower)"
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_fi_kind ON file_index(kind)")
        self.kb.conn.commit()

    def stats(self) -> Dict:
        cursor = self.kb.conn.cursor()
        cursor.execute("SELECT COUNT(*) AS c FROM file_index")
        total = cursor.fetchone()["c"]
        cursor.execute("SELECT MAX(indexed_at) AS m FROM file_index")
        last = cursor.fetchone()["m"]
        return {
            "total_entries": total,
            "last_indexed": last,
            "is_building": self._building,
        }

    def is_stale(self, max_age_hours: int = 24) -> bool:
        cursor = self.kb.conn.cursor()
        cursor.execute("SELECT MAX(indexed_at) AS m FROM file_index")
        last = cursor.fetchone()["m"]
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except (TypeError, ValueError):
            return True
        return datetime.now() - last_dt > timedelta(hours=max_age_hours)

    # ------------------------------------------------------------------
    # Build (async wrapper around a threaded sync walk)
    # ------------------------------------------------------------------

    async def build_index(self, force: bool = False) -> Dict:
        if self._building:
            return {"status": "already_building", **self.stats()}
        if not force and not self.is_stale():
            return {"status": "fresh", **self.stats()}

        self._building = True
        try:
            count = await asyncio.to_thread(self._build_sync)
            return {"status": "rebuilt", "entries": count}
        finally:
            self._building = False

    def _build_sync(self) -> int:
        cursor = self.kb.conn.cursor()
        cursor.execute("DELETE FROM file_index")
        self.kb.conn.commit()

        total = 0
        rows: List[tuple] = []

        def emit(path: str, name: str, kind: str, location: str, ext: str) -> bool:
            nonlocal total
            if total >= MAX_TOTAL_ENTRIES:
                return False
            rows.append(
                (path, name, name.lower(), kind, location, ext, datetime.now())
            )
            total += 1
            # Flush every 5k rows to keep memory bounded.
            if len(rows) >= 5000:
                cursor.executemany(
                    "INSERT OR REPLACE INTO file_index VALUES (?,?,?,?,?,?,?)",
                    rows,
                )
                self.kb.conn.commit()
                rows.clear()
            return True

        if sys.platform == "win32":
            self._index_windows_apps(emit)
        elif sys.platform == "darwin":
            self._index_mac_apps(emit)
        else:
            self._index_linux_apps(emit)

        # Cross-platform game launchers (best-effort)
        self._index_steam_games(emit)
        self._index_epic_games(emit)
        self._index_riot_games(emit)

        self._index_user_folders(emit)

        if rows:
            cursor.executemany(
                "INSERT OR REPLACE INTO file_index VALUES (?,?,?,?,?,?,?)",
                rows,
            )
        self.kb.conn.commit()
        logger.info(f"File index rebuilt with {total} entries")
        return total

    # ----- per-OS app discovery -----

    def _index_windows_apps(self, emit: Callable):
        roots = []
        for env_var in ("APPDATA", "PROGRAMDATA"):
            base = os.environ.get(env_var, "")
            if base:
                roots.append(
                    os.path.join(
                        base, "Microsoft", "Windows", "Start Menu", "Programs"
                    )
                )
        # Public desktop too — many installers drop shortcuts there.
        public = os.environ.get("PUBLIC", "")
        if public:
            roots.append(os.path.join(public, "Desktop"))

        for root_dir in roots:
            if not os.path.isdir(root_dir):
                continue
            count = 0
            for root, dirs, files in os.walk(root_dir):
                dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
                for f in files:
                    if not f.lower().endswith(".lnk"):
                        continue
                    full = os.path.join(root, f)
                    name = os.path.splitext(f)[0]
                    if not emit(full, name, "app", "start_menu", ".lnk"):
                        return
                    count += 1
                    if count >= MAX_PER_LOCATION:
                        break
                if count >= MAX_PER_LOCATION:
                    break

    def _index_mac_apps(self, emit: Callable):
        for app_dir in ("/Applications", os.path.expanduser("~/Applications")):
            if not os.path.isdir(app_dir):
                continue
            count = 0
            for entry in os.listdir(app_dir):
                if not entry.endswith(".app"):
                    continue
                full = os.path.join(app_dir, entry)
                name = entry[: -len(".app")]
                if not emit(full, name, "app", "applications", ".app"):
                    return
                count += 1
                if count >= MAX_PER_LOCATION:
                    break

    def _index_linux_apps(self, emit: Callable):
        dirs = [
            os.path.expanduser("~/.local/share/applications"),
            "/usr/share/applications",
            "/usr/local/share/applications",
            os.path.expanduser("~/.local/share/flatpak/exports/share/applications"),
        ]
        for d in dirs:
            if not os.path.isdir(d):
                continue
            count = 0
            for entry in os.listdir(d):
                if not entry.endswith(".desktop"):
                    continue
                full = os.path.join(d, entry)
                name = entry[: -len(".desktop")]
                # Prefer the friendly Name= line if present.
                try:
                    with open(full, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            if line.startswith("Name="):
                                name = line.split("=", 1)[1].strip() or name
                                break
                except OSError:
                    pass
                if not emit(full, name, "app", "desktop_entries", ".desktop"):
                    return
                count += 1
                if count >= MAX_PER_LOCATION:
                    break

    # ----- user document discovery -----

    # ----- game launchers -----

    def _index_steam_games(self, emit: Callable):
        """Scan Steam's library for installed games and emit them as 'game' kind."""
        steam_root = self._find_steam_root()
        if not steam_root:
            return

        # Collect all library paths from libraryfolders.vdf
        library_paths = [steam_root]
        vdf_path = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
        if os.path.exists(vdf_path):
            try:
                with open(vdf_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                # libraryfolders.vdf looks like:  "path"     "C:\\SteamLibrary"
                for m in re.finditer(r'"path"\s*"([^"]+)"', content):
                    library_paths.append(m.group(1).replace("\\\\", "\\"))
            except OSError:
                pass

        seen = set()
        for lib in library_paths:
            steamapps = os.path.join(lib, "steamapps")
            if not os.path.isdir(steamapps):
                continue
            try:
                for fname in os.listdir(steamapps):
                    if not fname.startswith("appmanifest_") or not fname.endswith(".acf"):
                        continue
                    full = os.path.join(steamapps, fname)
                    appid, name = self._parse_steam_manifest(full)
                    if not appid or not name or appid in seen:
                        continue
                    seen.add(appid)
                    # Store the steam:// launch URL in the path field; the
                    # executor's _open_indexed branches on this.
                    launch_url = f"steam://rungameid/{appid}"
                    if not emit(launch_url, name, "game", "steam", ".steam"):
                        return
            except OSError:
                continue

    def _find_steam_root(self) -> Optional[str]:
        if sys.platform == "win32":
            for env_var in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
                base = os.environ.get(env_var)
                if base:
                    cand = os.path.join(base, "Steam")
                    if os.path.isdir(cand):
                        return cand
            for cand in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
                if os.path.isdir(cand):
                    return cand
        elif sys.platform == "darwin":
            cand = os.path.expanduser("~/Library/Application Support/Steam")
            if os.path.isdir(cand):
                return cand
        else:
            cand = os.path.expanduser("~/.steam/steam")
            if os.path.isdir(cand):
                return cand
            cand = os.path.expanduser("~/.local/share/Steam")
            if os.path.isdir(cand):
                return cand
        return None

    @staticmethod
    def _parse_steam_manifest(path: str):
        """Pull appid + name from a .acf manifest. Skip Steamworks/redists."""
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            appid_m = re.search(r'"appid"\s*"(\d+)"', content)
            name_m = re.search(r'"name"\s*"([^"]+)"', content)
            if not appid_m or not name_m:
                return None, None
            appid = appid_m.group(1)
            name = name_m.group(1).strip()
            if not name or name.lower() in ("steamworks common redistributables",):
                return None, None
            return appid, name
        except OSError:
            return None, None

    def _index_epic_games(self, emit: Callable):
        """Read Epic Games Launcher manifests and emit each installed game."""
        if sys.platform == "win32":
            programdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
            manifest_dir = os.path.join(
                programdata, "Epic", "EpicGamesLauncher", "Data", "Manifests"
            )
        elif sys.platform == "darwin":
            manifest_dir = os.path.expanduser(
                "~/Library/Application Support/Epic/EpicGamesLauncher/Data/Manifests"
            )
        else:
            manifest_dir = os.path.expanduser(
                "~/.config/Epic/EpicGamesLauncher/Data/Manifests"
            )
        if not os.path.isdir(manifest_dir):
            return

        try:
            entries = [f for f in os.listdir(manifest_dir) if f.endswith(".item")]
        except OSError:
            return

        import json as _json

        for fname in entries:
            full = os.path.join(manifest_dir, fname)
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as f:
                    data = _json.load(f)
            except (OSError, ValueError):
                continue

            display = (
                data.get("DisplayName")
                or data.get("AppName")
                or os.path.splitext(fname)[0]
            )
            namespace = data.get("CatalogNamespace") or data.get("MainGameCatalogNamespace")
            item_id = data.get("CatalogItemId") or data.get("MainGameCatalogItemId")
            app_name = data.get("AppName") or data.get("MainGameAppName")
            if not (namespace and item_id and app_name):
                continue
            launch_url = (
                f"com.epicgames.launcher://apps/{namespace}%3A{item_id}%3A{app_name}"
                "?action=launch&silent=true"
            )
            if not emit(launch_url, display, "game", "epic", ".epic"):
                return

    def _index_riot_games(self, emit: Callable):
        """Best-effort Riot Client games (Valorant, League of Legends)."""
        if sys.platform != "win32":
            return
        candidates = [
            ("VALORANT", "riotclient://rcp/launch?productId=valorant&patchlineId=live"),
            ("League of Legends", "riotclient://rcp/launch?productId=league_of_legends&patchlineId=live"),
        ]
        # Riot doesn't ship a clean manifest; emit if Riot Client is installed.
        riot_root = None
        for env_var in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(env_var)
            if base:
                cand = os.path.join(base, "Riot Vanguard")
                cand2 = os.path.join(base, "Riot Games")
                if os.path.isdir(cand) or os.path.isdir(cand2):
                    riot_root = cand2 if os.path.isdir(cand2) else cand
                    break
        if not riot_root:
            return
        for display, url in candidates:
            if not emit(url, display, "game", "riot", ".riot"):
                return

    def _index_user_folders(self, emit: Callable):
        home = os.path.expanduser("~")
        targets = [
            ("Desktop", "desktop", 2),
            ("Documents", "documents", MAX_DEPTH_DEFAULT),
            ("Downloads", "downloads", 2),
            ("Pictures", "pictures", 2),
            ("Videos", "videos", 2),
            ("Music", "music", 2),
        ]
        for name, location, depth in targets:
            base = os.path.join(home, name)
            if not os.path.isdir(base):
                continue
            self._walk_folder(base, depth, location, emit)

    def _walk_folder(self, base: str, max_depth: int, location: str, emit: Callable):
        base_depth = base.rstrip(os.sep).count(os.sep)
        count = 0
        for root, dirs, files in os.walk(base, topdown=True):
            current_depth = root.rstrip(os.sep).count(os.sep) - base_depth
            if current_depth >= max_depth:
                dirs[:] = []
            else:
                dirs[:] = [
                    d
                    for d in dirs
                    if d not in SKIP_DIR_NAMES and not d.startswith(".")
                ]

            for d in dirs:
                full = os.path.join(root, d)
                if not emit(full, d, "folder", location, ""):
                    return
                count += 1
                if count >= MAX_PER_LOCATION:
                    return

            for f in files:
                if f.startswith("."):
                    continue
                ext = os.path.splitext(f)[1].lower()
                if ext in SKIP_EXTENSIONS:
                    continue
                full = os.path.join(root, f)
                name = os.path.splitext(f)[0]
                if not emit(full, name, "file", location, ext):
                    return
                count += 1
                if count >= MAX_PER_LOCATION:
                    return

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        kind: Optional[str] = None,
        limit: int = 10,
    ) -> List[IndexEntry]:
        """
        Fuzzy search by name. Returns ranked IndexEntry list.

        Scoring (higher is better):
          exact match           100
          starts-with query      80
          contains query         60
          all tokens present     40
          some tokens present    20 * (matched / total)
          + 5 if kind == 'app'
        """
        q = (query or "").lower().strip()
        if not q:
            return []

        tokens = [t for t in re.split(r"\W+", q) if t]
        if not tokens:
            return []

        cursor = self.kb.conn.cursor()
        sql_parts = ["name_lower LIKE ?"] * len(tokens)
        params: List = [f"%{t}%" for t in tokens]
        sql = (
            "SELECT path, name, name_lower, kind, location, ext "
            f"FROM file_index WHERE ({' OR '.join(sql_parts)})"
        )
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " LIMIT 200"
        cursor.execute(sql, params)
        candidates = cursor.fetchall()

        scored: List[IndexEntry] = []
        for row in candidates:
            name_lower = row["name_lower"]
            if name_lower == q:
                score = 100.0
            elif name_lower.startswith(q):
                score = 80.0
            elif q in name_lower:
                score = 60.0
            else:
                hits = sum(1 for t in tokens if t in name_lower)
                if hits == len(tokens):
                    score = 40.0
                elif hits > 0:
                    score = 20.0 * (hits / len(tokens))
                else:
                    score = 0.0

            if row["kind"] == "app":
                score += 5.0

            if score > 0:
                scored.append(
                    IndexEntry(
                        path=row["path"],
                        name=row["name"],
                        kind=row["kind"],
                        location=row["location"],
                        ext=row["ext"],
                        score=score,
                    )
                )

        scored.sort(key=lambda e: -e.score)
        return scored[:limit]
