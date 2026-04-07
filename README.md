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
- [Gemini CLI](https://github.com/google-gemini/gemini-cli) on PATH (`gemini` must be on your `$PATH`)
- Git
- A [Kie.ai](https://kie.ai) API key — GrindBot's brain is Claude Opus 4.6 running through Kie.ai

## Setup

**1. Install GrindBot**
```bash
pip install -e .
```

**2. Get a Kie.ai API key**

Go to [kie.ai](https://kie.ai), create an account, and copy your API key.

**3. Set the key in `~/.env`**

GrindBot reads `~/.env` in your home directory (not the project folder):

```bash
# Add to ~/.env (create the file if it doesn't exist)
KIE_API_KEY=your_key_here
```

> **Note:** Without `KIE_API_KEY`, GrindBot still runs but falls back to basic Gemini prompts — no AI-powered task planning, diff review, or PR descriptions. Setting the key is strongly recommended.
