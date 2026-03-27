"""GrindBot CLI — entry point with scan, grind, report, and init commands."""
import sys
from pathlib import Path

import click
from rich.console import Console

from grindbot import __version__

console = Console()


@click.group()
@click.version_option(version=__version__, prog_name="grindbot")
def main() -> None:
    """GrindBot — autonomous code improvement via Gemini CLI.

    Point GrindBot at any codebase to find issues, fix them overnight
    in isolated git worktrees, and wake up to merge-ready branches.
    """


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@main.command("init")
@click.argument("path", type=click.Path(resolve_path=True))
def init_cmd(path: str) -> None:
    """Set up .grindbot/ directory and generate GEMINI.md in a project."""
    from .config import init_project

    init_project(path)


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


@main.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, resolve_path=True))
def scan(path: str) -> None:
    """Analyze a codebase and generate a prioritized task list."""
    from . import config, planner, reporter, scanner

    # Ensure .grindbot/ directory exists in the target project.
    grindbot_dir = config.get_grindbot_dir(path)
    try:
        grindbot_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        console.print(f"[red]Cannot create .grindbot/ directory: {exc}[/red]")
        sys.exit(1)

    # Call Gemini CLI and get raw validated issues.
    try:
        raw_tasks = scanner.scan_project(path)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)
    except RuntimeError as exc:
        console.print(f"[red]Scan failed: {exc}[/red]")
        sys.exit(1)

    if not raw_tasks:
        console.print("[yellow]Gemini returned no actionable issues.[/yellow]")
        sys.exit(0)

    # Deduplicate, prioritize, assign IDs.
    tasks = planner.plan(raw_tasks)

    # Persist to .grindbot/tasks.json.
    config.save_tasks(path, tasks)

    # Display as a Rich table.
    reporter.show_scan_results(tasks, path)


# ---------------------------------------------------------------------------
# grind
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
    show_default=True,
    help="Project root to search for .grindbot/ directory.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Maximum number of pending tasks to execute.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show which tasks would run without executing them.",
)
def grind(path: Path, limit: int, dry_run: bool) -> None:
    """Execute pending tasks autonomously, each in its own git worktree."""
    from . import config, reporter
    from .config import check_dependencies
    from .executor import run_grind

    ok, missing = check_dependencies()
    if not ok:
        console.print(f"[red]Missing required tools:[/red] {', '.join(missing)}")
        sys.exit(1)

    grindbot_dir = config.find_grindbot_dir(path.resolve())
    if grindbot_dir is None:
        console.print(
            "[red]No .grindbot/ directory found.[/red] "
            "Run [bold]grindbot init <path>[/bold] first."
        )
        sys.exit(1)

    tasks = run_grind(grindbot_dir, console, limit=limit, dry_run=dry_run)
    if not dry_run:
        reporter.show_grind_report(tasks, str(grindbot_dir.parent))


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@main.command()
@click.argument(
    "path",
    default=".",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    required=False,
)
def report(path: str) -> None:
    """Show the current task list and grind session results.

    PATH defaults to the current directory.
    """
    from . import config, reporter

    tasks = config.load_tasks(path)
    reporter.show_grind_report(tasks, path)
