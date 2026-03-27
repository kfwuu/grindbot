# GrindBot

Autonomous code improvement using Gemini CLI. Point it at any codebase, run it overnight, and wake up to merge-ready branches.

## Install

```bash
pip install -e .
```

## Usage

```bash
# Set up a project
grindbot init /path/to/project

# Scan for issues
grindbot scan /path/to/project

# Fix issues autonomously (overnight)
grindbot grind

# Review results
grindbot report
```

## How It Works

1. `scan` — Calls Gemini CLI to analyze the codebase and produces a prioritized task list saved to `.grindbot/tasks.json`
2. `grind` — For each pending task: creates an isolated git worktree, lets Gemini CLI fix the issue, validates the result (syntax + tests), commits, and records the branch
3. `report` — Shows completed/failed tasks and branch names ready to merge

## Requirements

- Python 3.10+
- [Gemini CLI](https://github.com/google-gemini/gemini-cli) on PATH
- Git
