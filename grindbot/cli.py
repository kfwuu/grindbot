"""GrindBot CLI — entry point with scan, grind, report, and init commands."""
import json
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
    from .scanner import _collect_source_files as collect_source_files, _detect_languages

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

    from . import codebase_map as _cmap
    if _cmap.map_needs_rebuild(grindbot_dir, project_path):
        _cmap.build_map(project_path, grindbot_dir, console)
    map_ctx = _cmap.get_map_context(grindbot_dir)

    # When a codebase map exists, only send hot files (not all 219 files).
    # This cuts scan cost from ~120 credits to ~10.
    if map_ctx:
        source_context = _cmap.get_hot_file_contents(grindbot_dir, project_path)
        console.print(f"[dim]Using codebase map + hot files (lightweight scan)[/dim]")
    else:
        with console.status("[cyan]Collecting source files...[/cyan]", spinner="dots"):
            source_context = collect_source_files(project_path)

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

    brain.reset_task_credits()
    try:
        raw_tasks = brain.plan_tasks(source_context, goal=goal, map_context=map_ctx)
    except RuntimeError as exc:
        console.print(f"[red]Scan failed: {exc}[/red]")
        sys.exit(1)

    scan_credits = brain.get_task_credits()
    scan_usd = scan_credits * config.CREDIT_COST_USD
    console.print(
        f"  [bold green]Scan cost: {scan_credits:.2f} credits -> ${scan_usd:.4f}[/bold green]"
    )

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
@click.option(
    "--workers",
    type=int,
    default=1,
    show_default=True,
    help="Number of tasks to run in parallel.",
)
@click.option(
    "--sandbox",
    "use_sandbox",
    is_flag=True,
    default=False,
    help="Run tasks in Firecracker microVMs on your remote server.",
)
@click.option(
    "--no-sync",
    "no_sync",
    is_flag=True,
    default=False,
    help="Skip git fetch/rebase from origin before grinding.",
)
def grind(path: Path, limit: int, dry_run: bool, no_reflect: bool, workers: int, use_sandbox: bool, no_sync: bool) -> None:
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
    brain.load_prompt_overrides(store)
    scanner.load_prompt_overrides(store)
    executor.load_prompt_overrides(store)

    if use_sandbox:
        console.print("[bold cyan]Sandbox mode:[/bold cyan] tasks will run in Firecracker microVMs on your server.")

    tasks, grind_credits, session_id = run_grind(
        grindbot_dir, console, limit=limit, dry_run=dry_run,
        workers=workers, use_sandbox=use_sandbox, auto_sync=not no_sync,
    )
    if not dry_run:
        reporter.show_grind_report(tasks, str(grindbot_dir.parent))

        if not no_reflect and tasks:
            console.rule("[bold cyan]Reflection Loop[/bold cyan]")
            reflector.run_reflection(grindbot_dir, tasks, console, session_id=session_id)


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
@click.option(
    "--sandbox",
    "use_sandbox",
    is_flag=True,
    default=False,
    help="Run retried tasks in Firecracker microVMs on your remote server.",
)
def retry(ids: tuple[str, ...], all_failed: bool, reset_only: bool, path: Path, use_sandbox: bool) -> None:
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
    if use_sandbox:
        console.print("[bold cyan]Sandbox mode:[/bold cyan] tasks will run in Firecracker microVMs on your server.")
    tasks = retry_tasks(target_ids, grindbot_dir, console, use_sandbox=use_sandbox)
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


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


@main.command("push")
@click.argument("files", nargs=-1)
@click.option(
    "--message", "-m",
    default=None,
    help="Commit message (auto-generated from changed file names if omitted).",
)
@click.option(
    "--path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
    show_default=True,
    help="Project root to find the git repository.",
)
def push_cmd(files: tuple[str, ...], message: str, path: Path) -> None:
    """Stage, commit, and push grindbot source changes to origin.

    \b
    Examples:
      grindbot push grindbot/reflector.py grindbot/brain.py
      grindbot push -m "perf: cut reflect cost"
      grindbot push                     # stages all modified grindbot/*.py
    """
    import subprocess as _sp
    from . import worktree as wt

    repo_root = path.resolve()

    # Resolve files to stage: explicit list or fall back to modified grindbot/*.py
    if files:
        to_stage = list(files)
    else:
        status = _sp.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
        modified = [
            line[3:].strip()
            for line in status.stdout.splitlines()
            if line.strip() and "grindbot/" in line[3:]
        ]
        if not modified:
            console.print("[yellow]No modified grindbot/ files to push.[/yellow]")
            return
        to_stage = modified

    # Stage
    add = _sp.run(
        ["git", "add"] + to_stage,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if add.returncode != 0:
        console.print(f"[red]git add failed:[/red] {add.stderr.strip()}")
        return

    # Auto-generate commit message from file names if not provided
    if not message:
        basenames = ", ".join(Path(f).name for f in to_stage)
        message = f"chore: update {basenames}"

    # Commit
    commit = _sp.run(
        ["git", "commit", "-m", message],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        stderr = commit.stderr.strip() + commit.stdout.strip()
        if "nothing to commit" in stderr:
            console.print("[yellow]Nothing to commit — working tree is clean.[/yellow]")
            return
        console.print(f"[red]git commit failed:[/red] {commit.stderr.strip()}")
        return

    console.print(f"[green]Committed:[/green] {message}")

    # Push via worktree.push_branch (respects no-remote guard)
    default_branch = wt.get_default_branch(repo_root)
    ok, err = wt.push_branch(repo_root, default_branch)
    if not ok:
        console.print(f"[red]git push failed:[/red] {err}")
        return

    console.print(f"[green]Pushed[/green] {default_branch} to origin.")


# ---------------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------------


@main.command("daemon")
@click.option(
    "--path",
    "path",
    default=".",
    show_default=True,
    type=click.Path(exists=True),
    help="Project root.",
)
@click.option(
    "--workers",
    type=int,
    default=3,
    show_default=True,
    help="Parallel task workers per cycle.",
)
@click.option(
    "--interval",
    type=int,
    default=3600,
    show_default=True,
    help="Seconds between grind cycles.",
)
@click.option(
    "--budget",
    type=float,
    default=None,
    help="Stop after spending this many USD (cumulative).",
)
@click.option(
    "--no-sync",
    "no_sync",
    is_flag=True,
    default=False,
    help="Skip git fetch/rebase from origin at the start of each cycle.",
)
def daemon(path: str, workers: int, interval: int, budget: float, no_sync: bool) -> None:
    """Run grind → reflect → rescan in a continuous loop."""
    import time
    from rich.panel import Panel
    from . import brain, config, executor, planner, reflector, scanner
    from .brain import plan_tasks

    resolved_path = Path(path).resolve()
    grindbot_dir = config.find_grindbot_dir(resolved_path)
    if grindbot_dir is None:
        console.print(
            "[red]No .grindbot/ directory found.[/red] "
            "Run [bold]grindbot init <path>[/bold] first."
        )
        sys.exit(1)
    project_path = grindbot_dir.parent

    total_usd = 0.0
    cycle = 0

    # Resume accumulated spend from a previous run so --budget is reliable across restarts.
    state_file = grindbot_dir / "daemon-state.json"
    if state_file.exists():
        try:
            _saved = json.loads(state_file.read_text())
            total_usd = float(_saved.get("total_usd", 0.0))
            cycle = int(_saved.get("cycle_count", 0))
            console.print(
                f"[dim]Resumed daemon state: ${total_usd:.4f} spent, "
                f"{cycle} cycle(s) completed previously.[/dim]"
            )
        except Exception as _exc:
            console.print(f"[yellow][!] Could not load daemon-state.json ({_exc}) — starting fresh.[/yellow]")

    console.print(Panel(
        f"[bold green]Daemon started[/bold green]  workers={workers}  interval={interval}s"
        + (f"  budget=${budget:.2f}" if budget else "  no budget cap"),
        title="GrindBot Daemon",
    ))

    try:
        while True:
            cycle += 1
            console.rule(f"[bold]Cycle {cycle}[/bold]")

            # 1. Grind pending tasks (reset credits first so we can read grind cost)
            brain.reset_task_credits()
            tasks, _, session_id = executor.run_grind(
                grindbot_dir, console, workers=workers, auto_sync=not no_sync,
            )
            cycle_credits = brain.get_task_credits()

            # 2. Prompt RL reflection (resets/reads its own credits internally)
            reflector.run_reflection(grindbot_dir, tasks, console, session_id=session_id)

            # 3. Budget check
            cycle_usd = cycle_credits * config.CREDIT_COST_USD
            total_usd += cycle_usd
            console.print(f"  [dim]Cycle cost: ${cycle_usd:.4f}  Total: ${total_usd:.4f}[/dim]")

            # Persist state so budget enforcement survives daemon restarts.
            try:
                import datetime
                state_file.write_text(json.dumps({
                    "total_usd": total_usd,
                    "cycle_count": cycle,
                    "last_cycle_ts": datetime.datetime.utcnow().isoformat(),
                }, indent=2))
            except Exception as _exc:
                console.print(f"[yellow][!] Could not save daemon-state.json ({_exc})[/yellow]")

            if budget and total_usd >= budget:
                console.print(f"[yellow]Budget ${budget:.2f} reached — daemon stopping.[/yellow]")
                break

            # 4. Re-scan for new tasks
            try:
                console.print("  [dim]Re-scanning codebase for new issues...[/dim]")
                source_context = scanner.collect_source_files(project_path)
                raw_new = plan_tasks(source_context)
                all_tasks = config.load_tasks(grindbot_dir.parent)
                merged = planner.merge_new_tasks(all_tasks, raw_new)
                new_count = len(merged) - len(all_tasks)
                config.save_tasks(grindbot_dir.parent, merged)
                if new_count:
                    console.print(f"  [green]+{new_count} new task(s) queued.[/green]")
                else:
                    console.print("  [dim]No new tasks found.[/dim]")
            except RuntimeError as exc:
                console.print(f"  [yellow][!] Re-scan failed (skipping): {exc}[/yellow]")

            # 5. Sleep
            console.print(f"  [dim]Next cycle in {interval}s — Ctrl+C to stop.[/dim]")
            time.sleep(interval)

    except KeyboardInterrupt:
        console.print("\n[yellow]Daemon stopped.[/yellow]")
