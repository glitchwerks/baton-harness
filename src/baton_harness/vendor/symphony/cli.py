"""symphony/cli.py — CLI entry point."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys

import click

from . import __version__  # VENDOR-PATCH: relative import for vendoring


@click.group()
@click.version_option(version=__version__)
def main():
    """Baton — autonomous coding agent orchestrator."""
    pass


@main.command()
@click.option("--workflow", "-w", default="WORKFLOW.md", help="Path to WORKFLOW.md")
@click.option("--verbose", "-v", is_flag=True, help="Verbose logging")
def start(workflow: str, verbose: bool):
    """Start the Baton orchestrator in the current directory."""
    from .config import ConfigError, load_workflow  # VENDOR-PATCH: relative import for vendoring
    from .log import setup_logging  # VENDOR-PATCH: relative import for vendoring
    from .orchestrator import Orchestrator  # VENDOR-PATCH: relative import for vendoring

    setup_logging(verbose=verbose)

    project_root = os.getcwd()
    workflow_path = os.path.join(project_root, workflow) if not os.path.isabs(workflow) else workflow

    try:
        config = load_workflow(workflow_path)
    except ConfigError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    symphony_dir = os.path.join(project_root, ".symphony")
    state_path = os.path.join(symphony_dir, "state.json")

    orch = Orchestrator(
        config=config,
        project_root=project_root,
        state_path=state_path,
        workflow_path=workflow_path,
    )

    click.echo(f"Baton v{__version__} — watching {os.path.basename(project_root)}")
    click.echo(
        f"Polling every {config.poll_interval_ms // 1000}s | "
        f"Max {config.max_concurrent} concurrent agents"
    )
    click.echo()

    loop = asyncio.new_event_loop()

    def handle_signal(sig, frame):
        click.echo("\nShutting down...")
        orch.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        loop.run_until_complete(orch.run())
    finally:
        loop.close()


@main.command()
def status():
    """Show current orchestrator status."""
    state_path = os.path.join(os.getcwd(), ".symphony", "state.json")

    if not os.path.exists(state_path):
        click.echo("No Baton instance found in this directory.")
        click.echo("Run 'baton start' first.")
        sys.exit(1)

    with open(state_path) as f:
        data = json.load(f)

    click.echo(f"Baton — {os.path.basename(os.getcwd())}")
    click.echo()

    running = data.get("running", [])
    if running:
        click.echo(f"Running ({len(running)} agents):")
        for r in running:
            import time
            elapsed = time.time() - r.get("started_at", time.time())
            mins, secs = divmod(int(elapsed), 60)
            click.echo(
                f"  #{r['issue_number']:>4}  {r['title']:<40} "
                f"turn {r['turn']}/{r['max_turns']}  {mins}m{secs:02d}s"
            )
    else:
        click.echo("No running agents.")

    retrying = data.get("retrying", [])
    if retrying:
        click.echo(f"\nRetrying ({len(retrying)}):")
        for r in retrying:
            click.echo(
                f"  #{r['issue_number']:>4}  attempt {r['attempt']}  "
                f"error: {r.get('error', 'unknown')}"
            )

    completed = data.get("completed_count", 0)
    click.echo(f"\nCompleted this session: {completed}")


@main.command()
def stop():
    """Stop the running orchestrator (sends SIGTERM to the PID in state)."""
    click.echo("Use Ctrl+C in the baton start terminal, or kill the process.")
