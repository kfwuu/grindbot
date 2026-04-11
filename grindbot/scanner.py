"""Scanner: utilities for collecting and categorizing source files in a project.

Provides helpers to walk a project tree, skip irrelevant directories,
and identify source files by extension across many programming languages.
"""
from pathlib import Path
from typing import Any

from rich.console import Console

console = Console()

# Directory names to skip when collecting source files.
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git", "__pycache__", ".grindbot", ".worktrees", "node_modules",
    ".venv", "venv", "env", ".env", "dist", "build", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
})

# Source file extensions to collect across all supported languages.
_SOURCE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift",
    ".kt", ".scala", ".r", ".m", ".sh", ".bash", ".zsh", ".ps1",
    ".lua", ".dart", ".ex", ".exs", ".ml", ".hs", ".clj",
})

# Maps file extension to human-readable language name for display.
_EXT_TO_LANG: dict[str, str] = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
    ".jsx": "JavaScript", ".tsx": "TypeScript", ".go": "Go",
    ".rs": "Rust", ".java": "Java", ".c": "C", ".cpp": "C++",
    ".h": "C/C++", ".hpp": "C++", ".cs": "C#", ".rb": "Ruby",
    ".php": "PHP", ".swift": "Swift", ".kt": "Kotlin",
    ".scala": "Scala", ".r": "R", ".m": "Objective-C",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell",
    ".ps1": "PowerShell", ".lua": "Lua", ".dart": "Dart",
    ".ex": "Elixir", ".exs": "Elixir", ".ml": "OCaml",
    ".hs": "Haskell", ".clj": "Clojure",
}

# ---------------------------------------------------------------------------
# Prompt override injection (filled by cli.py before each grind run)
# ---------------------------------------------------------------------------

_PROMPT_OVERRIDES: dict = {}


def load_prompt_overrides(store: dict) -> None:
    """Inject evolved prompts from the prompt store into this module.

    Called by cli.py after loading .grindbot/prompts.json, before grind starts.

    Args:
        store: Full prompt store dict as returned by config.load_prompt_store().
    """
    global _PROMPT_OVERRIDES
    _PROMPT_OVERRIDES = store.get("prompts", {})


def _detect_languages(project_path: Path) -> tuple[list[str], int]:
    """Detect programming languages present in a project.

    Walks the project tree applying the same skip logic as _collect_source_files
    and returns the distinct language names found plus the total file count.

    Args:
        project_path: Resolved absolute path to the project root.

    Returns:
        Tuple of (sorted list of language names, total source file count).
    """
    seen_langs: set[str] = set()
    total = 0

    for f in project_path.rglob("*"):
        if not f.is_file():
            continue
        try:
            rel = f.relative_to(project_path)
        except ValueError:
            continue
        if any(
            part.startswith(".") or part in _SKIP_DIRS or part.endswith(".egg-info")
            for part in rel.parts[:-1]
        ):
            continue
        lang = _EXT_TO_LANG.get(f.suffix.lower())
        if lang:
            seen_langs.add(lang)
            total += 1

    return sorted(seen_langs), total


def _collect_source_files(project_path: Path) -> str:
    """Read source files and return them as a single labelled string.

    Collects all files whose extension is in _SOURCE_EXTENSIONS, skipping
    hidden directories, build artefacts, virtual environments, and other
    non-source trees listed in _SKIP_DIRS.  Prints each file to the console
    as it is collected so the user can see progress.

    Args:
        project_path: Resolved absolute path to the project root.

    Returns:
        Multi-section string with each file prefixed by a === path === header.
        Returns an empty string if no matching source files are found.
    """
    parts: list[str] = []
    skipped = 0

    all_files = sorted(
        f for f in project_path.rglob("*")
        if f.is_file() and f.suffix.lower() in _SOURCE_EXTENSIONS
    )
    total_files = len(all_files)

    for idx, src_file in enumerate(all_files, start=1):
        rel = src_file.relative_to(project_path)

        # Skip any path that passes through a disallowed directory name.
        if any(
            part.startswith(".") or part in _SKIP_DIRS or part.endswith(".egg-info")
            for part in rel.parts[:-1]  # exclude the filename itself
        ):
            skipped += 1
            continue

        try:
            content = src_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped += 1
            continue

        chunk = f"=== {rel.as_posix()} ===\n{content}\n"

        # Show each file as it's collected: [idx/total]  path  (N lines)
        line_count = content.count("\n")
        console.print(
            f"  [dim][{idx}/{total_files}][/dim]  [cyan]{rel.as_posix()}[/cyan]"
            f"  [dim]{line_count} lines[/dim]"
        )
        parts.append(chunk)

    included = len(parts)
    console.print(
        f"  [dim]Collected {included} file(s)"
        + (f", skipped {skipped}" if skipped else "")
        + f"[/dim]"
    )
    return "\n".join(parts)
