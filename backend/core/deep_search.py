"""
Deep filesystem search.

When the file indexer doesn't have a hit, this module walks the disk
on-demand using OS-native search tools (much faster than Python os.walk
for large trees) and returns matching paths.

Used as the fallback inside execution_engine._handle_open_app /
_handle_open_file / _handle_find_file so CHAPPIE can locate "anything
hidden anywhere" — code files, executables, documents, folders.
"""

import asyncio
import os
import re
import sys
from typing import List, Optional

from utils.logger import setup_logger

logger = setup_logger(__name__)


def default_search_roots() -> List[str]:
    """Reasonable per-OS roots for a deep search. Skips C:\\Windows etc."""
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        roots = [home]
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            roots.append(os.path.join(local_app, "Programs"))
        for env_var in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(env_var)
            if base and os.path.isdir(base):
                roots.append(base)
        return [r for r in roots if os.path.isdir(r)]
    if sys.platform == "darwin":
        return [r for r in (home, "/Applications") if os.path.isdir(r)]
    return [r for r in (home, "/opt", "/usr/local/bin") if os.path.isdir(r)]


SKIP_PARTS = {
    "node_modules", ".git", ".cache", "winsxs", "$recycle.bin",
    "system volume information", "driverstore", "cache",
}


async def deep_filesystem_search(
    query: str,
    roots: Optional[List[str]] = None,
    kind: str = "any",            # 'any' | 'file' | 'folder'
    extensions: Optional[List[str]] = None,
    limit: int = 20,
    timeout: float = 8.0,
) -> List[str]:
    """
    Search the filesystem for entries whose name contains `query`.
    Returns absolute paths, ordered by which root matched first.
    """
    roots = roots or default_search_roots()
    if not query or not roots:
        return []

    results: List[str] = []
    seen = set()

    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            chunk = await _native_search(root, query, kind, extensions, timeout)
            for p in chunk:
                if p and p not in seen:
                    seen.add(p)
                    results.append(p)
                    if len(results) >= limit:
                        return results
        except Exception as exc:
            logger.warning(f"Deep search failed for root={root}: {exc}")
    return results[:limit]


async def _native_search(root, query, kind, extensions, timeout):
    """
    Single-token searches go straight to the OS. Multi-word queries pick
    the longest token and then filter results that don't contain the
    others — handles "q4 report" matching "Q4_Report.docx", etc.
    """
    tokens = [t for t in re.split(r"\s+", query.strip()) if t]
    if len(tokens) <= 1:
        if sys.platform == "win32":
            return await _windows_search(root, query, kind, extensions, timeout)
        if sys.platform == "darwin":
            return await _mac_search(root, query, kind, extensions, timeout)
        return await _linux_search(root, query, kind, extensions, timeout)

    primary = max(tokens, key=len)
    if sys.platform == "win32":
        hits = await _windows_search(root, primary, kind, extensions, timeout)
    elif sys.platform == "darwin":
        hits = await _mac_search(root, primary, kind, extensions, timeout)
    else:
        hits = await _linux_search(root, primary, kind, extensions, timeout)

    others = [t.lower() for t in tokens if t != primary]
    return [p for p in hits if all(t in p.lower() for t in others)]


# ----------------------------------------------------------------------
# Per-OS implementations
# ----------------------------------------------------------------------

async def _windows_search(root, query, kind, extensions, timeout):
    """Windows: dir /s /b /a:[-D|D]  — bare recursive listing."""
    pattern_globs = []
    if extensions:
        for ext in extensions:
            pattern_globs.append(f"*{query}*.{ext.lstrip('.')}")
    else:
        pattern_globs.append(f"*{query}*")

    out: List[str] = []
    for pat in pattern_globs:
        attr = "/a:-D" if kind == "file" else ("/a:D" if kind == "folder" else "")
        cmd = f'cmd /c dir /s /b {attr} "{os.path.join(root, pat)}"'
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try: proc.kill()
            except Exception: pass
            continue
        for raw in stdout.decode("utf-8", errors="ignore").splitlines():
            path = raw.strip()
            if path and not _should_skip(path):
                out.append(path)
    return out


async def _linux_search(root, query, kind, extensions, timeout):
    cmd = ["find", root, "-iname", f"*{query}*"]
    if kind == "file":
        cmd += ["-type", "f"]
    elif kind == "folder":
        cmd += ["-type", "d"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass
        return []
    out = [
        line for line in stdout.decode("utf-8", errors="ignore").splitlines()
        if line and not _should_skip(line)
    ]
    if extensions:
        exts = {"." + e.lower().lstrip(".") for e in extensions}
        out = [p for p in out if os.path.splitext(p)[1].lower() in exts]
    return out


async def _mac_search(root, query, kind, extensions, timeout):
    """macOS: mdfind (Spotlight) — usually instant."""
    cmd = ["mdfind", "-name", query, "-onlyin", root]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, FileNotFoundError):
        return await _linux_search(root, query, kind, extensions, timeout)

    lines = [
        line for line in stdout.decode("utf-8", errors="ignore").splitlines()
        if line and not _should_skip(line)
    ]
    if kind == "file":
        lines = [p for p in lines if os.path.isfile(p)]
    elif kind == "folder":
        lines = [p for p in lines if os.path.isdir(p)]
    if extensions:
        exts = {"." + e.lower().lstrip(".") for e in extensions}
        lines = [p for p in lines if os.path.splitext(p)[1].lower() in exts]
    return lines


def _should_skip(path: str) -> bool:
    p = path.lower()
    parts = re.split(r"[\\/]+", p)
    return any(s in SKIP_PARTS for s in parts)


def rank_results(query: str, paths: List[str]) -> List[str]:
    """Re-rank by name closeness so the most likely target comes first."""
    q = (query or "").lower().strip()
    scored = []
    for p in paths:
        name = os.path.splitext(os.path.basename(p))[0].lower()
        if name == q:
            score = 100
        elif name.startswith(q):
            score = 80
        elif q in name:
            score = 60
        else:
            score = 30
        score -= min(len(p) // 60, 10)
        ext = os.path.splitext(p)[1].lower()
        if ext in (".lnk", ".exe", ".app", ".desktop"):
            score += 5
        if ext in (".dll", ".pdb", ".obj", ".o", ".pyc"):
            score -= 30
        scored.append((score, p))
    scored.sort(key=lambda t: -t[0])
    return [p for _, p in scored]
