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
@click.option(
    "--goal",
    default=None,
    help="Optional direction for Claude, e.g. 'focus on reliability and retry logic'.",
)
def scan(path: str, goal: str) -> None:
    """Analyze a codebase with Claude Opus 4.6 and generate a prioritized task list."""
    from pathlib import Path as _Path
    from . import brain, config, planner, reporter
    from .scanner import _collect_source_files, _detect_languages

    if not brain._get_api_key():
        console.print(
            "[red]KIE_API_KEY not found.[/red] "
            "Add KIE_API_KEY=<key> to ~/.env"
        )
        sys.exit(1)

    grindbot_dir = config.get_grindbot_dir(path)
    try:
        grindbot_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        console.print(f"[red]Cannot create .grindbot/ directory: {exc}[/red]")
        sys.exit(1)

    project_path = _Path(path)

    langs, lang_file_count = _detect_languages(project_path)
    if langs:
        console.print(
            f"[dim]Detected languages: {', '.join(langs)} ({lang_file_count} file(s))[/dim]"
        )
    console.print("[dim]Collecting source files...[/dim]")
    source_context = _collect_source_files(project_path)
    if not source_context.strip():
        console.print("[red]No source files found.[/red]")
        sys.exit(1)

    gemini_md = project_path / "GEMINI.md"
    if gemini_md.exists():
        source_context = (
            gemini_md.read_text(encoding="utf-8", errors="replace")
            + "\n\n"
            + source_context
        )

    try:
        raw_tasks = brain.plan_tasks(source_context, goal=goal)
    except RuntimeError as exc:
        console.print(f"[red]Scan failed: {exc}[/red]")
        sys.exit(1)

    if not raw_tasks:
        console.print("[yellow]Claude returned no actionable tasks.[/yellow]")
        sys.exit(0)

    tasks = planner.plan(raw_tasks)
    config.save_tasks(path, tasks)
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
@click.option(
    "--no-reflect",
    is_flag=True,
    default=False,
    help="Skip prompt optimization (reflection loop) after grind.",
)
def grind(path: Path, limit: int, dry_run: bool, no_reflect: bool) -> None:
    """Execute pending tasks autonomously, each in its own git worktree."""
    from . import brain, config, reporter, scanner
    from . import executor
    from . import reflector
    from .config import check_dependencies, load_prompt_store
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

    # Load prompt store and inject evolved overrides before grind starts.
    store = load_prompt_store(grindbot_dir)
    if store.get("prompts"):
        iteration = store.get("iteration", "?")
        console.print(
            f"[dim]Loaded evolved prompts from .grindbot/prompts.json "
            f"(iteration {iteration})[/dim]"
        )
    brain.load_prompt_overrides(store)
    scanner.load_prompt_overrides(store)
    executor.load_prompt_overrides(store)

    tasks = run_grind(grindbot_dir, console, limit=limit, dry_run=dry_run)
    if not dry_run:
        reporter.show_grind_report(tasks, str(grindbot_dir.parent))

        if not no_reflect and tasks:
            console.rule("[bold cyan]Reflection Loop[/bold cyan]")
            reflector.run_reflection(grindbot_dir, tasks, console)


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------


def _normalise_id(raw: str) -> str:
    """Zero-pad a task ID string to three digits.

    Accepts "3", "03", or "003" and always returns "003".

    Args:
        raw: Raw task ID string as typed by the user.

    Returns:
        Three-digit zero-padded string, e.g. "003".
    """
    try:
        return str(int(raw)).zfill(3)
    except ValueError:
        return raw.strip()


@main.command()
@click.argument("ids", nargs=-1, metavar="[ID]...")
@click.option(
    "--all-failed",
    is_flag=True,
    default=False,
    help="Retry every task currently marked as failed.",
)
@click.option(
    "--reset-only",
    is_flag=True,
    default=False,
    help="Reset to pending without executing (picked up by the next grind run).",
)
@click.option(
    "--path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
    show_default=True,
    help="Project root to search for .grindbot/ directory.",
)
def retry(ids: tuple[str, ...], all_failed: bool, reset_only: bool, path: Path) -> None:
    """Reset failed or completed tasks and re-run them.

    \b
    Examples:
      grindbot retry 3          # reset task 003 and run it
      grindbot retry 3 7 12     # reset tasks 003, 007, 012 and run them
      grindbot retry --all-failed          # retry every failed task
      grindbot retry 3 --reset-only        # reset to pending, don't run yet
    """
    from . import config, reporter
    from .config import check_dependencies, find_grindbot_dir, load_tasks, save_tasks
    from .executor import retry_tasks

    if not ids and not all_failed:
        console.print(
            "[yellow]Specify at least one task ID or use --all-failed.[/yellow]\n"
            "  Example: [bold]grindbot retry 003[/bold]"
        )
        sys.exit(1)

    if not reset_only:
        ok, missing = check_dependencies()
        if not ok:
            console.print(f"[red]Missing required tools:[/red] {', '.join(missing)}")
            sys.exit(1)

    grindbot_dir = find_grindbot_dir(path.resolve())
    if grindbot_dir is None:
        console.print(
            "[red]No .grindbot/ directory found.[/red] "
            "Run [bold]grindbot init <path>[/bold] first."
        )
        sys.exit(1)

    all_tasks = load_tasks(grindbot_dir.parent)
    if not all_tasks:
        console.print("[yellow]No tasks found in tasks.json.[/yellow]")
        sys.exit(0)

    # --- Resolve the target ID list -----------------------------------------
    if all_failed:
        target_ids = [
            t["id"] for t in all_tasks if t.get("status") == "failed"
        ]
        if not target_ids:
            console.print("[yellow]No failed tasks to retry.[/yellow]")
            sys.exit(0)
        console.print(f"[dim]--all-failed: found {len(target_ids)} failed task(s)[/dim]")
    else:
        target_ids = [_normalise_id(i) for i in ids]

    # --- Reset-only mode: update tasks.json and exit ------------------------
    if reset_only:
        task_index = {t["id"]: i for i, t in enumerate(all_tasks)}
        reset_count = 0
        for tid in target_ids:
            if tid not in task_index:
                console.print(f"[yellow][!] Task {tid} not found — skipped.[/yellow]")
                continue
            task = all_tasks[task_index[tid]]
            current = task.get("status", "pending")
            if current == "pending":
                console.print(f"[yellow][!] Task {tid} is already pending — skipped.[/yellow]")
                continue
            if current not in ("failed", "completed"):
                console.print(
                    f"[yellow][!] Task {tid} has status '{current}' — skipped.[/yellow]"
                )
                continue
            task["status"] = "pending"
            task["branch"] = None
            task["error"] = None
            all_tasks[task_index[tid]] = task
            console.print(f"  [dim]Reset {tid} ({current} -> pending):[/dim] {task.get('title', '')}")
            reset_count += 1

        if reset_count:
            save_tasks(grindbot_dir.parent, all_tasks)
            console.print(
                f"\n[green]{reset_count} task(s) reset to pending.[/green] "
                "Run [bold]grindbot grind[/bold] to execute them."
            )
        else:
            console.print("[yellow]Nothing was reset.[/yellow]")
        return

    # --- Full retry: reset + execute ----------------------------------------
    tasks = retry_tasks(target_ids, grindbot_dir, console)
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
