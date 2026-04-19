"""Sandbox executor — runs GrindBot tasks in Firecracker microVMs.

Each task gets a disposable Ubuntu 22.04 VM booted from a pre-built image.
The repo is uploaded via SCP, Gemini runs with full tool access, and the
resulting file changes are returned as a git diff to apply locally.

One-time server setup:
    See e2b/Dockerfile for how the base image was built.
    The server must have Firecracker installed and the base image at
    /opt/vm/rootfs.ext4 with /opt/vm/vmlinux.bin as the kernel.
"""
import io
import os
import tarfile
from pathlib import Path
from typing import Any, Optional

from rich.console import Console

_SANDBOX_TIMEOUT = 300       # 5 min total VM lifetime
_GEMINI_RUN_TIMEOUT = 210    # 3.5 min for the Gemini call itself

# Directories to exclude when uploading the repo to the sandbox
_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules",
    ".worktrees", ".grindbot", "out", "dist", ".next", ".venv", "venv",
})

# File name patterns to exclude (sensitive credentials must not reach the VM)
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
    """Run one task in a Firecracker microVM and return the file changes.

    Steps:
      1. Boot a fresh VM from the base image
      2. Upload repo as tar.gz via SCP
      3. Extract, init a temporary git baseline commit
      4. Write the prompt and a Python runner script
      5. Run Gemini CLI inside the VM
      6. Stage all changes and capture a full git diff (including new files)
      7. Kill VM (always, in finally block)
      8. Return diff + metadata for caller to apply locally

    Args:
        task: Task dict from tasks.json.
        repo_root: Absolute path to the local project repo root.
        prompt: Claude-orchestrated (or static) task prompt for Gemini.
        console: Rich console for progress output.
        timeout: Total VM lifetime in seconds.

    Returns:
        Dict with keys: success (bool), diff (str), changed_files (list[str]),
        stdout (str), stderr (str).
    """
    try:
        from .firecracker_vm import FirecrackerVM
    except ImportError:
        return _fail("firecracker_vm module not found — deploy grindbot to the Hetzner server")

    gemini_key = _load_gemini_key()
    if not gemini_key:
        return _fail("GEMINI_API_KEY not set — add it to ~/.env")

    vm: Any = None
    try:
        console.print("    [dim]🔥 Booting Firecracker VM...[/dim]")
        vm = FirecrackerVM.create(timeout=timeout)

        # ── Upload repo ───────────────────────────────────────────────────────
        console.print("    [dim]🔥 Uploading repo...[/dim]")
        tar_bytes = _tar_repo(repo_root)
        vm.run("mkdir -p /workspace", timeout=10)
        vm.write_file("/workspace/repo.tar.gz", tar_bytes)

        setup = vm.run(
            "cd /workspace && tar -xzf repo.tar.gz "
            "&& cd repo "
            "&& git init -q "
            "&& git config user.email 'grindbot@local' "
            "&& git config user.name 'GrindBot' "
            "&& git add . "
            "&& git commit -q -m 'base'",
            timeout=60,
        )
        if setup.exit_code != 0:
            return _fail(f"VM repo setup failed: {setup.stderr[:400]}")

        # ── Write prompt + runner ─────────────────────────────────────────────
        vm.write_file("/workspace/prompt.txt", prompt)

        # Python runner avoids all shell-escaping issues: the prompt is read
        # from a file, never embedded in a shell string.
        runner_script = _build_runner(_GEMINI_RUN_TIMEOUT)
        vm.write_file("/workspace/run_gemini.py", runner_script)

        # ── Run Gemini ────────────────────────────────────────────────────────
        console.print("    [dim]🔥 Gemini running in VM...[/dim]")
        gem = vm.run(
            "python3 /workspace/run_gemini.py",
            timeout=_GEMINI_RUN_TIMEOUT + 10,
            env={"GEMINI_API_KEY": gemini_key},
        )

        # ── Capture diff (including new/untracked files) ──────────────────────
        vm.run("cd /workspace/repo && git add .", timeout=15)

        status_r = vm.run("cd /workspace/repo && git status --porcelain", timeout=10)
        changed_files = [
            line[3:].strip()
            for line in (status_r.stdout or "").splitlines()
            if line.strip()
        ]

        diff_r = vm.run("cd /workspace/repo && git diff --cached", timeout=15)
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
        if vm is not None:
            try:
                vm.kill()
                console.print("    [dim]🔥 VM killed.[/dim]")
            except Exception:
                pass


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _build_runner(run_timeout: int) -> str:
    """Return a Python script that runs Gemini CLI inside the VM.

    Reads the prompt from /workspace/prompt.txt. GEMINI_API_KEY is injected
    via the SSH env= argument — it is intentionally NOT embedded in this
    script so Gemini's --yolo file-read access cannot expose it.
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
    """Create an in-memory tar.gz of the repo, skipping noise and credentials."""
    import fnmatch

    def _filter(info: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
        p = Path(info.name)
        if set(p.parts) & _SKIP_DIRS:
            return None
        for pattern in _SKIP_FILE_PATTERNS:
            if fnmatch.fnmatch(p.name, pattern):
                return None
        return info

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(str(repo_root), arcname="repo", filter=_filter)
    return buf.getvalue()


def _load_gemini_key() -> str:
    """Read GEMINI_API_KEY from environment or ~/.env."""
    val = os.environ.get("GEMINI_API_KEY", "").strip()
    if val:
        return val
    env_file = Path.home() / ".env"
    if not env_file.exists():
        return ""
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, v = line.partition("=")
            if key.strip() == "GEMINI_API_KEY":
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _sanitize(text: str, *secrets: str) -> str:
    """Replace any secret values in *text* with [REDACTED]."""
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
