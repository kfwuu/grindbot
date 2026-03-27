"""Task executor - runs Gemini CLI per task in isolated git worktrees.

Design rules enforced here:
  - No raw git calls (all git through worktree.py, rule #4)
  - No git checkout / branch -D / clean / reset --hard / push / merge (rule #9)
  - All output through Rich Console, never bare print() (rule #1)
  - All subprocess calls use subprocess.run (rule #3)
  - All errors caught, task marked failed, grind loop continues (rule #8)
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
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


def _build_task_prompt(task: dict, single_file_mode: bool = False) -> str:
    """Build the Gemini CLI prompt string for a single task.

    In single_file_mode the file content is supplied via stdin; Gemini is asked
    to output ONLY the corrected file — no prose, no fences.  This costs one
    API call instead of the 3-5 required when Gemini uses its file tools.

    Args:
        task: Task dict from tasks.json.
        single_file_mode: True when file content is piped via stdin.

    Returns:
        Multi-line prompt string ready to pass to Gemini CLI via -p.
    """
    category = _sanitize(task.get("category", "improvement"))
    severity = _sanitize(task.get("severity", "medium"))
    title = _sanitize(task.get("title", f"task-{task['id']}"))
    description = _sanitize(task.get("description", "no description provided"))
    file_hint = _sanitize(task.get("file") or "")
    line_hint = str(task.get("line") or "")

    if single_file_mode:
        # File content arrives via stdin.  Ask for raw output only.
        lines = [
            "The current file content is provided above via stdin.",
            "Output the COMPLETE corrected file. Nothing else.",
            "No explanation. No markdown fences. Raw source code only.",
            "Your response must start with the first character of the file.",
            "",
            f"FILE: {file_hint}",
        ]
        if line_hint:
            lines += [f"APPROX LINE: {line_hint}"]
        lines += [
            "",
            f"CHANGE NEEDED ({severity} {category}): {title}",
            "",
            description,
        ]
    else:
        # No file pre-loaded — Gemini uses its own file and web tools.
        # google_web_search and web_fetch are available and encouraged when
        # looking up docs, APIs, or best practices relevant to the fix.
        lines = ["TASK: Make one specific code change.", ""]
        if file_hint:
            lines += [f"FILE: {file_hint}"]
            if line_hint:
                lines += [f"LINE: approximately {line_hint}"]
            lines += [""]
        lines += [
            f"WHAT TO FIX ({severity} {category}): {title}",
            "",
            description,
            "",
            "RULES:",
            "- Change only what is described. Nothing else.",
            "- No new TODOs, comments, or debug code.",
            "- Stop as soon as the edit is saved.",
        ]

    return "\n".join(lines)


def _strip_fences(text: str) -> str:
    """Strip markdown code fences if Gemini wrapped its output despite instructions.

    Args:
        text: Raw stdout from Gemini.

    Returns:
        File content with fences removed, or the original text unchanged.
    """
    text = text.strip()
    m = re.match(r"^```(?:\w+)?\n(.*?)\n```\s*$", text, re.DOTALL)
    return m.group(1) if m else text


# ---------------------------------------------------------------------------
# Gemini CLI caller — single-API-call mode + tool-call fallback
# ---------------------------------------------------------------------------

_DEFAULT_MODEL: str = "gemini-2.5-pro"
_FLOOR_MODEL: str = "gemini-2.5-flash"

_MODEL_TIMEOUTS: dict[str, int] = {
    "gemini-2.5-pro": 20,
    "gemini-2.5-flash": 300,
}


def _run_single_file(
    gemini_path: str,
    model: str,
    prompt: str,
    cwd: Path,
    file_content: str,
    timeout: int,
    console: Console,
    system_md_path: Optional[Path] = None,
) -> tuple[int, str]:
    """Pipe file content to Gemini via stdin, stream stderr, capture stdout.

    Costs exactly one API call — Gemini reads from stdin instead of using
    its file tools, so there are no per-tool-call roundtrips.

    Args:
        system_md_path: Absolute path to GEMINI.md; passed to the subprocess
            as the GEMINI_SYSTEM_MD environment variable so Gemini CLI loads
            it as a system prompt without needing the file inside the worktree.

    Returns:
        (returncode, stdout).  returncode -1 means timeout.
    """
    env = os.environ.copy()
    if system_md_path is not None and system_md_path.exists():
        env["GEMINI_SYSTEM_MD"] = str(system_md_path)

    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(file_content)
            tmp_path = tmp.name

        stdin_fh = open(tmp_path, encoding="utf-8")
        proc = subprocess.Popen(
            [gemini_path, "--model", model, "-p", prompt, "--yolo"],
            cwd=str(cwd),
            stdin=stdin_fh,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        stdin_fh.close()

        stdout_lines: list[str] = []

        def _read_stdout() -> None:
            assert proc.stdout is not None
            for ln in proc.stdout:
                stdout_lines.append(ln)

        t = threading.Thread(target=_read_stdout, daemon=True)
        t.start()

        assert proc.stderr is not None
        for line in proc.stderr:
            stripped = line.rstrip()
            if stripped:
                console.print(f"    {stripped}")

        proc.wait(timeout=timeout)
        t.join(timeout=5)
        return proc.returncode, "".join(stdout_lines)

    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, ""
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _run_tool_mode(
    gemini_path: str,
    model: str,
    prompt: str,
    cwd: Path,
    timeout: int,
    system_md_path: Optional[Path] = None,
) -> tuple[int, str]:
    """Run Gemini in tool mode (no stdin), streaming all output to terminal.

    Fallback for tasks without a known target file.

    Args:
        system_md_path: Absolute path to GEMINI.md; injected as
            GEMINI_SYSTEM_MD so Gemini loads the system prompt without
            requiring the file to be present inside the worktree.

    Returns:
        (returncode, "").  returncode -1 means timeout.
    """
    env = os.environ.copy()
    if system_md_path is not None and system_md_path.exists():
        env["GEMINI_SYSTEM_MD"] = str(system_md_path)

    try:
        result = subprocess.run(
            [gemini_path, "--model", model, "-p", prompt, "--yolo"],
            cwd=str(cwd),
            timeout=timeout,
            env=env,
        )
        return result.returncode, ""
    except subprocess.TimeoutExpired:
        return -1, ""
    except Exception:
        return -2, ""


def _call_gemini(
    prompt: str,
    cwd: Path,
    console: Console,
    file_content: Optional[str] = None,
    system_md_path: Optional[Path] = None,
) -> tuple[bool, str, Optional[str]]:
    """Invoke Gemini CLI, using single-API-call mode when file content is supplied.

    Single-file mode (file_content provided):
      Pipes the file via stdin so Gemini outputs the corrected version in one
      API call.  GrindBot then writes the result back to disk.

    Tool mode (file_content=None):
      Gemini uses its built-in file tools; all output streams to terminal.
      Used as fallback for tasks with no specific target file.

    Args:
        prompt: Task prompt string.
        cwd: Worktree root directory.
        console: Rich console for status output.
        file_content: Current content of the target file, or None for tool mode.
        system_md_path: Absolute path to GEMINI.md, passed as GEMINI_SYSTEM_MD
            env var so Gemini loads the system prompt from the canonical source
            instead of looking for the file inside the worktree.

    Returns:
        (success, corrected_content_or_empty, error_message).
    """
    gemini_path = shutil.which("gemini")
    if gemini_path is None:
        return False, "", "Gemini CLI not found - ensure 'gemini' is on PATH"

    env_model = os.environ.get("GRINDBOT_MODEL", "").strip()
    models = [env_model] if env_model else [_DEFAULT_MODEL, _FLOOR_MODEL]

    for i, model in enumerate(models):
        if i > 0:
            console.print(f"    [yellow][!] Falling back to {model}...[/yellow]")

        mode_label = "single-file" if file_content is not None else "tool"
        console.print(f"    [dim]Model: {model}  ({mode_label} mode)[/dim]")
        console.print("    [dim]" + "-" * 56 + "[/dim]")

        timeout = _MODEL_TIMEOUTS.get(model, 300)

        if file_content is not None:
            rc, stdout = _run_single_file(
                gemini_path, model, prompt, cwd, file_content, timeout, console,
                system_md_path=system_md_path,
            )
        else:
            rc, stdout = _run_tool_mode(
                gemini_path, model, prompt, cwd, timeout,
                system_md_path=system_md_path,
            )

        console.print("    [dim]" + "-" * 56 + "[/dim]")

        if rc == -1:
            if i < len(models) - 1:
                console.print(
                    f"    [yellow][!] {model} timed out after {timeout}s, "
                    f"trying next model...[/yellow]"
                )
                continue
            return False, "", f"Gemini CLI timed out after {timeout}s"

        if rc == 0:
            return True, stdout, None

        if i < len(models) - 1:
            console.print(
                f"    [yellow][!] {model} exited {rc}, trying next model...[/yellow]"
            )
            continue

        return False, "", f"Gemini CLI exited with code {rc}"

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
      b. Resolve GEMINI.md path and pass it as GEMINI_SYSTEM_MD env var.
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
        # ---- b. Resolve GEMINI.md for GEMINI_SYSTEM_MD env var -------------
        # The file stays in the repo root — no copy into the worktree needed.
        system_md_path: Optional[Path] = None
        gemini_md = repo_root / "GEMINI.md"
        if gemini_md.exists():
            system_md_path = gemini_md
            console.print(f"    [dim]GEMINI_SYSTEM_MD -> {gemini_md}[/dim]")
        else:
            console.print("    [dim]GEMINI.md not found; running without system prompt.[/dim]")

        # ---- c. Call Gemini CLI --------------------------------------------
        # Single-file mode: read the target file ourselves and pipe it in.
        # Gemini outputs the corrected version in one API call; we write it back.
        # Falls back to tool mode if the task has no specific file or it's missing.
        file_hint = task.get("file") or ""
        file_content: Optional[str] = None
        if file_hint:
            target = worktree_path / file_hint
            if target.exists():
                try:
                    file_content = target.read_text(encoding="utf-8")
                except OSError:
                    file_content = None

        single_file = file_content is not None
        console.print(
            f"    [dim]Calling Gemini CLI "
            f"({'single-file' if single_file else 'tool'} mode)...[/dim]"
        )
        prompt = _build_task_prompt(task, single_file_mode=single_file)
        gem_ok, gem_out, gem_err = _call_gemini(
            prompt, worktree_path, console,
            file_content=file_content,
            system_md_path=system_md_path,
        )

        if not gem_ok:
            console.print(f"    [red]!! Gemini CLI failed:[/red] {gem_err}")
            task["status"] = "failed"
            task["error"] = gem_err
            return task

        # In single-file mode write Gemini's output back to the target file.
        if single_file and gem_out.strip():
            corrected = _strip_fences(gem_out)
            target = worktree_path / file_hint
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(corrected, encoding="utf-8")

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
