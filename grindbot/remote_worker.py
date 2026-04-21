"""Remote worker — runs one GrindBot task in a Firecracker microVM.

Called by sandbox.py via SSH. Reads a JSON payload from stdin (task + prompt
+ base64-encoded repo tarball), boots a VM, runs Gemini, and writes the
result JSON to stdout.

Never called directly by the user.
"""

import base64
import json
import os
import sys
from pathlib import Path


def main() -> None:
    """Read task payload from stdin, run in VM, write result JSON to stdout."""
    try:
        payload = json.loads(sys.stdin.read())
        task = payload["task"]
        prompt = payload["prompt"]
        gemini_key = payload["gemini_key"]
        tar_bytes = base64.b64decode(payload["repo_tar_b64"])
        gemini_run_timeout = payload.get("gemini_run_timeout", 210)
    except (KeyError, json.JSONDecodeError, Exception) as exc:
        _out_fail(f"Bad payload: {exc}")
        return

    try:
        from grindbot.firecracker_vm import FirecrackerVM
    except ImportError:
        _out_fail("grindbot not installed on server — run: pip install -e /opt/grindbot")
        return

    vm = None
    try:
        vm = FirecrackerVM.create()

        # Upload repo
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
            _out_fail(f"Repo setup failed: {setup.stderr[:400]}")
            return

        # Write prompt and runner
        vm.write_file("/workspace/prompt.txt", prompt)
        vm.write_file("/workspace/run_gemini.py", _build_runner(gemini_run_timeout))

        # Run Gemini
        gem = vm.run(
            "python3 /workspace/run_gemini.py",
            timeout=gemini_run_timeout + 10,
            env={"GEMINI_API_KEY": gemini_key},
        )

        # Capture diff
        vm.run("cd /workspace/repo && git add .", timeout=15)

        status_r = vm.run("cd /workspace/repo && git status --porcelain", timeout=10)
        changed_files = [
            line[3:].strip()
            for line in (status_r.stdout or "").splitlines()
            if line.strip()
        ]

        diff_r = vm.run("cd /workspace/repo && git diff --cached", timeout=15)
        diff = diff_r.stdout or ""

        result = {
            "success": bool(diff.strip()),
            "diff": diff,
            "changed_files": changed_files,
            "stdout": gem.stdout or "",
            "stderr": gem.stderr or "",
        }
        sys.stdout.write(json.dumps(result))
        sys.stdout.flush()

    except Exception as exc:
        _out_fail(str(exc))

    finally:
        if vm is not None:
            try:
                vm.kill()
            except Exception:
                pass


def _build_runner(run_timeout: int) -> str:
    """Return the Gemini runner script content."""
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


def _out_fail(msg: str) -> None:
    """Write a failure result to stdout."""
    sys.stdout.write(json.dumps({
        "success": False,
        "diff": "",
        "changed_files": [],
        "stdout": "",
        "stderr": msg,
    }))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
