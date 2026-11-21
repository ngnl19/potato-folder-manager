from pathlib import Path

import typer

from potato import __version__
from potato.app import PotatoApp
from potato.updater import (
    UpdateError,
    find_current_pipx_environment,
    get_latest_release,
    install_release,
    is_newer_version,
)

DEFAULT_ROOT = Path.home()

START_PATH_OPTION = typer.Option(
    DEFAULT_ROOT,
    "--path",
    "-p",
    exists=True,
    file_okay=False,
    dir_okay=True,
    readable=True,
    resolve_path=True,
    help="Starting directory for Potato.",
)

app = typer.Typer(
    name="potato",
    help="A secure, local-first interactive terminal file browser.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def callback() -> None:
    """Manage the Potato CLI."""


@app.command()
def start(path: Path = START_PATH_OPTION) -> None:
    """Launch the interactive Potato file browser."""
    PotatoApp(root=path).run()


@app.command()
def update() -> None:
    """Check for and install the latest stable Potato release."""
    typer.echo("Checking for updates...")

    try:
        latest = get_latest_release()
    except UpdateError as error:
        typer.secho(f"Update check failed: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from error

    typer.echo(f"\nCurrent version: {__version__}")
    typer.echo(f"Latest version:  {latest.version}")

    if not is_newer_version(latest.version, __version__):
        if latest.version == __version__:
            typer.secho(
                f"\nPotato is already up to date ({__version__}).",
                fg=typer.colors.GREEN,
            )
        else:
            typer.secho(
                f"\nYour installed version ({__version__}) is newer than "
                f"the latest stable release ({latest.version}).",
                fg=typer.colors.YELLOW,
            )
        return

    environment = find_current_pipx_environment()
    if environment is None:
        typer.secho(
            "\nAutomatic updates are currently supported only for "
            "writable pipx-managed installations.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=1)

    confirmed = typer.confirm(
        f"\nUpdate Potato from {__version__} to {latest.version}?",
        default=False,
    )
    if not confirmed:
        typer.echo("Update cancelled. Potato was not changed.")
        return

    typer.echo(f"\nInstalling Potato {latest.version}...")
    try:
        install_release(latest)
    except UpdateError as error:
        typer.secho(f"Update failed: {error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from error

    typer.secho(
        f"\nSuccessfully updated Potato to {latest.version}.",
        fg=typer.colors.GREEN,
    )
    typer.echo("Run Potato again to use the updated version.")


def main() -> None:
    app()
