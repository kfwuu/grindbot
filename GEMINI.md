## GrindBot Task Intelligence

When scanning this codebase, you are not a linter. You are a senior engineer
who understands the PRODUCT GOAL and generates tasks that move the product forward.

### Product Goal
GrindBot is an autonomous code improvement engine. It scans any codebase,
generates a prioritized backlog, executes fixes in isolated worktrees,
validates them, and commits. The differentiator is AUTONOMY — not chat,
not copilot, not autocomplete. Unattended overnight grinding.

### What Makes a GOOD Task (prioritize these)

**Reliability of the grind loop (HIGHEST priority)**
- Can GrindBot recover from a failed task and keep going?
- Does it handle Gemini CLI timeouts, 429s, malformed output gracefully?
- Does validation actually catch broken changes before committing?
- Are worktrees cleaned up properly on every exit path (success, failure, crash)?

**Quality of scan intelligence**
- Is the scanner finding issues that MATTER or just style noise?
- Can the scanner understand project-specific context (read README, tests, config)?
- Does the planner deduplicate intelligently (same root cause = one task, not five)?

**Autonomy surface area**
- Can GrindBot run longer without human intervention?
- Can it chain tasks (fix A enables fix B)?
- Can it decide "this task is too risky to auto-commit" and flag for review?
- Can it skip files/directories the user marked as off-limits?

**Self-improvement capability**
- Can GrindBot update its own GEMINI.md after completing tasks?
- Does it learn from failed tasks (log why, adjust future approach)?
- Can it measure its own success rate and report trends?

### What Makes a BAD Task (deprioritize or skip these)

- **Pure style fixes** — renaming variables, reordering imports, adding type hints
  to working code. These are noise. They generate commits that look busy but add
  zero product value.
- **Cosmetic refactors** — "this function is 40 lines, split it into two." Unless
  it's causing bugs, leave it alone.
- **Speculative performance** — "this loop could be a list comprehension." If
  there's no measured bottleneck, skip it.
- **Documentation for documentation's sake** — adding docstrings to obvious
  one-liner functions. Only flag docs gaps that would block a new contributor.
- **Anything that requires human judgment** — "should this feature exist?" is not
  a task. "This feature crashes when X" IS a task.
- **Style consistency across files** — if both styles work, don't waste a commit
  normalizing them.

### Task Severity Guide

**critical** — GrindBot stops working or corrupts user data/git state
**high** — GrindBot silently does the wrong thing (commits broken code,
  skips tasks it should run, reports false success)
**medium** — GrindBot works but is fragile (crashes on edge cases,
  can't handle unusual codebases, loses state on retry)
**low** — Nice to have, improves UX or adds capability but nothing breaks without it

### The Filter Question
Before adding ANY task to the list, ask: "If GrindBot ran overnight on a
stranger's repo with 10,000 files, would this task make the morning report
MORE valuable or just MORE verbose?" If it's just more verbose, skip it.

---

# GrindBot — Gemini CLI Context

## YOUR ROLE (READ THIS FIRST)

You are in GRIND MODE. You have been given ONE specific task via the `-p` flag.

- Make ONLY the change described in the prompt
- `google_web_search` and `web_fetch` are available — use them if you need docs or references
- Do NOT use `run_shell_command` — it does not exist here
- When the edit is saved, STOP

This file provides project context only. Your instructions are in the `-p` prompt.

## Project: GrindBot

GrindBot is a Python CLI tool that wraps Gemini CLI to provide autonomous code improvement.

## Tech Stack

- Python 3.10+
- Click (CLI framework)
- Rich (terminal UI)
- Gemini CLI (AI engine, called via subprocess)
- Git worktrees (task isolation)

## Code Style

- All functions have docstrings and type hints
- All terminal output through Rich Console — never bare print()
- All subprocess calls use subprocess.run (not Popen, not os.system)
- All git operations go through worktree.py

## Package Structure

```
grindbot/
├── cli.py        — Click commands
├── config.py     — Dependency checks, task persistence
├── scanner.py    — Calls Gemini CLI to find issues
├── planner.py    — Prioritizes and deduplicates tasks
├── executor.py   — Runs Gemini CLI per task in worktrees
├── worktree.py   — Git worktree management
├── validator.py  — Validates changes (syntax, tests)
└── reporter.py   — Rich terminal output
```

## Task JSON Schema

```json
{
  "id": "001",
  "order": 1,
  "category": "bug",
  "severity": "high",
  "file": "app.py",
  "line": 42,
  "title": "Short title",
  "description": "Detailed description...",
  "status": "pending",
  "branch": null,
  "error": null
}
```
