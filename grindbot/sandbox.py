"""Sandbox executor — runs GrindBot tasks in Firecracker microVMs on a remote server.

The local machine SSHes into a configured Hetzner (or any Linux) server,
pipes the task + repo to remote_worker.py, which boots a Firecracker VM,
runs Gemini, and streams the diff back.

User config in ~/.env (or environment):
    GRINDBOT_SERVER=root@65.109.94.87
    GRINDBOT_SSH_KEY=~/.ssh/id_ed25519   (optional, defaults to ~/.ssh/id_ed25519)
"""

import base64
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Any, Optional

from rich.console import Console

_SANDBOX_TIMEOUT = 300       # 5 min total VM lifetime
_GEMINI_RUN_TIMEOUT = 210    # 3.5 min for the Gemini call itself

# Directories/files to exclude from repo upload
_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules",
    ".worktrees", ".grindbot", "out", "dist", ".next", ".venv", "venv",
})
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
    """Run one task on a remote Firecracker server and return the file changes.

    Steps:
      1. Read server config from ~/.env (GRINDBOT_SERVER, GRINDBOT_SSH_KEY)
      2. Tar the repo and base64-encode it
      3. SSH into the server, run remote_worker.py with the payload piped to stdin
      4. Parse the JSON result from stdout
      5. Return diff + metadata for caller to apply locally

    Args:
        task: Task dict from tasks.json.
        repo_root: Absolute path to the local project repo root.
        prompt: Claude-orchestrated (or static) task prompt for Gemini.
        console: Rich console for progress output.
        timeout: Total allowed seconds for the remote job.

    Returns:
        Dict with keys: success (bool), diff (str), changed_files (list[str]),
        stdout (str), stderr (str).
    """
    env_vals = _load_env()

    server = env_vals.get("GRINDBOT_SERVER", "").strip()
    if not server:
        return _fail(
            "GRINDBOT_SERVER not set — add it to ~/.env, e.g.:\n"
            "  GRINDBOT_SERVER=root@65.109.94.87"
        )

    gemini_key = env_vals.get("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        return _fail("GEMINI_API_KEY not set — add it to ~/.env")

    ssh_key = env_vals.get("GRINDBOT_SSH_KEY", str(Path.home() / ".ssh" / "id_ed25519"))

    console.print(f"  [dim]🔥 Connecting to {server}...[/dim]")

    # Build payload
    try:
        tar_bytes = _tar_repo(repo_root)
    except Exception as exc:
        return _fail(f"Failed to tar repo: {exc}")

    payload = json.dumps({
        "task": task,
        "prompt": prompt,
        "gemini_key": gemini_key,
        "repo_tar_b64": base64.b64encode(tar_bytes).decode(),
        "gemini_run_timeout": _GEMINI_RUN_TIMEOUT,
    })

    console.print("  [dim]🔥 Uploading repo and booting VM...[/dim]")

    ssh_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout=10",
        "-i", ssh_key,
        server,
        "python3 -m grindbot.remote_worker",
    ]

    try:
        result = subprocess.run(
            ssh_cmd,
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        return _fail(f"Remote job timed out after {timeout}s")
    except FileNotFoundError:
        return _fail("ssh not found — install OpenSSH client")

    if result.returncode != 0:
        return _fail(
            f"SSH command failed (exit {result.returncode}):\n"
            f"{result.stderr[:500]}"
        )

    if not result.stdout.strip():
        return _fail(
            f"No output from remote worker.\nstderr: {result.stderr[:500]}"
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return _fail(
            f"Could not parse remote worker output:\n{result.stdout[:300]}"
        )

    # Redact Gemini key from output before returning
    data["stdout"] = _sanitize(data.get("stdout", ""), gemini_key)
    data["stderr"] = _sanitize(data.get("stderr", ""), gemini_key)

    if data.get("success"):
        console.print("  [dim]🔥 VM done, diff received.[/dim]")
    elif data.get("stderr"):
        console.print(f"  [red]!! VM error:[/red] {data['stderr'][:300]}")
    return data


# ─── Helpers ──────────────────────────────────────────────────────────────────


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


def _load_env() -> dict[str, str]:
    """Read GRINDBOT_SERVER, GRINDBOT_SSH_KEY, and GEMINI_API_KEY from env + ~/.env."""
    _WANTED = {"GRINDBOT_SERVER", "GRINDBOT_SSH_KEY", "GEMINI_API_KEY"}
    result: dict[str, str] = {}

    for key in _WANTED:
        val = os.environ.get(key, "").strip()
        if val:
            result[key] = val

    env_file = Path.home() / ".env"
    if not env_file.exists():
        return result
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key in _WANTED:
                result[key] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return result


def _sanitize(text: str, *secrets: str) -> str:
    """Replace secret values in text with [REDACTED]."""
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
