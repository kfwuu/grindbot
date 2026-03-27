"""Planner: deduplicates and prioritizes raw scan results from scanner.py."""
from typing import Any

# Lower number = higher priority
_SEVERITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}
_CATEGORY_RANK: dict[str, int] = {"bug": 0, "security": 1, "performance": 2, "style": 3}


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


def plan(raw_tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run the full planning pipeline on a list of raw validated task dicts.

    Steps: deduplicate → prioritize → assign IDs.
    Returns a list ready to be saved to .grindbot/tasks.json.
    """
    tasks = deduplicate(raw_tasks)
    tasks = prioritize(tasks)
    tasks = assign_ids(tasks)
    return tasks
