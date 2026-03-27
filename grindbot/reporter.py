"""Reporter: Rich formatted terminal output for all GrindBot user-facing displays."""
from typing import Any

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

_SEVERITY_STYLE: dict[str, str] = {
    "high": "bold red",
    "medium": "yellow",
    "low": "dim white",
}

_CATEGORY_STYLE: dict[str, str] = {
    "bug": "red",
    "security": "magenta",
    "performance": "cyan",
    "style": "blue",
}

_STATUS_STYLE: dict[str, str] = {
    "pending": "yellow",
    "completed": "bold green",
    "failed": "bold red",
}


def show_scan_results(tasks: list[dict[str, Any]], project_path: str) -> None:
    """Display scan results as a Rich table with severity, category, file, and title columns.

    Should be called after planner.plan() has assigned IDs.
    """
    count = len(tasks)
    noun = "issue" if count == 1 else "issues"

    console.print()
    console.print(
        Panel(
            f"[bold]Found [cyan]{count}[/cyan] {noun}[/bold] in [dim]{project_path}[/dim]",
            title="[bold green]GrindBot Scan Results[/bold green]",
            border_style="green",
        )
    )

    if not tasks:
        console.print("[green]No issues found — codebase looks clean![/green]")
        console.print()
        return

    table = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold white",
        border_style="dim",
        expand=True,
        padding=(0, 1),
    )
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Sev", width=7)
    table.add_column("Category", width=13)
    table.add_column("File", style="dim cyan", max_width=32, no_wrap=True)
    table.add_column("Title")

    for task in tasks:
        sev = task.get("severity", "low")
        cat = task.get("category", "style")
        file_str = task.get("file") or "-"
        line = task.get("line")
        if line:
            file_str = f"{file_str}:{line}"

        table.add_row(
            task.get("id", "?"),
            f"[{_SEVERITY_STYLE.get(sev, 'white')}]{sev}[/]",
            f"[{_CATEGORY_STYLE.get(cat, 'white')}]{cat}[/]",
            file_str,
            task.get("title", "") or "-",
        )

    console.print(table)
    console.print(
        "[dim]Run [bold]grindbot grind[/bold] to fix these issues autonomously.[/dim]"
    )
    console.print()


def show_grind_report(tasks: list[dict[str, Any]], project_path: str) -> None:
    """Display a grind session report with status, severity, title, and branch/error columns.

    Works at any point in the pipeline — pending tasks show in yellow,
    completed in green, failed in red.
    """
    if not tasks:
        console.print(
            "[yellow]No tasks found. "
            "Run [bold]grindbot scan <path>[/bold] first.[/yellow]"
        )
        console.print()
        return

    pending = sum(1 for t in tasks if t.get("status") == "pending")
    completed = sum(1 for t in tasks if t.get("status") == "completed")
    failed = sum(1 for t in tasks if t.get("status") == "failed")

    summary = (
        f"[bold green]{completed} completed[/bold green]  "
        f"[bold red]{failed} failed[/bold red]  "
        f"[yellow]{pending} pending[/yellow]"
    )

    console.print()
    console.print(
        Panel(
            summary,
            title="[bold cyan]GrindBot Report[/bold cyan]",
            subtitle=f"[dim]{project_path}[/dim]",
            border_style="cyan",
        )
    )

    table = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold white",
        border_style="dim",
        expand=True,
        padding=(0, 1),
    )
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Status", width=11)
    table.add_column("Sev", width=7)
    table.add_column("Title")
    table.add_column("Branch / Error", style="dim", max_width=38)

    for task in tasks:
        status = task.get("status", "pending")
        sev = task.get("severity", "low")
        detail = task.get("branch") or task.get("error") or "-"

        table.add_row(
            task.get("id", "?"),
            f"[{_STATUS_STYLE.get(status, 'white')}]{status}[/]",
            f"[{_SEVERITY_STYLE.get(sev, 'white')}]{sev}[/]",
            task.get("title", ""),
            detail,
        )

    console.print(table)
    console.print()
