from pathlib import Path

import typer

from potato.app import PotatoApp

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


def main() -> None:
    app()
