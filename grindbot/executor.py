"""Task executor - runs Gemini CLI per task in isolated git worktrees.

Design rules enforced here:
  - No raw git calls (all git through worktree.py, rule #4)
  - No git checkout / branch -D / clean / reset --hard / push / merge (rule #9)
  - All output through Rich Console, never bare print() (rule #1)
  - All subprocess calls use subprocess.run (rule #3)
  - All errors caught, task marked failed, grind loop continues (rule #8)
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel

from . import worktree as wt
from .validator import validate_changes


# ---------------------------------------------------------------------------
# Windows CMD safety
# ---------------------------------------------------------------------------

# Characters safe to embed in a Gemini CLI -p prompt on Windows CMD.
# Pipe, double-quote, angle brackets, ampersand, and caret are banned
# because Windows spawns cmd.exe to run .CMD wrappers even when
# subprocess is called with a list (not shell=True).
_SAFE_CHARS: frozenset[str] = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789 .,:-_/\n\t()"
)


def _sanitize(text: str) -> str:
    """Replace shell-special characters with a space for Windows CMD safety.

    Args:
        text: Arbitrary text from a task dict field.

    Returns:
        Sanitized string containing only characters from _SAFE_CHARS.
    """
    return "".join(c if c in _SAFE_CHARS else " " for c in (text or ""))


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def _build_task_prompt(task: dict) -> str:
    """Build the Gemini CLI prompt string for a single task.

    Args:
        task: Task dict from tasks.json.

    Returns:
        Multi-line prompt string ready to pass to Gemini CLI via -p.
    """
    # Sanitize all free-text fields so the prompt is safe on Windows CMD.
    category = _sanitize(task.get("category", "improvement"))
    severity = _sanitize(task.get("severity", "medium"))
    title = _sanitize(task.get("title", f"task-{task['id']}"))
    description = _sanitize(task.get("description", "no description provided"))
    file_hint = _sanitize(task.get("file") or "")
    line_hint = str(task.get("line") or "")

    lines = [
        "You are an AI coding assistant performing a focused code improvement task.",
        "",
        f"Task ID: {task['id']}",
        f"Category: {category}",
        f"Severity: {severity}",
        f"Title: {title}",
        "",
        "Description:",
        description,
    ]
    if file_hint:
        lines += ["", f"Primary file: {file_hint}"]
        if line_hint:
            lines += [f"Approx. line: {line_hint}"]
    lines += [
        "",
        "Instructions:",
        "- Fix only the issue described above. Do not refactor unrelated code.",
        "- Keep changes minimal and focused.",
        "- Ensure the code remains correct and consistent with the surrounding codebase.",
        "- Do not leave debugging code, TODOs, or commented-out blocks.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gemini CLI caller
# ---------------------------------------------------------------------------

# GrindBot selects the model; Gemini CLI handles retries, checkpointing,
# and all other execution details natively (see GEMINI.md / v1.1 design).
_DEFAULT_MODEL: str = "gemini-2.5-pro"   # highest capability; try first
_FLOOR_MODEL: str = "gemini-2.5-flash"   # minimum acceptable fallback
_TASK_TIMEOUT: int = 300  # generous — gives Gemini CLI's own backoff room to breathe


def _call_gemini(
    prompt: str,
    cwd: Path,
    console: Console,
) -> tuple[bool, str, Optional[str]]:
    """Invoke Gemini CLI for a task, falling back from pro to flash on any failure.

    Model selection:
      - Honours ``GRINDBOT_MODEL`` env var if set (single model, no fallback).
      - Otherwise tries ``gemini-2.5-pro`` first, then ``gemini-2.5-flash``.
      - Never falls below 2.5-flash.

    Gemini CLI handles retries, rate-limit backoff, checkpointing, and process
    lifecycle internally — GrindBot does not reimplement those concerns.

    Args:
        prompt: The task prompt string.
        cwd: Working directory for the subprocess (the worktree root).
        console: Rich console for model/status messages.

    Returns:
        (success, stdout, error_message).
        success=False means the task should be marked failed.
    """
    gemini_path = shutil.which("gemini")
    if gemini_path is None:
        return False, "", "Gemini CLI not found - ensure 'gemini' is on PATH"

    env_model = os.environ.get("GRINDBOT_MODEL", "").strip()
    models = [env_model] if env_model else [_DEFAULT_MODEL, _FLOOR_MODEL]

    for i, model in enumerate(models):
        if i > 0:
            console.print(f"    [yellow][!] Falling back to {model}...[/yellow]")

        console.print(f"    [dim]Using model: {model}[/dim]")
        console.print("    [dim]" + "-" * 56 + "[/dim]")

        # stdout and stderr are NOT captured — they flow directly to the
        # terminal so the user sees Gemini's output in real time.
        # We only check returncode for success/failure.
        try:
            result = subprocess.run(
                [gemini_path, "--model", model, "-p", prompt, "--yolo"],
                cwd=str(cwd),
                timeout=_TASK_TIMEOUT,
                # No capture_output — inherits terminal for live streaming
            )
        except subprocess.TimeoutExpired:
            console.print("    [dim]" + "-" * 56 + "[/dim]")
            return False, "", f"Gemini CLI timed out after {_TASK_TIMEOUT}s"
        except Exception as exc:
            console.print("    [dim]" + "-" * 56 + "[/dim]")
            return False, "", f"Failed to start Gemini CLI: {exc}"

        console.print("    [dim]" + "-" * 56 + "[/dim]")

        if result.returncode == 0:
            return True, "", None

        # Non-zero exit — try next model if one is available.
        if i < len(models) - 1:
            console.print(f"    [yellow][!] {model} exited {result.returncode}, trying next model...[/yellow]")
            continue

        return False, "", f"Gemini CLI exited with code {result.returncode}"

    return False, "", "All models failed"


# ---------------------------------------------------------------------------
# Branch name helper
# ---------------------------------------------------------------------------


def _safe_branch_name(task_id: str, title: str) -> str:
    """Derive a safe git branch name from a task ID and title.

    Args:
        task_id: Zero-padded task ID string, e.g. "001".
        title: Human-readable task title.

    Returns:
        Branch name like 'grindbot/task-001-fix-missing-error-handling'.
    """
    slug = "".join(c if c.isalnum() else "-" for c in title.lower())
    # Collapse multiple dashes and strip leading/trailing
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-")[:50]
    return f"grindbot/task-{task_id}-{slug}"


# ---------------------------------------------------------------------------
# Single-task executor
# ---------------------------------------------------------------------------


def execute_task(
    task: dict,
    repo_root: Path,
    grindbot_dir: Path,
    console: Console,
) -> dict:
    """Execute one task in an isolated git worktree.

    Steps:
      a. Create worktree on a fresh branch.
      b. Copy GEMINI.md into the worktree for context.
      c. Call Gemini CLI with the task prompt.
      d. Validate changes (files changed, syntax, tests).
      e. Commit if valid; mark failed otherwise.
      f. Always clean up the worktree directory.

    Args:
        task: Task dict (will be mutated and returned with updated status).
        repo_root: Absolute path to the git repository root.
        grindbot_dir: Absolute path to the project's .grindbot/ directory.
        console: Rich console for progress output.

    Returns:
        Updated task dict with status, branch, and/or error set.
    """
    task_id = task["id"]
    title = task.get("title", f"task-{task_id}")
    branch_name = _safe_branch_name(task_id, title)
    worktrees_dir = grindbot_dir / ".worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    worktree_path = worktrees_dir / f"task-{task_id}"

    console.print(f"  [cyan]->[/cyan] Task [bold]{task_id}[/bold]: {title}")

    # ---- a. Create worktree ------------------------------------------------
    wt_ok, wt_err = wt.create_worktree(repo_root, branch_name, worktree_path)
    if not wt_ok:
        console.print(f"    [red]!! Worktree creation failed:[/red] {wt_err}")
        task["status"] = "failed"
        task["error"] = f"Worktree creation failed: {wt_err}"
        return task

    try:
        # ---- b. Copy GEMINI.md for context ---------------------------------
        gemini_md = repo_root / "GEMINI.md"
        if gemini_md.exists():
            shutil.copy2(str(gemini_md), str(worktree_path / "GEMINI.md"))

        # ---- c. Call Gemini CLI --------------------------------------------
        console.print("    [dim]Calling Gemini CLI...[/dim]")
        prompt = _build_task_prompt(task)
        gem_ok, _gem_out, gem_err = _call_gemini(prompt, worktree_path, console)

        if not gem_ok:
            console.print(f"    [red]!! Gemini CLI failed:[/red] {gem_err}")
            task["status"] = "failed"
            task["error"] = gem_err
            return task

        # ---- d. Show which files changed -----------------------------------
        changed_preview = wt.get_changed_files(worktree_path)
        if changed_preview:
            console.print(f"    [dim]Changed files ({len(changed_preview)}):[/dim]")
            for f in changed_preview:
                console.print(f"      [green]{f}[/green]")
        else:
            console.print("    [dim]No file changes detected yet.[/dim]")

        # ---- e. Validate ---------------------------------------------------
        console.print("    [dim]Validating changes...[/dim]")
        result = validate_changes(worktree_path, task)

        if result.warnings:
            for w in result.warnings:
                console.print(f"    [yellow][!][/yellow] {w}")

        if not result.success:
            console.print(f"    [red]!! Validation failed:[/red] {result.error}")
            task["status"] = "failed"
            task["error"] = result.error
            return task

        # ---- e. Commit -----------------------------------------------------
        commit_msg = (
            f"grindbot: {title}\n\n"
            f"Task-ID: {task_id}\n"
            f"Severity: {task.get('severity', 'medium')}\n"
            f"Category: {task.get('category', 'improvement')}\n\n"
            f"{task.get('description', '')[:500]}"
        )
        commit_ok, commit_err = wt.commit_worktree(worktree_path, commit_msg)

        if not commit_ok:
            console.print(f"    [red]!! Commit failed:[/red] {commit_err}")
            task["status"] = "failed"
            task["error"] = f"Commit failed: {commit_err}"
            return task

        console.print(
            f"    [green]OK Completed[/green] -> branch [cyan]{branch_name}[/cyan] "
            f"({len(result.changed_files)} file(s) changed)"
        )
        task["status"] = "completed"
        task["branch"] = branch_name
        task["error"] = None
        return task

    finally:
        # ---- f. Always clean up the worktree directory ---------------------
        keep = task.get("status") == "completed"
        wt.cleanup_worktree(repo_root, worktree_path, branch_name, keep_branch=keep)


# ---------------------------------------------------------------------------
# Grind loop
# ---------------------------------------------------------------------------


def run_grind(
    grindbot_dir: Path,
    console: Console,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> list[dict]:
    """Load pending tasks and execute each one sequentially.

    Saves progress to tasks.json after every task so that a crash or
    interruption does not lose work.

    Args:
        grindbot_dir: Absolute path to the project's .grindbot/ directory.
        console: Rich console for all output.
        limit: If set, only execute the first N pending tasks.
        dry_run: If True, show what would run and return without executing.

    Returns:
        Full updated list of all tasks (completed, failed, and pending).
    """
    from .config import find_repo_root, load_tasks, save_tasks
    from rich.table import Table

    # --- Load tasks ---------------------------------------------------------
    all_tasks = load_tasks(grindbot_dir.parent)
    if not all_tasks:
        console.print(
            "[yellow]No tasks found. Run [bold]grindbot scan <path>[/bold] first.[/yellow]"
        )
        return []

    pending = [t for t in all_tasks if t.get("status") == "pending"]
    if not pending:
        console.print(
            "[yellow]No pending tasks - everything is already completed or failed.[/yellow]"
        )
        return all_tasks

    if limit is not None:
        pending = pending[:limit]

    # --- Dry-run display ----------------------------------------------------
    if dry_run:
        table = Table(
            title=f"[bold]Dry run[/bold] - {len(pending)} task(s) would execute",
            border_style="yellow",
            show_lines=True,
        )
        table.add_column("#", style="dim", width=4, justify="right")
        table.add_column("Severity", width=9)
        table.add_column("Category", width=13)
        table.add_column("Title")
        for t in pending:
            sev = t.get("severity", "low")
            sev_color = {"high": "red", "medium": "yellow", "low": "green"}.get(sev, "white")
            table.add_row(
                t.get("id", "?"),
                f"[{sev_color}]{sev}[/{sev_color}]",
                t.get("category", ""),
                t.get("title", ""),
            )
        console.print(table)
        return all_tasks

    # --- Locate repo root ---------------------------------------------------
    repo_root = find_repo_root(grindbot_dir)
    if repo_root is None:
        console.print(
            "[red]Cannot locate the git repository root. "
            "Is the project inside a git repo?[/red]"
        )
        return all_tasks

    console.print(
        Panel(
            f"[bold]GrindBot[/bold] - executing [cyan]{len(pending)}[/cyan] "
            f"pending task(s) in {repo_root}",
            border_style="cyan",
        )
    )

    start = time.monotonic()

    # --- Execute each pending task ------------------------------------------
    for task in pending:
        try:
            updated = execute_task(task, repo_root, grindbot_dir, console)
        except Exception as exc:
            # Catch-all safety net - the grind loop must never crash
            console.print(f"    [red]!! Unhandled exception:[/red] {exc}")
            task["status"] = "failed"
            task["error"] = f"Unhandled exception: {exc}"
            updated = task

        # Merge the updated task back into the master list
        for i, t in enumerate(all_tasks):
            if t["id"] == updated["id"]:
                all_tasks[i] = updated
                break

        # Persist after every task
        try:
            save_tasks(grindbot_dir.parent, all_tasks)
        except Exception as exc:
            console.print(f"  [yellow][!] Could not save tasks.json:[/yellow] {exc}")

    elapsed = time.monotonic() - start
    console.print(f"\n[dim]Finished in {elapsed:.1f}s[/dim]")
    return all_tasks
