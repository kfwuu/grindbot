# GrindBot — CLAUDE.md

## What This Is
GrindBot is a Python CLI tool that wraps Google's Gemini CLI to provide autonomous code improvement. Users point it at any codebase, it finds issues, fixes them overnight in isolated git worktrees, validates the fixes, and commits. Wake up to merge-ready branches.

## Core Commands
- grindbot scan <path> — Calls Gemini CLI to analyze codebase, outputs prioritized task list
- grindbot grind — Executes pending tasks autonomously, each in its own git worktree
- grindbot report — Shows results of the last grind session
- grindbot init <path> — Sets up .grindbot/ directory and generates GEMINI.md

## Tech Stack
- Python 3.10+
- Click (CLI framework)
- Rich (terminal UI — spinners, tables, panels, progress bars)
- Gemini CLI (the AI engine, called via subprocess)
- Git worktrees (isolation per task)
- JSON files for state (.grindbot/tasks.json)

## File Structure
grindbot/
├── CLAUDE.md              ← You are here
├── README.md              ← GitHub-facing docs
├── GEMINI.md              ← Context file for when GrindBot works on itself
├── pyproject.toml         ← Package config, pip installable
├── setup.py               ← Backwards compat
├── .gitignore
├── grindbot/
│   ├── init.py        ← Version string
│   ├── cli.py             ← Click commands: scan, grind, report, init
│   ├── config.py          ← Dependency checks, path utils, task persistence
│   ├── scanner.py         ← Calls Gemini CLI to find issues in a codebase
│   ├── planner.py         ← Prioritizes and deduplicates scan results
│   ├── executor.py        ← Runs one Gemini CLI call per task in a worktree
│   ├── worktree.py        ← Git worktree create/commit/merge/cleanup
│   ├── validator.py       ← Checks if changes parse, tests pass, nothing broke
│   └── reporter.py        ← Rich formatted terminal output for all displays
└── tests/
└── (later)

## How Gemini CLI Is Called
GrindBot does NOT implement its own agentic loop. It shells out to Gemini CLI:
```python
subprocess.run(
    ["gemini", "-p", prompt_string, "--yolo"],
    cwd=working_directory,
    capture_output=True,
    text=True,
    timeout=180
)
```
Gemini CLI handles all file reading, editing, tool calling, and reasoning. GrindBot is the orchestrator — it manages tasks, worktrees, validation, and reporting.

## Critical Design Rules
1. NEVER use bare print(). ALL terminal output goes through Rich Console.
2. ALL functions have docstrings and type hints.
3. ALL subprocess calls use subprocess.run (not Popen, not os.system).
4. ALL git operations go through worktree.py — no raw git calls elsewhere.
5. Task state lives in .grindbot/tasks.json — no database, no SQLite.
6. GrindBot NEVER runs git checkout. Only git worktree add/remove.
7. GrindBot NEVER deletes files outside the .worktrees/ directory.
8. ALL errors are caught and displayed with Rich — never crash silently.
9. Blocked git commands in executor: checkout, branch -D, clean, reset --hard, push, merge (merge only through worktree.py).

## How the Scan Pipeline Works
1. User runs: grindbot scan /path/to/project
2. scanner.py builds a prompt asking Gemini to find issues
3. scanner.py calls Gemini CLI with that prompt in the project directory
4. scanner.py parses the JSON response into a list of task dicts
5. planner.py deduplicates and prioritizes the tasks
6. Tasks are saved to .grindbot/tasks.json
7. reporter.py displays the task list as a Rich table

## How the Grind Pipeline Works
1. User runs: grindbot grind
2. executor.py loads pending tasks from .grindbot/tasks.json
3. For each task:
   a. worktree.py creates an isolated git worktree on a new branch
   b. GEMINI.md is copied into the worktree for context
   c. executor.py calls Gemini CLI with the task prompt in the worktree
   d. validator.py checks: files parse, tests pass, something changed
   e. If valid: commit in worktree, mark task completed, record branch name
   f. If invalid: mark task failed with reason
   g. worktree.py cleans up the worktree (keeps branch if completed)
4. executor.py saves updated tasks to .grindbot/tasks.json
5. reporter.py shows the grind report

## Task JSON Schema
```json
{
  "id": "001",
  "order": 1,
  "category": "bug",
  "severity": "high",
  "file": "app.py",
  "line": 42,
  "title": "No error handling in user endpoint",
  "description": "The /users/<id> endpoint has no try/except...",
  "status": "pending",
  "branch": null,
  "error": null
}
```
Status values: pending, completed, failed

## Agent Build Order
1. Agent 1 (Architect): cli.py, config.py, pyproject.toml, setup.py, .gitignore, __init__.py, GEMINI.md, README.md
2. Agent 2 (Scan): scanner.py, planner.py, reporter.py, update cli.py scan command
3. Agent 3 (Grind): executor.py, worktree.py, validator.py, update cli.py grind command
Agent 1 runs first. Agent 2 and 3 run in parallel after Agent
