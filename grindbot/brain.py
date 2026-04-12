"""Brain: Claude Opus 4.6 via KieAI — orchestrates task planning and code review.

Claude is the orchestrator. It never touches files directly.

  plan_tasks(source_context)  -> list[task_dicts]
      One call. Claude reads the full codebase, returns a prioritised task list.

  review_diff(task, diff)     -> (approved: bool, reason: str)
      One call per task. Claude reads what Gemini did and approves or rejects.
"""
import json
import os
import re
import stat
import threading
from typing import Any

import httpx
from rich.console import Console

console = Console()

_KIE_URL = "https://api.kie.ai/claude/v1/messages"
_MODEL = "claude-opus-4-6"
_THINKING = True  # always on for all Claude calls
_PLAN_TIMEOUT = 120        # Claude reads the full codebase
_ORCHESTRATE_TIMEOUT = 45  # Claude writes a Gemini prompt
_REVIEW_TIMEOUT = 60       # Claude reads a diff
_MAX_DIFF_BYTES = 8_000    # cap diff sent to reviewer
_MAX_FILE_PREVIEW = 3_000  # chars of file content sent to orchestrator

# Mask for overly permissive group/other permissions on .env files
_OVERLY_PERMISSIVE = stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP | \
                       stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH
_task_credits_local = threading.local()

_PLAN_SYSTEM = """\
You are the brain of GrindBot, an autonomous code improvement engine.
Read the full codebase provided and return a prioritized task list.

GOOD tasks (prioritize these):
- Real bugs that cause crashes, data loss, or silent wrong behavior
- Missing error handling that would break an unattended overnight run
- Security issues: hardcoded secrets, injection risks, unsafe subprocess use
- Reliability gaps in the grind loop: retry logic, timeout handling,
  worktree cleanup on every exit path, graceful degradation on API failures
- Features that expand autonomy: smarter validation, better task chaining,
  self-healing on failure

BAD tasks (skip entirely):
- Pure style: variable rename, import reorder, adding type hints to working code
- Cosmetic refactors with no behavioral impact
- Speculative performance with no measured bottleneck
- Docstrings on obvious one-liner functions

Severity: critical=GrindBot stops working or corrupts git state,
high=silently wrong behavior, medium=fragile on edge cases,
low=nice to have but nothing breaks without it.

Filter: would this task make the morning grind report more valuable
or just more verbose? If just verbose, skip it.

Output a raw JSON array only. No markdown fences. No prose. Start with [.
Each object must have: category (bug|security|performance|style),
severity (critical|high|medium|low), file (relative path or null),
line (integer or null), title (under 80 chars), description (what is wrong
and exactly how to fix it).
Return 3-15 real issues.\
"""

_ORCHESTRATE_SYSTEM = """\
You are a senior software engineer writing precise instructions for a Gemini code editing agent.
The agent will read the instructions and directly edit the target file.

Your output must be plain text only. No markdown. No code fences. No backticks.
Do not use: pipe characters, double quotes, angle brackets, ampersands, or carets.
Those characters break Windows CMD and will corrupt the agent call.

Write clear, specific instructions describing exactly what change to make and why.
Include the target file name, the approximate line or function involved, and the exact
behavioral change required. Be concrete. The agent cannot ask follow-up questions.\
"""

_ORCHESTRATE_RETRY_TIMEOUT = 60

_ORCHESTRATE_RETRY_SYSTEM = """\
You are writing a precise retry prompt for a Gemini code agent that ran once but made no changes.
The agent needs exact, unambiguous instructions with verbatim code to locate and replace.

Your output must be plain text only. No markdown. No code fences. No backticks.
Do not use: pipe characters, double quotes, angle brackets, ampersands, or carets.

Structure your output as:
1. FILE: the target file path
2. FIND: copy the exact verbatim lines from the file that need to change (3-10 lines of context)
3. REPLACE WITH: the exact replacement lines
4. WHY: one sentence explaining the behavioral change

Be surgical. Quote exact code. The agent will use your FIND block to locate the change site.\
"""

_REVIEW_SYSTEM = """\
You are a strict code reviewer for GrindBot.
A Gemini agent made a code change to address a specific task. Review the diff.

Approve if:
- The change correctly and completely addresses the described task
- No regressions, no unrelated edits, no broken logic
- Code is clean and production-ready

Reject if:
- Change is incomplete, incorrect, or introduces new bugs
- Diff includes unrelated changes beyond the task scope
- Change could break something or leaves the code in a worse state

Respond with ONLY a single line of valid JSON — no explanation before or after:
{"approved": true, "reason": "brief explanation"}\
"""

_MERGE_REVIEW_SYSTEM = """\
You are the final gatekeeper for GrindBot. A task branch was just merged into
the main branch. Review the commit that landed and confirm it is safe to keep.

Approve if:
- The change is coherent, targeted, and does not break the codebase
- No secrets, credentials, or debug code was introduced
- Nothing looks unintentionally destructive or out of scope

Revert if:
- The change is clearly wrong, corrupted, or dangerous
- It introduces obvious regressions or security issues
- The diff looks like it touched things it should not have

Respond with ONLY a single line of valid JSON — no explanation before or after:
{"approved": true, "reason": "brief explanation"}\
"""

_APPLY_TIMEOUT = 90
_APPLY_SYSTEM = """\
You are a code editor. You will be given a source file and a specific change to make.
Return the complete corrected file — every line, nothing omitted.
Start your response with the exact string <<<BEGIN>>> on its own line.
End your response with the exact string <<<END>>> on its own line.
No explanation. No markdown. No fences. Just the markers and the file.\
"""

_MERGE_REVIEW_TIMEOUT = 60
_MAX_HEAD_DIFF_BYTES = 10_000
_REFLECT_TIMEOUT = 120

_REFLECT_SYSTEM = """\
You are GrindBot's self-improvement engine. After each grind session you review
all task outcomes and improve the prompt templates used by each agent in the pipeline.

Agents and their prompt keys:
- brain_plan: Claude scans codebase and creates task list
- brain_orchestrate: Claude writes Gemini task prompts
- brain_review_diff: Claude approves/rejects per-task diffs
- brain_review_merge: Claude approves/rejects merged commits
- scanner_scan: Gemini scans codebase for issues
- executor_task_tool: Fallback Gemini task prompt when Claude is unavailable

Rules:
- Only modify prompts that clearly contributed to failures. Do not change working prompts.
- Make surgical improvements - not rewrites. Preserve all safety constraints.
- If a session had 100% success rate, return an empty changes list.
- Output ONLY valid JSON. No prose before or after.

Output format:
{
  "reasoning": "One-paragraph summary of what went wrong and the root cause",
  "changes": [
    {
      "agent": "brain_orchestrate",
      "reason": "Short explanation of why this prompt contributed to failures",
      "new_prompt": "Complete updated prompt text"
    }
  ]
}\
"""

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

_cached_api_key: str | None = None # Added for in-memory caching

def _get_prompt(key: str, default: str) -> str:
    """Return evolved prompt override if available, else the hardcoded default.

    Args:
        key: Prompt key, e.g. 'brain_plan' or 'brain_orchestrate'.
        default: Hardcoded default prompt string to fall back to.

    Returns:
        Evolved prompt from _PROMPT_OVERRIDES if present, else default.
    """
    return _PROMPT_OVERRIDES.get(key, default)


def _get_api_key() -> str | None:
    """Return KIE_API_KEY from environment or ~/.env, or None if not found.

    Python does not auto-load ~/.env the way Gemini CLI does, so we
    check the file directly and cache the result into os.environ.
    """
    global _cached_api_key
    if _cached_api_key is not None:
        return _cached_api_key

    key = os.environ.get("KIE_API_KEY", "").strip()
    if key:
        _cached_api_key = key
        return key

    from pathlib import Path
    env_file = Path.home() / ".env"
    if env_file.exists():
        # Check permissions on Unix-like systems
        if os.name != 'nt':
            try:
                mode = os.stat(str(env_file)).st_mode
                if mode & _OVERLY_PERMISSIVE:
                    console.print(
                        f"[bold yellow]WARNING:[/bold yellow] {env_file} has overly permissive permissions "
                        f"(mode {oct(mode)}). Run: chmod 600 {env_file}",
                        style="yellow",
                    )
            except OSError:
                pass
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("KIE_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if key:
                        _cached_api_key = key # Cache in module variable, not os.environ
                        return key
        except OSError:
            pass

    return None


def _call_claude(system: str, user_content: str, timeout: int) -> str:
    """POST to KieAI Claude API and return the text content of the response.

    Args:
        system: System prompt string (sent as top-level 'system' field).
        user_content: User message content.
        timeout: Request timeout in seconds.

    Returns:
        Text content of Claude's response (stripped).

    Raises:
        RuntimeError on API failure, missing key, or empty response.
    """
    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError(
            "KIE_API_KEY not set. Add KIE_API_KEY=<key> to ~/.env"
        )

    payload: dict[str, Any] = {
        "model": _MODEL,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
        "thinkingFlag": _THINKING,
        "stream": False,
    }

    try:
        resp = httpx.post(
            _KIE_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
    except httpx.TimeoutException:
        raise RuntimeError(f"Claude API timed out after {timeout}s")
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"Claude API error {exc.response.status_code}: "
            f"{exc.response.text[:400]}"
        )
    except httpx.RequestError as exc:
        raise RuntimeError(f"Claude API connection error: {exc}")

    data = resp.json()
    credits = data.get("credits_consumed")
    if credits is not None:
        _task_credits_local.value = getattr(_task_credits_local, 'value', 0.0) + credits # Accumulate credits per thread
        console.print(f"    [dim]Claude credits used: {credits}[/dim]")
    content = data.get("content", [])

    # Standard Anthropic format: array of typed blocks
    if isinstance(content, list):
        text = "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
    else:
        text = str(content).strip()

    if not text:
        raise RuntimeError(
            f"Claude returned an empty response. "
            f"Raw data keys: {list(data.keys())}"
        )
    return text


def plan_tasks(
    source_context: str,
    goal: str | None = None,
) -> list[dict[str, Any]]:
    """Ask Claude to analyze a codebase and return a prioritised task list.

    This replaces grindbot scan. One API call. Claude reads everything
    and returns tasks focused on real value, not style noise.

    Args:
        source_context: Full codebase as a concatenated labelled string
            (same format produced by scanner._collect_source_files).
        goal: Optional user-provided direction appended to the message.

    Returns:
        List of raw task dicts ready for planner.plan().
        Returns empty list if JSON cannot be parsed (caller should warn).

    Raises:
        RuntimeError if KIE_API_KEY is not set or the API call fails hard.
    """
    user_msg = source_context
    if goal:
        user_msg += f"\n\n--- USER GOAL ---\n{goal}"
    user_msg += (
        "\n\n--- INSTRUCTIONS ---\n"
        "Analyze the code above. Return a JSON array of tasks. "
        "Begin your response with [ and end with ]."
    )

    console.print(
        f"\n[bold cyan]Calling Claude Opus 4.6 (planning)...[/bold cyan]"
        f"  [dim]this may take up to {_PLAN_TIMEOUT}s[/dim]"
    )
    raw = _call_claude(_get_prompt("brain_plan", _PLAN_SYSTEM), user_msg, _PLAN_TIMEOUT)

    # Strategy 1: whole response is valid JSON array
    try:
        tasks = json.loads(raw)
        if isinstance(tasks, list):
            return tasks
    except json.JSONDecodeError:
        pass

    # Strategy 2: find [...] block anywhere in the response
    m = re.search(r"(\[.*\])", raw, re.DOTALL)
    if m:
        try:
            tasks = json.loads(m.group(1))
            if isinstance(tasks, list):
                return tasks
        except json.JSONDecodeError:
            pass

    console.print(
        f"[yellow][!] Could not parse JSON from Claude response.\n"
        f"First 400 chars:\n{raw[:400]}[/yellow]"
    )
    return []


def orchestrate_task(
    task: dict[str, Any],
    file_content: str | None = None,
) -> str | None:
    """Write a precise Gemini prompt for a single task.

    Claude reads the task metadata and optionally the first 3000 chars of the
    target file, then returns a plain-text prompt that Gemini will execute.

    Degrades gracefully: returns None if KIE_API_KEY is not set (no warning
    spam) or if the API call fails, so the caller falls back to the static
    template without interrupting the grind loop.

    Args:
        task: Task dict with title, description, severity, category, file.
        file_content: Current content of the target file, or None if unknown.

    Returns:
        Plain-text prompt string for Gemini, or None on failure/unavailability.
    """
    if not _get_api_key():
        return None

    file_hint = task.get("file") or "not specified"
    preview = ""
    if file_content:
        preview = (
            f"\n\nCURRENT FILE CONTENT (first {_MAX_FILE_PREVIEW} chars):\n"
            + file_content[:_MAX_FILE_PREVIEW]
        )
        if len(file_content) > _MAX_FILE_PREVIEW:
            preview += f"\n... (truncated at {_MAX_FILE_PREVIEW} chars)"

    user_msg = (
        f"TASK TITLE: {task.get('title', '')}\n"
        f"FILE: {file_hint}\n"
        f"SEVERITY: {task.get('severity', 'medium')}\n"
        f"CATEGORY: {task.get('category', 'improvement')}\n"
        f"DESCRIPTION: {task.get('description', '')}"
        f"{preview}\n\n"
        "Write the Gemini agent instructions now."
    )

    try:
        raw = _call_claude(_get_prompt("brain_orchestrate", _ORCHESTRATE_SYSTEM), user_msg, _ORCHESTRATE_TIMEOUT)
        return raw if raw else None
    except RuntimeError:
        return None


def orchestrate_retry(
    task: dict[str, Any],
    file_content: str | None = None,
) -> str | None:
    """Write a more precise Gemini retry prompt when the first attempt made no changes.

    Uses a different system prompt that requires Claude to include exact verbatim
    code snippets (FIND/REPLACE blocks) so Gemini can locate the change site
    without ambiguity. Returns None if KIE_API_KEY not set or API fails.

    Args:
        task: Task dict with title, description, severity, category, file.
        file_content: Current content of the target file, or None if unavailable.

    Returns:
        Plain-text retry prompt for Gemini, or None on failure.
    """
    if not _get_api_key():
        return None

    file_hint = task.get("file") or "not specified"
    preview = ""
    if file_content:
        preview = (
            f"\n\nFULL FILE CONTENT (use this to copy exact verbatim lines):\n"
            + file_content[:_MAX_FILE_PREVIEW]
        )
        if len(file_content) > _MAX_FILE_PREVIEW:
            preview += f"\n... (truncated at {_MAX_FILE_PREVIEW} chars)"

    user_msg = (
        f"TASK TITLE: {task.get('title', '')}\n"
        f"FILE: {file_hint}\n"
        f"SEVERITY: {task.get('severity', 'medium')}\n"
        f"DESCRIPTION: {task.get('description', '')}"
        f"{preview}\n\n"
        "The agent ran once but made no changes. "
        "Write a retry prompt with exact FIND/REPLACE blocks so the agent can locate the change."
    )

    try:
        raw = _call_claude(_ORCHESTRATE_RETRY_SYSTEM, user_msg, _ORCHESTRATE_RETRY_TIMEOUT)
        return raw if raw else None
    except RuntimeError:
        return None


def review_diff(
    task: dict[str, Any],
    diff: str,
) -> tuple[bool, str]:
    """Ask Claude to review a git diff and approve or reject the change.

    Degrades gracefully — returns (True, "skipped") if KIE_API_KEY is not
    set or the API call fails, so the grind loop is never blocked by the
    reviewer being unavailable.

    Args:
        task: Task dict with title, description, file, severity, category.
        diff: Git diff string captured from the worktree after Gemini ran.

    Returns:
        (approved, reason).
        approved=True  → commit the change.
        approved=False → mark task failed with reason.
    """
    if not _get_api_key():
        return True, "review skipped (KIE_API_KEY not set)"

    capped_diff = diff[:_MAX_DIFF_BYTES]
    if len(diff) > _MAX_DIFF_BYTES:
        capped_diff += f"\n... (diff truncated at {_MAX_DIFF_BYTES} bytes)"

    if not capped_diff.strip():
        return False, "diff is empty — no changes detected to review"

    user_msg = (
        f"TASK: {task.get('title', '')}\n"
        f"FILE: {task.get('file') or 'not specified'}\n"
        f"SEVERITY: {task.get('severity', 'medium')}\n"
        f"DESCRIPTION: {task.get('description', '')}\n\n"
        f"DIFF:\n{capped_diff}\n\n"
        "Approve or reject this change?"
    )

    try:
        raw = _call_claude(_get_prompt("brain_review_diff", _REVIEW_SYSTEM), user_msg, _REVIEW_TIMEOUT)
    except RuntimeError as exc:
        console.print(f"    [yellow][!] Claude review unavailable: {exc}[/yellow]")
        return True, f"review skipped: {exc}"

    # Parse the JSON response
    try:
        data = json.loads(raw)
        approved = bool(data.get("approved", True))
        reason = str(data.get("reason", "")).strip()
        return approved, reason or ("approved" if approved else "rejected")
    except json.JSONDecodeError:
        pass

    # Fallback: scan for explicit false in the raw text
    low = raw.lower()
    if '"approved": false' in low or '"approved":false' in low:
        m = re.search(r'"reason"\s*:\s*"([^"]+)"', raw)
        reason = m.group(1) if m else raw[:200]
        return False, reason

    # Default to approved if we genuinely can't parse the response
    console.print(
        f"    [yellow][!] Claude review response unparseable — auto-approved[/yellow]"
    )
    return True, f"auto-approved (parse failed): {raw[:100]}"


def review_merge(
    head_diff: str,
) -> tuple[bool, str]:
    """Ask Claude to review what just landed on the main branch.

    This is the final gatekeeper call — runs after the task branch has been
    merged into main. If Claude rejects, the caller should revert HEAD.

    Degrades gracefully — returns (True, "skipped") if KIE_API_KEY is not
    set or the API call fails, so the grind loop is never blocked.

    Args:
        head_diff: Output of git show HEAD on the main branch.

    Returns:
        (approved, reason). approved=False triggers a revert.
    """
    if not _get_api_key():
        return True, "merge review skipped (KIE_API_KEY not set)"

    capped = head_diff[:_MAX_HEAD_DIFF_BYTES]
    if len(head_diff) > _MAX_HEAD_DIFF_BYTES:
        capped += f"\n... (truncated at {_MAX_HEAD_DIFF_BYTES} bytes)"

    if not capped.strip():
        return True, "merge review skipped (empty diff)"

    user_msg = (
        f"This commit just landed on the main branch:\n\n{capped}\n\n"
        "Approve to keep it. Revert if it looks wrong or dangerous."
    )

    try:
        raw = _call_claude(_get_prompt("brain_review_merge", _MERGE_REVIEW_SYSTEM), user_msg, _MERGE_REVIEW_TIMEOUT)
    except RuntimeError as exc:
        console.print(f"    [yellow][!] Claude merge review unavailable: {exc}[/yellow]")
        return True, f"merge review skipped: {exc}"

    try:
        data = json.loads(raw)
        approved = bool(data.get("approved", True))
        reason = str(data.get("reason", "")).strip()
        return approved, reason or ("approved" if approved else "reverted")
    except json.JSONDecodeError:
        pass

    low = raw.lower()
    if '"approved": false' in low or '"approved":false' in low:
        m = re.search(r'"reason"\s*:\s*"([^"]+)"', raw)
        reason = m.group(1) if m else raw[:200]
        return False, reason

    console.print(
        "    [yellow][!] Claude merge review unparseable — auto-approved[/yellow]"
    )
    return True, f"auto-approved (parse failed): {raw[:100]}"


def apply_task(task: dict[str, Any], file_content: str) -> str | None:
    """Ask Claude to apply a task to a file and return the corrected content.

    Args:
        task: Task dict with title, description, severity, category, file.
        file_content: Current content of the target file.

    Returns:
        Complete corrected file as a string, or None on failure.
    """
    if not _get_api_key():
        return None

    user_msg = (
        f"FILE: {task.get('file', 'unknown')}\n"
        f"TASK: {task.get('title', '')}\n"
        f"SEVERITY: {task.get('severity', 'medium')}\n"
        f"DESCRIPTION: {task.get('description', '')}\n\n"
        f"CURRENT FILE CONTENT:\n{file_content}"
    )

    try:
        raw = _call_claude(_APPLY_SYSTEM, user_msg, _APPLY_TIMEOUT)
    except RuntimeError as exc:
        console.print(f"    [yellow][!] Claude apply unavailable: {exc}[/yellow]")
        return None

    start = raw.find("<<<BEGIN>>>")
    end = raw.find("<<<END>>>")
    if start != -1 and end != -1 and end > start:
        return raw[start + len("<<<BEGIN>>>"):end].strip("\n")

    console.print(f"    [yellow][!] Claude apply: markers not found in response.[/yellow]")
    return None


def reflect_session(session_data: dict, current_prompts: dict) -> dict | None:
    """Meta-evaluate a grind session and return updated prompt suggestions.

    Reviews all task outcomes and identifies which agent prompts contributed
    to failures. Returns surgical improvements as a structured dict, or None
    on API failure.

    Args:
        session_data: Structured session outcome dict (from reflector.py).
        current_prompts: Dict of current prompt templates keyed by agent name.

    Returns:
        Dict with 'reasoning' (str) and 'changes' (list of dicts with 'agent',
        'reason', 'new_prompt'), or None if the API call fails.
    """
    if not _get_api_key():
        return None

    user_msg = (
        "SESSION OUTCOMES:\n"
        + json.dumps(session_data, indent=2)
        + "\n\nCURRENT PROMPT TEMPLATES:\n"
        + json.dumps(current_prompts, indent=2)
    )

    try:
        raw = _call_claude(_REFLECT_SYSTEM, user_msg, _REFLECT_TIMEOUT)
    except RuntimeError as exc:
        console.print(f"    [yellow][!] Claude reflection unavailable: {exc}[/yellow]")
        return None

    # Strategy 1: whole response is valid JSON object
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "changes" in data:
            return data
    except json.JSONDecodeError:
        pass

    # Strategy 2: find {...} block anywhere in the response
    m = re.search(r"(\{.*\})", raw, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            if isinstance(data, dict) and "changes" in data:
                return data
        except json.JSONDecodeError:
            pass

    console.print(
        f"[yellow][!] Could not parse JSON from Claude reflection response.\n"
        f"First 400 chars:\n{raw[:400]}[/yellow]"
    )
    return None


def reset_task_credits() -> None:
    """Resets the task credit counter for the current thread."""
    _task_credits_local.value = 0.0


def get_task_credits() -> float:
    """Returns the accumulated task credits for the current thread."""
    return getattr(_task_credits_local, 'value', 0.0)
