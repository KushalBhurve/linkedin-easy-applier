import os
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import argparse
import logging
from pathlib import Path
import yaml
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from models import AppConfig
from browser import BrowserManager
from tracker import ApplicationTracker
from search import LinkedInJobSearcher
from llm_helper import answer_screening_question

console = Console(highlight=False)
logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")

CONFIG_PATH = Path(__file__).parent / "config.yaml"

def load_config() -> AppConfig:
    if not CONFIG_PATH.exists():
        console.print(f"[bold red]Config file not found at {CONFIG_PATH}[/bold red]")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return AppConfig(**data)

def cmd_login():
    """Opens browser for one-time manual login and preserves session cookies."""
    config = load_config()
    console.print(Panel("[bold cyan]LinkedIn Easy Apply - Authentication Setup[/bold cyan]\n"
                        "This will launch the Chromium browser and verify your session.\n"
                        "Your session is stored in .browser_profile and stays persistent across runs.",
                        border_style="cyan"))

    browser_mgr = BrowserManager(
        headless=False,
        min_delay=config.execution.min_delay_sec,
        max_delay=config.execution.max_delay_sec
    )
    try:
        browser_mgr.start()
        success = browser_mgr.ensure_login()
        if success:
            console.print("[bold green]✨ Authentication verified! You are ready to run the agent.[/bold green]")
        else:
            console.print("[bold red]Login was not completed. Please try again.[/bold red]")
    finally:
        browser_mgr.close()

def cmd_run(args):
    """Executes the search and auto-apply process."""
    config = load_config()

    # CLI Overrides
    if args.role:
        config.search.roles = [args.role]
    if args.location:
        config.search.location = args.location
    if args.limit:
        config.execution.max_applications_per_run = int(args.limit)
    if args.live:
        config.execution.dry_run = False
    elif args.dry_run:
        config.execution.dry_run = True
    if args.headless:
        config.execution.headless = True

    tracker = ApplicationTracker()

    mode_banner = (
        "[bold cyan]🔍 DRY RUN MODE ACTIVATED[/bold cyan] (Forms will be filled and validated, but NOT submitted)"
        if config.execution.dry_run
        else "[bold red]⚠️  LIVE SUBMISSION MODE ACTIVATED[/bold red] (Applications will be submitted to LinkedIn!)"
    )

    locs_display = ", ".join(config.search.locations) if config.search.locations else config.search.location
    console.print(Panel(
        f"[bold white]LinkedIn Easy Apply Agent[/bold white]\n"
        f"{mode_banner}\n\n"
        f"Candidate: [bold]{config.candidate.full_name}[/bold] ({config.candidate.current_title})\n"
        f"Locations: [bold]{locs_display}[/bold] | Roles: [bold]{', '.join(config.search.roles)}[/bold]\n"
        f"Applications Cap: [bold]{config.execution.max_applications_per_run}[/bold] | Headless: [bold]{config.execution.headless}[/bold]",
        border_style="blue"
    ))

    browser_mgr = BrowserManager(
        headless=config.execution.headless,
        min_delay=config.execution.min_delay_sec,
        max_delay=config.execution.max_delay_sec
    )

    try:
        page = browser_mgr.start()
        if not browser_mgr.ensure_login():
            console.print("[bold red]Cannot proceed without an active LinkedIn session. Run 'python main.py login' first.[/bold red]")
            return

        searcher = LinkedInJobSearcher(page, browser_mgr, config, tracker)
        searcher.run()

    except KeyboardInterrupt:
        console.print("\n[yellow]Session interrupted by user (Ctrl+C). Exiting cleanly.[/yellow]")
    except Exception as e:
        if any(term in str(e).lower() for term in ["browser has been closed", "target page", "context closed"]):
            console.print("\n[yellow]Browser window was closed. Session ended cleanly.[/yellow]")
        else:
            console.print(f"\n[red]Session stopped with error: {e}[/red]")
    finally:
        browser_mgr.close()

def cmd_stats():
    """Displays tracking database metrics and past history."""
    tracker = ApplicationTracker()
    stats = tracker.get_stats()

    table = Table(title="LinkedIn Easy Apply - Application Statistics", border_style="cyan")
    table.add_column("Status", style="bold yellow")
    table.add_column("Count", justify="right", style="bold green")

    for status, count in stats["breakdown"].items():
        table.add_row(status, str(count))

    console.print(table)
    console.print(f"\n[bold]Total Records in Database:[/bold] {stats['total_records']}")
    console.print(f"[bold]Completed Today:[/bold] {stats['today_count']}")

def cmd_test_ai(args):
    """Sanity test for Ollama / Groq question resolution."""
    config = load_config()
    test_q = args.question or "How many years of work experience do you have with LangGraph?"
    
    # Auto-infer field type if not specified
    field_type = args.type
    if not field_type:
        q_low = test_q.lower()
        if any(term in q_low for term in ["how many", "years", "number", "ctc", "salary"]):
            field_type = "numeric"
        elif any(term in q_low for term in ["are you", "do you", "will you", "is it", "have you", "comfortable"]):
            field_type = "boolean"
        else:
            field_type = "text"

    console.print(f"[cyan]Testing AI Screening Fallback...[/cyan]")
    console.print(f"Question: [yellow]{test_q}[/yellow]")
    console.print(f"Field Type: [yellow]{field_type}[/yellow]")
    console.print(f"Provider: [yellow]{config.ai_fallback.provider}[/yellow]")
    
    ans = answer_screening_question(
        question=test_q,
        field_type=field_type,
        options=["Yes", "No"] if field_type == "boolean" else None,
        candidate=config.candidate,
        ai_config=config.ai_fallback
    )
    console.print(f"[bold green]Resolved Answer:[/bold green] '{ans}'")

def main():
    parser = argparse.ArgumentParser(description="LinkedIn Easy Apply Autonomous Agent")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # Command: run
    p_run = subparsers.add_parser("run", help="Run the search and application agent")
    p_run.add_argument("--role", type=str, help="Target role override (e.g. 'AI Engineer')")
    p_run.add_argument("--location", type=str, help="Target location override (e.g. 'Bengaluru')")
    p_run.add_argument("--limit", type=int, help="Max applications to process this run")
    p_run.add_argument("--live", action="store_true", help="Enable LIVE submission (disables dry-run)")
    p_run.add_argument("--dry-run", action="store_true", help="Enable dry-run mode (default)")
    p_run.add_argument("--headless", action="store_true", help="Run browser in headless background")

    # Command: login
    subparsers.add_parser("login", help="Open browser to log into LinkedIn and store persistent session")

    # Command: stats
    subparsers.add_parser("stats", help="View application stats and history from local SQLite DB")

    # Command: test-ai
    p_test = subparsers.add_parser("test-ai", help="Test AI question resolution with Ollama/Groq")
    p_test.add_argument("--question", type=str, help="Sample question text")
    p_test.add_argument("--type", type=str, choices=["numeric", "boolean", "choice", "text"], help="Field type")

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "login":
        cmd_login()
    elif args.command == "stats":
        cmd_stats()
    elif args.command == "test-ai":
        cmd_test_ai(args)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
