"""Sandbox executor — runs GrindBot tasks in E2B cloud VMs.

Drop-in alternative to local worktree execution. The repo is uploaded to a
pre-built Linux sandbox, Gemini runs there with full tool access, and the
resulting file changes are returned as a git diff to apply locally.

Template must be built once:
    cd e2b && e2b template build
    # copy the printed template ID into E2B_TEMPLATE_ID in ~/.env
"""
import io
import os
import tarfile
from pathlib import Path
from typing import Any, Optional

from rich.console import Console

_TEMPLATE_ID_ENV = "E2B_TEMPLATE_ID"
_DEFAULT_TEMPLATE = "grindbot-gemini"
_SANDBOX_TIMEOUT = 300       # 5 min total sandbox lifetime
_GEMINI_RUN_TIMEOUT = 210    # 3.5 min for the Gemini call itself

# Directories to exclude when uploading the repo to the sandbox
_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules",
    ".worktrees", ".grindbot", "out", "dist", ".next", ".venv", "venv",
})

# File name patterns to exclude (sensitive credentials must not reach the sandbox)
_SKIP_FILE_PATTERNS = (
    ".env", "*.env", ".env.*",
    "*.key", "*.pem",
    "*secret*", "*credential*",
)


def execute_task_in_sandbox(
    task: dict[str, Any],
    repo_root: Path,
    prompt: str,
    console: Console,
    timeout: int = _SANDBOX_TIMEOUT,
) -> dict[str, Any]:
    """Run one task in an E2B cloud sandbox and return the file changes.

    Steps:
      1. Create sandbox from pre-built template (Gemini CLI already installed)
      2. Upload repo as tar.gz via E2B file API (no GitHub needed)
      3. Extract, init a temporary git baseline commit
      4. Write the Claude-orchestrated prompt and a Python runner script
      5. Run Gemini CLI inside the VM
      6. Stage all changes and capture a full git diff (including new files)
      7. Kill sandbox (always, in finally block)
      8. Return diff + metadata for caller to apply locally

    Args:
        task: Task dict from tasks.json.
        repo_root: Absolute path to the local project repo root.
        prompt: Claude-orchestrated (or static) task prompt for Gemini.
        console: Rich console for progress output.
        timeout: Total sandbox lifetime in seconds.

    Returns:
        Dict with keys: success (bool), diff (str), changed_files (list[str]),
        stdout (str), stderr (str).
    """
    try:
        from e2b import Sandbox
    except ImportError:
        return _fail("e2b package not installed — run: pip install e2b")

    _load_env_file()

    api_key = os.environ.get("E2B_API_KEY", "").strip()
    if not api_key:
        return _fail("E2B_API_KEY not set — add it to ~/.env")

    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        return _fail("GEMINI_API_KEY not set — add it to ~/.env")

    template = os.environ.get(_TEMPLATE_ID_ENV, _DEFAULT_TEMPLATE).strip()

    sandbox: Any = None
    try:
        console.print(f"    [dim]☁  Creating E2B sandbox (template: {template})...[/dim]")
        os.environ["E2B_API_KEY"] = api_key  # SDK reads from env
        sandbox = Sandbox.create(template=template, timeout=timeout)

        # ── Upload repo ───────────────────────────────────────────────────────
        console.print("    [dim]☁  Uploading repo...[/dim]")
        tar_bytes = _tar_repo(repo_root)
        sandbox.files.write("/workspace/repo.tar.gz", tar_bytes)

        setup = sandbox.commands.run(
            "cd /workspace && tar -xzf repo.tar.gz "
            "&& cd repo "
            "&& git init -q "
            "&& git add . "
            "&& git commit -q -m 'base'",
            timeout=60,
        )
        if setup.exit_code != 0:
            return _fail(f"Sandbox repo setup failed: {setup.stderr[:400]}")

        # ── Write prompt + runner ─────────────────────────────────────────────
        sandbox.files.write("/workspace/prompt.txt", prompt)

        # Python runner avoids all shell-escaping issues: the prompt is read
        # from a file, never embedded in a shell string.
        runner_script = _build_runner(_GEMINI_RUN_TIMEOUT)
        sandbox.files.write("/workspace/run_gemini.py", runner_script)

        # ── Run Gemini ────────────────────────────────────────────────────────
        console.print("    [dim]☁  Gemini running in sandbox...[/dim]")
        gem = sandbox.commands.run(
            "python3 /workspace/run_gemini.py",
            envs={"GEMINI_API_KEY": gemini_key},
            timeout=_GEMINI_RUN_TIMEOUT + 10,
        )

        # ── Capture diff (including new/untracked files) ──────────────────────
        # Stage everything so git diff --cached covers new files too.
        sandbox.commands.run("cd /workspace/repo && git add .", timeout=15)

        status_r = sandbox.commands.run(
            "cd /workspace/repo && git status --porcelain", timeout=10
        )
        changed_files = [
            line[3:].strip()
            for line in (status_r.stdout or "").splitlines()
            if line.strip()
        ]

        diff_r = sandbox.commands.run(
            "cd /workspace/repo && git diff --cached", timeout=15
        )
        diff = diff_r.stdout or ""

        success = bool(diff.strip())
        return {
            "success": success,
            "diff": diff,
            "changed_files": changed_files,
            "stdout": _sanitize(gem.stdout or "", gemini_key),
            "stderr": _sanitize(gem.stderr or "", gemini_key),
        }

    except Exception as exc:
        return _fail(str(exc))

    finally:
        if sandbox is not None:
            try:
                sandbox.kill()
                console.print("    [dim]☁  Sandbox killed.[/dim]")
            except Exception:
                pass


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _build_runner(run_timeout: int) -> str:
    """Return a Python script that runs Gemini CLI inside the sandbox VM.

    Reads the prompt from /workspace/prompt.txt.  GEMINI_API_KEY is injected
    via the sandbox process ``envs=`` argument — it is intentionally NOT
    embedded in this script so Gemini's ``--yolo`` file-read access cannot
    expose it.
    """
    return f"""\
import os, subprocess, sys

with open("/workspace/prompt.txt", encoding="utf-8") as f:
    prompt = f.read()

result = subprocess.run(
    ["gemini", "--model", "gemini-2.5-flash", "--yolo", "-p", prompt],
    cwd="/workspace/repo",
    capture_output=True,
    text=True,
    timeout={run_timeout},
    env=os.environ,
)
if result.stdout:
    print(result.stdout, end="")
if result.stderr:
    print(result.stderr, end="", file=sys.stderr)
sys.exit(result.returncode)
"""


def _tar_repo(repo_root: Path) -> bytes:
    """Create an in-memory tar.gz of the repo, skipping noise directories and
    sensitive credential files (.env, *.key, *.pem, *secret*, *credential*)."""
    import fnmatch

    def _filter(info: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
        p = Path(info.name)
        # Skip excluded directories at any depth
        if set(p.parts) & _SKIP_DIRS:
            return None
        # Skip sensitive files by name pattern
        filename = p.name
        for pattern in _SKIP_FILE_PATTERNS:
            if fnmatch.fnmatch(filename, pattern):
                return None
        return info

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(str(repo_root), arcname="repo", filter=_filter)
    return buf.getvalue()


def _load_env_file() -> None:
    """Load E2B_API_KEY, GEMINI_API_KEY, and E2B_TEMPLATE_ID from ~/.env if not
    already in the environment.  Mirrors the pattern used in brain.py for KIE_API_KEY."""
    env_file = Path.home() / ".env"
    if not env_file.exists():
        return
    _WANTED = {"E2B_API_KEY", "GEMINI_API_KEY", _TEMPLATE_ID_ENV}
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key in _WANTED and not os.environ.get(key, "").strip():
                os.environ[key] = val.strip().strip('"').strip("'")
    except OSError:
        pass


def _sanitize(text: str, *secrets: str) -> str:
    """Replace any secret values in *text* with ``[REDACTED]``."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def _fail(msg: str) -> dict[str, Any]:
    """Return a failed result dict."""
    return {
        "success": False,
        "diff": "",
        "changed_files": [],
        "stdout": "",
        "stderr": msg,
    }
