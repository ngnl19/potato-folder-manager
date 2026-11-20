from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from threading import Event

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import Input, ListItem, ListView, Static

from potato.security import (
    FileEntry,
    canonical_root,
    is_within_root,
    list_directory,
    read_preview,
    search_files,
    sort_entries,
)


def copy_to_system_clipboard(text: str, app: App) -> str:
    """Copy text through an available clipboard provider."""
    providers: list[tuple[str, list[str], list[str]]] = []

    wl_copy = shutil.which("wl-copy")
    wl_paste = shutil.which("wl-paste")
    xclip = shutil.which("xclip")
    xsel = shutil.which("xsel")

    if wl_copy and wl_paste:
        providers.append(
            ("Wayland", [wl_copy], [wl_paste, "--no-newline"])
        )

    if xclip:
        providers.append(
            (
                "X11",
                [xclip, "-selection", "clipboard", "-in"],
                [xclip, "-selection", "clipboard", "-out"],
            )
        )

    if xsel:
        providers.append(
            ("X11", [xsel, "--clipboard", "--input"],
             [xsel, "--clipboard", "--output"])
        )

    errors: list[str] = []

    for provider, copy_command, paste_command in providers:
        try:
            subprocess.run(
                copy_command,
                input=text,
                text=True,
                capture_output=True,
                timeout=5,
                check=True,
            )
            verification = subprocess.run(
                paste_command,
                text=True,
                capture_output=True,
                timeout=5,
                check=True,
            )
            if verification.stdout == text:
                return provider
            errors.append(f"{provider}: clipboard content did not match")
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(f"{provider}: {error}")

    # OSC 52 is only a best-effort fallback. Do not claim it was verified.
    is_vte = bool(
        os.environ.get("VTE_VERSION")
        or os.environ.get("GNOME_TERMINAL_SERVICE")
    )
    if not is_vte and not providers:
        app.copy_to_clipboard(text)
        return "OSC52-unverified"

    if errors:
        raise RuntimeError(
            "Clipboard operation failed: " + "; ".join(errors)
        )

    raise RuntimeError(
        "No supported system clipboard provider was found. "
        "Use wl-copy on Wayland or xclip/xsel on X11."
    )


class SearchCompleted(Message):
    """Message carrying the result of one background search."""

    def __init__(
        self,
        request_id: int,
        query: str,
        results: list[FileEntry],
        error: str | None = None,
    ) -> None:
        super().__init__()
        self.request_id = request_id
        self.query = query
        self.results = results
        self.error = error


class BrowserItem(ListItem):
    """A selectable filesystem entry."""

    def __init__(self, entry: FileEntry, root: Path) -> None:
        self.entry = entry
        icon = "[DIR] " if entry.is_dir else "[FILE] "

        try:
            relative = entry.path.relative_to(root)
            parent = str(relative.parent)
            label = (
                f"{icon}{entry.name}"
                if parent == "."
                else f"{icon}{entry.name}  ({parent})"
            )
        except ValueError:
            label = f"{icon}{entry.name}"

        super().__init__(Static(Text(label)))


class PreviewPane(VerticalScroll):
    """Keyboard-scrollable file preview panel."""

    can_focus = True


class PotatoApp(App):
    """Keyboard-first local filesystem browser."""

    TITLE = "Potato"
    ENABLE_COMMAND_PALETTE = False
    SORT_MODES = ("name", "size", "modified")

    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("escape", "cancel_search", "Cancel search"),
        ("backspace", "go_parent", "Parent"),
        ("f2", "toggle_hidden", "Hidden"),
        ("f3", "cycle_sort", "Sort"),
        ("f4", "focus_search", "Search here"),
        ("f5", "global_search", "Search all"),
        ("f6", "copy_current_directory", "Copy path"),
    ]

    CSS = """
    Screen {
        layout: vertical;
        padding: 0 1;
    }

    #status {
        height: 2;
        padding: 0 1;
        background: $panel;
    }

    #search {
        display: none;
        height: 3;
        margin: 0 0 1 0;
    }

    #main {
        height: 1fr;
        layout: horizontal;
    }

    #file-list {
        width: 45%;
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }

    #preview-pane {
        width: 55%;
        height: 1fr;
        border: round $secondary;
        padding: 1;
        overflow-y: auto;
    }

    #preview-content {
        width: 100%;
        height: auto;
    }

    #help {
        height: 2;
        padding: 0 1;
        background: $panel;
    }
    """

    def __init__(self, root: Path | None = None) -> None:
        super().__init__()
        self.root = canonical_root(root or Path.home())
        self.current = self.root
        self.include_hidden = False
        self.sort_by = "name"
        self._entries: list[FileEntry] = []

        # Search state must be initialized on the App, not SearchCompleted.
        self._search_request_id = 0
        self._search_cancel_event: Event | None = None
        self.search_all = False
        self._last_search_query = ""
        self._last_search_base = self.current

    def compose(self) -> ComposeResult:
        yield Static("", id="status")
        yield Input(
            placeholder=(
                "Enter a filename fragment, then press Enter to search."
            ),
            id="search",
        )
        with Horizontal(id="main"):
            yield ListView(id="file-list")
            with PreviewPane(id="preview-pane"):
                yield Static(
                    "Select a file or folder to inspect it.",
                    id="preview-content",
                )
        yield Static(
            "↑↓ Navigate | Enter Open | Backspace Parent | "
            "Tab Preview | "
            "F2 Hidden | F3 Sort | "
            "F4 Search here | F5 Search all | "
            "F6 Copy path | Ctrl+Q Quit",
            id="help",
        )

    async def on_mount(self) -> None:
        await self.refresh_directory()
        self.query_one("#file-list", ListView).focus()

    def update_status(self, detail: str = "") -> None:
        """Render the current location and browsing options."""
        hidden = "ON" if self.include_hidden else "OFF"
        message = (
            f"POTATO | {self.current} | "
            f"Hidden: {hidden} | Sort: {self.sort_by}"
        )
        if detail:
            message += f" | {detail}"
        self.query_one("#status", Static).update(Text(message))

    async def refresh_directory(self) -> None:
        """Reload the current directory."""
        if not is_within_root(self.current, self.root):
            self.current = self.root

        try:
            self._entries = list_directory(
                self.current,
                self.root,
                include_hidden=self.include_hidden,
            )
            self._entries = sort_entries(self._entries, self.sort_by)
        except (OSError, RuntimeError, ValueError) as error:
            self._entries = []
            self.query_one("#preview-content", Static).update(
                Text(f"Directory access failed:\n{error}")
            )
            return

        self.update_status()
        await self.render_entries(self._entries)

    async def render_entries(self, entries: list[FileEntry]) -> None:
        """Render directory entries or search results."""
        file_list = self.query_one("#file-list", ListView)
        await file_list.clear()

        if not entries:
            self.query_one("#preview-content", Static).update(
                Text("No matching files or folders.")
            )
            return

        items = [BrowserItem(entry, self.root) for entry in entries]
        await file_list.extend(items)
        file_list.index = 0
        self.update_preview(entries[0])

    # ------------------------------ Search ------------------------------

    def _invalidate_search(self) -> None:
        """Invalidate and cooperatively stop the previous search."""
        self._search_request_id += 1
        if self._search_cancel_event is not None:
            self._search_cancel_event.set()
            self._search_cancel_event = None

    def _start_search(self, query: str, search_base: Path) -> None:
        """Start a background search with a fresh cancellation token."""
        self._invalidate_search()
        request_id = self._search_request_id
        cancel_event = Event()
        self._search_cancel_event = cancel_event
        self._last_search_query = query
        self._last_search_base = search_base
        self.update_status(f"Searching: {search_base}")
        self.query_one("#preview-content", Static).update(
            Text(f"Searching for: {query}")
        )
        self.run_search_worker(
            query,
            search_base,
            self.root,
            self.include_hidden,
            self.sort_by,
            request_id,
            cancel_event,
        )

    def action_focus_search(self) -> None:
        """Open a search scoped to the current directory."""
        self.search_all = False
        search = self.query_one("#search", Input)
        search.placeholder = (
            "Search a file in this directory and its subdirectories; press Enter."
        )
        search.value = ""
        search.styles.display = "block"
        search.focus()

    def action_global_search(self) -> None:
        """Open a search scoped to the configured root."""
        self.search_all = True
        search = self.query_one("#search", Input)
        search.placeholder = "Search a file in the entire Potato root; press Enter."
        search.value = ""
        search.styles.display = "block"
        search.focus()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Run a search only after Enter is pressed."""
        if event.input.id != "search":
            return
        query = event.value.strip()
        if not query:
            await self.action_cancel_search()
            return
        search_base = self.root if self.search_all else self.current
        event.input.styles.display = "none"
        self.query_one("#file-list", ListView).focus()
        self._start_search(query, search_base)

    async def action_cancel_search(self) -> None:
        """Cancel search and restore the current directory listing."""
        self._invalidate_search()
        self._last_search_query = ""
        search = self.query_one("#search", Input)
        search.value = ""
        search.styles.display = "none"
        await self.refresh_directory()
        self.query_one("#file-list", ListView).focus()

    @work(thread=True, group="recursive-search", exit_on_error=False)
    def run_search_worker(
        self,
        query: str,
        search_base: Path,
        root: Path,
        include_hidden: bool,
        sort_by: str,
        request_id: int,
        cancel_event: Event,
    ) -> None:
        """Search off the UI thread and post one completion message."""
        try:
            results = search_files(
                search_base,
                root,
                query,
                include_hidden=include_hidden,
                cancel_event=cancel_event,
            )
            if cancel_event.is_set():
                return
            results = sort_entries(results, sort_by)
            message = SearchCompleted(request_id, query, results)
        except Exception as error:
            if cancel_event.is_set():
                return
            message = SearchCompleted(
                request_id,
                query,
                [],
                str(error),
            )

        try:
            # Textual UI/message operations must be marshalled to its loop.
            self.call_from_thread(self.post_message, message)
        except RuntimeError:
            # The app may have closed before the scan completed.
            return

    async def on_search_completed(self, event: SearchCompleted) -> None:
        """Display results only if this is still the latest search."""
        if event.request_id != self._search_request_id:
            return
        self._search_cancel_event = None
        if event.error:
            self.query_one("#preview-content", Static).update(
                Text(f"Search failed:\n{event.error}")
            )
            self.update_status("Search failed")
            return
        self.update_status(
            f"{len(event.results)} results for '{event.query}'"
        )
        await self.render_entries(event.results)

    # ------------------------------ Preview -----------------------------

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Preview the highlighted entry."""
        if isinstance(event.item, BrowserItem):
            self.update_preview(event.item.entry)

    def update_preview(self, entry: FileEntry) -> None:
        """Display metadata and a bounded text preview."""
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
            details.extend(["", "Press Enter to open this directory."])
        else:
            details.extend(["", "---------------- PREVIEW ----------------", ""])
            try:
                preview, truncated = read_preview(entry.path, self.root)
                details.append(preview)
                if truncated:
                    details.extend(["", "[Preview limited to 64 KB.]"])
            except (OSError, RuntimeError, ValueError) as error:
                details.append(f"Preview unavailable: {error}")

        self.query_one("#preview-content", Static).update(
            Text("\n".join(details))
        )
        self.query_one("#preview-pane", PreviewPane).scroll_y = 0

    # ----------------------------- Navigation ---------------------------

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Open a selected directory, including a directory search result."""
        if not isinstance(event.item, BrowserItem):
            return
        entry = event.item.entry
        if not entry.is_dir:
            return
        if not is_within_root(entry.path, self.root):
            self.notify(
                "Access outside the browsing root is blocked.",
                severity="warning",
            )
            return

        self._invalidate_search()
        self._last_search_query = ""
        search = self.query_one("#search", Input)
        search.value = ""
        search.styles.display = "none"
        try:
            self.current = entry.path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            self.notify(f"Unable to open directory: {error}", severity="error")
            return
        await self.refresh_directory()
        self.query_one("#file-list", ListView).focus()

    async def action_go_parent(self) -> None:
        """Navigate to the parent without leaving the configured root."""
        if isinstance(self.focused, Input):
            return
        if self.current == self.root:
            return
        parent = self.current.parent
        if not is_within_root(parent, self.root):
            return
        self._invalidate_search()
        self._last_search_query = ""
        search = self.query_one("#search", Input)
        search.value = ""
        search.styles.display = "none"
        self.current = parent
        await self.refresh_directory()
        self.query_one("#file-list", ListView).focus()

    # ---------------------------- Key actions ---------------------------

    async def action_toggle_hidden(self) -> None:
        """Toggle hidden entries and refresh the current view/search."""
        if isinstance(self.focused, Input):
            return
        self.include_hidden = not self.include_hidden
        if self._last_search_query:
            self._start_search(
                self._last_search_query,
                self._last_search_base,
            )
        else:
            await self.refresh_directory()
        self.notify(
            f"Hidden files {'shown' if self.include_hidden else 'hidden'}.",
            timeout=2,
        )

    async def action_cycle_sort(self) -> None:
        """Cycle sorting by name, size, and modification time."""
        if isinstance(self.focused, Input):
            return
        index = self.SORT_MODES.index(self.sort_by)
        self.sort_by = self.SORT_MODES[(index + 1) % len(self.SORT_MODES)]
        if self._last_search_query:
            self._start_search(
                self._last_search_query,
                self._last_search_base,
            )
        else:
            await self.refresh_directory()
        self.notify(f"Sorted by {self.sort_by}.", timeout=2)

    def action_copy_current_directory(self) -> None:
        """Copy the current directory path and report provider status."""
        try:
            provider = copy_to_system_clipboard(str(self.current), self)
        except RuntimeError as error:
            self.notify(str(error), severity="error", timeout=6)
            return
        if provider == "OSC52-unverified":
            self.notify(
                "Clipboard request sent; terminal support is unverified.",
                severity="warning",
                timeout=4,
            )
        else:
            self.notify(
                f"Directory path copied and verified ({provider}).",
                severity="information",
                timeout=3,
            )

    @staticmethod
    def format_size(size: int) -> str:
        """Format bytes into readable file sizes."""
        value = float(size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} TB"
