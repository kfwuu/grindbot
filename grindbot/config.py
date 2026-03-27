"""Dependency checks, path utilities, and task persistence for GrindBot."""
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from rich.console import Console

console = Console()


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------


def check_gemini_cli() -> bool:
    """Return True if the Gemini CLI is available on PATH."""
    return shutil.which("gemini") is not None


def check_dependencies() -> tuple[bool, list[str]]:
    """Check that git and gemini are both on PATH.

    Returns:
        Tuple of (all_present, list_of_missing_tools).
    """
    missing = [tool for tool in ("git", "gemini") if shutil.which(tool) is None]
    return len(missing) == 0, missing


# ---------------------------------------------------------------------------
# Path utilities
# ---------------------------------------------------------------------------


def get_grindbot_dir(project_path: str | Path) -> Path:
    """Return the .grindbot directory path for a given project root."""
    return Path(project_path) / ".grindbot"


def find_grindbot_dir(start: Path) -> Optional[Path]:
    """Walk up from start looking for a .grindbot/ directory.

    Args:
        start: Directory to begin searching from.

    Returns:
        Path to .grindbot/ directory, or None if not found.
    """
    current = start.resolve()
    while True:
        candidate = current / ".grindbot"
        if candidate.is_dir():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


def get_tasks_path(project_path: str | Path) -> Path:
    """Return the path to tasks.json for a given project root."""
    return get_grindbot_dir(project_path) / "tasks.json"


def find_repo_root(grindbot_dir: Path) -> Optional[Path]:
    """Find the git repository root that contains the .grindbot directory.

    Args:
        grindbot_dir: Path to the .grindbot/ directory.

    Returns:
        Path to the git repo root, or None if not in a git repo.
    """
    project_root = grindbot_dir.parent
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return Path(result.stdout.strip())
    return None


# ---------------------------------------------------------------------------
# Task persistence
# ---------------------------------------------------------------------------


def load_tasks(project_path: str | Path) -> list[dict[str, Any]]:
    """Load tasks from .grindbot/tasks.json.

    Args:
        project_path: Root directory of the project (not the .grindbot/ dir).

    Returns:
        List of task dicts, or empty list if file missing or corrupt.
    """
    tasks_path = get_tasks_path(project_path)
    if not tasks_path.exists():
        return []
    try:
        return json.loads(tasks_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        console.print(f"[red]Error loading tasks: {exc}[/red]")
        return []


def save_tasks(project_path: str | Path, tasks: list[dict[str, Any]]) -> None:
    """Persist tasks to .grindbot/tasks.json, creating the directory if needed.

    Args:
        project_path: Root directory of the project (not the .grindbot/ dir).
        tasks: List of task dicts to persist.
    """
    tasks_path = get_tasks_path(project_path)
    try:
        tasks_path.parent.mkdir(parents=True, exist_ok=True)
        tasks_path.write_text(
            json.dumps(tasks, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        console.print(f"[red]Error saving tasks: {exc}[/red]")


# ---------------------------------------------------------------------------
# Project initialisation
# ---------------------------------------------------------------------------


def init_project(project_path: str | Path) -> bool:
    """Initialise .grindbot/ and .worktrees/ directories in a project.

    Args:
        project_path: Root directory of the project to initialise.

    Returns:
        True on success, False if the path does not exist.
    """
    project_path = Path(project_path).resolve()

    if not project_path.exists():
        console.print(f"[red]Path does not exist: {project_path}[/red]")
        return False

    grindbot_dir = get_grindbot_dir(project_path)
    worktrees_dir = project_path / ".worktrees"

    try:
        grindbot_dir.mkdir(parents=True, exist_ok=True)
        worktrees_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        console.print(f"[red]Error creating directories: {exc}[/red]")
        return False

    # Seed an empty tasks.json if one does not exist yet.
    if not get_tasks_path(project_path).exists():
        save_tasks(project_path, [])

    # Copy GEMINI.md from the GrindBot package root into the target project.
    gemini_src = Path(__file__).parent.parent / "GEMINI.md"
    gemini_dst = project_path / "GEMINI.md"
    if gemini_src.exists() and not gemini_dst.exists():
        shutil.copy2(gemini_src, gemini_dst)
        console.print(f"[green]Copied GEMINI.md -> {gemini_dst}[/green]")

    console.print(f"[green]Initialized GrindBot in {project_path}[/green]")
    console.print("  [dim].grindbot/  created[/dim]")
    console.print("  [dim].worktrees/ created[/dim]")

    if not check_gemini_cli():
        console.print(
            "[yellow]Warning: 'gemini' CLI not found on PATH. "
            "Install it before running [bold]grindbot scan[/bold] "
            "or [bold]grindbot grind[/bold].[/yellow]"
        )

    return True
