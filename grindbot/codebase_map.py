"""Codebase map — persistent structural understanding across grind sessions.

Builds a compact JSON snapshot of the project structure, stores it at
.grindbot/codebase_map.json, and injects a ≤600-char summary into each
scan prompt so Claude develops a growing mental model of the codebase.

Public API:
    build_map(project_root, grindbot_dir, console)
    update_map_with_outcomes(grindbot_dir, tasks)
    get_map_context(grindbot_dir) -> str
    map_needs_rebuild(grindbot_dir, project_root) -> bool
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAP_FILE = "codebase_map.json"
_MAP_MAX_AGE_DAYS = 7
_MAP_STALE_COMMITS = 20
_MAP_CONTEXT_CAP = 600  # hard cap in chars for scan injection

_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".tox",
    ".eggs", "*.egg-info", ".grindbot", ".worktrees",
}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _git_head(project_root: Path) -> str:
    """Return current git HEAD commit hash, or '' on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _git_log(project_root: Path) -> str:
    """Return last 30 commits with changed file names."""
    try:
        result = subprocess.run(
            ["git", "log", "--name-only", "--oneline", "-30"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.stdout[:4000] if result.returncode == 0 else ""
    except Exception:
        return ""


def _commits_since(project_root: Path, commit: str) -> int:
    """Count commits between ``commit`` and HEAD. Returns large number on failure."""
    if not commit:
        return 9999
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{commit}..HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip().isdigit():
            return int(result.stdout.strip())
    except Exception:
        pass
    return 9999


def _collect_file_tree(project_root: Path) -> list[str]:
    """Walk project root and return relative file paths, skipping noise dirs."""
    paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        # Prune skip dirs in-place so os.walk doesn't descend into them
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.endswith(".egg-info")
        ]
        rel_dir = Path(dirpath).relative_to(project_root)
        for fname in filenames:
            paths.append(str(rel_dir / fname))
    return sorted(paths)[:500]  # cap to avoid absurd token count


def _read_key_files(project_root: Path, file_tree: list[str]) -> dict[str, str]:
    """Read content of up to 3 key files (entry point heuristics + largest)."""
    # Heuristic entry point candidates
    entry_candidates = [
        "main.py", "app.py", "index.py", "server.py",
        "manage.py", "run.py", "__main__.py",
        "src/main.py", "src/app.py",
        "index.js", "index.ts", "main.ts", "main.js",
    ]
    chosen: list[str] = []
    for candidate in entry_candidates:
        if candidate in file_tree and len(chosen) < 1:
            chosen.append(candidate)

    # Fill remaining slots with largest text files
    text_exts = {".py", ".js", ".ts", ".go", ".rs", ".rb", ".java", ".cs"}
    by_size: list[tuple[int, str]] = []
    for rel in file_tree:
        if any(rel.endswith(ext) for ext in text_exts) and rel not in chosen:
            abs_path = project_root / rel
            try:
                by_size.append((abs_path.stat().st_size, rel))
            except OSError:
                pass
    by_size.sort(reverse=True)
    for _, rel in by_size:
        if len(chosen) >= 3:
            break
        chosen.append(rel)

    result: dict[str, str] = {}
    for rel in chosen:
        try:
            content = (project_root / rel).read_text(encoding="utf-8", errors="replace")
            result[rel] = content[:2000]  # first 2000 chars only
        except OSError:
            pass
    return result


def _map_path(grindbot_dir: Path) -> Path:
    return grindbot_dir / _MAP_FILE


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def map_needs_rebuild(grindbot_dir: Path, project_root: Path) -> bool:
    """Return True if the codebase map should be (re)built before scanning.

    Rebuilds when:
    - Map file doesn't exist
    - Map is older than _MAP_MAX_AGE_DAYS
    - Git HEAD has diverged >_MAP_STALE_COMMITS commits from the map's commit

    Args:
        grindbot_dir: Path to .grindbot/ directory.
        project_root: Path to project root (for git commands).

    Returns:
        True if a rebuild is needed.
    """
    mp = _map_path(grindbot_dir)
    if not mp.exists():
        return True

    try:
        data = json.loads(mp.read_text(encoding="utf-8"))
    except Exception:
        return True

    # Age check
    built_at_str = data.get("built_at", "")
    if built_at_str:
        try:
            built_at = datetime.fromisoformat(built_at_str)
            if built_at.tzinfo is None:
                built_at = built_at.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - built_at).days
            if age_days > _MAP_MAX_AGE_DAYS:
                return True
        except ValueError:
            return True

    # Commit drift check
    commit_at_build = data.get("commit_at_build", "")
    if _commits_since(project_root, commit_at_build) > _MAP_STALE_COMMITS:
        return True

    return False


def build_map(project_root: Path, grindbot_dir: Path, console: Console) -> None:
    """Build the codebase map and save it to .grindbot/codebase_map.json.

    Collects file tree, git log, and key file contents, then asks Claude
    to produce a compact structural summary. Shows a Rich spinner while
    working. Silently skips if the Claude call fails (first scan still works).

    Args:
        project_root: Project root directory.
        grindbot_dir: Path to .grindbot/ directory (must already exist).
        console: Rich console for output.
    """
    from . import brain

    with console.status("[cyan]Building codebase map...[/cyan]", spinner="dots"):
        file_tree = _collect_file_tree(project_root)
        log = _git_log(project_root)
        key_files = _read_key_files(project_root, file_tree)
        head = _git_head(project_root)

        raw_map = brain.build_codebase_map(
            file_tree=file_tree,
            git_log=log,
            key_files=key_files,
        )

    if not raw_map:
        console.print("[yellow][!] Codebase map build skipped (Claude call failed).[/yellow]")
        return

    # Merge with existing map to preserve task_history and danger_zones
    mp = _map_path(grindbot_dir)
    existing: dict[str, Any] = {}
    if mp.exists():
        try:
            existing = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            pass

    merged = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "commit_at_build": head,
        **raw_map,
        # Preserve accumulated history across rebuilds
        "task_history": existing.get("task_history", {}),
        "danger_zones": {
            **raw_map.get("danger_zones", {}),
            **existing.get("danger_zones", {}),
        },
    }

    # Atomic write
    tmp = mp.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        tmp.replace(mp)
    except OSError as exc:
        console.print(f"[yellow][!] Could not save codebase map: {exc}[/yellow]")
        return

    n_files = len(file_tree)
    console.print(
        f"[dim]Codebase map built ({n_files} files, commit {head or 'unknown'}).[/dim]"
    )


def update_map_with_outcomes(grindbot_dir: Path, tasks: list[dict]) -> None:
    """Update task_history and danger_zones based on completed grind results.

    Called after run_grind() finishes. Increments per-file counters and
    flags files with ≥2 failures as danger zones. Uses atomic write.

    Args:
        grindbot_dir: Path to .grindbot/ directory.
        tasks: The full task list (all statuses) after a grind session.
    """
    mp = _map_path(grindbot_dir)
    if not mp.exists():
        return  # No map yet — nothing to update

    try:
        data = json.loads(mp.read_text(encoding="utf-8"))
    except Exception:
        return

    history: dict[str, dict[str, int]] = data.get("task_history", {})
    danger: dict[str, str] = data.get("danger_zones", {})

    for task in tasks:
        status = task.get("status", "")
        if status not in ("completed", "failed", "abandoned"):
            continue
        file_key = task.get("file") or ""
        if not file_key:
            continue
        if file_key not in history:
            history[file_key] = {"completed": 0, "failed": 0}
        if status == "completed":
            history[file_key]["completed"] = history[file_key].get("completed", 0) + 1
        else:
            history[file_key]["failed"] = history[file_key].get("failed", 0) + 1
            fail_count = history[file_key]["failed"]
            if fail_count >= 2:
                danger[file_key] = f"{fail_count} failed tasks — skip"

    data["task_history"] = history
    data["danger_zones"] = danger

    tmp = mp.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(mp)
    except OSError:
        pass  # Non-fatal — map just won't reflect latest outcomes


def get_hot_file_contents(grindbot_dir: Path, project_root: Path) -> str:
    """Read hot files + entry points from the codebase map and return their contents.

    Returns a labelled string (same format as scanner._collect_source_files)
    containing only the files identified as important by the map, keeping the
    scan prompt small while giving Claude enough context to find real issues.

    Args:
        grindbot_dir: Path to .grindbot/ directory.
        project_root: Project root directory.

    Returns:
        Multi-section string with file contents, or "" if no map exists.
    """
    mp = _map_path(grindbot_dir)
    if not mp.exists():
        return ""

    try:
        data = json.loads(mp.read_text(encoding="utf-8"))
    except Exception:
        return ""

    # Collect unique file paths from hot_files + entry_points
    targets: list[str] = []
    seen: set[str] = set()
    for key in ("hot_files", "entry_points"):
        for f in data.get(key, []):
            f = str(f)
            if f not in seen:
                seen.add(f)
                targets.append(f)

    # Also include any files from danger_zones (they need re-checking)
    for f in data.get("danger_zones", {}):
        f = str(f)
        if f not in seen:
            seen.add(f)
            targets.append(f)

    parts: list[str] = []
    for rel in targets:
        abs_path = project_root / rel
        if not abs_path.is_file():
            continue
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
            parts.append(f"=== {rel} ===\n{content}\n")
        except OSError:
            continue

    return "\n".join(parts)


def get_map_context(grindbot_dir: Path) -> str:
    """Read the codebase map and return a compact ≤600-char string for scan injection.

    Returns "" if the map doesn't exist yet (first scan works without it).

    Args:
        grindbot_dir: Path to .grindbot/ directory.

    Returns:
        Compact summary string, or "" if no map is available.
    """
    mp = _map_path(grindbot_dir)
    if not mp.exists():
        return ""

    try:
        data = json.loads(mp.read_text(encoding="utf-8"))
    except Exception:
        return ""

    parts: list[str] = []

    # Session count approximation from task_history
    history = data.get("task_history", {})
    session_hint = f"{len(history)} file(s) tracked" if history else ""
    header = "[Codebase Map"
    if session_hint:
        header += f" — {session_hint}"
    header += "]"
    parts.append(header)

    entry_points = data.get("entry_points", [])
    if entry_points:
        parts.append("Entry points: " + ", ".join(str(e) for e in entry_points[:4]))

    core_dirs = data.get("core_dirs", {})
    if core_dirs:
        core_str = ", ".join(
            f"{k} ({v})" for k, v in list(core_dirs.items())[:4]
        )
        parts.append("Core: " + core_str)

    hot_files = data.get("hot_files", [])
    if hot_files:
        parts.append("Hot files: " + ", ".join(str(f) for f in hot_files[:4]))

    danger_zones = data.get("danger_zones", {})
    if danger_zones:
        dz_parts = [f"{k} — {v}" for k, v in list(danger_zones.items())[:3]]
        parts.append("Danger: " + "; ".join(dz_parts))

    patterns = data.get("patterns", [])
    if patterns:
        parts.append("Patterns: " + ", ".join(str(p) for p in patterns[:4]))

    skip_hints = data.get("skip_hints", [])
    if skip_hints:
        parts.append("Skip: " + "; ".join(str(h) for h in skip_hints[:2]))

    result = "\n".join(parts)
    # Hard cap at _MAP_CONTEXT_CAP chars
    if len(result) > _MAP_CONTEXT_CAP:
        result = result[: _MAP_CONTEXT_CAP - 3] + "..."
    return result
