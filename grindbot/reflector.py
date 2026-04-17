"""reflector.py — Prompt RL engine for GrindBot.

After a grind session completes, collects outcome data, calls Claude to evaluate
which prompts contributed to failures, and updates .grindbot/prompts.json with
improved prompt templates for the next session.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import brain
from . import memory as _memory
from .config import CREDIT_COST_USD, load_prompt_store, save_prompt_store


def _collect_session_data(tasks: list[dict]) -> dict[str, Any]:
    """Aggregate task outcomes into a structured summary for Claude to evaluate.

    Args:
        tasks: Full task list after a grind session.

    Returns:
        Dict with total/completed/failed counts, success_rate, and per-task details.
    """
    executed = [t for t in tasks if t.get("status") in ("completed", "failed")]
    completed = [t for t in executed if t.get("status") == "completed"]
    failed = [t for t in executed if t.get("status") == "failed"]

    task_summaries = []
    for t in executed:
        task_summaries.append({
            "id": t.get("id"),
            "title": t.get("title"),
            "severity": t.get("severity"),
            "category": t.get("category"),
            "status": t.get("status"),
            "prompt_type": t.get("prompt_type"),
            "error": t.get("error"),
            "merge_reason": t.get("merge_reason"),
            "had_warnings": bool(t.get("validation_warnings")),
        })

    total = len(executed)
    return {
        "total_tasks": total,
        "completed": len(completed),
        "failed": len(failed),
        "success_rate": round(len(completed) / total, 2) if total else 0.0,
        "tasks": task_summaries,
    }


def _get_current_prompts(store: dict) -> dict[str, str]:
    """Return the current effective prompt templates (evolved or hardcoded defaults).

    Imports the hardcoded defaults from brain.py and scanner.py as fallbacks
    so Claude always sees the full prompt text, never a missing key.

    Args:
        store: Prompt store dict from .grindbot/prompts.json (may be empty).

    Returns:
        Dict keyed by agent name with the currently active prompt text.
    """
    from . import brain as _brain

    defaults = {
        "brain_plan": _brain._PLAN_SYSTEM,
        "brain_orchestrate": _brain._ORCHESTRATE_SYSTEM,
        "brain_review_diff": _brain._REVIEW_SYSTEM,
        "brain_review_merge": _brain._MERGE_REVIEW_SYSTEM,
        "executor_task_tool": "TASK: Make one specific code change.",
    }

    evolved = store.get("prompts", {})
    return {key: evolved.get(key, default) for key, default in defaults.items()}


def run_reflection(
    grindbot_dir: Path,
    tasks: list[dict],
    console: Console,
    session_id: Optional[str] = None,
) -> bool:
    """Run the reflection / prompt-RL step after a grind session.

    Steps:
      1. Collect structured session outcome data from the task list.
      2. Load current prompt store from .grindbot/prompts.json.
      3. Call brain.reflect_session() — Claude reviews outcomes and suggests changes.
      4. If changes returned: update store schema, save, display summary.
      5. If no changes or API failure: display status and return False.

    Args:
        grindbot_dir: Absolute path to the project's .grindbot/ directory.
        tasks: Full task list returned by run_grind().
        console: Rich console for all output.

    Returns:
        True if prompts were updated and saved, False otherwise.
    """
    # --- 1. Collect session data -------------------------------------------
    session_data = _collect_session_data(tasks)

    if session_data["total_tasks"] == 0:
        console.print("[dim]No executed tasks to reflect on — skipping.[/dim]")
        return False

    console.print(
        f"[dim]Reflecting on {session_data['total_tasks']} task(s): "
        f"{session_data['completed']} completed, "
        f"{session_data['failed']} failed "
        f"(success rate: {session_data['success_rate']:.0%})[/dim]"
    )

    # --- 2. Load current store + prompts ------------------------------------
    store = load_prompt_store(grindbot_dir)
    current_prompts = _get_current_prompts(store)

    # --- 3. Call Claude for reflection -------------------------------------
    console.print("[dim]Calling Claude Opus 4.6 for prompt reflection...[/dim]")
    brain.reset_task_credits()
    result = brain.reflect_session(session_data, current_prompts)
    reflect_credits = brain.get_task_credits()
    reflect_usd = reflect_credits * CREDIT_COST_USD
    console.print(
        f"  [bold green]Reflection cost: {reflect_credits:.2f} credits -> ${reflect_usd:.4f}[/bold green]"
    )

    if result is None:
        console.print(
            "[yellow][!] Reflection skipped — Claude unavailable or API error.[/yellow]"
        )
        return False

    changes: list[dict] = result.get("changes", [])
    reasoning: str = result.get("reasoning", "")
    belief_diffs: list[dict] = result.get("belief_diffs", [])

    if not changes:
        console.print(
            "[green]Reflection complete — no prompt changes needed "
            f"(success rate was {session_data['success_rate']:.0%}).[/green]"
        )
        if reasoning:
            console.print(f"[dim]{reasoning}[/dim]")
        return False

    # --- 4. Apply changes to the store ------------------------------------
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    iteration = store.get("iteration", 0) + 1

    evolved_prompts: dict[str, str] = store.get("prompts", {}).copy()
    history_entry: dict[str, Any] = {
        "iteration": iteration,
        "updated_at": now_iso,
        "reasoning": reasoning,
        "changes": [],
    }

    for change in changes:
        agent = change.get("agent", "")
        new_prompt = change.get("new_prompt", "")
        reason = change.get("reason", "")
        if agent and new_prompt:
            evolved_prompts[agent] = new_prompt
            history_entry["changes"].append(f"{agent}: {reason}")

    history: list[dict] = store.get("history", [])
    history.append(history_entry)

    updated_store: dict[str, Any] = {
        "version": 1,
        "iteration": iteration,
        "updated_at": now_iso,
        "prompts": evolved_prompts,
        "history": history,
    }

    # --- 5. Save and display -----------------------------------------------
    save_prompt_store(grindbot_dir, updated_store)

    # --- 6. Apply belief diffs to long-term memory -------------------------
    if belief_diffs:
        project_root = grindbot_dir.parent
        touched_keys: set[str] = set()
        applied = 0
        for diff in belief_diffs:
            agent = diff.get("agent", "")
            key = diff.get("key", "")
            if agent and key:
                try:
                    if session_id:
                        diff = {**diff, "session": session_id}
                    _memory.apply_belief_diffs(agent, [diff], project_root)
                    touched_keys.add(key)
                    applied += 1
                except Exception:
                    pass
        if applied:
            console.print(
                f"[dim]Memory: {applied} belief(s) written to "
                f".grindbot/memory/[/dim]"
            )
        # Decay stale beliefs and archive those below confidence threshold
        try:
            _memory.run_decay_pass(project_root, touched_keys)
            _memory.archive_decayed_beliefs(project_root)
        except Exception:
            pass

    _show_reflection(changes, reasoning, iteration, console)
    return True


def _show_reflection(
    changes: list[dict],
    reasoning: str,
    iteration: int,
    console: Console,
) -> None:
    """Display a Rich-formatted summary of what the reflector changed.

    Args:
        changes: List of change dicts with 'agent', 'reason', 'new_prompt'.
        reasoning: One-paragraph reasoning summary from Claude.
        iteration: New prompt store iteration number.
        console: Rich console for all output.
    """
    table = Table(
        title=f"[bold cyan]Prompt RL — Iteration {iteration}[/bold cyan]",
        border_style="cyan",
        show_lines=True,
        expand=True,
    )
    table.add_column("Agent", style="bold", width=22)
    table.add_column("Reason for Change")

    for change in changes:
        agent = change.get("agent", "?")
        reason = change.get("reason", "")
        table.add_row(agent, reason)

    console.print(table)

    if reasoning:
        console.print(
            Panel(
                reasoning,
                title="[bold]Reflection Summary[/bold]",
                border_style="dim",
                padding=(0, 1),
            )
        )

    console.print(
        f"[green]{len(changes)} prompt(s) updated[/green] and saved to "
        f"[dim].grindbot/prompts.json[/dim] (iteration {iteration})."
    )
