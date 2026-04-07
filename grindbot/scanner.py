"""Scanner: calls Gemini CLI to find issues in a codebase and parses the output.

Windows-specific constraints handled here:
- gemini is a .CMD file, so subprocess goes through cmd.exe.
- cmd.exe interprets | as a pipe even inside double-quoted arguments.
- Windows CreateProcess has an 8 191-char command line limit.

Strategy: GrindBot reads the source files itself and pipes their content to
Gemini via stdin.  The -p flag carries only a short instruction string (well
under 8 191 chars and free of shell-special characters).  This eliminates all
file-tool round-trips, so the entire scan costs exactly ONE API request and
never exhausts per-request rate limits.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from rich.console import Console

console = Console()

# Override with env var GRINDBOT_MODEL when the default has no capacity.
_DEFAULT_MODEL = "gemini-2.5-flash"
_FLOOR_MODEL = "gemini-2.5-flash"

_MODEL_TIMEOUTS: dict[str, int] = {
    "gemini-2.5-flash": 180,
}

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

# Prompt sent via -p.  Must be short (well under 8 191 chars) and free of
# the Windows CMD special chars  |  "  <  >  &  ^  {  }.
# File content is supplied via stdin, so the combined payload has no size limit.
#
# IMPORTANT: "Do not read any files or use any tools." is required.
# Without it, Gemini will try to read each source file again via its file tools,
# hitting the per-request rate limit on every tool call and causing timeouts.
_SCAN_PROMPT = (
    "The complete source code is provided above via stdin. "
    "Do not read any files or use any tools. "
    "Analyze only the code provided. "
    "The codebase may contain multiple programming languages — analyze all of them. "
    "Find real, specific, actionable code issues. "
    "Output a raw JSON array only. "
    "Your entire response must start with [ and end with ]. "
    "No markdown fences. No prose. No explanation. Raw JSON only. "
    "Each object must have: "
    "category as bug, security, performance, or style; "
    "severity as high, medium, or low; "
    "file as the relative file path; "
    "line as an integer or null; "
    "title as a short summary under 80 characters; "
    "description as a string explaining what is wrong and how to fix it. "
    "Find between 3 and 15 real issues. Do not fabricate issues. "
    "Begin your response with the opening bracket of the JSON array."
)

# Hard cap on piped content to avoid exceeding model context limits.
_MAX_STDIN_BYTES = 200_000

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


def _get_scan_prompt() -> str:
    """Return evolved scan prompt if available, else the hardcoded default."""
    return _PROMPT_OVERRIDES.get("scanner_scan", _SCAN_PROMPT)


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
    total_bytes = 0
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

        if total_bytes + len(chunk) > _MAX_STDIN_BYTES:
            console.print(
                f"  [yellow][!] {rel.as_posix()} skipped - content limit "
                f"({_MAX_STDIN_BYTES // 1000} KB) reached[/yellow]"
            )
            skipped += 1
            break

        # Show each file as it's collected: [idx/total]  path  (N lines)
        line_count = content.count("\n")
        console.print(
            f"  [dim][{idx}/{total_files}][/dim]  [cyan]{rel.as_posix()}[/cyan]"
            f"  [dim]{line_count} lines[/dim]"
        )
        parts.append(chunk)
        total_bytes += len(chunk)

    included = len(parts)
    console.print(
        f"  [dim]Collected {included} file(s)"
        + (f", skipped {skipped}" if skipped else "")
        + f"  ({total_bytes // 1024} KB total)[/dim]"
    )
    return "\n".join(parts)


def _extract_json_array(raw: str) -> list[Any]:
    """Extract and parse a JSON array from Gemini's raw text output.

    Tries three strategies in order:
    1. Parse the entire output as JSON directly.
    2. Extract JSON from a markdown ```json ... ``` code block.
    3. Find the first [...] block anywhere in the text.

    Returns an empty list if all strategies fail.
    """
    text = raw.strip()

    # Strategy 1: whole output is raw JSON
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # Strategy 2: JSON inside a markdown code block
    block_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if block_match:
        try:
            result = json.loads(block_match.group(1))
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    # Strategy 3: first [...] span in the text
    span_match = re.search(r"(\[.*\])", text, re.DOTALL)
    if span_match:
        try:
            result = json.loads(span_match.group(1))
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    return []


def _validate_task(raw: Any) -> dict[str, Any] | None:
    """Validate and normalise one raw task dict from Gemini output.

    Returns a clean task dict, or None if the item is too malformed to use.
    """
    if not isinstance(raw, dict):
        return None

    title = str(raw.get("title", "")).strip()
    description = str(raw.get("description", "")).strip()
    if not title or not description:
        return None

    category = str(raw.get("category", "style")).lower()
    if category not in {"bug", "security", "performance", "style"}:
        category = "style"

    severity = str(raw.get("severity", "medium")).lower()
    if severity not in {"high", "medium", "low"}:
        severity = "medium"

    file_val = str(raw.get("file", "")).strip() or None

    line = raw.get("line")
    try:
        line = int(line) if line is not None else None
    except (TypeError, ValueError):
        line = None

    return {
        "category": category,
        "severity": severity,
        "file": file_val,
        "line": line,
        "title": title[:120],
        "description": description,
    }


def scan_project(project_path: str | Path) -> list[dict[str, Any]]:
    """Call Gemini CLI to scan a codebase and return a list of validated issue dicts.

    Source files are read by GrindBot and piped to Gemini via stdin so that
    the scan costs exactly one API request (no per-file tool-call round-trips).
    This avoids the rate-limit retries that occur when Gemini reads each file
    individually via its file tools.

    Each returned dict has keys: category, severity, file, line, title, description.
    IDs and ordering are assigned later by planner.plan().

    Raises RuntimeError on subprocess failure or unparseable output.
    Raises FileNotFoundError if the 'gemini' binary is not on PATH.
    """
    project_path = Path(project_path).resolve()

    gemini_bin = shutil.which("gemini")
    if gemini_bin is None:
        raise FileNotFoundError(
            "'gemini' CLI not found on PATH. "
            "Install it from https://github.com/google-gemini/gemini-cli"
        )

    env_model = os.environ.get("GRINDBOT_MODEL", "").strip()
    models = [env_model] if env_model else [_DEFAULT_MODEL, _FLOOR_MODEL]

    # Detect languages and collect source files on the Python side — no file-tool calls needed.
    langs, lang_file_count = _detect_languages(project_path)
    if langs:
        console.print(
            f"[dim]Detected languages: {', '.join(langs)} ({lang_file_count} file(s))[/dim]"
        )
    console.print("[dim]Collecting source files...[/dim]")
    source_context = _collect_source_files(project_path)
    if not source_context.strip():
        raise RuntimeError(f"No source files found in {project_path}")

    file_count = source_context.count("\n=== ")

    # Prepend GEMINI.md scan intelligence so Gemini reads the product context
    # and task-quality guide before seeing any source files.
    gemini_md = Path(__file__).parent.parent / "GEMINI.md"
    if gemini_md.exists():
        scan_preamble = gemini_md.read_text(encoding="utf-8", errors="replace")
        stdin_payload = scan_preamble + "\n\n" + source_context
        console.print(f"[dim]Sending GEMINI.md + {file_count} file(s) to Gemini...[/dim]")
    else:
        stdin_payload = source_context
        console.print(f"[dim]Sending {file_count} file(s) to Gemini...[/dim]")

    # -p carries the short instruction; file content arrives via stdin.
    # Windows note: subprocess input= (pipe) does not work with .CMD wrappers;
    # NamedTemporaryFile + open file handle bypasses this reliably.
    tmp_path: str | None = None
    result = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(stdin_payload)
            tmp_path = tmp.name

        for i, model in enumerate(models):
            if i > 0:
                console.print(f"[yellow][!] Falling back to {model}...[/yellow]")

            cmd = [gemini_bin, "--model", model, "-p", _get_scan_prompt(), "--yolo"]
            console.print(
                f"\n[bold cyan]Calling Gemini CLI ({model})...[/bold cyan]"
                f"  [dim]streaming output below[/dim]"
            )
            console.print("[dim]" + "-" * 60 + "[/dim]")

            # Use Popen so we can stream stderr live while capturing stdout
            # for JSON parsing.  subprocess.run cannot do both simultaneously.
            import subprocess as _sp  # local alias for clarity

            try:
                with open(tmp_path, encoding="utf-8") as stdin_fh:
                    proc = _sp.Popen(
                        cmd,
                        cwd=str(Path.home()),
                        stdin=stdin_fh,
                        stdout=_sp.PIPE,   # capture for JSON parsing
                        stderr=_sp.PIPE,   # stream live below
                        text=True,
                    )

                # Read stderr line by line so it appears as Gemini produces it.
                # stdout is read after the process exits (it holds the JSON).
                import threading

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
                        console.print(f"  {stripped}")

                scan_timeout = _MODEL_TIMEOUTS.get(model, 180)
                proc.wait(timeout=scan_timeout)
                t.join(timeout=5)
                raw_stdout = "".join(stdout_lines)

            except _sp.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                if i < len(models) - 1:
                    console.print(f"[yellow][!] {model} timed out after {_MODEL_TIMEOUTS.get(model, 180)}s, trying next model...[/yellow]")
                    console.print("[dim]" + "-" * 60 + "[/dim]")
                    continue
                raise RuntimeError(f"Gemini CLI timed out after {_MODEL_TIMEOUTS.get(model, 180)} seconds.")

            console.print("[dim]" + "-" * 60 + "[/dim]")

            # Wrap in a fake CompletedProcess so the rest of the function
            # can use `result.returncode` and `result.stdout` unchanged.
            result = _sp.CompletedProcess(
                args=cmd,
                returncode=proc.returncode,
                stdout=raw_stdout,
                stderr="",
            )

            if result.returncode == 0:
                break
            if i < len(models) - 1:
                console.print(f"[yellow][!] {model} returned code {proc.returncode}[/yellow]")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    if result is None or result.returncode != 0:
        stderr = (result.stderr.strip() if result else "")
        raise RuntimeError(
            f"Gemini CLI exited with code {result.returncode if result else '?'}.\n"
            f"{stderr or '(no stderr)'}"
        )

    raw_output = result.stdout.strip()
    if not raw_output:
        raise RuntimeError("Gemini CLI returned no output.")

    raw_tasks = _extract_json_array(raw_output)
    if not raw_tasks:
        raise RuntimeError(
            "Could not parse any JSON tasks from Gemini output.\n"
            f"First 500 chars:\n{raw_output[:500]}"
        )

    validated: list[dict[str, Any]] = []
    for item in raw_tasks:
        task = _validate_task(item)
        if task is not None:
            validated.append(task)

    return validated
