"""Planner: deduplicates and prioritizes raw scan results from scanner.py."""
from typing import Any

# Lower number = higher priority
_SEVERITY_RANK: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_CATEGORY_RANK: dict[str, int] = {"bug": 0, "security": 1, "performance": 2, "reliability": 3, "style": 4}


def _dedup_key(task: dict[str, Any]) -> str:
    """Return a deduplication key derived from file path + normalised title.

    Two tasks with the same file and whitespace-collapsed title are considered
    duplicates; the first occurrence wins.
    """
    file_part = (task.get("file") or "").lower().strip()
    title_part = " ".join((task.get("title") or "").lower().split())
    return f"{file_part}::{title_part}"


def deduplicate(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove duplicate tasks, keeping the first occurrence of each file+title pair."""
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for task in tasks:
        key = _dedup_key(task)
        if key not in seen:
            seen.add(key)
            result.append(task)
    return result


def prioritize(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort tasks: high severity first, then by category (bug > security > performance > style).

    Within the same severity and category, tasks are sorted alphabetically by title
    for stable, deterministic output.
    """
    return sorted(
        tasks,
        key=lambda t: (
            _SEVERITY_RANK.get(t.get("severity", "low"), 99),
            _CATEGORY_RANK.get(t.get("category", "style"), 99),
            (t.get("title") or "").lower(),
        ),
    )


def assign_ids(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign sequential 'id' and 'order' fields and initialise task state fields.

    Adds: id (zero-padded string), order (int), status, branch, error.
    """
    result: list[dict[str, Any]] = []
    for i, task in enumerate(tasks, start=1):
        result.append(
            {
                "id": f"{i:03d}",
                "order": i,
                **task,
                "status": "pending",
                "branch": None,
                "error": None,
            }
        )
    return result


def _is_executable(task: dict[str, Any]) -> bool:
    """Return False for tasks that are too vague or destructive to run safely."""
    # Must target a specific file
    file_path = (task.get("file") or "").strip()
    if not file_path:
        return False

    # Reject tasks that start with destructive verbs
    title = (task.get("title") or "").lower().strip()
    _DESTRUCTIVE = ("remove ", "delete ", "drop ", "eliminate ", "uninstall ", "disable ")
    if any(title.startswith(verb) for verb in _DESTRUCTIVE):
        return False

    return True


def merge_new_tasks(
    existing_tasks: list[dict[str, Any]],
    raw_new_tasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge newly-scanned tasks into an existing task list without duplicates.

    New tasks are filtered, deduplicated against each other and against the
    existing list, then appended with IDs continuing from the current maximum.

    Args:
        existing_tasks: Current task list (may be empty).
        raw_new_tasks: Raw tasks returned by a fresh scan.

    Returns:
        Combined list with new non-duplicate tasks appended.
    """
    # Build a set of dedup keys from existing tasks so we don't re-add them.
    existing_keys: set[str] = {_dedup_key(t) for t in existing_tasks}

    new_candidates = [t for t in raw_new_tasks if _is_executable(t)]
    # Deduplicate among the new candidates themselves
    seen: set[str] = set()
    unique_new: list[dict[str, Any]] = []
    for t in new_candidates:
        key = _dedup_key(t)
        if key not in seen and key not in existing_keys:
            seen.add(key)
            unique_new.append(t)

    if not unique_new:
        return existing_tasks

    unique_new = prioritize(unique_new)

    # Assign IDs continuing after the current maximum
    max_order = max((t.get("order", 0) for t in existing_tasks), default=0)
    appended: list[dict[str, Any]] = []
    for i, task in enumerate(unique_new, start=max_order + 1):
        appended.append(
            {
                "id": f"{i:03d}",
                "order": i,
                **task,
                "status": "pending",
                "branch": None,
                "error": None,
            }
        )

    return existing_tasks + appended


def plan(raw_tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run the full planning pipeline on a list of raw validated task dicts.

    Steps: filter → deduplicate → prioritize → assign IDs.
    Returns a list ready to be saved to .grindbot/tasks.json.
    """
    tasks = [t for t in raw_tasks if _is_executable(t)]
    tasks = deduplicate(tasks)
    tasks = prioritize(tasks)
    tasks = assign_ids(tasks)
    return tasks
