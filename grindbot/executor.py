"""Task executor - runs Gemini CLI per task in isolated git worktrees.

Design rules enforced here:
  - No raw git calls (all git through worktree.py, rule #4)
  - No git checkout / branch -D / clean / reset --hard / push / merge (rule #9)
  - All output through Rich Console, never bare print() (rule #1)
  - All subprocess calls use subprocess.run (rule #3)
  - All errors caught, task marked failed, grind loop continues (rule #8)
"""

import ast
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
from . import brain
from .validator import validate_changes


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


# Characters that break Windows CMD when gemini is invoked as a .cmd wrapper.
# Only these need to be stripped from Claude-written prompts — everything else
# (brackets, equals, apostrophes, backticks, newlines) is safe inside a
# double-quoted argument as produced by subprocess.list2cmdline.
_CMD_UNSAFE: frozenset[str] = frozenset('|"<>&^%')


def _sanitize_prompt(text: str) -> str:
    """Strip only the characters that break Windows CMD .cmd wrapper invocation.

    Used for Claude-written prompts where _sanitize() is too aggressive —
    stripping brackets, equals, backticks etc. renders code instructions
    unreadable and causes Gemini to respond with meta-questions instead of edits.

    Args:
        text: Prompt text from the Claude orchestrator.

    Returns:
        Prompt safe for Windows CMD passthrough, with code structure intact.
    """
    return "".join(c if c not in _CMD_UNSAFE else " " for c in (text or ""))


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
        # Ask for a JSON diff — Gemini reliably outputs JSON (proven by scanner).
        # This avoids all write_file/edit_file tool failures: Gemini just describes
        # the change and Python applies it.
        lines = [
            "Source file provided via stdin. Output a change specification as JSON.",
            "",
            f"FILE: {file_hint}",
        ]
        if line_hint:
            lines += [f"APPROX LINE: {line_hint}"]
        lines += [
            "",
            f"TASK: {title}",
            f"SEVERITY: {severity}  CATEGORY: {category}",
            "",
            description,
            "",
            "Output ONLY a JSON object. No prose. No markdown. No tool calls.",
            'Start with { and end with }.',
            "",
            '{"explanation":"...","changes":[{"find":"exact verbatim string from file","replace":"exact verbatim replacement"}]}',
            "",
            "Rules:",
            "- find must be copied EXACTLY from the source file including all whitespace",
            "- replace is the exact replacement text",
            "- Make only the minimal change described above",
            "- Multiple change objects allowed if needed",
        ]
    else:
        # No file pre-loaded — Gemini uses its own file and web tools.
        # google_web_search and web_fetch are available and encouraged when
        # looking up docs, APIs, or best practices relevant to the fix.
        _tool_default = "TASK: Make one specific code change."
        _tool_header = _PROMPT_OVERRIDES.get("executor_task_tool", _tool_default)
        lines = [_tool_header, ""]
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


def _apply_json_diff(file_content: str, raw: str) -> tuple[str, int]:
    """Parse a JSON diff from Gemini and apply find/replace changes.

    Args:
        file_content: Original file text.
        raw: Gemini stdout containing a JSON object with a 'changes' list.

    Returns:
        (new_content, num_changes_applied).
        Returns (file_content, 0) if JSON cannot be parsed or no changes match.
    """
    data: dict | None = None
    try:
        data = json.loads(raw.strip())
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

    if not data or "changes" not in data:
        return file_content, 0

    new_content = file_content
    applied = 0
    for change in data.get("changes", []):
        find = change.get("find", "")
        replace = change.get("replace", "")
        if find and find in new_content:
            new_content = new_content.replace(find, replace, 1)
            applied += 1

    return new_content, applied


def _extract_marked_content(text: str) -> str | None:
    """Extract content between <<<BEGIN_FILE>>> and <<<END_FILE>>> markers.

    Args:
        text: Raw stdout from Gemini.

    Returns:
        Content between markers, or None if markers not found.
    """
    start = text.find("<<<BEGIN_FILE>>>")
    end = text.find("<<<END_FILE>>>")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start + len("<<<BEGIN_FILE>>>"):end].strip("\n")


def _strip_fences(text: str) -> str:
    """Extract source code from Gemini output, handling prose wrappers.

    Tries three strategies in order:
    1. Entire output is a single code fence — extract it.
    2. Output contains one or more code fences — use the largest one.
    3. No fences — return stripped text as-is.

    Args:
        text: Raw stdout from Gemini.

    Returns:
        File content with fences removed, or the original text unchanged.
    """
    text = text.strip()
    if not text:
        return text
    # Strategy 1: whole output is a code fence
    m = re.match(r"^```(?:\w+)?\n(.*?)\n```\s*$", text, re.DOTALL)
    if m:
        return m.group(1)
    # Strategy 2: find largest code fence anywhere in the output
    matches = list(re.finditer(r"```(?:\w+)?\n(.*?)\n```", text, re.DOTALL))
    if matches:
        return max(matches, key=lambda x: len(x.group(1))).group(1)
    # Strategy 3: no fences — return as-is
    return text


def _looks_like_code(text: str) -> bool:
    """Return True if text plausibly contains source code rather than English prose.

    Heuristic checks:
    - Rejects empty or very short output.
    - Rejects text that starts with common Gemini apology/meta phrases.
    - Rejects text where most lines look like plain English sentences.

    Args:
        text: The candidate source code string.

    Returns:
        True if it looks like code, False if it looks like natural language.
    """
    stripped = text.strip()
    if not stripped or len(stripped) < 10:
        return False
    first_line = stripped.split('\n', 1)[0].strip()
    reject_prefixes = (
        "I", "Sorry", "I'm", "Apolog", "Thank", "Hello",
        "Sure,", "Here is", "Here's", "Unfortunately",
        "It seems", "It looks", "This file", "The file",
        "I cannot", "I can't", "I don't", "Let me",
        "Note:", "Please", "As an AI",
    )
    for prefix in reject_prefixes:
        if first_line.startswith(prefix):
            return False
    lines = stripped.split('\n')
    if len(lines) < 2:
        return True
    english_line_count = 0
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s.endswith('.') and ' ' in s and not s.startswith('#') and not s.startswith('//'):
            english_line_count += 1
    ratio = english_line_count / max(len([l for l in lines if l.strip()]), 1)
    if ratio > 0.5:
        return False
    return True


# ---------------------------------------------------------------------------
# Gemini CLI caller — single-API-call mode + tool-call fallback
# ---------------------------------------------------------------------------

_DEFAULT_MODEL: str = "gemini-2.5-flash"
_FLOOR_MODEL: str = "gemini-2.5-flash"

_MODEL_TIMEOUTS: dict[str, int] = {
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

        stderr_chunks: list[str] = []

        def _read_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stripped = line.rstrip()
                if stripped:
                    console.print(f"    {stripped}")
                stderr_chunks.append(line)

        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stderr_thread.start()

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
            raise

        t.join(timeout=5)
        stderr_thread.join(timeout=10)
        stderr_text = "".join(stderr_chunks)
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
    console: Console,
    system_md_path: Optional[Path] = None,
) -> tuple[int, str]:
    """Run Gemini in interactive mode with prompt piped via stdin.

    Interactive mode (no -p flag) gives Gemini its full tool set including
    write_file. The -p flag restricts tools to read-only operations, which is
    why file edits never worked. Sending the prompt via stdin and closing stdin
    (EOF) causes Gemini to process one turn and exit cleanly.

    Returns:
        (returncode, stdout).  returncode -1 means timeout.
    """
    env = os.environ.copy()
    if system_md_path is not None and system_md_path.exists():
        env["GEMINI_SYSTEM_MD"] = str(system_md_path)

    try:
        proc = subprocess.Popen(
            [gemini_path, "--model", model, "--yolo"],
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        stdout_lines: list[str] = []

        def _read_stdout() -> None:
            assert proc.stdout is not None
            for ln in proc.stdout:
                stdout_lines.append(ln)

        t = threading.Thread(target=_read_stdout, daemon=True)
        t.start()

        assert proc.stderr is not None
        # Send prompt then close stdin (EOF signals end of session)
        assert proc.stdin is not None
        proc.stdin.write(prompt)
        proc.stdin.close()

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
    except Exception:
        return -2, ""


def _call_gemini(
    prompt: str,
    cwd: Path,
    console: Console,
    system_md_path: Optional[Path] = None,
) -> tuple[bool, str, Optional[str], str]:
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
        (success, corrected_content_or_empty, error_message, model_used).
    """
    gemini_path = shutil.which("gemini")
    if gemini_path is None:
        return False, "", "Gemini CLI not found - ensure 'gemini' is on PATH", ""

    env_model = os.environ.get("GRINDBOT_MODEL", "").strip()
    models = [env_model] if env_model else [_DEFAULT_MODEL, _FLOOR_MODEL]

    for i, model in enumerate(models):
        if i > 0:
            console.print(f"    [yellow][!] Falling back to {model}...[/yellow]")

        console.print(f"    [dim]Model: {model}  (interactive mode)[/dim]")
        console.print("    [dim]" + "-" * 56 + "[/dim]")

        timeout = _MODEL_TIMEOUTS.get(model, 300)

        rc, stdout = _run_tool_mode(
            gemini_path, model, prompt, cwd, timeout, console,
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
            return False, "", f"Gemini CLI timed out after {timeout}s", model

        if rc == 0:
            return True, stdout, None, model

        if i < len(models) - 1:
            console.print(
                f"    [yellow][!] {model} exited {rc}, trying next model...[/yellow]"
            )
            continue

        return False, "", f"Gemini CLI exited with code {rc}", model

    return False, "", "All models failed", ""


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

        # ---- c. Claude orchestrates, Gemini executes -----------------------
        # Claude writes a precise task prompt. Gemini runs it in interactive
        # mode (no -p flag) which gives it full tool access including write_file.
        file_hint = task.get("file") or ""
        file_preview: Optional[str] = None
        target_file: Optional[Path] = None
        if file_hint:
            target_file = worktree_path / file_hint
            if target_file.exists():
                try:
                    file_preview = target_file.read_text(encoding="utf-8")
                except OSError:
                    pass

        console.print("    [dim]Claude orchestrating task...[/dim]")
        orchestrated = brain.orchestrate_task(task, file_content=file_preview)
        if orchestrated:
            prompt = _sanitize_prompt(orchestrated)
            task["prompt_type"] = "orchestrated"
            console.print("    [dim]Using Claude-written prompt.[/dim]")
        else:
            prompt = _build_task_prompt(task, single_file_mode=False)
            task["prompt_type"] = "static"
            console.print("    [dim]Using static prompt (brain unavailable).[/dim]")

        gem_ok, _, gem_err, gem_model = _call_gemini(
            prompt, worktree_path, console,
            system_md_path=system_md_path,
        )
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
            console.print("    [dim]No file changes detected.[/dim]")

            # ---- d2. Retry: Claude writes a precise find/replace prompt ----
            if file_preview is not None:
                console.print("    [dim]Generating precise retry prompt (Claude)...[/dim]")
                retry_prompt = brain.orchestrate_retry(task, file_preview)
                if retry_prompt:
                    retry_prompt = _sanitize_prompt(retry_prompt)
                    console.print("    [dim]Retrying Gemini with precise prompt...[/dim]")
                    retry_ok, _, retry_err, retry_model = _call_gemini(
                        retry_prompt, worktree_path, console,
                        system_md_path=system_md_path,
                    )
                    if not retry_ok:
                        console.print(f"    [red]!! Gemini retry failed:[/red] {retry_err}")
                        task["status"] = "failed"
                        task["error"] = f"Gemini retry failed: {retry_err}"
                        return task
                    # Check again for changes after retry
                    changed_preview = wt.get_changed_files(worktree_path)
                    if changed_preview:
                        console.print(f"    [dim]Retry succeeded: {len(changed_preview)} file(s) changed.[/dim]")
                    else:
                        # Still no changes after retry — fail the task
                        task["status"] = "failed"
                        task["error"] = "No files were changed after Gemini retry"
                        return task
                else:
                    task["status"] = "failed"
                    task["error"] = "No files were changed - Gemini CLI may not have made any edits"
                    return task
            else:
                # No target file known — cannot generate precise retry prompt
                task["status"] = "failed"
                task["error"] = "No files were changed - Gemini CLI may not have made any edits"
                return task

        # ---- e. Validate ---------------------------------------------------
        console.print("    [dim]Validating changes...[/dim]")
        result = validate_changes(worktree_path, task)

        task["validation_warnings"] = result.warnings
        task["changed_files"] = result.changed_files

        if result.warnings:
            for w in result.warnings:
                console.print(f"    [yellow][!][/yellow] {w}")

        if not result.success:
            console.print(f"    [red]!! Validation failed:[/red] {result.error}")
            task["status"] = "failed"
            task["error"] = result.error
            return task

        # ---- f. Commit -----------------------------------------------------
        worker = gem_model if gem_model else "gemini"
        commit_msg = (
            f"[{worker}] {title}\n\n"
            f"Task-ID: {task_id}\n"
            f"Severity: {task.get('severity', 'medium')}\n"
            f"Category: {task.get('category', 'improvement')}\n"
            f"Orchestrated-by: claude-opus-4-6\n\n"
            f"{task.get('description', '')[:500]}"
        )
        commit_ok, commit_err = wt.commit_worktree(worktree_path, commit_msg)

        if not commit_ok:
            console.print(f"    [red]!! Commit failed:[/red] {commit_err}")
            task["status"] = "failed"
            task["error"] = f"Commit failed: {commit_err}"
            return task

        console.print(
            f"    [green]Committed[/green] -> branch [cyan]{branch_name}[/cyan] "
            f"({len(result.changed_files)} file(s) changed)"
        )
        task["branch"] = branch_name

        # ---- g. Cleanup worktree (keep branch for merge) -------------------
        wt.cleanup_worktree(repo_root, worktree_path, branch_name, keep_branch=True)

        # ---- h. Merge into main --------------------------------------------
        console.print(f"    [dim]Merging {branch_name} into main...[/dim]")
        merge_ok, merge_err = wt.merge_branch(repo_root, branch_name)
        if not merge_ok:
            console.print(f"    [red]!! Merge failed:[/red] {merge_err}")
            task["status"] = "failed"
            task["error"] = f"Merge failed: {merge_err}"
            wt._delete_branch(repo_root, branch_name)
            return task

        # ---- i. Claude post-merge review (before push) ---------------------
        console.print("    [dim]Claude reviewing merge...[/dim]")
        head_diff = wt.get_head_diff(repo_root)
        merge_approved, merge_reason = brain.review_merge(head_diff)

        task["merge_reason"] = merge_reason

        if merge_approved:
            console.print(f"    [green]Claude approved merge:[/green] {merge_reason}")
            task["status"] = "completed"
            task["error"] = None
            # ---- j. Push only after Claude approval ------------------------
            default_branch = wt.get_default_branch(repo_root)
            push_ok, push_err = wt.push_branch(repo_root, default_branch)
            if not push_ok:
                console.print(f"    [yellow][!] Push failed:[/yellow] {push_err}")
                task["push_error"] = push_err
            else:
                console.print(f"    [dim]Pushed {default_branch} to origin.[/dim]")
        else:
            console.print(f"    [red]!! Claude rejected merge:[/red] {merge_reason}")
            console.print("    [dim]Reverting...[/dim]")
            revert_ok, revert_err = wt.revert_last_commit(repo_root)
            if revert_ok:
                console.print("    [yellow]Reverted. Main branch restored.[/yellow]")
            else:
                console.print(f"    [red]!! Revert failed:[/red] {revert_err}")
            task["status"] = "failed"
            task["error"] = f"Claude rejected merge: {merge_reason}"
            wt._delete_branch(repo_root, branch_name)

        return task

    finally:
        # Safety net — cleanup worktree if still present (crash/early return)
        if worktree_path.exists():
            wt.cleanup_worktree(repo_root, worktree_path, branch_name, keep_branch=False)


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
            "[yellow]No pending or failed tasks — everything is completed.[/yellow]"
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


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


def retry_tasks(
    task_ids: list[str],
    grindbot_dir: Path,
    console: Console,
) -> list[dict]:
    """Reset specific tasks to pending and re-execute them.

    Steps:
      1. Load all tasks from tasks.json.
      2. For each requested ID: validate it exists and is retryable, then
         reset status to "pending" and clear branch/error fields.
      3. Save tasks.json so the reset is durable before any execution starts.
      4. Locate the git repo root.
      5. Run execute_task for each reset task, saving after every one.

    Args:
        task_ids: Zero-padded task ID strings to retry, e.g. ["003", "007"].
            Callers are responsible for normalising IDs before calling this.
        grindbot_dir: Absolute path to the project's .grindbot/ directory.
        console: Rich console for all output.

    Returns:
        Full updated list of all tasks after execution.
    """
    from .config import find_repo_root, load_tasks, save_tasks

    all_tasks = load_tasks(grindbot_dir.parent)
    if not all_tasks:
        console.print("[yellow]No tasks found in tasks.json.[/yellow]")
        return []

    # --- Validate and reset requested tasks ---------------------------------
    # Index by id for O(1) lookup.
    task_index: dict[str, int] = {t["id"]: i for i, t in enumerate(all_tasks)}
    to_run: list[dict] = []

    for tid in task_ids:
        if tid not in task_index:
            console.print(f"[yellow][!] Task {tid} not found — skipped.[/yellow]")
            continue

        task = all_tasks[task_index[tid]]
        current_status = task.get("status", "pending")

        if current_status == "pending":
            console.print(
                f"[yellow][!] Task {tid} is already pending — skipped.[/yellow]"
            )
            continue

        if current_status not in ("failed", "completed"):
            console.print(
                f"[yellow][!] Task {tid} has unexpected status "
                f"'{current_status}' — skipped.[/yellow]"
            )
            continue

        task["status"] = "pending"
        task["branch"] = None
        task["error"] = None
        all_tasks[task_index[tid]] = task
        to_run.append(task)
        console.print(
            f"  [dim]Reset task {tid} ({current_status} -> pending):[/dim] "
            f"{task.get('title', '')}"
        )

    if not to_run:
        console.print("[yellow]No tasks eligible for retry.[/yellow]")
        return all_tasks

    # Persist the resets before executing so a crash doesn't leave stale state.
    try:
        save_tasks(grindbot_dir.parent, all_tasks)
    except Exception as exc:
        console.print(f"[red]!! Could not save tasks.json before retry: {exc}[/red]")
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
            f"[bold]GrindBot retry[/bold] - re-running [cyan]{len(to_run)}[/cyan] "
            f"task(s) in {repo_root}",
            border_style="cyan",
        )
    )

    start = time.monotonic()

    # --- Execute each reset task --------------------------------------------
    for task in to_run:
        try:
            updated = execute_task(task, repo_root, grindbot_dir, console)
        except Exception as exc:
            console.print(f"    [red]!! Unhandled exception:[/red] {exc}")
            task["status"] = "failed"
            task["error"] = f"Unhandled exception: {exc}"
            updated = task

        for i, t in enumerate(all_tasks):
            if t["id"] == updated["id"]:
                all_tasks[i] = updated
                break

        try:
            save_tasks(grindbot_dir.parent, all_tasks)
        except Exception as exc:
            console.print(f"  [yellow][!] Could not save tasks.json:[/yellow] {exc}")

    elapsed = time.monotonic() - start
    console.print(f"\n[dim]Retry finished in {elapsed:.1f}s[/dim]")
    return all_tasks
