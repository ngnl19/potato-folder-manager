
from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Static


class PotatoApp(App):
    """Main Potato terminal application."""

    TITLE = "Potato CLI"
    SUB_TITLE = "Local-first file browser"

    CSS = """
    Screen {
        layout: vertical;
    }

    #welcome {
        width: 80%;
        height: auto;
        border: round $primary;
        padding: 2;
        margin: 2;
        align: center middle;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()

        yield Static(
            "[b]Welcome to Potato![/b]\n\n"
            "Your interactive file browser is starting.\n\n"
            "Upcoming features:\n"
            "- Browse directories\n"
            "- Navigate using arrow keys\n"
            "- Preview file contents\n"
            "- Search files and folders\n\n"
            "Press Q to quit.",
            id="welcome",
        )

        yield Footer()
