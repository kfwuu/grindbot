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
