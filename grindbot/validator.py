"""Validates changes made by Gemini CLI inside a worktree.

Checks (in order):
  1. At least one file was changed.
  2. All modified .py files parse without SyntaxError.
  3. Pyrefly type-checks modified .py files (non-fatal, reported as warnings).
  4. If pytest is available and a tests/ directory exists, the test suite passes.
"""

import ast
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ValidationResult:
    """Outcome of validating Gemini CLI changes in a worktree."""

    success: bool
    """True if all checks passed."""

    error: Optional[str] = None
    """Human-readable failure reason, or None on success."""

    changed_files: list[str] = field(default_factory=list)
    """Relative paths of files that were changed."""

    warnings: list[str] = field(default_factory=list)
    """Non-fatal warnings (e.g. tests skipped)."""


def validate_changes(worktree_path: Path, task: dict) -> ValidationResult:
    """Run all validation checks on a worktree after Gemini CLI has run.

    Args:
        worktree_path: Absolute path to the git worktree directory.
        task: The task dict (used for context in error messages).

    Returns:
        ValidationResult with success flag and details.
    """
    # --- 1. Check that something actually changed ---
    changed_files = _get_changed_files(worktree_path)
    if not changed_files:
        return ValidationResult(
            success=False,
            error="No files were changed - Gemini CLI may not have made any edits",
        )

    # --- 2. Syntax-check all modified Python files ---
    syntax_ok, syntax_error = _check_python_syntax(worktree_path, changed_files)
    if not syntax_ok:
        return ValidationResult(
            success=False,
            error=syntax_error,
            changed_files=changed_files,
        )

    # --- 3. Pyrefly type check (non-fatal) ---
    pyrefly_warnings = _check_pyrefly(worktree_path, changed_files)

    # --- 4. Run tests if available ---
    tests_ok, tests_error, tests_warning = _check_tests(worktree_path)
    if not tests_ok:
        return ValidationResult(
            success=False,
            error=tests_error,
            changed_files=changed_files,
            warnings=pyrefly_warnings,
        )

    warnings = pyrefly_warnings[:]
    if tests_warning:
        warnings.append(tests_warning)
    return ValidationResult(
        success=True,
        changed_files=changed_files,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_changed_files(worktree_path: Path) -> list[str]:
    """Return relative paths of all files modified or added in the worktree.

    Args:
        worktree_path: Absolute path to the git worktree directory.

    Returns:
        List of relative file path strings.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    files: list[str] = []
    for line in result.stdout.splitlines():
        if line.strip():
            files.append(line[3:].strip())
    return files


def _check_python_syntax(
    worktree_path: Path,
    changed_files: list[str],
) -> tuple[bool, Optional[str]]:
    """Parse every changed .py file and report the first SyntaxError found.

    Args:
        worktree_path: Root of the worktree.
        changed_files: List of relative file paths to check.

    Returns:
        (True, None) if all files parse OK, (False, error_message) otherwise.
    """
    for rel_path in changed_files:
        if not rel_path.endswith(".py"):
            continue
        full_path = worktree_path / rel_path
        if not full_path.exists():
            continue  # Deleted file - skip
        try:
            source = full_path.read_text(encoding="utf-8", errors="replace")
            ast.parse(source, filename=rel_path)
        except SyntaxError as exc:
            return False, f"Syntax error in {rel_path} (line {exc.lineno}): {exc.msg}"
        except Exception as exc:
            return False, f"Could not parse {rel_path}: {exc}"
    return True, None


def _check_pyrefly(
    worktree_path: Path,
    changed_files: list[str],
) -> list[str]:
    """Run pyrefly on modified Python files and return type errors as warnings.

    Non-fatal — existing codebases may have pre-existing type errors unrelated
    to Gemini's changes. Errors are surfaced as warnings so the grind loop
    continues and the reviewer can judge severity.

    Args:
        worktree_path: Root of the worktree.
        changed_files: List of relative file paths to check.

    Returns:
        List of warning strings (empty if pyrefly is unavailable or clean).
    """
    import shutil
    if shutil.which("pyrefly") is None:
        return []

    py_files = [
        f for f in changed_files
        if f.endswith(".py") and (worktree_path / f).exists()
    ]
    if not py_files:
        return []

    try:
        result = subprocess.run(
            ["pyrefly", "check"] + py_files,
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
    except (subprocess.TimeoutExpired, Exception):
        return ["pyrefly timed out or crashed — type check skipped"]

    if result.returncode == 0:
        return []

    # Cap output so it doesn't overflow the task record
    output = (result.stdout + result.stderr).strip()[:1000]
    return [f"pyrefly type errors (non-fatal):\n{output}"]


def _check_tests(
    worktree_path: Path,
) -> tuple[bool, Optional[str], Optional[str]]:
    """Run pytest if it is available and a tests/ directory exists.

    Args:
        worktree_path: Root of the worktree.

    Returns:
        Tuple of (passed, error_message, warning_message).
        If tests are skipped (no pytest / no tests dir), returns (True, None, warning).
    """
    # Check pytest availability
    try:
        pytest_check = subprocess.run(
            [sys.executable, "-m", "pytest", "--version"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (FileNotFoundError, OSError):
        return True, None, "Tests skipped - python executable not found on PATH"
    if pytest_check.returncode != 0:
        return True, None, "pytest not available - tests skipped"

    test_dirs = ['tests', 'test', 'spec']
    has_test_dir = any((worktree_path / d).is_dir() for d in test_dirs)

    if not has_test_dir:
        py_files = list(worktree_path.rglob('test_*.py'))
        py_files += list(worktree_path.rglob('*_test.py'))
        if not py_files:
            return True, None, (
                'No test directories or test files found'
                ' - skipping tests'
            )

    try:
        env = os.environ.copy()
        env['PYTHONPATH'] = str(worktree_path)
        result = subprocess.run(
            [sys.executable, '-m', 'pytest', '-q', '--tb=short', '--no-header'],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, "Test suite timed out after 120 seconds", None

    if result.returncode != 0:
        # Cap output to avoid overwhelming the task record
        combined = (result.stdout + result.stderr)[-2000:]
        return False, f"Tests failed:\n{combined}", None

    return True, None, None
