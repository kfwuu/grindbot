"""Two-layer memory system for GrindBot agents.

Layer 1 - Short-term (session): Shared World Model + Event Stream scoped to
    the entire grind session, stored in:
        .grindbot/sessions/{session_id}/world_model.json
        .grindbot/sessions/{session_id}/events.jsonl

    All agents read from and write to this living knowledge base.
    Task 7 inherits everything tasks 1-6 discovered.

Layer 2 - Long-term (persistent): Per-agent YAML belief files with named keys,
    confidence scores, decay mechanics, and cross-agent tagging, stored in:
        .grindbot/memory/{agent}.yaml
        .grindbot/memory/belief_archive.yaml  (decayed beliefs, auditable)

    Beliefs are revised, not appended. Confidence decays over unused sessions.

Design constraints:
    - No memory = no crash: get_context_for_agent() returns "" if no session/file.
    - Thread-safe writes: _world_model_lock, _events_lock, _beliefs_lock.
    - Token budget: get_context_for_agent() hard-capped at ~500 tokens.
    - PyYAML optional: if not installed, belief layer is silently skipped.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Thread-safety locks (same pattern as _save_lock in executor.py)
# ---------------------------------------------------------------------------

_world_model_lock = threading.Lock()
_events_lock = threading.Lock()
_beliefs_lock = threading.Lock()

# ---------------------------------------------------------------------------
# YAML — optional; beliefs are silently skipped if PyYAML is not installed
# ---------------------------------------------------------------------------

try:
    import yaml as _yaml
    _YAML_AVAILABLE = True
except ImportError:
    _yaml = None  # type: ignore[assignment]
    _YAML_AVAILABLE = False

# Max chars injected into any agent prompt (~500 tokens at ~4 chars/token)
_MAX_CONTEXT_CHARS = 2_000

# Agent names with long-term belief files
_KNOWN_AGENTS = ("orchestrator", "executor", "reviewer", "scanner", "merge", "reflector")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _get_session_dir(session_id: str, project_root: Path) -> Path:
    """Return .grindbot/sessions/{session_id}/ directory path (not created)."""
    return project_root / ".grindbot" / "sessions" / session_id


def _get_memory_dir(project_root: Path) -> Path:
    """Return .grindbot/memory/ directory path (not created)."""
    return project_root / ".grindbot" / "memory"


def _belief_path(agent: str, project_root: Path) -> Path:
    """Return .grindbot/memory/{agent}.yaml path."""
    return _get_memory_dir(project_root) / f"{agent}.yaml"


def _archive_path(project_root: Path) -> Path:
    """Return .grindbot/memory/belief_archive.yaml path."""
    return _get_memory_dir(project_root) / "belief_archive.yaml"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _deep_merge(base: dict, patch: dict) -> dict:
    """Recursively merge patch into base, mutating base in place.

    Lists are extended (deduplicating plain strings). Scalars are overwritten.
    Dicts are recursed into.

    Args:
        base: Dict to merge into (mutated).
        patch: Dict with new values to layer on top.

    Returns:
        base after merging.
    """
    for key, val in patch.items():
        if key in base:
            if isinstance(base[key], dict) and isinstance(val, dict):
                _deep_merge(base[key], val)
            elif isinstance(base[key], list) and isinstance(val, list):
                existing_strings: set[str] = {x for x in base[key] if isinstance(x, str)}
                for item in val:
                    if isinstance(item, str):
                        if item not in existing_strings:
                            base[key].append(item)
                            existing_strings.add(item)
                    else:
                        base[key].append(item)
            else:
                base[key] = val
        else:
            base[key] = val
    return base


def _write_world_model(session_id: str, project_root: Path, model: dict) -> None:
    """Write world_model.json to disk. Caller must hold _world_model_lock."""
    path = _get_session_dir(session_id, project_root) / "world_model.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(model, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass  # Memory writes are best-effort — never crash the grind loop


def _load_yaml_file(path: Path) -> list[dict]:
    """Load a YAML belief file, returning an empty list on any error."""
    if not _YAML_AVAILABLE or not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
        data = _yaml.safe_load(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_yaml_file(path: Path, data: list[dict]) -> None:
    """Save a list of belief dicts as YAML. Caller must hold _beliefs_lock."""
    if not _YAML_AVAILABLE:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _yaml.dump(data, default_flow_style=False, allow_unicode=True),
            encoding="utf-8",
        )
    except Exception:
        pass  # Best-effort


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def open_session(project_root: Path) -> str:
    """Create a new grind session directory and initialise world_model.json.

    Args:
        project_root: Root directory of the target project (parent of .grindbot/).

    Returns:
        session_id string, e.g. "grind-2026-04-09-a3f2".
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    short_id = uuid.uuid4().hex[:4]
    session_id = f"grind-{date_str}-{short_id}"

    session_dir = _get_session_dir(session_id, project_root)
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return session_id  # Directory creation failed — return ID anyway

    world_model: dict[str, Any] = {
        "session_id": session_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "project_observations": {
            "patterns": [],
            "file_notes": {},
            "gotchas": [],
        },
        "task_outcomes": {},
        "agent_observations": {
            "orchestrator": [],
            "executor": [],
            "reviewer": [],
            "scanner": [],
        },
        "hypotheses": [],
    }
    _write_world_model(session_id, project_root, world_model)
    return session_id


def close_session(session_id: str, project_root: Path) -> None:
    """Write a human-readable session_summary.md to the session directory.

    Args:
        session_id: Active session identifier.
        project_root: Root directory of the target project.
    """
    model = get_world_model(session_id, project_root)
    if not model:
        return

    outcomes = model.get("task_outcomes", {})
    completed_ids = [k for k, v in outcomes.items() if v.get("status") == "completed"]
    failed_ids = [k for k, v in outcomes.items() if v.get("status") == "failed"]

    lines = [
        f"# Session: {session_id}",
        f"Started: {model.get('started_at', 'unknown')}",
        f"Closed: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Task Outcomes",
        f"Completed: {len(completed_ids)}  Failed: {len(failed_ids)}",
        "",
    ]

    for task_id, outcome in outcomes.items():
        icon = "+" if outcome.get("status") == "completed" else "-"
        lines.append(f"  {icon} [{task_id}] {outcome.get('title', '')}")
        if outcome.get("key_learning"):
            lines.append(f"    Learning: {outcome['key_learning']}")
        if outcome.get("failure_reason"):
            lines.append(f"    Failed: {outcome['failure_reason']}")

    hypotheses = model.get("hypotheses", [])
    if hypotheses:
        lines += ["", "## Hypotheses"]
        for h in hypotheses:
            lines.append(f"  [{h.get('confidence', '?')}] {h.get('claim', '')}")
            if h.get("suggested_action"):
                lines.append(f"    Action: {h['suggested_action']}")

    summary_path = _get_session_dir(session_id, project_root) / "session_summary.md"
    try:
        summary_path.write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# World model — reads
# ---------------------------------------------------------------------------


def get_world_model(session_id: str, project_root: Path) -> dict:
    """Load world_model.json for the given session.

    Args:
        session_id: Session identifier.
        project_root: Root directory of the target project.

    Returns:
        Parsed world model dict, or empty dict if missing or corrupt.
    """
    path = _get_session_dir(session_id, project_root) / "world_model.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def get_context_for_agent(
    agent: str,
    session_id: str | None,
    task: dict | None,
    project_root: Path | None,
) -> str:
    """Return a formatted memory context string for injection into agent prompts.

    Merges the relevant world model slice with filtered long-term beliefs for
    this agent. Returns "" if nothing is available — zero impact on existing
    flows. Hard-capped at ~500 tokens (2000 chars).

    Args:
        agent: Agent name: "orchestrator", "executor", "reviewer", "merge",
               "scanner", or "reflector".
        session_id: Active session identifier, or None for a cold start.
        task: Current task dict (used to look up file-specific notes), or None.
        project_root: Root directory of the target project, or None.

    Returns:
        Formatted multi-line string starting with "[Memory Context]",
        or "" if no memory is available.
    """
    parts: list[str] = []

    # Layer 1 — world model slice (session-scoped)
    if session_id and project_root:
        model = get_world_model(session_id, project_root)
        if model:
            obs = model.get("project_observations", {})
            agent_obs = model.get("agent_observations", {})
            task_outcomes = model.get("task_outcomes", {})
            hypotheses = model.get("hypotheses", [])

            if agent == "scanner":
                if obs.get("patterns"):
                    parts.append("Patterns seen in this codebase:")
                    for p in obs["patterns"][:5]:
                        parts.append(f"  - {p}")
                if obs.get("gotchas"):
                    parts.append("Known gotchas:")
                    for g in obs["gotchas"][:3]:
                        parts.append(f"  - {g}")

            elif agent == "orchestrator":
                file_notes = obs.get("file_notes", {})
                if task and task.get("file") and task["file"] in file_notes:
                    notes = file_notes[task["file"]][:3]
                    parts.append(f"Notes for {task['file']}:")
                    for n in notes:
                        parts.append(f"  - {n}")
                if agent_obs.get("orchestrator"):
                    parts.append("Orchestrator learnings this session:")
                    for o in agent_obs["orchestrator"][:3]:
                        parts.append(f"  - {o}")
                if hypotheses:
                    parts.append("Active hypotheses:")
                    for h in hypotheses[:2]:
                        parts.append(f"  [{h.get('confidence', '?')}] {h.get('claim', '')}")
                        if h.get("suggested_action"):
                            parts.append(f"    Suggested: {h['suggested_action']}")

            elif agent == "executor":
                completed = {
                    k: v for k, v in task_outcomes.items()
                    if v.get("status") == "completed"
                }
                if completed:
                    parts.append(f"Completed tasks this session ({len(completed)}):")
                    for tid, outcome in list(completed.items())[:3]:
                        parts.append(f"  - [{tid}] {outcome.get('title', '')}")
                        if outcome.get("key_learning"):
                            parts.append(f"    Learning: {outcome['key_learning']}")
                if agent_obs.get("executor"):
                    parts.append("Executor learnings this session:")
                    for o in agent_obs["executor"][:3]:
                        parts.append(f"  - {o}")

            elif agent == "reviewer":
                if task_outcomes:
                    recent = list(task_outcomes.items())[-3:]
                    parts.append("Recent task outcomes:")
                    for tid, outcome in recent:
                        status_label = outcome.get("status", "?")
                        parts.append(
                            f"  - [{tid}] {status_label}: {outcome.get('title', '')}"
                        )
                if agent_obs.get("reviewer"):
                    parts.append("Reviewer learnings this session:")
                    for o in agent_obs["reviewer"][:3]:
                        parts.append(f"  - {o}")

            elif agent == "merge":
                if obs.get("gotchas"):
                    parts.append("Known gotchas (verify before merging):")
                    for g in obs["gotchas"][:3]:
                        parts.append(f"  - {g}")
                if hypotheses:
                    parts.append("Active hypotheses:")
                    for h in hypotheses[:2]:
                        parts.append(
                            f"  [{h.get('confidence', '?')}] {h.get('claim', '')}"
                        )

            elif agent == "reflector":
                if obs.get("patterns"):
                    parts.append(f"Patterns: {'; '.join(obs['patterns'][:5])}")
                if obs.get("gotchas"):
                    parts.append(f"Gotchas: {'; '.join(obs['gotchas'][:3])}")
                if task_outcomes:
                    n_done = sum(
                        1 for v in task_outcomes.values()
                        if v.get("status") == "completed"
                    )
                    n_fail = sum(
                        1 for v in task_outcomes.values()
                        if v.get("status") == "failed"
                    )
                    parts.append(
                        f"Session outcomes: {n_done} completed, {n_fail} failed"
                    )

    # Layer 2 — long-term beliefs (persistent across sessions)
    if project_root:
        beliefs = load_beliefs_for_agent(agent, project_root)
        if beliefs:
            belief_str = format_beliefs_for_prompt(beliefs)
            if belief_str:
                parts.append(belief_str)

    if not parts:
        return ""

    header = f"[Memory Context — {agent}]"
    full = header + "\n" + "\n".join(parts)

    # Hard cap at ~500 tokens
    if len(full) > _MAX_CONTEXT_CHARS:
        full = full[:_MAX_CONTEXT_CHARS] + "\n... (memory context truncated)"

    return full


# ---------------------------------------------------------------------------
# World model — writes (thread-safe)
# ---------------------------------------------------------------------------


def update_world_model(
    session_id: str,
    project_root: Path,
    patch: dict,
) -> None:
    """Thread-safe JSON-patch update to world_model.json.

    Args:
        session_id: Session identifier.
        project_root: Root directory of the target project.
        patch: Nested dict describing the update (deep-merged into the model).
    """
    with _world_model_lock:
        model = get_world_model(session_id, project_root)
        if not model:
            return  # Session not found — skip silently
        _deep_merge(model, patch)
        _write_world_model(session_id, project_root, model)


def append_event(
    session_id: str,
    project_root: Path,
    agent: str,
    event: str,
    data: dict,
) -> None:
    """Append one JSON line to events.jsonl (append-only audit log).

    Args:
        session_id: Session identifier.
        project_root: Root directory of the target project.
        agent: Name of the agent emitting the event.
        event: Event type string, e.g. "gemini_result" or "diff_decision".
        data: Arbitrary event payload dict.
    """
    event_obj = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session": session_id,
        "agent": agent,
        "event": event,
        "data": data,
    }
    events_path = _get_session_dir(session_id, project_root) / "events.jsonl"
    with _events_lock:
        try:
            events_path.parent.mkdir(parents=True, exist_ok=True)
            with events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event_obj, ensure_ascii=False) + "\n")
        except OSError:
            pass  # Best-effort — never crash the grind loop


# ---------------------------------------------------------------------------
# Long-term belief management
# ---------------------------------------------------------------------------


def load_beliefs_for_agent(agent: str, project_root: Path) -> list[dict]:
    """Load and merge beliefs relevant to an agent.

    Loads the agent's own belief file, then cross-loads beliefs from other
    agents where relevant_to includes this agent. Own beliefs win on key
    conflicts. Filters to confidence >= 0.3 and sorts descending.

    Args:
        agent: Target agent name.
        project_root: Root directory of the target project.

    Returns:
        List of belief dicts sorted by confidence (highest first), or [].
    """
    if not _YAML_AVAILABLE:
        return []

    own_beliefs = _load_yaml_file(_belief_path(agent, project_root))
    merged: dict[str, dict] = {
        b["key"]: b
        for b in own_beliefs
        if isinstance(b, dict) and "key" in b
    }

    for other in _KNOWN_AGENTS:
        if other == agent:
            continue
        for belief in _load_yaml_file(_belief_path(other, project_root)):
            if not isinstance(belief, dict) or "key" not in belief:
                continue
            if agent not in belief.get("relevant_to", []):
                continue
            key = belief["key"]
            if key not in merged:
                merged[key] = belief

    active = [
        b for b in merged.values()
        if isinstance(b.get("confidence"), (int, float)) and b["confidence"] >= 0.3
    ]
    active.sort(key=lambda b: b.get("confidence", 0.0), reverse=True)
    return active


def apply_belief_diffs(
    agent: str,
    diffs: list[dict],
    project_root: Path,
) -> None:
    """Apply structured belief diffs returned by brain.reflect_session().

    Supported actions: "add", "revise", "reinforce".

    Args:
        agent: Target agent name (whose belief file to update).
        diffs: List of diff dicts from brain.reflect_session().
        project_root: Root directory of the target project.
    """
    if not _YAML_AVAILABLE or not diffs:
        return

    with _beliefs_lock:
        beliefs = _load_yaml_file(_belief_path(agent, project_root))
        by_key: dict[str, dict] = {
            b["key"]: b
            for b in beliefs
            if isinstance(b, dict) and "key" in b
        }

        for diff in diffs:
            action = diff.get("action", "")
            key = diff.get("key", "")
            if not key:
                continue

            if action == "add" and key not in by_key:
                by_key[key] = {
                    "key": key,
                    "belief": diff.get("belief", ""),
                    "confidence": float(diff.get("confidence", 0.5)),
                    "primary_agent": agent,
                    "relevant_to": diff.get("relevant_to", []),
                    "sessions_seen": [diff.get("session", "")],
                    "sessions_since_reinforced": 0,
                    "history": [],
                }

            elif action == "revise" and key in by_key:
                existing = by_key[key]
                existing.setdefault("history", []).append({
                    "session": diff.get("session", ""),
                    "was": existing.get("belief", ""),
                })
                existing["belief"] = diff.get("new_belief", existing["belief"])
                existing["confidence"] = float(
                    diff.get("confidence", existing["confidence"])
                )
                if "relevant_to" in diff:
                    existing["relevant_to"] = diff["relevant_to"]
                sessions_seen = existing.setdefault("sessions_seen", [])
                s = diff.get("session", "")
                if s and s not in sessions_seen:
                    sessions_seen.append(s)
                existing["sessions_since_reinforced"] = 0

            elif action == "reinforce" and key in by_key:
                existing = by_key[key]
                delta = float(diff.get("confidence_delta", 0.05))
                existing["confidence"] = min(
                    1.0, existing.get("confidence", 0.5) + delta
                )
                sessions_seen = existing.setdefault("sessions_seen", [])
                s = diff.get("session", "")
                if s and s not in sessions_seen:
                    sessions_seen.append(s)
                existing["sessions_since_reinforced"] = 0

        _save_yaml_file(_belief_path(agent, project_root), list(by_key.values()))


def run_decay_pass(project_root: Path, touched_keys: set[str]) -> None:
    """Increment sessions_since_reinforced for untouched beliefs; reduce confidence.

    For every belief NOT in touched_keys:
      - Increment sessions_since_reinforced.
      - If sessions_since_reinforced > 5: reduce confidence by 0.1 per extra session.

    Args:
        project_root: Root directory of the target project.
        touched_keys: Keys revised/reinforced this session (exempt from decay).
    """
    if not _YAML_AVAILABLE:
        return

    with _beliefs_lock:
        for agent in _KNOWN_AGENTS:
            path = _belief_path(agent, project_root)
            beliefs = _load_yaml_file(path)
            if not beliefs:
                continue
            changed = False
            for belief in beliefs:
                if not isinstance(belief, dict) or "key" not in belief:
                    continue
                if belief["key"] in touched_keys:
                    continue
                belief["sessions_since_reinforced"] = (
                    belief.get("sessions_since_reinforced", 0) + 1
                )
                overdue = belief["sessions_since_reinforced"] - 5
                if overdue > 0:
                    belief["confidence"] = max(
                        0.0, belief.get("confidence", 0.5) - 0.1 * overdue
                    )
                changed = True
            if changed:
                _save_yaml_file(path, beliefs)


def archive_decayed_beliefs(
    project_root: Path,
    threshold: float = 0.2,
) -> None:
    """Move beliefs with confidence < threshold to belief_archive.yaml.

    Args:
        project_root: Root directory of the target project.
        threshold: Confidence floor; beliefs below this are archived.
    """
    if not _YAML_AVAILABLE:
        return

    with _beliefs_lock:
        archive = _load_yaml_file(_archive_path(project_root))

        for agent in _KNOWN_AGENTS:
            path = _belief_path(agent, project_root)
            beliefs = _load_yaml_file(path)
            if not beliefs:
                continue

            active: list[dict] = []
            for belief in beliefs:
                if not isinstance(belief, dict):
                    continue
                if belief.get("confidence", 1.0) < threshold:
                    belief["archived_from"] = agent
                    belief["archived_at"] = datetime.now(timezone.utc).isoformat()
                    archive.append(belief)
                else:
                    active.append(belief)

            if len(active) != len(beliefs):
                _save_yaml_file(path, active)

        if archive:
            _save_yaml_file(_archive_path(project_root), archive)


def format_beliefs_for_prompt(
    beliefs: list[dict],
    max_lines: int = 40,
) -> str:
    """Format active beliefs as a clean human-readable context string.

    Sorted by confidence descending, capped at max_lines.

    Args:
        beliefs: List of belief dicts (already filtered to confidence >= 0.3).
        max_lines: Maximum output lines before truncating.

    Returns:
        Formatted string suitable for injection into agent prompts, or "".
    """
    if not beliefs:
        return ""

    lines = ["Long-term learnings:"]
    for b in beliefs:
        if len(lines) >= max_lines:
            remaining = len(beliefs) - (max_lines - 1)
            lines.append(f"  ... ({remaining} more)")
            break
        conf = b.get("confidence", 0.0)
        key = b.get("key", "?")
        belief_text = b.get("belief", "")
        lines.append(f"  [{conf:.0%}] {key}: {belief_text}")

    return "\n".join(lines)
