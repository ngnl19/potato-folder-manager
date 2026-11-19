
import typer

app = typer.Typer(
    name="potato",
    help="A secure, local-first interactive terminal file browser.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def callback() -> None:
    """Manage the Potato CLI."""
    pass


@app.command()
def start() -> None:
    """Launch the interactive Potato file browser."""
    from potato.app import PotatoApp

    PotatoApp().run()


def main() -> None:
    app()
