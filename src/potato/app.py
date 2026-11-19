from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from potato.security import (
    FileEntry,
    canonical_root,
    is_within_root,
    list_directory,
    read_preview,
)


class BrowserItem(ListItem):
    """A selectable file or directory."""

    def __init__(self, entry: FileEntry) -> None:
        self.entry = entry

        icon = "📁" if entry.is_dir else "📄"

        super().__init__(
            Label(Text(f"{icon}  {entry.name}"))
        )


class PotatoApp(App):
    """Interactive local-first file browser."""

    TITLE = "Potato CLI"
    SUB_TITLE = "Local-first file browser"

    CSS = """
    Screen {
        layout: vertical;
    }

    #location {
        height: 2;
        padding: 0 1;
        background: $panel;
    }

    #main {
        height: 1fr;
        layout: horizontal;
        padding: 0 1;
    }

    #file-list {
        width: 2fr;
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }

    #preview {
        width: 3fr;
        height: 1fr;
        border: round $secondary;
        padding: 1;
        overflow-y: auto;
    }

    #search {
        margin: 0 1;
        height: 3;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("ctrl+f", "focus_search", "Search"),
        ("backspace", "go_parent", "Parent"),
        ("escape", "clear_search", "Clear"),
    ]

    def __init__(self, root: Path | None = None) -> None:
        super().__init__()

        self.root = canonical_root(root or Path.home())
        self.current = self.root

        self._entries: list[FileEntry] = []

    def compose(self) -> ComposeResult:
        yield Header()

        yield Static(
            Text(""),
            id="location",
        )

        yield Horizontal(
            ListView(id="file-list"),
            Static(
                Text("Select a file to preview its contents."),
                id="preview",
            ),
            id="main",
        )

        yield Input(
            placeholder="Search in this directory...",
            id="search",
        )

        yield Footer()

    async def on_mount(self) -> None:
        await self.refresh_directory()

        self.query_one("#file-list", ListView).focus()

    async def refresh_directory(self) -> None:
        """Reload the current directory."""

        location = self.query_one("#location", Static)

        location.update(
            Text(
                f"Root: {self.root}  |  Current: {self.current}"
            )
        )

        try:
            self._entries = list_directory(
                self.current,
                self.root,
            )

        except (OSError, RuntimeError) as error:
            self._entries = []

            self.query_one("#preview", Static).update(
                Text(f"Unable to access directory:\n{error}")
            )

        search = self.query_one("#search", Input)

        await self.render_entries(search.value)

    async def render_entries(self, query: str = "") -> None:
        """Display entries matching the current search."""

        search_term = query.casefold().strip()

        visible = [
            entry
            for entry in self._entries
            if search_term in entry.name.casefold()
        ]

        file_list = self.query_one("#file-list", ListView)

        await file_list.clear()

        await file_list.extend(
            BrowserItem(entry)
            for entry in visible
        )

        if visible:
            file_list.index = 0
            self.update_preview(visible[0])
        else:
            self.query_one("#preview", Static).update(
                Text("No matching files or folders.")
            )

    
    async def on_input_changed(self, event: Input.Changed) -> None:
        """Update visible entries when the search changes."""

        if event.input.id != "search":
            return

        await self.render_entries(event.value)

    def on_list_view_highlighted(
        self,
        event: ListView.Highlighted,
    ) -> None:
        """Update the preview when selection changes."""

        if isinstance(event.item, BrowserItem):
            self.update_preview(event.item.entry)

    def update_preview(self, entry: FileEntry) -> None:
        """Show metadata and a safe file preview."""

        kind = "Directory" if entry.is_dir else "File"

        modified = datetime.fromtimestamp(
            entry.modified
        ).astimezone().strftime("%Y-%m-%d %H:%M:%S")

        details = [
            entry.name,
            "",
            f"Type: {kind}",
            f"Path: {entry.path}",
            f"Size: {self.format_size(entry.size)}",
            f"Modified: {modified}",
        ]

        if entry.is_dir:
            details.extend([
                "",
                "Press Enter to open this directory.",
            ])
        else:
            details.extend([
                "",
                "-------------------- PREVIEW --------------------",
                "",
            ])

            try:
                preview, truncated = read_preview(
                    entry.path,
                    self.root,
                )

                details.append(preview)

                if truncated:
                    details.extend([
                        "",
                        "[Preview truncated at 64 KB.]",
                    ])

            except (OSError, RuntimeError, ValueError) as error:
                details.append(
                    f"Preview unavailable: {error}"
                )

        self.query_one("#preview", Static).update(
            Text("\n".join(details))
        )

    async def on_list_view_selected(
        self,
        event: ListView.Selected,
    ) -> None:
        """Open a selected directory."""

        if not isinstance(event.item, BrowserItem):
            return

        entry = event.item.entry

        if not entry.is_dir:
            return

        if not is_within_root(entry.path, self.root):
            self.query_one("#preview", Static).update(
                Text("Security boundary: access blocked.")
            )
            return

        try:
            self.current = entry.path.resolve(strict=True)
            await self.refresh_directory()

        except (OSError, RuntimeError) as error:
            self.query_one("#preview", Static).update(
                Text(f"Unable to open directory:\n{error}")
            )

        self.query_one("#file-list", ListView).focus()

    async def action_go_parent(self) -> None:
        """Navigate to the parent without leaving the root."""

        # Do not interfere with normal Backspace editing in search.
        if isinstance(self.focused, Input):
            return

        if self.current == self.root:
            return

        parent = self.current.parent

        if not is_within_root(parent, self.root):
            return

        self.current = parent

        await self.refresh_directory()

        self.query_one("#file-list", ListView).focus()

    def action_focus_search(self) -> None:
        """Focus the search field."""

        self.query_one("#search", Input).focus()

    def action_clear_search(self) -> None:
        """Clear search or return focus to the file list."""

        search = self.query_one("#search", Input)

        if search.value:
            search.value = ""
        else:
            self.query_one("#file-list", ListView).focus()

    @staticmethod
    def format_size(size: int) -> str:
        """Convert byte sizes into readable units."""

        value = float(size)

        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.1f} {unit}"

            value /= 1024

        return f"{value:.1f} TB"
