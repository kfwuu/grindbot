"""Git worktree management for GrindBot.

ALL git operations in GrindBot go through this module (design rule #4).
Rules enforced here:
  - NEVER runs `git checkout` (rule #6)
  - NEVER deletes files outside .worktrees/ (rule #7)
  - Only uses `git worktree add` / `git worktree remove` for isolation
"""

import shutil
import subprocess
from pathlib import Path
from typing import Optional


def create_worktree(
    repo_root: Path,
    branch_name: str,
    worktree_path: Path,
) -> tuple[bool, Optional[str]]:
    """Create a new git worktree on a fresh branch.

    If the target branch already exists (e.g. from a previous failed run),
    it is deleted before the worktree is created.  If the worktree path
    directory already exists it is removed first.

    Args:
        repo_root: Absolute path to the git repository root.
        branch_name: Name of the new branch to create inside the worktree.
        worktree_path: Filesystem path where the worktree will be placed.

    Returns:
        (True, None) on success, (False, error_message) on failure.
    """
    # Ensure the worktree directory does not already exist
    if worktree_path.exists():
        try:
            shutil.rmtree(str(worktree_path))
        except OSError as exc:
            return False, f"Could not remove existing worktree path: {exc}"

    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )

    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(worktree_path)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        # Branch already exists - delete it and retry
        if "already exists" in stderr:
            del_ok, del_err = _delete_branch(repo_root, branch_name)
            if not del_ok:
                return (
                    False,
                    f"Branch '{branch_name}' already exists and could not be deleted: {del_err}",
                )
            # Retry worktree creation
            retry = subprocess.run(
                ["git", "worktree", "add", "-b", branch_name, str(worktree_path)],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
            )
            if retry.returncode != 0:
                return False, retry.stderr.strip() or "git worktree add failed on retry"
            return True, None

        return False, stderr or "git worktree add failed"

    return True, None


def commit_worktree(
    worktree_path: Path,
    message: str,
) -> tuple[bool, Optional[str]]:
    """Stage all changes and create a commit inside a worktree.

    Args:
        worktree_path: Absolute path to the git worktree directory.
        message: Commit message string.

    Returns:
        (True, None) on success, (False, error_message) on failure.
    """
    # Stage everything
    add = subprocess.run(
        ["git", "add", "-A"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
    )
    if add.returncode != 0:
        return False, add.stderr.strip() or "git add -A failed"

    # Commit
    commit = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        stderr = commit.stderr.strip()
        # "nothing to commit" is not really an error from our perspective
        if "nothing to commit" in (stderr + commit.stdout):
            return False, "Nothing to commit - no changes were staged"
        return False, stderr or "git commit failed"

    return True, None


def remove_worktree(
    repo_root: Path,
    worktree_path: Path,
) -> tuple[bool, Optional[str]]:
    """Remove a git worktree directory (does NOT delete its branch).

    Args:
        repo_root: Absolute path to the git repository root.
        worktree_path: Absolute path to the worktree to remove.

    Returns:
        (True, None) on success, (False, error_message) on failure.
    """
    result = subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_path)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, result.stderr.strip() or "git worktree remove failed"
    return True, None


def cleanup_worktree(
    repo_root: Path,
    worktree_path: Path,
    branch_name: str,
    keep_branch: bool,
) -> None:
    """Remove the worktree and optionally delete the branch.

    Called after every task regardless of success or failure.  On success
    keep_branch=True so the branch remains available for merging.  On
    failure keep_branch=False so stale branches don't accumulate.

    Args:
        repo_root: Absolute path to the git repository root.
        worktree_path: Absolute path to the worktree directory.
        branch_name: Name of the branch associated with this worktree.
        keep_branch: If False, the branch is deleted after removing the worktree.
    """
    # Always attempt to remove the worktree
    remove_worktree(repo_root, worktree_path)

    # Also clean up the directory if git worktree remove left it behind
    if worktree_path.exists():
        try:
            shutil.rmtree(str(worktree_path))
        except OSError:
            pass  # Best-effort; don't crash the grind loop

    if not keep_branch:
        _delete_branch(repo_root, branch_name)


def merge_branch(
    repo_root: Path,
    branch_name: str,
) -> tuple[bool, Optional[str]]:
    """Merge a branch into the current HEAD with no-fast-forward.

    Stashes any uncommitted working-tree changes before merging and restores
    them afterwards, so GrindBot is safe to run against a repo with WIP edits.
    If git detects a merge conflict the merge is aborted automatically so
    the repository is left in a clean state.

    Args:
        repo_root: Absolute path to the git repository root (must be on the
            target branch already).
        branch_name: Name of the branch to merge in.

    Returns:
        (True, None) on success, (False, error_message) on conflict/failure.
    """
    # Guard: ensure HEAD is on main/master before merging (Gemini fix for task-001).
    default_branch_result = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if default_branch_result.returncode != 0:
        return (
            False,
            "HEAD is detached in repo_root; cannot merge. "
            "Ensure the main repository is on the default branch before merging.",
        )
    current_branch = default_branch_result.stdout.strip()
    if current_branch not in ("main", "master"):
        return (
            False,
            f"repo_root HEAD is on '{current_branch}', expected 'main' or 'master'. "
            "Aborting merge to avoid merging into the wrong branch.",
        )

    # Stash uncommitted changes so they don't block the merge.
    stash_result = subprocess.run(
        ["git", "stash", "--include-untracked", "-m", f"grindbot-pre-merge-{branch_name}"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    stashed = "No local changes" not in stash_result.stdout

    result = subprocess.run(
        ["git", "merge", branch_name, "--no-ff", "-m", f"Merge {branch_name}"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        subprocess.run(
            ["git", "merge", "--abort"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )
        if stashed:
            subprocess.run(
                ["git", "stash", "pop"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
            )
        err = result.stderr.strip() or result.stdout.strip() or "git merge failed"
        return False, err

    if stashed:
        subprocess.run(
            ["git", "stash", "pop"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
        )

    return True, None


def get_changed_files(worktree_path: Path) -> list[str]:
    """Return the list of files modified or added in the worktree.

    Uses `git status --porcelain` so both tracked and untracked new files
    are included.

    Args:
        worktree_path: Absolute path to the git worktree directory.

    Returns:
        List of relative file paths that have been changed.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
    )
    files: list[str] = []
    for line in result.stdout.splitlines():
        if line.strip():
            # Porcelain format: "XY filename" - filename starts at column 3
            files.append(line[3:].strip())
    return files


def get_diff(worktree_path: Path) -> str:
    """Return a combined diff of all changes in the worktree.

    Captures modifications to tracked files (git diff) plus a listing of
    any untracked new files, so the Claude reviewer always has full context.

    Args:
        worktree_path: Absolute path to the git worktree directory.

    Returns:
        Diff text, or empty string if no changes are detected.
    """
    # Modifications to tracked files (unstaged)
    diff_result = subprocess.run(
        ["git", "diff"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
    )
    diff = diff_result.stdout

    # New untracked files not shown by git diff
    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
    )
    new_files = [
        line[3:].strip()
        for line in status_result.stdout.splitlines()
        if line.startswith("?? ")
    ]
    if new_files:
        diff += "\n--- New untracked files ---\n" + "\n".join(new_files)

    return diff


def get_default_branch(repo_root: Path) -> str:
    """Return the name of the current branch at repo_root (master or main).

    Args:
        repo_root: Absolute path to the git repository root.

    Returns:
        Branch name string, defaults to 'master' if detection fails.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    branch = result.stdout.strip()
    return branch if branch else "master"


def push_branch(
    repo_root: Path,
    branch_name: str,
    remote: str = "origin",
) -> tuple[bool, Optional[str]]:
    """Push a branch to a remote. No-ops with a warning if no remote exists.

    Never uses --force. Only pushes the named branch, never HEAD or main
    directly, so there is no risk of overwriting remote history.

    Args:
        repo_root: Absolute path to the git repository root.
        branch_name: Local branch name to push.
        remote: Remote name (default 'origin').

    Returns:
        (True, None) on success or if no remote configured,
        (False, error_message) on push failure.
    """
    # Check remote exists
    check = subprocess.run(
        ["git", "remote", "get-url", remote],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        return True, None  # No remote - silently skip, not an error

    result = subprocess.run(
        ["git", "push", remote, branch_name],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, result.stderr.strip() or "git push failed"
    return True, None


def revert_last_commit(
    repo_root: Path,
    remote: str = "origin",
) -> tuple[bool, Optional[str]]:
    """Revert HEAD with a new commit and push if a remote exists.

    Non-destructive safety net — creates a revert commit rather than
    resetting history, so nothing is ever lost.

    Args:
        repo_root: Absolute path to the git repository root.
        remote: Remote name to push the revert to (default 'origin').

    Returns:
        (True, None) on success, (False, error_message) on failure.
    """
    result = subprocess.run(
        ["git", "revert", "HEAD", "--no-edit"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, result.stderr.strip() or "git revert HEAD failed"

    # Best-effort push of the revert commit
    default_branch = get_default_branch(repo_root)
    push_branch(repo_root, default_branch, remote=remote)

    return True, None


def get_head_diff(repo_root: Path) -> str:
    """Return the full diff of the most recent commit on the current branch.

    Used by the post-merge Claude reviewer to see exactly what landed.

    Args:
        repo_root: Absolute path to the git repository root.

    Returns:
        git show HEAD output as a string, or empty string on failure.
    """
    result = subprocess.run(
        ["git", "show", "HEAD", "--stat", "-p"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    return result.stdout if result.returncode == 0 else ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _delete_branch(
    repo_root: Path,
    branch_name: str,
) -> tuple[bool, Optional[str]]:
    """Delete a local git branch.

    This is an internal worktree.py helper - the only place in GrindBot
    allowed to run `git branch -D` (rule #4).

    Args:
        repo_root: Absolute path to the git repository root.
        branch_name: Name of the branch to delete.

    Returns:
        (True, None) on success, (False, error_message) on failure.
    """
    result = subprocess.run(
        ["git", "branch", "-D", branch_name],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, result.stderr.strip() or "git branch -D failed"
    return True, None
