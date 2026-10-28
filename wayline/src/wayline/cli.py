from pathlib import Path
import json

import click
import uvicorn

from wayline.dispatcher.logging import configure_logging
from wayline.dispatcher.scheduler.loop import DispatcherLoop
from wayline.server import db
from wayline.creation_cli import project, workflow


@click.group()
def main():
    """Wayline - Fact-graph based collaborative exploration protocol."""


main.add_command(project)
main.add_command(workflow)


@main.command()
@click.option("--runtime-dir", type=click.Path(path_type=Path), required=True, help="New per-instance directory; existing configuration is never overwritten.")
def init(runtime_dir: Path):
    """Create a local Pi configuration and runtime paths without touching credentials."""
    from wayline.doctor import initialize
    try:
        result = initialize(runtime_dir)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(json.dumps(result, indent=2))


@main.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path), help="Optional dispatcher configuration to validate.")
def doctor(config_path: Path | None):
    """Check offline installation and native Pi readiness without model calls."""
    from wayline.doctor import readiness
    result = readiness(config_path)
    click.echo(json.dumps(result, indent=2))
    if result['status'] == 'needs_setup':
        raise click.exceptions.Exit(1)


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host")
@click.option("--port", default=8000, show_default=True, help="Bind port")
@click.option(
    "--db-path",
    envvar="WAYLINE_DB_PATH",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
@click.option("--log-level", default="info", show_default=True, help="Uvicorn log level")
@click.option("--access-log/--no-access-log", default=True, show_default=True, help="Enable Uvicorn access log")
def serve(host: str, port: int, db_path: str, log_level: str, access_log: bool):
    """Start the Wayline API server."""
    db.configure(Path(db_path))
    from wayline.server.app import app

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=access_log,
    )


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--once", is_flag=True, help="Run one scheduling iteration and exit")
@click.option(
    "--startup-healthcheck-only",
    is_flag=True,
    help="Run startup worker healthchecks and exit",
)
@click.option("--log-level", default="INFO", show_default=True, help="Log level")
def dispatch(config_path: Path, once: bool, startup_healthcheck_only: bool, log_level: str):
    """Run the Wayline dispatcher."""
    configure_logging(log_level, bare=startup_healthcheck_only)
    loop = DispatcherLoop(config_path)
    try:
        if startup_healthcheck_only:
            loop.run_startup_healthchecks_only()
            return
        loop.run(once=once)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
